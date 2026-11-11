"""Phase 3: queries (Group by / Values / Filters / Sort) and calculated fields."""

import re
from datetime import date

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from my_insights import linking, querying
from my_insights.formula import Fields, FormulaError, compile_formula
from my_insights.loading import load_file
from my_insights.querying import BLANK
from my_insights.workspace import (
    SESSION_KEY,
    CalculatedField,
    FilterItem,
    GroupItem,
    LinkSpec,
    LinkStep,
    QuerySpec,
    SavedQuery,
    SortItem,
    ValueItem,
    store,
)
from tests.conftest import csv_bytes

TODAY = date(2026, 11, 10)  # in FY 2026-27
SALES = csv_bytes(
    "Date,Branch,Item,Amount,Cost,Qty",
    "05-04-2026,MUM,A1,100,60,2",
    "20-04-2026,DEL,B2,50,20,1",
    "03-05-2026,MUM,A1,200,150,5",
    "10-03-2026,,B2,30,10,0",
    "15-10-2026,mum,C3,70,0,12",
)
ITEMS = csv_bytes("Code,Name", "A1,Apple", "B2,Ball", "C3,Cup")
QUERY = reverse("my_insights:query")


@pytest.fixture
def sales(workspace):
    return workspace, load_file(workspace, "sales.csv", SALES)[0]


def run(workspace, dataset, spec, today=TODAY):
    plan = querying.plan_query(spec, dataset, today)
    assert not plan.problems, plan.problems
    result = querying.run_query(workspace.connection, plan, spec.top_n)
    labels = [column.label for column in plan.columns]
    rows = [[row[column.key] for column in plan.columns] for row in result.rows]
    return labels, rows, result, plan


def spec_of(dataset, groups=(), values=(), filters=(), sorts=(), top_n=None):
    return QuerySpec(
        dataset.dataset_id, list(groups), list(values), list(filters), list(sorts), top_n
    )


# ---------- formulas ----------


def fields_of(dataset):
    return querying.formula_fields(dataset)


def formula_values(workspace, dataset, formula):
    compiled = compile_formula(formula, fields_of(dataset))
    table = dataset.table_name
    sql = f'SELECT {compiled.sql} FROM "{table}" AS src'
    return [row[0] for row in workspace.connection.execute(sql).fetchall()], compiled


def test_arithmetic_and_divide_by_zero(sales):
    workspace, dataset = sales
    values, compiled = formula_values(workspace, dataset, "[Amount] - [Cost]")
    assert values == [40, 30, 50, 20, 70] and compiled.whole
    values, compiled = formula_values(workspace, dataset, "[Amount] / [Qty]")
    assert values[3] is None  # Qty 0: blank, not an error
    assert values[0] == 50 and not compiled.whole


def test_if_ifs_switch_and_logic(sales):
    workspace, dataset = sales
    values, _ = formula_values(workspace, dataset, 'IF([Qty] > 4, "Bulk", "Small")')
    assert values == ["Small", "Small", "Bulk", "Small", "Bulk"]
    values, _ = formula_values(
        workspace, dataset, 'IFS([Amount] >= 100, "Large", [Amount] >= 50, "Medium", "Small")'
    )
    assert values == ["Large", "Medium", "Large", "Small", "Medium"]
    # text matches ignore upper / lower case, like Excel
    values, _ = formula_values(workspace, dataset, 'SWITCH([Branch], "MUM", "West", "Other")')
    assert values == ["West", "Other", "West", "Other", "West"]
    values, _ = formula_values(workspace, dataset, '[Qty] > 1 AND NOT [Branch] = "DEL"')
    assert values == [True, False, True, False, True]
    values, _ = formula_values(workspace, dataset, '[Branch] & "-" & [Item]')
    assert values[0] == "MUM-A1" and values[3] == "-B2"


def test_dates_in_formulas(sales):
    workspace, dataset = sales
    values, _ = formula_values(workspace, dataset, 'DAYS_BETWEEN([Date], "30-04-2026")')
    assert values[:2] == [25, 10]
    values, _ = formula_values(workspace, dataset, "YEAR([Date]) * 100 + MONTH([Date])")
    assert values[0] == 202604


