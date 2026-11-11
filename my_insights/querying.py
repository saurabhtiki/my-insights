"""Queries: Group by / Values / Filters / Sort on one dataset → a result table.

The SQL is written here from the query's settings: names are quoted, typed values are passed
as parameters and formulas come from formula.py. Nothing the user types is run as SQL.

    Group by Month of Date, Branch · Values Sum of Amount · Filter Date this FY
    → SELECT CAST(date_trunc('month', src."Date") AS DATE) AS g0, src."Branch" AS g1,
             sum(src."Amount") AS v0
      FROM "sales" AS src WHERE CAST(src."Date" AS DATE) BETWEEN ? AND ?
      GROUP BY 1, 2 ORDER BY g0 NULLS LAST, g1 NULLS LAST
"""

import copy
import dataclasses
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

import duckdb
from django.utils import timezone

from .formula import Compiled, Fields, FormulaError, compile_formula
from .loading import display_value, quote_name
from .workspace import CalculatedField, QuerySpec, SavedQuery, ValueItem

logger = logging.getLogger(__name__)

BLANK = "__blank__"  # the "(blank)" choice in an "is one of" tick list
MAX_TICK_VALUES = 500
MAX_SEARCH_VALUES = 50
MAX_NAME_LENGTH = 100
MAX_FIELD_NAME_LENGTH = 60
MAX_ACROSS = 50  # columns made by an Across field (each value field makes one per value)

# key: (label, SQL template, kinds it works on — None = any)
AGGREGATIONS = {
    "sum": ("Sum", "sum({})", {"number"}),
    "avg": ("Average", "avg({})", {"number"}),
    "median": ("Median", "median({})", {"number"}),
    "count": ("Count", "count({})", None),
    "count_unique": ("Count unique", "count(DISTINCT {})", None),
    "min": ("Min", "min({})", None),
    "max": ("Max", "max({})", None),
}
AGGREGATION_HELP = {
    "sum": "Adds the values up",
    "avg": "Total ÷ number of filled rows",
    "median": "The middle value: half are below it, half above",
    "count": "Rows where this field is not blank",
    "count_unique": "How many different values, e.g. 3 branches",
    "min": "The smallest (or earliest) value",
    "max": "The biggest (or latest) value",
}
DATE_PARTS = {
    "day": ("Day", "CAST({} AS DATE)"),
    "week": ("Week", "CAST(date_trunc('week', {}) AS DATE)"),
    "month": ("Month", "CAST(date_trunc('month', {}) AS DATE)"),
    "quarter": ("Quarter", "CAST(date_trunc('quarter', {}) AS DATE)"),
    "year": ("Year", "year({})"),
    "fy": ("Financial year", "(CASE WHEN month({0}) >= 4 THEN year({0}) ELSE year({0}) - 1 END)"),
    # FY quarters line up with calendar quarters (April starts Q1), so the quarter's first day
    # sorts in time order and gives the label.
    "fy_quarter": ("FY quarter", "CAST(date_trunc('quarter', {}) AS DATE)"),
}
FILTER_OPS = {
    "text": [
        ("in", "is one of"),
        ("not_in", "is not one of"),
        ("contains", "contains"),
        ("blank", "is blank"),
        ("not_blank", "is not blank"),
    ],
    "number": [
        ("eq", "="),
        ("ne", "≠"),
        ("gt", ">"),
        ("lt", "<"),
        ("between", "between"),
        ("blank", "is blank"),
        ("not_blank", "is not blank"),
    ],
    "date": [
        ("between", "between"),
        ("today", "is today"),
        ("this_month", "this month"),
        ("last_month", "last month"),
        ("this_fy", "this financial year"),
        ("last_fy", "last financial year"),
        ("last_days", "in the last … days"),
        ("blank", "is blank"),
        ("not_blank", "is not blank"),
    ],
    "boolean": [("yes", "is Yes"), ("no", "is No"), ("blank", "is blank")],
    "total": [("gt", ">"), ("lt", "<"), ("eq", "="), ("ne", "≠"), ("between", "between")],
}
DEFAULT_OP = {"text": "in", "number": "gt", "date": "this_fy", "boolean": "yes", "total": "gt"}
COMPARE_SQL = {"eq": "=", "ne": "<>", "gt": ">", "lt": "<"}
NUMBER_FORMATS = [
    ("0", "12,345"),
    ("1", "12,345.6"),
    ("2", "12,345.67"),
    ("r0", "₹ 12,345"),
    ("r2", "₹ 12,345.67"),
]
FORMAT_KEYS = {key for key, _ in NUMBER_FORMATS}
KIND_WORDS = {
    "integer": "a whole number",
    "number": "a number",
    "text": "text",
    "date": "a date",
    "boolean": "yes / no",
}
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def kind_class(kind: str) -> str:
    """integer / number → number, date / datetime → date, boolean, text."""
    return {"integer": "number", "datetime": "date"}.get(kind, kind)


