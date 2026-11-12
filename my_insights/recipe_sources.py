"""Recipes and API / database datasets: what a recipe remembers about where a dataset came
from, and fetching it again when the recipe is applied.

    Nov: "sales" from https://api.shop.in/orders → Save recipe (keeps the URL and headers)
    Dec: Load recipe → "sales: ↻ Fetch again" → Apply fetches it, then builds everything

RECIPE_KEEP_SECRETS (on by default) keeps the secret parts (key-like headers, address and
body parts, the database password) as they are, in plain text. Off: they are saved empty
and asked on load. Datasets from one API or server share what is typed: one key per API.
Every fetch goes through the same checks as a manual import (allowed hosts, read-only SQL).
"""

import json
import logging
import re
from urllib.parse import quote, unquote_plus, urlsplit, urlunsplit

from . import conf, db_connect
from .api_import import ApiFailed, fetch_json, parse_headers, safe_url
from .db_loading import read_from_server
from .json_loading import load_parsed, parse_json
from .loading import UploadRejected
from .workspace import ApiSource, Dataset, Workspace

logger = logging.getLogger(__name__)

FETCH = "fetch"  # the match screen's "↻ Fetch again" choice
# Names of parts that usually hold a secret: Authorization, X-Api-Key, ?api_key=, {"token": …}
KEY_LIKE = re.compile(r"key|token|secret|pass|sig|auth|access|credential", re.IGNORECASE)
FIELDS = {
    "api": {
        "url": str,
        "method": str,
        "headers": str,
        "body": str,
        "rows_at": str,
        "name": str,
        "list_path": str,
    },
    "api_child": {"parent": int, "path": str},
    "database": {
        "kind": str,
        "host": str,
        "port": int,
        "database": str,
        "user": str,
        "password": str,
        "require_ssl": bool,
        "select": str,
        "table": str,  # the table read (for foreign keys); "" for an own query
    },
}
OPTIONAL = {"table": ""}  # fields older recipes may not have


class FetchFailed(Exception):
    """Fetching again did not work. The message names the dataset and is shown as it is."""


# ---------- the secret parts of a connection ----------


def _headers(text: str) -> dict[str, str]:
    try:
        return parse_headers(text)
    except ApiFailed as error:  # a damaged recipe: the fetch itself will say what is wrong
        logger.info("Recipe headers could not be read: %s", error)
        return {}


def _query_parts(url: str) -> list[tuple[str, str, str]]:
    """(raw piece, name, value) per address part, e.g. ("api_key=abc", "api_key", "abc")."""
    query = urlsplit(url).query
    parts = []
    for piece in query.split("&") if query else []:
        name, _, value = piece.partition("=")
        parts.append((piece, unquote_plus(name), unquote_plus(value)))
    return parts


def _body_object(body: str) -> dict:
    try:
        value = json.loads(body) if body.strip() else {}
    except (ValueError, RecursionError):
        return {}
    return value if isinstance(value, dict) else {}


def _body_leaves(value: dict, path: tuple = ()):
    """(path, text) for every text value, also inside nested objects: (("auth", "pass"), "b")"""
    for name, item in value.items():
        if isinstance(item, dict):
            yield from _body_leaves(item, (*path, name))
        elif isinstance(item, str):
            yield (*path, name), item


def _secret_parts(connection: dict) -> list[tuple[str, str, str]]:
    """(key, label, value) for each secret part, e.g. ("url:api_key", "“api_key” in the
    address", "abc123"). Only key-like names: "Accept" or "?page=1" are not secrets."""
    if connection["type"] == "database":
        return [("password", "Password", connection["password"])]
    if connection["type"] != "api":
        return []
    parts = [
        (f"header:{name}", f"Header {name}", value)
        for name, value in _headers(connection["headers"]).items()
        if KEY_LIKE.search(name)
    ]
    parts += [
        (f"url:{name}", f"“{name}” in the address", value)
        for _, name, value in _query_parts(connection["url"])
        if KEY_LIKE.search(name)
    ]
    parts += [
        (f"body:{'.'.join(path)}", f"“{'.'.join(path)}” in the body", value)
        for path, value in _body_leaves(_body_object(connection["body"]))
        if KEY_LIKE.search(path[-1])
    ]
    return parts


