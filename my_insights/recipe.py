"""Recipes: a workspace's setup (links, calculated fields, queries, charts, dashboards) as a
small JSON with no data, so it can be saved and used again on new files.

    Nov: sales + items → link, "Margin by city" chart, "Monthly review" dashboard → Save recipe
    Dec: upload sales_dec.csv → Load recipe → match "sales" → sales_dec → Apply

A recipe never holds rows, sample values, API addresses / headers or database details.
Applying is all or nothing: if a column the recipe uses is missing, nothing changes.
"""

import copy
import dataclasses
import difflib
import json
import logging
import re
import secrets
import types
import typing
from datetime import date

from . import charting, conf, linking, query_dataset, querying
from .dashboard import HEIGHTS, TILE_COLORS, TONES, WIDTHS, query_tiles, tile_source
from .formula import FormulaError, compile_formula
from .workspace import (
    CalculatedField,
    Dashboard,
    LinkSpec,
    QuerySpec,
    SavedChart,
    SavedQuery,
    Workspace,
)

logger = logging.getLogger(__name__)

VERSION = 1
MARKER = "my_insights_recipe"
NAME_LENGTH = 100
TILE_KINDS = {"chart", "table", "kpi", "note"}
JOIN_TYPES = {"left", "inner", "full"}
# Settings a recipe brings in are checked like the screens check them: keys and colours end
# up inside page scripts, so anything odd is refused or reset (a recipe may come from anyone).
KEY_PATTERN = re.compile(r"[a-z0-9]{1,16}")
COLOR_PATTERN = re.compile(r"#[0-9a-f]{6}")
AGGREGATIONS = {*querying.AGGREGATIONS, "rows", "formula"}
FILTER_OPS = {op for ops in querying.FILTER_OPS.values() for op, _ in ops}
FIELD_REF = re.compile(r"\[((?:[^\]]|\]\])*)\]")  # [Field name] in a formula ("]]" = "]")


class RecipeError(Exception):
    """The recipe can't be read or applied. The message is shown to the user as it is."""


# ---------- saving ----------


def to_recipe(workspace: Workspace, name: str) -> dict:
    """The workspace's setup as plain JSON values. Holds no data."""
    with workspace.lock:
        ordered = _in_build_order(workspace)
        needs = _needed_columns(workspace, ordered)
        required = _required_sources(workspace)
        datasets = []
        for dataset in ordered:
            entry = {
                "id": dataset.dataset_id,
                "name": dataset.name,
                "calculated": [dataclasses.asdict(c) for c in dataset.calculated],
            }
            if dataset.link is not None:
                columns, _ = linking.plan_link(dataset.link, workspace.datasets)
                entry |= {
                    "kind": "combined",
                    "link": dataclasses.asdict(dataset.link),
                    # [part (0 = main, k = k-th lookup), its column, the combined column]:
                    # a clash is named after the lookup ("Name (items)"), which changes when
                    # the lookup is matched to "items_dec", so names are mapped on apply.
                    "columns": [[c.alias, c.profile.name, c.name] for c in columns],
                }
            elif dataset.query_id is not None:
                entry |= {"kind": "query", "query_id": dataset.query_id}
            else:
                entry |= {
                    "kind": "source",
                    "columns": [{"name": c.name, "kind": c.kind} for c in dataset.columns],
                    "needs": sorted(needs.get(dataset.dataset_id, set())),
                    "required": dataset.dataset_id in required,
                }
            datasets.append(entry)
        return {
            MARKER: VERSION,
            "name": name,
            "saved_on": date.today().isoformat(),
            "datasets": datasets,
            "queries": [
                {"id": q.query_id, "name": q.name, "spec": dataclasses.asdict(q.spec)}
                for q in workspace.queries.values()
            ],
            "charts": [
                {
                    "id": c.chart_id,
                    "name": c.name,
                    "spec": dataclasses.asdict(c.spec),
                    "query_id": c.query_id,
                    "own_query": dataclasses.asdict(c.own_query) if c.own_query else None,
                }
                for c in workspace.charts.values()
            ],
            "dashboards": [
                {
                    "name": d.name,
                    "tiles": [dataclasses.asdict(t) for t in d.tiles],
                    "filters": [dataclasses.asdict(f) for f in d.filters],
                    "next_tile_id": d.next_tile_id,
                }
                for d in workspace.dashboards.values()
            ],
        }


