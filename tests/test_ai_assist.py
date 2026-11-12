"""AI assistant: what is sent, how answers become a query, errors, memory and the pages.
No real AI: the opener and the address lookup are replaced."""

import html
import http.client
import io
import json
import logging
import urllib.error
import urllib.request

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from my_insights import ai_assist, api_import
from my_insights.ai_assist import AiError, AiSettings
from my_insights.workspace import SESSION_KEY, store
from tests.conftest import csv_bytes, insights_settings

pytestmark = pytest.mark.django_db

KEY = "sk-secret-123"
PUBLIC_IP = "93.184.216.34"
AI_PAGE = reverse("my_insights:ai")
SETTINGS = reverse("my_insights:ai_settings")
DRAFT = reverse("my_insights:ai_draft")
MODELS = reverse("my_insights:ai_models")
HTMX = {"HX-Request": "true"}
SALES = csv_bytes(
    "Date,Branch,Amount,Item no",
    "2026-04-05,Pune,1200,X-77",
    "2026-05-06,Delhi,800,X-78",
    "2026-05-09,Pune,500,X-77",
)
ANSWER = {
    "dataset": "sales",
    "group_by": [{"field": "Date", "date_part": "month"}, {"field": "branch"}],
    "values": [{"field": "Amount", "agg": "sum", "label": "Sales"}],
    "filters": [{"field": "Date", "op": "this_fy"}],
    "sort": [{"by": "value", "index": 0, "descending": True}],
    "top_n": 10,
    "chart": "line",
    "note": "Sales per month and branch.",
}


def chat_reply(answer) -> dict:
    text = answer if isinstance(answer, str) else json.dumps(answer)
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


class FakeAi:
    """Stands in for the AI service: records requests, gives back `reply` (or raises it)."""

    def __init__(self, monkeypatch, reply=None):
        self.reply = chat_reply(ANSWER) if reply is None else reply
        self.requests = []
        monkeypatch.setattr(ai_assist, "_open", self.open)
        monkeypatch.setattr(api_import, "_resolve", lambda host: [PUBLIC_IP])

    def open(self, request, timeout):
        self.requests.append(request)
        if isinstance(self.reply, Exception):
            raise self.reply
        body = self.reply if isinstance(self.reply, bytes) else json.dumps(self.reply).encode()
        return io.BytesIO(body)

    def sent(self, at=-1) -> dict:
        return json.loads(self.requests[at].data)


@pytest.fixture
def fake(monkeypatch):
    return FakeAi(monkeypatch)


@pytest.fixture
def ready(client):
    """A client with sales.csv loaded and the AI service saved."""
    client.post(
        reverse("my_insights:upload"),
        {"files": [SimpleUploadedFile("sales.csv", SALES, "text/csv")]},
        headers=HTMX,
    )
    response = client.post(
        SETTINGS,
        {"address": "https://api.example.com/v1/", "key": KEY, "model": "gpt-test"},
        headers=HTMX,
    )
    assert "Connected" in response.content.decode()
    return client


def workspace_of(client):
    return store.get(client.session.get(SESSION_KEY))


def ask(client, question="monthly sales by branch", **extra):
    return client.post(DRAFT, {"question": question, **extra}, headers=HTMX)


# ---------- what is sent ----------


def test_question_sends_names_and_types_but_no_values(ready, fake):
    response = ask(ready, dataset=str(next(iter(workspace_of(ready).datasets))))
    assert response.status_code == 204
    [request] = fake.requests
    assert request.full_url == "https://api.example.com/v1/chat/completions"
    assert request.get_header("Authorization") == f"Bearer {KEY}"
    sent = fake.sent()
    assert sent["model"] == "gpt-test"
    text = json.dumps(sent)
    assert '\\"Amount\\" (number)' in text and '\\"Date\\" (date)' in text
    for value in ("Pune", "Delhi", "1200", "X-77", "2026-04-05"):
        assert value not in text  # no data leaves the server
    assert 'suggests the dataset \\"sales\\"' in text


def test_notes_are_kept_and_sent_with_every_question(ready, fake):
    ask(ready, notes='Item code = "Item no" column')
    ask(ready, "again")
    assert workspace_of(ready).ai_notes == 'Item code = "Item no" column'
    assert 'Item code = \\"Item no\\" column' in json.dumps(fake.sent())


# ---------- answer → query screen ----------


def test_good_answer_opens_the_query_screen_filled_in(ready, fake):
    response = ask(ready)
    url = response.headers["HX-Redirect"]
    assert "ai=" in url and "tab=chart" in url
    [draft] = workspace_of(ready).ai_drafts.values()
    spec = draft.spec
    assert [(g.column, g.date_part) for g in spec.groups] == [("Date", "month"), ("Branch", "")]
    assert [(v.column, v.agg, v.label) for v in spec.values] == [("Amount", "sum", "Sales")]
    assert [(f.column, f.op) for f in spec.filters] == [("Date", "this_fy")]
    assert spec.sorts[0].target == spec.values[0].key and spec.sorts[0].descending
    assert spec.top_n == 10 and draft.chart_type == "line"

    page = ready.get(url).content.decode()
    assert "Drafted by AI" in page and "monthly sales by branch" in page
    assert 'value="line"' in page and "Sales per month and branch." in page


