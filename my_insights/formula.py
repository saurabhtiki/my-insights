"""Calculated fields: a small formula language turned into DuckDB SQL.

    Profit   = [Sales] - [Cost]
    Size     = IFS([Qty] > 100, "Large", [Qty] > 10, "Medium", "Small")
    Margin % = SUM([Profit]) / SUM([Sales]) * 100        ← a "per group" formula (Values only)

The formula is read by our own parser, and the SQL is written by this module from fixed
pieces: column names are quoted, text is escaped, numbers are checked. Typed text is never
run as SQL, so a formula can't reach anything but this dataset's columns.
"""

import difflib
import re
from dataclasses import dataclass, field
from datetime import datetime

from .loading import quote_name

MAX_LENGTH = 2000
MAX_DEPTH = 60
MAX_SQL_LENGTH = 200_000  # fields built from fields that repeat each other grow fast

# Column kinds (from the profile) → the four kinds a formula works with.
FORMULA_KIND = {
    "integer": "number",
    "number": "number",
    "date": "date",
    "datetime": "date",
    "boolean": "yes/no",
    "text": "text",
}
KIND_WORDS = {"number": "a number", "text": "text", "date": "a date", "yes/no": "yes / no"}

AGGREGATES = {
    # name: (SQL template, kinds it accepts or None for any)
    "SUM": ("sum({})", {"number"}),
    "AVG": ("avg({})", {"number"}),
    "MEDIAN": ("median({})", {"number"}),
    "MIN": ("min({})", None),
    "MAX": ("max({})", None),
    "COUNT": ("count({})", None),
    "COUNT_UNIQUE": ("count(DISTINCT {})", None),
}
FUNCTIONS = {  # every word a formula may use, with a short help line for the chips
    "IF": "IF(condition, then, else)",
    "IFS": "IFS(condition 1, value 1, condition 2, value 2, …, else)",
    "SWITCH": "SWITCH([Field], match 1, result 1, …, otherwise)",
    "ROUND": "ROUND(number, decimals)",
    "ABS": "ABS(number) — drops the minus sign",
    "YEAR": "YEAR(date) — e.g. 2026",
    "MONTH": "MONTH(date) — 1 to 12",
    "DAYS_BETWEEN": "DAYS_BETWEEN(start date, end date)",
    "TODAY": "TODAY() — today's date",
    "CONTAINS": 'CONTAINS(text, part) — e.g. CONTAINS([City], "pur"); capitals don\'t matter',
    "STARTS_WITH": 'STARTS_WITH(text, start) — e.g. STARTS_WITH([Code], "IN")',
    "ENDS_WITH": 'ENDS_WITH(text, end) — e.g. ENDS_WITH([Email], ".com")',
    "ISBLANK": "ISBLANK(value) — yes when the value is empty",
    "SUM": "SUM(number) — total of each group",
    "AVG": "AVG(number) — average of each group",
    "MEDIAN": "MEDIAN(number) — middle value of each group",
    "MIN": "MIN(value) — smallest in each group",
    "MAX": "MAX(value) — biggest in each group",
    "COUNT": "COUNT(value) — rows that are not blank, per group",
    "COUNT_UNIQUE": "COUNT_UNIQUE(value) — different values, per group",
}
WORDS = {"AND", "OR", "NOT", "TRUE", "FALSE"}
TEXT_TESTS = {"CONTAINS": "contains", "STARTS_WITH": "starts_with", "ENDS_WITH": "ends_with"}

TOKEN = re.compile(
    r"""
    (?P<space>\s+)
  | (?P<number>\d+(\.\d+)?|\.\d+)
  | (?P<text>"(?:[^"]|"")*")
  | (?P<field>\[(?:[^\]]|\]\])*\])
  | (?P<op><=|>=|<>|!=|[-+*/&=<>(),])
  | (?P<word>[A-Za-z_][A-Za-z0-9_]*)
    """,
    re.VERBOSE,
)
DATE_TEXT = (("%d-%m-%Y", r"\d{1,2}-\d{1,2}-\d{4}"), ("%Y-%m-%d", r"\d{4}-\d{1,2}-\d{1,2}"))


class FormulaError(ValueError):
    """The formula can't be used; the message says why in plain words."""