@dataclass
class FieldInfo:
    """A field the query screen offers: a column, or a calculated field."""

    name: str
    kind: str  # integer | number | date | datetime | boolean | text
    sql: str = ""
    calculated: bool = False
    is_total: bool = False  # a total formula: Values only
    formula: str = ""
    error: str = ""  # a calculated field that no longer works

    @property
    def kind_class(self) -> str:
        return kind_class(self.kind)


@dataclass
class ResultColumn:
    key: str  # g0, g1… for groups, v0, v1… for values
    label: str
    kind: str  # how cells are shown: number kinds, date, text…
    date_part: str = ""
    number_format: str = ""
    is_value: bool = False


@dataclass
class QueryPlan:
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # e.g. a filter still waiting for values
    columns: list[ResultColumn] = field(default_factory=list)
    sql: str = ""
    params: list = field(default_factory=list)
    total_sql: str = ""
    total_params: list = field(default_factory=list)
    # Across: the field spread into columns, the cells (groups + across + values) and the
    # totals of each across column. Worked out by DuckDB, so averages are right too.
    across: ResultColumn | None = None
    cell_sql: str = ""
    cell_params: list = field(default_factory=list)
    across_total_sql: str = ""
    across_total_params: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems and bool(self.sql)


@dataclass
class QueryResult:
    rows: list[dict]
    totals: dict | None  # value key → total over the groups shown
    truncated: bool  # more rows than the cap
    columns: list[ResultColumn] = field(default_factory=list)  # as shown (Across adds some)
    notes: list[str] = field(default_factory=list)
    # Across: one row per group and across value, keyed like a plan of long_spec(spec)
    # (the across field is the last g key). The chart draws these.
    long_rows: list[dict] = field(default_factory=list)


# ---------- fields ----------


def formula_fields(dataset) -> Fields:
    return Fields(
        {column.name: column.kind for column in dataset.columns},
        {calc.name: calc.formula for calc in dataset.calculated},
    )


def field_catalog(dataset) -> dict[str, FieldInfo]:
    """Every field of a dataset by name: columns first, then calculated fields."""
    catalog = {
        column.name: FieldInfo(column.name, column.kind, f"src.{quote_name(column.name)}")
        for column in dataset.columns
    }
    fields = formula_fields(dataset)
    for calc in dataset.calculated:
        info = FieldInfo(calc.name, "text", calculated=True, formula=calc.formula)
        try:
            compiled = compile_formula(calc.formula, fields, (calc.name,))
        except FormulaError as error:
            info.error = str(error)
        else:
            info.sql = f"({compiled.sql})"
            info.kind = _profile_kind(compiled)
            info.is_total = compiled.is_total
        catalog[calc.name] = info
    return catalog


def _profile_kind(compiled: Compiled) -> str:
    if compiled.kind == "number" and compiled.whole:
        return "integer"
    return {"yes/no": "boolean"}.get(compiled.kind, compiled.kind)


def aggregations_for(info: FieldInfo) -> list[tuple[str, str, str]]:
    """(key, label, help) of the aggregations that make sense for this field."""
    return [
        (key, label, AGGREGATION_HELP[key])
        for key, (label, _, kinds) in AGGREGATIONS.items()
        if kinds is None or info.kind_class in kinds
    ]


def default_aggregation(info: FieldInfo) -> str:
    if info.is_total:
        return "formula"
    return "sum" if info.kind_class == "number" else "count_unique"


def value_label(item: ValueItem, catalog: dict) -> str:
    if item.label:
        return item.label
    if item.agg == "rows":
        return "Count of rows"
    if item.agg == "formula":
        return item.column
    label = AGGREGATIONS.get(item.agg, ("Value",))[0]
    return f"{label} of {item.column}"


def group_label(item, catalog: dict) -> str:
    info = catalog.get(item.column)
    if info and info.kind_class == "date" and item.date_part in DATE_PARTS:
        label = DATE_PARTS[item.date_part][0]
        return f"{item.column} ({label if label.startswith('FY') else label.lower()})"
    return item.column


def default_name(spec: QuerySpec, dataset, catalog: dict | None = None) -> str:
    """e.g. "Sum of Amount by Date (month), Branch"."""
    catalog = catalog if catalog is not None else field_catalog(dataset)
    values = ", ".join(value_label(item, catalog) for item in spec.values)
    groups = ", ".join(group_label(item, catalog) for item in spec.groups)
    name = f"{values} by {groups}" if values and groups else values or groups or dataset.name
    if len(name) > MAX_NAME_LENGTH:
        name = name[: MAX_NAME_LENGTH - 1].rstrip() + "…"
    return name


