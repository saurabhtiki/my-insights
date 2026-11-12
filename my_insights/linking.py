"""Link datasets: a main dataset (e.g. Sales) plus lookup lists (Items, Customers…) become one
combined dataset. It is saved as a DuckDB view, a stored query, so it holds no copy of the data.

Rule that keeps totals right: in every lookup list the link column(s) must be unique, so each
main row finds at most one match. The combined dataset then never repeats a main row.
"""

import dataclasses
import logging
import re
from dataclasses import dataclass, field

import duckdb

from . import conf
from .loading import fill_samples, quote_name
from .workspace import (
    PARENT_COLUMN,
    ROW_COLUMN,
    ColumnProfile,
    Dataset,
    LinkSpec,
    LinkStep,
    Workspace,
)

logger = logging.getLogger(__name__)

JOIN_TYPES = {
    "left": (
        "LEFT JOIN",
        "Keep all main rows",
        "Every main row stays. If no match is found, the lookup columns are blank.",
    ),
    "inner": (
        "INNER JOIN",
        "Only matches",
        "Main rows without a match in this lookup list are left out.",
    ),
    "full": (
        "FULL JOIN",
        "Keep everything",
        "Every main row stays, and lookup rows nobody matched are added too (main columns blank).",
    ),
}
# Kinds that can be compared directly. Any other mix is compared as text.
COMPARE_GROUPS = {
    "integer": "number",
    "number": "number",
    "date": "date",
    "datetime": "date",
    "boolean": "boolean",
    "text": "text",
}
SUGGEST_MIN_PERCENT = 30
MAX_NAME_LENGTH = 100
SUGGEST_KINDS = {"integer", "text"}  # codes and ids; decimals and dates rarely link tables


class LinkRejected(Exception):
    """The link can't be saved. The message is shown to the user as it is."""


@dataclass
class OutputColumn:
    """One column of the combined dataset and where it comes from."""

    name: str
    alias: int  # 0 = main, k = k-th lookup
    profile: ColumnProfile
    coalesce_with: tuple[int, str] | None = None  # "keep everything": take the lookup key if blank


@dataclass
class StepCheck:
    total: int = 0  # rows in the dataset this lookup is linked to
    blank: int = 0  # of those, rows with a blank link value
    matched: int = 0  # of those, rows that find a match
    duplicate_count: int = 0  # link values listed more than once in the lookup list
    duplicate_examples: list[str] = field(default_factory=list)

    @property
    def filled(self) -> int:
        return self.total - self.blank

    @property
    def match_percent(self) -> int:
        """Share of rows with a link value that find a match (blanks never can)."""
        return round(100 * self.matched / self.filled) if self.filled else 0


@dataclass
class LinkCheck:
    problems: list[str] = field(default_factory=list)  # any problem blocks saving
    steps: list[StepCheck | None] = field(default_factory=list)
    columns: list[OutputColumn] = field(default_factory=list)
    row_count: int | None = None
    sql: str = ""

    @property
    def ok(self) -> bool:
        return not self.problems and bool(self.sql)


@dataclass
class Suggestion:
    main_id: int
    lookup_id: int
    main_column: str
    lookup_column: str
    matched: int
    total: int
    from_database: bool = False  # a foreign key in the uploaded SQLite file

    @property
    def match_percent(self) -> int:
        return round(100 * self.matched / self.total) if self.total else 0


# ---------- building the SQL ----------


def _table_of(spec: LinkSpec, datasets: dict[int, Dataset], alias: int) -> Dataset:
    return datasets[spec.main_id if alias == 0 else spec.steps[alias - 1].lookup_id]


def _column(dataset: Dataset, name: str) -> ColumnProfile | None:
    return next((column for column in dataset.columns if column.name == name), None)


def _key_sql(alias: int, column: ColumnProfile, other: ColumnProfile, ignore_case: bool) -> str:
    """SQL for one side of a link condition. Different kinds are compared as text."""
    group, other_group = COMPARE_GROUPS[column.kind], COMPARE_GROUPS[other.kind]
    expression = f"t{alias}.{quote_name(column.name)}"
    if group == other_group and group != "text":
        return expression
    if group != other_group:
        expression = f"CAST({expression} AS VARCHAR)"
    return f"lower({expression})" if ignore_case else expression