def test_answer_in_code_fences_with_words_is_read():
    text = "Sure!\n```json\n" + json.dumps({"dataset": "x"}) + "\n```"
    assert ai_assist.parse_answer(text) == {"dataset": "x"}


def test_count_of_rows_and_total_filter(ready, fake):
    fake.reply = chat_reply(
        {
            "dataset": "SALES",
            "group_by": [{"field": "Branch"}],
            "values": [{"field": "*", "agg": "rows"}],
            "filters": [{"value": 0, "op": "gt", "values": [1]}],
        }
    )
    assert ask(ready).status_code == 204
    [draft] = workspace_of(ready).ai_drafts.values()
    assert draft.spec.values[0].agg == "rows"
    assert draft.spec.filters[0].value_key == draft.spec.values[0].key
    assert draft.spec.filters[0].values == ["1"]


@pytest.mark.parametrize(
    ("answer", "message"),
    [
        ("I can't help", "did not answer with query settings"),
        ('{"dataset": "sales", ', "did not answer with query settings"),
        ({**ANSWER, "dataset": "stock"}, "isn't loaded"),
        ({**ANSWER, "values": [{"field": "Revenue", "agg": "sum"}]}, "“Revenue”"),
        ({**ANSWER, "values": [{"field": "Branch", "agg": "sum"}]}, "doesn't work on"),
        ({**ANSWER, "filters": [{"field": "Branch", "op": "this_fy"}]}, "doesn't work on"),
        ({"dataset": "sales"}, "nothing to show"),
        ({"error": "No profit column"}, "could not build this: No profit column"),
    ],
)
def test_bad_answers_give_a_clear_message_and_save_nothing(ready, fake, answer, message):
    fake.reply = chat_reply(answer)
    response = ask(ready, "my question")
    page = html.unescape(response.content.decode())
    assert response.status_code == 200 and message in page
    assert "my question" in page  # the question stays in the box
    workspace = workspace_of(ready)
    assert not workspace.ai_drafts and not workspace.ai_history and not workspace.queries


# ---------- service errors ----------


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (urllib.error.HTTPError("u", 401, "x", {}, None), "API key is wrong"),
        (urllib.error.HTTPError("u", 404, "x", {}, None), "check the address and the model"),
        (urllib.error.HTTPError("u", 503, "x", {}, None), "try again later"),
        (TimeoutError(), "longer than 60 seconds"),
        (urllib.error.URLError("refused"), "Could not reach"),
        (http.client.RemoteDisconnected("gone"), "closed the connection"),
        (ConnectionResetError(), "closed the connection"),
    ],
)
def test_service_errors_are_plain_messages(ready, fake, error, message):
    fake.reply = error
    assert message in ask(ready).content.decode()


def test_huge_or_strange_replies_are_refused(fake):
    settings = AiSettings("https://api.example.com/v1", KEY, "m")
    fake.reply = b"x" * (ai_assist.MAX_REPLY_BYTES + 10)
    with pytest.raises(AiError, match="too large"):
        ai_assist.ask(settings, [])
    fake.reply = b"<html>login</html>"
    with pytest.raises(AiError, match="not understood"):
        ai_assist.ask(settings, [])
    fake.reply = {"choices": [{"message": {"content": None}}]}
    with pytest.raises(AiError, match="empty"):
        ai_assist.ask(settings, [])


# ---------- the key ----------


def test_key_is_never_shown_or_logged(ready, fake, caplog):
    caplog.set_level(logging.DEBUG)
    fake.reply = urllib.error.HTTPError("u", 401, "x", {}, None)
    ask(ready)
    page = ready.get(AI_PAGE).content.decode()
    assert "Connected" in page and "saved" in page
    assert KEY not in page and KEY not in caplog.text
    assert KEY not in json.dumps(ready.post(reverse("my_insights:recipe_save")).content.decode())


def test_empty_key_box_keeps_the_saved_key_and_forget_clears_it(ready, fake):
    ready.post(SETTINGS, {"address": "https://api.example.com/v1", "model": "other"})
    settings = workspace_of(ready).ai_settings
    assert settings.key == KEY and settings.model == "other"
    ready.post(reverse("my_insights:ai_forget"), headers=HTMX)
    assert workspace_of(ready).ai_settings is None
    assert "address, key and model first" in ask(ready).content.decode()


def test_clearing_my_data_clears_the_key(ready):
    ready.post(reverse("my_insights:clear"))
    assert workspace_of(ready) is None