def _replaced(connection: dict, values: dict[str, str]) -> dict:
    """The connection with these secret parts set; the rest stays exactly as it was."""
    if not values:
        return connection
    if connection["type"] == "database":
        return connection | {"password": values.get("password", connection["password"])}
    changed = dict(connection)
    headers = _headers(connection["headers"])
    if any(f"header:{name}" in values for name in headers):
        changed["headers"] = "\n".join(
            f"{name}: {values.get(f'header:{name}', value)}".rstrip()
            for name, value in headers.items()
        )
    parts = _query_parts(connection["url"])
    if any(f"url:{name}" in values for _, name, _ in parts):
        pieces = [
            f"{piece.partition('=')[0]}={quote(values[f'url:{name}'], safe='')}"
            if f"url:{name}" in values
            else piece
            for piece, name, _ in parts
        ]
        address = urlsplit(connection["url"])
        changed["url"] = urlunsplit(address._replace(query="&".join(pieces)))
    body = _body_object(connection["body"])
    wanted = {path: f"body:{'.'.join(path)}" for path, _ in _body_leaves(body)}
    if any(key in values for key in wanted.values()):
        for path, key in wanted.items():
            if key in values:
                holder = body
                for name in path[:-1]:
                    holder = holder[name]
                holder[path[-1]] = values[key]
        changed["body"] = json.dumps(body, ensure_ascii=False)
    return changed


def group_of(connection: dict) -> str:
    """Datasets from the same API call or the same server share one group, so a key or
    password is typed once for all of them. "" for a JSON child list (it comes with its parent)."""
    if connection["type"] == "api_child":
        return ""
    same = {k: v for k, v in connection.items() if k not in ("list_path", "select", "table")}
    return json.dumps(same, sort_keys=True)


# ---------- saving ----------


def describe(workspace: Workspace, dataset: Dataset, keep_secrets: bool) -> dict | None:
    """Where the dataset came from, for the recipe. None for a file."""
    connection = None
    source = dataset.api_source
    parent = workspace.datasets.get(dataset.parent_id or 0)
    if source is not None:
        connection = {
            "type": "api",
            "url": source.url,
            "method": source.method,
            "headers": source.headers,
            "body": source.body,
            "rows_at": source.rows_at,
            "name": source.name or dataset.name,
            "list_path": dataset.list_path,
        }
    elif parent is not None and parent.api_source is not None:
        return {"type": "api_child", "parent": parent.dataset_id, "path": dataset.child_path}
    elif dataset.db_source is not None:
        server = dataset.db_source.server
        connection = {
            "type": "database",
            "kind": server.kind,
            "host": server.host,
            "port": server.port,
            "database": server.database,
            "user": server.user,
            "password": server.password,
            "require_ssl": server.require_ssl,
            "select": dataset.db_source.select,
            "table": dataset.db_source.table,
        }
    if connection is None or keep_secrets:
        return connection
    return _replaced(connection, {key: "" for key, _, _ in _secret_parts(connection)})


# ---------- reading ----------


def checked(value, where: str) -> dict:
    """A recipe's connection, checked: right type and fields. Raises ValueError."""
    if not isinstance(value, dict) or value.get("type") not in FIELDS:
        raise ValueError(f"{where}.connection must be an API or database source.")
    connection = {"type": value["type"]}
    for name, kind in FIELDS[value["type"]].items():
        item = value.get(name, OPTIONAL.get(name))
        if not isinstance(item, kind) or (kind is int and isinstance(item, bool)):
            raise ValueError(f"{where}.connection.{name} is missing or of the wrong type.")
        connection[name] = item
    if connection["type"] == "database" and connection["kind"] not in db_connect.DIALECTS:
        raise ValueError(f"{where}.connection.kind must be postgres or mysql.")
    return connection


# ---------- the match screen ----------


def secret_fields(connection: dict) -> list[dict]:
    """The secrets the user may type, e.g. [{"key": "header:Authorization", "label":
    "Header Authorization", "saved": True}]. Saved = the recipe already has a value."""
    return [
        {"key": key, "label": label, "saved": bool(value)}
        for key, label, value in _secret_parts(connection)
    ]


def where_from(connection: dict) -> str:
    """For the screen, never with a key: "API: api.shop.in/orders", "erp on 192.168.1.20" """
    if connection["type"] == "database":
        return f"database: {connection['database']} on {connection['host']}"
    if connection["type"] == "api":
        return f"API: {safe_url(connection['url'])}"
    return ""


def switched_off(connection: dict) -> str:
    """Why this server can't fetch it at all, or ""."""
    if connection["type"] == "database":
        if not conf.get("ALLOW_DB_CONNECT"):
            return "Fetching from a database is switched off here. Pick an uploaded dataset."
    elif not conf.get("ALLOW_API_IMPORT"):
        return "Fetching from an API is switched off here. Pick an uploaded dataset."
    return ""


def fetch_problem(entry: dict, parent_pick, typed: dict) -> str:
    """Why "↻ Fetch again" can't run yet for this recipe dataset, or "".
    `parent_pick`: what the parent list is matched to (for a JSON child list);
    `typed`: {group: {secret key: value}}."""
    connection = entry.get("connection")
    if connection is None:
        return "It was not fetched from an API or database. Pick a dataset."
    off = switched_off(connection)
    if off:
        return off
    if connection["type"] == "api_child" and parent_pick != FETCH:
        return "It comes with its parent list: fetch that one too, or pick a dataset."
    mine = typed.get(group_of(connection), {})
    missing = [
        f["label"] for f in secret_fields(connection) if not f["saved"] and not mine.get(f["key"])
    ]
    if missing:
        return f"Type {', '.join(missing)} to fetch it."
    return ""


