"""Read JSON (a .json / .jsonl file, or an API's reply) into datasets.

Nested data is flattened, e.g. one order:
    {"id": 1, "customer": {"name": "Ravi"}, "tags": ["new", "vip"],
     "items": [{"sku": "A1", "qty": 2}, {"sku": "B7", "qty": 1}]}
becomes
    orders:          _row=1, id=1, customer.name=Ravi, tags="new, vip"
    orders – items:  _parent_row=1, sku=A1, qty=2  and  _parent_row=1, sku=B7, qty=1
Each table is then written as text to the raw table and typed exactly like a CSV
(loading._build_dataset), so dates, codes and mixed columns behave the same.

A reply with several lists, e.g. {"data": {"orders": [...], "shops": [...]}}, gives one
dataset per list ("sales – orders", "sales – shops"), unless "Rows at" names one of them.
"""

import json
import logging
from dataclasses import dataclass, field

import duckdb

from . import conf, query_dataset
from .loading import (
    RAW_TABLE,
    UploadRejected,
    _build_dataset,
    _too_many_rows_message,
    write_raw,
)
from .workspace import PARENT_COLUMN, ROW_COLUMN, ApiSource, Dataset, Workspace

logger = logging.getLogger(__name__)

MAX_DEPTH = 3  # customer.address.city; anything deeper is kept as JSON text
SEARCH_DEPTH = 5  # how deep the automatic search for the rows looks inside an object
NOT_IN_REPLY = "Not in the latest API reply, so it keeps its earlier data."


@dataclass
class JsonTable:
    path: str  # where the rows were ("data.orders", "" = top level); "items" for a child list
    columns: list[str]
    rows: list[list[str]]


@dataclass
class ParsedJson:
    """One list of rows and the child lists inside its rows."""

    rows_at: str  # where the rows were found, e.g. "data.orders" ("" = the top level)
    main: JsonTable
    children: list[JsonTable] = field(default_factory=list)


# ---------- reading the JSON ----------


def parse_json(data: bytes, lines: bool = False, rows_at: str = "") -> list[ParsedJson]:
    """JSON bytes → flat tables, one ParsedJson per list of rows found. Raises UploadRejected
    with a message naming the problem."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise UploadRejected(
            "This JSON is not UTF-8 text. Save it as UTF-8 and try again."
        ) from error
    if not text.strip():
        raise UploadRejected("The file is empty.")
    if lines:
        found = [("", _read_lines(text))]
    else:
        try:
            document = json.loads(text)
        except json.JSONDecodeError as error:
            raise UploadRejected(
                f"This is not valid JSON (line {error.lineno}, column {error.colno}: {error.msg})."
            ) from error
        found = _find_rows(document, rows_at.strip())
    max_rows = conf.get("MAX_ROWS")
    for found_at, records in found:
        if not records:
            raise UploadRejected("The JSON has no rows (the list is empty).")
        if len(records) > max_rows:
            where = f"The list “{found_at}”: " if len(found) > 1 else ""
            raise UploadRejected(f"{where}{_too_many_rows_message(max_rows)}")
    return [_flatten_all(records, found_at) for found_at, records in found]


def _read_lines(text: str) -> list:
    """JSON Lines: one record per line; blank lines are skipped."""
    records = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as error:
            raise UploadRejected(
                f"Line {line_no} is not valid JSON ({error.msg}). In a .jsonl file every "
                "line must be one complete record."
            ) from error
    return records


def _find_rows(document, rows_at: str) -> list[tuple[str, list]]:
    """[(where, records)] to load: the list at `rows_at`, else the top-level list, else every
    list of objects inside the object, else the object itself as one row."""
    if rows_at:
        node = document
        for part in rows_at.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
                node = node[int(part)]
            else:
                raise UploadRejected(
                    f"No rows found at “{rows_at}”: there is no “{part}” there. "
                    "Check the spelling, or leave “Rows at” empty to find them automatically."
                )
        if isinstance(node, list):
            return [(rows_at, node)]
        if isinstance(node, dict):
            return [(rows_at, [node])]
        raise UploadRejected(f"“{rows_at}” holds a single value, not rows.")
    if isinstance(document, list):
        return [("", document)]
    if isinstance(document, dict):
        return _lists_of_objects(document, "", 1) or [("", [document])]
    raise UploadRejected("The JSON holds a single value, not rows.")


def _lists_of_objects(node: dict, prefix: str, depth: int) -> list[tuple[str, list]]:
    """[(path, list)] of every list of objects inside an object, in the file's order. Lists
    inside those lists' rows are not included: they become child datasets."""
    found = []
    for key, value in node.items():
        path = f"{prefix}{key}"
        if isinstance(value, list) and value and all(isinstance(v, dict) for v in value):
            found.append((path, value))
        elif isinstance(value, dict) and depth < SEARCH_DEPTH:
            found += _lists_of_objects(value, f"{path}.", depth + 1)
    return found


