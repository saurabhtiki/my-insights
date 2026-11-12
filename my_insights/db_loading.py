"""Load tables from a database: an uploaded SQLite file, or a live Postgres / MySQL server
(`db_connect`). The same steps for both:

1. List: the tables and views are listed and wait in the workspace as a "pending database"
   (a SQLite file stays in memory, never on disk, read-only; a server is not kept open).
2. Import: each ticked table (and a server's own query) goes through the same text → type
   checks as a CSV (`write_raw` + `_build_dataset`), so dates, leading-zero codes and mixed
   columns behave the same. All or nothing. Foreign keys between the imported tables become
   link suggestions. Server datasets keep a `DbSource`, so Refresh can read them again.
"""

import datetime
import json
import logging
import sqlite3
from contextlib import contextmanager

import duckdb

from . import conf, db_connect, query_dataset
from .db_connect import fetch_batches
from .loading import (
    RAW_TABLE,
    UploadRejected,
    _build_dataset,
    _excel_cell_text,
    quote_name,
    write_raw,
)
from .workspace import Dataset, DbSource, DbTable, PendingDb, Workspace

logger = logging.getLogger(__name__)

DB_TYPES = {".db", ".sqlite", ".sqlite3"}
SQLITE_HEADER = b"SQLite format 3\x00"

__all__ = ["DB_TYPES", "DbTable", "load_tables", "open_database", "refresh_from_server"]


def open_database(workspace: Workspace, file_name: str, data: bytes | memoryview) -> PendingDb:
    """Open the file in memory, list its tables and keep it in the workspace until Import.
    `data` may be the upload's own buffer (no extra copy)."""
    if not conf.get("ALLOW_DB_IMPORT"):
        raise UploadRejected("Database files can't be uploaded here (switched off by the admin).")
    if not len(data):
        raise UploadRejected("The file is empty.")
    if bytes(data[: len(SQLITE_HEADER)]) != SQLITE_HEADER:
        raise UploadRejected(
            "This is not a SQLite database file. Only SQLite (.db, .sqlite, .sqlite3) is supported."
        )
    # check_same_thread=False: later requests (other threads) use it, always under workspace.lock
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    try:
        connection.deserialize(data)
        connection.execute("PRAGMA query_only = ON")  # read-only: nothing can be changed
        tables = _list_tables(connection)
    except sqlite3.DatabaseError as error:
        connection.close()
        logger.warning("Unreadable SQLite file %s: %s", file_name, error)
        raise UploadRejected(
            "The database file could not be read. It may be damaged or encrypted."
        ) from error
    if not tables:
        connection.close()
        raise UploadRejected("The database has no tables.")
    return add_pending(workspace, file_name, tables, connection=connection)


def add_pending(workspace, file_name, tables, connection=None, server=None) -> PendingDb:
    """Keep the pick list in the workspace. The same file or server again replaces the old."""
    with workspace.lock:
        for token, pending in list(workspace.pending_databases.items()):
            same_server = (pending.server and pending.server.place) == (server and server.place)
            if pending.file_name == file_name and same_server:
                workspace.drop_pending(token)
        return workspace.add_pending(file_name, connection, tables, server)


def _list_tables(connection) -> list[DbTable]:
    names = connection.execute(
        "SELECT name, type FROM sqlite_master WHERE type IN ('table', 'view') "
        "AND name NOT LIKE 'sqlite_%' ORDER BY type, name"
    ).fetchall()
    tables = []
    for name, kind in names:
        select = f"SELECT * FROM {quote_name(name)}"
        try:
            columns = [column[0] for column in connection.execute(f"{select} LIMIT 0").description]
            row_count = connection.execute(f"SELECT count(*) FROM {quote_name(name)}").fetchone()[0]
        except sqlite3.DatabaseError as error:  # e.g. a view on a missing table
            logger.info("Skipped %s %s: %s", kind, name, error)
            continue
        tables.append(DbTable(name, kind, row_count, columns, select))
    return tables


