"""Dashboard pages: the tile grid, its filters, Edit mode and "Add to dashboard".

The page draws empty tile boxes; each box then loads its own content (htmx, on load), so a
slow table never holds up the charts. Click filters live in the page (Alpine store) and go
along with every tile request; global filters are saved on the dashboard.
"""

import json
import logging
import re
import secrets

from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.html import format_html
from django.views.decorators.http import require_GET, require_POST
from django_datatable import RowsTable, table_response

from . import dashboard as dash
from . import querying
from .query_views import _number, _take_notice, table_columns, table_rows
from .views import home_after_expiry, page_context, panel_response
from .workspace import FilterItem, NoteValue, current_workspace

logger = logging.getLogger(__name__)

KEY_PATTERN = re.compile(r"[a-z0-9]{1,16}")


def _clean(text: str, length: int) -> str:
    return " ".join((text or "").split())[:length]


def _is_htmx(request) -> bool:
    return request.headers.get("HX-Request") == "true"


def _gone(request, expired: bool):
    """Where to go when the data (or this dashboard) is no longer here."""
    target = home_after_expiry() if expired else redirect("my_insights:home")
    if _is_htmx(request):
        return HttpResponse(status=204, headers={"HX-Redirect": target.url})
    return target


def _dashboard_url(dashboard, edit: bool = False) -> str:
    url = reverse("my_insights:dashboard", args=[dashboard.dashboard_id])
    return url + "?edit=1" if edit else url


# ---------- the page ----------


def _grid_context(workspace, dashboard, edit: bool, choices: list | None = None) -> dict:
    """choices: dash.kpi_choices, when the caller has them already (worked out here only in
    Edit mode with a Note tile: notes insert values from it)."""
    tiles = []
    names_by_dataset: dict[int, list[str]] = {}  # each dataset's field names, worked out once
    if choices is None:
        has_note = any(tile.kind == "note" for tile in dashboard.tiles)
        choices = dash.kpi_choices(workspace) if edit and has_note else []
    for tile in dashboard.tiles:
        # a filter on any of these fields reloads the tile (a note: its live values' fields)
        fields: list[str] = []
        for asked in dash.query_tiles(tile):
            dataset = dash.tile_source(workspace, asked).dataset
            if dataset is None:
                continue
            if dataset.dataset_id not in names_by_dataset:
                names = [info.name for info in dash.usable_fields(dataset)]
                names_by_dataset[dataset.dataset_id] = names
            fields += [name for name in names_by_dataset[dataset.dataset_id] if name not in fields]
        kind_icon, kind_label, _ = dash.TILE_KINDS[tile.kind]
        item = {
            "tile": tile,
            "icon": kind_icon,
            "kind_label": kind_label,
            "fields": json.dumps(fields),
            "prefix": f"t{tile.tile_id}_",  # its form fields, e.g. t3_color
        }
        if edit and tile.kind == "note":
            item["values_json"] = json.dumps(
                [
                    {"ref": f"{value.query_id}:{value.value_key}", "label": value.label}
                    for value in tile.values
                ]
            )
        tiles.append(item)
    return {
        "dashboard": dashboard,
        "tiles": tiles,
        "edit": edit,
        "widths": list(dash.WIDTHS.items()),
        "heights": list(dash.HEIGHTS.items()),
        "colors": [  # (key, name, swatch class), e.g. ("green", "Green", "bg-green-600")
            (key, name, strong.split()[0]) for key, (name, _, strong) in dash.TILE_COLORS.items()
        ],
        "tones": list(dash.TONES.items()),
        "value_choices": [
            {"ref": ref, "text": text, "label": text.rpartition(" — ")[2]} for ref, text in choices
        ],
        "max_note_values": dash.MAX_NOTE_VALUES,
    }


def _filter_context(workspace, dashboard, problem: str = "") -> dict:
    fields = dash.dashboard_fields(workspace, dashboard)
    rows = []
    for item in dashboard.filters:
        choice = fields.get(item.column)
        kind = choice.kind_class if choice else ""
        row = {
            "item": item,
            "kind": kind,
            "ops": querying.FILTER_OPS.get(kind, []),
            "dataset_id": choice.dataset_id if choice else None,
            "ready": dash.filter_ready(item),
            "a": item.values[0] if item.values else "",
            "b": item.values[1] if len(item.values) > 1 else "",
        }
        if kind == "text" and item.op in ("in", "not_in"):
            row["choices"] = [
                (value, "(blank)" if value == querying.BLANK else value, None, True)
                for value in item.values
            ]
            row["picked_count"] = len(item.values)
        rows.append(row)
    return {
        "dashboard": dashboard,
        "filter_rows": rows,
        "filter_fields": list(fields.values()),
        "filter_problem": problem,
    }


