"""Dashboards: tiles drawn from saved charts and queries, with filters for every tile.

    Global filter  Branch is one of Delhi  → added to each tile whose dataset has "Branch".
    Click filter   the "Apr 2026" bar of a Month chart → Date between 01-04-2026 and
                   30-04-2026 on the other tiles. Sent by the page with each tile request,
                   never stored.

Each tile asks its own query again with those filters added as "All of these" rules, so the
query builder's SQL, totals and number formats are used unchanged.
"""

import copy
import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from . import charting, querying
from .querying import DATE_PARTS, QueryFailed
from .workspace import Dashboard, DashboardTile, FilterItem, QuerySpec

logger = logging.getLogger(__name__)

TILE_KINDS = {
    "chart": ("📊", "Chart", "A saved chart. Editing the chart changes the tile too."),
    "table": ("▤", "Table", "A saved query's answer as a table you can sort and page."),
    "kpi": ("#", "KPI", "One big number: a value's grand total, e.g. ₹ 1.2 Cr Total sales."),
    "note": ("📝", "Note", "Plain text, e.g. what to look at. Links become clickable."),
}
WIDTHS = {"third": "⅓ width", "half": "½ width", "two_thirds": "⅔ width", "full": "Full width"}
HEIGHTS = {"short": "Short", "tall": "Tall"}
MAX_TILES = 40
MAX_FILTERS = 20
MAX_CLICKS = 10
MAX_CLICK_VALUES = 5000  # labels a chart sends for click-to-filter
NOTE_LENGTH = 2000
SUBTITLE_LENGTH = 120
NEEDS_TEXT = {"contains", "eq", "ne", "gt", "lt", "last_days"}


# ---------- names ----------


def name_error(workspace, name: str, ignore_id: int | None = None) -> str:
    if not name:
        return "Type a name for the dashboard."
    if len(name) > querying.MAX_NAME_LENGTH:
        return f"Keep the name under {querying.MAX_NAME_LENGTH} characters."
    for dashboard in workspace.dashboards.values():
        if dashboard.dashboard_id != ignore_id and dashboard.name.casefold() == name.casefold():
            return f"Another dashboard is already called “{name}”."
    return ""


def default_name(workspace) -> str:
    """ "Dashboard 1", "Dashboard 2"… the first free one."""
    taken = {dashboard.name.casefold() for dashboard in workspace.dashboards.values()}
    number = 1
    while f"dashboard {number}" in taken:
        number += 1
    return f"Dashboard {number}"


def create(workspace, name: str) -> Dashboard:
    dashboard = Dashboard(workspace.new_dashboard_id(), name)
    workspace.dashboards[dashboard.dashboard_id] = dashboard
    logger.info("Dashboard %s created", dashboard.dashboard_id)
    return dashboard


def add_tile(dashboard: Dashboard, kind: str, **settings) -> DashboardTile:
    width = "full" if kind == "table" else "third" if kind == "kpi" else "half"
    tile = DashboardTile(dashboard.new_tile_id(), kind, width=width, **settings)
    dashboard.tiles.append(tile)
    return tile


# ---------- what a tile shows ----------


@dataclass
class TileSource:
    spec: QuerySpec | None = None
    dataset: object = None
    name: str = ""  # the chart's or query's name
    problem: str = ""


def tile_source(workspace, tile: DashboardTile) -> TileSource:
    """The question a tile asks, or why it can't (its chart / query / dataset was removed)."""
    if tile.kind == "note":
        return TileSource()
    if tile.kind == "chart":
        chart = workspace.charts.get(tile.chart_id)
        if chart is None:
            return TileSource(problem="This chart was removed. Remove the tile, or add another.")
        spec, name = workspace.chart_query(chart), chart.name
        if spec is None:
            return TileSource(
                name=name, problem=f"The query the chart “{name}” follows was removed."
            )
    else:
        query = workspace.queries.get(tile.query_id)
        if query is None:
            return TileSource(problem="This query was removed. Remove the tile, or add another.")
        spec, name = query.spec, query.name
    dataset = workspace.datasets.get(spec.dataset_id)
    if dataset is None:
        return TileSource(name=name, problem=f"The data “{name}” uses was removed.")
    return TileSource(spec, dataset, name)


