"""Phase 6: exports — CSV / Excel files of a query's answer."""

import csv
import io
from datetime import datetime

import openpyxl
import pytest

from my_insights import exporting
from my_insights.exporting import ExportFailed
from my_insights.loading import load_file
from my_insights.querying import ResultColumn
from my_insights.workspace import FilterItem, GroupItem, ValueItem
from tests.conftest import csv_bytes, insights_settings
from tests.test_pivot import MONTH, SUM, across
from tests.test_querying import SALES, spec_of

TRICKY = csv_bytes(
    "Name,Amount,Phone",
    "=1+1,100,+91 98100",
    "-x,-5,@sum",
    "@SUM(A1),250000.5,\tcmd",
    "Plain,12345678,1",
)


@pytest.fixture
def sales(workspace):
    return workspace, load_file(workspace, "sales.csv", SALES)[0]


@pytest.fixture
def tricky(workspace):
    return workspace, load_file(workspace, "tricky.csv", TRICKY)[0]


def csv_rows(response) -> list[list[str]]:
    text = b"".join(response.streaming_content).decode("utf-8")
    assert text.startswith("﻿")  # Excel reads ₹ and accents right
    return list(csv.reader(io.StringIO(text[1:])))


def workbook(response):
    return openpyxl.load_workbook(io.BytesIO(response.content))


def excel(workspace, dataset, spec, title="Test"):
    about = exporting.about_rows(spec, dataset, title)
    return exporting.excel_response(workspace, spec, dataset, title, about)


BY_BRANCH = {
    "groups": [GroupItem("g", "Branch")],
    "values": [ValueItem("v", "Amount", "sum", number_format="r2")],
}


# ---------- formula safety ----------


def test_formula_like_text_is_escaped_in_csv_and_excel(tricky):
    workspace, dataset = tricky
    spec = spec_of(dataset)  # no groups or values: the rows themselves
    rows = csv_rows(exporting.csv_response(workspace, spec, dataset, "Tricky"))
    assert rows[0] == ["Name", "Amount", "Phone"]
    names = [row[0] for row in rows[1:]]
    assert names == ["'=1+1", "'-x", "'@SUM(A1)", "Plain"]
    assert [row[1] for row in rows[1:]] == ["100", "-5", "250000.5", "12345678"]  # numbers kept
    assert rows[1][2] == "'+91 98100" and rows[2][2] == "'@sum"

    sheet = workbook(excel(workspace, dataset, spec))["Data"]
    assert [sheet.cell(r, 1).value for r in range(2, 6)] == names
    assert sheet.cell(3, 2).value == -5 and sheet.cell(3, 2).data_type == "n"
    assert all(sheet.cell(r, c).data_type != "f" for r in range(1, 6) for c in range(1, 4))


def test_cell_value_keeps_numbers_dates_and_words():
    text = ResultColumn("c0", "Name", "text")
    assert exporting.cell_value("=cmd|' /C calc'!A0", text) == "'=cmd|' /C calc'!A0"
    assert exporting.cell_value(True, ResultColumn("c1", "Paid", "boolean")) == "Yes"
    month = ResultColumn("g0", "Date (month)", "date", "month")
    assert exporting.cell_value(datetime(2026, 4, 1).date(), month) == "Apr 2026"
    assert exporting.cell_value(None, text) is None


# ---------- full rows ----------


def test_export_has_every_row_not_only_the_preview(workspace):
    lines = ["Code,Amount", *(f"C{n},{n}" for n in range(50))]
    dataset = load_file(workspace, "many.csv", csv_bytes(*lines))[0]
    spec = spec_of(dataset)
    with insights_settings(PREVIEW_ROW_CAP=10):
        assert len(csv_rows(exporting.csv_response(workspace, spec, dataset, "Many"))) == 51
        assert workbook(excel(workspace, dataset, spec))["Data"].max_row == 51
    with insights_settings(EXPORT_ROW_CAP=20):
        assert len(csv_rows(exporting.csv_response(workspace, spec, dataset, "Many"))) == 21
        book = workbook(excel(workspace, dataset, spec))
        assert book["Data"].max_row == 21
        about = [row[1].value for row in book["About"].iter_rows()]
        assert "Cut at the first 20 rows (the export limit)." in about


def test_too_big_for_excel_says_use_csv(workspace):
    lines = ["Code,Amount", *(f"C{n},{n}" for n in range(30))]
    dataset = load_file(workspace, "many.csv", csv_bytes(*lines))[0]
    with insights_settings(EXCEL_MAX_CELLS=40), pytest.raises(ExportFailed, match="CSV"):
        excel(workspace, dataset, spec_of(dataset))
    with insights_settings(EXCEL_MAX_CELLS=60):  # 30 rows × 2 columns fit exactly
        assert workbook(excel(workspace, dataset, spec_of(dataset)))["Data"].max_row == 31


# ---------- Excel ----------


