"""Phase 4: charts built from a query's result, and the Chart tab on the query screen."""

import json
import re

import pytest
from django.urls import reverse

from my_insights import charting
from my_insights.loading import load_file
from my_insights.workspace import ChartSpec, GroupItem, ValueItem, ValueStyle
from tests.conftest import csv_bytes
from tests.test_querying import HTMX, QUERY, SALES, post, run, spec_of, upload

ITEMS_40 = csv_bytes(
    "Item,Branch,Amount,Qty", *(f"I{n:02d},{'AB'[n % 2]},{n},{n % 3}" for n in range(1, 41))
)


@pytest.fixture
def sales(workspace):
    return workspace, load_file(workspace, "sales.csv", SALES)[0]


@pytest.fixture
def items(workspace):
    return workspace, load_file(workspace, "items.csv", ITEMS_40)[0]


def draw(workspace, dataset, spec, chart=None):
    _, _, result, plan = run(workspace, dataset, spec)
    chart = chart or charting.default_chart(spec, plan)
    return chart, charting.build_chart(chart, spec, plan, result.rows)


def group(key, column, part=""):
    return GroupItem(key, column, part)


def value(key, column, agg="sum", fmt=""):
    return ValueItem(key, column, agg, "", fmt)


# ---------- picking fields ----------


def test_default_chart_picks_x_and_colour_from_group_by(sales):
    workspace, dataset = sales
    one = spec_of(dataset, [group("a", "Branch")], [value("s", "Amount")])
    chart, built = draw(workspace, dataset, one)
    assert (chart.chart_type, chart.x, chart.series) == ("column", "a", "")
    assert not built.problems

    by_date = spec_of(dataset, [group("d", "Date", "month")], [value("s", "Amount")])
    assert draw(workspace, dataset, by_date)[0].chart_type == "line"

    two = spec_of(
        dataset, [group("d", "Date", "month"), group("b", "Branch")], [value("s", "Amount")]
    )
    chart, built = draw(workspace, dataset, two)
    assert (chart.chart_type, chart.x, chart.series) == ("stacked_column", "d", "b")
    names = [series["name"] for series in built.option["series"]]
    assert set(names) == {"MUM", "DEL", "(blank)", "mum"}


def test_no_groups_or_no_values_gives_a_clear_message(sales):
    workspace, dataset = sales
    totals_only = spec_of(dataset, values=[value("s", "Amount")])
    built = draw(workspace, dataset, totals_only)[1]
    assert built.option is None
    assert "needs a field in Group by and one in Values" in built.problems[0]


# ---------- each chart type ----------


@pytest.mark.parametrize("kind", list(charting.CHART_TYPES))
def test_every_chart_type_builds_an_option(sales, kind):
    workspace, dataset = sales
    spec = spec_of(dataset, [group("b", "Branch")], [value("s", "Amount")])
    if kind == "scatter":
        spec.values.append(value("q", "Qty"))
    chart = ChartSpec(chart_type=kind, x="b")
    built = draw(workspace, dataset, spec, chart)[1]
    assert not built.problems, built.problems
    option = built.option
    json.dumps(option)  # plain JSON: no code sent to the page
    assert option["series"]
    if kind in charting.ROUND:
        assert option["series"][0]["type"] == "pie"
        assert {d["name"] for d in option["series"][0]["data"]} == {"MUM", "DEL", "(blank)", "mum"}
    elif kind == "scatter":
        assert option["series"][0]["type"] == "scatter"
    else:
        category_axis = option["yAxis" if kind in charting.HORIZONTAL else "xAxis"]
        assert category_axis["type"] == "category"
        assert len(category_axis["data"]) == 4


def test_dates_keep_time_order_and_plain_labels(sales):
    workspace, dataset = sales
    spec = spec_of(dataset, [group("d", "Date", "month")], [value("s", "Amount")])
    built = draw(workspace, dataset, spec)[1]
    assert built.option["xAxis"]["data"] == ["Mar 2026", "Apr 2026", "May 2026", "Oct 2026"]
    assert built.option["series"][0]["data"] == [30, 150, 200, 70]