@dataclass
class FieldChoice:
    name: str
    kind: str  # the profile kind, e.g. integer
    kind_class: str  # number | date | text | boolean
    dataset_id: int  # the first dataset with it (its values fill the tick list)


def usable_fields(dataset) -> list:
    """The fields (FieldInfo) a filter can test: not broken, not per-group formulas."""
    return [
        info
        for info in querying.field_catalog(dataset).values()
        if not info.error and not info.is_total
    ]


def dashboard_fields(workspace, dashboard: Dashboard) -> dict[str, FieldChoice]:
    """Every field of the dashboard's datasets, merged by name. The first dataset with a
    field sets its type (a tile where it has another type is not filtered by it)."""
    fields: dict[str, FieldChoice] = {}
    seen = set()
    for tile in dashboard.tiles:
        source = tile_source(workspace, tile)
        if source.dataset is None or source.dataset.dataset_id in seen:
            continue
        seen.add(source.dataset.dataset_id)
        for info in usable_fields(source.dataset):
            fields.setdefault(
                info.name,
                FieldChoice(info.name, info.kind, info.kind_class, source.dataset.dataset_id),
            )
    return fields


def filter_ready(item: FilterItem) -> bool:
    """Is a filter filled in enough to use? e.g. "is one of" with nothing ticked is not."""
    values = [value for value in item.values if value.strip()]
    if item.op in ("in", "not_in"):
        return bool(values)
    if item.op in NEEDS_TEXT:
        return bool(item.values and item.values[0].strip())
    if item.op == "between":
        return bool(values)
    return bool(item.op)


# ---------- click-to-filter ----------


@dataclass
class Click:
    """A clicked bar / slice / point: field = value on every other tile."""

    tile: int  # the chart tile clicked (it is not filtered by its own click)
    column: str
    part: str  # a date part, e.g. month ("" = the value itself)
    kind: str  # the field's kind class on the clicked chart
    value: str | None  # the raw value ("2026-04-01", "Delhi"); None = blank
    label: str = ""  # as shown, e.g. "Apr 2026"


def read_clicks(raw: str) -> list[Click]:
    """The page's click filters (JSON). Anything odd is dropped, never trusted."""
    try:
        items = json.loads(raw or "[]")
    except (TypeError, ValueError):
        logger.info("Click filters ignored: not JSON")
        return []
    clicks = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        tile, column, part = item.get("tile"), item.get("column"), item.get("part") or ""
        kind, value = item.get("kind"), item.get("value")
        if not isinstance(tile, int) or not isinstance(column, str) or not column:
            continue
        if part not in DATE_PARTS and part != "":
            continue
        if kind not in ("number", "date", "text", "boolean"):
            continue
        if value is not None and not isinstance(value, str):
            continue
        label = item.get("label") if isinstance(item.get("label"), str) else ""
        clicks.append(Click(tile, column[:200], part, kind, value and value[:200], label[:200]))
    return clicks[:MAX_CLICKS]


def _month_end(start: date, months: int) -> date:
    """The last day of the month `months - 1` after `start`'s month."""
    year, month = divmod(start.month - 1 + months, 12)
    return date(start.year + year, month + 1, 1) - timedelta(days=1)


def period_range(part: str, raw: str) -> tuple[date, date] | None:
    """The first and last day a date group stands for, e.g. month 2026-04-01 → 1–30 Apr."""
    try:
        if part in ("year", "fy"):
            year = int(raw)
            if part == "year":
                return date(year, 1, 1), date(year, 12, 31)
            return date(year, 4, 1), date(year + 1, 3, 31)
        day = date.fromisoformat(raw[:10])
    except (TypeError, ValueError):
        logger.info("Click on %s value %r: not a date", part, raw)
        return None
    if part == "week":
        return day, day + timedelta(days=6)
    if part == "month":
        return day.replace(day=1), _month_end(day, 1)
    if part in ("quarter", "fy_quarter"):
        start = day.replace(day=1)
        return start, _month_end(start, 3)
    return day, day


