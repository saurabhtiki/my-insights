"""Charts: a query's result rows → an Apache ECharts option (plain JSON, no data stored).

    Query: Group by Month, Branch · Values Sum of Amount
    Chart: Stacked column, X = Month, colour = Branch
    → xAxis = ["Apr 2026", "May 2026", …], one bar series per branch, stacked.

Numbers are sent as plain numbers. Each series / axis carries a small "_fmt" hint
("0", "r2", "pct", "short" …) that the page turns into Indian-style labels
(12,34,567 · ₹ 1.2 Cr), so this module never sends code to the browser.
"""

import logging
from dataclasses import dataclass, field
from decimal import Decimal

from .querying import format_value, kind_class
from .workspace import ChartSpec, QuerySpec, ValueStyle

logger = logging.getLogger(__name__)

# key: (label, help)
CHART_TYPES = {
    "column": ("Column", "Upright bars, one per X value. Good for comparing a few items."),
    "bar": ("Horizontal bar", "Bars lying down. Good for long names, e.g. item names."),
    "stacked_column": ("Stacked column", "Each column split into parts by colour."),
    "stacked_bar": ("Stacked bar", "Each horizontal bar split into parts by colour."),
    "line": ("Line", "A line through the points. Good for change over time."),
    "area": ("Area", "A line with the space under it filled in."),
    "pie": ("Pie", "Slices of one whole. Shows one value."),
    "donut": ("Donut", "A pie with a hole in the middle. Shows one value."),
    "scatter": ("Scatter", "One dot per group, placed by two values (X and Y)."),
    "combo": ("Combo", "Bars and lines together, each value on the left or right axis."),
}
STACKED = {"stacked_column", "stacked_bar"}
HORIZONTAL = {"bar", "stacked_bar"}
ROUND = {"pie", "donut"}
CAPPED = {"column", "bar", "stacked_column", "stacked_bar", "combo", "pie", "donut"}
# Colour sets (first colours are used first). "default" is ECharts' own.
THEMES = {
    "default": ("Standard", []),
    "calm": ("Calm blues", ["#1d4e89", "#3a7ca5", "#81c3d7", "#2f6690", "#16425b", "#a9d6e5"]),
    "warm": ("Warm", ["#d1495b", "#edae49", "#00798c", "#30638e", "#003d5b", "#f28f3b"]),
    "earth": ("Earth", ["#606c38", "#283618", "#dda15e", "#bc6c25", "#a3b18a", "#6b705c"]),
    "mono": ("Grey + one colour", ["#2563eb", "#6b7280", "#9ca3af", "#374151", "#d1d5db"]),
}
# ECharts' own colours, used by "default" (shown in the colour boxes, not sent to the chart).
ECHARTS_COLOURS = [
    "#5470c6", "#91cc75", "#fac858", "#ee6666", "#73c0de",
    "#3ba272", "#fc8452", "#9a60b4", "#ea7ccc",
]  # fmt: skip


def theme_colour(theme: str, position: int) -> str:
    """The colour the chart gives its n-th line / bar set when none is picked."""
    colours = THEMES.get(theme, THEMES["default"])[1] or ECHARTS_COLOURS
    return colours[position % len(colours)]


SORTS = {"": "Query order", "desc": "Biggest first", "asc": "Smallest first"}
DEFAULT_BARS = 30
DEFAULT_SLICES = 10
MAX_TOP = 500
MAX_POINTS = 5000
MAX_SERIES = 20
ADDITIVE = {"sum", "count", "rows"}  # parts can be added into an "Others" total
OTHERS = "Others"


@dataclass
class ChartResult:
    option: dict | None = None
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class _Value:
    key: str  # the ValueItem key
    column: str  # v0, v1… in the result rows
    label: str
    number_format: str
    additive: bool
    style: ValueStyle


# ---------- picking fields ----------


def default_chart(query: QuerySpec, plan) -> ChartSpec:
    """A first chart for a query: 1st group → X axis, 2nd group → colours. Dates draw as a line."""
    chart = ChartSpec()
    if query.groups:
        chart.x = query.groups[0].key
        if len(query.groups) > 1:
            chart.series = query.groups[1].key
            chart.chart_type = "stacked_column"
        x_column = _column(plan, "g0")
        if x_column and kind_class(x_column.kind) == "date" and not chart.series:
            chart.chart_type = "line"
    return chart


