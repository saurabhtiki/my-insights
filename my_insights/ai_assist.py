"""AI assistant: a question in plain words → the query screen's boxes, filled in.

The AI sees only dataset names, column names and types (never a value or a row) and
answers with the boxes as JSON, never SQL. That JSON is checked here, field by field, and
turned into a QuerySpec; the user looks at the filled screen before anything is saved.

Any "OpenAI-style" service works: POST {address}/chat/completions, GET {address}/models
(OpenAI, Claude's compatible endpoint, Groq, OpenRouter, a local Ollama …). The address goes
through the same safety rules as Import from API. Python's own urllib: no extra package.

    "monthly sales by branch this FY as a line chart"
    → {"dataset": "sales", "group_by": [{"field": "Date", "date_part": "month"},
       {"field": "Branch"}], "values": [{"field": "Amount", "agg": "sum"}],
       "filters": [{"field": "Date", "op": "this_fy"}], "chart": "line"}
"""

import http.client
import json
import logging
import secrets
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlsplit

from . import api_import, charting, conf, querying
from .api_import import ApiFailed, safe_url
from .querying import AGGREGATIONS, DATE_PARTS, FILTER_OPS, MAX_FIELD_NAME_LENGTH
from .workspace import FilterItem, GroupItem, QuerySpec, SortItem, ValueItem

logger = logging.getLogger(__name__)

MAX_QUESTION = 1000
MAX_NOTES = 1000
MAX_HISTORY = 5  # earlier questions (and the AI's answers) sent with a follow-up
MAX_DRAFTS = 5  # AI drafts waiting to be opened on the query screen (browser tabs)
MAX_ITEMS = 30  # per box, as on the query screen
MAX_REPLY_BYTES = 256 * 1024
MAX_MODELS_BYTES = 4 * 1024 * 1024  # some services list hundreds of models
MAX_MODELS = 500
CHUNK = 64 * 1024
STATUS_HELP = {
    400: "it did not accept the request: check the model name",
    401: "the API key is wrong or has expired",
    403: "the key may not use this model",
    404: "not found: check the address and the model name",
    429: "too many requests, or the account is out of credit: wait a minute and try again",
}


class AiError(Exception):
    """Something went wrong. The message is shown to the user as it is."""


@dataclass
class AiSettings:
    address: str  # e.g. https://api.openai.com/v1
    key: str = ""  # kept in the in-memory workspace only; never logged or shown back
    model: str = ""

    @property
    def label(self) -> str:
        return f"{self.model or 'no model'} @ {urlsplit(self.address).hostname or self.address}"


@dataclass
class AiDraft:
    """An AI answer waiting to open on the query screen."""

    spec: QuerySpec
    chart_type: str  # "" = the screen's own pick
    question: str
    note: str  # the AI's one-line explanation


# ---------- talking to the service ----------


def clean_address(address: str) -> str:
    """The service's base address: spaces, a trailing / and a pasted /chat/completions go."""
    address = address.strip().rstrip("/")
    for tail in ("/chat/completions", "/models"):
        address = address.removesuffix(tail)
    return address


