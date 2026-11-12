"""JSON files: finding the rows, flattening, child datasets, types and refusals."""

import datetime
import json

import pytest

from my_insights.linking import suggest_links
from my_insights.loading import UploadRejected, load_file
from tests.conftest import insights_settings

ORDERS = {
    "status": "ok",
    "data": {
        "orders": [
            {
                "id": 1,
                "date": "03-04-2026",
                "code": "007",
                "paid": True,
                "customer": {"name": "Ravi", "address": {"city": "Pune", "geo": {"lat": 18.5}}},
                "tags": ["new", "vip"],
                "items": [
                    {"sku": "A1", "qty": 2, "parts": [{"p": 1}]},
                    {"sku": "B7", "qty": 1},
                ],
            },
            {
                "id": 2,
                "date": "15-04-2026",
                "code": "012",
                "paid": False,
                "customer": {"name": "Asha"},
                "tags": [],
                "items": [{"sku": "C1", "qty": 5}],
            },
        ]
    },
}


def as_bytes(document) -> bytes:
    return json.dumps(document).encode()


def kinds(dataset) -> dict:
    return {column.name: column.kind for column in dataset.columns}


def rows(workspace, dataset):
    return workspace.connection.execute(f"SELECT * FROM {dataset.table_name}").fetchall()


def test_flat_list(workspace):
    data = as_bytes([{"Item": "A1", "Qty": 5, "Rate": 2.5}, {"Item": "A2", "Qty": 7, "Rate": None}])
    [dataset] = load_file(workspace, "stock.json", data)
    assert dataset.name == "stock"
    assert kinds(dataset) == {"Item": "text", "Qty": "integer", "Rate": "number"}
    assert rows(workspace, dataset) == [("A1", 5, 2.5), ("A2", 7, None)]


def test_json_lines_skip_blank_lines(workspace):
    [dataset] = load_file(workspace, "log.jsonl", b'{"a": 1}\n\n{"a": 2, "b": true}\n')
    assert dataset.row_count == 2
    assert kinds(dataset) == {"a": "integer", "b": "boolean"}


def test_rows_found_inside_an_object_and_nesting_flattened(workspace):
    main, items = load_file(workspace, "orders.json", as_bytes(ORDERS))
    assert main.source_file == "orders.json › data.orders"
    assert [c.name for c in main.columns] == [
        "_row",
        "id",
        "date",
        "code",
        "paid",
        "customer.name",
        "customer.address.city",
        "customer.address.geo",
        "tags",
    ]
    assert kinds(main)["date"] == "date"
    assert kinds(main)["code"] == "text"  # leading zero kept, same as CSV
    assert kinds(main)["paid"] == "boolean"
    first, second = rows(workspace, main)
    assert first[2] == datetime.date(2026, 4, 3)
    assert first[7] == '{"lat": 18.5}'  # deeper than 3 levels: kept as JSON text
    assert first[8] == "new, vip"
    assert second[6] is None and second[8] is None  # missing key / empty list → blank
    assert items.name == "orders – items"
    assert items.parent_id == main.dataset_id and items.child_path == "items"
    assert rows(workspace, items) == [
        (1, "A1", 2, '[{"p": 1}]'),
        (1, "B7", 1, None),
        (2, "C1", 5, None),
    ]


def test_rows_at_a_path_and_a_single_object(workspace):
    from my_insights.json_loading import parse_json

    [parsed] = parse_json(as_bytes(ORDERS), rows_at="data.orders")
    assert parsed.rows_at == "data.orders" and len(parsed.main.rows) == 2
    [one] = load_file(workspace, "one.json", as_bytes({"name": "Shop", "city": "Pune"}))
    assert one.row_count == 1
    with pytest.raises(UploadRejected, match="no “result” there"):
        parse_json(as_bytes(ORDERS), rows_at="data.result")


def test_list_of_plain_values(workspace):
    [dataset] = load_file(workspace, "codes.json", b'["A1", "A2", "A3"]')
    assert [c.name for c in dataset.columns] == ["value"]
    assert dataset.row_count == 3


def test_parent_child_link_is_the_top_suggestion(workspace):
    main, items = load_file(workspace, "orders.json", as_bytes(ORDERS))
    first = suggest_links(workspace)[0]
    assert (first.main_id, first.lookup_id) == (items.dataset_id, main.dataset_id)
    assert (first.main_column, first.lookup_column) == ("_parent_row", "_row")
    assert first.match_percent == 100


def test_removing_the_parent_removes_its_children(workspace):
    main, _items = load_file(workspace, "orders.json", as_bytes(ORDERS))
    removed = workspace.remove_dataset(main.dataset_id)
    assert set(removed) == {"orders", "orders – items"}
    assert workspace.datasets == {}


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b'{"a": 1,}', r"not valid JSON \(line 1"),
        (b"[]", "no rows"),
        (b"42", "single value"),
        (b"  ", "empty"),
        (b"[{}, {}]", "no fields"),
        ('[{"a": "é"}]'.encode("latin-1"), "not UTF-8"),
        (b'[{"Name": 1, "name": 2}]', "used twice"),
    ],
)
def test_bad_json_is_refused_with_a_message(workspace, data, message):
    with pytest.raises(UploadRejected, match=message):
        load_file(workspace, "bad.json", data)
    assert workspace.datasets == {}


def test_bad_line_in_jsonl_names_the_line(workspace):
    with pytest.raises(UploadRejected, match="Line 2 is not valid JSON"):
        load_file(workspace, "bad.jsonl", b'{"a": 1}\n{"a": \n')


def test_row_limit_and_all_or_nothing(workspace):
    with insights_settings(MAX_ROWS=2):
        with pytest.raises(UploadRejected, match="more than 2 data rows"):
            load_file(workspace, "big.json", as_bytes([{"a": n} for n in range(3)]))
        # the main list fits, its child list does not: nothing is kept
        document = [{"a": 1, "items": [{"b": 1}, {"b": 2}, {"b": 3}]}]
        with pytest.raises(UploadRejected, match="items"):
            load_file(workspace, "orders.json", as_bytes(document))
    assert workspace.datasets == {}
    assert workspace.connection.execute("SELECT count(*) FROM duckdb_tables()").fetchall() == [(0,)]


SHOP_REPLY = {
    "status": "ok",
    "data": {
        "orders": [{"id": 1, "amount": 500, "items": [{"sku": "A1"}]}, {"id": 2, "amount": 300}],
        "shops": [{"id": 9, "city": "Pune"}],
    },
}


def test_every_list_becomes_its_own_dataset(workspace):
    orders, items, shops = load_file(workspace, "sales.json", as_bytes(SHOP_REPLY))
    assert (orders.name, items.name, shops.name) == (
        "sales – orders",
        "sales – orders – items",
        "sales – shops",
    )
    assert (orders.row_count, items.row_count, shops.row_count) == (2, 1, 1)
    assert orders.list_path == "data.orders" and shops.list_path == "data.shops"
    assert items.parent_id == orders.dataset_id and shops.parent_id is None
    assert "status" not in [c.name for c in orders.columns]  # single values are left out


def test_rows_at_loads_only_that_list(workspace):
    from my_insights.json_loading import load_parsed, parse_json

    groups = parse_json(as_bytes(SHOP_REPLY), rows_at="data.shops")
    [shops] = load_parsed(workspace, "sales", "sales.json", groups)
    assert shops.name == "sales" and shops.row_count == 1