# ---------- building the SQL ----------


def long_spec(spec: QuerySpec) -> QuerySpec:
    """The query with its Across field as one more Group by field: the columns of a chart's
    rows (QueryResult.long_rows, which keep Top N and total filters per row)."""
    spec = copy.deepcopy(spec)
    if spec.across is not None:
        spec.groups.append(spec.across)
        spec.across = None
    return spec


def _group_sql(item, key: str, catalog: dict, dataset, plan: QueryPlan, box: str):
    """(SQL, result column) of a Group by / Across field, or None (problem reported)."""
    info = _usable(item.column, catalog, dataset, plan, box)
    if info is None:
        return None
    sql, kind, part = info.sql, info.kind, ""
    if info.kind_class == "date":
        part = item.date_part if item.date_part in DATE_PARTS else "month"
        sql = DATE_PARTS[part][1].format(info.sql)
        kind = "date" if part != "year" and part != "fy" else "integer"
    return sql, ResultColumn(key, group_label(item, catalog), kind, part)


def _combine(all_of: list, any_of: list) -> tuple[str, list]:
    """[(sql, params)] → "(a) AND (b) AND ((c) OR (d))" and its params, in the same order."""
    parts = [f"({sql})" for sql, _ in all_of]
    params = [param for _, values in all_of for param in values]
    if any_of:
        parts.append("(" + " OR ".join(f"({sql})" for sql, _ in any_of) + ")")
        params += [param for _, values in any_of for param in values]
    return " AND ".join(parts), params


def plan_query(
    spec: QuerySpec,
    dataset,
    today: date | None = None,
    catalog: dict | None = None,
    cap: int | None = None,
) -> QueryPlan:
    """The SQL for a query, or the problems that stop it from running. `cap`: the most rows
    to return (default: the preview cap)."""
    today = today or timezone.localdate()
    catalog = catalog if catalog is not None else field_catalog(dataset)
    plan = QueryPlan()
    cap = cap or row_cap()

    groups = []  # (key, sql)
    for index, item in enumerate(spec.groups):
        built = _group_sql(item, f"g{index}", catalog, dataset, plan, "Group by")
        if built is not None:
            groups.append((built[1].key, built[0]))
            plan.columns.append(built[1])
    across_sql = ""
    if spec.across is not None:
        built = _group_sql(spec.across, "a0", catalog, dataset, plan, "Across")
        if built is not None:
            across_sql, plan.across = built

    values = []  # (key, sql)
    value_sql_by_key = {}
    for index, item in enumerate(spec.values):
        built = _value_sql(item, catalog, dataset, plan)
        if built is None:
            continue
        sql, kind = built
        key = f"v{index}"
        values.append((key, sql))
        value_sql_by_key[item.key] = (key, sql)
        number_format = item.number_format if item.number_format in FORMAT_KEYS else ""
        if not number_format and kind_class(kind) == "number":
            number_format = "0" if kind == "integer" and item.agg not in ("avg", "median") else "2"
        plan.columns.append(
            ResultColumn(key, value_label(item, catalog), kind, "", number_format, True)
        )

    # {False: "All of these" (AND), True: "Any one of these" (OR)} → [(sql, params)]
    where_parts: dict[bool, list] = {False: [], True: []}
    having_parts: dict[bool, list] = {False: [], True: []}
    for item in spec.filters:
        if item.value_key:
            target = value_sql_by_key.get(item.value_key)
            if target is None:
                continue  # its value was removed or has a problem (already reported)
            built = _total_condition(item, target[1], plan)
            if built:
                having_parts[item.any_of].append(built)
            continue
        info = _usable(item.column, catalog, dataset, plan, "Filters")
        if info is None:
            continue
        built = _row_condition(item, info, today, plan)
        if built:
            where_parts[item.any_of].append(built)
    where, where_params = _combine(where_parts[False], where_parts[True])
    having, having_params = _combine(having_parts[False], having_parts[True])

    if plan.problems:
        return plan
    if plan.across is not None and not values:
        plan.notes.append("Across needs a value (e.g. Sum of Amount) to spread into columns.")
        plan.across, across_sql = None, ""
    if not groups and not values:
        return _records_plan(plan, catalog, dataset, where, where_params, spec.top_n, cap)

    select = ", ".join(f"{sql} AS {key}" for key, sql in groups + values)
    source = f"{quote_name(dataset.table_name)} AS src"
    sql = f"SELECT {'DISTINCT ' if not values else ''}{select} FROM {source}"
    if where:
        sql += " WHERE " + where
    if groups and values:
        sql += " GROUP BY " + ", ".join(str(n) for n in range(1, len(groups) + 1))
    if having:
        sql += " HAVING " + having

    order = []
    keys_by_item = {item.key: f"g{i}" for i, item in enumerate(spec.groups)}
    keys_by_item |= {item.key: f"v{i}" for i, item in enumerate(spec.values)}
    shown = {key for key, _ in groups + values}
    for sort in spec.sorts:
        key = keys_by_item.get(sort.target)
        if key in shown and key not in [o.split()[0] for o in order]:
            order.append(f"{key} {'DESC' if sort.descending else 'ASC'} NULLS LAST")
    used = {o.split()[0] for o in order}
    order += [f"{key} ASC NULLS LAST" for key, _ in groups if key not in used]
    outer = f"SELECT * FROM ({sql}) AS result"
    if order:
        outer += " ORDER BY " + ", ".join(order)
    plan.params = where_params + having_params
    limit = min(spec.top_n, cap) if spec.top_n else cap + 1  # +1 tells us there are more
    plan.sql = f"{outer} LIMIT {int(limit)}"
    if not values:
        return plan

    # The Total row (and the Across cells): over the rows that pass the filters, in the
    # groups shown (Top N and total filters leave some groups out).
    conditions = [where] if where else []
    params = list(where_params)
    prefix = ""
    if groups and (spec.top_n or having):
        match = " AND ".join(f"shown.{key} IS NOT DISTINCT FROM ({gsql})" for key, gsql in groups)
        conditions.append(f"EXISTS (SELECT 1 FROM shown WHERE {match})")
        prefix = f"WITH shown AS ({plan.sql}) "
        params = plan.params + params
    total_where = " WHERE " + " AND ".join(conditions) if conditions else ""
    totals = ", ".join(f"{sql} AS {key}" for key, sql in values)
    if groups:
        plan.total_sql = f"{prefix}SELECT {totals} FROM {source}{total_where}"
        plan.total_params = params
    if plan.across is not None:
        cells = ", ".join(f"{sql} AS {key}" for key, sql in [*groups, ("a0", across_sql), *values])
        numbers = ", ".join(str(n) for n in range(1, len(groups) + 2))
        plan.cell_sql = (
            f"{prefix}SELECT {cells} FROM {source}{total_where} GROUP BY {numbers} "
            f"ORDER BY {numbers.replace(',', ' NULLS LAST,')} NULLS LAST LIMIT {int(cap) + 1}"
        )
        plan.cell_params = params
        plan.across_total_sql = (
            f"{prefix}SELECT {across_sql} AS a0, {totals} FROM {source}{total_where} "
            "GROUP BY 1 ORDER BY 1 NULLS LAST"
        )
        plan.across_total_params = params
    return plan


