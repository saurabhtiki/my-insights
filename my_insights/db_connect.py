"""Read from a live PostgreSQL or MySQL server ("From database" on the home page).

- Drivers are optional extras: `psycopg` (uv add "my-insights[postgres]") and `PyMySQL`
  (uv add "my-insights[mysql]"). A missing one gives a clear message, not a crash.
- Every read runs in a READ ONLY transaction with a time limit, so nothing on the server can
  be changed, even by an own query. A read-only database user is still the safest.
- Nothing is kept open: each step (Connect, Import, Refresh) connects and closes again.
- The password is never logged (DbServer's repr hides it) and never saved.
"""

import itertools
import logging
import os
import re
import ssl
from contextlib import contextmanager

from . import conf
from .loading import UploadRejected
from .workspace import DbServer, DbTable

logger = logging.getLogger(__name__)

FETCH_ROWS = 20_000
MAX_QUERY_CHARS = 20_000
_cursor_numbers = itertools.count(1)


def fetch_batches(cursor):
    """Rows in batches, then the cursor is closed."""
    try:
        while batch := cursor.fetchmany(FETCH_ROWS):
            yield batch
    finally:
        cursor.close()


def _first_line(error) -> str:
    return (str(error).strip().splitlines() or ["unknown error"])[0][:300]


def _display(schema: str, table: str, default_schema: str) -> str:
    """orders in the default schema; sales.orders elsewhere."""
    return table if schema == default_schema else f"{schema}.{table}"


class Postgres:
    kind, title, extra, default_port = "postgres", "PostgreSQL", "postgres", 5432

    def load(self):
        import psycopg

        self.driver = psycopg
        self.errors = (psycopg.Error,)

    def quote(self, name: str) -> str:
        return '"' + name.replace('"', '""') + '"'

    def connect(self, server: DbServer):
        connection = self.driver.connect(
            host=server.host,
            port=server.port,
            dbname=server.database,
            user=server.user,
            password=server.password,
            connect_timeout=conf.get("DB_CONNECT_SECONDS"),
            sslmode="require" if server.require_ssl else "prefer",
            application_name="my-insights",
            passfile=os.devnull,  # never a password from this server's own ~/.pgpass
        )
        try:
            connection.read_only = True  # every transaction is READ ONLY
            milliseconds = str(int(conf.get("DB_QUERY_SECONDS")) * 1000)
            connection.execute("SELECT set_config('statement_timeout', %s, false)", [milliseconds])
        except BaseException:
            connection.close()
            raise
        return connection

    def stream(self, connection, select: str):
        cursor = connection.cursor(name=f"my_insights_{next(_cursor_numbers)}")
        cursor.execute(select)  # a server-side cursor: rows come in batches
        return cursor

    def tables(self, connection, server: DbServer) -> list[DbTable]:
        rows = connection.execute(
            "SELECT n.nspname, c.relname, c.relkind, c.reltuples::bigint, "
            "array(SELECT a.attname::text FROM pg_attribute a WHERE a.attrelid = c.oid "
            "AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum) "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f') AND NOT c.relispartition "
            "AND n.nspname <> 'information_schema' AND n.nspname NOT LIKE 'pg\\_%' "
            "AND has_schema_privilege(n.oid, 'USAGE') AND has_table_privilege(c.oid, 'SELECT') "
            "ORDER BY n.nspname <> 'public', n.nspname, c.relname"
        ).fetchall()
        tables = []
        for schema, name, relkind, estimate, columns in rows:
            is_view = relkind in ("v", "m")
            row_count = None if is_view or estimate < 0 else estimate  # -1 = never analysed
            tables.append(
                DbTable(
                    _display(schema, name, "public"),
                    "view" if is_view else "table",
                    row_count,
                    list(columns),
                    f"SELECT * FROM {self.quote(schema)}.{self.quote(name)}",
                    estimated=True,
                )
            )
        return tables

    def foreign_keys(self, connection, server: DbServer) -> list[tuple[str, str, str, str]]:
        rows = connection.execute(
            "SELECT cn.nspname, cl.relname, a.attname, fn.nspname, fl.relname, fa.attname "
            "FROM pg_constraint k "
            "JOIN pg_class cl ON cl.oid = k.conrelid "
            "JOIN pg_namespace cn ON cn.oid = cl.relnamespace "
            "JOIN pg_class fl ON fl.oid = k.confrelid "
            "JOIN pg_namespace fn ON fn.oid = fl.relnamespace "
            "JOIN pg_attribute a ON a.attrelid = k.conrelid AND a.attnum = k.conkey[1] "
            "JOIN pg_attribute fa ON fa.attrelid = k.confrelid AND fa.attnum = k.confkey[1] "
            "WHERE k.contype = 'f' AND cardinality(k.conkey) = 1"
        ).fetchall()
        return [
            (_display(s, t, "public"), column, _display(rs, rt, "public"), ref_column)
            for s, t, column, rs, rt, ref_column in rows
        ]

    def message(self, error, server: DbServer) -> str:
        errors = self.driver.errors
        text = str(error)
        lowered = text.lower()
        seconds = conf.get("DB_QUERY_SECONDS")
        if isinstance(error, errors.QueryCanceled):
            return f"It ran longer than {seconds} seconds and was stopped. Read fewer rows, e.g. add a WHERE."
        if isinstance(error, errors.ReadOnlySqlTransaction):
            return "Only reading is allowed here: this query tries to change data."
        if isinstance(error, errors.InsufficientPrivilege):
            return f"The user “{server.user}” may not read this (permission denied)."
        if "password authentication failed" in lowered or (
            "role" in lowered and "does not exist" in lowered
        ):
            return "The user name or password is wrong."
        if "database" in lowered and "does not exist" in lowered:
            return f"There is no database “{server.database}” on this server."
        if "pg_hba.conf" in lowered:
            return (
                "The server does not let this user connect from here (its pg_hba.conf rules). "
                "Ask the database admin, or try ticking Require SSL."
            )
        if "server does not support ssl" in lowered:
            return "This server does not support SSL. Untick Require SSL."
        return _network_message(lowered, server) or f"The database said: {_first_line(text)}"