def test_combo_puts_lines_on_the_right_axis(sales):
    workspace, dataset = sales
    spec = spec_of(dataset, [group("b", "Branch")], [value("s", "Amount"), value("q", "Qty")])
    chart = ChartSpec(chart_type="combo", x="b")
    option = draw(workspace, dataset, spec, chart)[1].option
    assert [s["type"] for s in option["series"]] == ["bar", "line"]
    assert option["series"][1]["yAxisIndex"] == 1 and "yAxisIndex" not in option["series"][0]
    assert len(option["yAxis"]) == 2

    chart.styles["q"] = ValueStyle("bar", "left")
    option = draw(workspace, dataset, spec, chart)[1].option
    assert [s["type"] for s in option["series"]] == ["bar", "bar"]
    assert len(option["yAxis"]) == 1


def test_percent_stacked_adds_up_to_100(sales):
    workspace, dataset = sales
    spec = spec_of(
        dataset, [group("d", "Date", "year"), group("b", "Branch")], [value("s", "Amount")]
    )
    chart = ChartSpec(chart_type="stacked_column", x="d", series="b", percent=True)
    option = draw(workspace, dataset, spec, chart)[1].option
    column_total = sum(series["data"][0] or 0 for series in option["series"])
    assert column_total == pytest.approx(100, abs=0.05)
    assert option["yAxis"][0]["max"] == 100 and option["series"][0]["_fmt"] == "pct"


def test_sort_by_value_and_number_formats(sales):
    workspace, dataset = sales
    spec = spec_of(dataset, [group("i", "Item")], [value("s", "Amount", fmt="r2")])
    chart = ChartSpec(chart_type="column", x="i", sort="desc")
    option = draw(workspace, dataset, spec, chart)[1].option
    assert option["xAxis"]["data"] == ["A1", "B2", "C3"]  # 300, 80, 70
    assert option["series"][0]["_fmt"] == "r2" and option["yAxis"][0]["_fmt"] == "rshort"
    chart.sort = "asc"
    assert draw(workspace, dataset, spec, chart)[1].option["xAxis"]["data"] == ["C3", "B2", "A1"]


def test_hidden_values_and_text_values_are_left_off(sales):
    workspace, dataset = sales
    spec = spec_of(
        dataset,
        [group("b", "Branch")],
        [value("s", "Amount"), value("q", "Qty"), value("m", "Date", "max")],
    )
    chart = ChartSpec(chart_type="column", x="b", hidden=["q"])
    built = draw(workspace, dataset, spec, chart)[1]
    assert [s["name"] for s in built.option["series"]] == ["Sum of Amount"]
    assert any("“Max of Date” is not a number" in note for note in built.notes)
    chart.hidden = ["q", "s"]
    assert "Tick at least one number value" in draw(workspace, dataset, spec, chart)[1].problems[0]


# ---------- too many points ----------


def test_top_n_with_others_for_sums(items):
    workspace, dataset = items
    spec = spec_of(dataset, [group("i", "Item")], [value("s", "Amount")])
    chart = ChartSpec(chart_type="column", x="i")
    built = draw(workspace, dataset, spec, chart)[1]
    labels = built.option["xAxis"]["data"]
    assert len(labels) == 31 and labels[-1] == "Others"
    assert built.option["series"][0]["data"][-1] == sum(range(1, 11))  # the 10 smallest
    assert "top 30 of 40" in built.notes[0]

    chart.top_n = 5
    labels = draw(workspace, dataset, spec, chart)[1].option["xAxis"]["data"]
    assert labels == ["I36", "I37", "I38", "I39", "I40", "Others"]  # query order kept


def test_pie_shows_top_10_and_averages_get_no_others(items):
    workspace, dataset = items
    spec = spec_of(dataset, [group("i", "Item")], [value("s", "Amount")])
    data = draw(workspace, dataset, spec, ChartSpec(chart_type="pie", x="i"))[1].option["series"][
        0
    ]["data"]
    assert len(data) == 11 and data[-1]["name"] == "Others"

    average = spec_of(dataset, [group("i", "Item")], [value("a", "Amount", "avg")])
    built = draw(workspace, dataset, average, ChartSpec(chart_type="column", x="i"))[1]
    assert (
        "Others" not in built.option["xAxis"]["data"] and len(built.option["xAxis"]["data"]) == 30
    )
    assert "can't be added" in built.notes[0]


