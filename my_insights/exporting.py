"""Exports: a query's answer as CSV or Excel.

    CSV    every row (up to EXPORT_ROW_CAP), streamed from DuckDB in batches, so even
           20 lakh rows use little memory. Plain data: header + rows, no Total row.
    Excel  real numbers and dates with their formats (₹ 12,34,567.50 stays a number Excel can
           add up), a Total row and an "About" sheet (query, filters, time). Built in memory
           (nothing is written to disk), so it has a size limit: EXCEL_MAX_CELLS.

Text that starts like a formula (=, +, -, @) gets a leading ' so Excel shows it instead of
running it, e.g. "=HYPERLINK(…)" → "'=HYPERLINK(…)". Numbers such as -5 are left alone.
"""

import io
import logging
import math
import re
from datetime import date, datetime
from decimal import Decimal

import duckdb
import xlsxwriter
from django.http import HttpResponse
from django.utils import timezone
from django.utils.http import content_disposition_header
from django_datatable import formula_safe, rows_csv_response

from . import conf, querying
from .dashboard import filter_ready
from .querying import BLANK, FILTER_OPS, QueryFailed

logger = logging.getLogger(__name__)

EXCEL_MAX_ROWS = 1_048_576 - 2  # Excel's own limit, less the header and the Total row
EXCEL_MAX_TEXT = 32_767  # characters Excel keeps in one cell
CSV_BATCH = 5_000  # rows fetched from DuckDB at a time
MAX_LISTED_VALUES = 10  # "is one of" values named on the About sheet
FILE_NAME_CHARACTERS = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')
OP_WORDS = {op: words for ops in FILTER_OPS.values() for op, words in ops}


class ExportFailed(Exception):
    """The export could not be made; the message is safe to show."""


def export_cap() -> int:
    return int(conf.get("EXPORT_ROW_CAP") or conf.get("MAX_ROWS"))


def excel_cell_cap() -> int:
    return int(conf.get("EXCEL_MAX_CELLS"))


def file_name(name: str, extension: str) -> str:
    """ "Sales: Apr/May" → "Sales- Apr-May.xlsx" (no characters Windows refuses)."""
    cleaned = FILE_NAME_CHARACTERS.sub("-", " ".join((name or "").split())).strip(" .-")
    return f"{cleaned[:80] or 'export'}.{extension}"


def attachment(response, name: str):
    response["Content-Disposition"] = content_disposition_header(True, name)
    return response


# ---------- one cell ----------


def cell_value(value, column):
    """A result cell as it goes into the file: date groups as their label ("Apr 2026"),
    yes / no as words, text made formula-safe. Numbers and dates stay real values."""
    if value is None:
        return None
    if column.date_part and column.date_part != "day":
        if column.date_part == "year":
            return value
        return querying.format_value(value, column)
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, datetime):
        if timezone.is_aware(value):
            value = timezone.localtime(value)
        return value.replace(tzinfo=None)
    if isinstance(value, date | int | float | Decimal):
        return value
    return formula_safe(str(value))


def _csv_cell(value, column):
    value = cell_value(value, column)
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, float) and value.is_integer():
        return int(value)  # 100, not 100.0
    return value  # rows_csv_response writes numbers, dates (yyyy-mm-dd) and text


# ---------- running the query ----------


def _plan(spec, dataset, catalog, cap, today):
    plan = querying.plan_query(spec, dataset, today=today, catalog=catalog, cap=cap)
    if not plan.ok:
        raise ExportFailed(" ".join(plan.problems) or "The query has nothing to export yet.")
    return plan


def _run(workspace, spec, plan, cap):
    try:
        with workspace.lock:
            return querying.run_query(workspace.connection, plan, spec.top_n, cap=cap)
    except QueryFailed as error:
        raise ExportFailed(str(error)) from error


def csv_response(workspace, spec, dataset, title: str, today=None):
    """Every row of the answer as CSV, streamed."""
    catalog = querying.field_catalog(dataset)
    cap = export_cap()
    plan = _plan(spec, dataset, catalog, cap, today)
    if plan.across is not None:  # spread into columns in Python: rows are groups, not records
        result = _run(workspace, spec, plan, cap)
        columns = result.columns
        rows = ([_csv_cell(row[c.key], c) for c in columns] for row in result.rows)
    else:
        columns = plan.columns
        rows = _stream(workspace, plan, cap)
    response = rows_csv_response("export.csv", [column.label for column in columns], rows)
    logger.info("CSV export of %r started", title)
    return attachment(response, file_name(title, "csv"))