@pytest.mark.parametrize(
    ("formula", "message"),
    [
        ("DROP", "Unknown word “DROP”"),
        ("[Cots]", "Did you mean “[Cost]”?"),
        ("[Amount] +", "stops too early"),
        ("[Amount]; DELETE", "“;” can't be used"),
        ('"open', "missing its closing quote"),
        ("[Amount] + [Branch]", "Expected a number after “+”, but this is text"),
        ("IF([Qty], 1, 2)", "Expected yes / no as the IF condition"),
        ('IF([Qty] > 1, 1, "x")', "mix a number and text"),
        ("[Amount] / SUM([Cost])", "must be inside one too, e.g. SUM([Amount])"),
        ("SUM(SUM([Amount]))", "can't go inside another"),
        ("ROUND([Amount], [Qty])", "whole number from 0 to 9"),
        ("", "Type a formula"),
    ],
)
def test_bad_formulas_are_refused_with_a_clear_message(sales, formula, message):
    _, dataset = sales
    with pytest.raises(FormulaError) as error:
        compile_formula(formula, fields_of(dataset))
    assert message in str(error.value)


def test_text_in_a_formula_cannot_break_out_of_its_quotes(sales):
    workspace, dataset = sales
    values, _ = formula_values(workspace, dataset, '"x\'); DROP TABLE ds_1; --"')
    assert values[0] == "x'); DROP TABLE ds_1; --"
    assert workspace.connection.execute(f'SELECT count(*) FROM "{dataset.table_name}"').fetchall()
    with pytest.raises(FormulaError):
        compile_formula('[Amount]") FROM x --', fields_of(dataset))


def test_calculated_fields_use_each_other_but_not_themselves():
    fields = Fields({"Sales": "integer", "Cost": "integer"}, {"Profit": "[Sales] - [Cost]"})
    margin = compile_formula("SUM([Profit]) / SUM([Sales])", fields)
    assert margin.is_total and margin.kind == "number"
    loop = Fields({"Sales": "integer"}, {"A": "[B] + 1", "B": "[A] + 1"})
    with pytest.raises(FormulaError, match="uses itself"):
        compile_formula("[A]", loop, ("A",))


# ---------- the query engine ----------


def test_group_and_values(sales):
    workspace, dataset = sales
    spec = spec_of(
        dataset,
        [GroupItem("g", "Item")],
        [
            ValueItem("a", "Amount", "sum"),
            ValueItem("b", "Amount", "avg"),
            ValueItem("c", "Qty", "median"),
            ValueItem("d", "Branch", "count_unique"),
            ValueItem("e", "Amount", "max"),
            ValueItem("f", "*", "rows"),
        ],
    )
    labels, rows, result, _ = run(workspace, dataset, spec)
    assert labels == [
        "Item", "Sum of Amount", "Average of Amount", "Median of Qty",
        "Count unique of Branch", "Max of Amount", "Count of rows",
    ]  # fmt: skip
    assert rows[0] == ["A1", 300, 150.0, 3.5, 1, 200, 2]
    assert rows[1][:2] == ["B2", 80] and rows[1][4] == 1  # a blank branch is not counted
    assert result.totals == {"v0": 450, "v1": 90.0, "v2": 2.0, "v3": 3, "v4": 200, "v5": 5}


def test_date_groups_sort_by_time_and_show_plain_labels(sales):
    workspace, dataset = sales
    for part, expected in [
        ("month", ["Mar 2026", "Apr 2026", "May 2026", "Oct 2026"]),
        ("quarter", ["Q1 2026", "Q2 2026", "Q4 2026"]),
        ("fy", ["FY 2025-26", "FY 2026-27"]),
        ("fy_quarter", ["FY Q4 2025-26", "FY Q1 2026-27", "FY Q3 2026-27"]),
        ("week", ["Week of 09-03-2026", "Week of 30-03-2026", "Week of 20-04-2026",
                  "Week of 27-04-2026", "Week of 12-10-2026"]),
    ]:  # fmt: skip
        spec = spec_of(dataset, [GroupItem("g", "Date", part)], [ValueItem("v", "*", "rows")])
        _, rows, _, plan = run(workspace, dataset, spec)
        labels = [querying.format_value(row[0], plan.columns[0]) for row in rows]
        assert labels == expected, part