def test_line_with_too_many_points_is_refused(items, monkeypatch):
    workspace, dataset = items
    monkeypatch.setattr(charting, "MAX_POINTS", 10)
    spec = spec_of(dataset, [group("i", "Item")], [value("s", "Amount")])
    built = draw(workspace, dataset, spec, ChartSpec(chart_type="line", x="i"))[1]
    assert built.option is None and "would have 40 points; the most is 10" in built.problems[0]


# ---------- messages ----------


def test_pie_with_two_values_and_scatter_with_one(sales):
    workspace, dataset = sales
    two = spec_of(dataset, [group("b", "Branch")], [value("s", "Amount"), value("q", "Qty")])
    built = draw(workspace, dataset, two, ChartSpec(chart_type="donut", x="b"))[1]
    assert "A donut shows one value" in built.problems[0] and "“Sum of Qty”" in built.problems[0]

    one = spec_of(dataset, [group("b", "Branch")], [value("s", "Amount")])
    built = draw(workspace, dataset, one, ChartSpec(chart_type="scatter", x="b"))[1]
    assert "needs two number values" in built.problems[0]


def test_a_removed_field_is_named_and_the_chart_moves_on(sales):
    workspace, dataset = sales
    spec = spec_of(
        dataset, [group("d", "Date", "month"), group("b", "Branch")], [value("s", "Amount")]
    )
    chart = ChartSpec(chart_type="stacked_column", x="d", series="b", names={"b": "Branch"})
    spec.groups = spec.groups[:1]
    assert (
        charting.broken_reason(chart, spec)
        == "The colour field “Branch” is no longer in the query."
    )
    built = draw(workspace, dataset, spec, chart)[1]
    assert "The colour field “Branch” is no longer" in built.notes[0]
    assert chart.series == "" and built.option is not None
    assert charting.broken_reason(chart, None) == "Its query was removed."


# ---------- the screen ----------


SALES_FIELDS = {
    "groups": "g1",
    "g1_column": "Branch",
    "values": "v1",
    "v1_column": "Amount",
    "v1_agg": "sum",
}


def chart_fields(**changes):
    fields = {
        "chart_touched": "1",
        "chart_type": "column",
        "chart_x": "g1",
        "chart_values": "v1",
        "chart_shown": "v1",
        "chart_legend": "1",
    }
    fields.update(changes)
    return fields


def chart_option(page: str) -> dict:
    found = re.search(
        r'<script id="insights-chart-option" type="application/json">(.*?)</script>',
        page,
        re.DOTALL,
    )
    assert found, "no chart on the page"
    return json.loads(found.group(1))


@pytest.mark.django_db
def test_chart_tab_draws_and_redraws_with_the_query(client):
    _workspace, dataset = upload(client)
    page = post(client, dataset, tab="chart", **SALES_FIELDS).content.decode()
    assert 'name="chart_type"' in page and "Download PNG" in page
    assert chart_option(page)["series"][0]["name"] == "Sum of Amount"

    fields = SALES_FIELDS | {
        "filters": "f1",
        "f1_column": "Branch",
        "f1_op": "in",
        "f1_values": "DEL",
    }
    page = post(client, dataset, tab="chart", **fields, **chart_fields()).content.decode()
    assert chart_option(page)["xAxis"]["data"] == ["DEL"]

    page = post(
        client, dataset, tab="chart", **SALES_FIELDS, **chart_fields(chart_type="pie")
    ).content.decode()
    assert chart_option(page)["series"][0]["type"] == "pie"
    table = post(
        client, dataset, tab="table", **SALES_FIELDS, **chart_fields(chart_type="pie")
    ).content.decode()
    assert "insights-chart-option" not in table and 'value="pie" class="hidden" checked' in table


