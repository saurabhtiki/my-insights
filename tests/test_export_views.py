"""Phase 6: the export pages — query, Table tile and dashboard downloads."""

import csv
import io
import json

import openpyxl
import pytest
from django.test import Client

from my_insights.workspace import FilterItem
from tests.conftest import csv_bytes, insights_settings
from tests.test_dashboard_views import add, setup, url  # noqa: F401  (setup is a fixture)
from tests.test_querying import post, upload

pytestmark = pytest.mark.django_db


def csv_rows(response) -> list[list[str]]:
    text = b"".join(response.streaming_content).decode("utf-8")
    return list(csv.reader(io.StringIO(text.lstrip("﻿"))))


def by_item(client, dataset):
    """The query screen showing Sum of Amount by Item (draft "tab1")."""
    return post(
        client,
        dataset,
        groups="g1",
        g1_column="Item",
        values="v1",
        v1_column="Amount",
        v1_agg="sum",
    )


def test_query_screen_offers_csv_and_excel_of_the_current_answer(client):
    _, dataset = upload(client)
    page = by_item(client, dataset).content.decode()
    assert "⤓ Export ▾" in page
    assert url("query_export", "tab1", "xlsx") + "?name=" in page

    response = client.get(url("query_export", "tab1", "csv"), {"name": "Sales: by item"})
    assert response["Content-Type"].startswith("text/csv")
    assert 'filename="Sales- by item.csv"' in response["Content-Disposition"]
    assert csv_rows(response) == [
        ["Item", "Sum of Amount"],
        ["A1", "300"],
        ["B2", "80"],
        ["C3", "70"],
    ]

    response = client.get(url("query_export", "tab1", "xlsx"))
    assert "spreadsheetml" in response["Content-Type"]
    assert 'filename="Sum of Amount by Item.xlsx"' in response["Content-Disposition"]
    sheet = openpyxl.load_workbook(io.BytesIO(response.content))["Data"]
    assert sheet.cell(5, 1).value == "Total" and sheet.cell(5, 2).value == 450


def test_expired_or_unknown_drafts_explain_instead_of_a_file(client):
    upload(client)
    page = client.get(url("query_export", "nosuch", "csv"))
    assert page.status_code == 200 and "This result has expired" in page.content.decode()
    assert client.get(url("query_export", "tab1", "pdf")).status_code == 404


def test_too_big_for_excel_shows_a_message(client):
    _, dataset = upload(client)
    by_item(client, dataset)
    with insights_settings(EXCEL_MAX_CELLS=2):
        page = client.get(url("query_export", "tab1", "xlsx")).content.decode()
    assert "too big for an Excel file" in page and "CSV" in page


def test_sessions_are_isolated(client):
    _, dataset = upload(client)
    by_item(client, dataset)
    other = Client()
    upload(other)
    page = other.get(url("query_export", "tab1", "csv")).content.decode()
    assert "This result has expired" in page


def test_table_tile_exports_what_it_shows_with_click_filters(setup, client):  # noqa: F811
    _, query, board = setup
    add(client, board, kind="table", query_id=query.query_id)
    tile_id = board.tiles[0].tile_id
    tile_page = client.get(url("dashboard_tile", board.dashboard_id, tile_id)).content.decode()
    assert url("tile_export", board.dashboard_id, tile_id, "csv") in tile_page
    assert "dashboardClicks()" in tile_page

    rows = csv_rows(client.get(url("tile_export", board.dashboard_id, tile_id, "csv")))
    assert [row[0] for row in rows[1:]] == ["A1", "B2", "C3"]

    clicks = json.dumps([{"tile": 99, "column": "Item", "part": "", "kind": "text", "value": "B2"}])
    rows = csv_rows(
        client.get(url("tile_export", board.dashboard_id, tile_id, "csv"), {"clicks": clicks})
    )
    assert rows[1:] == [["B2", "80"]]

    response = client.get(
        url("tile_export", board.dashboard_id, tile_id, "xlsx"), {"clicks": clicks}
    )
    book = openpyxl.load_workbook(io.BytesIO(response.content))
    assert [row[0].value for row in book["Data"].iter_rows(min_row=2)] == ["B2", "Total"]
    about = {row[0].value: row[1].value for row in book["About"].iter_rows()}
    assert about["Dashboard"] == "Monthly review" and about["Filter"] == "Item is one of B2"


