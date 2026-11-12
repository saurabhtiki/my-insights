"""Databases on the home page: connect to a server, import the ticked tables (of a server or an
uploaded SQLite file) or cancel, and Refresh a dataset (from a server or an API)."""

import logging

from django.views.decorators.http import require_POST

from . import api_views, db_connect
from .api_views import names_and_rows
from .db_loading import add_pending, load_tables, refresh_from_server
from .loading import UploadRejected
from .views import panel_response
from .workspace import ServerBusy, check_capacity, current_workspace

logger = logging.getLogger(__name__)


@require_POST
def db_connect_view(request):
    """Connect to a Postgres / MySQL server and show its tables as a pick list."""
    workspace, expired = current_workspace(request, create=True)
    label = request.POST.get("host", "").strip()
    try:
        server = db_connect.server_from_form(request.POST)
        label = server.label
        tables = db_connect.list_tables(server)
    except UploadRejected as error:
        logger.info("Database connect failed for %s: %s", label, error)
        result = {"ok": False, "title": "Not connected", "file": label, "message": str(error)}
        return panel_response(request, workspace, [result], expired)
    add_pending(workspace, server.label, tables, server=server)
    count = len(tables)
    message = (
        f"Found {count} table{'' if count == 1 else 's'} you may read. "
        "Tick the ones you want below (or write a query), then click Import."
    )
    result = {"ok": True, "title": "Connected", "file": server.label, "message": message}
    return panel_response(request, workspace, [result], expired)


@require_POST
def db_import(request, token: str):
    workspace, expired = current_workspace(request)
    if workspace is None:
        message = "Your data was cleared. Connect or upload the database again."
        return panel_response(request, workspace, [{"ok": False, "message": message}], expired)
    with workspace.lock:
        pending = workspace.pending_databases.get(token)
        file_name = pending.file_name if pending else ""
    try:
        check_capacity()
        datasets = load_tables(
            workspace,
            token,
            request.POST.getlist("tables"),
            query_name=request.POST.get("query_name", ""),
            query_sql=request.POST.get("query_sql", ""),
        )
    except (UploadRejected, ServerBusy) as error:
        logger.info("Database import failed for %s: %s", file_name, error)
        result = {"ok": False, "file": file_name, "message": str(error)}
        return panel_response(request, workspace, [result], expired)
    result = {"ok": True, "file": file_name, "message": f"Loaded as {names_and_rows(datasets)}."}
    return panel_response(request, workspace, [result], expired)


@require_POST
def db_cancel(request, token: str):
    workspace, expired = current_workspace(request)
    if workspace is not None:
        with workspace.lock:
            workspace.drop_pending(token)
    return panel_response(request, workspace, [], expired)


@require_POST
def dataset_refresh(request, dataset_id: int):
    """Refresh: read a server dataset again, or fetch an API dataset again."""
    workspace, expired = current_workspace(request)
    dataset = workspace.datasets.get(dataset_id) if workspace else None
    if dataset is None or dataset.db_source is None:
        return api_views.dataset_refresh(request, dataset_id)
    try:
        check_capacity()
        notes = refresh_from_server(workspace, dataset)
    except (UploadRejected, ServerBusy) as error:
        logger.info("Database refresh failed for %s: %s", dataset.name, error)
        result = {
            "ok": False,
            "title": "Not refreshed",
            "file": dataset.name,
            "message": f"{error} The earlier data is kept.",
        }
        return panel_response(request, workspace, [result], expired)
    results = [
        {
            "ok": True,
            "title": "Refreshed",
            "file": dataset.db_source.server.label,
            "message": f"{names_and_rows([dataset])}.",
        }
    ]
    results += [{"ok": False, "warn": True, "title": "Note", "message": note} for note in notes]
    return panel_response(request, workspace, results, expired)
