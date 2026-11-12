"""Phase 9a: recipes. Save the setup (no data), load it on new files, match, apply."""

import json

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from my_insights import dashboard as dash
from my_insights import linking, query_dataset, querying
from my_insights.loading import load_file
from my_insights.recipe import (
    RecipeError,
    apply_recipe,
    guess_matches,
    match_problems,
    read_recipe,
    to_recipe,
)
from my_insights.workspace import (
    ApiSource,
    CalculatedField,
    ChartSpec,
    FilterItem,
    GroupItem,
    LinkSpec,
    LinkStep,
    NoteValue,
    QuerySpec,
    SavedChart,
    SavedQuery,
    ValueItem,
    store,
)
from tests.conftest import csv_bytes, insights_settings
from tests.test_querying import ITEMS, SALES, upload

# Next month's sales: new rows, same columns.
SALES_DEC = csv_bytes(
    "Date,Branch,Item,Amount,Cost,Qty",
    "05-12-2026,MUM,A1,400,100,3",
    "06-12-2026,DEL,C3,90,30,1",
)
NO_COST = csv_bytes("Date,Branch,Item,Amount,Qty", "05-12-2026,MUM,A1,400,3")


def build_setup(workspace, sales, items):
    """A bit of everything: formula, link, queries, a query-dataset, charts, a dashboard."""
    sales.calculated = [CalculatedField("Profit", "[Amount] - [Cost]")]
    combined = linking.save_link(
        workspace,
        LinkSpec(sales.dataset_id, [LinkStep(items.dataset_id, 0, [("Item", "Code")])]),
        "sales (with items)",
    )
    by_name = SavedQuery(
        workspace.new_query_id(),
        "By name",
        QuerySpec(
            combined.dataset_id,
            groups=[GroupItem("g1", "Name")],
            values=[ValueItem("v1", "Amount", "sum")],
        ),
    )
    by_branch = SavedQuery(
        workspace.new_query_id(),
        "Profit by branch",
        QuerySpec(
            sales.dataset_id,
            groups=[GroupItem("g1", "Branch")],
            values=[ValueItem("v1", "Profit", "sum")],
            filters=[FilterItem("f1", "Qty", op="gt", values=["0"])],
        ),
    )
    for query in (by_name, by_branch):
        workspace.queries[query.query_id] = query
    made = query_dataset.make_query_dataset(workspace, by_branch.query_id)
    on_made = SavedQuery(
        workspace.new_query_id(),
        "Top branch",
        QuerySpec(made.dataset_id, values=[ValueItem("v1", "Sum of Profit", "max")]),
    )
    workspace.queries[on_made.query_id] = on_made
    chart = SavedChart(workspace.new_chart_id(), "Name chart", ChartSpec(x="g1"), by_name.query_id)
    own = SavedChart(
        workspace.new_chart_id(),
        "Items count",
        ChartSpec(chart_type="pie", x="g1"),
        own_query=QuerySpec(items.dataset_id, groups=[GroupItem("g1", "Name")]),
    )
    workspace.charts |= {chart.chart_id: chart, own.chart_id: own}
    board = dash.create(workspace, "Monthly review")
    dash.add_tile(board, "chart", chart_id=chart.chart_id)
    dash.add_tile(board, "kpi", query_id=by_branch.query_id, value_key="v1")
    dash.add_tile(
        board,
        "note",
        text="Best: [[Top]]",
        values=[NoteValue(on_made.query_id, "v1", "Top")],
    )
    board.filters = [FilterItem("d1", "Branch", op="in", values=["MUM"])]
    return made


@pytest.fixture
def setup(workspace):
    sales = load_file(workspace, "sales.csv", SALES)[0]
    items = load_file(workspace, "items.csv", ITEMS)[0]
    build_setup(workspace, sales, items)
    return workspace


def fresh(*files):
    """A new workspace with these (name, bytes) files loaded."""
    _, workspace = store.create()
    for name, data in files:
        load_file(workspace, name, data)
    return workspace


def saved(workspace, name="Monthly"):
    """The recipe as it would come back from a file."""
    return read_recipe(json.dumps(to_recipe(workspace, name)).encode())


# ---------- saving ----------


