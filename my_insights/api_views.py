""" "From API" on the home page: test an API, import its JSON, refresh an API dataset."""

import logging
from urllib.parse import urlsplit

from django.shortcuts import render
from django.views.decorators.http import require_POST

from .api_import import ApiFailed, fetch_json, safe_url
from .json_loading import load_parsed, parse_json, refresh_parsed
from .loading import UploadRejected
from .views import name_error, panel_response
from .workspace import ApiSource, ServerBusy, check_capacity, current_workspace

logger = logging.getLogger(__name__)

PREVIEW_COLUMNS = 30


def _source_from(post) -> ApiSource:
    return ApiSource(
        url=post.get("url", "").strip(),
        method=post.get("method", "GET").upper(),
        headers=post.get("headers", ""),
        body=post.get("body", "") if post.get("method", "GET").upper() == "POST" else "",
        rows_at=post.get("rows_at", "").strip(),
    )


def _default_name(url: str) -> str:
    """e.g. https://api.shop.in/v1/orders?page=1 → orders"""
    parts = urlsplit(url)
    last = [part for part in parts.path.split("/") if part]
    return (last[-1] if last else parts.hostname or "API data")[:100]


def _fetch(source: ApiSource):
    """Fetch and read the reply. Raises ApiFailed or UploadRejected."""
    return parse_json(fetch_json(source), rows_at=source.rows_at)


@require_POST
def api_test(request):
    """Fetch once and show what would be loaded: for each list, its rows, columns with an
    example, and child lists."""
    source = _source_from(request.POST)
    try:
        groups = _fetch(source)
    except (ApiFailed, UploadRejected) as error:
        logger.info("API test failed for %s: %s", safe_url(source.url), error)
        return render(request, "my_insights/partials/api_test.html", {"error": str(error)})
    lists = []
    for parsed in groups:
        main = parsed.main
        first = main.rows[0] if main.rows else []
        columns = [
            {"name": name, "example": first[index] if index < len(first) else ""}
            for index, name in enumerate(main.columns)
        ]
        lists.append(
            {
                "rows_at": parsed.rows_at,
                "row_count": len(main.rows),
                "columns": columns[:PREVIEW_COLUMNS],
                "column_count": len(columns),
                "more_columns": len(columns) > PREVIEW_COLUMNS,
                "children": [{"path": t.path, "row_count": len(t.rows)} for t in parsed.children],
            }
        )
    return render(request, "my_insights/partials/api_test.html", {"lists": lists})


@require_POST
def api_import(request):
    workspace, expired = current_workspace(request, create=True)
    source = _source_from(request.POST)
    name = " ".join(request.POST.get("name", "").split()) or _default_name(source.url)
    try:
        check_capacity()
        error = name_error(workspace, name)
        if error:
            raise ApiFailed(error)
        groups = _fetch(source)
        datasets = load_parsed(workspace, name, safe_url(source.url), groups)
    except (ApiFailed, UploadRejected, ServerBusy) as error:
        logger.info("API import failed for %s: %s", safe_url(source.url), error)
        result = {"ok": False, "file": safe_url(source.url), "message": str(error)}
        return panel_response(request, workspace, [result], expired)
    source.name = name
    for dataset in datasets:
        if dataset.parent_id is None:
            dataset.api_source = source
    found = (
        f" Rows taken from {groups[0].rows_at}." if len(groups) == 1 and groups[0].rows_at else ""
    )
    result = {
        "ok": True,
        "file": safe_url(source.url),
        "message": f"Loaded as {names_and_rows(datasets)}.{found}",
    }
    return panel_response(request, workspace, [result], expired)


def names_and_rows(datasets) -> str:
    """e.g. sales – orders (2 rows), sales – shops (1 row)"""
    return ", ".join(
        f"{d.name} ({d.row_count:,} row{'' if d.row_count == 1 else 's'})" for d in datasets
    )


@require_POST
def dataset_refresh(request, dataset_id: int):
    """Fetch an API dataset again; its id and name stay, so its queries and charts follow."""
    workspace, expired = current_workspace(request)
    dataset = workspace.datasets.get(dataset_id) if workspace else None
    if dataset is None or dataset.api_source is None:
        result = {"ok": False, "message": "This dataset is not loaded from an API any more."}
        return panel_response(request, workspace, [result], expired)
    source = dataset.api_source
    try:
        check_capacity()
        notes = refresh_parsed(workspace, source, _fetch(source), safe_url(source.url))
    except (ApiFailed, UploadRejected, ServerBusy) as error:
        logger.info("API refresh failed for %s: %s", dataset.name, error)
        result = {
            "ok": False,
            "title": "Not refreshed",
            "file": dataset.name,
            "message": f"{error} The earlier data is kept.",
        }
        return panel_response(request, workspace, [result], expired)
    refreshed = [d for d in workspace.datasets.values() if d.api_source is source and not d.notice]
    results = [
        {
            "ok": True,
            "title": "Refreshed",
            "file": safe_url(source.url),
            "message": f"{names_and_rows(refreshed)}.",
        }
    ]
    results += [{"ok": False, "warn": True, "title": "Note", "message": note} for note in notes]
    return panel_response(request, workspace, results, expired)