def _wider_kind(first: str, second: str) -> str:
    if first == second:
        return first
    return "number" if COMPARE_GROUPS[first] == "number" else "datetime"


def _unique_name(wanted: str, label: str, taken: set[str]) -> str:
    name, number = wanted, 2
    if name.casefold() in taken:
        name = f"{wanted} ({label})"
    while name.casefold() in taken:
        name = f"{wanted} ({label} {number})"
        number += 1
    taken.add(name.casefold())
    return name


def plan_link(spec: LinkSpec, datasets: dict[int, Dataset]) -> tuple[list[OutputColumn], list[str]]:
    """The combined dataset's columns, plus problems in the settings themselves (missing
    dataset, no link columns…). Needs no database."""
    problems: list[str] = []
    if spec.main_id not in datasets:
        return [], ["Choose the main dataset."]
    if not spec.steps:
        return [], ["Add at least one lookup list."]
    main = datasets[spec.main_id]
    columns = [OutputColumn(column.name, 0, column) for column in main.columns]
    taken = {column.name.casefold() for column in main.columns}

    for index, step in enumerate(spec.steps, start=1):
        if step.lookup_id not in datasets:
            problems.append(f"Lookup {index}: choose a dataset.")
            continue
        if not 0 <= step.linked_to < index:
            problems.append(f"Lookup {index}: choose what it links to.")
            continue
        lookup = datasets[step.lookup_id]
        linked = _table_of(spec, datasets, step.linked_to)
        if not step.pairs:
            problems.append(f"“{lookup.name}”: choose the column(s) that link it.")
            continue
        skip: set[str] = set()
        for left_name, right_name in step.pairs:
            if not left_name or not right_name:
                problems.append(f"“{lookup.name}”: choose both link columns.")
                continue
            left, right = _column(linked, left_name), _column(lookup, right_name)
            if left is None or right is None:
                missing = left_name if left is None else right_name
                owner = linked if left is None else lookup
                problems.append(f"“{owner.name}” has no column “{missing}”.")
                continue
            if step.join_type == "full":
                shown = next(
                    (
                        c
                        for c in columns
                        if c.alias == step.linked_to and c.profile.name == left_name
                    ),
                    None,
                )
                if shown is None or COMPARE_GROUPS[left.kind] != COMPARE_GROUPS[right.kind]:
                    continue  # keep the lookup's own key column, so unmatched rows show it
                shown.coalesce_with = (index, right_name)
                shown.profile = dataclasses.replace(
                    shown.profile, kind=_wider_kind(left.kind, right.kind)
                )
            skip.add(right_name)  # the link column appears once
        for column in lookup.columns:
            if column.name not in skip:
                name = _unique_name(column.name, lookup.name, taken)
                columns.append(OutputColumn(name, index, column))
    return columns, problems


def build_sql(spec: LinkSpec, datasets: dict[int, Dataset], columns: list[OutputColumn]) -> str:
    selects = []
    for column in columns:
        expression = f"t{column.alias}.{quote_name(column.profile.name)}"
        if column.coalesce_with:
            alias, name = column.coalesce_with
            expression = f"coalesce({expression}, t{alias}.{quote_name(name)})"
        selects.append(f"{expression} AS {quote_name(column.name)}")
    sql = f"SELECT {', '.join(selects)} FROM {quote_name(datasets[spec.main_id].table_name)} AS t0"
    for index, step in enumerate(spec.steps, start=1):
        lookup = datasets[step.lookup_id]
        linked = _table_of(spec, datasets, step.linked_to)
        conditions = []
        for left_name, right_name in step.pairs:
            left, right = _column(linked, left_name), _column(lookup, right_name)
            conditions.append(
                f"{_key_sql(step.linked_to, left, right, spec.ignore_case)} = "
                f"{_key_sql(index, right, left, spec.ignore_case)}"
            )
        join = JOIN_TYPES[step.join_type][0]
        sql += f" {join} {quote_name(lookup.table_name)} AS t{index} ON {' AND '.join(conditions)}"
    return sql


# ---------- checking ----------