def _page(request, workspace, dashboard, edit: bool, **extra):
    choices = dash.kpi_choices(workspace) if edit else []
    context = page_context(
        request,
        workspace,
        False,
        **(
            _grid_context(workspace, dashboard, edit, choices)
            | _filter_context(workspace, dashboard)
        ),
        tile_kinds=[(key, *values) for key, values in dash.TILE_KINDS.items()],
        charts=sorted(workspace.charts.values(), key=lambda chart: chart.name.casefold()),
        queries=sorted(workspace.queries.values(), key=lambda query: query.name.casefold()),
        kpi_choices=choices,
        new_note={"tile_id": 0, "text": "", "values_json": "[]"},  # the Add tile box's editor
        problem_notice=_take_notice(workspace),
        **extra,
    )
    return render(request, "my_insights/dashboard.html", context)


def _dashboard_or_gone(request, dashboard_id: int):
    """(workspace, dashboard, None) or (…, None, a response to send instead)."""
    workspace, expired = current_workspace(request)
    if workspace is None:
        return None, None, _gone(request, expired)
    dashboard = workspace.dashboards.get(dashboard_id)
    if dashboard is None:
        if _is_htmx(request):
            return workspace, None, _gone(request, False)
        raise Http404("This dashboard is not here (it may have been removed or cleared).")
    return workspace, dashboard, None


@require_GET
def dashboard_page(request, dashboard_id: int):
    workspace, dashboard, response = _dashboard_or_gone(request, dashboard_id)
    if response:
        return response
    with workspace.lock:
        return _page(request, workspace, dashboard, request.GET.get("edit") == "1")


@require_POST
def dashboard_create(request):
    workspace, expired = current_workspace(request)
    if workspace is None:
        return _gone(request, expired)
    with workspace.lock:
        name = _clean(request.POST.get("name", ""), querying.MAX_NAME_LENGTH + 1)
        name = name or dash.default_name(workspace)
        problem = dash.name_error(workspace, name)
        if problem:
            logger.info("Dashboard not made: %s", problem)
            workspace.notice = problem
            return _go(request, reverse("my_insights:home"))
        made = dash.create(workspace, name)
    return _go(request, _dashboard_url(made, edit=True))


def _go(request, url: str):
    if _is_htmx(request):
        return HttpResponse(status=204, headers={"HX-Redirect": url})
    return redirect(url)


@require_POST
def dashboard_rename(request, dashboard_id: int):
    workspace, dashboard, response = _dashboard_or_gone(request, dashboard_id)
    if response:
        return response
    with workspace.lock:
        name = _clean(request.POST.get("name", ""), querying.MAX_NAME_LENGTH + 1)
        problem = dash.name_error(workspace, name, ignore_id=dashboard.dashboard_id)
        if problem:
            logger.info("Dashboard %s not renamed: %s", dashboard_id, problem)
            return _page(request, workspace, dashboard, True, rename_problem=problem)
        dashboard.name = name
    return _go(request, _dashboard_url(dashboard, edit=True))


@require_POST
def dashboard_remove(request, dashboard_id: int):
    workspace, _ = current_workspace(request)
    if workspace is not None:
        with workspace.lock:
            if workspace.dashboards.pop(dashboard_id, None) is not None:
                logger.info("Dashboard %s removed", dashboard_id)
    if _is_htmx(request) and request.POST.get("from") == "home":
        return panel_response(request, workspace)
    return _go(request, reverse("my_insights:home"))


# ---------- one tile ----------


@require_GET
def tile_view(request, dashboard_id: int, tile_id: int):
    """One tile's content (and the sort / page clicks inside a Table tile)."""
    workspace, dashboard, response = _dashboard_or_gone(request, dashboard_id)
    if response:
        return response
    with workspace.lock:
        tile = dashboard.tile(tile_id)
        if tile is None:
            return HttpResponse(
                '<p class="text-sm text-base-content/60">This tile was removed.</p>'
            )
        clicks = dash.read_clicks(request.GET.get("clicks", ""))
        fields = dash.dashboard_fields(workspace, dashboard)
        view = dash.build_tile(workspace, dashboard, tile, fields, clicks)
        if view.problem:
            logger.info("Tile %s/%s problem shown: %s", dashboard_id, tile_id, view.problem)
        context = {
            "view": view,
            "dashboard": dashboard,
            "tile": tile,
            "option_id": f"tile-option-{tile.tile_id}",
            "click_id": f"tile-click-{tile.tile_id}",
        }
        if view.result is not None:
            table = _tile_table(request, dashboard, tile, view.result)
            context["table"] = table
            return table_response(
                request,
                table,
                "my_insights/partials/dashboard_tile.html",
                "my_insights/partials/query_result.html",
                context,
            )
    return render(request, "my_insights/partials/dashboard_tile.html", context)