def test_blank_group_shows_as_blank_and_sorts_last(sales):
    workspace, dataset = sales
    spec = spec_of(dataset, [GroupItem("g", "Branch")], [ValueItem("v", "Amount", "sum")])
    _, rows, _, plan = run(workspace, dataset, spec)
    assert [row[0] for row in rows] == ["DEL", "MUM", "mum", None]
    assert querying.format_value(None, plan.columns[0]) == "(blank)"


def test_text_filters(sales):
    workspace, dataset = sales
    group = [GroupItem("g", "Branch")]
    value = [ValueItem("v", "*", "rows")]
    cases = [
        (FilterItem("f", "Branch", "", "in", ["MUM", BLANK]), ["MUM", None]),
        (FilterItem("f", "Branch", "", "not_in", ["MUM", BLANK]), ["DEL", "mum"]),
        (FilterItem("f", "Branch", "", "contains", ["um"]), ["MUM", "mum"]),
        (FilterItem("f", "Branch", "", "blank", []), [None]),
    ]
    for item, expected in cases:
        _, rows, _, _ = run(workspace, dataset, spec_of(dataset, group, value, [item]))
        assert [row[0] for row in rows] == expected, item.op


def test_number_and_date_filters(sales):
    workspace, dataset = sales
    value = [ValueItem("v", "Amount", "sum")]
    cases = [
        (FilterItem("f", "Amount", "", "between", ["50", "1,00"]), 220),
        (FilterItem("f", "Amount", "", "gt", ["₹ 60"]), 370),
        (FilterItem("f", "Date", "", "this_fy", []), 420),  # Apr 2026 – Mar 2027
        (FilterItem("f", "Date", "", "last_fy", []), 30),
        (FilterItem("f", "Date", "", "last_days", ["30"]), 70),
        (FilterItem("f", "Date", "", "between", ["2026-04-01", "30-04-2026"]), 150),
    ]
    for item, expected in cases:
        _, rows, _, _ = run(workspace, dataset, spec_of(dataset, (), value, [item]))
        assert rows == [[expected]], item.op


def test_unfinished_and_wrong_filter_values(sales):
    _, dataset = sales
    value = [ValueItem("v", "*", "rows")]
    waiting = spec_of(dataset, (), value, [FilterItem("f", "Branch", "", "in", [])])
    plan = querying.plan_query(waiting, dataset, TODAY)
    assert plan.ok and "Filter on Branch: tick at least one value." in plan.notes
    wrong = spec_of(dataset, (), value, [FilterItem("f", "Amount", "", "gt", ["lots"])])
    assert querying.plan_query(wrong, dataset, TODAY).problems == [
        "Filter on Amount: “lots” is not a number."
    ]


def test_total_filter_sort_top_n_and_total_row(sales):
    workspace, dataset = sales
    spec = spec_of(
        dataset,
        [GroupItem("g", "Item")],
        [ValueItem("v", "Amount", "sum"), ValueItem("n", "*", "rows")],
        [FilterItem("f", "", "v", "gt", ["50"])],
        [SortItem("s", "v", descending=True)],
        top_n=1,
    )
    _, rows, result, _ = run(workspace, dataset, spec)
    assert rows == [["A1", 300, 2]]
    assert result.totals == {"v0": 300, "v1": 2}  # the Total covers the groups shown


def test_no_group_gives_one_total_row_and_no_values_gives_a_list(sales):
    workspace, dataset = sales
    _, rows, result, _ = run(
        workspace, dataset, spec_of(dataset, (), [ValueItem("v", "*", "rows")])
    )
    assert rows == [[5]] and result.totals is None
    _, rows, _, _ = run(workspace, dataset, spec_of(dataset, [GroupItem("g", "Item")]))
    assert rows == [["A1"], ["B2"], ["C3"]]


def test_only_filters_shows_the_matching_rows(sales):
    workspace, dataset = sales
    dataset.calculated += [
        CalculatedField("Profit", "[Amount] - [Cost]"),
        CalculatedField("Total", "SUM([Amount])"),  # a total formula: not a row column
    ]
    labels, rows, result, plan = run(
        workspace,
        dataset,
        spec_of(dataset, filters=[FilterItem("f", "Branch", op="in", values=["MUM"])]),
    )
    assert labels == ["Date", "Branch", "Item", "Amount", "Cost", "Qty", "Profit"]
    assert [row[1:] for row in rows] == [
        ["MUM", "A1", 100, 60, 2, 40],
        ["MUM", "A1", 200, 150, 5, 50],
    ]
    assert result.totals is None and plan.notes  # the note says how to get totals
    labels, rows, _, _ = run(workspace, dataset, spec_of(dataset, top_n=2))  # nothing at all
    assert len(rows) == 2 and rows[0][2] == "A1"


