"""Read an uploaded CSV / Excel file (bytes in memory) into a workspace's DuckDB database.

Steps for every table (a CSV file, or one Excel sheet):
1. Copy the cells as plain text into a "raw" table, with the row number in the file.
2. Clean: trim spaces, empty cells become blank, blank rows are skipped.
3. Header = first non-blank row. Check the column names.
4. Work out each column's type (whole number, number, date, true/false or text).
5. Create the final typed table and drop the raw one.
Every refusal is an UploadRejected with a message that names the problem.
"""

import codecs
import csv
import io
import logging
from datetime import date, datetime, time, timedelta
from pathlib import Path

import duckdb
from python_calamine import CalamineWorkbook

from . import conf
from .workspace import ColumnProfile, Dataset, Workspace

logger = logging.getLogger(__name__)

CSV_TYPES = {".csv"}
EXCEL_TYPES = {".xlsx", ".xlsm"}
RAW_TABLE = "_raw_load"
EXCEL_BATCH_ROWS = 20_000
SAMPLE_ROWS = 200
TRIM_CHARS = " \t\r\n "  # spaces, tabs, line breaks and the no-break space Excel adds
TRUE_WORDS = "'true', 'yes'"
BOOLEAN_WORDS = "'true', 'yes', 'false', 'no'"
MIXED_SHARE = 0.5  # half the values fit a type but some don't → "mixed", flagged

# Date formats tried for a column, best first. Each also accepts a time after the date, so a
# column mixing "03-04-2026" and "03-04-2026 10:30" is still one date column. Day-before-month
# wins a tie (03-04-2026 is 3 April); month-first only when the values prove it (04-25-2026).
DATE_FORMATS = [
    ("%Y-%m-%d", "yyyy-mm-dd"),
    ("%Y/%m/%d", "yyyy/mm/dd"),
    ("%d-%m-%Y", "dd-mm-yyyy"),
    ("%d/%m/%Y", "dd/mm/yyyy"),
    ("%d.%m.%Y", "dd.mm.yyyy"),
    ("%d-%m-%y", "dd-mm-yy"),
    ("%d/%m/%y", "dd/mm/yy"),
    ("%d-%b-%Y", "dd-mon-yyyy"),
    ("%d %b %Y", "dd mon yyyy"),
    ("%d-%b-%y", "dd-mon-yy"),
    ("%d %B %Y", "dd month yyyy"),
    ("%m-%d-%Y", "mm-dd-yyyy"),
    ("%m/%d/%Y", "mm/dd/yyyy"),
]
TIME_PARTS = ("", " %H:%M:%S", " %H:%M", "T%H:%M:%S")
# Cheap first test: only columns whose values look like this are tried against every format.
DATE_LIKE = r"\d{1,4}[-/. ][0-9A-Za-z]{1,9}[-/. ]\d{2,4}([ T]\d{1,2}:\d{2}(:\d{2})?)?"
# A number with thousand separators: Indian 1,00,000 or western 100,000.
COMMA_NUMBER = r"[+-]?(\d{1,3}(,\d{2})*,\d{3}|\d{1,3}(,\d{3})+)(\.\d+)?"


class UploadRejected(Exception):
    """The file can't be loaded. The message is shown to the user as it is."""


# ---------- entry point ----------


def load_file(workspace: Workspace, file_name: str, data: bytes | io.BytesIO) -> list[Dataset]:
    """Load one uploaded file; returns the new datasets (one per CSV, one per Excel sheet).
    `data` may be the upload's own in-memory buffer, so the file is never copied whole."""
    source = io.BytesIO(data) if isinstance(data, bytes | bytearray) else data
    suffix = Path(file_name).suffix.lower()
    if suffix == ".xls":
        raise UploadRejected(
            "Old Excel format (.xls) is not supported. Open it in Excel and "
            "“Save As” .xlsx, then upload again."
        )
    if suffix not in CSV_TYPES | EXCEL_TYPES:
        raise UploadRejected("Only CSV, XLSX and XLSM files can be uploaded.")
    max_mb = conf.get("MAX_FILE_MB")
    size = source.getbuffer().nbytes
    if size > max_mb * 1024 * 1024:
        raise UploadRejected(f"The file is larger than the {max_mb} MB limit.")
    if size == 0:
        raise UploadRejected("The file is empty.")
    source.seek(0)

    stem = Path(file_name).stem
    with workspace.lock:
        try:
            if suffix in CSV_TYPES:
                return [_load_csv(workspace, stem, file_name, source)]
            return _load_excel(workspace, stem, file_name, source)
        except duckdb.OutOfMemoryException as error:
            logger.warning("Out of memory loading %s: %s", file_name, error)
            raise UploadRejected(
                "This file is too large for the memory allowed per user. "
                "Try a smaller file, fewer rows or fewer columns, or remove other datasets first."
            ) from error
        finally:
            workspace.connection.execute(f"DROP TABLE IF EXISTS {RAW_TABLE}")
            workspace.refresh_memory()