def _tile_table(request, dashboard, tile, result) -> RowsTable:
    rows = table_rows(result)
    per_page = 25 if tile.height == "tall" else 10
    return RowsTable(
        request.GET,
        rows,
        table_columns(result.columns, rows),
        table_id=f"dashboard-table-{tile.tile_id}",
        base_url=reverse("my_insights:dashboard_tile", args=[dashboard.dashboard_id, tile.tile_id]),
        push_url=False,  # a table inside the dashboard: never change the page address
        search_fields=tuple("t" + column.key for column in result.columns),
        search_placeholder="Search",
        noun="rows",
        default_per_page=per_page,
        page_sizes=(10, 25, 50),
        density_switch=False,
        border_toggle=False,
        sticky_header=False,
    )


# ---------- Edit mode ----------


@require_POST
def tile_add(request, dashboard_id: int):
    workspace, dashboard, response = _dashboard_or_gone(request, dashboard_id)
    if response:
        return response
    with workspace.lock:
        problem = _add_from_form(workspace, dashboard, request.POST)
        if problem:
            logger.info("Tile not added to dashboard %s: %s", dashboard_id, problem)
            workspace.notice = problem
    return _go(request, _dashboard_url(dashboard, edit=True))


def _add_from_form(workspace, dashboard, data) -> str:
    """Add the tile the "Add tile" box asks for. Returns a problem ("" when added)."""
    if len(dashboard.tiles) >= dash.MAX_TILES:
        return f"A dashboard can hold {dash.MAX_TILES} tiles. Remove one first."
    kind = data.get("kind", "")
    color = data.get("t0_color", "")  # KPI / Note: the card colour picked in the box
    look = {
        "color": color if color in dash.TILE_COLORS else "",
        "tone": data.get("t0_tone") if data.get("t0_tone") in dash.TONES else "soft",
    }
    if kind == "chart":
        chart_id = _number(data.get("chart_id"))
        if chart_id not in workspace.charts:
            return "Pick a saved chart."
        dash.add_tile(dashboard, "chart", chart_id=chart_id)
    elif kind == "table":
        query_id = _number(data.get("query_id"))
        if query_id not in workspace.queries:
            return "Pick a saved query."
        dash.add_tile(dashboard, "table", query_id=query_id)
    elif kind == "kpi":
        query_part, _, value_key = data.get("kpi_value", "").partition(":")
        query = workspace.queries.get(_number(query_part))
        if query is None or not any(item.key == value_key for item in query.spec.values):
            return "Pick a value for the KPI, e.g. Total sales — Sum of Amount."
        dash.add_tile(
            dashboard,
            "kpi",
            query_id=query.query_id,
            value_key=value_key,
            title=_clean(data.get("title", ""), querying.MAX_NAME_LENGTH),
            subtitle=_clean(data.get("subtitle", ""), dash.SUBTITLE_LENGTH),
            **look,
        )
    elif kind == "note":
        text = (data.get("t0_text") or data.get("text") or "").strip()[: dash.NOTE_LENGTH]
        if not text:
            return "Type the note's text."
        values = _read_note_values(workspace, data.get("t0_values", ""), 0)
        dash.add_tile(dashboard, "note", text=text, values=values, **look)
    else:
        return "Pick what kind of tile to add."
    logger.info("Tile (%s) added to dashboard %s", kind, dashboard.dashboard_id)
    return ""


