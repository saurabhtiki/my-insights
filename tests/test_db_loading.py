"""SQLite upload: listing tables, importing the ticked ones, types, foreign keys, errors."""

import sqlite3

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from my_insights.db_loading import load_tables, open_database
from my_insights.linking import suggest_links
from my_insights.loading import UploadRejected
from my_insights.workspace import SESSION_KEY, store
from tests.conftest import csv_bytes, insights_settings

pytestmark = pytest.mark.django_db

SHOP = """
CREATE TABLE customers (id INTEGER PRIMARY KEY, city TEXT, pin TEXT);
CREATE TABLE orders (
    id INTEGER PRIMARY KEY, customer_id INTEGER REFERENCES customers(id),
    ordered_on TEXT, amount REAL, note BLOB);
INSERT INTO customers VALUES (1, 'Pune', '0411'), (2, 'Mumbai', '0400');
INSERT INTO orders VALUES
    (10, 1, '2026-04-01', 500.5, NULL), (11, 2, '2026-04-02', 300, x'0102'),
    (12, 1, '2026-04-03', 950, NULL);
CREATE VIEW big_orders AS SELECT * FROM orders WHERE amount > 400;
"""


def sqlite_bytes(script: str = SHOP) -> bytes:
    connection = sqlite3.connect(":memory:")
    connection.executescript(script)
    data = connection.serialize()
    connection.close()
    return data


def opened(workspace, script: str = SHOP, name: str = "shop.db"):
    return open_database(workspace, name, sqlite_bytes(script))


def test_tables_and_views_are_listed(workspace):
    pending = opened(workspace)
    listed = {t.name: (t.kind, t.row_count, len(t.columns)) for t in pending.tables}
    assert listed == {
        "customers": ("table", 2, 3),
        "orders": ("table", 3, 5),
        "big_orders": ("view", 2, 5),
    }
    assert workspace.datasets == {}  # nothing loaded yet


def test_import_only_ticked_tables_with_types(workspace):
    pending = opened(workspace)
    datasets = load_tables(workspace, pending.token, ["orders", "customers"])
    assert [d.name for d in datasets] == ["shop – customers", "shop – orders"]
    customers, orders = datasets
    kinds = {c.name: c.kind for c in orders.columns}
    assert kinds["ordered_on"] == "date" and kinds["amount"] == "number"
    assert {c.name: c.kind for c in customers.columns}["pin"] == "text"  # leading zero kept
    note = workspace.connection.execute(f"SELECT note FROM {orders.table_name}").fetchall()
    assert ("(binary, 2 bytes)",) in note
    assert workspace.pending_databases == {}  # the file is dropped after import


def test_names_say_the_table_even_when_one_is_ticked(workspace):
    pending = opened(workspace)
    [dataset] = load_tables(workspace, pending.token, ["customers"])
    assert dataset.name == "shop – customers" and dataset.source_file == "shop.db › customers"


def test_foreign_key_is_the_first_suggestion(workspace):
    pending = opened(workspace)
    customers, orders = load_tables(workspace, pending.token, ["customers", "orders"])
    with workspace.lock:
        first = suggest_links(workspace)[0]
    assert (first.main_id, first.lookup_id) == (orders.dataset_id, customers.dataset_id)
    assert (first.main_column, first.lookup_column) == ("customer_id", "id")
    assert first.from_database and first.match_percent == 100


def test_foreign_key_to_a_table_not_imported_is_ignored(workspace):
    pending = opened(workspace)
    [orders] = load_tables(workspace, pending.token, ["orders"])
    assert orders.foreign_keys == []


def test_foreign_key_without_column_uses_the_primary_key(workspace):
    script = """
    CREATE TABLE shops (code TEXT PRIMARY KEY, city TEXT);
    CREATE TABLE sales (shop TEXT REFERENCES shops, qty INTEGER);
    INSERT INTO shops VALUES ('S1', 'Pune'); INSERT INTO sales VALUES ('S1', 4);
    """
    pending = opened(workspace, script)
    sales, shops = load_tables(workspace, pending.token, ["sales", "shops"])
    assert sales.foreign_keys == [("shop", shops.dataset_id, "code")]


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"not a database at all", "not a SQLite database file"),
        (b"SQLite format 3\x00" + b"\x00" * 100, "could not be read"),
        (b"", "empty"),
    ],
)
def test_bad_files(workspace, data, message):
    with pytest.raises(UploadRejected, match=message):
        open_database(workspace, "x.db", data)