def default_style(chart: ChartSpec, index: int) -> ValueStyle:
    """Combo: first value as bars on the left, the others as lines on the right."""
    if chart.chart_type == "combo":
        return ValueStyle("bar" if index == 0 else "line", "left" if index == 0 else "right")
    return ValueStyle("", "left")


def value_style(chart: ChartSpec, key: str, index: int) -> ValueStyle:
    saved = chart.styles.get(key)
    base = default_style(chart, index)
    if saved is None:
        return base
    return ValueStyle(saved.kind or base.kind, saved.axis or base.axis, saved.color)


def fit_to_query(chart: ChartSpec, query: QuerySpec) -> list[str]:
    """Point the chart at fields the query still has. Returns notes naming what moved."""
    notes = []
    group_keys = [item.key for item in query.groups]
    value_keys = {item.key for item in query.values}
    if chart.x and chart.x not in group_keys:
        notes.append(_gone("X-axis", chart.names.get(chart.x)))
        chart.x = ""
    if chart.series and chart.series not in group_keys:
        notes.append(_gone("colour", chart.names.get(chart.series)))
        chart.series = ""
    if not chart.x:
        chart.x = next((key for key in group_keys if key != chart.series), "")
    if chart.series == chart.x:
        chart.series = ""
    chart.hidden = [key for key in chart.hidden if key in value_keys]
    chart.styles = {key: style for key, style in chart.styles.items() if key in value_keys}
    return notes


def _gone(role: str, name: str | None) -> str:
    what = f"“{name}”" if name else "it used"
    return (
        f"The {role} field {what} is no longer in the query's Group by, so the chart was changed."
    )


def broken_reason(chart: ChartSpec, query: QuerySpec | None) -> str:
    """Why a saved chart can't draw as saved (for the home list), or ""."""
    if query is None:
        return "Its query was removed."
    group_keys = {item.key for item in query.groups}
    for key, role in ((chart.x, "X-axis"), (chart.series, "colour")):
        if key and key not in group_keys:
            name = chart.names.get(key)
            what = f"“{name}”" if name else "it used"
            return f"The {role} field {what} is no longer in the query."
    if not query.groups or not query.values:
        return "The query has no Group by or no Values."
    return ""


def names_of(query: QuerySpec, plan) -> dict[str, str]:
    """{item key: heading}, saved with a chart so a missing field can be named later."""
    names = {}
    for index, item in enumerate(query.groups):
        column = _column(plan, f"g{index}")
        names[item.key] = column.label if column else item.column
    for index, item in enumerate(query.values):
        column = _column(plan, f"v{index}")
        names[item.key] = column.label if column else item.column
    return names


def _column(plan, key: str):
    return next((column for column in plan.columns if column.key == key), None)


# ---------- the chart ----------


def build_chart(chart: ChartSpec, query: QuerySpec, plan, rows: list[dict]) -> ChartResult:
    """The ECharts option for this chart, or the problems that stop it."""
    out = ChartResult()
    out.notes += fit_to_query(chart, query)
    if chart.chart_type not in CHART_TYPES:
        chart.chart_type = "column"
    if not query.groups or not query.values:
        out.problems.append(
            "A chart needs a field in Group by and one in Values. "
            "Example: Group by Branch, Values Sum of Amount."
        )
        return out

    values = []
    for index, item in enumerate(query.values):
        column = _column(plan, f"v{index}")
        if column is None or item.key in chart.hidden:
            continue
        if kind_class(column.kind) != "number":
            out.notes.append(f"“{column.label}” is not a number, so it is left off the chart.")
            continue
        values.append(
            _Value(
                item.key,
                column.key,
                column.label,
                column.number_format,
                item.agg in ADDITIVE,
                value_style(chart, item.key, index),
            )
        )
    if not values:
        out.problems.append("Tick at least one number value under Chart → Values to draw.")
        return out

    x_key = _group_column(query, chart.x)
    series_key = _group_column(query, chart.series)
    x_column = _column(plan, x_key)
    series_column = _column(plan, series_key) if series_key else None

    if chart.chart_type == "scatter":
        _scatter(chart, values, rows, x_column, series_column, out)
    elif chart.chart_type in ROUND:
        _round(chart, values, rows, x_column, series_column, out)
    else:
        _cartesian(chart, values, rows, x_column, series_column, out)
    if out.option is not None:
        _finish(chart, out.option)
    return out