# ---------- fetching ----------


def filled(connection: dict, typed: dict) -> dict:
    """The connection with what the user typed put in (typed wins over saved)."""
    mine = typed.get(group_of(connection), {})
    return _replaced(connection, {key: value for key, value in mine.items() if value})


def fetch_again(
    workspace: Workspace, entries: list[dict], typed: dict
) -> tuple[dict[int, int | None], list[int]]:
    """Fetch these recipe datasets again: each API / server once, however many datasets came
    from it. Returns ({recipe id: new dataset id, or None for a child list that is not in the
    reply}, every new dataset id). All or nothing: raises FetchFailed.
    Call WITHOUT holding workspace.lock: the waiting for the network happens unlocked, so the
    user's other pages keep working; each dataset is added under the lock."""
    groups: dict[str, tuple[dict, list]] = {}
    for entry in entries:
        if entry["connection"]["type"] != "api_child":
            connection = filled(entry["connection"], typed)
            groups.setdefault(group_of(entry["connection"]), (connection, []))[1].append(entry)
    found: dict[int, int | None] = {}
    made: list[int] = []
    try:
        for connection, group in groups.values():
            if connection["type"] == "api":
                datasets = _fetch_api(workspace, connection, group)
                made += [d.dataset_id for d in datasets]
                for entry in group:
                    found[entry["id"]] = _list_in(datasets, entry)
            else:
                datasets = _fetch_database(workspace, connection, group)
                made += [d.dataset_id for d in datasets]
                for entry, dataset in zip(group, datasets, strict=True):
                    found[entry["id"]] = dataset.dataset_id
        with workspace.lock:
            for entry in entries:
                connection = entry["connection"]
                if connection["type"] == "api_child":
                    parent_id = found.get(connection["parent"])
                    child = next(
                        (
                            d
                            for d in workspace.datasets.values()
                            if parent_id is not None
                            and d.parent_id == parent_id
                            and d.child_path == connection["path"]
                        ),
                        None,
                    )
                    found[entry["id"]] = child.dataset_id if child else None
    except BaseException:
        remove_fetched(workspace, made)
        raise
    return found, made


def remove_fetched(workspace: Workspace, made: list[int]) -> None:
    with workspace.lock:
        for dataset_id in reversed(made):
            if dataset_id in workspace.datasets:  # a child goes with its parent
                workspace.remove_dataset(dataset_id)
        workspace.refresh_memory()


def _list_in(datasets: list[Dataset], entry: dict) -> int:
    list_path = entry["connection"]["list_path"]
    main = next((d for d in datasets if d.parent_id is None and d.list_path == list_path), None)
    if main is None:
        raise FetchFailed(
            f"“{entry['name']}”: the API's reply has no list at "
            f"“{list_path or 'the top level'}” any more."
        )
    return main.dataset_id


def _names(group) -> str:
    return ", ".join(f"“{entry['name']}”" for entry in group)


def _fetch_api(workspace: Workspace, connection: dict, group: list[dict]) -> list[Dataset]:
    method = connection["method"].upper()
    source = ApiSource(
        url=connection["url"],
        method=method,
        headers=connection["headers"],
        body=connection["body"] if method == "POST" else "",
        rows_at=connection["rows_at"],
        name=connection["name"],
    )
    try:
        parsed = parse_json(fetch_json(source), rows_at=source.rows_at)  # unlocked: the wait
        datasets = load_parsed(workspace, source.name, safe_url(source.url), parsed)
    except (ApiFailed, UploadRejected) as error:
        logger.info("Recipe fetch from %s failed: %s", safe_url(source.url), error)
        raise FetchFailed(f"Could not fetch {_names(group)} from the API: {error}") from error
    for dataset in datasets:
        if dataset.parent_id is None:
            dataset.api_source = source  # so Refresh works afterwards, as after an import
    return datasets


def _fetch_database(workspace: Workspace, connection: dict, group: list[dict]) -> list[Dataset]:
    try:
        server = db_connect.server_from_form(connection | {"port": str(connection["port"])})
        wanted = [
            (
                entry["name"],
                db_connect.check_select(entry["connection"]["select"]),
                entry["connection"]["table"],
            )
            for entry in group
        ]
        return read_from_server(workspace, server, wanted)
    except UploadRejected as error:
        logger.info(
            "Recipe fetch from %s on %s failed: %s",
            connection["database"],
            connection["host"],
            error,
        )
        raise FetchFailed(f"Could not read {_names(group)} from the database: {error}") from error
