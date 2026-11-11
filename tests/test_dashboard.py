"""Phase 5: dashboard tiles, global filters, click filters and KPIs (no pages)."""

import json
from datetime import date

import pytest

from my_insights import dashboard as dash
from my_insights.loading import load_file
from my_insights.workspace import (
    CalculatedField,
    ChartSpec,
    FilterItem,
    GroupItem,
    QuerySpec,
    SavedChart,
    SavedQuery,
    ValueItem,
)
from tests.test_querying import ITEMS, SALES

TODAY = date(2026, 6, 15)


@pytest.fixture
def board(workspace):
    """Sales + Items datasets, 3 saved queries, 1 chart, and a dashboard with every tile."""
    sales = load_file(workspace, "sales.csv", SALES)[0]
    items = load_file(workspace, "items.csv", ITEMS)[0]
    by_branch = QuerySpec(
        sales.dataset_id,
        groups=[GroupItem("b", "Branch")],
        values=[ValueItem("amt", "Amount", "sum", "", "r0"), ValueItem("avg", "Amount", "avg")],
    )
    by_month = QuerySpec(
        sales.dataset_id,
        groups=[GroupItem("m", "Date", "month"), GroupItem("b", "Branch")],
        values=[ValueItem("amt", "Amount", "sum")],
    )
    names = QuerySpec(items.dataset_id, groups=[GroupItem("n", "Name")], values=[])
    for query_id, (name, spec) in enumerate(
        [("By branch", by_branch), ("By month", by_month), ("Names", names)], start=1
    ):
        workspace.queries[query_id] = SavedQuery(query_id, name, spec)
    workspace.charts[1] = SavedChart(
        1, "Month chart", ChartSpec("stacked_column", x="m", series="b"), query_id=2
    )
    board = dash.create(workspace, "Monthly review")
    dash.add_tile(board, "chart", chart_id=1)
    dash.add_tile(board, "table", query_id=1)
    dash.add_tile(board, "kpi", query_id=1, value_key="amt")
    dash.add_tile(board, "table", query_id=3)
    dash.add_tile(board, "note", text="Before GST")
    return workspace, board


def draw(workspace, board, number, clicks=()):
    fields = dash.dashboard_fields(workspace, board)
    return dash.build_tile(workspace, board, board.tiles[number], fields, list(clicks), TODAY)


def branches(view):
    return [row["g0"] for row in view.result.rows]


# ---------- dashboards and tiles ----------


def test_names_are_unique_and_default_names_count_up(board):
    workspace, _ = board
    assert dash.name_error(workspace, "monthly REVIEW") == (
        "Another dashboard is already called “monthly REVIEW”."
    )
    assert dash.name_error(workspace, "") == "Type a name for the dashboard."
    assert dash.default_name(workspace) == "Dashboard 1"
    dash.create(workspace, "Dashboard 1")
    assert dash.default_name(workspace) == "Dashboard 2"


def test_tiles_keep_their_order_and_get_sizes_by_kind(board):
    _, board = board
    assert [tile.kind for tile in board.tiles] == ["chart", "table", "kpi", "table", "note"]
    assert [tile.tile_id for tile in board.tiles] == [1, 2, 3, 4, 5]
    assert [tile.width for tile in board.tiles] == ["half", "full", "third", "full", "half"]


def test_every_tile_draws(board):
    workspace, board = board
    chart = draw(workspace, board, 0)
    assert not chart.problem and chart.option["series"]
    assert chart.click["x"]["column"] == "Date" and chart.click["x"]["part"] == "month"
    assert chart.click["x"]["values"]["Apr 2026"] == "2026-04-01"
    assert chart.click["series"]["values"]["MUM"] == "MUM"
    assert chart.click["series"]["values"]["(blank)"] is None
    assert branches(draw(workspace, board, 1)) == ["DEL", "MUM", "mum", None]
    kpi = draw(workspace, board, 2)
    assert (kpi.kpi_label, kpi.kpi_value) == ("Sum of Amount", "₹ 450")
    assert draw(workspace, board, 4).problem == ""


