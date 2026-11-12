"""Reading files: types, dates, cleaning and every refusal message."""

import datetime
import re

import pytest

from my_insights.loading import UploadRejected, load_file
from tests.conftest import csv_bytes, excel_bytes, insights_settings


def kinds(dataset) -> dict:
    return {column.name: column.kind for column in dataset.columns}


def column(dataset, name):
    return next(column for column in dataset.columns if column.name == name)


def rows(workspace, dataset):
    return workspace.connection.execute(f"SELECT * FROM {dataset.table_name}").fetchall()


# ---------- good files ----------


def test_csv_types_counts_and_samples(workspace):
    data = csv_bytes(
        "Item Code,Qty,Rate,Audit Date,Active,Remark",
        "A1,10,12.5,03-04-2026,yes,first",
        'A2,"1,00,000",3,15-04-2026,no,',
        "A3,7,8.25,30-04-2026,Yes,third",
    )
    [dataset] = load_file(workspace, "stock.csv", data)
    assert dataset.name == "stock"
    assert dataset.row_count == 3
    assert kinds(dataset) == {
        "Item Code": "text",
        "Qty": "integer",
        "Rate": "number",
        "Audit Date": "date",
        "Active": "boolean",
        "Remark": "text",
    }
    assert column(dataset, "Remark").blanks == 1
    assert column(dataset, "Item Code").samples == ["A1", "A2", "A3"]
    assert rows(workspace, dataset)[1][1] == 100000  # Indian grouping read as a number


def test_unclear_date_is_day_first(workspace):
    [dataset] = load_file(workspace, "d.csv", csv_bytes("When", "03-04-2026", "05-06-2026"))
    assert column(dataset, "When").date_format == "dd-mm-yyyy"
    assert rows(workspace, dataset)[0][0] == datetime.date(2026, 4, 3)  # 3 April


def test_month_first_only_when_proven(workspace):
    [dataset] = load_file(workspace, "d.csv", csv_bytes("When", "04-03-2026", "04-25-2026"))
    assert column(dataset, "When").date_format == "mm-dd-yyyy"
    assert rows(workspace, dataset)[1][0] == datetime.date(2026, 4, 25)


def test_spaces_trimmed_and_blank_rows_skipped(workspace):
    data = csv_bytes("", " Name , Qty ", "  apple  , 5 ", ",", "", " pear ,6")
    [dataset] = load_file(workspace, "f.csv", data)
    assert [c.name for c in dataset.columns] == ["Name", "Qty"]
    assert rows(workspace, dataset) == [("apple", 5), ("pear", 6)]


def test_mixed_column_kept_as_text_and_flagged(workspace):
    lines = ["Qty"] + [str(number) for number in range(1, 20)] + ["N/A"]
    [dataset] = load_file(workspace, "m.csv", csv_bytes(*lines))
    qty = column(dataset, "Qty")
    assert qty.kind == "text"
    assert qty.mixed
    assert qty.mixed_examples == ["“N/A” (row 21)"]
    assert dataset.flagged_count == 1


def test_mostly_text_column_is_not_flagged(workspace):
    [dataset] = load_file(workspace, "t.csv", csv_bytes("Name", "apple", "pear", "42"))
    assert column(dataset, "Name").kind == "text"
    assert not column(dataset, "Name").mixed


def test_leading_zero_codes_stay_text(workspace):
    [dataset] = load_file(workspace, "c.csv", csv_bytes("Bin", "007", "120"))
    bin_column = column(dataset, "Bin")
    assert bin_column.kind == "text"
    assert "start with 0" in bin_column.note
    assert rows(workspace, dataset)[0][0] == "007"


def test_windows_encoded_csv(workspace):
    data = csv_bytes("Name,Price", "Café,5", encoding="cp1252")
    [dataset] = load_file(workspace, "w.csv", data)
    assert rows(workspace, dataset)[0][0] == "Café"


def test_unnamed_empty_column_is_ignored(workspace):
    [dataset] = load_file(workspace, "p.csv", csv_bytes("A,B,", "1,2,", "3,4,"))
    assert [c.name for c in dataset.columns] == ["A", "B"]


def test_excel_one_dataset_per_sheet_and_empty_sheets_skipped(workspace):
    data = excel_bytes(
        {
            "Stock": [
                [],
                [None, "Code", "Qty", "When"],
                [None, "A1", 10, datetime.date(2026, 4, 3)],
            ],
            "Empty": [],
            "Prices": [["Code", "Rate"], ["A1", 2.5]],
        }
    )
    datasets = load_file(workspace, "book.xlsx", data)
    assert [d.name for d in datasets] == ["book – Stock", "book – Prices"]
    assert kinds(datasets[0]) == {"Code": "text", "Qty": "integer", "When": "date"}
    assert kinds(datasets[1]) == {"Code": "text", "Rate": "number"}


def test_xlsm_is_read(workspace):
    [dataset] = load_file(workspace, "macro.xlsm", excel_bytes({"S": [["A"], [1]]}))
    assert dataset.name == "macro"


def test_same_name_gets_a_number(workspace):
    load_file(workspace, "s.csv", csv_bytes("A", "1"))
    [second] = load_file(workspace, "s.csv", csv_bytes("A", "1"))
    assert second.name == "s (2)"


# ---------- refusals ----------


