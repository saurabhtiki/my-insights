"""Import from API: requests, safety checks, error messages, the views and Refresh.
No real network: the opener and the address lookup are replaced."""

import http.client
import io
import json
import urllib.error

import pytest
from django.urls import reverse

from my_insights import api_import
from my_insights.api_import import ApiFailed, fetch_json
from my_insights.linking import save_link
from my_insights.workspace import SESSION_KEY, ApiSource, LinkSpec, LinkStep, store
from tests.conftest import insights_settings

pytestmark = pytest.mark.django_db

PUBLIC_IP = "93.184.216.34"


class FakeApi:
    """Stands in for the network: records the requests and gives back `reply`."""

    def __init__(self, monkeypatch, reply=b"[]", addresses=(PUBLIC_IP,)):
        self.reply = reply
        self.requests = []
        self.addresses = list(addresses)
        monkeypatch.setattr(api_import, "_open", self.open)
        monkeypatch.setattr(api_import, "_resolve", lambda host: self.addresses)

    def open(self, request, timeout):
        self.requests.append(request)
        if isinstance(self.reply, Exception):
            raise self.reply
        body = self.reply if isinstance(self.reply, bytes) else json.dumps(self.reply).encode()
        return io.BytesIO(body)


def http_error(code):
    return urllib.error.HTTPError("https://api.example.com", code, "x", {}, None)


# ---------- fetching ----------


def test_get_sends_headers(monkeypatch):
    api = FakeApi(monkeypatch, reply=[{"a": 1}])
    data = fetch_json(ApiSource("https://api.example.com/v1/orders", headers="X-Api-Key: abc\n\n"))
    assert json.loads(data) == [{"a": 1}]
    [request] = api.requests
    assert request.get_method() == "GET" and request.data is None
    assert request.get_header("X-api-key") == "abc"


def test_post_sends_the_json_body(monkeypatch):
    api = FakeApi(monkeypatch, reply={"rows": []})
    fetch_json(ApiSource("https://api.example.com/q", method="POST", body='{"from": "2026-04-01"}'))
    [request] = api.requests
    assert request.get_method() == "POST"
    assert json.loads(request.data) == {"from": "2026-04-01"}
    assert request.get_header("Content-type") == "application/json"


@pytest.mark.parametrize(
    ("source", "message"),
    [
        (ApiSource("ftp://example.com/x"), "full web address"),
        (ApiSource("https://api.example.com", headers="no colon here"), "Headers, line 1"),
        (
            ApiSource("https://api.example.com", method="POST", body="{bad"),
            "Body is not valid JSON",
        ),
    ],
)
def test_bad_form_values(monkeypatch, source, message):
    FakeApi(monkeypatch)
    with pytest.raises(ApiFailed, match=message):
        fetch_json(source)


@pytest.mark.parametrize(
    "address", ["127.0.0.1", "10.1.2.3", "192.168.1.5", "169.254.169.254", "::1"]
)
def test_internal_addresses_are_refused(monkeypatch, address):
    api = FakeApi(monkeypatch, addresses=[address])
    with (
        insights_settings(API_BLOCK_INTERNAL=True),
        pytest.raises(ApiFailed, match="inside the network"),
    ):
        fetch_json(ApiSource("http://intranet.example.com/data"))
    assert api.requests == []


def test_allowed_hosts_may_be_internal(monkeypatch):
    api = FakeApi(monkeypatch, reply=[{"a": 1}], addresses=["10.0.0.12"])
    with insights_settings(API_BLOCK_INTERNAL=True, API_ALLOWED_HOSTS=["erp.local"]):
        fetch_json(ApiSource("http://erp.local/api"))
    assert len(api.requests) == 1


def test_a_redirect_inside_the_network_is_refused(monkeypatch):
    monkeypatch.setattr(api_import, "_resolve", lambda host: ["10.0.0.1"])
    handler = api_import._CheckedRedirects()
    with (
        insights_settings(API_BLOCK_INTERNAL=True),
        pytest.raises(ApiFailed, match="inside the network"),
    ):
        handler.redirect_request(None, None, 302, "Found", {}, "http://internal.example.com/")