# ---------- reading into the raw table ----------


def _is_utf8(view: memoryview) -> bool:
    """Check the encoding in 1 MB steps, without making a text copy of the whole file."""
    decoder = codecs.getincrementaldecoder("utf-8")()
    step = 1024 * 1024
    try:
        for start in range(0, len(view), step):
            decoder.decode(view[start : start + step])
        decoder.decode(b"", final=True)
    except UnicodeDecodeError:
        return False
    return True


def _load_csv(workspace: Workspace, stem: str, file_name: str, source: io.BytesIO) -> Dataset:
    with source.getbuffer() as view:
        looks_binary = b"\x00" in bytes(view[:8192])
        is_utf8 = not looks_binary and _is_utf8(view)
    if looks_binary:
        raise UploadRejected(
            "This file is not a readable text CSV (it looks like a binary file). "
            "If it is an Excel file, keep its .xlsx name."
        )
    if is_utf8:  # DuckDB reads UTF-8 (with or without a BOM) straight from the buffer
        source.seek(0)
        csv_input = source
    else:  # older Windows / Excel exports
        csv_input = io.BytesIO(source.getvalue().decode("cp1252", errors="replace").encode())
    connection = workspace.connection
    try:
        relation = connection.read_csv(csv_input, header=False, all_varchar=True, null_padding=True)
        relation.query(
            "csv_input", "SELECT row_number() OVER () AS row_no, * FROM csv_input"
        ).create(RAW_TABLE)
    except duckdb.OutOfMemoryException:
        raise
    except duckdb.Error as error:
        logger.warning("Unreadable CSV %s: %s", file_name, error)
        raise UploadRejected(
            "The file could not be read as CSV. It may be damaged or not a CSV file."
        ) from error
    return _build_dataset(workspace, stem, file_name, excel_first_column=None)


def _load_excel(
    workspace: Workspace, stem: str, file_name: str, source: io.BytesIO
) -> list[Dataset]:
    try:
        workbook = CalamineWorkbook.from_filelike(source)
    except Exception as error:  # calamine raises its own, undocumented error types
        logger.warning("Unreadable Excel %s: %s", file_name, error)
        raise UploadRejected(
            "The Excel file could not be opened. It may be damaged or password-protected."
        ) from error

    sheets = []
    for sheet_name in workbook.sheet_names:
        sheet = workbook.get_sheet_by_name(sheet_name)
        if sheet.height and sheet.width:  # empty sheets are skipped
            sheets.append(sheet)
    if not sheets:
        raise UploadRejected("Every sheet in this workbook is empty.")

    datasets = []
    try:
        for sheet in sheets:
            label = f"{file_name} › {sheet.name}"
            name = stem if len(sheets) == 1 else f"{stem} – {sheet.name}"
            _excel_sheet_to_raw(workspace, sheet, label)
            try:
                datasets.append(
                    _build_dataset(workspace, name, label, excel_first_column=sheet.start[1])
                )
            except UploadRejected as error:
                raise UploadRejected(f"Sheet “{sheet.name}”: {error}") from error
            workspace.connection.execute(f"DROP TABLE IF EXISTS {RAW_TABLE}")
    except BaseException:
        for dataset in datasets:  # all sheets or nothing, so a re-upload doesn't duplicate
            workspace.remove_dataset(dataset.dataset_id)
        raise
    return datasets