def check_link(
    connection, spec: LinkSpec, datasets: dict[int, Dataset], exclude: set[int] = frozenset()
) -> LinkCheck:
    """Everything the link screen shows before saving: match counts, duplicates, row count.
    `exclude` = datasets that can't be used (the one being edited and those built on it)."""
    check = LinkCheck(steps=[None] * len(spec.steps))
    used = set(spec.source_ids()) & set(exclude)
    if used:
        names = ", ".join(f"“{datasets[i].name}”" for i in sorted(used))
        check.problems.append(
            f"{names} can't be used here: it is this dataset or is built from it."
        )
        return check
    check.columns, check.problems = plan_link(spec, datasets)
    if check.problems:
        return check
    try:
        for index, step in enumerate(spec.steps, start=1):
            check.steps[index - 1] = _check_step(connection, spec, datasets, index, step)
            if check.steps[index - 1].duplicate_count:
                check.problems.append(_duplicate_message(datasets, step, check.steps[index - 1]))
        if check.problems:
            return check
        sql = build_sql(spec, datasets, check.columns)
        check.row_count = connection.execute(f"SELECT count(*) FROM ({sql})").fetchall()[0][0]
    except duckdb.OutOfMemoryException:
        logger.warning("Out of memory checking a link")
        check.problems.append(
            "These datasets are too large to link within the memory allowed per user."
        )
        return check
    except duckdb.Error as error:
        logger.exception("Could not check a link")
        check.problems.append(f"These columns could not be compared ({error}).")
        return check
    max_rows = conf.get("MAX_ROWS")
    if check.row_count > max_rows:
        check.problems.append(
            f"The result would have {check.row_count:,} rows, more than the {max_rows:,} limit."
        )
    elif check.row_count == 0:
        check.problems.append(
            "No rows would be left: nothing matched. Check the link columns, "
            "or choose “Keep all main rows”."
        )
    else:
        check.sql = sql
    return check


def _check_step(connection, spec, datasets, index: int, step: LinkStep) -> StepCheck:
    lookup = datasets[step.lookup_id]
    linked = _table_of(spec, datasets, step.linked_to)
    left_keys, right_keys = [], []
    for left_name, right_name in step.pairs:
        left, right = _column(linked, left_name), _column(lookup, right_name)
        left_keys.append(_key_sql(step.linked_to, left, right, spec.ignore_case))
        right_keys.append(_key_sql(index, right, left, spec.ignore_case))
    lookup_table = f"{quote_name(lookup.table_name)} AS t{index}"
    all_filled = " AND ".join(f"{key} IS NOT NULL" for key in right_keys)

    shown_keys = [f"min(t{index}.{quote_name(right)})" for _, right in step.pairs]
    duplicates = connection.execute(
        f"SELECT {', '.join(shown_keys)}, count(*) AS times, count(*) OVER () AS how_many "
        f"FROM {lookup_table} WHERE {all_filled} GROUP BY {', '.join(right_keys)} "
        f"HAVING count(*) > 1 ORDER BY times DESC LIMIT 3"
    ).fetchall()

    keyed = ", ".join(f"{key} AS k{n}" for n, key in enumerate(right_keys))
    on = " AND ".join(f"{key} = hits.k{n}" for n, key in enumerate(left_keys))
    any_blank = " OR ".join(f"{key} IS NULL" for key in left_keys)
    total, blank, matched = connection.execute(
        f"SELECT count(*), count(*) FILTER (WHERE {any_blank}), count(hits.hit) "
        f"FROM {quote_name(linked.table_name)} AS t{step.linked_to} "
        f"LEFT JOIN (SELECT DISTINCT {keyed}, true AS hit FROM {lookup_table} WHERE {all_filled})"
        f" AS hits ON {on}"
    ).fetchall()[0]
    return StepCheck(
        total=total,
        blank=blank,
        matched=matched,
        duplicate_count=duplicates[0][-1] if duplicates else 0,
        duplicate_examples=[
            f"{' + '.join(str(value) for value in row[:-2])} ({row[-2]} times)"
            for row in duplicates
        ],
    )