@pytest.mark.parametrize(
    ("reply", "message"),
    [
        (http_error(401), r"401 \(not authorised: check the key in Headers\)"),
        (http_error(404), "check the URL"),
        (http_error(503), "server had a problem"),
        (TimeoutError(), "longer than 30 seconds"),
        (urllib.error.URLError("refused"), "Could not reach the API"),
        (http.client.RemoteDisconnected("gone"), "closed the connection"),
        (ConnectionResetError(), "closed the connection"),
        (b"<!DOCTYPE html><html>", "not JSON"),
        (b"   ", "empty reply"),
    ],
)
def test_error_messages(monkeypatch, reply, message):
    FakeApi(monkeypatch, reply=reply)
    with pytest.raises(ApiFailed, match=message):
        fetch_json(ApiSource("https://api.example.com/x"))


def test_too_large_a_reply(monkeypatch):
    FakeApi(monkeypatch, reply=b"[" + b" " * (2 * 1024 * 1024) + b"]")
    with insights_settings(MAX_FILE_MB=1), pytest.raises(ApiFailed, match="larger than the 1 MB"):
        fetch_json(ApiSource("https://api.example.com/x"))


def test_internal_addresses_allowed_by_default(monkeypatch):
    api = FakeApi(monkeypatch, reply=[{"a": 1}], addresses=["192.168.1.5"])
    fetch_json(ApiSource("http://erp.local/api"))
    assert len(api.requests) == 1


def test_switched_off(monkeypatch):
    FakeApi(monkeypatch)
    with insights_settings(ALLOW_API_IMPORT=False), pytest.raises(ApiFailed, match="switched off"):
        fetch_json(ApiSource("https://api.example.com/x"))


# ---------- the views ----------


def orders(*items_per_order, extra=None):
    rows = []
    for number, items in enumerate(items_per_order, start=1):
        row = {"id": number, "amount": 100 * number, "items": [{"sku": s} for s in items]}
        rows.append({**row, **(extra or {})})
    return {"data": {"orders": rows}}


def workspace_of(client):
    return store.get(client.session.get(SESSION_KEY))


def post(client, name, data=None, **kwargs):
    return client.post(
        reverse(f"my_insights:{name}", kwargs=kwargs), data or {}, headers={"HX-Request": "true"}
    )


FORM = {
    "url": "https://api.example.com/v1/orders?key=secret",
    "method": "GET",
    "headers": "X-Api-Key: abc",
}


def test_test_button_shows_rows_and_columns(client, monkeypatch):
    FakeApi(monkeypatch, reply=orders(["A1", "B2"]))
    response = post(client, "api_test", FORM)
    text = response.content.decode()
    assert "data.orders</code>: 1 row\n" in text
    assert "amount" in text and "items</b> (2 rows)" in text
    assert workspace_of(client) is None  # nothing loaded


def test_test_button_shows_the_error(client, monkeypatch):
    FakeApi(monkeypatch, reply=http_error(401))
    assert "not authorised" in post(client, "api_test", FORM).content.decode()


def test_import_loads_datasets_and_keeps_the_source(client, monkeypatch):
    FakeApi(monkeypatch, reply=orders(["A1", "B2"], ["C3"]))
    text = post(client, "api_import", FORM).content.decode()
    assert (
        "Loaded as orders (2 rows), orders – items (3 rows). Rows taken from data.orders." in text
    )
    assert "secret" not in text  # the ?query part (it may hold a key) is never shown
    workspace = workspace_of(client)
    main, items = workspace.datasets.values()
    assert main.api_source.headers == "X-Api-Key: abc" and items.parent_id == main.dataset_id
    assert "↻ Refresh" in text


