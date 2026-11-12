"""Phase 9b: recipes remember API / database sources and fetch them again on Apply.
No real network or server: the fake API (test_api_import) and fake database (test_db_connect)."""

import json
import logging
import threading

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from my_insights import api_import
from my_insights import dashboard as dash
from my_insights.api_import import fetch_json, safe_url
from my_insights.db_loading import read_from_server
from my_insights.json_loading import load_parsed, parse_json
from my_insights.recipe import (
    RecipeError,
    apply_recipe,
    guess_matches,
    match_problems,
    read_recipe,
    to_recipe,
)
from my_insights.recipe_sources import FETCH, describe, filled, group_of, secret_fields
from my_insights.workspace import (
    SESSION_KEY,
    ApiSource,
    DbServer,
    GroupItem,
    QuerySpec,
    SavedQuery,
    ValueItem,
    store,
)
from tests.conftest import insights_settings
from tests.test_api_import import FakeApi
from tests.test_db_connect import PASSWORD, fake  # noqa: F401 (the fake database fixture)

pytestmark = pytest.mark.django_db

URL = "https://api.shop.in/orders?page=1&api_key=abc123"
HEADERS = "Accept: application/json\nAuthorization: Bearer xyz"
SALES = [{"Branch": "MUM", "Amount": 5}, {"Branch": "DEL", "Amount": 7}]
SERVER = DbServer("postgres", "192.168.1.20", 5432, "erp", "report", PASSWORD)


def import_api(workspace, api, name="sales"):
    source = ApiSource(URL, headers=HEADERS, name=name)
    groups = parse_json(fetch_json(source), rows_at="")
    datasets = load_parsed(workspace, name, safe_url(URL), groups)
    for dataset in datasets:
        if dataset.parent_id is None:
            dataset.api_source = source
    return datasets


def add_query(workspace, dataset, name, group, value):
    query = SavedQuery(
        workspace.new_query_id(),
        name,
        QuerySpec(
            dataset.dataset_id,
            groups=[GroupItem("g1", group)],
            values=[ValueItem("v1", value, "sum")],
        ),
    )
    workspace.queries[query.query_id] = query
    return query


@pytest.fixture
def api(monkeypatch):
    return FakeApi(monkeypatch, reply=SALES)


@pytest.fixture
def setup(workspace, api, fake):  # noqa: F811
    """November: "sales" from the API and "orders" from the database, a query on each."""
    [sales] = import_api(workspace, api)
    orders, _ = read_from_server(
        workspace,
        SERVER,
        [
            ("orders", 'SELECT * FROM "orders"', "orders"),
            ("customers", 'SELECT * FROM "customers"', "customers"),
        ],
    )
    by_branch = add_query(workspace, sales, "Sales by branch", "Branch", "Amount")
    add_query(workspace, orders, "Orders by customer", "customer_id", "amount")
    board = dash.create(workspace, "Monthly review")
    dash.add_tile(board, "kpi", query_id=by_branch.query_id, value_key="v1")
    api.requests.clear()
    return workspace


def december():
    _, workspace = store.create()
    return workspace


def ids(recipe):
    return {entry["name"]: entry["id"] for entry in recipe["datasets"]}


def group(recipe, name):
    """The key typed secrets are kept under: one per API / server."""
    return group_of(next(e for e in recipe["datasets"] if e["name"] == name)["connection"])


# ---------- saving ----------


def test_by_default_the_recipe_keeps_the_whole_connection(setup):
    text = json.dumps(to_recipe(setup, "Monthly"))
    for kept in ("api_key=abc123", "Bearer xyz", PASSWORD, "192.168.1.20", 'FROM \\"orders\\"'):
        assert kept in text
    assert "MUM" not in text  # still no data


def test_secrets_off_leaves_keys_and_passwords_empty(setup):
    text = json.dumps(to_recipe(setup, "Monthly", keep_secrets=False))
    for secret in ("abc123", "xyz", PASSWORD):
        assert secret not in text
    assert "page=1&api_key=" in text and "Authorization:" in text and "192.168.1.20" in text
    assert "Accept: application/json" in text  # not a secret: kept, and not asked