def _duplicate_message(datasets, step: LinkStep, step_check: StepCheck) -> str:
    lookup = datasets[step.lookup_id]
    key_names = " + ".join(right for _, right in step.pairs)
    count = step_check.duplicate_count
    return (
        f"“{lookup.name}” has {count:,} {key_names} value{'s' if count != 1 else ''} listed more "
        f"than once, e.g. {', '.join(step_check.duplicate_examples)}. In a lookup list each value "
        f"must appear only once, otherwise main rows would repeat and totals would double. "
        f"Fix the file and upload it again."
    )


# ---------- saving ----------


def default_name(spec: LinkSpec, datasets: dict[int, Dataset]) -> str:
    main = datasets.get(spec.main_id)
    lookups = ", ".join(datasets[s.lookup_id].name for s in spec.steps if s.lookup_id in datasets)
    if main is None or not lookups:
        return ""
    name = f"{main.name} (with {lookups})"
    if len(name) > MAX_NAME_LENGTH:  # many lookups: keep the suggested name usable
        count = sum(1 for s in spec.steps if s.lookup_id in datasets)
        name = f"{main.name[: MAX_NAME_LENGTH - 30]} (with {count} lookups)"
    return name


def describe_sources(spec: LinkSpec, datasets: dict[int, Dataset]) -> str:
    return ", ".join(datasets[i].name for i in spec.source_ids() if i in datasets)


def save_link(
    workspace: Workspace, spec: LinkSpec, name: str, edit_id: int | None = None
) -> Dataset:
    """Create (or change, with `edit_id`) a combined dataset. Changing one also rebuilds the
    datasets built on it; if any of them would break, nothing is changed. Raises LinkRejected."""
    connection = workspace.connection
    with workspace.lock:
        if edit_id is not None:
            dependants = workspace.dependants(edit_id)
            exclude = {edit_id, *(d.dataset_id for d in dependants)}
        else:
            dependants, exclude = [], set()
        connection.execute("BEGIN TRANSACTION")
        try:
            dataset_id = edit_id if edit_id is not None else workspace.new_dataset_id()
            datasets = dict(workspace.datasets)
            datasets[dataset_id] = _build_view(
                connection, datasets, spec, dataset_id, name, exclude
            )
            for dependant in dependants:  # each after the datasets it is built from
                if dependant.link is None:
                    continue  # made from a query: rebuilt after this (query_dataset.py)
                try:
                    datasets[dependant.dataset_id] = _build_view(
                        connection, datasets, dependant.link, dependant.dataset_id, dependant.name
                    )
                except LinkRejected as error:
                    raise LinkRejected(
                        f"“{dependant.name}” is built from this dataset and would stop working: "
                        f"{error} Change it or remove it first."
                    ) from error
            connection.execute("COMMIT")
        except duckdb.OutOfMemoryException as error:
            connection.execute("ROLLBACK")
            logger.warning("Out of memory saving a link: %s", error)
            raise LinkRejected(
                "These datasets are too large to link within the memory allowed per user."
            ) from error
        except duckdb.Error as error:
            connection.execute("ROLLBACK")
            logger.exception("Could not save a link")
            raise LinkRejected(f"The combined dataset could not be saved ({error}).") from error
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        for key in [dataset_id, *(d.dataset_id for d in dependants)]:
            workspace.datasets[key] = datasets[key]
        workspace.refresh_memory()
    logger.info("Saved combined dataset %s (%s rows)", name, datasets[dataset_id].row_count)
    return datasets[dataset_id]


def _build_view(connection, datasets, spec, dataset_id, name, exclude=frozenset()) -> Dataset:
    check = check_link(connection, spec, datasets, exclude)
    if not check.ok:
        raise LinkRejected(" ".join(check.problems))
    view_name = f"ds_{dataset_id}"
    connection.execute(f"CREATE OR REPLACE VIEW {view_name} AS {check.sql}")
    profiles = [
        dataclasses.replace(column.profile, name=column.name, samples=[])
        for column in check.columns
    ]
    filled = connection.execute(
        f"SELECT {', '.join(f'count({quote_name(p.name)})' for p in profiles)} FROM {view_name}"
    ).fetchall()[0]
    for profile, filled_count in zip(profiles, filled, strict=True):
        profile.blanks = check.row_count - filled_count
    fill_samples(connection, view_name, profiles)
    return Dataset(
        dataset_id=dataset_id,
        name=name,
        table_name=view_name,
        source_file=describe_sources(spec, datasets),
        row_count=check.row_count,
        columns=profiles,
        link=spec,
    )