def _in_build_order(workspace: Workspace) -> list:
    """Every dataset, each after the ones it is built from."""
    ordered: dict[int, object] = {}

    def visit(dataset_id: int, path: tuple = ()) -> None:
        if dataset_id in ordered or dataset_id in path or dataset_id not in workspace.datasets:
            return
        dataset = workspace.datasets[dataset_id]
        for source_id in workspace.sources_of(dataset):
            visit(source_id, (*path, dataset_id))
        ordered[dataset_id] = dataset

    for dataset_id in workspace.datasets:
        visit(dataset_id)
    return list(ordered.values())


def query_fields(spec: QuerySpec) -> set[str]:
    """The field names a query asks for (columns or calculated fields)."""
    names = {group.column for group in spec.groups}
    names |= {v.column for v in spec.values if v.agg != "rows" and v.column != "*"}
    names |= {f.column for f in spec.filters if f.column}
    if spec.across is not None:
        names.add(spec.across.column)
    return {name for name in names if name}


def _real_columns(dataset, names: set[str]) -> set[str]:
    """Field names → the real columns they read (a calculated field reads its formula's)."""
    calculated = {calc.name: calc.formula for calc in dataset.calculated}
    fields = querying.formula_fields(dataset)
    columns = set()
    for name in names:
        if name not in calculated:
            columns.add(name)
            continue
        try:
            columns |= compile_formula(calculated[name], fields, (name,)).columns
        except FormulaError:
            continue  # already broken: it shows its own problem
    return columns


def _needed_columns(workspace: Workspace, ordered: list) -> dict[int, set[str]]:
    """{dataset id: the columns the setup reads from it}, traced back from combined datasets
    to the datasets they are built from."""
    used: dict[int, set[str]] = {d.dataset_id: set() for d in ordered}

    def add(dataset_id: int, names: set[str]) -> None:
        if dataset_id in used:
            dataset = workspace.datasets[dataset_id]
            used[dataset_id] |= _real_columns(dataset, names)

    for dataset in ordered:
        add(dataset.dataset_id, {calc.name for calc in dataset.calculated})
    specs = [q.spec for q in workspace.queries.values()]
    specs += [c.own_query for c in workspace.charts.values() if c.own_query is not None]
    for spec in specs:
        add(spec.dataset_id, query_fields(spec))
    for dashboard in workspace.dashboards.values():
        wanted = {f.column for f in dashboard.filters if f.column}
        for dataset in _dashboard_datasets(workspace, dashboard):
            add(dataset.dataset_id, wanted & {c.name for c in dataset.columns})

    for dataset in reversed(ordered):  # combined ones first, then what they are built from
        spec = dataset.link
        if spec is None:
            continue
        for index, step in enumerate(spec.steps, start=1):
            linked_id = (
                spec.main_id if step.linked_to == 0 else spec.steps[step.linked_to - 1].lookup_id
            )
            add(linked_id, {left for left, _ in step.pairs})
            add(step.lookup_id, {right for _, right in step.pairs})
        columns, _ = linking.plan_link(spec, workspace.datasets)
        for column in columns:
            if column.name in used[dataset.dataset_id]:
                owner = (
                    spec.main_id if column.alias == 0 else spec.steps[column.alias - 1].lookup_id
                )
                add(owner, {column.profile.name})
                if column.coalesce_with:
                    alias, name = column.coalesce_with
                    add(spec.steps[alias - 1].lookup_id, {name})
    return used


def _dashboard_datasets(workspace, dashboard) -> list:
    """The datasets the dashboard's tiles (and notes' live values) ask about."""
    found = {}
    for part in [part for tile in dashboard.tiles for part in query_tiles(tile)]:
        dataset = tile_source(workspace, part).dataset
        if dataset is not None:
            found[dataset.dataset_id] = dataset
    return list(found.values())


def _required_sources(workspace: Workspace) -> set[int]:
    """Datasets something in the setup is built on (directly or through others)."""
    roots = {q.spec.dataset_id for q in workspace.queries.values()}
    roots |= {c.own_query.dataset_id for c in workspace.charts.values() if c.own_query}
    roots |= {d.dataset_id for d in workspace.datasets.values() if d.calculated}
    roots |= {d.dataset_id for d in workspace.datasets.values() if d.link or d.query_id}
    required, waiting = set(), list(roots)
    while waiting:
        dataset_id = waiting.pop()
        if dataset_id in required or dataset_id not in workspace.datasets:
            continue
        required.add(dataset_id)
        waiting += workspace.sources_of(workspace.datasets[dataset_id])
    return required