# ---------- flattening ----------


def cell_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() and abs(value) < 1e15 else repr(value)
    if isinstance(value, dict | list):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _flatten(record: dict, prefix: str, depth: int, out: dict, children: dict | None, row_no):
    """One record → out {column: text}. Lists of objects go to `children` {path: [(row, item)]}
    (None inside a child: only one level of children)."""
    for key, value in record.items():
        name = f"{prefix}{key or '(no name)'}"
        if isinstance(value, dict):
            if value and depth < MAX_DEPTH:
                _flatten(value, f"{name}.", depth + 1, out, children, row_no)
            else:
                out[name] = cell_text(value) if value else ""
        elif isinstance(value, list):
            if not value:
                out[name] = ""
            elif all(not isinstance(item, dict | list) for item in value):
                out[name] = ", ".join(cell_text(item) for item in value if item is not None)
            elif children is not None and all(isinstance(item, dict) for item in value):
                children.setdefault(name, []).extend((row_no, item) for item in value)
            else:
                out[name] = cell_text(value)
        else:
            out[name] = cell_text(value)


def _as_record(item) -> dict:
    return item if isinstance(item, dict) else {"value": item}


def _table(path: str, flat_rows: list[dict], first_column: str = "") -> JsonTable:
    columns: dict[str, None] = {first_column: None} if first_column else {}
    for row in flat_rows:
        columns.update(dict.fromkeys(row))
    names = list(columns)
    return JsonTable(path, names, [[row.get(name, "") for name in names] for row in flat_rows])


def _flatten_all(records: list, found_at: str) -> ParsedJson:
    children: dict[str, list] = {}
    main_rows = []
    for row_no, item in enumerate(records, start=1):
        out: dict[str, str] = {}
        _flatten(_as_record(item), "", 1, out, children, row_no)
        main_rows.append(out)
    if children:
        for row_no, out in enumerate(main_rows, start=1):
            out[ROW_COLUMN] = str(row_no)
    child_tables = []
    max_rows = conf.get("MAX_ROWS")
    for path, items in children.items():
        if len(items) > max_rows:
            raise UploadRejected(f"The list “{path}”: {_too_many_rows_message(max_rows)}")
        flat = []
        for parent_row, item in items:
            out = {PARENT_COLUMN: str(parent_row)}
            _flatten(item, "", 1, out, None, parent_row)
            flat.append(out)
        child_tables.append(_table(path, flat, PARENT_COLUMN))
    main = _table(found_at, main_rows, ROW_COLUMN if children else "")
    return ParsedJson(found_at, main, child_tables)


# ---------- into datasets ----------


def load_json(
    workspace: Workspace, name: str, label: str, data: bytes, lines: bool = False
) -> list[Dataset]:
    """Load a JSON file: the main dataset first, then one per child list."""
    return load_parsed(workspace, name, label, parse_json(data, lines=lines))


def list_name(name: str, rows_at: str, several: bool) -> str:
    """The dataset name for one list: "sales", or "sales – orders" when there are several."""
    return f"{name} – {rows_at.rsplit('.', 1)[-1]}" if several and rows_at else name


