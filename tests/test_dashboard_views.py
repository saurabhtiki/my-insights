"""Phase 5: the dashboard pages (create, tiles, Edit mode, filters, Add to dashboard)."""

import json

import pytest
from django.test import Client
from django.urls import reverse

from my_insights.workspace import ChartSpec, SavedChart
from tests.test_query_dataset import save_query
from tests.test_querying import HTMX, QUERY, upload

pytestmark = pytest.mark.django_db
HOME = reverse("my_insights:home")


def url(name, *args):
    return reverse(f"my_insights:{name}", args=args)


@pytest.fixture
def setup(client):
    """Sales loaded, the query "By item" saved with a chart on it, and an empty dashboard."""
    workspace, sales = upload(client)
    save_query(client, sales)
    query = next(iter(workspace.queries.values()))
    workspace.charts[1] = SavedChart(
        1, "Item chart", ChartSpec("column", x=query.spec.groups[0].key), query_id=query.query_id
    )
    client.post(url("dashboard_create"), {"name": "Monthly review"})
    board = next(iter(workspace.dashboards.values()))
    return workspace, query, board


def add(client, board, **fields):
    return client.post(url("tile_add", board.dashboard_id), fields)


def tile(client, board, tile_id, **params):
    return client.get(url("dashboard_tile", board.dashboard_id, tile_id), params, headers=HTMX)


def test_make_a_dashboard_from_home_and_see_it_listed(setup, client):
    workspace, _, board = setup
    assert board.name == "Monthly review"
    home = client.get(HOME).content.decode()
    assert "Monthly review" in home and url("dashboard", board.dashboard_id) in home
    response = client.post(url("dashboard_create"), {"name": ""})
    assert response.url == url("dashboard", 2) + "?edit=1"
    assert workspace.dashboards[2].name == "Dashboard 1"
    client.post(url("dashboard_create"), {"name": "monthly review"})  # taken
    assert "Another dashboard is already called" in client.get(HOME).content.decode()


def test_add_every_kind_of_tile_and_draw_them(setup, client):
    _, query, board = setup
    item_key = query.spec.values[0].key
    add(client, board, kind="chart", chart_id=1)
    add(client, board, kind="table", query_id=query.query_id)
    add(client, board, kind="kpi", kpi_value=f"{query.query_id}:{item_key}", title="Sales")
    add(
        client,
        board,
        kind="note",
        text="Before GST <script>alert(1)</script>\nsee https://example.com",
    )
    assert [t.kind for t in board.tiles] == ["chart", "table", "kpi", "note"]

    page = client.get(url("dashboard", board.dashboard_id)).content.decode()
    assert page.count("<article data-tile=") == 4 and "insightChart" in page

    chart = tile(client, board, 1).content.decode()
    assert 'id="tile-option-1"' in chart and 'id="tile-click-1"' in chart
    table = tile(client, board, 2).content.decode()
    assert 'id="dashboard-table-2"' in table and "A1" in table
    kpi = tile(client, board, 3).content.decode()
    assert "Sales" in kpi and "450" in kpi
    note = tile(client, board, 4).content.decode()
    assert "&lt;script&gt;" in note and "<script>alert" not in note
    assert "<br>" in note and 'href="https://example.com"' in note


def test_adding_a_bad_tile_explains_why(setup, client):
    _, _, board = setup
    add(client, board, kind="chart", chart_id=99)
    page = client.get(url("dashboard", board.dashboard_id) + "?edit=1").content.decode()
    assert "Pick a saved chart." in page and not board.tiles
    add(client, board, kind="note", text="   ")
    assert "Type the note" in client.get(url("dashboard", board.dashboard_id)).content.decode()


def test_table_tile_sorts_and_pages_inside_itself(setup, client):
    _, query, board = setup
    add(client, board, kind="table", query_id=query.query_id)
    response = client.get(
        url("dashboard_tile", board.dashboard_id, 1),
        {"sort": "-v0"},
        headers={**HTMX, "HX-Target": "dashboard-table-1"},
    )
    body = response.content.decode()
    assert body.startswith('<div id="dashboard-table-1"') or 'id="dashboard-table-1"' in body
    assert body.index("A1") < body.index("B2")  # A1 (300) first by Sum of Amount, biggest first


def test_edit_mode_saves_order_sizes_and_texts(setup, client):
    _, query, board = setup
    add(client, board, kind="chart", chart_id=1)
    add(client, board, kind="kpi", kpi_value=f"{query.query_id}:{query.spec.values[0].key}")
    add(client, board, kind="note", text="first")
    response = client.post(
        url("dashboard_layout", board.dashboard_id),
        {
            "order": ["3", "1", "2"],
            "t1_width": "full",
            "t1_height": "tall",
            "t2_width": "huge",  # not a size: kept as it was
            "t2_title": "Total sales",
            "t2_subtitle": "April to date",
            "t3_text": "second",
        },
        headers=HTMX,
    )
    assert response.status_code == 200 and 'id="dashboard-grid"' in response.content.decode()
    assert [t.tile_id for t in board.tiles] == [3, 1, 2]
    assert (board.tiles[1].width, board.tiles[1].height) == ("full", "tall")
    assert board.tiles[2].width == "third" and board.tiles[2].title == "Total sales"
    assert board.tiles[2].subtitle == "April to date" and board.tiles[0].text == "second"

    # KPI and note texts are edited behind ✎ (hidden until clicked), not shown twice
    page = client.get(url("dashboard", board.dashboard_id) + "?edit=1").content.decode()
    assert page.count("data-tile-editor x-show") == 2 and page.count('x-data="tileEditor()"') == 2
    # only those tiles hide their body while editing (a chart has no "editing" to read)
    assert page.count('x-show="!editing" class="card-body') == 2
    # only the size menus save on "change" (Sortable fires its own "change" mid-drag)
    assert "change[target.matches('[data-tile-size]')]" in page
    assert page.count("<select data-tile-size") == 4  # chart: width + height, KPI and note: width
    view = client.get(url("dashboard", board.dashboard_id)).content.decode()
    assert "data-tile-editor x-show" not in view

    client.post(url("dashboard_layout", board.dashboard_id), {"order": ["2"]}, headers=HTMX)
    assert [t.tile_id for t in board.tiles] == [2, 3, 1]  # none lost
    client.post(url("tile_remove", board.dashboard_id, 3), headers=HTMX)
    assert [t.tile_id for t in board.tiles] == [2, 1]


