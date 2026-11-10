"""The query screen: fields → Group by / Values / Filters / Sort boxes → live result table.

Like the link screen, the whole form is posted on every change (htmx) and drawn again by
the server, so the result always matches what the boxes show. Dragging a field into a box
just posts "add_group:Amount" etc., the same as clicking the box's + menu.
"""

import copy
import logging
import re
import secrets

import duckdb
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.html import format_html
from django.views.decorators.http import require_GET, require_http_methods, require_POST
from django_datatable import CHOICE, DATE, NUMBER, TEXT, YES_NO, Column, RowsTable, table_response

from . import querying
from .formula import FUNCTIONS
from .querying import (
    AGGREGATIONS,
    BLANK,
    DATE_PARTS,
    DEFAULT_OP,
    FILTER_OPS,
    NUMBER_FORMATS,
    QueryFailed,
)
from .views import home_after_expiry, page_context, panel_response
from .workspace import (
    FilterItem,
    GroupItem,
    QuerySpec,
    SavedQuery,
    SortItem,
    ValueItem,
    current_workspace,
)

logger = logging.getLogger(__name__)

MAX_ITEMS = 30  # per box
MAX_DRAFTS = 5  # query screens remembered per user (browser tabs)
MAX_TICK_CHOICES = 100  # a result column with more values gets a text filter instead
RESULT_TABLE_ID = "insights-query-result"
KEY_PATTERN = re.compile(r"[a-z0-9]{1,16}")
KIND_ICONS = {
    "integer": ("#", "Whole number"),
    "number": ("#", "Number"),
    "date": ("📅", "Date"),
    "datetime": ("📅", "Date & time"),
    "boolean": ("✓", "Yes / No"),
    "text": ("Aa", "Text"),
}
SIGNS = [
    ("+", "Add"),
    ("-", "Subtract"),
    ("*", "Multiply"),
    ("/", "Divide (÷ by 0 gives a blank)"),
    ("(", "Open bracket"),
    (")", "Close bracket"),
    ("&", 'Join text, e.g. [City] & " - " & [Branch]'),
    ("=", "Equal"),
    ("<>", "Not equal"),
    (">", "Greater than"),
    ("<", "Less than"),
    (">=", "At least"),
    ("<=", "At most"),
    ("AND", "Both must be true"),
    ("OR", "Either can be true"),
    ("NOT", "The opposite"),
]
COMPARE_SIGNS = ["=", "<>", ">", "<", ">=", "<="]


def _new_key() -> str:
    return secrets.token_hex(3)