@pytest.mark.django_db
def test_chart_json_is_escaped_in_the_page(client):
    tricky = csv_bytes("Name,Amount", '"</script><b>x",5', "ok,3")
    _workspace, dataset = upload(client, "tricky.csv", tricky)
    fields = {
        "groups": "g1",
        "g1_column": "Name",
        "values": "v1",
        "v1_column": "Amount",
        "v1_agg": "sum",
    }
    page = post(client, dataset, tab="chart", **fields).content.decode()
    assert "</script><b>x" not in page
    assert "</script><b>x" in chart_option(page)["xAxis"]["data"]


@pytest.mark.django_db
def test_save_a_chart_on_a_saved_query_then_remove_the_query(client):
    workspace, dataset = upload(client)
    post(client, dataset, action="save", name="By branch", **SALES_FIELDS)
    query = next(iter(workspace.queries.values()))
    fields = SALES_FIELDS | {"query_id": query.query_id}
    response = post(
        client,
        dataset,
        tab="chart",
        action="save_chart",
        chart_name="Branch bars",
        **fields,
        **chart_fields(),
    )
    assert response.status_code == 204
    chart = next(iter(workspace.charts.values()))
    assert chart.query_id == query.query_id and chart.own_query is None
    assert response.headers["HX-Redirect"].endswith(f"?chart={chart.chart_id}&saved=chart")

    page = client.get(QUERY + f"?chart={chart.chart_id}&saved=chart").content.decode()
    assert "Chart saved." in page and 'value="Branch bars"' in page and "follows query" in page
    assert chart_option(page)["series"]

    home = client.get(reverse("my_insights:home")).content.decode()
    assert "Branch bars" in home and "Its charts are removed too: “Branch bars”." in home

    clash = post(
        client,
        dataset,
        tab="chart",
        action="save_chart",
        chart_name="branch BARS",
        **fields,
        **chart_fields(),
    )
    assert "Another chart is already called “branch BARS”." in clash.content.decode()

    client.post(reverse("my_insights:query_remove", args=[query.query_id]))
    assert not workspace.queries and not workspace.charts


@pytest.mark.django_db
def test_chart_from_a_dataset_keeps_its_own_query(client):
    workspace, dataset = upload(client)
    page = client.get(QUERY + f"?dataset={dataset.dataset_id}&tab=chart").content.decode()
    assert "<title>" in page and "New chart" in page and "Put a field in Group by" in page
    response = post(
        client, dataset, tab="chart", action="save_chart", **SALES_FIELDS, **chart_fields()
    )
    chart = next(iter(workspace.charts.values()))
    assert response.status_code == 204 and chart.query_id is None
    assert chart.own_query.groups[0].column == "Branch" and not workspace.queries
    assert chart.name == "Column: Sum of Amount by Branch"
    assert workspace.charts_of([dataset.dataset_id]) == [chart]

    home = client.get(reverse("my_insights:home")).content.decode()
    assert "New chart" in home and chart.name in home
    assert "Charts removed too" in home  # the dataset's remove question names it

    client.post(reverse("my_insights:dataset_remove", args=[dataset.dataset_id]))
    assert not workspace.charts


@pytest.mark.django_db
def test_unsaved_query_changes_give_the_chart_its_own_copy(client):
    workspace, dataset = upload(client)
    post(client, dataset, action="save", name="By branch", **SALES_FIELDS)
    query = next(iter(workspace.queries.values()))
    changed = SALES_FIELDS | {"query_id": query.query_id, "v1_agg": "max"}
    response = post(client, dataset, tab="chart", action="save_chart", **changed, **chart_fields())
    chart = next(iter(workspace.charts.values()))
    assert chart.query_id is None and chart.own_query.values[0].agg == "max"
    assert response.headers["HX-Redirect"].endswith("&own=1")
    page = client.get(response.headers["HX-Redirect"]).content.decode()
    assert "keeps its own copy" in page


@pytest.mark.django_db
def test_a_chart_with_problems_is_not_saved(client):
    workspace, dataset = upload(client)
    page = post(
        client,
        dataset,
        tab="chart",
        action="save_chart",
        **SALES_FIELDS,
        **chart_fields(chart_shown=""),
    ).content.decode()
    assert "Fix the problems shown before saving the chart." in page and not workspace.charts