def test_recipe_holds_the_setup_but_no_data(setup):
    items = next(d for d in setup.datasets.values() if d.name == "items")
    items.api_source = ApiSource("https://erp.example/items", headers="Authorization: secret-key")
    text = json.dumps(to_recipe(setup, "Monthly"))
    for data in ("Apple", "Ball", "erp.example", "secret-key", "samples"):
        assert data not in text
    recipe = json.loads(text)
    assert recipe["my_insights_recipe"] == 1 and recipe["name"] == "Monthly"
    kinds = [(d["name"], d["kind"]) for d in recipe["datasets"]]
    assert kinds == [
        ("sales", "source"),
        ("items", "source"),
        ("sales (with items)", "combined"),
        ("Profit by branch (result)", "query"),
    ]
    assert len(recipe["queries"]) == 3 and len(recipe["charts"]) == 2


def test_needs_trace_formulas_links_and_filters_back_to_the_files(setup):
    sales, items = saved(setup)["datasets"][:2]
    # Profit reads Amount and Cost; Item links; Branch is grouped and filtered; Qty filters.
    assert sales["needs"] == ["Amount", "Branch", "Cost", "Item", "Qty"]
    assert items["needs"] == ["Code", "Name"]  # Name used through the combined dataset
    assert sales["required"] and items["required"]


# ---------- reading ----------


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"not json", "not valid JSON"),
        (b'{"hello": 1}', "not a My Insights recipe"),
        (b'{"my_insights_recipe": 99, "datasets": []}', "version 99"),
        (b'{"my_insights_recipe": 1, "datasets": "x"}', "datasets must be a list"),
        (
            (
                b'{"my_insights_recipe": 1, "datasets": [], "queries": '
                b'[{"id": 1, "name": "Q", "spec": {"dataset_id": 5}}]}'
            ),
            "not in the recipe",
        ),
        (
            (
                b'{"my_insights_recipe": 1, "datasets": [{"id": 1, "name": "s", "kind": "source"}],'
                b' "charts": [{"id": 1, "name": "C", "spec": {}, "query_id": 9}]}'
            ),
            "follows a query that is not in the recipe",
        ),
    ],
)
def test_bad_files_get_a_specific_message(raw, message):
    with pytest.raises(RecipeError, match=message):
        read_recipe(raw)


def test_too_big_a_recipe_is_refused():
    with insights_settings(RECIPE_MAX_KB=1), pytest.raises(RecipeError, match="larger than 1 KB"):
        read_recipe(b" " * 2000)


# ---------- matching ----------


def test_matches_are_guessed_by_name_then_by_columns(setup):
    recipe = saved(setup)
    other = fresh(("sales_dec.csv", SALES_DEC), ("Items.csv", ITEMS))
    matches = guess_matches(recipe, other)
    names = {other.datasets[i].name for i in matches.values()}
    assert names == {"sales_dec", "Items"}
    assert match_problems(recipe, other, matches) == {}


def test_a_missing_column_is_named_and_blocks_apply(setup):
    recipe = saved(setup)
    other = fresh(("sales.csv", NO_COST), ("items.csv", ITEMS))
    other.queries[99] = SavedQuery(99, "Mine", QuerySpec(1))
    matches = guess_matches(recipe, other)
    problems = match_problems(recipe, other, matches)
    assert list(problems.values()) == ["“sales” is missing: Cost."]
    with pytest.raises(RecipeError, match="missing: Cost"):
        apply_recipe(other, recipe, matches)
    assert list(other.queries) == [99] and len(other.datasets) == 2  # nothing changed
    assert all(not d.calculated for d in other.datasets.values())


def test_a_required_dataset_must_be_picked_once(setup):
    recipe = saved(setup)
    other = fresh(("sales.csv", SALES))
    matches = guess_matches(recipe, other)
    assert "Pick a dataset" in " ".join(match_problems(recipe, other, matches).values())
    sales_id = next(iter(other.datasets))
    both = dict.fromkeys(matches, sales_id)
    assert "already picked" in " ".join(match_problems(recipe, other, both).values())


# ---------- applying ----------