def _records_plan(plan, catalog, dataset, where, where_params, top_n, cap) -> QueryPlan:
    """No Group by and no Values: the matching records themselves, every field, in file order.
    e.g. Filter Department is one of IT → each IT row, not a total."""
    fields = [info for info in catalog.values() if not info.error and not info.is_total]
    select = ", ".join(f"{info.sql} AS c{index}" for index, info in enumerate(fields))
    for index, info in enumerate(fields):
        plan.columns.append(ResultColumn(f"c{index}", info.name, info.kind))
    sql = f"SELECT {select} FROM {quote_name(dataset.table_name)} AS src"
    if where:
        sql += " WHERE " + where
    limit = min(top_n, cap) if top_n else cap + 1  # +1 tells us there are more
    plan.sql = f"{sql} LIMIT {int(limit)}"
    plan.params = where_params
    plan.notes.append(
        "Showing the rows themselves. Put a field in Group by or Values to get totals instead."
    )
    return plan


def row_cap() -> int:
    from . import conf

    return int(conf.get("PREVIEW_ROW_CAP"))


def _usable(name: str, catalog: dict, dataset, plan: QueryPlan, box: str) -> FieldInfo | None:
    info = catalog.get(name)
    if info is None:
        plan.problems.append(f"“{name}” is no longer in {dataset.name}. Remove it from {box}.")
        return None
    if info.error:
        plan.problems.append(f"Calculated field “{name}”: {info.error}")
        return None
    if info.is_total:
        plan.problems.append(f"“{name}” is worked out per group, so it can only go in Values.")
        return None
    return info