@pytest.mark.django_db
def test_charts_are_private_to_each_session(client, django_user_model):
    from django.test import Client

    workspace, dataset = upload(client)
    post(client, dataset, tab="chart", action="save_chart", **SALES_FIELDS, **chart_fields())
    chart = next(iter(workspace.charts.values()))
    other = Client()
    page = other.get(QUERY + f"?chart={chart.chart_id}", headers=HTMX)
    assert page.status_code in (204, 302)  # no data in that session: sent home, chart unseen
    other.post(reverse("my_insights:chart_remove", args=[chart.chart_id]))
    assert chart.chart_id in workspace.charts


@pytest.mark.django_db
def test_switching_to_combo_puts_the_second_value_on_the_right(client):
    _workspace, dataset = upload(client)
    fields = SALES_FIELDS | {"values": ["v1", "v2"], "v2_column": "Qty", "v2_agg": "sum"}
    styles = {"v1_caxis": "left", "v2_caxis": "left", "v1_ckind": "", "v2_ckind": ""}
    shown = chart_fields(chart_type="combo", chart_shown_type="donut", chart_values=["v1", "v2"])
    shown["chart_shown"] = ["v1", "v2"]
    page = post(client, dataset, tab="chart", **fields, **shown, **styles).content.decode()
    option = chart_option(page)
    assert [(s["type"], s.get("yAxisIndex", 0)) for s in option["series"]] == [
        ("bar", 0),
        ("line", 1),
    ]

    moved = shown | {"chart_shown_type": "combo"}  # the user puts Qty back on the left
    page = post(client, dataset, tab="chart", **fields, **moved, **styles).content.decode()
    assert len(chart_option(page)["yAxis"]) == 1


# ---------- review fixes ----------


def test_a_real_others_value_is_not_mixed_with_the_rest(workspace):
    data = csv_bytes("Item,Amount", "Others,1000", *(f"I{n:02d},{n}" for n in range(1, 41)))
    dataset = load_file(workspace, "others.csv", data)[0]
    spec = spec_of(dataset, [group("i", "Item")], [value("s", "Amount")])
    option = draw(workspace, dataset, spec, ChartSpec(chart_type="column", x="i"))[1].option
    labels, numbers = option["xAxis"]["data"], option["series"][0]["data"]
    assert numbers[labels.index("Others")] == 1000
    assert labels[-1] == "Others (rest)" and numbers[-1] == sum(range(1, 12))


def test_broken_reason_without_a_saved_name():
    from my_insights.workspace import QuerySpec

    chart = ChartSpec(x="gone")
    assert "no longer in the query" in charting.broken_reason(chart, QuerySpec(1))


@pytest.mark.django_db
def test_own_axis_choice_survives_switching_chart_type(client):
    _workspace, dataset = upload(client)
    fields = SALES_FIELDS | {"values": ["v1", "v2"], "v2_column": "Qty", "v2_agg": "sum"}
    base = chart_fields(chart_values=["v1", "v2"]) | {"chart_shown": ["v1", "v2"]}
    # On Combo the user moves Qty to the left side.
    post(
        client,
        dataset,
        tab="chart",
        **fields,
        **(base | {"chart_type": "combo"}),
        chart_shown_type="combo",
        v2_caxis="left",
        v2_ckind="line",
    )
    # Then Column (untouched control shows "left", the user's own choice kept) and back.
    page = post(
        client,
        dataset,
        tab="chart",
        **fields,
        **(base | {"chart_type": "column"}),
        chart_shown_type="combo",
        v2_caxis="left",
        v2_cwas="|left",
    ).content.decode()
    assert 'name="v2_cwas" value="|left"' in page
    page = post(
        client,
        dataset,
        tab="chart",
        **fields,
        **(base | {"chart_type": "combo"}),
        chart_shown_type="column",
        v2_caxis="left",
        v2_cwas="|left",
    ).content.decode()
    assert len(chart_option(page)["yAxis"]) == 1