def file_name(name: str) -> str:
    """Monthly review → Monthly_review.json"""
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_") or "recipe"
    return f"{stem[:60]}.json"


# ---------- reading ----------


def read_recipe(raw) -> dict:
    """Bytes / text / dict from a file or the host → a checked recipe. Raises RecipeError."""
    if isinstance(raw, dict):
        data = raw
    else:
        if not isinstance(raw, str | bytes | bytearray):
            logger.error("A recipe came as %s, not text or a dict", type(raw).__name__)
            _refuse("This is not a My Insights recipe.")
        limit = conf.get("RECIPE_MAX_KB") * 1024
        if len(raw) > limit:
            _refuse(
                f"The recipe is larger than {conf.get('RECIPE_MAX_KB')} KB, so it was not read."
            )
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as error:
            _refuse(f"This file is not a My Insights recipe (it is not valid JSON: {error}).")
        except RecursionError:
            _refuse("This file is not a My Insights recipe (it is nested too deeply).")
    if not isinstance(data, dict) or MARKER not in data:
        _refuse("This file is not a My Insights recipe.")
    version = data[MARKER]
    if not isinstance(version, int) or isinstance(version, bool):
        _refuse("This recipe is damaged: its version is not a number.")
    if version > VERSION:
        _refuse(f"This recipe is version {version}. It needs a newer My Insights.")
    try:
        recipe = _check(data)
    except _Bad as error:
        _refuse(f"This recipe is damaged: {error}")
    return recipe


def _refuse(message: str) -> typing.NoReturn:
    logger.info("Recipe refused: %s", message)
    raise RecipeError(message)


class _Bad(Exception):
    pass


def _check(data: dict) -> dict:
    name = _text(data.get("name", "Recipe"), "name") or "Recipe"
    datasets, ids = [], set()
    for at, entry in enumerate(_list(data.get("datasets"), "datasets")):
        where = f"datasets[{at}]"
        entry = _dict(entry, where)
        dataset_id = _int(entry.get("id"), f"{where}.id")
        kind = entry.get("kind")
        checked = {
            "id": dataset_id,
            "name": _text(entry.get("name"), f"{where}.name"),
            "kind": kind,
            "calculated": _build(
                list[CalculatedField], entry.get("calculated", []), f"{where}.calculated"
            ),
        }
        if kind == "source":
            columns = _list(entry.get("columns", []), f"{where}.columns")
            checked["columns"] = [
                _text(_dict(c, where).get("name"), f"{where}.columns") for c in columns
            ]
            checked["kinds"] = {
                c["name"]: c["kind"] for c in columns if isinstance(c.get("kind"), str)
            }
            checked["needs"] = [
                _text(n, f"{where}.needs") for n in _list(entry.get("needs", []), f"{where}.needs")
            ]
            checked["required"] = bool(entry.get("required", True))
        elif kind == "combined":
            link = _build(LinkSpec, entry.get("link"), f"{where}.link")
            for step in link.steps:
                if step.join_type not in JOIN_TYPES:
                    raise _Bad(f"{where}.link has an unknown join type “{step.join_type}”.")
            if not all(id_ in ids for id_ in link.source_ids()):
                raise _Bad(f"{where}.link uses a dataset listed after it.")
            checked["link"] = link
            checked["origins"] = {}  # (part, its column) → the combined column
            for item in _list(entry.get("columns", []), f"{where}.columns"):
                part, column, output = _build(tuple[int, str, str], item, f"{where}.columns")
                checked["origins"][(part, column)] = output
        elif kind == "query":
            checked["query_id"] = _int(entry.get("query_id"), f"{where}.query_id")
        else:
            raise _Bad(f"{where}.kind must be source, combined or query.")
        if dataset_id in ids:
            raise _Bad(f"{where}.id {dataset_id} is used twice.")
        ids.add(dataset_id)
        datasets.append(checked)

    queries = {}
    for at, entry in enumerate(_list(data.get("queries", []), "queries")):
        where = f"queries[{at}]"
        entry = _dict(entry, where)
        query = SavedQuery(
            _int(entry.get("id"), f"{where}.id"),
            _text(entry.get("name"), f"{where}.name"),
            _clean_query(_build(QuerySpec, entry.get("spec"), f"{where}.spec"), where),
        )
        if query.spec.dataset_id not in ids:
            raise _Bad(f"{where} asks about a dataset that is not in the recipe.")
        queries[query.query_id] = query
    for checked in datasets:
        if checked["kind"] == "query" and checked["query_id"] not in queries:
            raise _Bad(f"“{checked['name']}” is made from a query that is not in the recipe.")

    charts = {}
    for at, entry in enumerate(_list(data.get("charts", []), "charts")):
        where = f"charts[{at}]"
        chart = _build(SavedChart, _dict(entry, where) | {"chart_id": entry.get("id")}, where)
        _clean_chart(chart.spec, where)
        if chart.own_query is not None:
            _clean_query(chart.own_query, where)
        if (chart.query_id is None) == (chart.own_query is None):
            raise _Bad(f"{where} must follow one saved query or have its own.")
        if chart.query_id is not None and chart.query_id not in queries:
            raise _Bad(f"{where} follows a query that is not in the recipe.")
        if chart.own_query is not None and chart.own_query.dataset_id not in ids:
            raise _Bad(f"{where} asks about a dataset that is not in the recipe.")
        charts[chart.chart_id] = chart

    dashboards = []
    for at, entry in enumerate(_list(data.get("dashboards", []), "dashboards")):
        where = f"dashboards[{at}]"
        dashboard = _build(Dashboard, _dict(entry, where) | {"dashboard_id": 0}, where)
        for tile in dashboard.tiles:
            if tile.kind not in TILE_KINDS:
                raise _Bad(f"{where} has a tile of unknown kind “{tile.kind}”.")
            _key(tile.value_key, where, empty=True)
            for value in tile.values:
                _key(value.value_key, where)
            tile.width = tile.width if tile.width in WIDTHS else "half"
            tile.height = tile.height if tile.height in HEIGHTS else "short"
            tile.color = tile.color if tile.color in TILE_COLORS else ""
            tile.tone = tile.tone if tile.tone in TONES else "soft"
            if tile.chart_id is not None and tile.chart_id not in charts:
                raise _Bad(f"{where} shows a chart that is not in the recipe.")
            used = [tile.query_id, *(value.query_id for value in tile.values)]
            if any(q is not None and q not in queries for q in used):
                raise _Bad(f"{where} shows a query that is not in the recipe.")
        for item in dashboard.filters:
            _clean_filter(item, where)
        dashboard.next_tile_id = max(
            [dashboard.next_tile_id, *(t.tile_id + 1 for t in dashboard.tiles)]
        )
        dashboards.append(dashboard)
    return {
        "token": secrets.token_hex(8),  # the match form sends it back: still this recipe?
        "name": name,
        "datasets": datasets,
        "queries": queries,
        "charts": charts,
        "dashboards": dashboards,
    }