def refresh_view(workspace: Workspace, dataset: Dataset) -> None:
    """Build a combined dataset's view again after a dataset it uses changed (e.g. a query
    made into a dataset now has other columns). Keeps its calculated fields. Raises
    LinkRejected (the dataset is then left as it was)."""
    rebuilt = _build_view(
        workspace.connection, workspace.datasets, dataset.link, dataset.dataset_id, dataset.name
    )
    dataset.columns, dataset.row_count = rebuilt.columns, rebuilt.row_count
    dataset.source_file = rebuilt.source_file


def preview_rows(connection, sql: str, limit: int) -> list[tuple]:
    return connection.execute(f"SELECT * FROM ({sql}) LIMIT {int(limit)}").fetchall()


# ---------- suggestions ----------


def _plain(name: str) -> str:
    return re.sub(r"[^0-9a-z]", "", name.casefold())


def names_look_alike(first: str, second: str) -> bool:
    """`Item Code` ≈ `item_code` ≈ `ItemCode`; `Item Code` ≈ `Code`."""
    a, b = _plain(first), _plain(second)
    if not a or not b:
        return False
    if a == b:
        return True
    short, long = sorted((a, b), key=len)
    return len(short) >= 2 and (long.endswith(short) or long.startswith(short))


def guess_pair(linked: Dataset, lookup: Dataset) -> tuple[str, str] | None:
    """The most likely link columns between two datasets, by name only (for the form)."""
    for exact in (True, False):
        for right in lookup.columns:
            for left in linked.columns:
                same = _plain(left.name) == _plain(right.name)
                if same if exact else names_look_alike(left.name, right.name):
                    return left.name, right.name
    return None


def suggest_links(workspace: Workspace) -> list[Suggestion]:
    """Likely links between uploaded (not combined) datasets, best first. Cached until the
    list of datasets changes. Call while holding the workspace lock."""
    base = [d for d in workspace.datasets.values() if d.link is None]
    key = tuple(d.dataset_id for d in base)
    if workspace.suggestion_cache[0] == key:
        return workspace.suggestion_cache[1]
    connection = workspace.connection
    unique_columns = {d.dataset_id: _unique_columns(connection, d) for d in base}
    best: dict[tuple[int, int], Suggestion] = {}
    for lookup in base:
        for main in base:
            if main is lookup:
                continue
            main_unique = {c.name for c in unique_columns[main.dataset_id]}
            for right in unique_columns[lookup.dataset_id]:
                for left in main.columns:
                    if left.kind not in SUGGEST_KINDS or not names_look_alike(
                        left.name, right.name
                    ):
                        continue
                    # One-to-one (unique on both sides): the bigger dataset is the main one.
                    if left.name in main_unique and (
                        (main.row_count, -main.dataset_id) < (lookup.row_count, -lookup.dataset_id)
                    ):
                        continue
                    matched, total = _overlap(connection, main, left, lookup, right)
                    suggestion = Suggestion(
                        main.dataset_id, lookup.dataset_id, left.name, right.name, matched, total
                    )
                    current = best.get((main.dataset_id, lookup.dataset_id))
                    if suggestion.match_percent >= SUGGEST_MIN_PERCENT and (
                        current is None or suggestion.match_percent > current.match_percent
                    ):
                        best[(main.dataset_id, lookup.dataset_id)] = suggestion
    suggestions = list(best.values())
    suggestions.sort(key=lambda s: -s.match_percent)
    # A JSON child list and its parent: always linked by _parent_row → _row, shown first.
    family = [
        Suggestion(
            child.dataset_id, child.parent_id, PARENT_COLUMN, ROW_COLUMN, *[child.row_count] * 2
        )
        for child in base
        if child.parent_id in {d.dataset_id for d in base}
        and _column(child, PARENT_COLUMN)
        and _column(workspace.datasets[child.parent_id], ROW_COLUMN)
    ]
    # A foreign key in an uploaded SQLite file (orders.customer_id → customers.id): also first.
    by_id = {d.dataset_id: d for d in base}
    for main in base:
        for column, lookup_id, lookup_column in main.foreign_keys:
            lookup = by_id.get(lookup_id)
            left, right = _column(main, column), lookup and _column(lookup, lookup_column)
            if left and right:
                matched, total = _overlap(connection, main, left, lookup, right)
                family.append(
                    Suggestion(
                        main.dataset_id, lookup_id, column, lookup_column, matched, total, True
                    )
                )
    pairs = {pair for s in family for pair in ((s.main_id, s.lookup_id), (s.lookup_id, s.main_id))}
    suggestions = family + [s for s in suggestions if (s.main_id, s.lookup_id) not in pairs]
    workspace.suggestion_cache = (key, suggestions)
    return suggestions