def _excel_sheet_to_raw(workspace: Workspace, sheet, label: str) -> None:
    """Stream the sheet into the raw table in batches (as CSV text, all in memory)."""
    connection = workspace.connection
    width = sheet.width
    names = [f"column{index}" for index in range(width)]
    columns_sql = ", ".join(f"{name} VARCHAR" for name in names)
    connection.execute(f"CREATE TABLE {RAW_TABLE} (row_no BIGINT, {columns_sql})")
    max_rows = conf.get("MAX_ROWS")
    column_types = {"row_no": "BIGINT", **{name: "VARCHAR" for name in names}}

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    pending = 0

    def flush():
        nonlocal buffer, writer, pending
        if not pending:
            return
        payload = io.BytesIO(buffer.getvalue().encode("utf-8"))
        batch = connection.read_csv(
            payload,
            header=False,
            columns=column_types,
            auto_detect=False,
            delimiter=",",
            quotechar='"',
            escapechar='"',
        )
        batch.insert_into(RAW_TABLE)
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        pending = 0

    filled_rows = 0
    for index, row in enumerate(sheet.iter_rows()):
        cells = [_excel_cell_text(value) for value in row]
        if any(cells):
            filled_rows += 1
            if filled_rows > max_rows + 1:  # +1 for the header row
                raise UploadRejected(_too_many_rows_message(max_rows))
            writer.writerow([index + 1, *cells])
            pending += 1
            if pending >= EXCEL_BATCH_ROWS:
                flush()
    flush()
    logger.info("Read %s rows from %s", filled_rows, label)


def _excel_cell_text(value) -> str:
    """Excel cell → text. Real dates become yyyy-mm-dd so they are never ambiguous."""
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() and abs(value) < 1e15 else repr(value)
    if isinstance(value, datetime):
        if value.time() == time(0, 0):
            return value.strftime("%Y-%m-%d")
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, timedelta | time):
        return str(value)
    return str(value)


# ---------- from raw text to a typed table ----------


def _clean(column: str) -> str:
    """SQL for a trimmed cell, with empty text turned into blank (NULL)."""
    return f"nullif(trim({column}, '{TRIM_CHARS}'), '')"


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _column_label(position: int, excel_first_column: int | None) -> str:
    """Human name of a column position, e.g. "column 5" or "column E" in Excel."""
    if excel_first_column is None:
        return f"column {position + 1}"
    number, letters = excel_first_column + position + 1, ""
    while number:
        number, remainder = divmod(number - 1, 26)
        letters = chr(65 + remainder) + letters
    return f"column {letters}"


def _too_many_rows_message(max_rows: int) -> str:
    return f"The file has more than {max_rows:,} data rows (the limit). Split it and try again."


def _build_dataset(
    workspace: Workspace, name: str, source_label: str, excel_first_column: int | None
) -> Dataset:
    connection = workspace.connection
    raw_columns = [column for column in connection.table(RAW_TABLE).columns if column != "row_no"]
    cleaned = [_clean(column) for column in raw_columns]
    not_blank_row = f"coalesce({', '.join(cleaned)}) IS NOT NULL"

    header_row = connection.execute(
        f"SELECT row_no, {', '.join(cleaned)} FROM {RAW_TABLE} "
        f"WHERE {not_blank_row} ORDER BY row_no LIMIT 1"
    ).fetchall()  # fetchall, not fetchone: DuckDB 2 blocks the connection on an unread result
    if not header_row:
        raise UploadRejected("The file is empty.")
    header_row_no, header_names = header_row[0][0], list(header_row[0][1:])
    data_rows = f"row_no > {header_row_no} AND {not_blank_row}"

    row_count = connection.execute(
        f"SELECT count(*) FROM {RAW_TABLE} WHERE {data_rows}"
    ).fetchall()[0][0]
    if row_count == 0:
        raise UploadRejected("The file has a header row but no data rows below it.")
    max_rows = conf.get("MAX_ROWS")
    if row_count > max_rows:
        raise UploadRejected(_too_many_rows_message(max_rows))

    stats = _column_stats(connection, cleaned, data_rows)
    kept = _check_header(header_names, stats, header_row_no, excel_first_column)
    _add_date_stats(connection, cleaned, stats, data_rows)

    profiles, select_parts = [], []
    for position in kept:
        column_name = header_names[position]
        profile, cast_sql = _decide_type(
            connection, column_name, cleaned[position], stats[position], row_count, data_rows
        )
        profiles.append(profile)
        select_parts.append(f"{cast_sql} AS {_quote(column_name)}")

    dataset_id = workspace.new_dataset_id()
    table_name = f"ds_{dataset_id}"
    connection.execute(
        f"CREATE TABLE {table_name} AS SELECT {', '.join(select_parts)} "
        f"FROM {RAW_TABLE} WHERE {data_rows} ORDER BY row_no"
    )
    try:
        connection.execute(f"DROP TABLE {RAW_TABLE}")
        _fill_samples(connection, table_name, profiles)
    except BaseException:
        connection.execute(f"DROP TABLE IF EXISTS {table_name}")  # don't leave it in memory
        raise

    dataset = Dataset(
        dataset_id=dataset_id,
        name=workspace.unique_name(name),
        table_name=table_name,
        source_file=source_label,
        row_count=row_count,
        columns=profiles,
    )
    workspace.datasets[dataset_id] = dataset
    logger.info("Loaded %s: %s rows × %s columns", source_label, row_count, len(profiles))
    return dataset