class MySql:
    kind, title, extra, default_port = "mysql", "MySQL", "mysql", 3306
    SYSTEM = "('mysql', 'information_schema', 'performance_schema', 'sys')"

    def load(self):
        import pymysql
        import pymysql.cursors

        self.driver = pymysql
        self.errors = (pymysql.Error,)

    def quote(self, name: str) -> str:
        return "`" + name.replace("`", "``") + "`"

    def connect(self, server: DbServer):
        context = None
        if server.require_ssl:  # encrypted, without checking the certificate (like Postgres)
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        connection = self.driver.connect(
            host=server.host,
            port=server.port,
            user=server.user,
            password=server.password,
            database=server.database,
            charset="utf8mb4",
            connect_timeout=conf.get("DB_CONNECT_SECONDS"),
            read_timeout=conf.get("DB_QUERY_SECONDS"),
            ssl=context,
        )
        seconds = int(conf.get("DB_QUERY_SECONDS"))
        try:
            with connection.cursor() as cursor:
                for setting in (
                    f"SET SESSION max_execution_time = {seconds * 1000}",  # MySQL
                    f"SET SESSION max_statement_time = {seconds}",  # MariaDB
                ):
                    try:
                        cursor.execute(setting)
                    except self.driver.Error:
                        pass  # the other kind of server: this setting is not known there
                cursor.execute("START TRANSACTION READ ONLY")
        except BaseException:
            connection.close()
            raise
        return connection

    def _all(self, connection, sql: str) -> list[tuple]:
        with connection.cursor() as cursor:
            cursor.execute(sql)
            return list(cursor.fetchall())

    def stream(self, connection, select: str):
        cursor = connection.cursor(self.driver.cursors.SSCursor)  # rows come in batches
        cursor.execute(select)
        return cursor

    def tables(self, connection, server: DbServer) -> list[DbTable]:
        columns: dict[tuple[str, str], list[str]] = {}
        for schema, table, column in self._all(
            connection,
            "SELECT table_schema, table_name, column_name FROM information_schema.columns "
            f"WHERE table_schema NOT IN {self.SYSTEM} "
            "ORDER BY table_schema, table_name, ordinal_position",
        ):
            columns.setdefault((schema, table), []).append(column)
        tables = []
        for schema, name, table_type, estimate in self._all(
            connection,
            "SELECT table_schema, table_name, table_type, table_rows "
            f"FROM information_schema.tables WHERE table_schema NOT IN {self.SYSTEM} "
            "ORDER BY table_schema <> DATABASE(), table_schema, table_name",
        ):
            is_view = "VIEW" in table_type
            tables.append(
                DbTable(
                    _display(schema, name, server.database),
                    "view" if is_view else "table",
                    None if is_view or estimate is None else int(estimate),
                    columns.get((schema, name), []),
                    f"SELECT * FROM {self.quote(schema)}.{self.quote(name)}",
                    estimated=True,
                )
            )
        return tables

    def foreign_keys(self, connection, server: DbServer) -> list[tuple[str, str, str, str]]:
        rows = self._all(
            connection,
            "SELECT table_schema, table_name, constraint_name, column_name, "
            "referenced_table_schema, referenced_table_name, referenced_column_name "
            "FROM information_schema.key_column_usage WHERE referenced_table_name IS NOT NULL",
        )
        groups: dict[tuple, list] = {}
        for row in rows:
            groups.setdefault(row[:3], []).append(row)
        keys = []
        for parts in groups.values():
            if len(parts) == 1:  # keys over several columns are left to the normal suggestions
                schema, table, _, column, ref_schema, ref_table, ref_column = parts[0]
                keys.append(
                    (
                        _display(schema, table, server.database),
                        column,
                        _display(ref_schema, ref_table, server.database),
                        ref_column,
                    )
                )
        return keys

    def message(self, error, server: DbServer) -> str:
        code = error.args[0] if error.args and isinstance(error.args[0], int) else None
        text = str(error.args[1]) if len(error.args) > 1 else str(error)
        seconds = conf.get("DB_QUERY_SECONDS")
        known = {
            1045: "The user name or password is wrong.",
            1044: f"The user “{server.user}” may not open the database “{server.database}”.",
            1049: f"There is no database “{server.database}” on this server.",
            1142: f"The user “{server.user}” may not read this (permission denied).",
            1792: "Only reading is allowed here: this query tries to change data.",
            3024: f"It ran longer than {seconds} seconds and was stopped. Read fewer rows, e.g. add a WHERE.",
            1969: f"It ran longer than {seconds} seconds and was stopped. Read fewer rows, e.g. add a WHERE.",
            3159: "This server only accepts encrypted connections. Tick Require SSL.",
        }
        if code in known:
            return known[code]
        if code in (2003, 2013, 2006):
            return _network_message(text.lower(), server) or (
                f"The connection to {server.host} was lost or took longer than {seconds} seconds."
            )
        return f"The database said: {_first_line(text)}"