# ---------- safety ----------


def test_internal_address_refused_when_the_rule_is_on(ready, monkeypatch):
    monkeypatch.setattr(api_import, "_resolve", lambda host: ["127.0.0.1"])
    with insights_settings(API_BLOCK_INTERNAL=True):
        page = ready.post(
            SETTINGS, {"address": "http://localhost:11434/v1", "model": "llama"}, headers=HTMX
        ).content.decode()
    assert "inside the network" in page


def test_local_ollama_allowed_by_default(ready, fake):
    page = ready.post(
        SETTINGS, {"address": "http://localhost:11434/v1", "model": "llama3"}, headers=HTMX
    ).content.decode()
    assert "Connected" in page


def test_switched_off_hides_the_button_and_the_page(ready):
    with insights_settings(ALLOW_AI=False):
        assert ready.get(AI_PAGE).status_code == 404
        assert "Ask AI" not in ready.get(reverse("my_insights:home")).content.decode()
    assert "Ask AI" in ready.get(reverse("my_insights:home")).content.decode()


def test_no_data_sends_the_user_home(client):
    assert client.get(AI_PAGE).status_code == 302


# ---------- models ----------


def test_load_models_fills_the_list(ready, fake):
    fake.reply = {"data": [{"id": "b-model"}, {"id": "a-model"}, {"x": 1}]}
    page = ready.post(MODELS, {"address": "https://api.example.com/v1"}, headers=HTMX)
    page = page.content.decode()
    assert page.index("a-model") < page.index("b-model") and "2 models found" in page
    assert fake.requests[-1].full_url == "https://api.example.com/v1/models"
    assert fake.requests[-1].get_header("Authorization") == f"Bearer {KEY}"  # the saved key


def test_load_models_failure_still_lets_you_type(ready, fake):
    fake.reply = urllib.error.HTTPError("u", 404, "x", {}, None)
    page = ready.post(MODELS, {"address": "https://api.example.com/v1"}).content.decode()
    assert "You can still type the name" in page


# ---------- memory ----------


def test_follow_up_sends_earlier_turns_and_only_five_are_kept(ready, fake):
    ask(ready, "sales by branch")
    ask(ready, "show avg also", **{"from": "query"})
    messages = fake.sent()["messages"]
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]
    assert messages[1]["content"] == "sales by branch"
    assert json.loads(messages[2]["content"])["dataset"] == "sales"
    for number in range(6):
        ask(ready, f"question {number}")
    history = workspace_of(ready).ai_history
    assert len(history) == 5 and history[-1][0] == "question 5"
    assert all("Pune" not in answer for _, answer in history)


def test_new_topic_forgets_earlier_questions(ready, fake):
    ask(ready)
    page = ready.post(reverse("my_insights:ai_new_topic"), headers=HTMX).content.decode()
    assert "New topic" in page and not workspace_of(ready).ai_history
    ask(ready, "fresh")
    assert len(fake.sent()["messages"]) == 2


def test_failed_follow_up_shows_in_the_follow_up_box(ready, fake):
    fake.reply = chat_reply({"error": "No such thing"})
    page = ask(ready, "only Mumbai", **{"from": "query"}).content.decode()
    assert 'id="ai-followup"' in page and "No such thing" in page and "only Mumbai" in page


# ---------- review fixes ----------


def test_redirect_is_refused_so_the_key_never_moves_host():
    request = urllib.request.Request("https://api.example.com/v1/models")
    request.add_header("Authorization", f"Bearer {KEY}")
    with pytest.raises(AiError, match="another address"):
        ai_assist._NoRedirects().redirect_request(
            request, None, 302, "Found", {}, "https://elsewhere.example/v1/models"
        )


def test_saved_key_is_not_sent_to_a_new_address(ready, fake):
    fake.reply = {"data": []}
    ready.post(MODELS, {"address": "https://other.example/v1"}, headers=HTMX)
    assert fake.requests[-1].get_header("Authorization") is None
    ready.post(SETTINGS, {"address": "https://other.example/v1", "model": "m"}, headers=HTMX)
    assert workspace_of(ready).ai_settings.key == ""


def test_new_topic_keeps_notes_typed_but_not_sent(ready):
    ready.post(reverse("my_insights:ai_new_topic"), {"notes": "Code = Item no"}, headers=HTMX)
    assert workspace_of(ready).ai_notes == "Code = Item no"


def test_true_is_not_a_number_and_a_missing_field_is_refused(ready, fake):
    fake.reply = chat_reply({**ANSWER, "top_n": True})
    ask(ready)
    [draft] = workspace_of(ready).ai_drafts.values()
    assert draft.spec.top_n is None
    fake.reply = chat_reply({**ANSWER, "values": [{"agg": "sum"}]})
    assert "doesn" in html.unescape(ask(ready).content.decode())  # “” isn't a field