@dataclass
class Compiled:
    sql: str
    kind: str  # number | text | date | yes/no
    is_total: bool = False  # uses SUM/AVG/…: worked out on totals, Values only
    literal: str | None = None  # the text of a "quoted" value, so it can act as a date
    columns: set[str] = field(default_factory=set)  # real columns it reads
    whole: bool = False  # a number with no decimals, e.g. [Qty] * 2


@dataclass
class Token:
    kind: str
    text: str
    position: int


def tokenize(text: str) -> list[Token]:
    tokens, position = [], 0
    while position < len(text):
        match = TOKEN.match(text, position)
        if match is None:
            if text[position] == '"':
                raise FormulaError('A text value is missing its closing quote (").')
            if text[position] == "[":
                raise FormulaError("A field name is missing its closing bracket (]).")
            raise FormulaError(f"“{text[position]}” can't be used in a formula.")
        kind = match.lastgroup
        if kind != "space":
            tokens.append(Token(kind, match.group(), position))
        position = match.end()
    return tokens


class Fields:
    """The names a formula can use: the dataset's columns and its other calculated fields."""

    def __init__(self, columns: dict[str, str], calculated: dict[str, str]):
        # columns: {name: profile kind}; calculated: {name: formula}
        self.columns = columns
        self.calculated = calculated
        self.lookup = {name.casefold(): name for name in [*columns, *calculated]}
        self.compiled: dict[str, Compiled] = {}  # calculated fields already read once

    def resolve(self, typed: str) -> str:
        name = self.lookup.get(typed.casefold())
        if name is None:
            close = difflib.get_close_matches(typed, list(self.lookup.values()), n=1, cutoff=0.6)
            hint = f" Did you mean “[{close[0]}]”?" if close else ""
            raise FormulaError(f"“[{typed}]” is not a column.{hint}")
        return name


def compile_formula(text: str, fields: Fields, _stack: tuple[str, ...] = ()) -> Compiled:
    """Read a formula and return its SQL and kind, or raise FormulaError naming the problem."""
    if not text or not text.strip():
        raise FormulaError("Type a formula, e.g. [Sales] - [Cost].")
    if len(text) > MAX_LENGTH:
        raise FormulaError(f"Keep the formula under {MAX_LENGTH:,} characters.")
    parser = _Parser(tokenize(text), fields, _stack)
    result = parser.expression()
    if parser.position < len(parser.tokens):
        extra = parser.tokens[parser.position].text
        raise FormulaError(f"Unexpected “{extra}”. Is a sign like + or a comma missing before it?")
    if len(result.sql) > MAX_SQL_LENGTH:
        raise FormulaError(
            "This formula is too complex: the fields it uses repeat each other too many times."
        )
    if result.is_total and parser.bare_columns:
        name = min(parser.bare_columns)
        raise FormulaError(
            f"This is a per-group formula (it uses SUM, AVG…), so “[{name}]” must be inside one "
            f"too, e.g. SUM([{name}])."
        )
    return result