def _network_message(lowered: str, server: DbServer) -> str:
    """The usual "can't reach it" problems, in plain words ("" when it is none of them)."""
    if any(
        hint in lowered
        for hint in (
            "could not translate host",
            "name or service not known",
            "getaddrinfo",
            "nodename nor servname",
            "no such host",
            "unknown host",
        )
    ):
        return f"The server “{server.host}” could not be found. Check the host name."
    if "refused" in lowered:
        return (
            f"Nothing answered at {server.host}, port {server.port}. "
            "Check the port and that the database server is running."
        )
    if "timeout" in lowered or "timed out" in lowered:
        return (
            f"{server.host} did not answer within {conf.get('DB_CONNECT_SECONDS')} seconds. "
            "Check the host, the port and the firewall."
        )
    return ""


DIALECTS = {"postgres": Postgres(), "mysql": MySql()}


def server_from_form(data) -> DbServer:
    """The connection form's fields → DbServer. Raises UploadRejected for a missing or bad
    field (before anything is sent)."""
    kind = data.get("kind", "postgres")
    if kind not in DIALECTS:
        raise UploadRejected("Choose PostgreSQL or MySQL.")
    host = data.get("host", "").strip()
    # Only a plain name or IP: no socket path (/var/run/postgresql) or host list (a,b), which
    # could reach this server's own database without a password.
    if not host or any(character.isspace() or character in "/\\,=@" for character in host):
        raise UploadRejected("Type the server's host name or IP, e.g. 192.168.1.20 or erp.local.")
    port_text = str(data.get("port", "")).strip() or str(DIALECTS[kind].default_port)
    if not port_text.isdigit() or not 0 < int(port_text) < 65536:
        raise UploadRejected("The port must be a number, e.g. 5432 for PostgreSQL, 3306 for MySQL.")
    database, user = data.get("database", "").strip(), data.get("user", "").strip()
    if not database:
        raise UploadRejected("Type the database name, e.g. erp.")
    if not user:
        raise UploadRejected("Type the database user, e.g. report.")
    if not data.get("password"):  # an empty one may let the driver use this server's own login
        raise UploadRejected("Type the user's password.")
    return DbServer(
        kind=kind,
        host=host,
        port=int(port_text),
        database=database,
        user=user,
        password=data.get("password", ""),
        require_ssl=bool(data.get("require_ssl")),
    )