def _value_sql(item: ValueItem, catalog: dict, dataset, plan: QueryPlan):
    if item.agg == "rows":
        return "count(*)", "integer"
    info = catalog.get(item.column)
    if info is None:
        plan.problems.append(
            f"“{item.column}” is no longer in {dataset.name}. Remove it from Values."
        )
        return None
    if info.error:
        plan.problems.append(f"Calculated field “{item.column}”: {info.error}")
        return None
    if info.is_total:
        return info.sql, info.kind
    agg = AGGREGATIONS.get(item.agg)
    if agg is None or (agg[2] is not None and info.kind_class not in agg[2]):
        agg_key = default_aggregation(info)
        agg = AGGREGATIONS[agg_key]
        item.agg = agg_key
    kind = value_kind(item, info)
    return agg[1].format(info.sql), kind


def value_kind(item: ValueItem, info: FieldInfo | None) -> str:
    """The kind of a value's result, e.g. Sum of a whole-number column is a whole number."""
    if item.agg in ("rows", "count", "count_unique") or info is None:
        return "integer"
    if info.is_total or item.agg in ("min", "max"):
        return info.kind
    if item.agg == "sum":
        return "integer" if info.kind == "integer" else "number"
    return "number"


def _number(text: str) -> float | None:
    cleaned = text.replace(",", "").replace("₹", "").strip()
    try:
        number = float(Decimal(cleaned))
    except (InvalidOperation, ValueError):
        return None
    return number if math.isfinite(number) else None  # "1e400", "inf" and "nan" are not numbers


def _date(text: str) -> date | None:
    for date_format in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text.strip(), date_format).date()
        except ValueError:
            continue
    return None


def financial_year_start(day: date) -> date:
    return date(day.year if day.month >= 4 else day.year - 1, 4, 1)