def test_calculated_fields_in_a_query(sales):
    workspace, dataset = sales
    dataset.calculated += [
        CalculatedField("Profit", "[Amount] - [Cost]"),
        CalculatedField("Margin %", "SUM([Profit]) / SUM([Amount]) * 100"),
        CalculatedField("Size", 'IF([Qty] > 4, "Bulk", "Small")'),
    ]
    spec = spec_of(
        dataset,
        [GroupItem("g", "Size")],
        [ValueItem("p", "Profit", "sum"), ValueItem("m", "Margin %", "formula")],
        [FilterItem("f", "Profit", "", "gt", ["0"])],
    )
    labels, rows, _, plan = run(workspace, dataset, spec)
    assert labels == ["Size", "Sum of Profit", "Margin %"]
    assert rows[0][0] == "Bulk" and rows[0][1] == 120
    assert round(rows[0][2], 2) == round(120 / 270 * 100, 2)
    assert plan.columns[1].number_format == "0" and plan.columns[2].number_format == "2"


def test_a_total_formula_cannot_group_or_filter(sales):
    _, dataset = sales
    dataset.calculated.append(CalculatedField("Margin", "SUM([Amount]) / SUM([Cost])"))
    spec = spec_of(dataset, [GroupItem("g", "Margin")], [ValueItem("v", "*", "rows")])
    assert querying.plan_query(spec, dataset).problems == [
        "“Margin” is worked out per group, so it can only go in Values."
    ]


def test_missing_column_is_named(sales):
    _, dataset = sales
    spec = spec_of(dataset, [GroupItem("g", "Price")], [ValueItem("v", "*", "rows")])
    assert querying.plan_query(spec, dataset).problems == [
        "“Price” is no longer in sales. Remove it from Group by."
    ]


def test_query_on_a_combined_dataset(sales):
    workspace, dataset = sales
    items = load_file(workspace, "items.csv", ITEMS)[0]
    step = LinkStep(items.dataset_id, 0, [("Item", "Code")])
    combined = linking.save_link(workspace, LinkSpec(dataset.dataset_id, [step]), "Sales + Items")
    spec = spec_of(combined, [GroupItem("g", "Name")], [ValueItem("v", "Amount", "sum")])
    _, rows, _, _ = run(workspace, combined, spec)
    assert rows == [["Apple", 300], ["Ball", 80], ["Cup", 70]]


def test_indian_number_format():
    assert querying.indian_number(12345678.9, 2) == "1,23,45,678.90"
    assert querying.indian_number(-100000, 0) == "-1,00,000"
    assert querying.indian_number(999, 0) == "999"
    column = querying.ResultColumn("v", "", "number", number_format="r2", is_value=True)
    assert querying.format_value(150000, column) == "₹ 1,50,000.00"


def test_date_quick_picks():
    assert querying.date_range("this_fy", date(2026, 2, 1)) == (date(2025, 4, 1), date(2026, 3, 31))
    assert querying.date_range("last_month", date(2026, 3, 15)) == (
        date(2026, 2, 1),
        date(2026, 2, 28),
    )
    assert querying.date_range("this_month", date(2026, 12, 5))[1] == date(2026, 12, 31)


def test_saving_a_formula_renames_it_everywhere(sales):
    workspace, dataset = sales
    assert querying.save_formula(workspace, dataset, "Profit", "[Amount] - [Cost]") == ""
    assert querying.save_formula(workspace, dataset, "Double", "[Profit] * 2") == ""
    spec = spec_of(dataset, (), [ValueItem("v", "Profit", "sum")])
    workspace.queries[1] = SavedQuery(1, "Profit total", spec)
    assert querying.save_formula(workspace, dataset, "Gain", "[Amount] - [Cost]", "Profit") == ""
    assert dataset.calculated[1].formula == "[Gain] * 2"
    assert workspace.queries[1].spec.values[0].column == "Gain"
    assert "already has a field called “Amount”" in querying.save_formula(
        workspace, dataset, "Amount", "1"
    )
    assert "is not a column" in querying.save_formula(workspace, dataset, "X", "[Nope]")


# ---------- the screen ----------


