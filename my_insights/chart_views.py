"""Charts on the query screen: the Chart tab's settings, saving and removing charts.

The chart settings are part of the query screen's one form, so a change to a filter or to
the chart type both post the same form and redraw the result and the chart together.
"""

import copy
import logging
import re

from django.shortcuts import redirect
from django.views.decorators.http import require_POST

from . import charting
from .querying import MAX_NAME_LENGTH
from .views import panel_response
from .workspace import ChartSpec, SavedChart, ValueStyle, current_workspace

logger = logging.getLogger(__name__)

COLOR_PATTERN = re.compile(r"#[0-9a-fA-F]{6}")
KEY_PATTERN = re.compile(r"[a-z0-9]{1,16}")
TITLE_LENGTH = 100


def _clean(text: str, length: int = TITLE_LENGTH) -> str:
    return " ".join((text or "").split())[:length]


def read_chart_form(data) -> ChartSpec | None:
    """The Chart settings posted with the query form, or None before the Chart tab was used."""
    if data.get("chart_touched") != "1":
        return None
    chart = ChartSpec()
    chart_type = data.get("chart_type", "")
    chart.chart_type = chart_type if chart_type in charting.CHART_TYPES else "column"
    chart.x = data.get("chart_x", "")
    chart.series = data.get("chart_series", "")
    listed = [key for key in data.getlist("chart_values") if KEY_PATTERN.fullmatch(key)]
    shown = set(data.getlist("chart_shown"))
    chart.hidden = [key for key in listed if key not in shown]
    # A value's bar/line and left/right: the user's own choice ("{key}_cwas", e.g. "line|left")
    # is kept until they change the control. An untouched control shows the drawn type's own
    # pick, which is not kept, so e.g. switching to Combo still puts the 2nd value on the right.
    shown_type = data.get("chart_shown_type", "")
    drawn = ChartSpec(chart_type=shown_type if shown_type in charting.CHART_TYPES else "column")
    for index, key in enumerate(listed):
        was_kind, _, was_axis = data.get(f"{key}_cwas", "").partition("|")
        was_kind = was_kind if was_kind in ("bar", "line") else ""
        was_axis = was_axis if was_axis in ("left", "right") else ""
        default = charting.default_style(drawn, index)
        kind, axis = data.get(f"{key}_ckind", ""), data.get(f"{key}_caxis", "")
        color = data.get(f"{key}_ccolor", "")
        if kind not in ("bar", "line") or kind == (was_kind or default.kind):
            kind = was_kind  # control not changed
        if axis not in ("left", "right") or axis == (was_axis or default.axis):
            axis = was_axis
        chart.styles[key] = ValueStyle(
            kind, axis, color.lower() if COLOR_PATTERN.fullmatch(color) else ""
        )
    chart.title = _clean(data.get("chart_title", ""))
    chart.legend = data.get("chart_legend") == "1"
    chart.labels = data.get("chart_labels") == "1"
    chart.x_title = _clean(data.get("chart_x_title", ""), 60)
    chart.y_title = _clean(data.get("chart_y_title", ""), 60)
    chart.y2_title = _clean(data.get("chart_y2_title", ""), 60)
    theme = data.get("chart_theme", "")
    chart.theme = theme if theme in charting.THEMES else "default"
    sort = data.get("chart_sort", "")
    chart.sort = sort if sort in charting.SORTS else ""
    chart.percent = data.get("chart_percent") == "1"
    try:
        top_n = int(data.get("chart_top_n", ""))
    except ValueError:
        top_n = 0
    chart.top_n = top_n if 0 < top_n <= charting.MAX_TOP else None
    return chart


def chart_name_error(workspace, name: str, ignore_id: int | None = None) -> str:
    if not name:
        return "Type a name for the chart."
    if len(name) > MAX_NAME_LENGTH:
        return f"Keep the name under {MAX_NAME_LENGTH} characters."
    for chart in workspace.charts.values():
        if chart.chart_id != ignore_id and chart.name.casefold() == name.casefold():
            return f"Another chart is already called “{name}”."
    return ""


def suggested_chart_name(chart: ChartSpec, query_name: str) -> str:
    if chart.title:
        return chart.title
    name = f"{charting.CHART_TYPES[chart.chart_type][0]}: {query_name}"
    return name if len(name) <= MAX_NAME_LENGTH else name[: MAX_NAME_LENGTH - 1].rstrip() + "…"