def _unique_columns(connection, dataset: Dataset) -> list[ColumnProfile]:
    candidates = [
        c for c in dataset.columns if c.kind in SUGGEST_KINDS and c.blanks < dataset.row_count
    ]
    if not candidates:
        return []
    parts = []
    for column in candidates:
        value = quote_name(column.name)
        value = f"lower({value})" if column.kind == "text" else value
        parts.append(f"count(DISTINCT {value}) = count({value})")
    flags = connection.execute(
        f"SELECT {', '.join(parts)} FROM {quote_name(dataset.table_name)}"
    ).fetchall()[0]
    return [column for column, unique in zip(candidates, flags, strict=True) if unique]


def _overlap(connection, main, left, lookup, right) -> tuple[int, int]:
    left_key = _key_sql(0, left, right, ignore_case=True)
    right_key = _key_sql(1, right, left, ignore_case=True)
    return connection.execute(
        f"SELECT count(hits.k), count(*) FROM {quote_name(main.table_name)} AS t0 "
        f"LEFT JOIN (SELECT DISTINCT {right_key} AS k FROM {quote_name(lookup.table_name)} AS t1)"
        f" AS hits ON {left_key} = hits.k WHERE {left_key} IS NOT NULL"
    ).fetchall()[0]


def suggestion_groups(workspace: Workspace) -> list[dict]:
    """Suggestions grouped per main dataset, ready for one "Link" button each:
    {"main": Dataset, "spec": LinkSpec, "items": [{"lookup": Dataset, "suggestion": …}]}. Lookups of lookups are included
    (Sales → Items → Sub-categories). Skips mains that already have a combined dataset."""
    suggestions = suggest_links(workspace)
    lookups_of: dict[int, list[Suggestion]] = {}
    for suggestion in suggestions:
        lookups_of.setdefault(suggestion.main_id, []).append(suggestion)
    used_as_lookup = {s.lookup_id for s in suggestions}
    already_linked = {d.link.main_id for d in workspace.datasets.values() if d.link}
    groups = []
    for main_id in lookups_of:
        if main_id in used_as_lookup or main_id in already_linked:
            continue
        spec = suggested_spec(main_id, suggestions)
        groups.append(
            {
                "main": workspace.datasets[main_id],
                "spec": spec,
                "items": [
                    {
                        "lookup": workspace.datasets[step.lookup_id],
                        "suggestion": suggestion,
                        "via": workspace.datasets[suggestion.main_id] if step.linked_to else None,
                    }
                    for step in spec.steps
                    for suggestion in suggestions
                    if suggestion.lookup_id == step.lookup_id
                    and suggestion.main_id
                    == _table_of(spec, workspace.datasets, step.linked_to).dataset_id
                ],
            }
        )
    return groups


def suggested_spec(main_id: int, suggestions: list[Suggestion]) -> LinkSpec:
    """A ready-made link from the suggestions: main, its lookups, then their lookups."""
    spec = LinkSpec(main_id=main_id)
    positions = {main_id: 0}
    queue = [main_id]
    while queue:
        current = queue.pop(0)
        for suggestion in suggestions:
            if suggestion.main_id == current and suggestion.lookup_id not in positions:
                spec.steps.append(
                    LinkStep(
                        lookup_id=suggestion.lookup_id,
                        linked_to=positions[current],
                        pairs=[(suggestion.main_column, suggestion.lookup_column)],
                    )
                )
                positions[suggestion.lookup_id] = len(spec.steps)
                queue.append(suggestion.lookup_id)
    return spec