class _Parser:
    def __init__(self, tokens: list[Token], fields: Fields, stack: tuple[str, ...]):
        self.tokens = tokens
        self.fields = fields
        self.stack = stack
        self.position = 0
        self.depth = 0
        self.in_aggregate = False
        self.bare_columns: set[str] = set()  # columns used outside SUM/AVG/…

    # ---------- tokens ----------

    def peek(self) -> Token | None:
        return self.tokens[self.position] if self.position < len(self.tokens) else None

    def take(self) -> Token:
        token = self.peek()
        if token is None:
            raise FormulaError("The formula stops too early. Is something missing at the end?")
        self.position += 1
        return token

    def accept(self, text: str) -> bool:
        token = self.peek()
        if token is not None and token.kind in ("op", "word") and token.text.upper() == text:
            self.position += 1
            return True
        return False

    def expect(self, text: str, where: str) -> None:
        if not self.accept(text):
            token = self.peek()
            found = f"“{token.text}”" if token else "the end"
            raise FormulaError(f"Expected “{text}” {where}, found {found}.")

    # ---------- grammar (lowest priority first) ----------

    def expression(self) -> Compiled:
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise FormulaError("The formula is nested too deeply.")
        try:
            return self.or_part()
        finally:
            self.depth -= 1

    def or_part(self) -> Compiled:
        left = self.and_part()
        while self.accept("OR"):
            left = _logic("OR", left, self.and_part())
        return left

    def and_part(self) -> Compiled:
        left = self.not_part()
        while self.accept("AND"):
            left = _logic("AND", left, self.not_part())
        return left

    def not_part(self) -> Compiled:
        if self.accept("NOT"):
            inner = self.not_part()
            _need(inner, "yes/no", "after NOT")
            return _join(f"(NOT {inner.sql})", "yes/no", inner)
        return self.comparison()

    def comparison(self) -> Compiled:
        left = self.concat()
        token = self.peek()
        if token and token.kind == "op" and token.text in ("=", "<>", "!=", "<", ">", "<=", ">="):
            self.position += 1
            right = self.concat()
            return _compare(token.text, left, right)
        return left

    def concat(self) -> Compiled:
        left = self.additive()
        while self.accept("&"):
            right = self.additive()
            left = _join(
                f"concat(CAST({left.sql} AS VARCHAR), CAST({right.sql} AS VARCHAR))",
                "text",
                left,
                right,
            )
        return left

    def additive(self) -> Compiled:
        left = self.term()
        while (token := self.peek()) and token.kind == "op" and token.text in "+-":
            self.position += 1
            right = self.term()
            _need(left, "number", f"before “{token.text}”")
            _need(right, "number", f"after “{token.text}”")
            left = _join(
                f"({left.sql} {token.text} {right.sql})",
                "number",
                left,
                right,
                whole=left.whole and right.whole,
            )
        return left

    def term(self) -> Compiled:
        left = self.unary()
        while (token := self.peek()) and token.kind == "op" and token.text in "*/":
            self.position += 1
            right = self.unary()
            _need(left, "number", f"before “{token.text}”")
            _need(right, "number", f"after “{token.text}”")
            if token.text == "/":  # dividing by 0 gives a blank, not an error
                sql = f"(CAST({left.sql} AS DOUBLE) / NULLIF(CAST({right.sql} AS DOUBLE), 0))"
            else:
                sql = f"({left.sql} * {right.sql})"
            whole = token.text == "*" and left.whole and right.whole
            left = _join(sql, "number", left, right, whole=whole)
        return left

    def unary(self) -> Compiled:
        if self.accept("-"):
            inner = self.unary()
            _need(inner, "number", "after the minus sign")
            return _join(f"(- {inner.sql})", "number", inner, whole=inner.whole)
        if self.accept("+"):
            return self.unary()
        return self.primary()

    def primary(self) -> Compiled:
        token = self.take()
        if token.kind == "number":
            return Compiled(token.text, "number", whole="." not in token.text)
        if token.kind == "text":
            value = token.text[1:-1].replace('""', '"')
            return Compiled("'" + value.replace("'", "''") + "'", "text", literal=value)
        if token.kind == "field":
            return self.field(token.text[1:-1].replace("]]", "]"))
        if token.text == "(":
            inner = self.expression()
            self.expect(")", "to close the bracket")
            return inner
        if token.kind == "word":
            word = token.text.upper()
            if word in ("TRUE", "FALSE"):
                return Compiled(word, "yes/no")
            if word in FUNCTIONS:
                self.expect("(", f"after {word}")
                return self.function(word)
            close = difflib.get_close_matches(word, [*FUNCTIONS, *WORDS], n=1)
            hint = f" Did you mean {close[0]}?" if close else ""
            raise FormulaError(f"Unknown word “{token.text}”.{hint}")
        raise FormulaError(f"Unexpected “{token.text}”.")

    def field(self, typed: str) -> Compiled:
        name = self.fields.resolve(typed)
        if name in self.fields.columns:
            if not self.in_aggregate:
                self.bare_columns.add(name)
            kind = FORMULA_KIND[self.fields.columns[name]]
            whole = self.fields.columns[name] == "integer"
            return Compiled(f"src.{quote_name(name)}", kind, columns={name}, whole=whole)
        if name in self.stack:
            raise FormulaError(f"“[{name}]” uses itself, so it can't be worked out.")
        inner = self.fields.compiled.get(name)
        if inner is None:
            try:
                inner = compile_formula(
                    self.fields.calculated[name], self.fields, (*self.stack, name)
                )
            except FormulaError as error:
                raise FormulaError(f"“[{name}]” has a problem: {error}") from error
            self.fields.compiled[name] = inner
        if inner.is_total and self.in_aggregate:
            raise FormulaError(f"“[{name}]” is already a total, so it can't go inside SUM, AVG…")
        if not inner.is_total and not self.in_aggregate:
            self.bare_columns |= inner.columns
        return Compiled(
            f"({inner.sql})", inner.kind, inner.is_total, columns=inner.columns, whole=inner.whole
        )

    def arguments(self, word: str) -> list[Compiled]:
        args = []
        if self.accept(")"):
            return args
        while True:
            args.append(self.expression())
            if self.accept(")"):
                return args
            self.expect(",", f"between the parts of {word}( )")

    def function(self, word: str) -> Compiled:
        if word in AGGREGATES:
            return self.aggregate(word)
        args = self.arguments(word)
        if word == "TODAY":
            _count(word, args, 0, 0)
            return Compiled("current_date", "date")
        if word == "IF":
            _count(word, args, 2, 3)
            _need(args[0], "yes/no", "as the IF condition")
            return _case([(args[0], args[1])], args[2] if len(args) == 3 else None, word)
        if word == "IFS":
            if len(args) < 2:
                raise FormulaError(
                    'IFS needs a condition and a value, e.g. IFS([Qty] > 10, "Bulk", "Small").'
                )
            pairs, otherwise = _pairs(args)
            for condition, _ in pairs:
                _need(condition, "yes/no", "as an IFS condition")
            return _case(pairs, otherwise, word)
        if word == "SWITCH":
            return _switch(args)
        if word == "ROUND":
            _count(word, args, 1, 2)
            _need(args[0], "number", "in ROUND")
            places = "0"
            if len(args) == 2:
                if not re.fullmatch(r"\d", args[1].sql):
                    raise FormulaError("ROUND's second part must be a whole number from 0 to 9.")
                places = args[1].sql
            return _join(f"round({args[0].sql}, {places})", "number", *args, whole=places == "0")
        if word == "ABS":
            _count(word, args, 1, 1)
            _need(args[0], "number", "in ABS")
            return _join(f"abs({args[0].sql})", "number", *args, whole=args[0].whole)
        if word in ("YEAR", "MONTH"):
            _count(word, args, 1, 1)
            date = _as_date(args[0], f"in {word}")
            return _join(f"{word.lower()}({date.sql})", "number", *args, whole=True)
        if word == "DAYS_BETWEEN":
            _count(word, args, 2, 2)
            start = _as_date(args[0], "as the start date")
            end = _as_date(args[1], "as the end date")
            return _join(
                f"date_diff('day', CAST({start.sql} AS DATE), CAST({end.sql} AS DATE))",
                "number",
                start,
                end,
                whole=True,
            )
        if word in TEXT_TESTS:  # like "=": capitals don't matter; a blank counts as ""
            _count(word, args, 2, 2)
            _need(args[0], "text", f"as the first part of {word}")
            _need(args[1], "text", f"as the second part of {word}")
            text, part = (f"lower(coalesce({arg.sql}, ''))" for arg in args)
            sql = f"{TEXT_TESTS[word]}({text}, {part})"
            return _join(sql, "yes/no", *args)
        if word == "ISBLANK":
            _count(word, args, 1, 1)
            value = args[0].sql
            sql = (
                f"({value} IS NULL OR trim({value}) = '')"
                if args[0].kind == "text"
                else f"({value} IS NULL)"
            )
            return _join(sql, "yes/no", *args)
        raise FormulaError(f"Unknown word “{word}”.")  # pragma: no cover - FUNCTIONS covers all

    def aggregate(self, word: str) -> Compiled:
        if self.in_aggregate:
            raise FormulaError(f"{word} can't go inside another SUM, AVG, COUNT…")
        self.in_aggregate = True
        try:
            args = self.arguments(word)
        finally:
            self.in_aggregate = False
        _count(word, args, 1, 1)
        template, kinds = AGGREGATES[word]
        if kinds:
            _need(args[0], "number", f"in {word}")
        kind = args[0].kind if word in ("MIN", "MAX") else "number"
        whole = word in ("COUNT", "COUNT_UNIQUE") or (
            word in ("SUM", "MIN", "MAX") and args[0].whole
        )
        return Compiled(
            template.format(args[0].sql), kind, True, columns=args[0].columns, whole=whole
        )