def test_a_damaged_connection_is_refused(setup):
    data = to_recipe(setup, "Monthly")
    data["datasets"][0]["connection"]["type"] = "ftp"
    with pytest.raises(RecipeError, match="damaged"):
        read_recipe(json.dumps(data))
    data["datasets"][0]["connection"] = {"type": "database", "host": "x"}
    with pytest.raises(RecipeError, match="damaged"):
        read_recipe(json.dumps(data))


# ---------- applying ----------


def test_apply_fetches_again_with_nothing_to_type(setup, api):
    recipe = read_recipe(json.dumps(to_recipe(setup, "Monthly")))
    workspace = december()
    matches = guess_matches(recipe, workspace)
    assert set(matches.values()) == {FETCH} and not match_problems(recipe, workspace, matches)
    lines = apply_recipe(workspace, recipe, matches)
    assert "Fetched again: sales, orders, customers." in lines
    names = {d.name: d for d in workspace.datasets.values()}
    assert set(names) == {"sales", "orders", "customers"}
    # Foreign keys come back too, as after a manual import: orders.customer_id → customers.id
    assert names["orders"].foreign_keys == [("customer_id", names["customers"].dataset_id, "id")]
    assert names["sales"].api_source.url == URL and names["orders"].db_source.server == SERVER
    assert len(workspace.queries) == 2 and len(workspace.dashboards) == 1
    [request] = api.requests
    assert request.full_url == URL and request.get_header("Authorization") == "Bearer xyz"


def test_secrets_off_asks_for_them_then_works(setup, api):
    recipe = read_recipe(json.dumps(to_recipe(setup, "Monthly", keep_secrets=False)))
    workspace = december()
    matches = guess_matches(recipe, workspace)
    problems = match_problems(recipe, workspace, matches)
    sales, orders = ids(recipe)["sales"], ids(recipe)["orders"]
    assert problems[sales] == "Type Header Authorization, “api_key” in the address to fetch it."
    assert problems[orders] == "Type Password to fetch it."
    with pytest.raises(RecipeError, match="Type Password"):
        apply_recipe(workspace, recipe, matches)
    typed = {
        group(recipe, "sales"): {"header:Authorization": "Bearer xyz", "url:api_key": "abc123"},
        group(recipe, "orders"): {"password": PASSWORD},
    }
    apply_recipe(workspace, recipe, matches, typed)
    assert api.requests[0].full_url == URL
    assert api.requests[0].get_header("Authorization") == "Bearer xyz"
    assert len(workspace.queries) == 2


def test_a_failed_fetch_changes_nothing(setup, caplog):
    recipe = read_recipe(json.dumps(to_recipe(setup, "Monthly")))
    workspace = december()
    matches = guess_matches(recipe, workspace)
    wrong = {group(recipe, "orders"): {"password": "not-it"}}
    with caplog.at_level(logging.DEBUG), pytest.raises(RecipeError) as refused:
        apply_recipe(workspace, recipe, matches, wrong)
    message = str(refused.value)
    assert "“orders”" in message and "password is wrong" in message
    assert "Nothing was changed" in message
    assert not workspace.datasets and not workspace.queries  # the API data went out again
    assert "not-it" not in caplog.text and PASSWORD not in caplog.text


def test_fetched_data_without_a_needed_column_is_refused(setup, api):
    recipe = read_recipe(json.dumps(to_recipe(setup, "Monthly")))
    api.reply = [{"Branch": "MUM"}]
    workspace = december()
    with pytest.raises(RecipeError, match="does not fit the recipe.*missing: Amount"):
        apply_recipe(workspace, recipe, guess_matches(recipe, workspace))
    assert not workspace.datasets and not workspace.queries


def test_an_uploaded_dataset_can_still_be_picked(setup):
    recipe = read_recipe(json.dumps(to_recipe(setup, "Monthly")))
    workspace = december()
    [mine] = load_parsed(workspace, "my sales", "file", parse_json(json.dumps(SALES).encode()))
    matches = guess_matches(recipe, workspace) | {ids(recipe)["sales"]: mine.dataset_id}
    apply_recipe(workspace, recipe, matches)
    assert {d.name for d in workspace.datasets.values()} == {"my sales", "orders", "customers"}