def cell_text(value) -> str:
    """A database value as the text the type checks read, the same way as an Excel cell
    (a date-time to the second: the wall-clock time the server sent, its zone dropped)."""
    if isinstance(value, bytes | bytearray | memoryview):
        return f"(binary, {len(value):,} bytes)"
    if isinstance(value, dict | list):  # JSON / array columns
        return json.dumps(value, ensure_ascii=False, default=str)
    if isinstance(value, datetime.datetime | datetime.time):
        value = value.replace(tzinfo=None)
    return _excel_cell_text(value)


class SqliteReader:
    """Reads an uploaded SQLite file (see db_connect.ServerReader for a server)."""

    errors = (sqlite3.DatabaseError,)
    too_many_hint = " Split it and try again."

    def __init__(self, connection):
        self.connection = connection

    def rows(self, select: str):
        cursor = self.connection.execute(select)
        return [column[0] for column in cursor.description], fetch_batches(cursor)

    def message(self, error) -> str:
        return "It could not be read from the database file."

    def foreign_keys(self, table_names) -> list[tuple[str, str, str, str]]:
        """[(orders, customer_id, customers, id)]; keys over several columns are left out."""
        keys = []
        for table_name in table_names:
            pragma = f"PRAGMA foreign_key_list({quote_name(table_name)})"
            groups: dict[int, list] = {}
            for row in self.connection.execute(pragma).fetchall():  # id, seq, table, from, to
                groups.setdefault(row[0], []).append(row)
            for parts in groups.values():
                if len(parts) == 1:
                    _, _, ref_table, column, ref_column = parts[0][:5]
                    ref_column = ref_column or self._primary_key(ref_table)
                    keys.append((table_name, column, ref_table, ref_column))
        return keys

    def _primary_key(self, table_name: str) -> str | None:
        """`REFERENCES customers` with no column means the primary key."""
        pragma = f"PRAGMA table_info({quote_name(table_name)})"
        keys = [row[1] for row in self.connection.execute(pragma) if row[5]]
        return keys[0] if len(keys) == 1 else None


@contextmanager
def _reader(pending: PendingDb):
    if pending.server is None:
        yield SqliteReader(pending.connection)
    else:
        with db_connect.reading(pending.server) as reader:
            yield reader


def load_tables(
    workspace: Workspace, token: str, picked: list[str], query_name: str = "", query_sql: str = ""
) -> list[Dataset]:
    """Import the ticked tables (and, for a server, the own query) of a pending database, all
    or nothing. It is dropped after a successful import; on failure it stays, so the user can
    untick and retry."""
    with workspace.lock:
        pending = workspace.pending_databases.get(token)
        if pending is None:
            raise UploadRejected("This database is no longer waiting. Connect or upload it again.")
        query_sql = query_sql.strip() if pending.server else ""
        tables = [t for t in pending.tables if t.name in set(picked)]
        if not tables and not query_sql:
            or_query = ", or write a query," if pending.server else ""
            raise UploadRejected(f"Tick at least one table{or_query} to import.")
        max_rows = conf.get("MAX_ROWS")
        for table in tables:  # an exact count (a SQLite file) can be refused before reading
            if not table.estimated and (table.row_count or 0) > max_rows:
                raise UploadRejected(f"Table “{table.name}”: {_too_many(max_rows, '')}")
        several = pending.server is not None or len(pending.tables) > 1
        jobs = [
            _Job(
                f"Table “{t.name}”",
                f"{pending.stem} – {t.name}" if several else pending.stem,
                f"{pending.file_name} › {t.name}",
                t.select,
                t.name,
            )
            for t in tables
        ]
        if query_sql:
            select = db_connect.check_select(query_sql)
            name = " ".join(query_name.split())[:100] or f"{pending.stem} – query"
            jobs.append(_Job(f"Query “{name}”", name, f"{pending.file_name} › {name}", select))
        with _reader(pending) as reader:
            datasets = _read_jobs(workspace, reader, jobs, pending.file_name)
        if pending.server is not None:
            for job, dataset in zip(jobs, datasets, strict=True):
                dataset.db_source = DbSource(pending.server, job.select)
        workspace.drop_pending(token)
    return datasets