def date_range(op: str, today: date, days: int = 0) -> tuple[date, date] | None:
    """First and last day of a quick pick, worked out on the day the query runs."""
    if op == "today":
        return today, today
    if op == "this_month":
        start = today.replace(day=1)
        return start, (start + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    if op == "last_month":
        end = today.replace(day=1) - timedelta(days=1)
        return end.replace(day=1), end
    if op == "this_fy":
        start = financial_year_start(today)
        return start, date(start.year + 1, 3, 31)
    if op == "last_fy":
        start = financial_year_start(today)
        return date(start.year - 1, 4, 1), date(start.year, 3, 31)
    if op == "last_days" and days > 0:
        return today - timedelta(days=days - 1), today
    return None


def _row_condition(item, info: FieldInfo, today: date, plan: QueryPlan):
    """(SQL, params) for a filter on rows, or None when it is still waiting for values."""
    sql, kind, op = info.sql, info.kind_class, item.op
    if op not in {key for key, _ in FILTER_OPS[kind]}:
        op = item.op = DEFAULT_OP[kind]
    values = [value for value in item.values if value.strip()]
    if op == "blank":
        return f"{sql} IS NULL", []
    if op == "not_blank":
        return f"{sql} IS NOT NULL", []
    if kind == "boolean":
        return f"{sql} = {'TRUE' if op == 'yes' else 'FALSE'}", []
    if op in ("in", "not_in"):
        if not values:
            plan.notes.append(f"Filter on {item.column}: tick at least one value.")
            return None
        picked = [value for value in values if value != BLANK]
        blank = BLANK in values
        marks = ", ".join("?" for _ in picked)
        text = f"CAST({sql} AS VARCHAR)"
        if op == "in":
            parts = [f"{text} IN ({marks})"] if picked else []
            parts += [f"{sql} IS NULL"] if blank else []
            return "(" + " OR ".join(parts) + ")", picked
        condition = f"({sql} IS NULL OR {text} NOT IN ({marks}))" if picked else "TRUE"
        if blank:
            condition = f"({sql} IS NOT NULL AND {condition})"
        return condition, picked
    if op == "contains":
        if not values:
            plan.notes.append(f"Filter on {item.column}: type the text to look for.")
            return None
        return f"contains(lower(CAST({sql} AS VARCHAR)), lower(?))", [values[0]]
    if kind == "date":
        if op == "between":
            low = _date(item.values[0]) if item.values and item.values[0] else None
            high = _date(item.values[1]) if len(item.values) > 1 and item.values[1] else None
            for raw, parsed in zip(item.values[:2], (low, high), strict=False):
                if raw.strip() and parsed is None:
                    plan.problems.append(f"Filter on {item.column}: “{raw}” is not a date.")
                    return None
            if low is None and high is None:
                plan.notes.append(f"Filter on {item.column}: choose a start and / or end date.")
                return None
            parts, params = [], []
            if low:
                parts.append(f"CAST({sql} AS DATE) >= ?")
                params.append(low)
            if high:
                parts.append(f"CAST({sql} AS DATE) <= ?")
                params.append(high)
            return " AND ".join(parts), params
        days = 0
        if op == "last_days":
            days = int(_number(values[0]) or 0) if values else 0
            if not 1 <= days <= 36_500:
                plan.notes.append(f"Filter on {item.column}: type how many days, e.g. 30.")
                return None
        start, end = date_range(op, today, days)
        return f"CAST({sql} AS DATE) BETWEEN ? AND ?", [start, end]
    return _compare(item, sql, op, plan, item.column)


def _total_condition(item, sql: str, plan: QueryPlan):
    op = item.op if item.op in {key for key, _ in FILTER_OPS["total"]} else "gt"
    item.op = op
    return _compare(item, sql, op, plan, "the total")


def _compare(item, sql: str, op: str, plan: QueryPlan, what: str):
    numbers = []
    needed = 2 if op == "between" else 1
    for raw in (item.values + ["", ""])[:needed]:
        if not raw.strip():
            numbers.append(None)
            continue
        number = _number(raw)
        if number is None:
            plan.problems.append(f"Filter on {what}: “{raw}” is not a number.")
            return None
        numbers.append(number)
    if op == "between":
        low, high = numbers
        if low is None and high is None:
            plan.notes.append(f"Filter on {what}: type the lowest and / or highest number.")
            return None
        parts = ([f"{sql} >= ?"] if low is not None else []) + (
            [f"{sql} <= ?"] if high is not None else []
        )
        return " AND ".join(parts), [n for n in numbers if n is not None]
    if numbers[0] is None:
        plan.notes.append(f"Filter on {what}: type a number.")
        return None
    return f"{sql} {COMPARE_SQL[op]} ?", [numbers[0]]


# ---------- running ----------


class QueryFailed(Exception):
    """The query could not run; the message is safe to show."""


def _fetch(connection, sql: str, params: list) -> list[tuple]:
    try:
        return connection.execute(sql, params).fetchall()
    except duckdb.OutOfMemoryException as error:
        logger.warning("Out of memory running a query: %s", error)
        raise QueryFailed("This query needs more memory than allowed per user.") from error
    except duckdb.Error as error:
        logger.warning("Query failed: %s", error)
        raise QueryFailed(
            f"The query could not be worked out on this data ({_short(error)})."
        ) from error


def run_query(connection, plan: QueryPlan, top_n: int | None = None) -> QueryResult:
    """Run a planned query. With an Across field, the result is spread into columns."""
    records = _fetch(connection, plan.sql, plan.params)
    totals = None
    if plan.total_sql:
        total_record = _fetch(connection, plan.total_sql, plan.total_params)[0]
        value_keys = [column.key for column in plan.columns if column.is_value]
        totals = dict(zip(value_keys, total_record, strict=True))
    cap = row_cap()
    truncated = not top_n and len(records) > cap
    keys = [column.key for column in plan.columns]
    rows = [dict(zip(keys, record, strict=True)) for record in records[:cap]]
    result = QueryResult(rows, totals, truncated, list(plan.columns))
    if plan.across is not None:
        _spread_across(connection, plan, result)
    return result


def _spread_across(connection, plan: QueryPlan, result: QueryResult) -> None:
    """Across: one column per across value and value field, plus a Total column per value,
    e.g. Branch | Apr 2026 | May 2026 | Total. Every total is asked of DuckDB (not added up
    here), so an Average's total is the average of the rows, not of the cells."""
    group_columns = [column for column in plan.columns if not column.is_value]
    value_columns = [column for column in plan.columns if column.is_value]
    across_totals = _fetch(connection, plan.across_total_sql, plan.across_total_params)
    cells = _fetch(connection, plan.cell_sql, plan.cell_params)
    count = len(group_columns)
    if len(cells) > row_cap():
        result.truncated = True
        cells = cells[: row_cap()]
    found = {(record[:count], record[count]): record[count + 1 :] for record in cells}
    long_keys = [*(column.key for column in group_columns), f"g{count}"]
    long_keys += [column.key for column in value_columns]
    result.long_rows = [dict(zip(long_keys, record, strict=True)) for record in cells]
    if not group_columns and not result.rows:  # a total filter left nothing
        across_totals = []
    if len(across_totals) > MAX_ACROSS:
        result.notes.append(
            f"{plan.across.label} has {len(across_totals):,} different values: showing the "
            f"first {MAX_ACROSS} as columns. Add a filter to see the others."
        )
        across_totals = across_totals[:MAX_ACROSS]
    single = len(value_columns) == 1
    spread, totals = [], dict(result.totals or {})
    if not group_columns and result.rows:  # one row: it is its own total
        totals |= {column.key: result.rows[0][column.key] for column in value_columns}
    for index, (across_value, *across_total) in enumerate(across_totals):
        heading = format_value(across_value, plan.across)
        for position, column in enumerate(value_columns):
            key = f"p{index}{column.key}"
            label = heading if single else f"{heading} · {column.label}"
            spread.append(dataclasses.replace(column, key=key, label=label))
            totals[key] = across_total[position]
    for row in result.rows:
        groups = tuple(row[column.key] for column in group_columns)
        for index, (across_value, *_) in enumerate(across_totals):
            cell = found.get((groups, across_value))
            for position, column in enumerate(value_columns):
                row[f"p{index}{column.key}"] = cell[position] if cell else None
    total_columns = [
        dataclasses.replace(column, label="Total" if single else f"Total · {column.label}")
        for column in value_columns
    ]
    result.columns = group_columns + spread + total_columns
    result.totals = totals


def _short(error: Exception) -> str:
    return str(error).splitlines()[0][:200]


# ---------- showing values ----------


def indian_number(value, decimals: int) -> str:
    """12345678.9 → "1,23,45,678.90" (lakh / crore grouping)."""
    number = float(value)
    text = f"{abs(number):.{decimals}f}"
    whole, _, fraction = text.partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts) + "," + tail
    sign = "-" if number < 0 and float(text) != 0 else ""
    return sign + whole + ("." + fraction if fraction else "")