def _column_stats(connection, cleaned: list[str], data_rows: str) -> list[dict]:
    """One pass over the data: per column, counts of filled / number-like / date-like values."""
    parts = []
    for index, value in enumerate(cleaned):
        number = (
            f"(isfinite(try_cast(replace({value}, ',', '') AS DOUBLE)) AND "
            f"(NOT contains({value}, ',') OR regexp_full_match({value}, '{COMMA_NUMBER}')))"
        )
        parts += [
            f"count({value}) AS filled_{index}",
            f"count(*) FILTER (WHERE {number}) AS number_{index}",
            (
                f"count(*) FILTER (WHERE {number} AND NOT regexp_matches({value}, '[.eE]') AND "
                f"try_cast(replace({value}, ',', '') AS BIGINT) IS NOT NULL) AS integer_{index}"
            ),
            f"count(*) FILTER (WHERE regexp_matches({value}, '^[+-]?0[0-9]')) AS zero_{index}",
            f"count(*) FILTER (WHERE lower({value}) IN ({BOOLEAN_WORDS})) AS boolean_{index}",
            f"count(*) FILTER (WHERE regexp_full_match({value}, '{DATE_LIKE}')) AS datelike_{index}",
        ]
    row = connection.execute(
        f"SELECT {', '.join(parts)} FROM {RAW_TABLE} WHERE {data_rows}"
    ).fetchall()[0]
    keys = ("filled", "number", "integer", "zero", "boolean", "datelike")
    return [
        {key: row[index * len(keys) + offset] for offset, key in enumerate(keys)}
        for index in range(len(cleaned))
    ]


def _check_header(
    header_names: list, stats: list[dict], header_row_no: int, excel_first_column: int | None
) -> list[int]:
    """Positions of the columns to keep. Refuses unnamed or repeated column names."""
    kept, seen = [], {}
    for position, column_name in enumerate(header_names):
        if column_name is None:
            if stats[position]["filled"]:
                label = _column_label(position, excel_first_column)
                raise UploadRejected(
                    f"{label.capitalize()} has data but no name in the header row "
                    f"(row {header_row_no}). Add a column name and upload again."
                )
            continue  # an empty, unnamed column is just padding: ignore it
        key = column_name.casefold()
        if key in seen:
            first = _column_label(seen[key], excel_first_column)
            second = _column_label(position, excel_first_column)
            raise UploadRejected(
                f"The column name “{column_name}” is used twice ({first} and {second}). "
                "Rename one of them and upload again."
            )
        seen[key] = position
        kept.append(position)
    max_columns = conf.get("MAX_COLUMNS")
    if len(kept) > max_columns:
        raise UploadRejected(
            f"The file has {len(kept)} columns; the limit is {max_columns}. "
            "Remove some columns and try again."
        )
    return kept


def _parse_date(value: str, date_format: str) -> str:
    """SQL reading a cell with one date format, with or without a time after it."""
    formats = ", ".join(f"'{date_format}{time_part}'" for time_part in TIME_PARTS)
    return f"try_strptime({value}, [{formats}])"


def _date_fits(value: str, date_format: str) -> str:
    """SQL test: the cell is a real date in this format (a year like 0026 is not)."""
    return f"coalesce(year({_parse_date(value, date_format)}) BETWEEN 1900 AND 2200, false)"


def _add_date_stats(connection, cleaned: list[str], stats: list[dict], data_rows: str) -> None:
    """For columns that look like dates, find the format that reads the most values, and
    whether any value has a time of day. One query for all such columns."""
    candidates = [
        index
        for index, column_stats in enumerate(stats)
        if column_stats["filled"]
        and column_stats["datelike"] >= column_stats["filled"] * MIXED_SHARE
    ]
    if not candidates:
        return
    parts = []
    for index in candidates:
        for date_format, _ in DATE_FORMATS:
            parsed, fits = (
                _parse_date(cleaned[index], date_format),
                _date_fits(cleaned[index], date_format),
            )
            parts += [
                f"count(*) FILTER (WHERE {fits})",
                f"count(*) FILTER (WHERE {fits} AND CAST({parsed} AS TIME) <> TIME '00:00')",
            ]
    row = connection.execute(
        f"SELECT {', '.join(parts)} FROM {RAW_TABLE} WHERE {data_rows}"
    ).fetchall()[0]
    per_column = len(DATE_FORMATS) * 2
    for order, index in enumerate(candidates):
        counts = row[order * per_column : (order + 1) * per_column]
        # Most values read wins; on a tie the earlier (day-first) format wins.
        best = max(range(len(DATE_FORMATS)), key=lambda number: (counts[number * 2], -number))
        date_format, label = DATE_FORMATS[best]
        stats[index]["date"] = {
            "format": date_format,
            "label": label,
            "fit": counts[best * 2],
            "has_time": counts[best * 2 + 1] > 0,
        }