@require_POST
def layout_save(request, dashboard_id: int):
    """Edit mode: the tile order (drag), sizes and KPI / note texts. Redraws the grid."""
    workspace, dashboard, response = _dashboard_or_gone(request, dashboard_id)
    if response:
        return response
    with workspace.lock:
        data = request.POST
        by_id = {tile.tile_id: tile for tile in dashboard.tiles}
        order = [number for number in map(_number, data.getlist("order")) if number in by_id]
        order += list(by_id)  # never lose one
        dashboard.tiles = [by_id[number] for number in dict.fromkeys(order)]
        for tile in dashboard.tiles:
            prefix = f"t{tile.tile_id}_"
            if data.get(prefix + "width") in dash.WIDTHS:
                tile.width = data[prefix + "width"]
            if data.get(prefix + "height") in dash.HEIGHTS:
                tile.height = data[prefix + "height"]
            if tile.kind == "kpi" and prefix + "title" in data:
                tile.title = _clean(data[prefix + "title"], querying.MAX_NAME_LENGTH)
                tile.subtitle = _clean(data.get(prefix + "subtitle", ""), dash.SUBTITLE_LENGTH)
            if tile.kind == "note" and (data.get(prefix + "text") or "").strip():
                tile.text = data[prefix + "text"].strip()[: dash.NOTE_LENGTH]
            if tile.kind == "note" and prefix + "values" in data:
                tile.values = _read_note_values(workspace, data[prefix + "values"], tile.tile_id)
            if tile.kind in dash.COLORED_KINDS and prefix + "color" in data:
                color = data[prefix + "color"]
                tile.color = color if color in dash.TILE_COLORS else ""
                if data.get(prefix + "tone") in dash.TONES:
                    tile.tone = data[prefix + "tone"]
        logger.info("Dashboard %s layout saved", dashboard_id)
        return _grid(request, workspace, dashboard)


def _read_note_values(workspace, raw: str, tile_id: int) -> list[NoteValue]:
    """A note's live values from the editor's JSON, e.g. [{"ref": "3:avg", "label": "Avg
    price"}]. Unknown queries / values, empty or repeated names are dropped; at most
    MAX_NOTE_VALUES are kept (the editor stops there too)."""
    try:
        items = json.loads(raw or "[]")
    except ValueError:
        logger.warning("Note %s values were not readable: %r", tile_id, raw[:200])
        return []
    values: list[NoteValue] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        query_part, _, value_key = str(item.get("ref", "")).partition(":")
        query = workspace.queries.get(_number(query_part))
        label = _clean(str(item.get("label", "")), querying.MAX_NAME_LENGTH).replace("[", "")
        label = label.replace("]", "").strip()
        if query is None or not any(value.key == value_key for value in query.spec.values):
            continue
        if not label or any(value.label == label for value in values):
            continue
        values.append(NoteValue(query.query_id, value_key, label))
    if len(values) > dash.MAX_NOTE_VALUES:
        logger.warning("Note %s had %s values; kept %s", tile_id, len(values), dash.MAX_NOTE_VALUES)
    return values[: dash.MAX_NOTE_VALUES]


@require_POST
def note_preview(request, dashboard_id: int, tile_id: int):
    """Edit mode: the note being typed, formatted, with its live values as name chips."""
    _, _, response = _dashboard_or_gone(request, dashboard_id)
    if response:
        return response
    prefix = f"t{tile_id}_"
    text = (request.POST.get(prefix + "text") or "")[: dash.NOTE_LENGTH]
    try:
        items = json.loads(request.POST.get(prefix + "values") or "[]")
        labels = [str(item.get("label", "")) for item in items if isinstance(item, dict)]
    except (ValueError, AttributeError, TypeError):
        logger.info("Note %s/%s preview: values not readable", dashboard_id, tile_id)
        labels = []
    html = dash.render_note(text, dict.fromkeys(labels))
    return HttpResponse(html or '<span class="text-base-content/50">Nothing typed yet.</span>')


def _grid(request, workspace, dashboard):
    return render(
        request,
        "my_insights/partials/dashboard_grid.html",
        _grid_context(workspace, dashboard, edit=True),
    )


@require_POST
def tile_remove(request, dashboard_id: int, tile_id: int):
    workspace, dashboard, response = _dashboard_or_gone(request, dashboard_id)
    if response:
        return response
    with workspace.lock:
        dashboard.tiles = [tile for tile in dashboard.tiles if tile.tile_id != tile_id]
        logger.info("Tile %s removed from dashboard %s", tile_id, dashboard_id)
        return _grid(request, workspace, dashboard)


# ---------- global filters ----------


def _read_filters(data, fields) -> list[FilterItem]:
    filters = []
    for key in list(dict.fromkeys(data.getlist("filters")))[: dash.MAX_FILTERS]:
        if not KEY_PATTERN.fullmatch(key):
            continue
        column = data.get(f"{key}_column", "")
        op = data.get(f"{key}_op", "")
        if op in ("in", "not_in"):
            values = list(dict.fromkeys(data.getlist(f"{key}_values")))[: querying.MAX_TICK_VALUES]
        else:
            values = [data.get(f"{key}_a", ""), data.get(f"{key}_b", "")]
        choice = fields.get(column)
        if choice is not None:
            known = {op_key for op_key, _ in querying.FILTER_OPS[choice.kind_class]}
            op = op if op in known else querying.DEFAULT_OP[choice.kind_class]
        filters.append(FilterItem(key, column, "", op, [value[:200] for value in values]))
    return filters