def load_parsed(
    workspace: Workspace, name: str, label: str, groups: list[ParsedJson]
) -> list[Dataset]:
    """Make the datasets for parsed JSON, all or nothing: each list, followed by its children."""
    several = len(groups) > 1
    datasets: list[Dataset] = []
    with workspace.lock:
        try:
            for parsed in groups:
                main_name = list_name(name, parsed.rows_at, several)
                main_label = f"{label} › {parsed.rows_at}" if parsed.rows_at else label
                main = _table_to_dataset(workspace, parsed.main, main_name, main_label)
                main.list_path = parsed.rows_at
                datasets.append(main)
                for table in parsed.children:
                    child = _table_to_dataset(
                        workspace,
                        table,
                        f"{main_name} – {table.path}",
                        f"{main_label} › {table.path}",
                    )
                    child.parent_id, child.child_path = main.dataset_id, table.path
                    datasets.append(child)
        except BaseException:
            for dataset in reversed(datasets):
                workspace.datasets.pop(dataset.dataset_id, None)
                workspace.connection.execute(f"DROP TABLE IF EXISTS {dataset.table_name}")
            raise
        finally:
            workspace.connection.execute(f"DROP TABLE IF EXISTS {RAW_TABLE}")
            workspace.refresh_memory()
    return datasets


def _table_to_dataset(workspace: Workspace, table: JsonTable, name: str, label: str) -> Dataset:
    width = len(table.columns)
    if width == 0:
        raise UploadRejected(f"{_where(table)}has no fields, only empty objects.")

    def numbered_rows():
        yield 1, table.columns
        yield from enumerate(table.rows, start=2)

    try:
        write_raw(workspace.connection, width, numbered_rows())
        return _build_dataset(workspace, name, label, excel_first_column=None)
    except UploadRejected as error:
        if table.path:
            raise UploadRejected(f"{_where(table)}{error}") from error
        raise
    except duckdb.OutOfMemoryException as error:
        logger.warning("Out of memory loading JSON %s: %s", label, error)
        raise UploadRejected(
            "This data is too large for the memory allowed per user. "
            "Try fewer rows, or remove other datasets first."
        ) from error
    finally:
        workspace.connection.execute(f"DROP TABLE IF EXISTS {RAW_TABLE}")


def _where(table: JsonTable) -> str:
    return f"The list “{table.path}”: " if table.path else "The JSON "


# ---------- refreshing an API dataset ----------


def refresh_parsed(
    workspace: Workspace, source: ApiSource, groups: list[ParsedJson], label: str
) -> list[str]:
    """Put newly fetched data into every dataset loaded from `source` and their children,
    keeping their ids and names so queries, charts and dashboards keep working.
    Returns notes for the user."""
    notes: list[str] = []
    missing: list[Dataset] = []
    with workspace.lock:
        old_mains = {d.list_path: d for d in workspace.datasets.values() if d.api_source is source}
        fresh = load_parsed(workspace, source.name, label, groups)
        fresh_mains = [d for d in fresh if d.parent_id is None]
        several = len(fresh_mains) > 1
        pairs = []
        for new_main in fresh_mains:
            new_children = {d.child_path: d for d in fresh if d.parent_id == new_main.dataset_id}
            old_main = old_mains.get(new_main.list_path)
            if old_main is None:  # a new list in the reply: new datasets
                new_main.api_source = source
                new_main.name = workspace.unique_name(
                    list_name(source.name, new_main.list_path, several),
                    ignore_id=new_main.dataset_id,
                )
                notes.append(f"New list “{new_main.list_path}” added as “{new_main.name}”.")
                for child in new_children.values():
                    child.name = workspace.unique_name(
                        f"{new_main.name} – {child.child_path}", ignore_id=child.dataset_id
                    )
                continue
            pairs.append((old_main, new_main))
            old_children = {
                d.child_path: d
                for d in workspace.datasets.values()
                if d.parent_id == old_main.dataset_id
            }
            for path, child in new_children.items():
                if path in old_children:
                    pairs.append((old_children[path], child))
                else:  # a new list inside the rows: a new child dataset
                    child.parent_id = old_main.dataset_id
                    child.name = workspace.unique_name(
                        f"{old_main.name} – {path}", ignore_id=child.dataset_id
                    )
                    notes.append(f"New list “{path}” added as “{child.name}”.")
            missing += [old for path, old in old_children.items() if path not in new_children]
        fresh_paths = {d.list_path for d in fresh_mains}
        missing += [old for path, old in old_mains.items() if path not in fresh_paths]
        for old in missing:
            old.notice = NOT_IN_REPLY
        notes += query_dataset.swap_in_fresh(workspace, pairs, fresh)
        notes += [f"“{old.name}”: {NOT_IN_REPLY}" for old in missing]
    logger.info("Refreshed API datasets from %s", label)
    return notes