class _Job:
    """One dataset to read: a table, or a server's own query."""

    def __init__(self, what: str, name: str, label: str, select: str, table: str = ""):
        self.what, self.name, self.label, self.select, self.table = what, name, label, select, table


def _read_jobs(workspace: Workspace, reader, jobs: list[_Job], source_name: str) -> list[Dataset]:
    """Read every job into a dataset, all or nothing. Call while holding `workspace.lock`."""
    datasets: list[Dataset] = []
    try:
        for job in jobs:
            datasets.append(_read_one(workspace, reader, job))
        by_table = {job.table: d for job, d in zip(jobs, datasets, strict=True) if job.table}
        _attach_foreign_keys(by_table, reader.foreign_keys(list(by_table)))
    except duckdb.OutOfMemoryException as error:
        logger.warning("Out of memory loading %s: %s", source_name, error)
        _undo(workspace, datasets)
        raise UploadRejected(
            "These tables are too large for the memory allowed per user. "
            "Tick fewer tables, or remove other datasets first."
        ) from error
    except BaseException:
        _undo(workspace, datasets)
        raise
    finally:
        workspace.connection.execute(f"DROP TABLE IF EXISTS {RAW_TABLE}")
        workspace.refresh_memory()
    return datasets


def _read_one(workspace: Workspace, reader, job: _Job) -> Dataset:
    try:
        columns, batches = reader.rows(job.select)
        if not columns:
            raise UploadRejected("It has no columns.")
        rows = _numbered_rows(columns, batches, reader.too_many_hint)
        write_raw(workspace.connection, len(columns), rows)
        return _build_dataset(workspace, job.name, job.label, excel_first_column=None)
    except reader.errors as error:
        logger.warning("Could not read %s: %s", job.label, error)
        raise UploadRejected(f"{job.what}: {reader.message(error)}") from error
    except UploadRejected as error:
        raise UploadRejected(f"{job.what}: {error}") from error


def _too_many(max_rows: int, hint: str) -> str:
    return f"It has more than {max_rows:,} data rows (the limit).{hint}"


def _numbered_rows(columns: list[str], batches, hint: str):
    """(row number, cells) with the header as row 1; stops past MAX_ROWS."""
    max_rows = conf.get("MAX_ROWS")
    yield 1, columns
    row_no = 1
    try:
        for batch in batches:
            for row in batch:
                row_no += 1
                if row_no - 1 > max_rows:
                    raise UploadRejected(_too_many(max_rows, hint))
                yield row_no, [cell_text(value) for value in row]
    finally:
        batches.close()  # closes the cursor now, while the connection is still open


def _undo(workspace: Workspace, datasets: list[Dataset]) -> None:
    for dataset in datasets:
        workspace.remove_dataset(dataset.dataset_id)
    datasets.clear()


def _attach_foreign_keys(by_table: dict[str, Dataset], keys) -> None:
    """orders.customer_id → customers.id becomes (customer_id, customers' id, id) on orders,
    when both tables were imported and both columns were kept."""
    for table, column, ref_table, ref_column in keys:
        dataset, lookup = by_table.get(table), by_table.get(ref_table)
        if dataset is None or lookup is None or not ref_column:
            continue
        if column in {c.name for c in dataset.columns} and ref_column in {
            c.name for c in lookup.columns
        }:
            dataset.foreign_keys.append((column, lookup.dataset_id, ref_column))


def refresh_from_server(workspace: Workspace, dataset: Dataset) -> list[str]:
    """Read a server dataset again; its id and name stay, so queries and charts follow.
    Raises UploadRejected (the earlier data is kept). Returns notes for the user."""
    source = dataset.db_source
    job = _Job(f"“{dataset.name}”", f"{dataset.name} (new)", dataset.source_file, source.select)
    with db_connect.reading(source.server) as reader, workspace.lock:
        [fresh] = _read_jobs(workspace, reader, [job], dataset.name)
        return query_dataset.swap_in_fresh(workspace, [(dataset, fresh)], [fresh])