def format_value(value, column: ResultColumn) -> str:
    if value is None:
        return "(blank)" if not column.is_value else ""
    if column.date_part:
        return date_part_label(value, column.date_part)
    if (
        column.number_format
        and isinstance(value, int | float | Decimal)
        and not isinstance(value, bool)
    ):
        rupee = column.number_format.startswith("r")
        text = indian_number(value, int(column.number_format[-1]))
        return f"₹ {text}" if rupee else text
    return display_value(value)


def date_part_label(value, part: str) -> str:
    if part in ("year",):
        return str(value)
    if part == "fy":
        return f"FY {value}-{(int(value) + 1) % 100:02d}"
    if not isinstance(value, date):
        return display_value(value)
    if part == "week":
        return f"Week of {value:%d-%m-%Y}"
    if part == "month":
        return f"{MONTHS[value.month - 1]} {value.year}"
    if part == "quarter":
        return f"Q{(value.month - 1) // 3 + 1} {value.year}"
    if part == "fy_quarter":
        start = financial_year_start(value)
        quarter = (value.month - 4) % 12 // 3 + 1
        return f"FY Q{quarter} {start.year}-{(start.year + 1) % 100:02d}"
    return f"{value:%d-%m-%Y}"


# ---------- filter tick lists ----------


def value_choices(connection, dataset, column: str, search: str = "") -> list[tuple[str, str, int]]:
    """(value, label, rows) for an "is one of" tick list: the most common values first.
    With `search`, values containing it (any of them, not only the top 500)."""
    info = field_catalog(dataset).get(column)
    if info is None or info.error or info.is_total:
        return []
    text = f"CAST({info.sql} AS VARCHAR)"
    sql = f"SELECT {text} AS value, count(*) AS rows FROM {quote_name(dataset.table_name)} AS src"
    params: list = []
    limit = MAX_TICK_VALUES
    if search.strip():
        sql += f" WHERE contains(lower({text}), lower(?))"
        params.append(search.strip())
        limit = MAX_SEARCH_VALUES
    sql += f" GROUP BY 1 ORDER BY rows DESC, value NULLS LAST LIMIT {limit}"
    records = connection.execute(sql, params).fetchall()
    return [
        (BLANK if value is None else value, "(blank)" if value is None else value, rows)
        for value, rows in records
    ]


# ---------- calculated fields ----------


FORMULA_MODES = ("row", "group")  # the "Work it out: Per row / Per group" switch


def mode_problem(is_total: bool, mode: str) -> str:
    """Does the formula match the switch? "" when it does (or no switch was sent)."""
    if mode == "row" and is_total:
        return (
            "“Per row” can't use SUM, AVG, COUNT…: they add up many rows. "
            "Pick “Per group” above, or take them out."
        )
    if mode == "group" and not is_total:
        return (
            "“Per group” needs a total, e.g. SUM([Basic]): click a field to add one. "
            "Or pick “Per row” above."
        )
    return ""