def _number(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ---------- the screen ----------


@require_http_methods(["GET", "POST"])
def query(request):
    if request.method == "GET" and request.GET.get("draft"):  # a click inside the result table
        return query_table(request, request.GET["draft"])
    workspace, expired = current_workspace(request)
    is_htmx = request.headers.get("HX-Request") == "true"
    if workspace is None or not workspace.datasets:
        target = home_after_expiry() if expired else redirect("my_insights:home")
        if is_htmx:
            return HttpResponse(status=204, headers={"HX-Redirect": target.url})
        return target
    draft_id = (request.POST.get("draft") or "").strip()
    if not KEY_PATTERN.fullmatch(draft_id):
        draft_id = secrets.token_hex(8)

    with workspace.lock:
        formula = {"open": False, "problem": "", "name": "", "text": "", "original": ""}
        name_problem = ""
        notice = ""
        if request.method == "POST":
            spec, name, query_id, action = _read_form(request.POST, workspace)
            dataset = workspace.datasets[spec.dataset_id]
            if action == "save_formula":
                formula = _formula_form(request.POST)
                problem = querying.save_formula(
                    workspace, dataset, formula["name"], formula["text"], formula["original"]
                )
                if problem:
                    formula |= {"open": True, "problem": problem}
                else:
                    notice = f"Saved calculated field “{formula['name']}”."
                    if formula["original"] and formula["original"] != formula["name"]:
                        querying.rename_in_spec(spec, formula["original"], formula["name"])
            elif action.startswith("remove_formula:"):
                removed = action.partition(":")[2]
                dataset.calculated = [c for c in dataset.calculated if c.name != removed]
                notice = f"Removed calculated field “{removed}”."
        else:
            spec, name, query_id = _start_spec(request.GET, workspace)
            dataset = workspace.datasets[spec.dataset_id]
            action = ""

        catalog = querying.field_catalog(dataset)  # built once per request: it reads formulas
        if action and not action.startswith(("save_formula", "remove_formula:")):
            _apply_action(spec, action, catalog)
        plan = querying.plan_query(spec, dataset, catalog=catalog)
        _remember_draft(workspace, draft_id, spec)
        result, table = None, None
        if plan.ok:
            try:
                result = querying.run_query(workspace.connection, plan, spec.top_n)
            except QueryFailed as error:
                plan.problems.append(str(error))
            else:
                rows = _table_rows(plan, result)
                workspace.query_result_cache = (draft_id, copy.deepcopy(spec), plan, rows)
                table = _result_table(request, plan, rows, draft_id)
        suggested_name = querying.default_name(spec, dataset, catalog)

        if action == "save":
            final_name = name or suggested_name
            saved = workspace.queries.get(query_id) if query_id else None
            name_problem = querying.query_name_error(
                workspace, final_name, ignore_id=saved.query_id if saved else None
            )
            if not name_problem and plan.problems:
                name_problem = "Fix the problems above before saving."
            if not name_problem:
                if saved is None:
                    saved = SavedQuery(workspace.new_query_id(), final_name, copy.deepcopy(spec))
                    workspace.queries[saved.query_id] = saved
                else:
                    saved.name, saved.spec = final_name, copy.deepcopy(spec)
                logger.info("Query %s saved", saved.query_id)
                url = reverse("my_insights:query") + f"?open={saved.query_id}&saved=1"
                if is_htmx:
                    return HttpResponse(
                        status=204, headers={"HX-Redirect": url, "HX-Trigger": "query-saved"}
                    )
                return redirect(url)
            logger.info("Query name refused: %s", name_problem)
        if plan.problems:
            logger.info("Query problems shown: %s", plan.problems)

        context = page_context(
            request,
            workspace,
            False,
            **_screen_context(workspace, dataset, spec, catalog, plan, result, table),
            draft_id=draft_id,
            query_id=query_id,
            saved=workspace.queries.get(query_id) if query_id else None,
            name=name,
            suggested_name=suggested_name,
            name_problem=name_problem,
            notice=notice or ("Saved." if request.GET.get("saved") == "1" else ""),
            dirty=request.method == "POST",  # a saved query leaves through the redirect above
            formula=formula,
        )
    if is_htmx:
        return render(request, "my_insights/partials/query_screen.html", context)
    return render(request, "my_insights/query.html", context)


def _start_spec(params, workspace) -> tuple[QuerySpec, str, int | None]:
    saved = workspace.queries.get(_number(params.get("open")))
    if saved is not None and saved.spec.dataset_id in workspace.datasets:
        return copy.deepcopy(saved.spec), saved.name, saved.query_id
    dataset_id = _number(params.get("dataset"))
    if dataset_id not in workspace.datasets:
        dataset_id = next(iter(workspace.datasets))
    return QuerySpec(dataset_id=dataset_id), "", None


def _keys(data, name: str) -> list[str]:
    seen = []
    for key in data.getlist(name)[:MAX_ITEMS]:
        if KEY_PATTERN.fullmatch(key) and key not in seen:
            seen.append(key)
    return seen


def _read_form(data, workspace) -> tuple[QuerySpec, str, int | None, str]:
    """The posted form → a QuerySpec, with the clicked button's change still to apply."""
    dataset_id = _number(data.get("dataset"))
    if dataset_id not in workspace.datasets:
        dataset_id = next(iter(workspace.datasets))
    spec = QuerySpec(dataset_id=dataset_id)
    query_id = _number(data.get("query_id")) or None
    if dataset_id != _number(data.get("previous_dataset"), dataset_id):
        name = " ".join(data.get("name", "").split())
        return spec, name, query_id, ""  # another dataset: start with empty boxes
    for key in _keys(data, "groups"):
        spec.groups.append(
            GroupItem(key, data.get(f"{key}_column", ""), data.get(f"{key}_part", ""))
        )
    for key in _keys(data, "values"):
        spec.values.append(
            ValueItem(
                key,
                data.get(f"{key}_column", ""),
                data.get(f"{key}_agg", ""),
                " ".join(data.get(f"{key}_label", "").split())[:100],
                data.get(f"{key}_format", ""),
            )
        )
    for key in _keys(data, "filters"):
        op = data.get(f"{key}_op", "")
        if op in ("in", "not_in"):
            values = data.getlist(f"{key}_values")[: querying.MAX_TICK_VALUES]
        else:
            values = [data.get(f"{key}_a", ""), data.get(f"{key}_b", "")]
        spec.filters.append(
            FilterItem(
                key,
                data.get(f"{key}_column", ""),
                data.get(f"{key}_value_key", ""),
                op,
                [value[:200] for value in values],
            )
        )
    for key in _keys(data, "sorts"):
        spec.sorts.append(
            SortItem(key, data.get(f"{key}_target", ""), data.get(f"{key}_desc") == "1")
        )
    top_n = _number(data.get("top_n"))
    spec.top_n = top_n if 0 < top_n <= querying.row_cap() else None
    name = " ".join(data.get("name", "").split())
    action = data.get("action") or data.get("drop_action") or ""
    return spec, name, query_id, action


def _formula_form(data) -> dict:
    return {
        "name": " ".join(data.get("formula_name", "").split()),
        "text": data.get("formula_text", "").strip(),
        "original": data.get("formula_original", ""),
        "open": False,
        "problem": "",
    }


def _apply_action(spec: QuerySpec, action: str, catalog: dict) -> None:
    kind, _, target = action.partition(":")
    info = catalog.get(target)
    if kind == "add_group" and info and not info.is_total and len(spec.groups) < MAX_ITEMS:
        part = "month" if info.kind_class == "date" else ""
        if not any(g.column == target and g.date_part == part for g in spec.groups):
            spec.groups.append(GroupItem(_new_key(), target, part))
    elif kind == "add_value" and len(spec.values) < MAX_ITEMS:
        if target == "*":
            spec.values.append(ValueItem(_new_key(), "*", "rows"))
        elif info:
            spec.values.append(ValueItem(_new_key(), target, querying.default_aggregation(info)))
    elif kind == "add_filter" and info and not info.is_total and len(spec.filters) < MAX_ITEMS:
        spec.filters.append(FilterItem(_new_key(), target, "", DEFAULT_OP[info.kind_class]))
    elif kind == "add_total_filter" and any(v.key == target for v in spec.values):
        spec.filters.append(FilterItem(_new_key(), "", target, DEFAULT_OP["total"]))
    elif kind == "add_sort" and len(spec.sorts) < MAX_ITEMS:
        is_value = any(v.key == target for v in spec.values)
        known = is_value or any(g.key == target for g in spec.groups)
        if known and not any(s.target == target for s in spec.sorts):
            spec.sorts.append(SortItem(_new_key(), target, descending=is_value))
    elif kind == "remove":
        spec.groups = [g for g in spec.groups if g.key != target]
        spec.values = [v for v in spec.values if v.key != target]
        spec.filters = [f for f in spec.filters if f.key != target and f.value_key != target]
        spec.sorts = [s for s in spec.sorts if s.key != target and s.target != target]
    elif kind == "clear_filter":  # keep the filter, empty what it tests (ticks, text, dates)
        for item in spec.filters:
            if item.key == target:
                item.values = []
    elif kind == "clear":
        spec.groups, spec.values, spec.filters, spec.sorts, spec.top_n = [], [], [], [], None


def _remember_draft(workspace, draft_id: str, spec: QuerySpec) -> None:
    drafts = workspace.query_drafts
    drafts.pop(draft_id, None)
    drafts[draft_id] = copy.deepcopy(spec)  # newest last
    while len(drafts) > MAX_DRAFTS:
        drafts.pop(next(iter(drafts)))


# ---------- what the template shows ----------


def _screen_context(workspace, dataset, spec, catalog, plan, result, table) -> dict:
    fields = [
        {
            "info": info,
            "icon": "ƒ" if info.calculated else KIND_ICONS[info.kind][0],
            "kind_label": KIND_ICONS[info.kind][1],
            "values_only": info.is_total,
            "remove_confirm": _formula_remove_confirm(workspace, dataset, info.name)
            if info.calculated
            else "",
        }
        for info in catalog.values()
    ]
    group_targets = [(g.key, querying.group_label(g, catalog)) for g in spec.groups]
    value_targets = [(v.key, querying.value_label(v, catalog)) for v in spec.values]
    labels = dict(group_targets + value_targets)
    value_keys = {v.key for v in spec.values}

    groups = []
    for item in spec.groups:
        info = catalog.get(item.column)
        groups.append(
            {
                "item": item,
                "info": info,
                "parts": [(key, label) for key, (label, _) in DATE_PARTS.items()]
                if info and info.kind_class == "date"
                else [],
            }
        )
    values = []
    for item in spec.values:
        info = catalog.get(item.column)
        values.append(
            {
                "item": item,
                "info": info,
                "aggregations": querying.aggregations_for(info)
                if info and not info.is_total
                else [],
                "default_label": querying.value_label(
                    ValueItem(item.key, item.column, item.agg), catalog
                ),
                "number_result": querying.kind_class(querying.value_kind(item, info)) == "number",
            }
        )
    filters = []
    for item in spec.filters:
        info = None if item.value_key else catalog.get(item.column)
        kind = "total" if item.value_key else (info.kind_class if info and not info.error else "")
        view = {
            "item": item,
            "info": info,
            "kind": kind,
            "ops": FILTER_OPS.get(kind, []),
            "label": labels.get(item.value_key, "") if item.value_key else item.column,
            "a": item.values[0] if item.values else "",
            "b": item.values[1] if len(item.values) > 1 else "",
        }
        if kind == "text" and item.op in ("in", "not_in"):
            # Only the ticked values here; the full list loads when the tick list is opened.
            view["choices"] = [
                (value, "(blank)" if value == BLANK else value, None, True) for value in item.values
            ]
            view["picked_count"] = len(item.values)
        filters.append(view)
    sorts = [
        {"item": s, "label": labels.get(s.target, "?"), "is_value": s.target in value_keys}
        for s in spec.sorts
    ]
    sorted_targets = {s.target for s in spec.sorts}
    totals = []
    if result and result.totals is not None:
        for column in plan.columns:
            if column.is_value:
                totals.append(
                    (column.label, querying.format_value(result.totals[column.key], column))
                )
    return {
        "dataset": dataset,
        "spec": spec,
        "fields": fields,
        "groups": groups,
        "values": values,
        "filters": filters,
        "sorts": sorts,
        "sort_options": [t for t in group_targets + value_targets if t[0] not in sorted_targets],
        "value_targets": value_targets,
        "plan": plan,
        "result": result,
        "table": table,
        "totals": totals,
        "row_count": len(result.rows) if result else 0,
        "row_cap": querying.row_cap(),
        "number_formats": NUMBER_FORMATS,
        "functions": [(name, help_text) for name, help_text in FUNCTIONS.items()],
        "signs": SIGNS,
        "compare_signs": COMPARE_SIGNS,
        "aggregation_labels": {key: label for key, (label, _, _) in AGGREGATIONS.items()},
        "all_datasets": list(workspace.datasets.values()),
        "blank": BLANK,
    }


def _formula_remove_confirm(workspace, dataset, name: str) -> str:
    text = f"Remove the calculated field “{name}”?"
    queries = querying.queries_using(workspace, dataset, name)
    if queries:
        text += (
            " Saved queries using it will show a problem: "
            + ", ".join(f"“{q.name}”" for q in queries)
            + "."
        )
    formulas = querying.formulas_using(dataset, name)
    if formulas:
        text += (
            " These fields use it and will stop working: "
            + ", ".join(f"“{f}”" for f in formulas)
            + "."
        )
    return text


def _tick_choices(workspace, dataset, column: str, picked: list[str], search: str = ""):
    """(value, label, rows, ticked) for an "is one of" list: ticked ones first."""
    try:
        options = querying.value_choices(workspace.connection, dataset, column, search)
    except duckdb.Error:
        logger.exception("Could not list the values of %s", column)
        options = []
    picked_set = set(picked)
    listed = {value for value, _, _ in options}
    extra = [
        (value, "(blank)" if value == BLANK else value, None, True)
        for value in picked
        if value not in listed
    ]
    ordered = [(v, label, rows, v in picked_set) for v, label, rows in options]
    ordered.sort(key=lambda option: not option[3])  # ticked first, else most common first
    return extra + ordered


def _table_rows(plan, result) -> list[dict]:
    """Result rows with each cell's shown text added ("t" + key), formatted once."""
    rows = []
    for record in result.rows:
        row = dict(record)
        for column in plan.columns:
            row["t" + column.key] = querying.format_value(record[column.key], column)
        rows.append(row)
    return rows


def _result_table(request, plan, rows: list[dict], draft_id: str) -> RowsTable:
    columns = []
    for column in plan.columns:
        if column.is_value:
            kind = querying.kind_class(column.kind)
            table_type = NUMBER if kind == "number" else DATE if kind == "date" else TEXT
            columns.append(Column(column.key, column.label, type=table_type))
        elif column.date_part == "day" or (column.kind == "date" and not column.date_part):
            columns.append(Column(column.key, column.label, type=DATE))
        elif column.date_part:
            choices = {
                str(r[column.key]): r["t" + column.key] for r in rows if r[column.key] is not None
            }
            columns.append(Column(column.key, column.label, type=CHOICE, choices=choices))
        elif column.kind == "boolean":
            columns.append(Column(column.key, column.label, type=CHOICE, choices=YES_NO))
        elif querying.kind_class(column.kind) == "number":
            columns.append(Column(column.key, column.label, type=NUMBER))
        elif len({row[column.key] for row in rows}) <= MAX_TICK_CHOICES:
            columns.append(Column(column.key, column.label, type=CHOICE))
        else:  # many different values: a "contains" box, not a huge tick list
            columns.append(Column(column.key, column.label, type=TEXT))
    # The package builds its sort / page links as "?sort=…", so they go to the page's own
    # address (the query screen). Every link carries the draft, and query() hands those
    # clicks to query_table(), e.g. /query/?dataset=1 + "?draft=ab12&sort=g0".
    params = request.GET.copy()
    params["draft"] = draft_id
    return RowsTable(
        params,
        rows,
        columns,
        table_id=RESULT_TABLE_ID,
        base_url=reverse("my_insights:query_table", args=[draft_id]),
        keep=("draft",),
        push_url=False,
        search_fields=tuple("t" + column.key for column in plan.columns),
        search_placeholder="Search the result",
        noun="rows",
        default_per_page=25,
        page_sizes=(25, 50, 100, 200),
        column_toggle=True,
    )


# ---------- small endpoints ----------


@require_GET
def query_table(request, draft_id: str):
    """Sort / filter / page clicks inside the result table."""
    workspace, _ = current_workspace(request)
    spec = workspace.query_drafts.get(draft_id) if workspace else None
    expired = (
        '<p class="text-sm text-base-content/60">The result has expired. '
        "Change any setting above to see it again.</p>"
    )
    if spec is None or spec.dataset_id not in workspace.datasets:
        return HttpResponse(expired)
    with workspace.lock:
        cache = workspace.query_result_cache
        if cache and cache[0] == draft_id and cache[1] == spec:  # the rows just shown
            plan, rows = cache[2], cache[3]
        else:
            dataset = workspace.datasets[spec.dataset_id]
            plan = querying.plan_query(copy.deepcopy(spec), dataset)
            if not plan.ok:
                return HttpResponse(expired)
            try:
                result = querying.run_query(workspace.connection, plan, spec.top_n)
            except QueryFailed as error:
                return HttpResponse(format_html('<p class="text-sm text-error">{}</p>', error))
            rows = _table_rows(plan, result)
        table = _result_table(request, plan, rows, draft_id)
    return table_response(
        request,
        table,
        "my_insights/partials/query_result.html",
        "my_insights/partials/query_result.html",
        {"table": table},
    )


@require_POST
def formula_check(request):
    """The live ✓ / ✗ line under the formula box."""
    workspace, _ = current_workspace(request)
    dataset = workspace.datasets.get(_number(request.POST.get("dataset"))) if workspace else None
    form = _formula_form(request.POST)
    check = None
    if dataset is not None and form["text"]:
        with workspace.lock:
            check = querying.check_formula(
                workspace.connection, dataset, form["name"], form["text"], form["original"]
            )
    return render(request, "my_insights/partials/formula_check.html", {"check": check})


@require_GET
def filter_values(request):
    """The "is one of" tick list, searched for any value (not only the 500 most common)."""
    workspace, _ = current_workspace(request)
    dataset = workspace.datasets.get(_number(request.GET.get("dataset"))) if workspace else None
    key = request.GET.get("key", "")
    if dataset is None or not KEY_PATTERN.fullmatch(key):
        return HttpResponse("")
    picked = request.GET.getlist(f"{key}_values")[: querying.MAX_TICK_VALUES]
    with workspace.lock:
        choices = _tick_choices(
            workspace, dataset, request.GET.get("column", ""), picked, request.GET.get("q", "")
        )
    return render(
        request,
        "my_insights/partials/tick_list.html",
        {"choices": choices, "key": key},
    )


@require_POST
def query_remove(request, query_id: int):
    workspace, _ = current_workspace(request)
    if workspace is not None:
        with workspace.lock:
            removed = workspace.queries.pop(query_id, None)
        if removed is not None:
            logger.info("Query %s removed", query_id)
    if request.headers.get("HX-Request") == "true" and request.POST.get("from") == "home":
        return panel_response(request, workspace)
    return redirect("my_insights:home")