def click_filter(click: Click) -> FilterItem | None:
    """The filter a click puts on the other tiles."""
    item = FilterItem("", click.column)
    if click.value is None:
        item.op = "blank"
    elif click.part or click.kind == "date":
        found = period_range(click.part or "day", click.value)
        if found is None:
            return None
        item.op, item.values = "between", [found[0].isoformat(), found[1].isoformat()]
    elif click.kind == "number":
        item.op, item.values = "eq", [click.value]
    elif click.kind == "boolean":
        item.op = "yes" if click.value == "true" else "no"
    else:
        item.op, item.values = "in", [click.value]
    return item


def _raw_text(value) -> str | None:
    """A result cell as the text a click sends back (dates as yyyy-mm-dd)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _click_side(query: QuerySpec, plan, rows, key: str, catalog) -> dict | None:
    """{column, part, kind, title, values: {label shown: raw value}} for one chart field."""
    index = next((i for i, item in enumerate(query.groups) if item.key == key), None)
    if index is None:
        return None
    item = query.groups[index]
    column = next((c for c in plan.columns if c.key == f"g{index}"), None)
    info = catalog.get(item.column)
    if column is None or info is None or info.error:
        return None
    values: dict[str, str | None] = {}
    for row in rows:
        if len(values) >= MAX_CLICK_VALUES:
            break
        values.setdefault(
            querying.format_value(row[column.key], column), _raw_text(row[column.key])
        )
    return {
        "column": item.column,
        "part": column.date_part,
        "kind": info.kind_class,
        "title": column.label,
        "values": values,
    }


def click_map(chart, query: QuerySpec, plan, rows, catalog) -> dict:
    """What the page needs to turn a click on this chart into filters."""
    series = None
    if chart.series and chart.chart_type not in charting.ROUND:
        series = _click_side(query, plan, rows, chart.series, catalog)
    return {
        "x": _click_side(query, plan, rows, chart.x, catalog),
        "series": series,
        "scatter": chart.chart_type == "scatter",
    }


# ---------- drawing a tile ----------


@dataclass
class TileView:
    tile: DashboardTile
    name: str = ""
    problem: str = ""
    notes: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # e.g. "Not filtered by Branch (…)"
    filtered_by: list[str] = field(default_factory=list)  # fields filtering this tile
    option: dict | None = None  # chart
    click: dict | None = None  # chart
    result: object = None  # table: a QueryResult
    kpi_label: str = ""
    kpi_value: str = ""


def _add_filters(spec, catalog, wanted, view: TileView) -> None:
    """Add (filter, kind class) pairs to the tile's query as "All of these" rules."""
    for index, (item, kind) in enumerate(wanted):
        info = catalog.get(item.column)
        if info is None or info.error or info.is_total:
            reason = "no such field"
        elif info.kind_class != kind:
            reason = f"it is {querying.KIND_WORDS.get(info.kind, info.kind)} here"
        else:
            spec.filters.append(
                FilterItem(f"dash{index}", item.column, "", item.op, list(item.values))
            )
            if item.column not in view.filtered_by:
                view.filtered_by.append(item.column)
            continue
        text = f"Not filtered by {item.column} ({reason})"
        if text not in view.skipped:
            view.skipped.append(text)