def _stream(workspace, plan, cap: int):
    """The plan's rows, a batch at a time, on a cursor of their own: the user's other pages
    keep working while a big file downloads."""
    cursor = None
    try:
        with workspace.lock:
            cursor = workspace.connection.cursor()
        cursor.execute(plan.sql, plan.params)
        sent = 0
        while sent < cap:
            batch = cursor.fetchmany(min(CSV_BATCH, cap - sent))
            if not batch:
                break
            for record in batch:
                yield [_csv_cell(value, column) for value, column in zip(record, plan.columns)]
            sent += len(batch)
    except duckdb.Error:
        logger.exception("CSV export stopped part way")
        yield ["Export stopped early: the data was changed or cleared. Download it again."]
    finally:
        if cursor is not None:
            cursor.close()


# ---------- Excel ----------


def indian_format(decimals: int, rupee: bool) -> str:
    """Excel's number format for lakh / crore grouping, e.g. 1,23,45,678.50.
    Excel allows only two conditions, so values under -1 lakh get their own format
    (see negative_format)."""
    tail = "." + "0" * decimals if decimals else ""
    sign = '"₹ "' if rupee else ""
    return (
        rf"[>=10000000]{sign}##\,##\,##\,##0{tail};"
        rf"[>=100000]{sign}##\,##\,##0{tail};"
        f"{sign}##,##0{tail}"
    )


LAKH_CONDITIONS = ("[>=10000000]", "[>=100000]")


def negative_format(number_format: str, value: float) -> str | None:
    """For an Indian format and a value at or below -1 lakh, a fixed format that groups it
    right, e.g. -12345678 → -1,23,45,678. None = keep the column's own format."""
    if value > -100000 or not number_format.startswith(LAKH_CONDITIONS[0]):
        return None
    crore, lakh, _ = number_format.split(";")
    pattern = (
        crore.removeprefix(LAKH_CONDITIONS[0])
        if value <= -10000000
        else lakh.removeprefix(LAKH_CONDITIONS[1])
    )
    return f"{pattern};-{pattern}"  # 2nd section = negatives, shown without sign


def excel_number_format(column) -> str:
    """The Excel format for a result column ("" = General)."""
    kind = querying.kind_class(column.kind)
    if column.date_part == "day" or (kind == "date" and not column.date_part):
        return "dd-mm-yyyy hh:mm" if column.kind == "datetime" else "dd-mm-yyyy"
    if column.date_part == "year":
        return "0"
    if column.number_format:
        return indian_format(int(column.number_format[-1]), column.number_format.startswith("r"))
    if column.is_value and column.kind == "integer":
        return indian_format(0, False)
    return ""