def test_database_with_no_tables(workspace):
    with pytest.raises(UploadRejected, match="no tables"):
        opened(workspace, "CREATE TABLE t (a); DROP TABLE t;")


def test_nothing_ticked_keeps_the_pick_list(workspace):
    pending = opened(workspace)
    with pytest.raises(UploadRejected, match="Tick at least one"):
        load_tables(workspace, pending.token, [])
    assert pending.token in workspace.pending_databases


def test_all_or_nothing(workspace):
    script = SHOP + "CREATE TABLE empty_one (a INTEGER);"
    pending = opened(workspace, script)
    with pytest.raises(UploadRejected, match="Table “empty_one”: .*no data rows"):
        load_tables(workspace, pending.token, ["customers", "empty_one"])
    assert workspace.datasets == {}
    assert pending.token in workspace.pending_databases  # untick and try again
    load_tables(workspace, pending.token, ["customers"])
    assert workspace.pending_databases == {}


def test_a_file_with_one_table_keeps_the_file_name(workspace):
    pending = opened(workspace, "CREATE TABLE t (a); INSERT INTO t VALUES (1);", "one.db")
    [dataset] = load_tables(workspace, pending.token, ["t"])
    assert dataset.name == "one"


def test_row_limit(workspace):
    pending = opened(workspace)
    with (
        insights_settings(MAX_ROWS=2),
        pytest.raises(UploadRejected, match="more than 2 data rows"),
    ):
        load_tables(workspace, pending.token, ["orders"])


def test_the_file_is_read_only(workspace):
    pending = opened(workspace)
    with pytest.raises(sqlite3.OperationalError):
        pending.connection.execute("DELETE FROM orders")


def test_uploading_the_same_file_again_keeps_one_pick_list(workspace):
    opened(workspace)
    opened(workspace)
    assert len(workspace.pending_databases) == 1


def test_switched_off(workspace):
    with (
        insights_settings(ALLOW_DB_IMPORT=False),
        pytest.raises(UploadRejected, match="switched off"),
    ):
        opened(workspace)


# ---------- the views ----------


def upload(client, *files):
    return client.post(
        reverse("my_insights:upload"), {"files": list(files)}, headers={"HX-Request": "true"}
    )


def test_mixed_upload_then_import_and_cancel(client):
    text = upload(
        client,
        SimpleUploadedFile("stock.csv", csv_bytes("Code,Qty", "A1,5"), "text/csv"),
        SimpleUploadedFile("shop.db", sqlite_bytes(), "application/octet-stream"),
    ).content.decode()
    assert "Found 3 tables" in text and "'Import ' + count" in text
    workspace = store.get(client.session.get(SESSION_KEY))
    assert [d.name for d in workspace.datasets.values()] == ["stock"]  # the CSV loaded at once
    [token] = workspace.pending_databases

    url = reverse("my_insights:db_import", kwargs={"token": token})
    text = client.post(url, {"tables": ["customers", "orders"]}, headers={"HX-Request": "true"})
    text = text.content.decode()
    assert "Loaded as shop – customers (2 rows), shop – orders (3 rows)." in text
    assert workspace.pending_databases == {}

    upload(client, SimpleUploadedFile("shop.db", sqlite_bytes(), "application/octet-stream"))
    [token] = workspace.pending_databases
    client.post(reverse("my_insights:db_cancel", kwargs={"token": token}))
    assert workspace.pending_databases == {} and len(workspace.datasets) == 3


def test_import_after_the_file_is_gone(client):
    upload(client, SimpleUploadedFile("stock.csv", csv_bytes("Code,Qty", "A1,5"), "text/csv"))
    url = reverse("my_insights:db_import", kwargs={"token": "99"})
    text = client.post(url, {"tables": ["orders"]}, headers={"HX-Request": "true"}).content
    assert "no longer waiting" in text.decode()
