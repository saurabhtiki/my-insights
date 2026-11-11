"""Phase 4.5: filters in "All of these" (AND) and "Any one of these" (OR)."""

import pytest

from my_insights import querying
from my_insights.loading import load_file
from my_insights.workspace import FilterItem, GroupItem, ValueItem
from tests.test_querying import QUERY, SALES, TODAY, post, run, spec_of, upload


@pytest.fixture
def sales(workspace):
    return workspace, load_file(workspace, "sales.csv", SALES)[0]


def by_item(dataset, filters):
    return spec_of(
        dataset,
        groups=[GroupItem("g", "Item")],
        values=[ValueItem("v", "Amount", "sum")],
        filters=filters,
    )


def test_all_of_and_any_of_mix(sales):
    workspace, dataset = sales
    spec = by_item(
        dataset,
        [
            FilterItem("f1", "Date", op="this_fy"),
            FilterItem("f2", "Branch", op="in", values=["DEL"], any_of=True),
            FilterItem("f3", "Amount", op="gt", values=["150"], any_of=True),
        ],
    )
    _labels, rows, _result, plan = run(workspace, dataset, spec)
    # this FY AND (Branch is DEL OR Amount > 150): B2 50 (DEL) and A1 200
    assert rows == [["A1", 200], ["B2", 50]]
    assert " OR " in plan.sql and plan.sql.count("?") == len(plan.params)


def test_between_inside_any_of_keeps_its_brackets(sales):
    workspace, dataset = sales
    spec = by_item(
        dataset,
        [
            FilterItem("f0", "Qty", op="gt", values=["1"]),
            FilterItem("f1", "Amount", op="between", values=["60", "80"], any_of=True),
            FilterItem("f2", "Qty", op="between", values=["4", "6"], any_of=True),
        ],
    )
    _labels, rows, _result, _plan = run(workspace, dataset, spec)
    assert rows == [["A1", 200], ["C3", 70]]  # Qty > 1 AND (Amount 60-80 OR Qty 4-6)


def test_an_empty_any_of_box_or_unfinished_filter_keeps_every_row(sales):
    workspace, dataset = sales
    spec = by_item(dataset, [FilterItem("f1", "Branch", op="in", values=[], any_of=True)])
    plan = querying.plan_query(spec, dataset, TODAY)
    assert "WHERE" not in plan.sql and plan.notes
    _labels, rows, _result, _plan = run(workspace, dataset, by_item(dataset, []))
    assert len(rows) == 3


def test_total_filters_can_be_any_of(sales):
    workspace, dataset = sales
    spec = spec_of(
        dataset,
        groups=[GroupItem("g", "Branch")],
        values=[ValueItem("v", "Amount", "sum")],
        filters=[
            FilterItem("f1", value_key="v", op="gt", values=["250"], any_of=True),
            FilterItem("f2", value_key="v", op="lt", values=["40"], any_of=True),
        ],
    )
    _labels, rows, result, _plan = run(workspace, dataset, spec)
    assert rows == [["MUM", 300], [None, 30]]
    assert result.totals["v0"] == 330  # the total is over the groups shown


@pytest.mark.django_db
def test_screen_moves_a_filter_between_boxes_and_saves_both(client):
    workspace, dataset = upload(client)
    fields = {
        "groups": "g1",
        "g1_column": "Item",
        "values": "v1",
        "v1_column": "Amount",
        "v1_agg": "sum",
        "filters": ["f1", "f2"],
        "f1_column": "Branch",
        "f1_op": "in",
        "f1_values": "DEL",
        "f1_any": "1",
        "f2_column": "Amount",
        "f2_op": "gt",
        "f2_a": "150",
        "f2_any": "1",
    }
    page = post(client, dataset, **fields).content.decode()
    assert "Any one of these" in page and 'name="f1_any" value="1"' in page
    assert "2 rows" in page  # DEL OR > 150

    post(client, dataset, action="move_filter:f2", **fields)
    moved = workspace.query_drafts["tab1"].filters
    assert [item.any_of for item in moved] == [True, False]

    added = post(client, dataset, action="add_any_filter:Qty", **fields)
    assert added.status_code == 200
    assert workspace.query_drafts["tab1"].filters[-1].any_of

    post(client, dataset, action="save", name="Delhi or big", **fields)
    saved = next(iter(workspace.queries.values()))
    assert [item.any_of for item in saved.spec.filters] == [True, True]
    page = client.get(QUERY + f"?open={saved.query_id}").content.decode()
    assert page.count('_any" value="1"') == 2