# ---------- helpers ----------


def _join(sql: str, kind: str, *parts: Compiled, whole: bool = False) -> Compiled:
    return Compiled(
        sql,
        kind,
        any(part.is_total for part in parts),
        columns=set().union(*(part.columns for part in parts)) if parts else set(),
        whole=whole,
    )


def _need(part: Compiled, kind: str, where: str) -> None:
    if part.kind != kind:
        raise FormulaError(
            f"Expected {KIND_WORDS[kind]} {where}, but this is {KIND_WORDS[part.kind]}."
        )


def _count(word: str, args: list, low: int, high: int) -> None:
    if not low <= len(args) <= high:
        raise FormulaError(f"Wrong number of parts in {word}. Use: {FUNCTIONS[word]}")


def _as_date(part: Compiled, where: str) -> Compiled:
    """A date, or a "dd-mm-yyyy" / "yyyy-mm-dd" text value read as one."""
    if part.kind == "date":
        return part
    if part.literal is not None:
        for date_format, pattern in DATE_TEXT:
            if re.fullmatch(pattern, part.literal):
                try:
                    value = datetime.strptime(part.literal, date_format).date()
                except ValueError:
                    break
                return Compiled(f"DATE '{value.isoformat()}'", "date")
        raise FormulaError(f'“{part.literal}” is not a date. Write dates as "31-03-2026".')
    _need(part, "date", where)
    return part