def _group_column(query: QuerySpec, key: str) -> str:
    for index, item in enumerate(query.groups):
        if item.key == key:
            return f"g{index}"
    return ""


def _number(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal | int | float):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _label(row, column) -> str:
    return format_value(row[column.key], column) if column else ""


def _pivot(values, rows, x_column, series_column, out, quiet=False):
    """{x label: {(series label, value key): number}} in row order, plus the series order.
    Rows that land in the same cell (a 3rd Group by field) are added up, or the first kept."""
    cells: dict[str, dict] = {}
    series_order: list[tuple[str, str]] = []
    seen_series = set()
    merged = False
    for row in rows:
        x_label = _label(row, x_column)
        series_label = _label(row, series_column) if series_column else ""
        bucket = cells.setdefault(x_label, {})
        for value in values:
            slot = (series_label, value.key)
            if slot not in seen_series:
                seen_series.add(slot)
                series_order.append(slot)
            number = _number(row[value.column])
            if slot in bucket:
                merged = True
                if value.additive and number is not None:
                    bucket[slot] = (bucket[slot] or 0) + number
            else:
                bucket[slot] = number
    if merged and not quiet:
        out.notes.append(
            "Group by has more fields than the chart uses, so rows with the same X "
            "(and colour) are added together. Averages and other values keep the first row."
        )
    if series_column:  # colours in value order (months in time order), blank last
        raw = {_label(row, series_column): row[series_column.key] for row in rows}
        position = {value.key: index for index, value in enumerate(values)}
        series_order.sort(
            key=lambda slot: (
                raw[slot[0]] is None,
                raw[slot[0]] if raw[slot[0]] is not None else 0,
                position[slot[1]],
            )
        )
    return cells, series_order


def _others_name(taken) -> str:
    """ "Others", or "Others (rest)" when the data already has an "Others" value."""
    name = OTHERS
    while name in taken:
        name += " (rest)"
    return name


def _rank(bucket: dict, value_key: str) -> float:
    return sum(abs(number or 0) for (_, key), number in bucket.items() if key == value_key)


def _cap_categories(chart, values, cells, out) -> list[str]:
    """Keep the biggest N X values (+ Others), then sort. Returns the X labels to draw."""
    labels = list(cells)
    first = values[0].key
    others_name = ""
    if chart.chart_type in CAPPED:
        default = DEFAULT_SLICES if chart.chart_type in ROUND else DEFAULT_BARS
        top = chart.top_n or default
        if len(labels) > top:
            ranked = sorted(labels, key=lambda label: _rank(cells[label], first), reverse=True)
            keep = set(ranked[:top])
            rest = [label for label in labels if label not in keep]
            labels = [label for label in labels if label in keep]
            if all(value.additive for value in values):
                others: dict = {}
                for label in rest:
                    for slot, number in cells[label].items():
                        if number is not None:
                            others[slot] = (others.get(slot) or 0) + number
                others_name = _others_name(cells)
                cells[others_name] = others
                labels.append(others_name)
                out.notes.append(
                    f"Showing the top {top} of {top + len(rest)}; the other {len(rest)} "
                    f"are added into “{others_name}”. Change “Show top” to see more."
                )
            else:
                out.notes.append(
                    f"Showing the top {top} of {top + len(rest)}. The rest are left out "
                    "(averages and similar values can't be added into one “Others”)."
                )
    elif len(labels) > MAX_POINTS:
        out.problems.append(
            f"This chart would have {len(labels):,} points; the most is {MAX_POINTS:,}. "
            "Group by a bigger date step (e.g. Month instead of Day) or add a filter."
        )
        return []
    if chart.sort:
        tail = [others_name] if others_name else []
        body = labels[:-1] if tail else labels
        body.sort(key=lambda label: _rank(cells[label], first), reverse=chart.sort == "desc")
        labels = body + tail
    return labels