def test_removed_chart_query_or_dataset_flags_the_tile(board):
    workspace, board = board
    del workspace.charts[1]
    assert draw(workspace, board, 0).problem.startswith("This chart was removed.")
    workspace.remove_query(1)
    assert draw(workspace, board, 1).problem.startswith("This query was removed.")
    assert draw(workspace, board, 2).problem.startswith("This query was removed.")
    items_id = workspace.queries[3].spec.dataset_id
    workspace.datasets.pop(items_id)
    assert draw(workspace, board, 3).problem == "The data “Names” uses was removed."
    assert len(board.tiles) == 5  # tiles stay, flagged


def test_kpi_is_the_grand_total_whatever_the_group_by(board):
    workspace, board = board
    board.tiles[2].value_key = "avg"
    board.tiles[2].title = "Average sale"
    kpi = draw(workspace, board, 2)
    assert (kpi.kpi_label, kpi.kpi_value) == ("Average sale", "90.00")
    workspace.queries[1].spec.top_n = 1  # Top N and total filters don't cut a KPI
    workspace.queries[1].spec.filters.append(FilterItem("t", "", "amt", "gt", ["1000"]))
    assert draw(workspace, board, 2).kpi_value == "90.00"
    board.tiles[2].value_key = "gone"
    assert "no longer in the query “By branch”" in draw(workspace, board, 2).problem


def test_kpi_choices_list_every_value_of_every_query(board):
    workspace, _ = board
    choices = dict(dash.kpi_choices(workspace))
    assert choices["1:amt"] == "By branch — Sum of Amount"
    assert choices["1:avg"] == "By branch — Average of Amount"


# ---------- global filters ----------


def test_fields_are_merged_by_name_first_dataset_sets_the_type(board):
    workspace, board = board
    fields = dash.dashboard_fields(workspace, board)
    assert {"Date", "Branch", "Amount", "Code", "Name"} <= fields.keys()
    assert fields["Date"].kind_class == "date" and fields["Amount"].kind_class == "number"


def test_a_global_filter_applies_to_tiles_with_that_field(board):
    workspace, board = board
    board.filters = [FilterItem("f1", "Branch", "", "in", ["MUM"])]
    assert branches(draw(workspace, board, 1)) == ["MUM"]
    assert draw(workspace, board, 2).kpi_value == "₹ 300"
    chart = draw(workspace, board, 0)
    assert [s["_series"] for s in chart.option["series"]] == ["MUM"]
    names = draw(workspace, board, 3)  # the Items data has no Branch
    assert names.skipped == ["Not filtered by Branch (no such field)"]
    assert len(names.result.rows) == 3


def test_a_field_of_another_type_is_not_filtered(board):
    workspace, board = board
    # Sales gets a number field "Name"; its tiles come first, so the filter is a number test
    sales = workspace.datasets[workspace.queries[1].spec.dataset_id]
    sales.calculated.append(CalculatedField("Name", "[Amount] * 2"))
    assert dash.dashboard_fields(workspace, board)["Name"].kind_class == "number"
    board.filters = [FilterItem("f1", "Name", "", "gt", ["150"])]
    assert branches(draw(workspace, board, 1)) == ["MUM"]
    assert draw(workspace, board, 3).skipped == ["Not filtered by Name (it is text here)"]


def test_a_filter_still_waiting_for_values_does_nothing(board):
    workspace, board = board
    board.filters = [FilterItem("f1", "Branch", "", "in", [])]
    assert not dash.filter_ready(board.filters[0])
    assert len(draw(workspace, board, 1).result.rows) == 4
    assert dash.filter_ready(FilterItem("f", "Date", "", "this_fy"))
    assert not dash.filter_ready(FilterItem("f", "Amount", "", "gt", ["", ""]))
    assert dash.filter_ready(FilterItem("f", "Date", "", "between", ["", "2026-05-01"]))


def test_date_filter_this_fy(board):
    workspace, board = board
    board.filters = [FilterItem("f1", "Date", "", "this_fy")]  # FY 2026-27 on 15-06-2026
    assert draw(workspace, board, 2).kpi_value == "₹ 420"  # the March sale is left out