def _key(value: str, where: str, empty: bool = False) -> str:
    if not (empty and value == "") and not KEY_PATTERN.fullmatch(value):
        raise _Bad(f"{where} has a bad field key “{value[:20]}”.")
    return value


def _clean_filter(item, where: str) -> None:
    _key(item.key, where)
    _key(item.value_key, where, empty=True)
    item.op = item.op if item.op in FILTER_OPS else ""


def _clean_query(spec: QuerySpec, where: str) -> QuerySpec:
    """Keys must look like the screens' own (g1, v2…); unknown choices go back to defaults."""
    for group in [*spec.groups, *([spec.across] if spec.across else [])]:
        _key(group.key, where)
        group.date_part = group.date_part if group.date_part in querying.DATE_PARTS else ""
    for value in spec.values:
        _key(value.key, where)
        value.agg = value.agg if value.agg in AGGREGATIONS else "count"
        if value.number_format not in querying.FORMAT_KEYS:
            value.number_format = ""
    for item in spec.filters:
        _clean_filter(item, where)
    for item in spec.sorts:
        _key(item.key, where)
        _key(item.target, where)
    if spec.top_n is not None and spec.top_n < 1:
        spec.top_n = None
    return spec


def _clean_chart(chart, where: str) -> None:
    chart.chart_type = chart.chart_type if chart.chart_type in charting.CHART_TYPES else "column"
    chart.theme = chart.theme if chart.theme in charting.THEMES else "default"
    chart.sort = chart.sort if chart.sort in charting.SORTS else ""
    if chart.top_n is not None and not 0 < chart.top_n <= charting.MAX_TOP:
        chart.top_n = None
    for key in [chart.x, chart.series, *chart.hidden, *chart.names]:
        _key(key, where, empty=True)
    for key, style in chart.styles.items():
        _key(key, where)
        style.kind = style.kind if style.kind in ("bar", "line") else ""
        style.axis = style.axis if style.axis in ("left", "right") else ""
        color = style.color.lower()
        style.color = color if COLOR_PATTERN.fullmatch(color) else ""