def test_apply_rebuilds_everything_on_the_new_files(setup):
    recipe = saved(setup)
    other = fresh(("sales_dec.csv", SALES_DEC), ("items.csv", ITEMS))
    lines = apply_recipe(other, recipe, guess_matches(recipe, other))
    assert lines[0] == "Applied the recipe “Monthly”: 3 queries, 2 charts, 1 dashboard."
    names = sorted(d.name for d in other.datasets.values())
    assert names == ["Profit by branch (result)", "items", "sales (with items)", "sales_dec"]
    sales = next(d for d in other.datasets.values() if d.name == "sales_dec")
    assert [c.name for c in sales.calculated] == ["Profit"]
    for query in other.queries.values():  # every query runs on the new data
        dataset = other.datasets[query.spec.dataset_id]
        plan = querying.plan_query(query.spec, dataset)
        assert not plan.problems, (query.name, plan.problems)
    by_branch = next(q for q in other.queries.values() if q.name == "Profit by branch")
    rows = querying.run_query(
        other.connection, querying.plan_query(by_branch.spec, sales), None
    ).rows
    assert sorted(row["v0"] for row in rows) == [60, 300]  # December's profit
    board = next(iter(other.dashboards.values()))
    chart_tile, kpi, note = board.tiles
    assert other.charts[chart_tile.chart_id].name == "Name chart"
    assert other.queries[kpi.query_id].name == "Profit by branch"
    assert other.queries[note.values[0].query_id].name == "Top branch"
    assert board.filters[0].column == "Branch"
    own = next(c for c in other.charts.values() if c.own_query)
    assert other.datasets[own.own_query.dataset_id].name == "items"


def test_apply_replaces_old_work_but_keeps_the_data(setup):
    recipe = saved(setup, "Again")
    before = {d.dataset_id for d in setup.datasets.values() if not d.link and not d.query_id}
    old_queries = set(setup.queries)
    apply_recipe(setup, recipe, guess_matches(recipe, setup))
    assert not old_queries & set(setup.queries) and len(setup.queries) == 3
    assert len(setup.dashboards) == 1 and len(setup.charts) == 2
    sources = {d.dataset_id for d in setup.datasets.values() if not d.link and not d.query_id}
    assert sources == before
    assert "sales (with items)" in {d.name for d in setup.datasets.values()}  # not "(2)"


def test_a_failure_part_way_leaves_everything_as_it_was(setup, monkeypatch):
    recipe = saved(setup)
    other = fresh(("sales.csv", SALES), ("items.csv", ITEMS))
    other.queries[99] = SavedQuery(99, "Mine", QuerySpec(1))

    def fail(workspace, query_id):
        raise query_dataset.QueryDatasetFailed("the server is busy")

    monkeypatch.setattr(query_dataset, "make_query_dataset", fail)
    with pytest.raises(RecipeError, match="the server is busy"):
        apply_recipe(other, recipe, guess_matches(recipe, other))
    assert list(other.queries) == [99] and not other.charts and not other.dashboards
    assert sorted(d.name for d in other.datasets.values()) == ["items", "sales"]
    assert all(not d.calculated for d in other.datasets.values())


# ---------- pages and host hooks ----------

HOST_STORE: dict[str, dict] = {}


def host_save(request, recipe, name):
    HOST_STORE["7"] = recipe
    return f"Saved “{name}” in Stock Audit."


def host_load(request, recipe_id):
    return HOST_STORE.get(recipe_id)


def host_broken(*args):
    raise RuntimeError("database down")


def client_setup(client):
    workspace, sales = upload(client)
    _, items = upload(client, "items.csv", ITEMS)
    build_setup(workspace, sales, items)
    return workspace


@pytest.mark.django_db
def test_save_downloads_a_json_file_without_hooks(client):
    client_setup(client)
    home = client.get(reverse("my_insights:home")).content.decode()
    assert "Save recipe" in home and "Load recipe" in home
    response = client.post(reverse("my_insights:recipe_save"), {"name": "Monthly review"})
    assert response["Content-Disposition"] == 'attachment; filename="Monthly_review.json"'
    assert json.loads(response.content)["name"] == "Monthly review"


@pytest.mark.django_db
def test_host_hooks_save_and_load(client):
    client_setup(client)
    hooks = {
        "RECIPE_SAVE": "tests.test_recipe.host_save",
        "RECIPE_LOAD": "tests.test_recipe.host_load",
    }
    with insights_settings(**hooks):
        page = client.post(reverse("my_insights:recipe_save"), {"name": "Monthly"}).content.decode()
        assert "Saved “Monthly” in Stock Audit." in page and HOST_STORE["7"]["name"] == "Monthly"
        response = client.get(reverse("my_insights:recipe_load") + "?id=7")
        assert response.url == reverse("my_insights:recipe_match")
        missing = client.get(reverse("my_insights:recipe_load") + "?id=8")
        assert missing.url == reverse("my_insights:home")
        assert "was not found" in client.get(missing.url).content.decode()


@pytest.mark.django_db
def test_a_failing_hook_shows_a_message_not_a_crash(client):
    client_setup(client)
    with insights_settings(RECIPE_SAVE="tests.test_recipe.host_broken"):
        page = client.post(reverse("my_insights:recipe_save"), {"name": "M"}).content.decode()
    assert "Could not save the recipe" in page