# ---------- click filters ----------


@pytest.mark.parametrize(
    ("part", "raw", "start", "end"),
    [
        ("month", "2026-04-01", date(2026, 4, 1), date(2026, 4, 30)),
        ("month", "2026-12-01", date(2026, 12, 1), date(2026, 12, 31)),
        ("quarter", "2026-10-01", date(2026, 10, 1), date(2026, 12, 31)),
        ("fy_quarter", "2027-01-01", date(2027, 1, 1), date(2027, 3, 31)),
        ("week", "2026-04-06", date(2026, 4, 6), date(2026, 4, 12)),
        ("year", "2026", date(2026, 1, 1), date(2026, 12, 31)),
        ("fy", "2026", date(2026, 4, 1), date(2027, 3, 31)),
        ("day", "2026-04-05", date(2026, 4, 5), date(2026, 4, 5)),
    ],
)
def test_a_clicked_date_group_becomes_its_date_range(part, raw, start, end):
    assert dash.period_range(part, raw) == (start, end)


def test_clicks_become_the_right_filters():
    def made(**fields):
        item = dash.click_filter(dash.Click(1, "F", **fields))
        return item.op, item.values

    assert made(part="", kind="text", value="Delhi") == ("in", ["Delhi"])
    assert made(part="", kind="number", value="5") == ("eq", ["5"])
    assert made(part="", kind="boolean", value="true") == ("yes", [])
    assert made(part="", kind="text", value=None) == ("blank", [])
    assert made(part="fy", kind="date", value="2026") == ("between", ["2026-04-01", "2027-03-31"])
    assert dash.click_filter(dash.Click(1, "F", "month", "date", "not a date")) is None


def test_odd_click_data_is_dropped():
    raw = json.dumps(
        [
            {"tile": 1, "column": "Branch", "part": "", "kind": "text", "value": "MUM"},
            {"tile": "1", "column": "Branch", "kind": "text", "value": "x"},
            {"tile": 1, "column": "Date", "part": "hour", "kind": "date", "value": "x"},
            {"tile": 1, "column": "Qty", "kind": "money", "value": "1"},
            {"tile": 1, "column": "Qty", "kind": "number", "value": 5},
            "junk",
        ]
    )
    clicks = dash.read_clicks(raw)
    assert [(c.column, c.value) for c in clicks] == [("Branch", "MUM")]
    assert dash.read_clicks("{not json") == []
    many = json.dumps([{"tile": 1, "column": "B", "kind": "text"}] * 50)
    assert len(dash.read_clicks(many)) == dash.MAX_CLICKS


def test_a_click_filters_the_other_tiles_but_not_its_own(board):
    workspace, board = board
    # a stacked segment: Apr 2026 and MUM
    clicks = [
        dash.Click(1, "Date", "month", "date", "2026-04-01", "Date (month) = Apr 2026"),
        dash.Click(1, "Branch", "", "text", "MUM", "Branch = MUM"),
    ]
    assert branches(draw(workspace, board, 1, clicks)) == ["MUM"]
    assert draw(workspace, board, 2, clicks).kpi_value == "₹ 100"
    own = draw(workspace, board, 0, clicks)
    assert len(own.option["xAxis"]["data"]) == 4  # every month still drawn
    assert draw(workspace, board, 3, clicks).skipped == [
        "Not filtered by Date (no such field)",
        "Not filtered by Branch (no such field)",
    ]


def test_clicks_from_two_charts_add_up_with_the_global_filters(board):
    workspace, board = board
    board.filters = [FilterItem("f1", "Date", "", "this_fy")]
    clicks = [
        dash.Click(9, "Branch", "", "text", "MUM"),
        dash.Click(8, "Amount", "", "number", "200"),
    ]
    assert draw(workspace, board, 2, clicks).kpi_value == "₹ 200"
    blank = [dash.Click(9, "Branch", "", "text", None)]
    assert draw(workspace, board, 2, blank).kpi_value == "—"  # the blank-branch sale is in March
    board.filters = []
    assert draw(workspace, board, 2, blank).kpi_value == "₹ 30"
