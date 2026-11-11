"""Downloads: a query's answer (CSV / Excel), a dashboard Table tile's (CSV / Excel, with the
dashboard's filters and the page's clicks) and a whole dashboard as one HTML file.

The buttons are plain links (or a plain form for the dashboard), so the file downloads and
the page stays. When an export can't be made, a short page says why, with a way back.
"""

import copy
import logging

from django.http import HttpResponse
from django.shortcuts import render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from . import dashboard as dash
from . import exporting, querying
from .exporting import ExportFailed
from .query_views import KEY_PATTERN, table_rows
from .views import page_context
from .workspace import current_workspace

logger = logging.getLogger(__name__)

FORMATS = ("csv", "xlsx")
HTML_TABLE_ROWS = 500  # rows of a Table tile written into the dashboard's HTML file


def _problem(request, workspace, message: str, back: str, status: int = 200):
    """A short page saying why the file could not be made. back="" = the previous page."""
    logger.info("Export refused: %s", message)
    context = page_context(request, workspace, False, message=message, back=back)
    return render(request, "my_insights/export_problem.html", context, status=status)


def _export(workspace, spec, dataset, title: str, fmt: str, about_extra=()):
    if fmt == "csv":
        return exporting.csv_response(workspace, spec, dataset, title)
    catalog = querying.field_catalog(dataset)
    about = exporting.about_rows(spec, dataset, title, catalog)
    about[1:1] = list(about_extra)
    return exporting.excel_response(workspace, spec, dataset, title, about)


@require_GET
def query_export(request, draft_id: str, fmt: str):
    """The query screen's current answer, e.g. /query/export/ab12cd/xlsx/?name=Sales."""
    workspace, expired = current_workspace(request)
    back = reverse("my_insights:query") if not expired else reverse("my_insights:home")
    if fmt not in FORMATS:
        return _problem(request, workspace, "Pick CSV or Excel.", back, status=404)
    spec = (
        workspace.query_drafts.get(draft_id)
        if workspace and KEY_PATTERN.fullmatch(draft_id)
        else None
    )
    if spec is None or spec.dataset_id not in workspace.datasets:
        message = (
            "Your data was cleared after a long break. Upload it again to export."
            if expired or workspace is None
            else "This result has expired. Change any setting on the query screen, then export again."
        )
        return _problem(request, workspace, message, back)
    with workspace.lock:  # planning changes the spec it is given: use a copy of the draft
        spec = copy.deepcopy(spec)
    dataset = workspace.datasets[spec.dataset_id]
    title = " ".join(request.GET.get("name", "").split())[: querying.MAX_NAME_LENGTH]
    title = title or querying.default_name(spec, dataset)
    try:
        return _export(workspace, spec, dataset, title, fmt)
    except ExportFailed as error:
        return _problem(request, workspace, str(error), "")


@require_GET
def tile_export(request, dashboard_id: int, tile_id: int, fmt: str):
    """A Table tile's answer as you see it: saved filters + the page's click filters."""
    workspace, expired = current_workspace(request)
    dashboard = workspace.dashboards.get(dashboard_id) if workspace else None
    if dashboard is None:
        message = (
            "Your data was cleared after a long break. Upload it again to export."
            if expired or workspace is None
            else "This dashboard was removed."
        )
        return _problem(request, workspace, message, reverse("my_insights:home"))
    back = reverse("my_insights:dashboard", args=[dashboard_id])
    if fmt not in FORMATS:
        return _problem(request, workspace, "Pick CSV or Excel.", back, status=404)
    with workspace.lock:
        tile = dashboard.tile(tile_id)
        if tile is None or tile.kind != "table":
            return _problem(request, workspace, "This table tile was removed.", back)
        view = dash.TileView(tile)
        clicks = dash.read_clicks(request.GET.get("clicks", ""))
        fields = dash.dashboard_fields(workspace, dashboard)
        found = dash.tile_query(workspace, dashboard, tile, fields, clicks, view)
    if found is None:
        return _problem(
            request, workspace, view.problem or "This tile has nothing to export.", back
        )
    spec, dataset, _ = found
    extra = [("Dashboard", dashboard.name)] + [("Note", text) for text in view.skipped]
    try:
        return _export(workspace, spec, dataset, view.name, fmt, extra)
    except ExportFailed as error:
        return _problem(request, workspace, str(error), back)


@require_POST
def dashboard_export(request, dashboard_id: int):
    """The dashboard as one HTML file: what the page shows now, filters and clicks included.
    Charts stay live (hover works) but need the internet to load the chart library."""
    workspace, expired = current_workspace(request)
    dashboard = workspace.dashboards.get(dashboard_id) if workspace else None
    if dashboard is None:
        message = (
            "Your data was cleared after a long break. Upload it again to export."
            if expired or workspace is None
            else "This dashboard was removed."
        )
        return _problem(request, workspace, message, reverse("my_insights:home"))
    clicks = dash.read_clicks(request.POST.get("clicks", ""))
    with workspace.lock:
        fields = dash.dashboard_fields(workspace, dashboard)
        tiles = []
        for tile in dashboard.tiles:
            view = dash.build_tile(workspace, dashboard, tile, fields, clicks)
            item = {
                "tile": tile,
                "view": view,
                "option_id": f"tile-option-{tile.tile_id}",
                "click_id": f"tile-click-{tile.tile_id}",
                "picked_id": f"tile-picked-{tile.tile_id}",
                # this chart's own click, shown bright with the rest faded
                "picked": [click.label for click in clicks if click.tile == tile.tile_id],
            }
            if view.result is not None:
                rows = table_rows(view.result)
                item |= {
                    "columns": view.result.columns,
                    "rows": [
                        [(row["t" + c.key], c.is_value) for c in view.result.columns]
                        for row in rows[:HTML_TABLE_ROWS]
                    ],
                    "row_count": len(rows),
                    "cut": len(rows) > HTML_TABLE_ROWS or view.result.truncated,
                }
            tiles.append(item)
        # every filter, as on the page: an empty one is listed as "not used", never hidden
        filters = [
            (exporting.filter_text(item, None, {}), True)
            if dash.filter_ready(item)
            else (f"{item.column}: nothing picked, not used", False)
            for item in dashboard.filters
        ]
    clicked = [click.label or f"{click.column} = {click.value or '(blank)'}" for click in clicks]
    html = render_to_string(
        "my_insights/dashboard_export.html",
        {
            "dashboard": dashboard,
            "tiles": tiles,
            "filters": filters,
            "clicked": clicked,
            "exported": timezone.localtime(),
            "table_rows_cap": HTML_TABLE_ROWS,
        },
    )
    logger.info("Dashboard %r exported as HTML (%s tiles)", dashboard.name, len(tiles))
    response = HttpResponse(html, content_type="text/html; charset=utf-8")
    return exporting.attachment(response, exporting.file_name(dashboard.name, "html"))
