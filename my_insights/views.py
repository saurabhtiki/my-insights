"""Pages: upload + dataset list (home) and one dataset's profile."""

import logging

from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt, csrf_protect
from django.views.decorators.http import require_GET, require_POST
from django_datatable import (
    CHOICE,
    DATE,
    NUMBER,
    TEXT,
    YES_NO,
    Column,
    RowsTable,
    table_response,
)

from . import charting, conf
from .linking import describe_sources
from .loading import UploadRejected, load_file
from .uploads import MemoryOnlyUploadHandler
from .workspace import (
    MB,
    ServerBusy,
    check_capacity,
    clear_workspace,
    current_workspace,
    memory_status,
)

logger = logging.getLogger(__name__)

PREVIEW_ROWS = 100
KIND_LABELS = {
    "integer": "Whole number",
    "number": "Number",
    "date": "Date",
    "datetime": "Date & time",
    "boolean": "Yes / No",
    "text": "Text",
}
KIND_HELP = {
    "integer": "Numbers without decimals, e.g. 10, 250, 1,00,000.",
    "number": "Numbers that can have decimals, e.g. 12.5.",
    "date": "Calendar dates. Shown as dd-mm-yyyy.",
    "datetime": "Dates with a time of day.",
    "boolean": "Only yes/no or true/false values.",
    "text": "Words, codes or anything that is not purely a number or date.",
}
TABLE_KINDS = {"integer": NUMBER, "number": NUMBER, "date": DATE}


def remove_confirm(workspace, dataset) -> str:
    """The question asked before removing, naming the combined datasets that go with it."""
    return f"Remove “{dataset.name}” from memory?" + _removed_with(workspace, dataset)


def _removed_with(workspace, dataset) -> str:
    """ " This also removes …": what goes with a dataset ("" when nothing)."""
    text = ""
    dependants = workspace.dependants(dataset.dataset_id)
    if dependants:
        names = ", ".join(f"“{d.name}”" for d in dependants)
        text += f" This also removes {names}, built from it."
    queries = workspace.queries_of([dataset.dataset_id, *(d.dataset_id for d in dependants)])
    if queries:
        names = ", ".join(f"“{q.name}”" for q in queries)
        text += f" Saved queries removed too: {names}."
    charts = workspace.charts_of([dataset.dataset_id, *(d.dataset_id for d in dependants)])
    if charts:
        names = ", ".join(f"“{c.name}”" for c in charts)
        text += f" Charts removed too: {names}."
    return text


def query_remove_confirm(workspace, query) -> str:
    text = f"Remove the saved query “{query.name}”?"
    charts = workspace.charts_of_query(query.query_id)
    if charts:
        names = ", ".join(f"“{c.name}”" for c in charts)
        text += f" Its charts are removed too: {names}."
    for dataset in workspace.datasets.values():
        if dataset.query_id == query.query_id:
            text += f" The dataset “{dataset.name}” made from it goes too."
            text += _removed_with(workspace, dataset)
    return text


def chart_rows(workspace, dataset_ids) -> list[dict]:
    """Saved charts for a list: each with the query it follows and why it can't draw ("")."""
    rows = []
    for chart in workspace.charts_of(dataset_ids):
        query = workspace.queries.get(chart.query_id) if chart.query_id else None
        rows.append(
            {
                "chart": chart,
                "query": query,
                "problem": charting.broken_reason(chart.spec, workspace.chart_query(chart)),
            }
        )
    return rows


def page_context(request, workspace, expired: bool, **extra) -> dict:
    datasets = list(workspace.datasets.values()) if workspace else []
    used_mb = workspace.memory_used_bytes / MB if workspace else 0
    return {
        "base_template": conf.get("BASE_TEMPLATE"),
        "datasets": datasets,
        "can_link": len(datasets) >= 2,
        "dashboards": list(workspace.dashboards.values()) if workspace else [],
        "expired": expired,
        "memory": memory_status(),
        "my_memory_mb": round(used_mb, 1),
        "my_memory_limit_mb": conf.get("SESSION_MEMORY_MB"),
        "max_file_mb": conf.get("MAX_FILE_MB"),
        "max_files": conf.get("MAX_FILES_PER_UPLOAD"),
        "idle_warn_seconds": conf.get("IDLE_WARN_MINUTES") * 60,
        "idle_clear_seconds": conf.get("IDLE_CLEAR_MINUTES") * 60,
        "idle_clear_minutes": conf.get("IDLE_CLEAR_MINUTES"),
        **extra,
    }


def _cards(workspace) -> list[dict]:
    """The dataset cards on the home page, each with its remove question."""
    if workspace is None:
        return []
    return [
        {
            "dataset": dataset,
            "remove_confirm": remove_confirm(workspace, dataset),
            "queries": [
                {"query": query, "remove_confirm": query_remove_confirm(workspace, query)}
                for query in workspace.queries_of([dataset.dataset_id])
            ],
            "charts": chart_rows(workspace, [dataset.dataset_id]),
        }
        for dataset in workspace.datasets.values()
    ]