def _signature(filters) -> dict[str, list]:
    """{field: what its ready filters test}: a field whose entry changed must be redrawn."""
    found: dict[str, list] = {}
    for item in filters:
        if dash.filter_ready(item):
            found.setdefault(item.column, []).append((item.op, tuple(item.values)))
    return found


@require_POST
def filters_save(request, dashboard_id: int):
    """A change in the filter bar. Saves the filters and tells the page which fields
    changed, so only the tiles using them reload."""
    workspace, dashboard, response = _dashboard_or_gone(request, dashboard_id)
    if response:
        return response
    with workspace.lock:
        fields = dash.dashboard_fields(workspace, dashboard)
        before = _signature(dashboard.filters)
        filters = _read_filters(request.POST, fields)
        kind, _, target = request.POST.get("action", "").partition(":")
        problem = ""
        if kind == "add_filter":
            choice = fields.get(target)
            if choice is None:
                problem = "That field is not in any tile's data."
            elif len(filters) >= dash.MAX_FILTERS:
                problem = f"Use at most {dash.MAX_FILTERS} filters."
            else:
                op = querying.DEFAULT_OP[choice.kind_class]
                filters.append(FilterItem(secrets.token_hex(3), target, "", op))
        elif kind == "remove":
            filters = [item for item in filters if item.key != target]
        elif kind == "clear_filter":
            for item in filters:
                if item.key == target:
                    item.values = []
        elif kind == "clear_all":
            filters = []
        if problem:
            logger.info("Dashboard %s filter refused: %s", dashboard_id, problem)
        dashboard.filters = filters
        after = _signature(filters)
        changed = sorted(c for c in before.keys() | after.keys() if before.get(c) != after.get(c))
        context = _filter_context(workspace, dashboard, problem)
    response = render(request, "my_insights/partials/dashboard_filters.html", context)
    if changed:
        response["HX-Trigger"] = json.dumps({"insights-filters-changed": {"columns": changed}})
    return response


# ---------- "Add to dashboard" from the chart / query screens ----------


@require_POST
def add_to_dashboard(request):
    """Put a saved chart (or a saved query as a table) on a dashboard, or on a new one."""
    workspace, expired = current_workspace(request)
    if workspace is None:
        return _gone(request, expired)
    with workspace.lock:
        kind = request.POST.get("kind", "")
        chart_id, query_id = (
            _number(request.POST.get("chart_id")),
            _number(request.POST.get("query_id")),
        )
        if (kind == "chart" and chart_id not in workspace.charts) or (
            kind == "table" and query_id not in workspace.queries
        ):
            return _added(problem="Save it first, then add it to a dashboard.")
        if kind not in ("chart", "table"):
            return _added(problem="Only a saved chart or query can be added.")
        target = request.POST.get("dashboard", "")
        if target == "new":
            name = _clean(request.POST.get("name", ""), querying.MAX_NAME_LENGTH + 1)
            name = name or dash.default_name(workspace)
            problem = dash.name_error(workspace, name)
            if problem:
                return _added(problem=problem)
            dashboard = dash.create(workspace, name)
        else:
            dashboard = workspace.dashboards.get(_number(target))
            if dashboard is None:
                return _added(problem="That dashboard was removed. Pick another.")
        if len(dashboard.tiles) >= dash.MAX_TILES:
            return _added(problem=f"“{dashboard.name}” is full ({dash.MAX_TILES} tiles).")
        if kind == "chart":
            dash.add_tile(dashboard, "chart", chart_id=chart_id)
        else:
            dash.add_tile(dashboard, "table", query_id=query_id)
        logger.info("Added %s to dashboard %s", kind, dashboard.dashboard_id)
    return _added(dashboard=dashboard)


def _added(dashboard=None, problem: str = "") -> HttpResponse:
    if problem:
        logger.info("Not added to a dashboard: %s", problem)
        return HttpResponse(format_html('<span class="text-sm text-error">{}</span>', problem))
    return HttpResponse(
        format_html(
            '<span class="text-sm text-success">Added to “{}”. '
            '<a class="link" href="{}" title="Open the dashboard">Open it</a></span>',
            dashboard.name,
            _dashboard_url(dashboard),
        )
    )