def test_global_filters_save_and_name_the_fields_that_changed(setup, client):
    _, query, board = setup
    add(client, board, kind="table", query_id=query.query_id)
    page = client.get(url("dashboard", board.dashboard_id)).content.decode()
    assert "+ Add filter" in page
    filters = url("dashboard_filters", board.dashboard_id)
    response = client.post(filters, {"action": "add_filter:Branch"}, headers=HTMX)
    assert "HX-Trigger" not in response  # nothing ticked yet: no tile changes
    key = board.filters[0].key
    assert "waiting" in response.content.decode()
    response = client.post(
        filters,
        {"filters": key, f"{key}_column": "Branch", f"{key}_op": "in", f"{key}_values": "MUM"},
        headers=HTMX,
    )
    assert json.loads(response["HX-Trigger"]) == {
        "insights-filters-changed": {"columns": ["Branch"]}
    }
    assert board.filters[0].values == ["MUM"]
    body = tile(client, board, 1).content.decode()
    assert "A1" in body and "B2" not in body  # B2 was sold in DEL only

    response = client.post(filters, {"filters": key, f"{key}_column": "Branch",
                                     f"{key}_op": "in", f"{key}_values": "MUM",
                                     "action": f"remove:{key}"}, headers=HTMX)  # fmt: skip
    assert not board.filters and "Branch" in response["HX-Trigger"]


def test_click_filters_come_with_the_tile_request(setup, client):
    _, query, board = setup
    add(client, board, kind="chart", chart_id=1)
    add(client, board, kind="table", query_id=query.query_id)
    clicks = json.dumps([{"tile": 1, "column": "Item", "part": "", "kind": "text", "value": "B2"}])
    body = tile(client, board, 2, clicks=clicks).content.decode()
    assert "B2" in body and "A1" not in body
    assert "A1" in tile(client, board, 2, clicks="not json").content.decode()


def test_add_to_dashboard_from_the_chart_and_query_screens(setup, client):
    workspace, query, board = setup
    chart_page = client.get(QUERY + "?chart=1").content.decode()
    assert "Add to dashboard" in chart_page and "Monthly review" in chart_page
    query_page = client.get(QUERY + f"?open={query.query_id}").content.decode()
    assert "Add to dashboard" in query_page

    added = url("add_to_dashboard")
    response = client.post(added, {"kind": "chart", "chart_id": 1, "dashboard": board.dashboard_id})
    assert "Added to “Monthly review”" in response.content.decode()
    response = client.post(
        added, {"kind": "table", "query_id": query.query_id, "dashboard": "new", "name": "Items"}
    )
    assert "Added to “Items”" in response.content.decode()
    made = workspace.dashboards[2]
    assert [(t.kind, t.query_id) for t in made.tiles] == [("table", query.query_id)]
    response = client.post(added, {"kind": "chart", "chart_id": 42, "dashboard": "new"})
    assert "Save it first" in response.content.decode() and len(workspace.dashboards) == 2


def test_removing_a_saved_chart_leaves_a_flagged_tile(setup, client):
    _, _, board = setup
    add(client, board, kind="chart", chart_id=1)
    client.post(url("chart_remove", 1))
    assert "This chart was removed" in tile(client, board, 1).content.decode()


def test_remove_a_dashboard_from_home(setup, client):
    workspace, _, board = setup
    response = client.post(
        url("dashboard_remove", board.dashboard_id), {"from": "home"}, headers=HTMX
    )
    assert 'id="workspace-panel"' in response.content.decode() and not workspace.dashboards
    assert client.get(url("dashboard", board.dashboard_id)).status_code == 404


def test_other_users_cannot_see_a_dashboard(setup):
    _, _, board = setup
    other = Client()
    upload(other)
    assert other.get(url("dashboard", board.dashboard_id)).status_code == 404
    response = other.get(url("dashboard_tile", board.dashboard_id, 1), headers=HTMX)
    assert response.status_code == 204 and response["HX-Redirect"] == HOME


def test_rename_a_dashboard(setup, client):
    _, _, board = setup
    client.post(url("dashboard_create"), {"name": "Other"})
    page = client.post(url("dashboard_rename", board.dashboard_id), {"name": "other"})
    assert "Another dashboard is already called" in page.content.decode()
    client.post(url("dashboard_rename", board.dashboard_id), {"name": "Branch view"})
    assert board.name == "Branch view"