def _dialect_for(server: DbServer):
    if not conf.get("ALLOW_DB_CONNECT"):
        raise UploadRejected("Connecting to a database is switched off on this server.")
    allowed = {host.casefold() for host in conf.get("DB_ALLOWED_HOSTS")}
    if allowed and server.host.casefold() not in allowed:
        raise UploadRejected(
            f"The server “{server.host}” is not on this app's list of allowed databases. "
            "Ask the admin to add it to DB_ALLOWED_HOSTS."
        )
    dialect = DIALECTS[server.kind]
    try:
        dialect.load()
    except ImportError as error:
        logger.warning("%s driver missing: %s", dialect.title, error)
        raise UploadRejected(
            f"{dialect.title} support is not installed on this server. Ask the admin to run: "
            f'uv add "my-insights[{dialect.extra}]"'
        ) from error
    return dialect


class ServerReader:
    """One open, read-only connection (see db_loading.SqliteReader for a SQLite file)."""

    too_many_hint = " Use an Own query with a WHERE to read fewer rows."

    def __init__(self, dialect, connection, server: DbServer):
        self.dialect, self.connection, self.server = dialect, connection, server
        self.errors = dialect.errors

    def rows(self, select: str):
        cursor = self.dialect.stream(self.connection, select)
        return [column[0] for column in cursor.description], fetch_batches(cursor)

    def message(self, error) -> str:
        return self.dialect.message(error, self.server)

    def foreign_keys(self, table_names) -> list[tuple[str, str, str, str]]:
        wanted = set(table_names)
        if not wanted:  # e.g. Refresh or only an own query: no need to ask the server
            return []
        return [
            key
            for key in self.dialect.foreign_keys(self.connection, self.server)
            if key[0] in wanted
        ]


@contextmanager
def reading(server: DbServer):
    """Connect (read-only) and close again. Connection problems become UploadRejected."""
    dialect = _dialect_for(server)
    try:
        connection = dialect.connect(server)
    except dialect.errors as error:
        logger.info("Could not connect to %s (%s): %s", server.label, dialect.title, error)
        raise UploadRejected(dialect.message(error, server)) from error
    try:
        yield ServerReader(dialect, connection, server)
    finally:
        try:
            connection.close()
        except dialect.errors as error:
            logger.info("Closing %s failed: %s", server.label, error)


def list_tables(server: DbServer) -> list[DbTable]:
    """The tables and views the user may read, with the server's row estimates."""
    with reading(server) as reader:
        try:
            tables = reader.dialect.tables(reader.connection, server)
        except reader.errors as error:
            logger.info("Listing tables on %s failed: %s", server.label, error)
            raise UploadRejected(reader.message(error)) from error
    if not tables:
        raise UploadRejected(
            f"No tables found that the user “{server.user}” may read in “{server.database}”."
        )
    return tables


_FIRST_WORD = re.compile(r"(?:\s+|--[^\n]*(?:\n|$)|/\*.*?\*/|\()*(\w+)", re.DOTALL)


def check_select(sql: str) -> str:
    """One SELECT (or WITH … SELECT) only, checked before it is sent. The read-only
    transaction is the real guard; this gives a clear message early."""
    text = sql.strip().rstrip(";").strip()
    if not text:
        raise UploadRejected("Write a query, e.g. SELECT * FROM orders WHERE city = 'Pune'.")
    if len(text) > MAX_QUERY_CHARS:
        raise UploadRejected(f"The query is longer than {MAX_QUERY_CHARS:,} characters.")
    if ";" in text:
        raise UploadRejected("Write one query only (no ; between two queries).")
    first = _FIRST_WORD.match(text)
    if not first or first.group(1).lower() not in ("select", "with"):
        raise UploadRejected(
            "Only a SELECT query can run here (it may start with WITH). It can't change data."
        )
    return text