def check_address(address: str) -> None:
    """A usable web address that the safety rules allow. Raises AiError."""
    parts = urlsplit(address)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise AiError(
            "Type the service's full address, e.g. https://api.openai.com/v1 "
            "(or http://localhost:11434/v1 for Ollama)."
        )
    try:
        api_import.check_host(address)
    except ApiFailed as error:
        raise AiError(str(error).replace("API_ALLOWED_HOSTS", "API_ALLOWED_HOSTS (AI)")) from error


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """AI services answer directly. A redirect is refused: urllib would send the key
    (Authorization header) on to the new host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        logger.warning(
            "AI service redirect refused: %s → %s", safe_url(req.full_url), safe_url(newurl)
        )
        raise AiError(
            "The AI service sent us to another address. Type the final address instead "
            f"(it starts with {safe_url(newurl)[:80]})."
        )


def _open(request, timeout):
    """Send the request (replaced in tests)."""
    return urllib.request.build_opener(_NoRedirects).open(request, timeout=timeout)


def _call(settings: AiSettings, path: str, payload: dict | None, max_bytes: int) -> dict:
    """GET (payload None) or POST JSON to {address}{path}; the reply as JSON. Raises AiError."""
    check_address(settings.address)
    url = settings.address + path
    headers = {"Accept": "application/json", "User-Agent": "my-insights"}
    if settings.key:
        headers["Authorization"] = f"Bearer {settings.key}"
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=headers)
    timeout = conf.get("AI_TIMEOUT_SECONDS")
    try:
        with _open(request, timeout) as response:
            chunks, size = [], 0
            while chunk := response.read(CHUNK):
                size += len(chunk)
                if size > max_bytes:
                    raise AiError("The AI service sent a reply that is too large.")
                chunks.append(chunk)
    except urllib.error.HTTPError as error:
        reason = STATUS_HELP.get(error.code) or (
            "its server had a problem: try again later" if error.code >= 500 else error.reason
        )
        logger.info("AI service replied %s for %s", error.code, safe_url(url))
        raise AiError(f"The AI service replied {error.code} ({reason}).") from error
    except TimeoutError as error:
        logger.info("AI service timed out: %s", safe_url(url))
        raise AiError(f"The AI service took longer than {timeout} seconds to answer.") from error
    except urllib.error.URLError as error:
        if isinstance(error.reason, TimeoutError):
            raise AiError(
                f"The AI service took longer than {timeout} seconds to answer."
            ) from error
        logger.info("AI service not reachable: %s (%s)", safe_url(url), error.reason)
        raise AiError(f"Could not reach the AI service ({error.reason}).") from error
    except (OSError, http.client.HTTPException) as error:  # e.g. the connection dropped
        logger.info("AI service connection failed: %s (%r)", safe_url(url), error)
        raise AiError(
            "The AI service closed the connection without answering. Try again."
        ) from error
    try:
        return json.loads(b"".join(chunks))
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        logger.info("AI service sent something that is not JSON: %s", safe_url(url))
        raise AiError(
            "The AI service's reply was not understood. Check the address: it usually ends in /v1."
        ) from error


def list_models(settings: AiSettings) -> list[str]:
    """The model names the service offers, sorted. Raises AiError."""
    reply = _call(settings, "/models", None, MAX_MODELS_BYTES)
    items = reply.get("data") if isinstance(reply, dict) else None
    if not isinstance(items, list):
        raise AiError("The service did not list its models. Type the model name instead.")
    names = {
        item["id"] for item in items if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    return sorted(names, key=str.casefold)[:MAX_MODELS]


def ask(settings: AiSettings, messages: list[dict]) -> str:
    """The AI's answer text. Raises AiError."""
    if not settings.model:
        raise AiError("Pick or type a model first, e.g. gpt-4o-mini.")
    reply = _call(
        settings,
        "/chat/completions",
        {"model": settings.model, "messages": messages},
        MAX_REPLY_BYTES,
    )
    try:
        text = reply["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as error:
        logger.info("AI reply without choices[0].message.content: %s", str(reply)[:200])
        raise AiError("The AI service's answer was empty or not understood. Try again.") from error
    if not isinstance(text, str) or not text.strip():
        raise AiError("The AI gave an empty answer. Try again or use another model.")
    return text


# ---------- the question ----------

INSTRUCTIONS = """You turn a question about tabular data into the settings of a query builder.
Answer with ONE JSON object and nothing else (no SQL, no explanation outside the JSON).

Shape:
{
  "dataset": "<dataset name, exactly as listed>",
  "group_by": [{"field": "<field>", "date_part": "<date part, date fields only, else omit>"}],
  "across": null or {"field": "<field>", "date_part": "..."},
  "values": [{"field": "<field, or * to count rows>", "agg": "<aggregation>", "label": "<optional heading>"}],
  "filters": [{"field": "<field>", "op": "<row filter op>", "values": ["..."], "any_of": false}
              or {"value": <index into values, 0 = first>, "op": "<total filter op>", "values": ["..."]}],
  "sort": [{"by": "group" or "value", "index": <0-based index into group_by or values>, "descending": true}],
  "top_n": null or <number of rows to keep, used with sort>,
  "chart": "<chart type, or empty>",
  "note": "<one short sentence saying what you built>"
}
If the question cannot be answered with these datasets, answer {"error": "<short reason, naming what is missing>"}.

Rules:
- Use only datasets and fields listed below, spelled exactly. Never invent a field.
- "group_by" = the rows of the answer (e.g. per Branch, per Month of Date). "across" spreads one field into columns.
- "values" = numbers to work out. A field marked "total" is already a total: use agg "formula".
- Count of rows: {"field": "*", "agg": "rows"}.
- Aggregations: AGGS. sum / avg / median only on number fields.
- Date parts: PARTS. Financial year runs April to March (fy, fy_quarter).
- Row filter ops by field kind: ROW_OPS.
  Text "in" / "not_in" take the exact values in "values". Number ops take one number ("between" two).
  Date "between" takes two dates as YYYY-MM-DD; "last_days" takes one number of days; the others take no values.
- Total filter ops (on a value's total, e.g. sales over 1000): TOTAL_OPS.
- "Top 10 by sales" = sort that value descending + "top_n": 10.
- Chart types: CHARTS. Pick one only if asked for a chart or it clearly helps, else "".
- A follow-up question changes your previous answer: keep what the user did not ask to change.
"""


def _instructions() -> str:
    row_ops = "; ".join(
        f"{kind}: {', '.join(op for op, _ in ops)}"
        for kind, ops in FILTER_OPS.items()
        if kind != "total"
    )
    return (
        INSTRUCTIONS.replace("AGGS", ", ".join(AGGREGATIONS))
        .replace("PARTS", ", ".join(DATE_PARTS))
        .replace("ROW_OPS", row_ops)
        .replace("TOTAL_OPS", ", ".join(op for op, _ in FILTER_OPS["total"]))
        .replace("CHARTS", ", ".join(charting.CHART_TYPES))
    )


def describe_datasets(workspace) -> str:
    """Every dataset's name and fields with their kinds. No values, no rows."""
    lines = []
    for dataset in workspace.datasets.values():
        lines.append(f'Dataset "{dataset.name}":')
        for name, info in querying.field_catalog(dataset).items():
            if info.error:
                continue
            kind = info.kind_class
            if info.calculated:
                kind += ", calculated, total" if info.is_total else ", calculated"
            lines.append(f'  - "{name}" ({kind})')
    return "\n".join(lines)


def build_messages(workspace, question: str, dataset_hint: str = "") -> list[dict]:
    """The chat sent to the AI: instructions + datasets + notes, earlier turns, the question."""
    system = _instructions() + "\nDatasets:\n" + describe_datasets(workspace)
    notes = workspace.ai_notes.strip()
    if notes:
        system += "\n\nThe user's notes (their words for fields, and other hints):\n" + notes
    messages = [{"role": "system", "content": system}]
    for earlier_question, earlier_answer in workspace.ai_history:
        messages.append({"role": "user", "content": earlier_question})
        messages.append({"role": "assistant", "content": earlier_answer})
    text = question
    if dataset_hint:
        text += f'\n(The user suggests the dataset "{dataset_hint}".)'
    messages.append({"role": "user", "content": text})
    return messages


def remember(workspace, question: str, answer: dict) -> None:
    """Keep a question and the AI's (checked) answer for follow-ups: the last few only."""
    workspace.ai_history.append((question, json.dumps(answer, ensure_ascii=False)))
    del workspace.ai_history[:-MAX_HISTORY]


# ---------- the answer ----------


def parse_answer(text: str) -> dict:
    """The JSON object in the AI's answer (``` fences and words around it are allowed)."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        logger.info("AI answer without JSON: %s", text[:200])
        raise AiError("The AI did not answer with query settings. Try asking again in other words.")
    try:
        answer = json.loads(text[start : end + 1])
    except json.JSONDecodeError as error:
        logger.info("AI answer with broken JSON: %s", text[:200])
        raise AiError("The AI's answer was not complete. Try again.") from error
    if not isinstance(answer, dict):
        raise AiError("The AI did not answer with query settings. Try again.")
    if answer.get("error"):
        raise AiError(f"The AI could not build this: {str(answer['error'])[:300]}")
    return answer


def _key() -> str:
    return secrets.token_hex(3)


def _list(answer: dict, name: str) -> list:
    value = answer.get(name) or []
    if not isinstance(value, list):
        raise AiError(f"The AI's answer has a bad “{name}” part. Try again.")
    if len(value) > MAX_ITEMS:
        raise AiError(f"The AI put more than {MAX_ITEMS} items in “{name}”. Ask for less.")
    return [item for item in value if isinstance(item, dict)]


def _text(value) -> str:
    return value if isinstance(value, str) else ("" if value is None else str(value))


def _whole(value) -> bool:
    """A JSON whole number (true / false are not numbers here)."""
    return isinstance(value, int) and not isinstance(value, bool)


def _find_dataset(workspace, name: str):
    wanted = _text(name).strip().casefold()
    for dataset in workspace.datasets.values():
        if dataset.name.casefold() == wanted:
            return dataset
    names = ", ".join(f"“{d.name}”" for d in workspace.datasets.values())
    raise AiError(
        f"The AI picked a dataset “{_text(name)[:60]}” that isn't loaded (you have {names})."
    )


def _field(catalog: dict, name, dataset) -> str:
    """The field's exact name. Spelling case may differ; an unknown field is refused."""
    name = _text(name).strip()
    if name in catalog:
        return name
    for known in catalog:
        if known.casefold() == name.casefold():
            return known
    raise AiError(
        f"The AI used a field “{name[:MAX_FIELD_NAME_LENGTH]}” that “{dataset.name}” doesn't have. "
        "Use the field's own name, or add it to Notes for AI."
    )


def _group(item: dict, catalog: dict, dataset) -> GroupItem:
    column = _field(catalog, item.get("field"), dataset)
    if catalog[column].is_total:
        raise AiError(f"“{column}” is a total, so it can only go in Values.")
    part = _text(item.get("date_part"))
    if catalog[column].kind_class != "date" or part not in DATE_PARTS:
        part = ""
    return GroupItem(_key(), column, part)


def _value(item: dict, catalog: dict, dataset) -> ValueItem:
    label = " ".join(_text(item.get("label")).split())[:MAX_FIELD_NAME_LENGTH]
    agg = _text(item.get("agg")).strip().lower()
    if _text(item.get("field")).strip() == "*" or agg == "rows":
        return ValueItem(_key(), "*", "rows", label)
    column = _field(catalog, item.get("field"), dataset)
    info = catalog[column]
    if info.is_total:
        return ValueItem(_key(), column, "formula", label)
    allowed = [key for key, _, _ in querying.aggregations_for(info)]
    if agg not in allowed:
        raise AiError(
            f"The AI asked for “{agg[:20]}” of “{column}”, which doesn't work on "
            f"{querying.KIND_WORDS.get(info.kind_class, info.kind_class)} field. Try asking again."
        )
    return ValueItem(_key(), column, agg, label)


def _values_of(item: dict) -> list[str]:
    values = item.get("values")
    if values is None:
        return []
    if not isinstance(values, list):
        values = [values]
    return [_text(value) for value in values][: querying.MAX_TICK_VALUES]


def _filter(item: dict, catalog: dict, dataset, values: list[ValueItem]) -> FilterItem:
    op = _text(item.get("op")).strip().lower()
    if item.get("value") is not None and item.get("field") is None:  # on a value's total
        index = item.get("value")
        if not _whole(index) or not 0 <= index < len(values):
            raise AiError("The AI's answer filters a value that isn't in Values. Try again.")
        if op not in {key for key, _ in FILTER_OPS["total"]}:
            raise AiError(f"The AI used a filter “{op[:20]}” that totals don't have. Try again.")
        return FilterItem(_key(), value_key=values[index].key, op=op, values=_values_of(item))
    column = _field(catalog, item.get("field"), dataset)
    kind = catalog[column].kind_class
    if catalog[column].is_total or kind not in FILTER_OPS:
        raise AiError(f"“{column}” can't be used as a filter.")
    if op not in {key for key, _ in FILTER_OPS[kind]}:
        raise AiError(
            f"The AI used a filter “{op[:20]}” that doesn't work on “{column}”. Try again."
        )
    return FilterItem(
        _key(), column=column, op=op, values=_values_of(item), any_of=item.get("any_of") is True
    )


def _sort(item: dict, groups: list, values: list) -> SortItem | None:
    targets = groups if item.get("by") == "group" else values
    index = item.get("index", 0)
    if not _whole(index) or not 0 <= index < len(targets):
        return None  # a sort on something not there: left off, the screen still works
    return SortItem(_key(), targets[index].key, item.get("descending") is True)


def to_draft(workspace, answer: dict, question: str) -> AiDraft:
    """The AI's (parsed) answer → a QuerySpec for the screen. Raises AiError naming the problem."""
    dataset = _find_dataset(workspace, answer.get("dataset"))
    catalog = querying.field_catalog(dataset)
    groups = [_group(item, catalog, dataset) for item in _list(answer, "group_by")]
    across = None
    if isinstance(answer.get("across"), dict):
        across = _group(answer["across"], catalog, dataset)
    values = [_value(item, catalog, dataset) for item in _list(answer, "values")]
    filters = [_filter(item, catalog, dataset, values) for item in _list(answer, "filters")]
    sorts = [s for item in _list(answer, "sort") if (s := _sort(item, groups, values))]
    top_n = answer.get("top_n")
    top_n = top_n if _whole(top_n) and 0 < top_n <= querying.row_cap() else None
    if not groups and not values:
        raise AiError("The AI's answer has nothing to show (no Group by and no Values). Try again.")
    spec = QuerySpec(dataset.dataset_id, groups, values, filters, sorts, top_n, across)
    chart_type = _text(answer.get("chart"))
    chart_type = chart_type if chart_type in charting.CHART_TYPES else ""
    return AiDraft(spec, chart_type, question, " ".join(_text(answer.get("note")).split())[:300])


def keep_draft(workspace, draft: AiDraft) -> str:
    """Hold a draft for the query screen; returns its key for ?ai=<key>."""
    key = secrets.token_hex(8)
    workspace.ai_drafts[key] = draft
    while len(workspace.ai_drafts) > MAX_DRAFTS:
        workspace.ai_drafts.pop(next(iter(workspace.ai_drafts)))
    return key