def _decide_type(
    connection, column_name: str, value: str, stats: dict, row_count: int, data_rows: str
):
    """The column's profile and the SQL that turns its text into the right type."""
    filled = stats["filled"]
    profile = ColumnProfile(name=column_name, kind="text", blanks=row_count - filled)
    if filled == 0:
        return profile, value

    plain_number = f"CAST(replace({value}, ',', '') AS"
    if stats["boolean"] == filled:
        profile.kind = "boolean"
        return (
            profile,
            f"CASE WHEN {value} IS NULL THEN NULL ELSE lower({value}) IN ({TRUE_WORDS}) END",
        )
    if stats["number"] == filled:
        if stats["zero"]:
            profile.note = (
                "Kept as text: some values start with 0 (like codes), which a number would lose."
            )
            return profile, value
        if stats["integer"] == filled:
            profile.kind = "integer"
            return profile, f"{plain_number} BIGINT)"
        profile.kind = "number"
        return profile, f"{plain_number} DOUBLE)"

    date = stats.get("date")
    date_fit, date_format = (date["fit"], date["format"]) if date else (0, None)
    if date and date_fit == filled:
        profile.date_format = date["label"]
        parsed = _parse_date(value, date_format)
        if date["has_time"]:
            profile.kind = "datetime"
            return profile, parsed
        profile.kind = "date"
        return profile, f"CAST({parsed} AS DATE)"

    # Not every value fits one type. If most do, flag the column so the user can fix the file.
    fits = {
        "number": (stats["number"], "number"),
        "date": (date_fit, "date"),
        "boolean": (stats["boolean"], "true / false value"),
    }
    best_kind = max(fits, key=lambda kind: fits[kind][0])
    best_count, best_label = fits[best_kind]
    if best_count >= filled * MIXED_SHARE:
        profile.mixed = True
        profile.mixed_examples = _misfit_examples(
            connection, value, best_kind, date_format, data_rows
        )
        profile.note = (
            f"Mostly {best_label}s, but {filled - best_count:,} value(s) are not, "
            "so the column is kept as text."
        )
    return profile, value


def _misfit_examples(connection, value, kind, date_format, data_rows) -> list[str]:
    """Up to 3 values that don't fit the column's main type, with their row in the file."""
    if kind == "number":
        fits = f"isfinite(try_cast(replace({value}, ',', '') AS DOUBLE))"
    elif kind == "date":
        fits = _date_fits(value, date_format)
    else:
        fits = f"lower({value}) IN ({BOOLEAN_WORDS})"
    rows = connection.execute(
        f"SELECT {value}, row_no FROM {RAW_TABLE} WHERE {data_rows} AND {value} IS NOT NULL "
        f"AND NOT coalesce({fits}, false) ORDER BY row_no LIMIT 3"
    ).fetchall()
    return [f"“{text}” (row {row_no})" for text, row_no in rows]


def _fill_samples(connection, table_name: str, profiles: list[ColumnProfile]) -> None:
    """Up to 3 different example values per column, from the first rows."""
    rows = connection.execute(f"SELECT * FROM {table_name} LIMIT {SAMPLE_ROWS}").fetchall()
    for position, profile in enumerate(profiles):
        samples: list[str] = []
        for row in rows:
            text = display_value(row[position])
            if text and text not in samples:
                samples.append(text)
                if len(samples) == 3:
                    break
        profile.samples = samples


def display_value(value) -> str:
    """A cell as the user should read it (dates as dd-mm-yyyy)."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, datetime):
        return value.strftime("%d-%m-%Y %H:%M")
    if isinstance(value, date):
        return value.strftime("%d-%m-%Y")
    if isinstance(value, float):
        return f"{value:.10g}"
    return str(value)
