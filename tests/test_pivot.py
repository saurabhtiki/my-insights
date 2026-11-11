"""Phase 4.5: an Across field spreads its values into columns (a pivot table)."""

import pytest

from my_insights import querying
from my_insights.loading import load_file
from my_insights.workspace import FilterItem, GroupItem, SortItem, ValueItem
from tests.test_charting import chart_option
from tests.test_querying import SALES, TODAY, post, spec_of, upload


@pytest.fixture
def sales(workspace):
    return workspace, load_file(workspace, "sales.csv", SALES)[0]


def across(dataset, values, across_item, groups=("Branch",), sorts=(), top_n=None):
    spec = spec_of(
        dataset,
        groups=[GroupItem(f"g{n}", name) for n, name in enumerate(groups)],
        values=values,
        sorts=sorts,
        top_n=top_n,
    )
    spec.across = across_item
    return spec


def spread(workspace, dataset, spec):
    plan = querying.plan_query(spec, dataset, TODAY)
    assert not plan.problems, plan.problems
    result = querying.run_query(workspace.connection, plan, spec.top_n)
    labels = [column.label for column in result.columns]
    rows = [[row[column.key] for column in result.columns] for row in result.rows]
    totals = [result.totals.get(column.key) for column in result.columns if column.is_value]
    return labels, rows, totals, result


SUM = [ValueItem("v", "Amount", "sum")]
MONTH = GroupItem("a", "Date", "month")


def test_months_across_in_time_order_with_totals(sales):
    workspace, dataset = sales
    labels, rows, totals, _result = spread(workspace, dataset, across(dataset, SUM, MONTH))
    assert labels == ["Branch", "Mar 2026", "Apr 2026", "May 2026", "Oct 2026", "Total"]
    assert rows == [
        ["DEL", None, 50, None, None, 50],
        ["MUM", None, 100, 200, None, 300],
        ["mum", None, None, None, 70, 70],
        [None, 30, None, None, None, 30],
    ]
    assert totals == [30, 150, 200, 70, 450]


def test_two_values_make_two_columns_per_across_value(sales):
    workspace, dataset = sales
    values = [ValueItem("v", "Amount", "sum"), ValueItem("w", "Qty", "sum")]
    spec = across(dataset, values, GroupItem("a", "Item"), groups=())
    labels, rows, totals, _result = spread(workspace, dataset, spec)
    assert labels[:3] == ["A1 · Sum of Amount", "A1 · Sum of Qty", "B2 · Sum of Amount"]
    assert labels[-2:] == ["Total · Sum of Amount", "Total · Sum of Qty"]
    assert rows == [[300, 7, 80, 1, 70, 12, 450, 20]]  # no Group by: one row
    assert totals == rows[0]


def test_totals_are_worked_out_not_added_up(sales):
    workspace, dataset = sales
    values = [ValueItem("v", "Amount", "avg"), ValueItem("w", "Item", "count_unique")]
    labels, rows, totals, _result = spread(workspace, dataset, across(dataset, values, MONTH))
    mum = dict(zip(labels, rows[1], strict=True))
    assert mum["Total · Average of Amount"] == 150  # (100 + 200) / 2
    assert mum["Total · Count unique of Item"] == 1  # A1 in both months: not 1 + 1
    assert totals[-2] == 90  # the average of all 5 rows
    assert totals[-1] == 3


def test_sort_and_top_n_go_by_the_row_total(sales):
    workspace, dataset = sales
    spec = across(dataset, SUM, MONTH, sorts=[SortItem("s", "v", descending=True)], top_n=2)
    labels, rows, totals, _result = spread(workspace, dataset, spec)
    assert [row[0] for row in rows] == ["MUM", "mum"]
    assert totals == [100, 200, 70, 370]  # only the 2 branches shown
    assert labels[1] == "Apr 2026"  # months with nothing left drop out


def test_too_many_across_values_shows_the_first_ones(sales, monkeypatch):
    workspace, dataset = sales
    monkeypatch.setattr(querying, "MAX_ACROSS", 2)
    labels, _rows, _totals, result = spread(workspace, dataset, across(dataset, SUM, MONTH))
    assert labels == ["Branch", "Mar 2026", "Apr 2026", "Total"]
    assert "showing the first 2" in result.notes[0]


def test_across_needs_a_value(sales):
    _workspace, dataset = sales
    plan = querying.plan_query(across(dataset, [], MONTH), dataset, TODAY)
    assert plan.across is None and "Across needs a value" in plan.notes[0]


def test_long_spec_turns_across_into_a_group(sales):
    _workspace, dataset = sales
    spec = across(dataset, SUM, MONTH, top_n=3)
    long = querying.long_spec(spec)
    assert [g.column for g in long.groups] == ["Branch", "Date"]
    assert long.across is None and long.top_n == 3 and spec.across is not None


FIELDS = {
    "groups": "g1",
    "g1_column": "Branch",
    "across": "a1",
    "a1_column": "Date",
    "a1_part": "month",
    "values": "v1",
    "v1_column": "Amount",
    "v1_agg": "sum",
}


@pytest.mark.django_db
def test_screen_shows_across_columns_and_charts_it_as_colours(client):
    workspace, dataset = upload(client)
    page = post(client, dataset, **FIELDS).content.decode()
    assert "Across (columns)" in page and 'name="across" value="a1"' in page
    assert "Apr 2026" in page and "Oct 2026" in page and "4 rows" in page

    chart = chart_option(post(client, dataset, tab="chart", **FIELDS).content.decode())
    assert chart["xAxis"]["data"][:2] == ["DEL", "MUM"]
    assert [s["name"] for s in chart["series"]] == ["Mar 2026", "Apr 2026", "May 2026", "Oct 2026"]

    post(client, dataset, action="remove:a1", **FIELDS)
    assert workspace.query_drafts["tab1"].across is None
    post(client, dataset, action="add_across:Item", **FIELDS)
    assert workspace.query_drafts["tab1"].across.column == "Item"

    table = client.get(
        "/insights/query/table/tab1/", {"sort": "g0"}, headers={"HX-Request": "true"}
    )
    assert table.status_code == 200


def test_across_value_keys_are_valid_table_keys(sales):
    workspace, dataset = sales
    _labels, _rows, _totals, result = spread(workspace, dataset, across(dataset, SUM, MONTH))
    assert all(column.key.isalnum() for column in result.columns)
    assert result.rows[0]["g0"] == "DEL"


def test_a_total_filter_with_no_group_by_leaves_no_columns(sales):
    workspace, dataset = sales
    spec = across(dataset, SUM, MONTH, groups=())
    spec.filters = [FilterItem("f", value_key="v", op="gt", values=["1000000"])]
    labels, rows, _totals, _result = spread(workspace, dataset, spec)
    assert rows == [] and labels == ["Total"]


@pytest.mark.django_db
def test_chart_with_across_keeps_top_n(client):
    _workspace, dataset = upload(client)
    fields = FIELDS | {"sorts": "s1", "s1_target": "v1", "s1_desc": "1", "top_n": "1"}
    chart = chart_option(post(client, dataset, tab="chart", **fields).content.decode())
    assert chart["xAxis"]["data"] == ["MUM"]  # only the branch the table shows