def upload(client, name="sales.csv", data=SALES):
    client.post(
        reverse("my_insights:upload"),
        {"files": [SimpleUploadedFile(name, data, "text/csv")]},
        headers={"HX-Request": "true"},
    )
    workspace = store.get(client.session[SESSION_KEY])
    return workspace, list(workspace.datasets.values())[-1]


HTMX = {"HX-Request": "true"}


def post(client, dataset, **fields):
    data = {"dataset": dataset.dataset_id, "previous_dataset": dataset.dataset_id, "draft": "tab1"}
    data.update(fields)
    return client.post(QUERY, data, headers=HTMX)


@pytest.mark.django_db
def test_query_page_builds_a_result_step_by_step(client):
    workspace, dataset = upload(client)
    page = client.get(QUERY + f"?dataset={dataset.dataset_id}").content.decode()
    assert "Group by" in page and "Drag a field here" in page
    response = post(client, dataset, action="add_group:Item")
    page = response.content.decode()
    assert 'name="groups"' in page and "3 rows" in page
    key = workspace.query_drafts["tab1"].groups[0].key
    response = post(
        client, dataset, groups=key, **{f"{key}_column": "Item"}, drop_action="add_value:Amount"
    )
    page = response.content.decode()
    assert "Sum of Amount" in page and "Total" in page and "450" in page


@pytest.mark.django_db
def test_query_with_only_a_filter_lists_the_rows(client):
    workspace, dataset = upload(client)
    page = post(client, dataset, action="add_filter:Item").content.decode()
    key = workspace.query_drafts["tab1"].filters[0].key
    fields = {"filters": key, f"{key}_column": "Item", f"{key}_op": "in", f"{key}_values": "B2"}
    page = post(client, dataset, **fields).content.decode()
    assert "Showing the rows themselves" in page and "2 rows" in page
    rows = workspace.query_result_cache[3]
    assert [(row["c1"], row["c2"]) for row in rows] == [("DEL", "B2"), (None, "B2")]
    assert f'value="clear_filter:{key}"' in page
    assert 'x-data="dtDensity"' in page and 'x-data="dtBorder"' in page  # S/M/L and Lines


@pytest.mark.django_db
def test_clear_filter_empties_its_values_but_keeps_it(client):
    workspace, dataset = upload(client)
    post(client, dataset, action="add_filter:Item")
    key = workspace.query_drafts["tab1"].filters[0].key
    fields = {"filters": key, f"{key}_column": "Item", f"{key}_op": "in", f"{key}_values": "B2"}
    page = post(client, dataset, action=f"clear_filter:{key}", **fields).content.decode()
    kept = workspace.query_drafts["tab1"].filters
    assert [(item.key, item.values) for item in kept] == [(key, [])]
    assert "tick at least one value" in page and f'value="clear_filter:{key}"' not in page


@pytest.mark.django_db
def test_save_open_and_remove_a_query(client):
    workspace, dataset = upload(client)
    fields = {
        "groups": "g1",
        "g1_column": "Branch",
        "values": "v1",
        "v1_column": "*",
        "v1_agg": "rows",
    }
    response = post(client, dataset, action="save", name="By branch", **fields)
    assert response.status_code == 204
    saved = next(iter(workspace.queries.values()))
    assert response.headers["HX-Redirect"].endswith(f"?open={saved.query_id}&saved=1")
    page = client.get(QUERY + f"?open={saved.query_id}").content.decode()
    assert 'value="By branch"' in page and "Save changes" in page
    home = client.get(reverse("my_insights:home")).content.decode()
    assert "By branch" in home
    clash = post(client, dataset, action="save", name="by BRANCH", **fields).content.decode()
    assert "Another query is already called “by BRANCH”." in clash
    client.post(reverse("my_insights:query_remove", args=[saved.query_id]))
    assert not workspace.queries


@pytest.mark.django_db
def test_removing_a_dataset_removes_its_queries(client):
    workspace, dataset = upload(client)
    workspace.queries[7] = SavedQuery(7, "By item", spec_of(dataset, [GroupItem("g", "Item")]))
    page = client.get(reverse("my_insights:dataset", args=[dataset.dataset_id])).content.decode()
    assert "Saved queries removed too: “By item”." in page
    client.post(reverse("my_insights:dataset_remove", args=[dataset.dataset_id]))
    assert not workspace.queries