def _logic(word: str, left: Compiled, right: Compiled) -> Compiled:
    _need(left, "yes/no", f"before {word}")
    _need(right, "yes/no", f"after {word}")
    return _join(f"({left.sql} {word} {right.sql})", "yes/no", left, right)


def _compare(sign: str, left: Compiled, right: Compiled) -> Compiled:
    sql_sign = "<>" if sign == "!=" else sign
    if left.kind == "date" or right.kind == "date":
        left, right = _as_date(left, f"before “{sign}”"), _as_date(right, f"after “{sign}”")
    if left.kind != right.kind:
        raise FormulaError(
            f"“{sign}” compares {KIND_WORDS[left.kind]} with {KIND_WORDS[right.kind]}. "
            "Both sides must be the same kind."
        )
    if left.kind == "text":  # like Excel: "mum" = "MUM"
        return _join(f"(lower({left.sql}) {sql_sign} lower({right.sql}))", "yes/no", left, right)
    return _join(f"({left.sql} {sql_sign} {right.sql})", "yes/no", left, right)


def _result_kind(results: list[Compiled], word: str) -> str:
    kinds = {result.kind for result in results}
    if len(kinds) > 1:
        names = " and ".join(sorted(KIND_WORDS[kind] for kind in kinds))
        raise FormulaError(f"The results in {word} mix {names}. Use one kind, e.g. all text.")
    return kinds.pop()


def _pairs(args: list[Compiled]) -> tuple[list, Compiled | None]:
    """(when, then) pairs, plus the last "otherwise" part when the count is odd."""
    otherwise = args[-1] if len(args) % 2 else None
    body = args[:-1] if otherwise else args
    return list(zip(body[0::2], body[1::2], strict=True)), otherwise


def _case(pairs: list, otherwise: Compiled | None, word: str) -> Compiled:
    results = [value for _, value in pairs] + ([otherwise] if otherwise else [])
    kind = _result_kind(results, word)
    whens = " ".join(f"WHEN {condition.sql} THEN {value.sql}" for condition, value in pairs)
    tail = f" ELSE {otherwise.sql}" if otherwise else ""
    parts = [part for pair in pairs for part in pair] + results
    whole = all(result.whole for result in results)
    return _join(f"(CASE {whens}{tail} END)", kind, *parts, whole=whole)


def _switch(args: list[Compiled]) -> Compiled:
    if len(args) < 3:
        raise FormulaError(f"Wrong number of parts in SWITCH. Use: {FUNCTIONS['SWITCH']}")
    subject, rest = args[0], args[1:]
    pairs, otherwise = _pairs(rest)
    for match, _ in pairs:
        if match.kind != subject.kind:
            raise FormulaError(
                f"SWITCH compares {KIND_WORDS[subject.kind]} with {KIND_WORDS[match.kind]}. "
                "Each match value must be the same kind as the field."
            )
    results = [result for _, result in pairs] + ([otherwise] if otherwise else [])
    kind = _result_kind(results, "SWITCH")

    def same(sql: str) -> str:
        return f"lower({sql})" if subject.kind == "text" else sql

    whens = " ".join(f"WHEN {same(match.sql)} THEN {result.sql}" for match, result in pairs)
    tail = f" ELSE {otherwise.sql}" if otherwise else ""
    parts = [subject, *(p for pair in pairs for p in pair), *results]
    whole = all(result.whole for result in results)
    return _join(f"(CASE {same(subject.sql)} {whens}{tail} END)", kind, *parts, whole=whole)