@pytest.mark.django_db
def test_load_from_host_needs_the_hook(client):
    assert client.get(reverse("my_insights:recipe_load") + "?id=1").status_code == 404


@pytest.mark.django_db
def test_upload_match_and_apply_in_the_pages(client, setup):
    recipe_file = json.dumps(to_recipe(setup, "Monthly")).encode()
    workspace, _ = upload(client, "sales_dec.csv", SALES_DEC)
    upload(client, "items.csv", ITEMS)
    response = client.post(
        reverse("my_insights:recipe_upload"),
        {"recipe": SimpleUploadedFile("m.json", recipe_file, "application/json")},
    )
    assert response.url == reverse("my_insights:recipe_match")
    assert "Recipe “Monthly” is waiting" in client.get(reverse("my_insights:home")).content.decode()
    page = client.get(response.url).content.decode()
    assert "✓ Ready" in page and "Apply recipe" in page
    picks = {
        f"match_{rid}": wid
        for rid, wid in guess_matches(workspace.pending_recipe, workspace).items()
    }
    done = client.post(
        reverse("my_insights:recipe_match"),
        {**picks, "action": "apply", "token": workspace.pending_recipe["token"]},
    )
    assert done.url == reverse("my_insights:home")
    home = client.get(done.url).content.decode()
    assert "Applied the recipe “Monthly”" in home and workspace.pending_recipe is None
    assert len(workspace.queries) == 3


@pytest.mark.django_db
def test_pages_refuse_apply_with_a_missing_column(client, setup):
    workspace, _ = upload(client, "sales.csv", NO_COST)
    upload(client, "items.csv", ITEMS)
    workspace.pending_recipe = saved(setup)
    page = client.get(reverse("my_insights:recipe_match")).content.decode()
    assert "is missing: Cost." in page and "disabled" in page
    picks = {
        f"match_{rid}": wid
        for rid, wid in guess_matches(workspace.pending_recipe, workspace).items()
    }
    refused = client.post(
        reverse("my_insights:recipe_match"),
        {**picks, "action": "apply", "token": workspace.pending_recipe["token"]},
    )
    assert "missing: Cost" in refused.content.decode() and not workspace.queries


@pytest.mark.django_db
def test_a_bad_upload_and_cancel(client, setup):
    client.get(reverse("my_insights:home"))
    response = client.post(
        reverse("my_insights:recipe_upload"),
        {"recipe": SimpleUploadedFile("x.json", b"nope", "application/json")},
    )
    assert "not valid JSON" in client.get(response.url).content.decode()
    workspace, _ = upload(client)
    workspace.pending_recipe = saved(setup)
    client.post(reverse("my_insights:recipe_cancel"))
    assert workspace.pending_recipe is None


# ---------- review fixes: crafted recipes, renamed lookups, changed types ----------


def recipe_json(workspace) -> dict:
    return json.loads(json.dumps(to_recipe(workspace, "Monthly")))


def test_a_crafted_chart_colour_or_type_is_reset_not_used(setup):
    data = recipe_json(setup)
    chart = data["charts"][0]["spec"]
    chart["chart_type"] = "nope"
    chart["theme"] = "x"
    chart["styles"] = {"v1": {"kind": "x", "axis": "y", "color": "');alert(1);//"}}
    data["dashboards"][0]["tiles"][0] |= {"width": "huge", "color": "'><script>", "tone": "x"}
    recipe = read_recipe(data)
    spec = recipe["charts"][1].spec
    assert (spec.chart_type, spec.theme) == ("column", "default")
    assert spec.styles["v1"].color == "" and spec.styles["v1"].kind == ""
    tile = recipe["dashboards"][0].tiles[0]
    assert (tile.width, tile.color, tile.tone) == ("half", "", "soft")


@pytest.mark.parametrize(
    ("path", "bad"),
    [
        (("queries", 0, "spec", "groups", 0, "key"), "g1'"),
        (("charts", 0, "spec", "x"), "');x"),
        (("dashboards", 0, "tiles", 1, "value_key"), "<b>"),
    ],
)
def test_a_bad_field_key_is_refused(setup, path, bad):
    data = recipe_json(setup)
    target = data
    for step in path[:-1]:
        target = target[step]
    target[path[-1]] = bad
    with pytest.raises(RecipeError, match="bad field key"):
        read_recipe(data)


def test_deep_nesting_and_odd_host_values_are_refused():
    with pytest.raises(RecipeError, match="nested too deeply"):
        read_recipe(b"[" * 200_000)
    with pytest.raises(RecipeError, match="not a My Insights recipe"):
        read_recipe(42)