def tile_query(workspace, dashboard, tile, fields, clicks, view: TileView):
    """(query with the dashboard's filters and the page's clicks added, dataset, catalog), or
    None when the tile has no query or its source is gone (view.problem says why)."""
    if tile.kind == "note":
        return None
    source = tile_source(workspace, tile)
    view.name, view.problem = source.name, source.problem
    if source.problem:
        return None
    dataset = source.dataset
    catalog = querying.field_catalog(dataset)
    spec = copy.deepcopy(source.spec)
    wanted = [
        (item, fields[item.column].kind_class)
        for item in dashboard.filters
        if item.column in fields and filter_ready(item)
    ]
    for click in clicks:
        if click.tile != tile.tile_id:
            made = click_filter(click)
            if made is not None:
                wanted.append((made, click.kind))
    _add_filters(spec, catalog, wanted, view)
    return spec, dataset, catalog


def build_tile(workspace, dashboard, tile, fields, clicks, today=None) -> TileView:
    """Everything one tile shows, with the dashboard's filters and the page's clicks."""
    view = TileView(tile)
    found = tile_query(workspace, dashboard, tile, fields, clicks, view)
    if found is None:
        return view
    spec, dataset, catalog = found

    if tile.kind == "kpi":
        _kpi(workspace, tile, spec, dataset, catalog, view, today)
        return view
    plan = querying.plan_query(spec, dataset, today=today, catalog=catalog)
    if not plan.ok:
        view.problem = " ".join(plan.problems) or "The query has nothing to show yet."
        return view
    try:
        result = querying.run_query(workspace.connection, plan, spec.top_n)
    except QueryFailed as error:
        view.problem = str(error)
        return view
    view.notes = plan.notes + result.notes
    if tile.kind == "table":
        view.result = result
        return view

    saved = workspace.charts[tile.chart_id]
    chart = copy.deepcopy(saved.spec)  # build_chart points it at fields the query still has
    chart_spec, chart_plan, rows = spec, plan, result.rows
    if plan.across is not None:
        chart_spec = querying.long_spec(spec)
        chart_plan = querying.plan_query(chart_spec, dataset, today=today, catalog=catalog)
        rows = result.long_rows
    built = charting.build_chart(chart, chart_spec, chart_plan, rows)
    view.notes = [*view.notes, *built.notes]
    if built.problems:
        view.problem = " ".join(built.problems)
        return view
    view.option = built.option
    view.click = click_map(chart, chart_spec, chart_plan, rows, catalog)
    return view


def kpi_spec(spec: QuerySpec, value_key: str) -> QuerySpec | None:
    """The question a KPI asks: one value's grand total over the rows passing the row filters
    (Group by, Across, sorts, Top N and total filters are left out)."""
    item = next((value for value in spec.values if value.key == value_key), None)
    if item is None:
        return None
    return QuerySpec(
        dataset_id=spec.dataset_id,
        values=[copy.deepcopy(item)],
        filters=[copy.deepcopy(f) for f in spec.filters if not f.value_key],
    )


def _kpi(workspace, tile, spec, dataset, catalog, view: TileView, today) -> None:
    question = kpi_spec(spec, tile.value_key)
    if question is None:
        view.problem = f"The value this KPI showed is no longer in the query “{view.name}”."
        return
    plan = querying.plan_query(question, dataset, today=today, catalog=catalog)
    if not plan.ok:
        view.problem = " ".join(plan.problems) or "The query has nothing to show yet."
        return
    try:
        result = querying.run_query(workspace.connection, plan)
    except QueryFailed as error:
        view.problem = str(error)
        return
    column = plan.columns[0]
    value = result.rows[0][column.key] if result.rows else None
    view.kpi_label = tile.title or column.label
    view.kpi_value = querying.format_value(value, column) or "—"
    view.notes = plan.notes


def kpi_choices(workspace) -> list[tuple[str, str]]:
    """("query id:value key", "Query name — Value") for the KPI picker."""
    choices = []
    for query in workspace.queries.values():
        dataset = workspace.datasets.get(query.spec.dataset_id)
        if dataset is None:
            continue
        catalog = querying.field_catalog(dataset)
        for item in query.spec.values:
            label = querying.value_label(item, catalog)
            choices.append((f"{query.query_id}:{item.key}", f"{query.name} — {label}"))
    return choices