def test_excel_has_real_numbers_formats_total_row_and_about_sheet(sales):
    workspace, dataset = sales
    spec = spec_of(dataset, **BY_BRANCH)
    spec.filters.append(FilterItem("f", "Item", "", "in", ["A1", "B2"]))
    book = workbook(excel(workspace, dataset, spec, "Sales by branch"))
    sheet = book["Data"]
    assert [c.value for c in sheet[1]] == ["Branch", "Sum of Amount"]
    assert sheet[1][0].font.b and sheet.freeze_panes == "A2"
    assert sheet.auto_filter.ref == "A1:B4"
    assert [[c.value for c in row] for row in sheet.iter_rows(min_row=2)] == [
        ["DEL", 50],
        ["MUM", 300],
        [None, 30],  # a blank Branch stays an empty cell
        ["Total", 380],
    ]
    amount = sheet.cell(2, 2)
    assert amount.data_type == "n" and "₹" in amount.number_format
    assert "##\\,##\\,##0.00" in amount.number_format  # lakh grouping, 2 decimals
    assert sheet.cell(sheet.max_row, 1).font.b
    about = {row[0].value: row[1].value for row in book["About"].iter_rows()}
    assert about["Query"] == "Sales by branch" and about["Data"] == "sales"
    assert about["Filter"] == "Item is one of A1, B2"
    assert about["Rows"] == "3" and "Exported" in about


def test_dates_are_real_dates_shown_dd_mm_yyyy(sales):
    workspace, dataset = sales
    spec = spec_of(dataset, groups=[GroupItem("g", "Date", "day")], values=SUM)
    sheet = workbook(excel(workspace, dataset, spec))["Data"]
    cell = sheet.cell(2, 1)
    assert isinstance(cell.value, datetime) and cell.number_format == "dd-mm-yyyy"


def test_across_exports_wide_like_the_screen(sales):
    workspace, dataset = sales
    spec = across(dataset, SUM, MONTH)
    labels = ["Branch", "Mar 2026", "Apr 2026", "May 2026", "Oct 2026", "Total"]
    assert csv_rows(exporting.csv_response(workspace, spec, dataset, "Wide"))[0] == labels
    sheet = workbook(excel(workspace, dataset, spec))["Data"]
    assert [c.value for c in sheet[1]] == labels
    assert next(c.value for c in sheet[sheet.max_row]) == "Total"


def test_csv_and_excel_hold_the_same_values(sales):
    workspace, dataset = sales
    spec = spec_of(dataset, **BY_BRANCH)
    rows = csv_rows(exporting.csv_response(workspace, spec, dataset, "x"))
    sheet = workbook(excel(workspace, dataset, spec))["Data"]
    for number, row in enumerate(rows[1:], start=2):
        assert row[0] == (sheet.cell(number, 1).value or "")
        assert float(row[1]) == sheet.cell(number, 2).value


def test_a_broken_query_explains_instead_of_a_file(sales):
    workspace, dataset = sales
    spec = spec_of(dataset, groups=[GroupItem("g", "Gone")], values=SUM)
    with pytest.raises(ExportFailed, match="no longer in"):
        exporting.csv_response(workspace, spec, dataset, "x")


def test_file_names_are_safe():
    assert exporting.file_name('Sales: Apr/May "draft"', "xlsx") == "Sales- Apr-May -draft.xlsx"
    assert exporting.file_name("  ", "csv") == "export.csv"
    assert exporting.file_name("बिक्री ₹", "csv") == "बिक्री ₹.csv"


def test_indian_number_format():
    assert exporting.indian_format(0, False) == (
        r"[>=10000000]##\,##\,##\,##0;[>=100000]##\,##\,##0;##,##0"
    )
    assert exporting.indian_format(2, True).startswith('[>=10000000]"₹ "##')


def test_top_n_bigger_than_the_excel_limit_is_refused_not_cut(workspace):
    lines = ["Code,Amount", *(f"C{n},{n}" for n in range(30))]
    dataset = load_file(workspace, "many.csv", csv_bytes(*lines))[0]
    spec = spec_of(dataset, groups=[GroupItem("g", "Code")], values=SUM, top_n=25)
    with insights_settings(EXCEL_MAX_CELLS=40), pytest.raises(ExportFailed, match="CSV"):
        excel(workspace, dataset, spec)


def test_no_group_across_has_no_extra_total_row(sales):
    workspace, dataset = sales
    spec = across(dataset, SUM, MONTH, groups=())
    sheet = workbook(excel(workspace, dataset, spec))["Data"]
    assert sheet.max_row == 2


def test_big_negative_numbers_keep_lakh_grouping(workspace):
    lines = ["Code,Amount", "A,-12345678", "B,-250000", "C,-500", "D,7000000"]
    dataset = load_file(workspace, "neg.csv", csv_bytes(*lines))[0]
    spec = spec_of(
        dataset,
        groups=[GroupItem("g", "Code")],
        values=[ValueItem("v", "Amount", "sum", number_format="r2")],
    )
    sheet = workbook(excel(workspace, dataset, spec))["Data"]
    crore, lakh, small, positive, total = (sheet.cell(r, 2) for r in range(2, 7))
    assert crore.value == -12345678 and crore.number_format == (
        r'"₹ "##\,##\,##\,##0.00;-"₹ "##\,##\,##\,##0.00'
    )
    assert lakh.number_format == r'"₹ "##\,##\,##0.00;-"₹ "##\,##\,##0.00'
    assert small.number_format == positive.number_format  # the column's own format
    assert small.number_format.startswith("[>=10000000]")
    assert total.value == -5596178 and total.font.b and ";-" in total.number_format
    assert exporting.negative_format("0.00", -1e9) is None