def test_import_failure_shows_a_message(client, monkeypatch):
    FakeApi(monkeypatch, addresses=["127.0.0.1"])
    with insights_settings(API_BLOCK_INTERNAL=True):
        text = post(client, "api_import", FORM).content.decode()
    assert "inside the network" in text
    assert workspace_of(client).datasets == {}


def test_refresh_keeps_ids_and_what_is_built_on_it(client, monkeypatch):
    api = FakeApi(monkeypatch, reply=orders(["A1", "B2"], ["C3"]))
    post(client, "api_import", FORM)
    workspace = workspace_of(client)
    main, items = workspace.datasets.values()
    combined = save_link(
        workspace,
        LinkSpec(
            main_id=items.dataset_id,
            steps=[LinkStep(main.dataset_id, pairs=[("_parent_row", "_row")])],
        ),
        "Items with orders",
    )

    api.reply = orders(["A1"], ["C3"], ["D4", "E5"], extra={"region": "West"})
    text = post(client, "dataset_refresh", dataset_id=main.dataset_id).content.decode()
    assert "Refreshed" in text and "orders (3 rows)." in text
    assert set(workspace.datasets) == {main.dataset_id, items.dataset_id, combined.dataset_id}
    assert main.row_count == 3 and items.row_count == 4
    assert "region" in [c.name for c in main.columns]
    count = workspace.connection.execute(f"SELECT count(*) FROM {combined.table_name}").fetchall()
    assert count == [(4,)] and combined.row_count == 4


def test_refresh_notes_gone_columns_and_lists(client, monkeypatch):
    api = FakeApi(monkeypatch, reply=orders(["A1"]))
    post(client, "api_import", FORM)
    workspace = workspace_of(client)
    main, items = workspace.datasets.values()

    api.reply = {"data": {"orders": [{"id": 1, "payments": [{"mode": "UPI"}]}]}}
    text = post(client, "dataset_refresh", dataset_id=main.dataset_id).content.decode()
    assert "no longer has: amount" in text
    assert "Not in the latest API reply" in text and items.notice
    assert "New list “payments” added as “orders – payments”" in text
    assert items.row_count == 1  # old data kept
    assert len(workspace.datasets) == 3


def test_failed_refresh_keeps_the_old_data(client, monkeypatch):
    api = FakeApi(monkeypatch, reply=orders(["A1"]))
    post(client, "api_import", FORM)
    workspace = workspace_of(client)
    main, _ = workspace.datasets.values()
    api.reply = http_error(500)
    text = post(client, "dataset_refresh", dataset_id=main.dataset_id).content.decode()
    assert "Not refreshed" in text and "The earlier data is kept" in text
    assert main.row_count == 1 and len(workspace.datasets) == 2


def test_home_shows_api_tab_unless_switched_off(client):
    assert "From API" in client.get(reverse("my_insights:home")).content.decode()
    with insights_settings(ALLOW_API_IMPORT=False):
        assert "From API" not in client.get(reverse("my_insights:home")).content.decode()


def test_several_lists_import_and_refresh_together(client, monkeypatch):
    shops = [{"id": 9, "city": "Pune"}]
    reply = orders(["A1"])
    reply["data"]["shops"] = shops
    api = FakeApi(monkeypatch, reply=reply)
    text = post(client, "api_import", {**FORM, "name": "sales"}).content.decode()
    assert "sales – orders (1 row), sales – orders – items (1 row), sales – shops (1 row)" in text
    workspace = workspace_of(client)
    main, _items, shop_list = workspace.datasets.values()
    assert main.api_source is shop_list.api_source

    # Refresh from either list updates both; a gone list keeps its data, a new one is added
    api.reply = {"data": {"shops": shops * 2, "staff": [{"name": "Ravi"}]}}
    text = post(client, "dataset_refresh", dataset_id=shop_list.dataset_id).content.decode()
    assert shop_list.row_count == 2
    assert "“sales – orders”: Not in the latest API reply" in text and main.row_count == 1
    assert "New list “data.staff” added as “sales – staff”" in text
    assert len(workspace.datasets) == 4