def test_the_same_safety_rules_as_an_import(setup, api):
    recipe = read_recipe(json.dumps(to_recipe(setup, "Monthly")))
    api.addresses = ["10.0.0.5"]
    workspace = december()
    with insights_settings(API_BLOCK_INTERNAL=True), pytest.raises(RecipeError, match="sales"):
        apply_recipe(workspace, recipe, guess_matches(recipe, workspace))
    assert not api.requests and not workspace.datasets
    with insights_settings(ALLOW_DB_CONNECT=False):
        matches = guess_matches(recipe, workspace)
        assert matches[ids(recipe)["orders"]] is None  # not offered
        forced = matches | {ids(recipe)["orders"]: FETCH}
        problems = match_problems(recipe, workspace, forced)
        assert "switched off" in problems[ids(recipe)["orders"]]
    with insights_settings(DB_ALLOWED_HOSTS=["erp.local"]), pytest.raises(RecipeError) as error:
        apply_recipe(workspace, recipe, guess_matches(recipe, workspace))
    assert "DB_ALLOWED_HOSTS" in str(error.value)


def test_child_lists_come_back_with_their_parent(workspace, api):
    api.reply = [{"Branch": "MUM", "items": [{"sku": "A", "qty": 2}, {"sku": "B", "qty": 1}]}]
    datasets = import_api(workspace, api)
    child = next(d for d in datasets if d.parent_id is not None)
    add_query(workspace, child, "Qty by sku", "sku", "qty")
    recipe = read_recipe(json.dumps(to_recipe(workspace, "Items")))
    entry = next(e for e in recipe["datasets"] if e["name"] == child.name)
    assert entry["connection"]["type"] == "api_child"
    fresh = december()
    matches = guess_matches(recipe, fresh)
    assert matches[entry["id"]] == FETCH
    apply_recipe(fresh, recipe, matches)
    [query] = fresh.queries.values()
    assert fresh.datasets[query.spec.dataset_id].child_path == "items"
    # The child alone can't be fetched: its parent must come too.
    alone = matches | {next(e["id"] for e in recipe["datasets"] if e["name"] == "sales"): None}
    assert "parent list" in match_problems(recipe, december(), alone)[entry["id"]]


# ---------- the match screen ----------


def test_the_match_screen_fetches_and_never_shows_a_secret(client, setup, api, caplog):
    recipe_file = json.dumps(to_recipe(setup, "Monthly", keep_secrets=False)).encode()
    response = client.post(
        reverse("my_insights:recipe_upload"),
        {"recipe": SimpleUploadedFile("m.json", recipe_file, "application/json")},
    )
    workspace = store.get(client.session[SESSION_KEY])
    recipe = workspace.pending_recipe
    page = client.get(response.url).content.decode()
    assert "↻ Fetch again from saved API" in page and "API: api.shop.in/orders" in page
    assert 'type="password"' in page and "disabled" in page
    sales, orders = ids(recipe)["sales"], ids(recipe)["orders"]
    picks = {f"match_{sales}": FETCH, f"match_{orders}": FETCH, "token": recipe["token"]}
    typed = {f"secret_{sales}_0": "Bearer xyz", f"secret_{sales}_1": "abc123"}
    with caplog.at_level(logging.DEBUG):
        page = client.post(
            reverse("my_insights:recipe_match"),
            picks | typed | {f"secret_{orders}_0": PASSWORD},
            headers={"HX-Request": "true"},
        ).content.decode()
        assert "typed ✓" in page and "↻ On Apply" in page
        for secret in ("xyz", "abc123", PASSWORD):
            assert secret not in page
        done = client.post(reverse("my_insights:recipe_match"), picks | {"action": "apply"})
    assert done.url == reverse("my_insights:home")
    assert "Fetched again: sales, orders." in client.get(done.url).content.decode()
    for secret in ("xyz", "abc123", PASSWORD):
        assert secret not in caplog.text