@pytest.mark.django_db
def test_formula_box_checks_and_saves(client):
    _, dataset = upload(client)
    check = client.post(
        reverse("my_insights:formula_check"),
        {
            "dataset": dataset.dataset_id,
            "formula_name": "Profit",
            "formula_text": "[Amount] - [Cost]",
        },
    ).content.decode()
    assert "OK — gives a whole number" in check and "40" in check
    bad = client.post(
        reverse("my_insights:formula_check"),
        {"dataset": dataset.dataset_id, "formula_text": "[Amount] - [Cots]"},
    ).content.decode()
    assert "Did you mean “[Cost]”?" in bad
    page = post(
        client,
        dataset,
        action="save_formula",
        formula_name="Profit",
        formula_text="[Amount] - [Cost]",
    ).content.decode()
    assert "Saved calculated field “Profit”." in page and dataset.calculated[0].name == "Profit"
    refused = post(
        client, dataset, action="save_formula", formula_name="Loss", formula_text="DROP"
    ).content.decode()
    assert "Unknown word “DROP”" in refused and len(dataset.calculated) == 1


@pytest.mark.django_db
def test_formula_must_match_per_row_or_per_group(client):
    _, dataset = upload(client)

    def check(text, mode):
        return client.post(
            reverse("my_insights:formula_check"),
            {"dataset": dataset.dataset_id, "formula_text": text, "formula_mode": mode},
        ).content.decode()

    assert "OK" in check("[Amount] - [Cost]", "row")
    assert "OK" in check("SUM([Amount]) / SUM([Cost])", "group")
    assert "“Per row” can&#x27;t use SUM" in check("SUM([Amount])", "row")
    assert "“Per group” needs a total, e.g. SUM([Basic])" in check("[Amount] * 2", "group")
    assert "OK" in check("[Amount] * 2", "bogus")  # an unknown switch value is ignored
    refused = post(
        client,
        dataset,
        action="save_formula",
        formula_name="Margin",
        formula_text="[Amount] - [Cost]",
        formula_mode="group",
    ).content.decode()
    assert "needs a total" in refused and not dataset.calculated


@pytest.mark.django_db
def test_formula_box_shows_only_what_the_switch_allows(client):
    _, dataset = upload(client)
    page = post(client, dataset).content.decode()
    assert 'name="formula_mode" value="row"' in page and "Row or total?" not in page

    # SUM chips only for Per group, IF only for Per row, ROUND for both
    def chip(name):  # the opening tag of a function chip
        return re.search(rf'data-fn="{name}"[^>]*>', page).group()

    assert "formulaMode === 'group'" in chip("SUM")
    assert "formulaMode === 'row'" in chip("IF")
    assert "x-show" not in chip("ROUND")
    # no Group by → a new field starts on Per row; with one → Per group
    assert "'', false)" in page
    grouped = post(client, dataset, action="add_group:Item").content.decode()
    assert "'', true)" in grouped


@pytest.mark.django_db
def test_tick_list_search_finds_any_value(client):
    _, dataset = upload(client)
    url = reverse("my_insights:filter_values")
    page = client.get(
        url,
        {
            "dataset": dataset.dataset_id,
            "key": "f1",
            "column": "Branch",
            "q": "de",
            "f1_values": "MUM",
        },
    ).content.decode()
    assert 'value="DEL"' in page and 'value="MUM" class="checkbox checkbox-xs" checked' in page
    assert 'value="mum"' not in page


@pytest.mark.django_db
def test_result_table_sorts_without_rerunning_the_form(client):
    _, dataset = upload(client)
    post(
        client,
        dataset,
        groups="g1",
        g1_column="Item",
        values="v1",
        v1_column="Amount",
        v1_agg="sum",
    )
    response = client.get(
        reverse("my_insights:query_table", args=["tab1"]) + "?sort=v0",
        headers={"HX-Request": "true", "HX-Target": "insights-query-result"},
    )
    body = response.content.decode().split("<tbody>")[1]
    assert body.index("C3") < body.index("B2") < body.index("A1")