def test_a_lookup_matched_under_another_name_keeps_its_columns(workspace):
    """Items and sales both have "Name", so the lookup's becomes "Name (items)". Matched to
    "items_dec" it is "Name (items_dec)": queries, formulas and filters follow it."""
    sales = load_file(workspace, "sales.csv", csv_bytes("Item,Name,Amount", "A1,Ravi,10"))[0]
    items = load_file(workspace, "items.csv", csv_bytes("Code,Name", "A1,Apple"))[0]
    combined = linking.save_link(
        workspace,
        LinkSpec(sales.dataset_id, [LinkStep(items.dataset_id, 0, [("Item", "Code")])]),
        "joined",
    )
    assert "Name (items)" in {c.name for c in combined.columns}
    combined.calculated = [CalculatedField("Label", 'IF([Name (items)] = "Apple", 1, 0)')]
    workspace.queries[1] = SavedQuery(
        1,
        "By item name",
        QuerySpec(combined.dataset_id, groups=[GroupItem("g1", "Name (items)")]),
    )
    board = dash.create(workspace, "B")
    dash.add_tile(board, "table", query_id=1)
    board.filters = [FilterItem("d1", "Name (items)", op="in", values=["Apple"])]
    recipe = saved(workspace)

    other = fresh(
        ("sales.csv", csv_bytes("Item,Name,Amount", "A1,Ravi,10")),
        ("items_dec.csv", csv_bytes("Code,Name", "A1,Apple")),
    )
    apply_recipe(other, recipe, guess_matches(recipe, other))
    query = next(iter(other.queries.values()))
    assert query.spec.groups[0].column == "Name (items_dec)"
    joined = other.datasets[query.spec.dataset_id]
    assert joined.calculated[0].formula == 'IF([Name (items_dec)] = "Apple", 1, 0)'
    assert next(iter(other.dashboards.values())).filters[0].column == "Name (items_dec)"


def test_a_needed_column_of_another_type_blocks_apply(setup):
    recipe = saved(setup)
    text_qty = csv_bytes("Date,Branch,Item,Amount,Cost,Qty", "05-12-2026,MUM,A1,400,100,some")
    other = fresh(("sales.csv", text_qty), ("items.csv", ITEMS))
    problems = match_problems(recipe, other, guess_matches(recipe, other))
    assert list(problems.values()) == ["In “sales”: “Qty” is now text (was a whole number)."]


@pytest.mark.django_db
def test_odd_picks_and_a_stale_tab_are_handled(client, setup):
    workspace, _ = upload(client)
    upload(client, "items.csv", ITEMS)
    workspace.pending_recipe = saved(setup)
    token = workspace.pending_recipe["token"]
    odd = client.post(reverse("my_insights:recipe_match"), {"match_1": "²", "token": token})
    assert odd.status_code == 200 and "Pick a dataset" in odd.content.decode()
    stale = client.post(reverse("my_insights:recipe_match"), {"action": "apply", "token": "old"})
    assert "Another recipe was loaded" in stale.content.decode() and not workspace.queries


@pytest.mark.django_db
def test_the_match_screen_lists_needed_columns_and_gives_a_template(client, setup):
    workspace, _ = upload(client, "sales_dec.csv", SALES_DEC)
    workspace.pending_recipe = saved(setup)
    page = client.get(reverse("my_insights:recipe_match")).content.decode()
    assert 'Cost</span> <span class="text-base-content/50">(a whole number)' in page
    assert "Extra columns in your file are fine." in page
    sales = next(e for e in workspace.pending_recipe["datasets"] if e["name"] == "sales")
    response = client.get(reverse("my_insights:recipe_template"), {"id": sales["id"]})
    body = b"".join(response.streaming_content).decode("utf-8-sig")
    # In the file's own order; Date is not used by anything, so it is left out.
    assert body.strip() == "Branch,Item,Amount,Cost,Qty"
    assert "sales template.csv" in response["Content-Disposition"]
    sales["needs"] = ["=1+2"]  # a crafted recipe can't make Excel run a formula
    response = client.get(reverse("my_insights:recipe_template"), {"id": sales["id"]})
    assert b"".join(response.streaming_content).decode("utf-8-sig").strip() == "'=1+2"
    assert client.get(reverse("my_insights:recipe_template"), {"id": "99"}).status_code == 404
    workspace.pending_recipe = None
    assert client.get(reverse("my_insights:recipe_template"), {"id": "1"}).status_code == 302