# ---------- secret parts ----------


def api_connection(url="https://api.shop.in/o", headers="", body="", method="GET"):
    source = ApiSource(url, method=method, headers=headers, body=body, name="x")
    _, workspace = store.create()
    [dataset] = load_parsed(workspace, "x", "x", parse_json(json.dumps(SALES).encode()))
    dataset.api_source = source
    return dataset, workspace


def test_the_address_is_kept_exactly_except_the_key():
    dataset, workspace = api_connection("https://api.shop.in/o?q=a%20b&flag&api_key=abc")
    saved = describe(workspace, dataset, keep_secrets=False)
    assert saved["url"] == "https://api.shop.in/o?q=a%20b&flag&api_key="
    typed = {group_of(saved): {"url:api_key": "x y/z"}}
    assert filled(saved, typed)["url"] == "https://api.shop.in/o?q=a%20b&flag&api_key=x%20y%2Fz"


def test_a_key_inside_a_nested_body_keeps_the_shape():
    body = json.dumps({"auth": {"user": "u", "pass": "p"}, "page": 1})
    dataset, workspace = api_connection(body=body, method="POST")
    saved = describe(workspace, dataset, keep_secrets=False)
    assert json.loads(saved["body"]) == {"auth": {"user": "u", "pass": ""}, "page": 1}
    assert [f["key"] for f in secret_fields(saved)] == ["body:auth.pass"]
    back = filled(saved, {group_of(saved): {"body:auth.pass": "p"}})
    assert json.loads(back["body"]) == json.loads(body)


def test_lists_from_one_api_ask_once_and_fetch_once(workspace, api):
    api.reply = {"orders": [{"Branch": "MUM", "Amount": 5}], "shops": [{"Code": "S1"}]}
    datasets = import_api(workspace, api)
    assert {d.name for d in datasets} == {"sales – orders", "sales – shops"}
    recipe = read_recipe(json.dumps(to_recipe(workspace, "Two", keep_secrets=False)))
    assert group(recipe, "sales – orders") == group(recipe, "sales – shops")
    api.requests.clear()
    fresh = december()
    typed = {
        group(recipe, "sales – orders"): {
            "header:Authorization": "Bearer xyz",
            "url:api_key": "abc123",
        }
    }
    matches = guess_matches(recipe, fresh)
    assert not match_problems(recipe, fresh, matches, typed)
    apply_recipe(fresh, recipe, matches, typed)
    assert len(api.requests) == 1 and len(fresh.datasets) == 2


def test_the_fetch_waits_without_holding_the_lock(setup, api):
    recipe = read_recipe(json.dumps(to_recipe(setup, "Monthly")))
    workspace = december()
    free = []
    opened = api.open

    def try_lock():  # another request of the same user, while the API is still answering
        taken = workspace.lock.acquire(timeout=1)
        free.append(taken)
        if taken:
            workspace.lock.release()

    def check_then_open(request, timeout):
        other = threading.Thread(target=try_lock)
        other.start()
        other.join()
        return opened(request, timeout)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(api_import, "_open", check_then_open)
        apply_recipe(workspace, recipe, guess_matches(recipe, workspace))
    assert free == [True]


def test_a_fetch_pick_is_ignored_when_fetching_is_switched_off(client, setup):
    recipe_file = json.dumps(to_recipe(setup, "Monthly")).encode()
    client.post(
        reverse("my_insights:recipe_upload"),
        {"recipe": SimpleUploadedFile("m.json", recipe_file, "application/json")},
    )
    recipe = store.get(client.session[SESSION_KEY]).pending_recipe
    sales = ids(recipe)["sales"]
    with insights_settings(ALLOW_API_IMPORT=False):
        page = client.post(
            reverse("my_insights:recipe_match"),
            {f"match_{sales}": FETCH, "token": recipe["token"]},
            headers={"HX-Request": "true"},
        ).content.decode()
    assert (
        "Fetch again from saved API" not in page and "Pick a dataset: the recipe needs it." in page
    )