@pytest.mark.parametrize(
    ("file_name", "data", "message"),
    [
        ("old.xls", b"x", "Save As"),
        ("notes.txt", b"x", r"Only CSV, XLSX, XLSM, JSON, JSONL and SQLite \(\.db\)"),
        ("empty.csv", b"  \n\n", "empty"),
        ("header.csv", b"A,B\n", "no data rows"),
        ("binary.csv", b"PK\x03\x04\x00\x00junk", "not a readable text CSV"),
        ("broken.xlsx", b"not a zip", "could not be opened"),
        ("noname.csv", b"A,,C\n1,2,3\n", "Column 2 has data but no name"),
        (
            "dupes.csv",
            b"Qty,Code,qty\n1,2,3\n",
            re.escape("“qty” is used twice (column 1 and column 3)"),
        ),
    ],
)
def test_refusals_name_the_problem(workspace, file_name, data, message):
    with pytest.raises(UploadRejected, match=message):
        load_file(workspace, file_name, data)
    assert workspace.datasets == {}


def test_excel_refusal_names_sheet_and_column_letter(workspace):
    data = excel_bytes({"Good": [["A"], [1]], "Bad": [[None, "X", "x"], [None, 1, 2]]})
    with pytest.raises(UploadRejected, match="Sheet “Bad”.*column B and column C"):
        load_file(workspace, "b.xlsx", data)
    assert workspace.datasets == {}  # the good sheet is not kept either


def test_all_sheets_empty(workspace):
    with pytest.raises(UploadRejected, match="Every sheet"):
        load_file(workspace, "e.xlsx", excel_bytes({"One": [], "Two": []}))


@insights_settings(MAX_COLUMNS=2)
def test_too_many_columns(workspace):
    with pytest.raises(UploadRejected, match="3 columns; the limit is 2"):
        load_file(workspace, "w.csv", csv_bytes("A,B,C", "1,2,3"))


@insights_settings(MAX_ROWS=3)
@pytest.mark.parametrize("file_name", ["r.csv", "r.xlsx"])
def test_too_many_rows(workspace, file_name):
    lines = [["N"]] + [[number] for number in range(5)]
    data = (
        csv_bytes(*(str(line[0]) for line in lines))
        if file_name.endswith(".csv")
        else excel_bytes({"S": lines})
    )
    with pytest.raises(UploadRejected, match="more than 3 data rows"):
        load_file(workspace, file_name, data)


@insights_settings(MAX_FILE_MB=1)
def test_file_too_big(workspace):
    with pytest.raises(UploadRejected, match="larger than the 1 MB"):
        load_file(workspace, "big.csv", b"A\n" + b"1\n" * 600_000)


@insights_settings(SESSION_MEMORY_MB=8, MAX_FILE_MB=100)
def test_out_of_memory_is_a_message_not_a_crash():
    from my_insights.workspace import store

    _, small_workspace = store.create()
    data = csv_bytes("A,B", *(f"{'x' * 40}{number},{'y' * 40}" for number in range(300_000)))
    with pytest.raises(UploadRejected, match="too large for the memory"):
        load_file(small_workspace, "big.csv", data)


def test_duckdb_never_spills_to_disk(workspace):
    setting = workspace.connection.execute("SELECT current_setting('temp_directory')").fetchone()
    assert setting == ("",)


# ---------- fixes from the phase-1 code review ----------


def test_dates_with_and_without_time_are_one_datetime_column(workspace):
    data = csv_bytes("When", "03-04-2026", "03-04-2026 10:30", "05-04-2026 08:15:00")
    [dataset] = load_file(workspace, "t.csv", data)
    assert column(dataset, "When").kind == "datetime"
    assert rows(workspace, dataset)[1][0] == datetime.datetime(2026, 4, 3, 10, 30)


def test_excel_midnight_and_timed_cells_are_one_datetime_column(workspace):
    cells = [["When"], [datetime.datetime(2026, 4, 3)], [datetime.datetime(2026, 4, 3, 10, 30)]]
    [dataset] = load_file(workspace, "t.xlsx", excel_bytes({"S": cells}))
    assert column(dataset, "When").kind == "datetime"
    assert not column(dataset, "When").mixed


def test_odd_date_examples_are_listed(workspace):
    lines = ["When"] + [f"{day:02d}-04-2026" for day in range(1, 10)] + ["03-04-26"]
    [dataset] = load_file(workspace, "d.csv", csv_bytes(*lines))
    when = column(dataset, "When")
    assert when.mixed
    assert when.mixed_examples == ["“03-04-26” (row 11)"]


def test_failed_load_leaves_no_table_behind(workspace, monkeypatch):
    import duckdb

    from my_insights import loading

    def out_of_memory(*args):
        raise duckdb.OutOfMemoryException("simulated")

    monkeypatch.setattr(loading, "fill_samples", out_of_memory)
    with pytest.raises(UploadRejected, match="too large"):
        load_file(workspace, "a.csv", csv_bytes("A", "1"))
    assert workspace.connection.execute("SELECT count(*) FROM duckdb_tables()").fetchone() == (0,)
    assert workspace.datasets == {}


def test_memory_use_is_tracked_without_locking(workspace):
    load_file(workspace, "a.csv", csv_bytes("A", *map(str, range(1000))))
    assert workspace.memory_used_bytes > 0
    [dataset_id] = workspace.datasets
    workspace.remove_dataset(dataset_id)
    assert workspace.memory_used_bytes >= 0