def check_formula(
    connection, dataset, name: str, formula: str, original: str = "", mode: str = ""
) -> dict:
    """Try a formula on the data: {"ok", "message", "kind", "is_total", "examples"}."""
    calculated = {c.name: c.formula for c in dataset.calculated if c.name != original}
    fields = Fields({column.name: column.kind for column in dataset.columns}, calculated)
    try:
        compiled = compile_formula(formula, fields, (name,) if name else ())
    except FormulaError as error:
        logger.info("Formula refused: %s", error)
        return {"ok": False, "message": str(error)}
    problem = mode_problem(compiled.is_total, mode)
    if problem:
        logger.info("Formula refused: %s", problem)
        return {"ok": False, "message": problem}
    table = quote_name(dataset.table_name)
    try:
        if compiled.is_total:
            records = connection.execute(f"SELECT {compiled.sql} FROM {table} AS src").fetchall()
        else:
            records = connection.execute(
                f"SELECT DISTINCT {compiled.sql} AS value FROM "
                f"(SELECT * FROM {table} LIMIT 200) AS src WHERE value IS NOT NULL LIMIT 3"
            ).fetchall()
    except duckdb.Error as error:
        logger.info("Formula failed on the data: %s", error)
        return {
            "ok": False,
            "message": f"This formula can't be worked out on your data ({_short(error)}).",
        }
    kind = _profile_kind(compiled)
    number_format = {"number": "2", "integer": "0"}.get(kind, "")
    column = ResultColumn("x", "", kind, number_format=number_format)
    examples = [format_value(record[0], column) for record in records if record[0] is not None]
    return {
        "ok": True,
        "kind": kind,
        "kind_word": KIND_WORDS[kind],
        "is_total": compiled.is_total,
        "examples": examples,
    }


def field_name_error(dataset, name: str, original: str = "") -> str:
    if not name:
        return "Give the field a name, e.g. Profit."
    if len(name) > MAX_FIELD_NAME_LENGTH:
        return f"Keep the name under {MAX_FIELD_NAME_LENGTH} characters."
    if re.search(r"[\[\]]", name):
        return "A field name can't contain [ or ]."
    taken = {column.name.casefold() for column in dataset.columns}
    taken |= {c.name.casefold() for c in dataset.calculated if c.name != original}
    if name.casefold() in taken:
        return f"{dataset.name} already has a field called “{name}”."
    return ""


def save_formula(
    workspace, dataset, name: str, formula: str, original: str = "", mode: str = ""
) -> str:
    """Add or change a calculated field. Returns "" or the problem to show."""
    problem = field_name_error(dataset, name, original)
    if not problem:
        check = check_formula(workspace.connection, dataset, name, formula, original, mode)
        problem = "" if check["ok"] else check["message"]
    if problem:
        logger.info("Calculated field refused: %s", problem)
        return problem
    existing = (
        next((c for c in dataset.calculated if c.name == original), None) if original else None
    )
    if existing is None:
        dataset.calculated.append(CalculatedField(name, formula))
        return ""
    existing.formula = formula
    if name != original:
        _rename_field(workspace, dataset, original, name)
        existing.name = name
    return ""


def _rename_field(workspace, dataset, old: str, new: str) -> None:
    """Point other formulas and saved queries at the field's new name."""
    pattern = re.compile(r"\[" + re.escape(old.replace("]", "]]")) + r"\]", re.IGNORECASE)
    replacement = "[" + new.replace("]", "]]") + "]"
    for calc in dataset.calculated:
        calc.formula = pattern.sub(lambda _: replacement, calc.formula)
    for query in workspace.queries_of([dataset.dataset_id]):
        rename_in_spec(query.spec, old, new)
    own_queries = [chart.own_query for chart in workspace.charts.values() if chart.own_query]
    for draft in [*workspace.query_drafts.values(), *own_queries]:
        if draft.dataset_id == dataset.dataset_id:
            rename_in_spec(draft, old, new)


def spec_items(spec: QuerySpec) -> list:
    """Every item that names a field: Group by, Across, Values and Filters."""
    across = [spec.across] if spec.across is not None else []
    return [*spec.groups, *across, *spec.values, *spec.filters]


def rename_in_spec(spec: QuerySpec, old: str, new: str) -> None:
    for item in spec_items(spec):
        if item.column == old:
            item.column = new


def queries_using(workspace, dataset, name: str) -> list[SavedQuery]:
    used = []
    for query in workspace.queries_of([dataset.dataset_id]):
        spec = query.spec
        if any(item.column == name for item in spec_items(spec)):
            used.append(query)
    return used


def formulas_using(dataset, name: str) -> list[str]:
    users = []
    for calc in dataset.calculated:
        if calc.name == name:
            continue
        if re.search(
            r"\[" + re.escape(name.replace("]", "]]")) + r"\]", calc.formula, re.IGNORECASE
        ):
            users.append(calc.name)
    return users


def query_name_error(workspace, name: str, ignore_id: int | None = None) -> str:
    if not name:
        return "Type a name for the query."
    if len(name) > MAX_NAME_LENGTH:
        return f"Keep the name under {MAX_NAME_LENGTH} characters."
    for query in workspace.queries.values():
        if query.query_id != ignore_id and query.name.casefold() == name.casefold():
            return f"Another query is already called “{name}”."
    return ""