def test_tile_export_of_a_removed_query_explains(setup, client):  # noqa: F811
    workspace, query, board = setup
    add(client, board, kind="table", query_id=query.query_id)
    workspace.queries.clear()
    page = client.get(url("tile_export", board.dashboard_id, 1, "csv")).content.decode()
    assert "This query was removed" in page
    page = client.get(url("tile_export", board.dashboard_id, 7, "csv")).content.decode()
    assert "This table tile was removed" in page


def test_chart_tiles_have_their_own_png_button(setup, client):  # noqa: F811
    _, _, board = setup
    add(client, board, kind="chart", chart_id=1)
    body = client.get(url("dashboard_tile", board.dashboard_id, 1)).content.decode()
    assert "⤓ PNG" in body and "{tile: 1}" in body
    assert "$event.detail.tile === 1 && downloadPng()" in body


def test_dashboard_exports_as_one_html_file(setup, client):  # noqa: F811
    _, query, board = setup
    item_key = query.spec.values[0].key
    add(client, board, kind="chart", chart_id=1)
    add(client, board, kind="table", query_id=query.query_id)
    add(client, board, kind="kpi", kpi_value=f"{query.query_id}:{item_key}", title="Sales")
    add(client, board, kind="note", text="Read me <script>alert(1)</script>")
    page = client.get(url("dashboard", board.dashboard_id)).content.decode()
    assert url("dashboard_export", board.dashboard_id) in page and "⤓ HTML" in page

    clicks = json.dumps(
        [
            {
                "tile": 1,
                "column": "Item",
                "part": "",
                "kind": "text",
                "value": "B2",
                "label": "Item = B2",
            }
        ]
    )
    response = client.post(url("dashboard_export", board.dashboard_id), {"clicks": clicks})
    assert response["Content-Type"].startswith("text/html")
    assert 'filename="Monthly review.html"' in response["Content-Disposition"]
    html = response.content.decode()
    assert "<!doctype html>" in html[:400]
    assert "<title>Monthly review</title>" in html and "Item = B2" in html
    assert 'id="tile-option-1"' in html and "echarts@5.6.0" in html  # live chart
    assert '<script id="tile-picked-1" type="application/json">["Item = B2"]</script>' in html
    assert "Sales" in html and "80" in html  # KPI filtered by the click: B2 only
    assert "&lt;script&gt;alert(1)" in html and "<script>alert(1)" not in html
    assert "<td" in html and "C3" not in html.split("<table")[1]  # the table is filtered too
    assert "fileTable(" in html  # search + CSV in the file
    assert 'aria-label="Search the table"' in html and "⤓ CSV" in html
    assert "hx-get" not in html  # stands alone: nothing loads from the app


def test_html_table_shows_at_most_500_rows(client):
    lines = ["Code,Amount", *(f"C{n:04d},{n}" for n in range(600))]
    workspace, dataset = upload(client, "many.csv", csv_bytes(*lines))
    post(client, dataset, action="save", name="All codes")
    query = next(iter(workspace.queries.values()))
    client.post(url("dashboard_create"), {"name": "Codes"})
    board = next(iter(workspace.dashboards.values()))
    add(client, board, kind="table", query_id=query.query_id)
    html = client.post(url("dashboard_export", board.dashboard_id)).content.decode()
    assert "Showing the first 500 of 600 rows" in html
    assert "C0499" in html and "C0500" not in html


def test_dashboard_export_needs_post(setup, client):  # noqa: F811
    _, _, board = setup
    assert client.get(url("dashboard_export", board.dashboard_id)).status_code == 405


def test_export_works_on_a_copy_of_the_draft(client):
    workspace, dataset = upload(client)
    by_item(client, dataset)
    draft = workspace.query_drafts["tab1"]
    draft.filters.append(FilterItem("f1", "Amount", "", "bogus", ["5"]))  # planning fixes the op
    client.get(url("query_export", "tab1", "csv"))
    assert draft.filters[0].op == "bogus"


def test_html_file_lists_every_dashboard_filter(setup, client):  # noqa: F811
    _, query, board = setup
    add(client, board, kind="table", query_id=query.query_id)
    board.filters = [
        FilterItem("f1", "Branch", "", "in", ["MUM"]),
        FilterItem("f2", "Item", "", "in", []),  # added but nothing ticked
    ]
    html = client.post(url("dashboard_export", board.dashboard_id)).content.decode()
    assert "Filtered by:" in html and "Branch is one of MUM" in html
    assert "Item: nothing picked, not used" in html