def _cap_series(series_order, cells, labels, values, out):
    """At most MAX_SERIES colours: the biggest ones, the rest into "Others" when they add up."""
    names = list(dict.fromkeys(series for series, _ in series_order))
    if len(names) <= MAX_SERIES:
        return series_order
    totals = {
        name: sum(
            abs(cells[label].get((name, value.key)) or 0) for label in labels for value in values
        )
        for name in names
    }
    keep = set(sorted(names, key=totals.get, reverse=True)[:MAX_SERIES])
    others_name = _others_name(names)
    if all(value.additive for value in values):
        for label in labels:
            bucket = cells[label]
            for (series, key), number in list(bucket.items()):
                if series not in keep and number is not None:
                    slot = (others_name, key)
                    bucket[slot] = (bucket.get(slot) or 0) + number
        out.notes.append(
            f"Showing the {MAX_SERIES} biggest colours; the rest are in “{others_name}”."
        )
        kept = [slot for slot in series_order if slot[0] in keep]
        return kept + [(others_name, value.key) for value in values]
    out.notes.append(f"Showing the {MAX_SERIES} biggest colours; the rest are left out.")
    return [slot for slot in series_order if slot[0] in keep]


def _cartesian(chart, values, rows, x_column, series_column, out):
    cells, series_order = _pivot(values, rows, x_column, series_column, out)
    labels = _cap_categories(chart, values, cells, out)
    if out.problems:
        return
    if series_column:
        series_order = _cap_series(series_order, cells, labels, values, out)
    kind = chart.chart_type
    horizontal = kind in HORIZONTAL
    stacked = kind in STACKED
    percent = stacked and chart.percent
    two_axes = not horizontal and not percent and any(v.style.axis == "right" for v in values)
    by_key = {value.key: value for value in values}

    totals = {}
    if percent:
        for label in labels:
            totals[label] = sum(abs(number or 0) for number in cells[label].values()) or None

    series = []
    for series_label, value_key in series_order:
        value = by_key[value_key]
        data = []
        for label in labels:
            number = cells[label].get((series_label, value_key))
            if percent and number is not None and totals[label]:
                number = round(number / totals[label] * 100, 2)
            data.append(number)
        if series_column:
            name = series_label if len(values) == 1 else f"{series_label} · {value.label}"
        else:
            name = value.label
        draw = "line" if kind in ("line", "area") else "bar"
        if kind == "combo":
            draw = value.style.kind or "bar"
        item = {
            "type": draw,
            "name": name,
            "data": data,
            "_fmt": "pct" if percent else value.number_format,
            "emphasis": {"focus": "series"},
            "label": {
                "show": chart.labels,
                "position": "inside" if stacked else ("right" if horizontal else "top"),
            },
        }
        if series_column:  # the colour's own value, for click-to-filter on a dashboard
            item["_series"] = series_label
        if stacked:
            item["stack"] = "total"
        if kind == "area":
            item["areaStyle"] = {"opacity": 0.35}
        if draw == "line":
            item["symbolSize"] = 6 if len(labels) <= 60 else 0
        if two_axes and value.style.axis == "right":
            item["yAxisIndex"] = 1
        if value.style.color and not series_column:
            item["itemStyle"] = {"color": value.style.color}
        series.append(item)

    left = [v for v in values if not (two_axes and v.style.axis == "right")]
    right = [v for v in values if two_axes and v.style.axis == "right"]
    category_axis = {
        "type": "category",
        "data": labels,
        "name": chart.x_title,
        "nameLocation": "middle",
        "nameGap": 32,
        "axisLabel": {"hideOverlap": True},
    }
    value_axes = [_value_axis(chart.y_title, left, percent)]
    if right:
        value_axes.append(_value_axis(chart.y2_title, right, False))
        value_axes[1]["splitLine"] = {"show": False}
    if horizontal:
        category_axis |= {"inverse": True, "nameGap": 64}
        option = {"xAxis": value_axes[0], "yAxis": category_axis}
    else:
        option = {"xAxis": category_axis, "yAxis": value_axes}
    option |= {
        "series": series,
        "tooltip": {
            "trigger": "axis",
            "axisPointer": {"type": "line" if kind in ("line", "area") else "shadow"},
        },
        "_legend_items": len(series),
    }
    out.option = option


def _value_axis(title: str, values, percent: bool) -> dict:
    axis = {"type": "value", "name": title, "nameLocation": "middle", "nameGap": 56}
    if percent:
        axis |= {"max": 100, "_fmt": "pct"}
    else:
        rupee = values and all(value.number_format.startswith("r") for value in values)
        axis["_fmt"] = "rshort" if rupee else "short"
    return axis