def excel_response(workspace, spec, dataset, title: str, about: list, today=None):
    """The answer as an .xlsx file, or ExportFailed when it is too big for Excel here."""
    catalog = querying.field_catalog(dataset)
    columns_guess = max(1, len(_plan(spec, dataset, catalog, 1, today).columns))
    cell_cap = excel_cell_cap()
    rows_cap = min(export_cap(), EXCEL_MAX_ROWS, max(1, cell_cap // columns_guess))
    plan = _plan(spec, dataset, catalog, rows_cap, today)
    result = _run(workspace, spec, plan, rows_cap)
    cells = len(result.rows) * len(result.columns)
    # A Top N bigger than the cap is cut by the LIMIT without run_query noticing.
    cut = result.truncated or (bool(spec.top_n) and len(result.rows) >= rows_cap < spec.top_n)
    if (cut and rows_cap < export_cap()) or cells > cell_cap:
        logger.info("Excel export of %r refused: too big (%s rows shown)", title, len(result.rows))
        raise ExportFailed(
            f"This answer is too big for an Excel file (the limit is {cell_cap:,} cells, "
            f"e.g. {cell_cap // 10:,} rows × 10 columns). Download it as CSV instead, "
            "or add filters."
        )
    info = list(about)
    info.append(("Rows", f"{len(result.rows):,}"))
    if cut:
        info.append(("Note", f"Cut at the first {rows_cap:,} rows (the export limit)."))
    info += [("Note", note) for note in plan.notes + result.notes]
    content = excel_bytes(result, info)
    logger.info("Excel export of %r: %s rows", title, len(result.rows))
    response = HttpResponse(
        content,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    return attachment(response, file_name(title, "xlsx"))


def excel_bytes(result, about: list[tuple[str, str]]) -> bytes:
    """Sheet "Data": bold header (frozen, with filter buttons), the rows, a Total row.
    Sheet "About": what was exported."""
    buffer = io.BytesIO()
    workbook = xlsxwriter.Workbook(buffer, {"in_memory": True})  # never a temp file on disk
    sheet = workbook.add_worksheet("Data")
    bold = workbook.add_format({"bold": True, "bg_color": "#EEF2F7", "bottom": 1})
    formats = []
    for column in result.columns:
        number_format = excel_number_format(column)
        formats.append(
            workbook.add_format({"num_format": number_format}) if number_format else None
        )
    total_style = {"bold": True, "top": 1}
    total_formats = [
        workbook.add_format(total_style | ({"num_format": f.num_format} if f else {}))
        for f in formats
    ]
    made = {}  # (format string, is total) → format, for big negative numbers

    def cell_format(value, index, total=False):
        base = (total_formats if total else formats)[index]
        if base is None or not isinstance(value, int | float | Decimal):
            return base
        special = negative_format(base.num_format, float(value))
        if special is None:
            return base
        if (special, total) not in made:
            style = (total_style if total else {}) | {"num_format": special}
            made[special, total] = workbook.add_format(style)
        return made[special, total]

    widths = [min(max(len(column.label), 8), 50) for column in result.columns]
    for index, column in enumerate(result.columns):
        sheet.write_string(0, index, column.label, bold)
    number = 0
    for number, row in enumerate(result.rows, start=1):
        for index, column in enumerate(result.columns):
            value = cell_value(row[column.key], column)
            _write(sheet, number, index, value, cell_format(value, index))
            if number <= 200 and isinstance(value, str):  # width from the first rows
                widths[index] = min(max(widths[index], len(value) + 2), 50)
    last_row = number
    # No Group by (only Values, maybe Across): the one row is already the total.
    if result.totals and result.rows and result.columns and not result.columns[0].is_value:
        last_row += 1
        for index, column in enumerate(result.columns):
            if column.key in result.totals:
                total = result.totals[column.key]
                _write(sheet, last_row, index, total, cell_format(total, index, total=True))
            elif index == 0:
                sheet.write_string(last_row, 0, "Total", total_formats[0])
    for index, width in enumerate(widths):
        is_date = formats[index] is not None and "yyyy" in formats[index].num_format
        sheet.set_column(index, index, max(width, 12 if is_date else 0) + 1)
    sheet.freeze_panes(1, 0)
    if result.columns:
        sheet.autofilter(0, 0, max(number, 1), len(result.columns) - 1)

    about_sheet = workbook.add_worksheet("About")
    about_sheet.set_column(0, 0, 14)
    about_sheet.set_column(1, 1, 90)
    for number, (label, text) in enumerate(about):
        about_sheet.write_string(number, 0, label, bold)
        about_sheet.write_string(number, 1, formula_safe(str(text))[:EXCEL_MAX_TEXT])
    workbook.close()
    return buffer.getvalue()


def _write(sheet, row: int, col: int, value, cell_format) -> None:
    """One cell with its real type (never write(): it would turn "=…" into a formula)."""
    if value is None:
        return
    if isinstance(value, Decimal):
        value = float(value)
    if isinstance(value, int | float) and not isinstance(value, bool):
        if not math.isfinite(value):  # NaN / infinity: Excel has no such number
            return
        sheet.write_number(row, col, value, cell_format)
    elif isinstance(value, date):
        sheet.write_datetime(row, col, value, cell_format)
    else:
        sheet.write_string(row, col, str(value)[:EXCEL_MAX_TEXT], cell_format)


# ---------- what was exported (the About sheet) ----------


def filter_text(item, spec, catalog) -> str:
    """e.g. "Branch is one of Delhi, Mumbai", "Date between 2026-04-01 and 2026-04-30"."""
    subject = item.column
    if item.value_key and spec is not None:
        value = next((v for v in spec.values if v.key == item.value_key), None)
        subject = querying.value_label(value, catalog) if value else "A total"
    words = OP_WORDS.get(item.op, item.op)
    values = ["(blank)" if v == BLANK else v for v in item.values]
    if item.op == "between":
        return f"{subject} between {' and '.join(values[:2])}"
    if item.op == "last_days":
        return f"{subject} in the last {values[0] if values else '…'} days"
    if len(values) > MAX_LISTED_VALUES:
        values = [*values[:MAX_LISTED_VALUES], f"and {len(values) - MAX_LISTED_VALUES} more"]
    return f"{subject} {words} {', '.join(values)}".strip()


def about_rows(spec, dataset, title: str, catalog=None) -> list[tuple[str, str]]:
    """The About sheet's first lines: the query, its data, its filters and the time."""
    catalog = catalog if catalog is not None else querying.field_catalog(dataset)
    rows = [("Query", title), ("Data", dataset.name)]
    filters = [item for item in spec.filters if filter_ready(item)]
    for item in filters:
        box = "Any one of" if item.any_of else "Filter"
        rows.append((box, filter_text(item, spec, catalog)))
    if spec.top_n:
        rows.append(("Top", f"{spec.top_n:,} rows"))
    rows.append(("Exported", timezone.localtime().strftime("%d-%m-%Y %H:%M")))
    return rows
