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
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from django.utils.html import escape, urlize
from django.utils.safestring import SafeString, mark_safe

from . import charting, querying
from .querying import DATE_PARTS, QueryFailed
from .workspace import Dashboard, DashboardTile, FilterItem, NoteValue, QuerySpec

logger = logging.getLogger(__name__)

TILE_KINDS = {
    "chart": ("📊", "Chart", "A saved chart. Editing the chart changes the tile too."),
    "table": ("▤", "Table", "A saved query's answer as a table you can sort and page."),
    "kpi": ("#", "KPI", "One big number: a value's grand total, e.g. ₹ 1.2 Cr Total sales."),
    "note": (
        "📝",
        "Note",
        "Text with simple formatting and live values, e.g. Avg price is [[Avg of Price]].",
    ),
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
MAX_NOTE_VALUES = 10

# KPI / note colours: key → (name, soft classes, strong classes). Fixed Tailwind colours, not
# the host's theme colours, so "Purple" is purple in every host. Soft is a see-through tint,
# so it suits dark mode too. Class names are written out in full: a host's Tailwind build
# keeps only names it finds in the my_insights folder (README, step 5).
TILE_COLORS = {
    "blue": ("Blue", "bg-blue-500/10 border-blue-500/40", "bg-blue-600 text-white border-blue-600"),
    "purple": (
        "Purple",
        "bg-violet-500/10 border-violet-500/40",
        "bg-violet-600 text-white border-violet-600",
    ),
    "teal": ("Teal", "bg-teal-500/10 border-teal-500/40", "bg-teal-600 text-white border-teal-600"),
    "green": (
        "Green",
        "bg-green-500/10 border-green-500/40",
        "bg-green-600 text-white border-green-600",
    ),
    "orange": (
        "Orange",
        "bg-orange-500/10 border-orange-500/40",
        "bg-orange-600 text-white border-orange-600",
    ),
    "red": ("Red", "bg-red-500/10 border-red-500/40", "bg-red-600 text-white border-red-600"),
    "grey": (
        "Grey",
        "bg-slate-500/10 border-slate-500/40",
        "bg-slate-600 text-white border-slate-600",
    ),
}
TONES = {"soft": "Soft", "strong": "Strong"}
COLORED_KINDS = {"kpi", "note"}


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


def tile_color_class(tile: DashboardTile) -> str:
    """The card's colour classes: soft = a light tint, strong = full colour with its own
    readable text colour. Plain (or a chart / table tile) = the normal card."""
    color = TILE_COLORS.get(tile.color)
    if color is None or tile.kind not in COLORED_KINDS:
        return "bg-base-100"
    _, soft, strong = color
    return strong if tile.tone == "strong" else soft


def is_strong(tile: DashboardTile) -> bool:
    """A full-colour card: its number and links take the card's text colour."""
    return tile.kind in COLORED_KINDS and tile.color in TILE_COLORS and tile.tone == "strong"


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


def value_tile(tile: DashboardTile, value: NoteValue) -> DashboardTile:
    """A note's live value, asked like a KPI tile. It keeps the note's id, so the note is
    never "its own" clicked chart."""
    return DashboardTile(tile.tile_id, "kpi", query_id=value.query_id, value_key=value.value_key)


def query_tiles(tile: DashboardTile) -> list[DashboardTile]:
    """The tiles whose questions this tile shows: itself, or a note's live values."""
    if tile.kind == "note":
        return [value_tile(tile, value) for value in tile.values]
    return [tile]


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
    for asked in [part for tile in dashboard.tiles for part in query_tiles(tile)]:
        source = tile_source(workspace, asked)  # a note: each live value's query
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
    note_html: str = ""  # note: the formatted text with its live values


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
    if tile.kind == "note":
        shown = _note_values(workspace, dashboard, tile, fields, clicks, view, today)
        view.note_html = render_note(tile.text, shown)
        return view
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


# ---------- notes: simple formatting and live values ----------


@dataclass
class ShownValue:
    text: str = ""  # e.g. ₹ 1,245.50
    problem: str = ""
    gone: bool = False  # its query, value or data was removed
    name: str = ""  # the query's name


def _note_values(workspace, dashboard, tile, fields, clicks, view, today) -> dict:
    """{label: ShownValue} for a note's live values, each worked out like a KPI with the
    dashboard's filters and the page's clicks. Their badges and notes go on the note."""
    shown = {}
    for value in tile.values:
        part = TileView(value_tile(tile, value))
        found = tile_query(workspace, dashboard, part.tile, fields, clicks, part)
        gone = found is None or kpi_spec(found[0], value.value_key) is None
        if found is not None:
            _kpi(workspace, part.tile, *found, part, today)
        shown[value.label] = ShownValue(part.kpi_value, part.problem, gone, part.name)
        if part.problem:
            logger.info("Note %s value %r not shown: %s", tile.tile_id, value.label, part.problem)
        view.filtered_by += [name for name in part.filtered_by if name not in view.filtered_by]
        view.skipped += [text for text in part.skipped if text not in view.skipped]
        view.notes += [text for text in part.notes if text not in view.notes]
    return shown


TOKEN = re.compile(r"\[\[([^\[\]\n]{1,100})\]\]")  # [[Avg of Selling price]]
# a line's size: the mark at its start → (name, classes). Normal has no mark.
NOTE_SIZES = {
    "# ": ("Heading", "text-xl font-bold"),
    "## ": ("Large", "text-base font-semibold"),
    "-# ": ("Small", "text-xs"),
}
_MARKS = [  # on escaped text; bold first, then underline (__) before italic (_)
    (re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*"), r"<strong>\1</strong>"),
    (re.compile(r"(?<!\w)__(?=\S)(.+?)(?<=\S)__(?!\w)"), r"<u>\1</u>"),
    (re.compile(r"(?<!\w)_(?=[^\s_])(.+?)(?<=[^\s_])_(?!\w)"), r"<em>\1</em>"),
]
_LINK = re.compile(r"(<a [^>]*>.*?</a>)")
# a value's place while the line is escaped: private-use characters no one types
KEPT_OPEN, KEPT_CLOSE = "\ue000", "\ue001"
_KEPT = re.compile(KEPT_OPEN + r"(\d+)" + KEPT_CLOSE)
# a value right after a link ("https://x.com[[Total]]") gets a space before it, or urlize
# would make it part of the address
_GLUED = re.compile(r"((?:https?://|www\.)[^\s" + KEPT_OPEN + r"]*)(?=" + KEPT_OPEN + ")")
WARN = "font-semibold underline decoration-dotted"  # readable on any card colour
_BULLET = re.compile(r"^- (.*)$")
_NUMBER = re.compile(r"^(?P<number>\d{1,4})\. (.*)$")


def _value_html(label: str, shown: dict) -> str:
    """A [[label]] in the note: its value, a chip (the editor's preview), or why not."""
    if label not in shown:
        return (
            f'<span class="{WARN}" title="No live value has this name: insert it again '
            f'or fix the name">⚠ unknown value [[{escape(label)}]]</span>'
        )
    value = shown[label]
    if value is None:  # the editor's preview: no numbers worked out
        return f'<span class="badge badge-sm badge-outline">{escape(label)}</span>'
    if value.problem:
        words = "value removed" if value.gone else "no value"
        return f'<span class="{WARN}" title="{escape(value.problem)}">⚠ {words}</span>'
    return (
        f'<span class="font-semibold tabular-nums" title="{escape(label)}, from the query '
        f'“{escape(value.name)}”">{escape(value.text)}</span>'
    )


def _inline(raw: str, shown: dict) -> str:
    """One line: escaped, links clickable, **bold** _italic_ __underline__, [[values]]."""
    kept: list[str] = []

    def keep(match):
        kept.append(_value_html(match.group(1).strip(), shown))
        return f"{KEPT_OPEN}{len(kept) - 1}{KEPT_CLOSE}"

    html = urlize(_GLUED.sub(r"\1 ", TOKEN.sub(keep, raw)), nofollow=True, autoescape=True)
    parts = []
    for part in _LINK.split(html):  # marks never touch a link (its address may have _)
        if not part.startswith("<a "):
            for pattern, tag in _MARKS:
                part = pattern.sub(tag, part)
        parts.append(part)
    return _KEPT.sub(lambda match: kept[int(match.group(1))], "".join(parts))


def render_note(text: str, shown: dict) -> SafeString:
    """A note's text as safe HTML. Everything is escaped first; only these marks become
    styles, so a pasted <script> stays text:

        # Heading   ## Large   -# Small   - bullet   1. numbered
        **bold**  _italic_  __underline__  [[Avg of Price]] → ₹ 1,245.50

    A plain old note (no marks) looks as before: lines kept, links clickable.
    shown: {label: ShownValue}; a None value is the editor's preview (shown as a chip)."""
    blocks: list[str] = []
    lines: list[str] = []  # plain lines, joined with <br>
    items: list[str] = []  # list items
    list_tag = ""
    list_start = 1  # a numbered list keeps its first number: 5. 6. stays 5. 6.

    def flush():
        nonlocal list_tag
        if lines:
            blocks.append("<div>" + "<br>".join(lines) + "</div>")
            lines.clear()
        if items:
            classes = "list-disc" if list_tag == "ul" else "list-decimal"
            start = f' start="{list_start}"' if list_tag == "ol" and list_start != 1 else ""
            html = "".join(items)
            blocks.append(f'<{list_tag}{start} class="{classes} pl-5">{html}</{list_tag}>')
            items.clear()
        list_tag = ""

    for raw in (text or "").splitlines():
        size = next((mark for mark in NOTE_SIZES if raw.startswith(mark)), None)
        item = _BULLET.match(raw) or _NUMBER.match(raw)
        if size:
            flush()
            classes = NOTE_SIZES[size][1]
            blocks.append(f'<div class="{classes}">{_inline(raw[len(size) :], shown)}</div>')
        elif item:
            tag = "ul" if raw.startswith("- ") else "ol"
            if list_tag != tag:
                flush()
                list_tag = tag
                list_start = int(item.group("number")) if tag == "ol" else 1
            items.append(f"<li>{_inline(item.groups()[-1], shown)}</li>")
        else:
            if items:
                flush()
            lines.append(_inline(raw, shown))
    flush()
    return mark_safe("".join(blocks))  # every piece is escaped above


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