def panel_response(request, workspace, results=(), expired=False):
    """The dataset list (+ upload results): only the panel for htmx, else the whole page."""
    context = page_context(
        request, workspace, expired, results=list(results), cards=_cards(workspace)
    )
    if request.headers.get("HX-Request") == "true":
        return render(request, "my_insights/partials/workspace_panel.html", context)
    return render(request, "my_insights/home.html", context)


@require_GET
def home(request):
    workspace, expired = current_workspace(request)
    expired = expired or request.GET.get("cleared") == "1"  # sent here from a dataset page
    notice = ""
    if workspace is not None:
        notice, workspace.notice = workspace.notice, ""
    results = [{"ok": False, "title": "Not done", "message": notice}] if notice else []
    context = page_context(request, workspace, expired, cards=_cards(workspace), results=results)
    return render(request, "my_insights/home.html", context)


@csrf_exempt  # CSRF is checked in _receive_upload, after the memory-only handler is set
@require_POST
def upload(request):
    try:
        check_capacity()
    except ServerBusy as error:
        workspace, expired = current_workspace(request)
        return panel_response(request, workspace, [{"ok": False, "message": str(error)}], expired)
    handler = MemoryOnlyUploadHandler(
        request,
        max_file_bytes=conf.get("MAX_FILE_MB") * MB,
        max_files=conf.get("MAX_FILES_PER_UPLOAD"),
    )
    request.upload_handlers = [handler]
    return _receive_upload(request, handler)


@csrf_protect
def _receive_upload(request, handler):
    files = request.FILES.getlist("files")
    workspace, expired = current_workspace(request, create=True)
    results = []
    for file_name in handler.too_big:
        logger.info("Upload refused, over size limit: %s", file_name)
        results.append(
            {
                "ok": False,
                "file": file_name,
                "message": f"The file is larger than the {conf.get('MAX_FILE_MB')} MB limit.",
            }
        )
    if handler.too_many:
        logger.info("Upload refused, too many files: %s", handler.too_many)
        results.append(
            {
                "ok": False,
                "file": ", ".join(handler.too_many),
                "message": f"Only {conf.get('MAX_FILES_PER_UPLOAD')} files can be uploaded at once. "
                "Upload these again separately.",
            }
        )
    if not files and not results:
        results.append({"ok": False, "message": "Choose at least one CSV or Excel file."})

    for uploaded in files:
        try:
            # The upload's own in-memory buffer: no copy of the file is made.
            datasets = load_file(workspace, uploaded.name, uploaded.file)
        except UploadRejected as error:
            logger.info("Upload rejected: %s: %s", uploaded.name, error)
            results.append({"ok": False, "file": uploaded.name, "message": str(error)})
            continue
        finally:
            uploaded.close()  # free the file's memory before reading the next one
        names = ", ".join(dataset.name for dataset in datasets)
        rows = sum(dataset.row_count for dataset in datasets)
        results.append(
            {
                "ok": True,
                "file": uploaded.name,
                "message": f"Loaded as {names} ({rows:,} rows).",
            }
        )
    return panel_response(request, workspace, results, expired)


@require_POST
def clear(request):
    clear_workspace(request)
    return redirect("my_insights:home")


@require_POST
def keep_alive(request):
    """The user clicked "Keep my data": touching the workspace restarts its idle timer."""
    workspace, expired = current_workspace(request)
    return HttpResponse(status=410 if expired or workspace is None else 204)


def home_after_expiry():
    """Back to the start page, which then explains that idle data was cleared."""
    return redirect(reverse("my_insights:home") + "?cleared=1")


def _dataset_or_404(request, dataset_id):
    workspace, expired = current_workspace(request)
    dataset = workspace.datasets.get(dataset_id) if workspace else None
    if dataset is None:
        if expired:
            return workspace, None
        raise Http404("This dataset is not loaded (it may have been removed or cleared).")
    return workspace, dataset


@require_GET
def dataset_detail(request, dataset_id: int):
    workspace, dataset = _dataset_or_404(request, dataset_id)
    if dataset is None:
        return home_after_expiry()
    return _dataset_page(request, workspace, dataset)