def _text(value, where: str) -> str:
    if not isinstance(value, str):
        raise _Bad(f"{where} must be text.")
    return value[:NAME_LENGTH] if where.endswith("name") else value


def _int(value, where: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise _Bad(f"{where} must be a whole number.")
    return value


def _list(value, where: str) -> list:
    if not isinstance(value, list | tuple):  # a tuple: a recipe dict straight from to_recipe
        raise _Bad(f"{where} must be a list.")
    return value


def _dict(value, where: str) -> dict:
    if not isinstance(value, dict):
        raise _Bad(f"{where} must be an object.")
    return value


def _build(kind, value, where: str):
    """A JSON value → the type `kind` (a dataclass, list[…], dict[str, …], X | None…),
    checking every field. Unknown keys are ignored; missing ones take their default."""
    origin, args = typing.get_origin(kind), typing.get_args(kind)
    if origin in (typing.Union, types.UnionType):
        if value is None and type(None) in args:
            return None
        return _build(next(a for a in args if a is not type(None)), value, where)
    if origin is list:
        return [
            _build(args[0], item, f"{where}[{at}]") for at, item in enumerate(_list(value, where))
        ]
    if origin is tuple:
        items = _list(value, where)
        if len(items) != len(args):
            raise _Bad(f"{where} must have {len(args)} parts.")
        return tuple(_build(a, item, where) for a, item in zip(args, items, strict=True))
    if origin is dict:
        return {
            _text(k, where): _build(args[1], v, f"{where}.{k}")
            for k, v in _dict(value, where).items()
        }
    if dataclasses.is_dataclass(kind):
        value = _dict(value, where)
        hints = typing.get_type_hints(kind)
        settings = {}
        for item in dataclasses.fields(kind):
            if item.name in value:
                settings[item.name] = _build(
                    hints[item.name], value[item.name], f"{where}.{item.name}"
                )
            elif (
                item.default is dataclasses.MISSING and item.default_factory is dataclasses.MISSING
            ):
                raise _Bad(f"{where}.{item.name} is missing.")
        return kind(**settings)
    if kind is bool:
        if not isinstance(value, bool):
            raise _Bad(f"{where} must be true or false.")
        return value
    if kind is int:
        return _int(value, where)
    if kind is str:
        return _text(value, where)
    raise _Bad(f"{where} has an unexpected type.")  # never expected: a new field type


# ---------- matching ----------


def candidates(workspace: Workspace) -> list:
    """Loaded datasets a recipe dataset can be matched to: files, API / database tables
    (not combined ones or query results, which the recipe builds again)."""
    return [d for d in workspace.datasets.values() if d.link is None and d.query_id is None]


def sources(recipe: dict) -> list[dict]:
    return [entry for entry in recipe["datasets"] if entry["kind"] == "source"]


def needed_columns(entry: dict) -> list[tuple[str, str]]:
    """The columns a file must have for this recipe dataset, in the file's own order, with
    their type in words: [("Date", "a date"), ("Amount", "a whole number")]. Others are ignored."""
    order = {name: place for place, name in enumerate(entry["columns"])}
    names = sorted(entry["needs"], key=lambda name: order.get(name, len(order)))
    kinds = entry["kinds"]
    return [(name, querying.KIND_WORDS.get(kinds.get(name, ""), "any type")) for name in names]


def _plain(name: str) -> str:
    """ "Sales (2)" / "sales.csv" → "sales" """
    name = re.sub(r"\s*\(\d+\)$", "", name.casefold())
    name = re.sub(r"\.(csv|xlsx|xlsm|json|jsonl)$", "", name)
    return re.sub(r"[^a-z0-9]+", "", name)


def guess_matches(recipe: dict, workspace: Workspace) -> dict[int, int | None]:
    """{recipe dataset id: loaded dataset id or None}. Same name first, then a similar name
    with every needed column (e.g. "sales" → "sales_dec"). Each dataset is used once."""
    loaded = candidates(workspace)
    matches: dict[int, int | None] = {entry["id"]: None for entry in sources(recipe)}
    used: set[int] = set()
    tests = [
        lambda entry, d: d.name.casefold() == entry["name"].casefold(),
        lambda entry, d: _plain(d.name) == _plain(entry["name"]),
    ]
    for test in tests:
        for entry in sources(recipe):
            if matches[entry["id"]] is None:
                found = next(
                    (d for d in loaded if d.dataset_id not in used and test(entry, d)), None
                )
                if found is not None:
                    matches[entry["id"]] = found.dataset_id
                    used.add(found.dataset_id)
    for entry in sources(recipe):
        if matches[entry["id"]] is not None:
            continue
        fitting = [
            d
            for d in loaded
            if d.dataset_id not in used
            and set(entry["needs"]) <= {c.name for c in d.columns}
            and _likeness(entry, d) >= 0.5
        ]
        if fitting:
            best = max(fitting, key=lambda d: _likeness(entry, d))
            matches[entry["id"]] = best.dataset_id
            used.add(best.dataset_id)
    return matches


def _likeness(entry: dict, dataset) -> float:
    """0..1: how alike the names are, or the column lists, whichever is higher."""
    names = difflib.SequenceMatcher(None, _plain(entry["name"]), _plain(dataset.name)).ratio()
    theirs, ours = set(entry["columns"]), {c.name for c in dataset.columns}
    columns = len(theirs & ours) / len(theirs | ours) if theirs | ours else 0
    return max(names, columns)


def match_problems(recipe: dict, workspace: Workspace, matches: dict) -> dict[int, str]:
    """{recipe dataset id: why it can't be used yet}. Empty = ready to apply."""
    problems, picked = {}, {}
    for entry in sources(recipe):
        dataset = workspace.datasets.get(matches.get(entry["id"]) or 0)
        loaded = {d.dataset_id for d in candidates(workspace)}
        if dataset is None or dataset.dataset_id not in loaded:
            if entry["required"]:
                problems[entry["id"]] = "Pick a dataset: the recipe needs it."
            continue
        if dataset.dataset_id in picked:
            problems[entry["id"]] = (
                f"“{dataset.name}” is already picked for “{picked[dataset.dataset_id]}”."
            )
            continue
        picked[dataset.dataset_id] = entry["name"]
        have = {c.name for c in dataset.columns}
        missing = [name for name in entry["needs"] if name not in have]
        if missing:
            problems[entry["id"]] = f"“{dataset.name}” is missing: {', '.join(missing)}."
            continue
        changed = _kind_changes(entry, dataset)
        if changed:
            problems[entry["id"]] = f"In “{dataset.name}”: {'; '.join(changed)}."
    return problems


def _kind_changes(entry: dict, dataset) -> list[str]:
    """Needed columns that now hold another type, e.g. Date read as text this time."""
    kinds = {c.name: c.kind for c in dataset.columns}
    words = querying.KIND_WORDS
    changed = []
    for name in entry["needs"]:
        was, now = entry["kinds"].get(name), kinds[name]
        if was and querying.kind_class(was) != querying.kind_class(now):
            changed.append(f"“{name}” is now {words.get(now, now)} (was {words.get(was, was)})")
    return changed


# ---------- applying ----------


def apply_recipe(workspace: Workspace, recipe: dict, matches: dict) -> list[str]:
    """Replace the workspace's setup with the recipe's, on the matched datasets. All or
    nothing: raises RecipeError and leaves everything as it was. Returns report lines."""
    with workspace.lock:
        problems = match_problems(recipe, workspace, matches)
        if problems:
            _refuse(" ".join(problems.values()))
        built = _Built(workspace)
        try:
            _build_all(workspace, recipe, matches, built)
            _check_built(workspace, built)
        except RecipeError:
            built.undo()
            raise
        except (linking.LinkRejected, query_dataset.QueryDatasetFailed) as error:
            built.undo()
            _refuse(f"The recipe could not be applied: {error}")
        except Exception:
            built.undo()
            logger.exception("Applying a recipe failed")
            raise
        built.keep()
    made = ", ".join(
        [
            _count(len(built.queries), "query", "queries"),
            _count(len(built.charts), "chart"),
            _count(len(built.dashboards), "dashboard"),
        ]
    )
    lines = [f"Applied the recipe “{recipe['name']}”: {made}."]
    skipped = [e["name"] for e in sources(recipe) if not matches.get(e["id"])]
    if skipped:
        lines.append(f"Not matched (nothing used them): {', '.join(skipped)}.")
    logger.info("Recipe applied: %s", lines[0])
    return lines


def _count(number: int, word: str, plural: str = "") -> str:
    return f"{number} {word if number == 1 else plural or word + 's'}"


class _Built:
    """What applying added so far, so a failure can take it all out again."""

    def __init__(self, workspace: Workspace):
        self.workspace = workspace
        self.old_datasets = [
            d.dataset_id for d in workspace.datasets.values() if d.link or d.query_id
        ]
        self.old_queries = list(workspace.queries)
        self.old_charts = list(workspace.charts)
        self.old_dashboards = list(workspace.dashboards)
        self.old_calculated = {d.dataset_id: d.calculated for d in workspace.datasets.values()}
        self.datasets: dict[int, str] = {}  # new id → the name it should end with
        self.sources: list[int] = []  # matched datasets that got the recipe's calculated fields
        self.queries: list[int] = []
        self.charts: list[int] = []
        self.dashboards: list[int] = []

    def undo(self) -> None:
        workspace = self.workspace
        for dataset_id in reversed(list(self.datasets)):
            if dataset_id in workspace.datasets:
                workspace.remove_dataset(dataset_id)
        for query_id in self.queries:
            workspace.queries.pop(query_id, None)
        for chart_id in self.charts:
            workspace.charts.pop(chart_id, None)
        for dashboard_id in self.dashboards:
            workspace.dashboards.pop(dashboard_id, None)
        for dataset_id, calculated in self.old_calculated.items():
            if dataset_id in workspace.datasets:
                workspace.datasets[dataset_id].calculated = calculated
        workspace.refresh_memory()

    def keep(self) -> None:
        """The new setup works: take out the old one."""
        workspace = self.workspace
        for dataset_id in self.old_datasets:
            if dataset_id in workspace.datasets:
                workspace.remove_dataset(dataset_id)
        for query_id in self.old_queries:
            if query_id in workspace.queries:
                workspace.remove_query(query_id)
        for chart_id in self.old_charts:
            workspace.charts.pop(chart_id, None)
        for dashboard_id in self.old_dashboards:
            workspace.dashboards.pop(dashboard_id, None)
        for dataset_id, name in self.datasets.items():
            dataset = workspace.datasets[dataset_id]
            dataset.name = workspace.unique_name(name, ignore_id=dataset_id)
        workspace.query_drafts.clear()
        workspace.link_drafts.clear()
        workspace.query_result_cache = None
        workspace.suggestion_cache = ((), [])
        workspace.refresh_memory()


def _build_all(workspace: Workspace, recipe: dict, matches: dict, built: _Built) -> None:
    dataset_ids = {rid: wid for rid, wid in matches.items() if wid}
    query_ids: dict[int, int] = {}
    # {recipe dataset id: {old column name: new}}: combined columns named after a lookup.
    renames: dict[int, dict[str, str]] = {}

    def add_query(query: SavedQuery) -> None:
        spec = _with_dataset(_renamed_spec(query.spec, renames), dataset_ids)
        new = SavedQuery(workspace.new_query_id(), query.name, spec)
        workspace.queries[new.query_id] = new
        query_ids[query.query_id] = new.query_id
        built.queries.append(new.query_id)

    for entry in recipe["datasets"]:
        calculated = [dataclasses.replace(c) for c in entry["calculated"]]
        if entry["kind"] == "source":
            if entry["id"] in dataset_ids:
                workspace.datasets[dataset_ids[entry["id"]]].calculated = calculated
                built.sources.append(dataset_ids[entry["id"]])
            continue
        if entry["kind"] == "combined":
            link = _renamed_link(entry["link"], renames)
            spec = _mapped_link(link, dataset_ids)
            made = linking.save_link(workspace, spec, workspace.unique_name(entry["name"]))
            renames[entry["id"]] = _moved_columns(entry, link, made, workspace, renames)
            calculated = [
                CalculatedField(c.name, _renamed_formula(c.formula, renames[entry["id"]]))
                for c in calculated
            ]
        else:
            add_query(recipe["queries"][entry["query_id"]])
            made = query_dataset.make_query_dataset(workspace, query_ids[entry["query_id"]])
        built.datasets[made.dataset_id] = entry["name"]
        made.calculated = calculated
        dataset_ids[entry["id"]] = made.dataset_id

    for query in recipe["queries"].values():
        if query.query_id not in query_ids:
            add_query(query)
    chart_ids = {}
    for chart in recipe["charts"].values():
        new = SavedChart(
            workspace.new_chart_id(),
            chart.name,
            dataclasses.replace(chart.spec),
            query_ids.get(chart.query_id) if chart.query_id is not None else None,
            _with_dataset(_renamed_spec(chart.own_query, renames), dataset_ids)
            if chart.own_query
            else None,
        )
        workspace.charts[new.chart_id] = new
        chart_ids[chart.chart_id] = new.chart_id
        built.charts.append(new.chart_id)
    for dashboard in recipe["dashboards"]:
        new = _copy(dashboard)
        new.dashboard_id = workspace.new_dashboard_id()
        everywhere = {old: name for moved in renames.values() for old, name in moved.items()}
        for item in new.filters:
            item.column = everywhere.get(item.column, item.column)
        for tile in new.tiles:
            tile.chart_id = chart_ids.get(tile.chart_id) if tile.chart_id is not None else None
            tile.query_id = query_ids.get(tile.query_id) if tile.query_id is not None else None
            for value in tile.values:
                value.query_id = query_ids[value.query_id]
        workspace.dashboards[new.dashboard_id] = new
        built.dashboards.append(new.dashboard_id)


def _moved_columns(entry: dict, link: LinkSpec, made, workspace, renames: dict) -> dict:
    """{old combined column: new} where the combined dataset's columns got other names."""
    columns, _ = linking.plan_link(made.link, workspace.datasets)
    now = {(c.alias, c.profile.name): c.name for c in columns}
    parts = [link.main_id, *(step.lookup_id for step in link.steps)]
    moved = {}
    for (part, column), old in entry["origins"].items():
        if 0 <= part < len(parts):
            new = now.get((part, renames.get(parts[part], {}).get(column, column)))
            if new is not None and new != old:
                moved[old] = new
    return moved


def _renamed_spec(spec: QuerySpec, renames: dict) -> QuerySpec:
    moved = renames.get(spec.dataset_id, {})
    spec = _copy(spec)
    for item in querying.spec_items(spec):
        item.column = moved.get(item.column, item.column)
    return spec


def _renamed_link(link: LinkSpec, renames: dict) -> LinkSpec:
    link = _copy(link)
    for step in link.steps:
        linked = link.main_id if step.linked_to == 0 else link.steps[step.linked_to - 1].lookup_id
        left, right = renames.get(linked, {}), renames.get(step.lookup_id, {})
        step.pairs = [(left.get(a, a), right.get(b, b)) for a, b in step.pairs]
    return link


def _renamed_formula(formula: str, moved: dict) -> str:
    """[Name (items)] → [Name (items_dec)], all names in one pass so swaps can't mix."""
    if not moved:
        return formula

    def swap(match) -> str:
        name = match.group(1).replace("]]", "]")
        return "[" + moved.get(name, name).replace("]", "]]") + "]"

    return FIELD_REF.sub(swap, formula)


def _copy(item):
    return copy.deepcopy(item)


def _with_dataset(spec: QuerySpec, dataset_ids: dict) -> QuerySpec:
    spec = _copy(spec)
    if spec.dataset_id not in dataset_ids:
        _refuse("The recipe asks about a dataset that was not matched. Pick one for it.")
    spec.dataset_id = dataset_ids[spec.dataset_id]
    return spec


def _mapped_link(link: LinkSpec, dataset_ids: dict) -> LinkSpec:
    link = _copy(link)
    if any(id_ not in dataset_ids for id_ in link.source_ids()):
        _refuse("A combined dataset in the recipe uses a dataset that was not matched.")
    link.main_id = dataset_ids[link.main_id]
    for step in link.steps:
        step.lookup_id = dataset_ids[step.lookup_id]
    return link


def _check_built(workspace: Workspace, built: _Built) -> None:
    """Every new query and chart must work on the matched data, so no wrong report is shown."""
    problems = []
    names = {d: workspace.datasets[d].name for d in built.sources} | built.datasets
    for dataset_id, name in names.items():
        for info in querying.field_catalog(workspace.datasets[dataset_id]).values():
            if info.error:
                problems.append(f"Calculated field “{info.name}” in “{name}”: {info.error}")
    specs = [(workspace.queries[q].name, workspace.queries[q].spec) for q in built.queries]
    specs += [
        (workspace.charts[c].name, workspace.charts[c].own_query)
        for c in built.charts
        if workspace.charts[c].own_query is not None
    ]
    for name, spec in specs:
        dataset = workspace.datasets[spec.dataset_id]
        plan = querying.plan_query(_copy(spec), dataset)
        problems += [f"“{name}”: {problem}" for problem in plan.problems]
    if problems:
        _refuse("The recipe does not fit this data, so nothing was changed. " + " ".join(problems))