def _round(chart, values, rows, x_column, series_column, out):
    if len(values) > 1:
        names = ", ".join(f"“{value.label}”" for value in values)
        out.problems.append(
            f"A {CHART_TYPES[chart.chart_type][0].lower()} shows one value. "
            f"Untick all but one under Chart → Values (now: {names})."
        )
        return
    if series_column:
        out.notes.append(
            f"A pie has no colours by field, so each slice adds up every “{series_column.label}”."
        )
    value = values[0]
    cells, _ = _pivot(values, rows, x_column, None, out, quiet=bool(series_column))
    labels = _cap_categories(chart, values, cells, out)
    data, negative = [], 0
    for label in labels:
        number = cells[label].get(("", value.key))
        if number is None:
            continue
        if number < 0:
            negative += 1
            continue
        data.append({"name": label, "value": number})
    if negative:
        out.notes.append(f"{negative} below-zero slice{'s' if negative > 1 else ''} left out.")
    if not data:
        out.problems.append("Nothing to draw: every value is blank or below zero.")
        return
    item = {
        "type": "pie",
        "name": value.label,
        "data": data,
        "_fmt": value.number_format,
        "radius": ["42%", "70%"] if chart.chart_type == "donut" else "70%",
        "center": ["50%", "50%"],
        "label": {"show": True, "_pie": "name_pct" if chart.labels else "name"},
        "emphasis": {"label": {"show": True, "fontWeight": "bold"}},
    }
    out.option = {
        "series": [item],
        "tooltip": {"trigger": "item"},
        "_legend_items": len(data),
    }


def _scatter(chart, values, rows, x_column, series_column, out):
    if len(values) < 2:
        out.problems.append(
            "A scatter chart needs two number values: the first goes across (X), the second "
            "up (Y). Example: Values Sum of Qty and Sum of Amount."
        )
        return
    if len(values) > 2:
        out.notes.append(
            f"Only the first two values are used: “{values[0].label}” across, "
            f"“{values[1].label}” up."
        )
    if len(rows) > MAX_POINTS:
        out.problems.append(
            f"This chart would have {len(rows):,} dots; the most is {MAX_POINTS:,}. Add a filter."
        )
        return
    across, up = values[0], values[1]
    groups: dict[str, list] = {}
    for row in rows:
        x, y = _number(row[across.column]), _number(row[up.column])
        if x is None or y is None:
            continue
        name = _label(row, series_column) if series_column else up.label
        groups.setdefault(name, []).append([x, y, _label(row, x_column)])
    series = [
        {
            "type": "scatter",
            "name": name,
            "data": points,
            "symbolSize": 10,
            "_fmt": up.number_format,
            "_xfmt": across.number_format,
            "label": {"show": chart.labels, "position": "top", "_point": True},
            "emphasis": {"focus": "series"},
        }
        for name, points in groups.items()
    ]
    if not series:
        out.problems.append("Nothing to draw: every point has a blank value.")
        return
    out.option = {
        "xAxis": {
            "type": "value",
            "name": chart.x_title or across.label,
            "nameLocation": "middle",
            "nameGap": 32,
            "scale": True,
            "_fmt": "short",
        },
        "yAxis": {
            "type": "value",
            "name": chart.y_title or up.label,
            "nameLocation": "middle",
            "nameGap": 56,
            "scale": True,
            "_fmt": "short",
        },
        "series": series,
        "tooltip": {"trigger": "item", "_scatter": [across.label, up.label]},
        "_legend_items": len(series),
    }


def _finish(chart: ChartSpec, option: dict) -> None:
    """Title, legend, colours and margins: the same for every chart type."""
    items = option.pop("_legend_items", 0)
    show_legend = chart.legend and (items > 1 or chart.chart_type in ROUND)
    top = 16
    if chart.title:
        option["title"] = {"text": chart.title, "left": "center", "textStyle": {"fontSize": 15}}
        top = 48
    if show_legend:
        option["legend"] = {"type": "scroll", "bottom": 0}
    option["grid"] = {
        "left": 24,
        "right": 24,
        "top": top + 16,
        "bottom": 48 if show_legend else 16,
        "containLabel": True,
    }
    colours = THEMES.get(chart.theme, THEMES["default"])[1]
    if colours:
        option["color"] = colours
    option["animationDuration"] = 300