def save_chart(workspace, chart_id, query_id, name, chart, spec, names) -> tuple[SavedChart, str]:
    """Keep the chart. It follows the saved query when the boxes match it; otherwise (a chart
    started on a dataset, or query changes not saved) it keeps its own copy of the question.
    `names`: the headings drawn (charting.names_of). Returns the chart and a note for the
    user ("" when nothing to say)."""
    saved_query = workspace.queries.get(query_id) if query_id else None
    chart = copy.deepcopy(chart)
    chart.names = names
    note = ""
    if saved_query is not None and saved_query.spec == spec:
        link, own = saved_query.query_id, None
    else:
        link, own = None, copy.deepcopy(spec)
        if saved_query is not None:
            note = (
                f"The query “{saved_query.name}” has changes that are not saved, so this chart "
                "keeps its own copy of them. Save the query first to keep the two together."
            )
    existing = workspace.charts.get(chart_id) if chart_id else None
    if existing is None:
        existing = SavedChart(workspace.new_chart_id(), name, chart, link, own)
        workspace.charts[existing.chart_id] = existing
    else:
        existing.name, existing.spec = name, chart
        existing.query_id, existing.own_query = link, own
    logger.info("Chart %s saved (query %s)", existing.chart_id, link)
    return existing, note


def chart_context(
    workspace, chart: ChartSpec, spec, plan, built, chart_id, chart_name, query_id=None
) -> dict:
    """What the Chart tab's settings and drawing need."""
    groups = [(item.key, label) for item, label in _labels(spec.groups, plan, "g")]
    values = []
    position = 0  # the chart colours only the values it draws, in order
    for index, (item, label) in enumerate(_labels(spec.values, plan, "v")):
        shown = item.key not in chart.hidden
        values.append(
            {
                "key": item.key,
                "label": label,
                "shown": shown,
                "style": charting.value_style(chart, item.key, index),
                "own": chart.styles.get(item.key),  # the user's own choices only
                "auto_colour": charting.theme_colour(chart.theme, position),
            }
        )
        position += shown
    saved = workspace.charts.get(chart_id) if chart_id else None
    # The row of chart tabs: a saved query's charts, or (no saved query) the charts on this
    # dataset that keep their own copy of a query.
    if query_id:
        siblings = workspace.charts_of_query(query_id)
        new_chart_url = f"?open={query_id}&tab=chart"
    else:
        siblings = [
            item
            for item in workspace.charts.values()
            if item.own_query is not None and item.own_query.dataset_id == spec.dataset_id
        ]
        new_chart_url = f"?dataset={spec.dataset_id}&tab=chart"
    kind = chart.chart_type
    return {
        "chart": chart,
        "chart_types": [
            (key, label, help_text) for key, (label, help_text) in charting.CHART_TYPES.items()
        ],
        "chart_themes": [(key, label) for key, (label, _) in charting.THEMES.items()],
        "chart_sorts": list(charting.SORTS.items()),
        "chart_groups": groups,
        "chart_series_label": dict(groups).get(chart.series, ""),
        "chart_values": values,
        "chart_built": built,
        "chart_saved": saved,
        "chart_siblings": sorted(siblings, key=lambda item: item.chart_id),
        "new_chart_url": new_chart_url,
        "chart_id": saved.chart_id if saved else None,
        "chart_name": chart_name,
        "chart_stacked": kind in charting.STACKED,
        "chart_capped": kind in charting.CAPPED,
        "chart_round": kind in charting.ROUND,
        "chart_axes": kind not in charting.ROUND,
        "chart_two_axes": kind in ("column", "line", "area", "combo"),
        "chart_default_top": charting.DEFAULT_SLICES
        if kind in charting.ROUND
        else charting.DEFAULT_BARS,
    }


def _labels(items, plan, prefix):
    columns = {column.key: column.label for column in plan.columns}
    return [
        (item, columns.get(f"{prefix}{index}", item.column)) for index, item in enumerate(items)
    ]


@require_POST
def chart_remove(request, chart_id: int):
    workspace, _ = current_workspace(request)
    if workspace is not None:
        with workspace.lock:
            removed = workspace.charts.pop(chart_id, None)
        if removed is not None:
            logger.info("Chart %s removed", chart_id)
    if request.headers.get("HX-Request") == "true" and request.POST.get("from") == "home":
        return panel_response(request, workspace)
    return redirect("my_insights:home")