def _dataset_page(request, workspace, dataset, rename_error: str = "", status: int = 200):
    view = "rows" if request.GET.get("view") == "rows" else "columns"
    table = (
        _rows_table(request, workspace, dataset)
        if view == "rows"
        else _profile_table(request, dataset)
    )
    context = page_context(
        request,
        workspace,
        False,
        dataset=dataset,
        table=table,
        view=view,
        rename_error=rename_error,
        preview_rows=PREVIEW_ROWS,
        remove_confirm=remove_confirm(workspace, dataset),
        queries=workspace.queries_of([dataset.dataset_id]),
        charts=workspace.charts_of([dataset.dataset_id]),
    )
    response = table_response(
        request,
        table,
        "my_insights/dataset.html",
        "my_insights/partials/dataset_table.html",
        context,
    )
    response.status_code = status
    return response


def _profile_table(request, dataset) -> RowsTable:
    rows = [
        {
            "name": column.name,
            "kind": KIND_LABELS[column.kind],
            "kind_help": KIND_HELP[column.kind],
            "date_format": column.date_format,
            "blanks": column.blanks,
            "blank_percent": round(100 * column.blanks / dataset.row_count)
            if dataset.row_count
            else 0,
            "samples": ", ".join(column.samples),
            "check": "Needs a look" if column.mixed else ("Note" if column.note else "OK"),
            "note": column.note,
            "mixed_examples": column.mixed_examples,
        }
        for column in dataset.columns
    ]
    return RowsTable(
        request.GET,
        rows,
        [
            Column("name", "Column"),
            Column("kind", "Type", type=CHOICE),
            Column("blanks", "Blanks", type=NUMBER),
            Column("samples", "Examples", sortable=False),
            Column("check", "Check", type=CHOICE),
        ],
        table_id="insights-profile",
        base_url=request.path,
        search_fields=("name", "samples"),
        search_placeholder="Find a column",
        keep=("view",),
        noun="columns",
        default_per_page=50,
        page_sizes=(25, 50, 100, 300),
    )


def _rows_table(request, workspace, dataset) -> RowsTable:
    with workspace.lock:
        records = workspace.connection.execute(
            f'SELECT * FROM "{dataset.table_name}" LIMIT {PREVIEW_ROWS}'
        ).fetchall()
    return rows_table(request, records, dataset.columns, "insights-rows", request.path)


def rows_table(
    request, records, profiles, table_id: str, base_url: str, push_url=True
) -> RowsTable:
    """A table of data rows. Columns use keys c0, c1… so a column called "page" or "sort"
    can't clash with the table's own URL settings."""
    keys = [f"c{index}" for index in range(len(profiles))]
    rows = [dict(zip(keys, record, strict=True)) for record in records]
    columns = [
        # Yes / No columns get a tick-list showing Yes / No, the same words as the cells.
        Column(key, column.name, type=CHOICE, choices=YES_NO)
        if column.kind == "boolean"
        else Column(key, column.name, type=TABLE_KINDS.get(column.kind, TEXT))
        for key, column in zip(keys, profiles, strict=True)
    ]
    return RowsTable(
        request.GET,
        rows,
        columns,
        table_id=table_id,
        base_url=base_url,
        push_url=push_url,
        search_fields=tuple(keys),
        search_placeholder="Search these rows",
        keep=("view",),
        noun="rows",
        default_per_page=25,
        page_sizes=(25, 50, 100),
        column_toggle=True,
    )


def name_error(workspace, new_name: str, ignore_id: int | None = None) -> str:
    """Why a dataset name can't be used, or "" when it is fine."""
    if not new_name:
        return "Type a name for the dataset."
    if len(new_name) > 100:
        return "Keep the name under 100 characters."
    if workspace.unique_name(new_name, ignore_id=ignore_id) != new_name:
        return f"Another dataset is already called “{new_name}”."
    return ""


@require_POST
def dataset_rename(request, dataset_id: int):
    workspace, dataset = _dataset_or_404(request, dataset_id)
    if dataset is None:
        return home_after_expiry()
    new_name = " ".join(request.POST.get("name", "").split())
    error = name_error(workspace, new_name, ignore_id=dataset_id)
    if error:
        logger.info("Rename refused for dataset %s: %s", dataset_id, error)
        return _dataset_page(request, workspace, dataset, rename_error=error, status=400)
    with workspace.lock:
        dataset.name = new_name
        for other in workspace.datasets.values():  # "Built from …" lines show the new name
            if other.link and dataset_id in other.link.source_ids():
                other.source_file = describe_sources(other.link, workspace.datasets)
    return redirect("my_insights:dataset", dataset_id=dataset_id)


@require_POST
def dataset_remove(request, dataset_id: int):
    workspace, dataset = _dataset_or_404(request, dataset_id)
    results = []
    if dataset is not None:
        with workspace.lock:
            removed = workspace.remove_dataset(dataset_id)
        if len(removed) > 1:
            names = ", ".join(f"“{name}”" for name in removed)
            results.append({"ok": True, "title": "Removed", "message": names})
    if request.headers.get("HX-Request") == "true":
        return panel_response(request, workspace, results)
    return redirect("my_insights:home")