@pytest.mark.django_db
def test_sort_link_on_the_screen_returns_only_the_table(client):
    # The browser sends the table's "?draft=…&sort=…" link to the page's own address.
    _, dataset = upload(client)
    screen = post(
        client,
        dataset,
        groups="g1",
        g1_column="Item",
        values="v1",
        v1_column="Amount",
        v1_agg="sum",
    ).content.decode()
    link = re.search(r'hx-get="(\?[^"]*sort=v0[^"]*)"', screen).group(1).replace("&amp;", "&")
    assert "draft=tab1" in link
    response = client.get(
        reverse("my_insights:query") + link,
        headers={"HX-Request": "true", "HX-Target": "insights-query-result"},
    )
    page = response.content.decode()
    assert 'id="query-screen"' not in page  # the table only, not the whole screen again
    body = page.split("<tbody>")[1]
    assert body.index("C3") < body.index("B2") < body.index("A1")


@pytest.mark.django_db
def test_query_screen_after_data_was_cleared(client):
    _, dataset = upload(client)
    client.post(reverse("my_insights:clear"))
    response = post(client, dataset, action="add_group:Item")
    assert response.status_code == 204 and "HX-Redirect" in response.headers


@pytest.mark.django_db
def test_other_users_cannot_see_a_draft(client):
    _, dataset = upload(client)
    post(client, dataset, groups="g1", g1_column="Item")
    from django.test import Client

    stranger = Client().get(reverse("my_insights:query_table", args=["tab1"]), headers=HTMX)
    assert "expired" in stranger.content.decode()


# ---------- review fixes ----------


def test_a_huge_number_of_days_is_a_note_not_a_crash(sales):
    _, dataset = sales
    for days in ("1e400", "inf", "nan"):
        spec = spec_of(
            dataset,
            (),
            [ValueItem("v", "*", "rows")],
            [FilterItem("f", "Date", "", "last_days", [days])],
        )
        plan = querying.plan_query(spec, dataset, TODAY)
        assert "Filter on Date: type how many days, e.g. 30." in plan.notes


def test_fields_that_repeat_each_other_are_refused_not_expanded_forever():
    calculated = {"F0": "[Qty] + [Qty]"}
    for level in range(1, 30):
        calculated[f"F{level}"] = f"[F{level - 1}] + [F{level - 1}]"
    fields = Fields({"Qty": "integer"}, calculated)
    with pytest.raises(FormulaError, match="too complex"):
        compile_formula("[F29]", fields)


@pytest.mark.django_db
def test_a_refused_save_still_counts_as_unsaved(client):
    _, dataset = upload(client)
    fields = {"groups": "g1", "g1_column": "Item"}
    post(client, dataset, action="save", name="Items", **fields)
    page = post(client, dataset, action="save", name="items", **fields).content.decode()
    assert "Another query is already called" in page
    assert 'name="dirty" value="1"' in page and "Not saved" in page


@pytest.mark.django_db
def test_table_clicks_reuse_the_result_and_escape_errors(client, monkeypatch):
    _, dataset = upload(client)
    post(
        client,
        dataset,
        groups="g1",
        g1_column="Item",
        values="v1",
        v1_column="Amount",
        v1_agg="sum",
    )

    def fail(*args, **kwargs):
        raise querying.QueryFailed("bad <b>value</b>")

    monkeypatch.setattr(querying, "run_query", fail)
    table_url = reverse("my_insights:query_table", args=["tab1"])
    headers = {"HX-Request": "true", "HX-Target": "insights-query-result"}
    assert "A1" in client.get(table_url + "?sort=v0", headers=headers).content.decode()
    post(client, dataset, groups="g1", g1_column="Branch", draft="tab2")  # another tab: no cache
    page = client.get(reverse("my_insights:query_table", args=["tab2"]), headers=headers)
    assert "bad &lt;b&gt;value&lt;/b&gt;" in page.content.decode()


@pytest.mark.django_db
def test_many_group_values_get_a_text_filter_and_tick_lists_load_on_open(client):
    rows = ["Code,Amount", *(f"C{n},{n}" for n in range(150))]
    _, dataset = upload(client, "codes.csv", csv_bytes(*rows))
    page = post(
        client,
        dataset,
        groups="g1",
        g1_column="Code",
        filters="f1",
        f1_column="Code",
        f1_op="not_in",
        f1_values="C7",
    ).content.decode()
    # the result column gets a "contains" box, not 150 tick boxes
    assert '<input type="search" name="g0"' in page and 'name="g0" value="C7"' not in page
    assert page.count('name="f1_values"') == 1  # only the ticked value until the list is opened