@pytest.mark.django_db
def test_saving_the_query_from_an_own_query_chart_links_them(client):
    workspace, dataset = upload(client)
    post(client, dataset, tab="chart", action="save_chart", **SALES_FIELDS, **chart_fields())
    chart = next(iter(workspace.charts.values()))
    response = post(
        client,
        dataset,
        tab="chart",
        action="save",
        name="By branch",
        chart_id=chart.chart_id,
        **SALES_FIELDS,
        **chart_fields(),
    )
    query = next(iter(workspace.queries.values()))
    assert response.headers["HX-Redirect"].endswith(f"?chart={chart.chart_id}&saved=1")
    assert chart.query_id == query.query_id and chart.own_query is None


@pytest.mark.django_db
def test_chart_says_when_the_answer_was_cut(client):
    from tests.conftest import insights_settings

    _workspace, dataset = upload(client)
    fields = {
        "groups": "g1",
        "g1_column": "Date",
        "g1_part": "day",
        "values": "v1",
        "v1_column": "Amount",
        "v1_agg": "sum",
    }
    with insights_settings(PREVIEW_ROW_CAP=2):
        page = post(client, dataset, tab="chart", **fields).content.decode()
    assert "chart uses only the first ones" in page


@pytest.mark.django_db
def test_query_chart_tab_lists_its_saved_charts_and_updates_the_open_one(client):
    workspace, dataset = upload(client)
    post(client, dataset, action="save", name="By branch", **SALES_FIELDS)
    query = next(iter(workspace.queries.values()))
    fields = SALES_FIELDS | {"query_id": query.query_id}
    for name in ("Bars", "Pie view"):
        post(
            client,
            dataset,
            tab="chart",
            action="save_chart",
            chart_name=name,
            **fields,
            **chart_fields(),
        )
    bars, pie = sorted(workspace.charts.values(), key=lambda item: item.chart_id)

    # Opening the query's Chart tab shows both charts and "+ New chart" (the one on screen).
    page = client.get(QUERY + f"?open={query.query_id}&tab=chart").content.decode()
    assert f'href="?chart={bars.chart_id}"' in page and f'href="?chart={pie.chart_id}"' in page
    assert f'href="?open={query.query_id}&amp;tab=chart"' in page and "+ New chart" in page

    # Opening one chart and saving updates it, it does not add a new chart.
    page = client.get(QUERY + f"?chart={pie.chart_id}").content.decode()
    assert "Save chart changes" in page and 'aria-current="page"' in page
    post(
        client,
        dataset,
        tab="chart",
        action="save_chart",
        chart_name="Pie view",
        chart_id=pie.chart_id,
        **fields,
        **chart_fields(chart_type="pie"),
    )
    assert len(workspace.charts) == 2 and pie.spec.chart_type == "pie"


@pytest.mark.django_db
def test_dataset_chart_tab_lists_own_copy_charts(client):
    workspace, dataset = upload(client)
    post(
        client,
        dataset,
        tab="chart",
        action="save_chart",
        chart_name="Own one",
        **SALES_FIELDS,
        **chart_fields(),
    )
    chart = next(iter(workspace.charts.values()))
    page = client.get(QUERY + f"?dataset={dataset.dataset_id}&tab=chart").content.decode()
    assert f'href="?chart={chart.chart_id}"' in page and "Own one" in page


@pytest.mark.django_db
def test_colour_boxes_show_the_colour_the_chart_uses(client):
    _workspace, dataset = upload(client)
    fields = SALES_FIELDS | {"values": ["v1", "v2"], "v2_column": "Amount", "v2_agg": "avg"}
    page = post(
        client,
        dataset,
        tab="chart",
        **fields,
        **chart_fields(chart_values=["v1", "v2"], chart_shown=["v1", "v2"]),
    ).content.decode()
    assert "colour || '#5470c6'" in page and "colour || '#91cc75'" in page
    page = post(
        client,
        dataset,
        tab="chart",
        **fields,
        **chart_fields(chart_values=["v1", "v2"], chart_shown=["v1", "v2"], chart_theme="warm"),
    ).content.decode()
    assert "colour || '#d1495b'" in page and "colour || '#edae49'" in page
