"""Each browser session gets its own in-memory DuckDB database, kept in this server process.

Nothing is written to disk: DuckDB runs with no temp folder, so a job that needs more memory
than allowed stops with a message instead of spilling to a file. The server must run as ONE
process (many threads are fine), because the data lives in that process's memory.
"""

import logging
import secrets
import threading
import time
from dataclasses import dataclass, field

import duckdb
import psutil

from . import conf

logger = logging.getLogger(__name__)

SESSION_KEY = "my_insights_workspace"
MB = 1024 * 1024
# JSON: the row number column of a main dataset, and its children's link back to it.
ROW_COLUMN = "_row"
PARENT_COLUMN = "_parent_row"


class ServerBusy(Exception):
    """Too much memory in use across all users to take a new upload right now."""


@dataclass
class ColumnProfile:
    name: str
    kind: str  # integer | number | date | datetime | boolean | text
    blanks: int
    samples: list[str] = field(default_factory=list)
    date_format: str = ""  # e.g. "dd-mm-yyyy", for date columns
    note: str = ""  # why a column is text when it looks like something else
    mixed: bool = False  # mostly one type, but some values don't fit, so kept as text
    mixed_examples: list[str] = field(default_factory=list)  # e.g. ['"N/A" (data row 57)']


@dataclass
class LinkStep:
    """One lookup list joined in. `linked_to` is 0 for the main dataset, or k for the k-th
    lookup (1-based), so a lookup can hang off an earlier one (Item → Sub-category)."""

    lookup_id: int
    linked_to: int = 0
    pairs: list[tuple[str, str]] = field(default_factory=list)  # (linked-to column, lookup column)
    join_type: str = "left"  # left | inner | full


@dataclass
class LinkSpec:
    """How a combined dataset is built: a main dataset plus lookup lists. Holds no data."""

    main_id: int
    steps: list[LinkStep] = field(default_factory=list)
    ignore_case: bool = True

    def source_ids(self) -> list[int]:
        return [self.main_id, *(step.lookup_id for step in self.steps)]


@dataclass
class CalculatedField:
    """A column made from a formula, e.g. Profit = [Sales] - [Cost]. Belongs to a dataset."""

    name: str
    formula: str


@dataclass
class GroupItem:
    """One field in the Group by box. `date_part` (month, fy…) is used for date fields."""

    key: str
    column: str  # a column or calculated field
    date_part: str = ""


@dataclass
class ValueItem:
    """One entry in the Values box, e.g. Sum of Amount."""

    key: str
    column: str  # a column or calculated field; "*" = Count of rows
    agg: str  # sum | avg | median | count | count_unique | min | max | rows | formula
    label: str = ""  # the user's own name for it, e.g. "Revenue"
    number_format: str = ""  # "0" / "1" / "2" decimals, "r0" / "r2" with ₹; "" = default


@dataclass
class FilterItem:
    """One filter. On a field (row filter) or on a value's total (`value_key` set)."""

    key: str
    column: str = ""
    value_key: str = ""
    op: str = ""
    values: list[str] = field(default_factory=list)
    any_of: bool = False  # in the "Any one of these" box (OR) instead of "All of these" (AND)


@dataclass
class SortItem:
    key: str
    target: str  # a group's or value's key
    descending: bool = False


@dataclass
class QuerySpec:
    """A question asked of one dataset. Holds no data."""

    dataset_id: int
    groups: list[GroupItem] = field(default_factory=list)
    values: list[ValueItem] = field(default_factory=list)
    filters: list[FilterItem] = field(default_factory=list)
    sorts: list[SortItem] = field(default_factory=list)
    top_n: int | None = None
    # A group whose values become columns ("Across"), e.g. Month → Apr 2026 | May 2026 | …
    across: GroupItem | None = None


@dataclass
class SavedQuery:
    query_id: int
    name: str
    spec: QuerySpec


@dataclass
class ValueStyle:
    """How one value is drawn on a chart. Empty = the chart's default."""

    kind: str = ""  # bar | line (combo charts)
    axis: str = ""  # left | right
    color: str = ""  # "#1f77b4"


@dataclass
class ChartSpec:
    """How to draw a query's result. Fields are the query's own item keys (GroupItem.key,
    ValueItem.key), so renaming a heading never breaks a chart. Holds no data."""

    chart_type: str = "column"
    x: str = ""  # a group's key: the X axis (pie: the slices)
    series: str = ""  # a group's key: one colour per value, e.g. one line per Branch
    hidden: list[str] = field(default_factory=list)  # value keys left off the chart
    styles: dict[str, ValueStyle] = field(default_factory=dict)  # value key → style
    title: str = ""
    legend: bool = True
    labels: bool = False  # numbers written on the bars / points
    x_title: str = ""
    y_title: str = ""
    y2_title: str = ""
    theme: str = "default"
    sort: str = ""  # "" = the query's order | desc | asc (by the first value)
    percent: bool = False  # stacked charts: each bar adds up to 100%
    top_n: int | None = None  # None = the chart type's default (30 bars, 10 slices)
    # The headings when the chart was last saved, so a missing field can be named.
    names: dict[str, str] = field(default_factory=dict)


@dataclass
class SavedChart:
    """A chart on a saved query (`query_id`), or on a dataset with its own hidden query
    (`own_query`). Exactly one of the two is set."""

    chart_id: int
    name: str
    spec: ChartSpec
    query_id: int | None = None
    own_query: QuerySpec | None = None


@dataclass
class NoteValue:
    """A live number inside a note: a saved query's value, worked out like a KPI.
    Shown where the note's text has [[label]], e.g. [[Avg of Selling price]] → ₹ 1,245.50."""

    query_id: int
    value_key: str
    label: str


@dataclass
class DashboardTile:
    """One box on a dashboard. Chart / Table / KPI tiles point at a saved chart or query (by
    id, not a copy), so editing the chart changes the tile. Holds no data."""

    tile_id: int
    kind: str  # chart | table | kpi | note
    chart_id: int | None = None
    query_id: int | None = None  # table and KPI tiles
    value_key: str = ""  # KPI: which value of the query (ValueItem.key)
    title: str = ""  # KPI title ("" = the value's name)
    subtitle: str = ""  # KPI: a small line under the number
    text: str = ""  # note
    values: list[NoteValue] = field(default_factory=list)  # note: live values in the text
    color: str = ""  # KPI / note: "" (plain) or a TILE_COLORS key, e.g. green
    tone: str = "soft"  # soft (light tint) | strong (full colour)
    width: str = "half"  # third | half | two_thirds | full
    height: str = "short"  # short | tall


@dataclass
class Dashboard:
    """Tiles in order (the list order is the layout) plus filters for every tile."""

    dashboard_id: int
    name: str
    tiles: list[DashboardTile] = field(default_factory=list)
    filters: list[FilterItem] = field(default_factory=list)
    next_tile_id: int = 1

    def new_tile_id(self) -> int:
        tile_id = self.next_tile_id
        self.next_tile_id += 1
        return tile_id

    def tile(self, tile_id: int) -> DashboardTile | None:
        return next((tile for tile in self.tiles if tile.tile_id == tile_id), None)


@dataclass
class ApiSource:
    """Where an API dataset came from, so Refresh can fetch it again. Memory only: never saved,
    exported or put in a recipe (headers may hold a key)."""

    url: str
    method: str = "GET"  # GET | POST
    headers: str = ""  # one "Name: value" per line, as typed
    body: str = ""  # JSON, POST only
    rows_at: str = ""  # e.g. "data.results"; "" = every list found
    name: str = ""  # the name given at import, e.g. "sales" (new lists on Refresh use it)


@dataclass
class DbServer:
    """A Postgres / MySQL server the user connected to. Memory only: never saved, exported,
    logged or put in a recipe. `repr` hides the password."""

    kind: str  # "postgres" | "mysql"
    host: str
    port: int
    database: str
    user: str
    password: str = field(default="", repr=False)
    require_ssl: bool = False

    @property
    def label(self) -> str:
        """e.g. erp on 192.168.1.20"""
        return f"{self.database} on {self.host}"

    @property
    def kind_title(self) -> str:
        return {"postgres": "PostgreSQL", "mysql": "MySQL"}.get(self.kind, self.kind)

    @property
    def place(self) -> tuple:
        """Which server, database and user (not the password): one pick list each."""
        return (self.kind, self.host.casefold(), self.port, self.database, self.user)


@dataclass
class DbSource:
    """Where a server dataset came from, so Refresh can read it again (memory only)."""

    server: DbServer
    select: str  # the SQL that is run, e.g. SELECT * FROM "sales"."orders"


@dataclass
class Dataset:
    dataset_id: int
    name: str
    table_name: str  # a table for an uploaded file or a query's result, a view for a combined one
    source_file: str  # file name, "Sales, Items" for a combined dataset, or the query's name
    row_count: int
    columns: list[ColumnProfile]
    link: LinkSpec | None = None  # set for a combined dataset
    query_id: int | None = None  # set for a dataset made from a saved query's result
    calculated: list[CalculatedField] = field(default_factory=list)
    # JSON: a list of objects inside each row becomes a child dataset, e.g. "orders – items"
    # (parent_id = orders, child_path = "items"). Removing the parent removes its children.
    parent_id: int | None = None
    child_path: str = ""
    list_path: str = ""  # JSON: where its rows were, e.g. "data.orders" ("" = top level)
    # Set on each list imported from an API. Lists from one import share the same object,
    # so Refresh updates them together.
    api_source: ApiSource | None = None
    db_source: DbSource | None = None  # set on each table / own query read from a server
    notice: str = ""  # e.g. "Not in the latest API reply"
    # Database foreign keys: (column, lookup dataset id, lookup column), shown as top suggestions
    foreign_keys: list[tuple[str, int, str]] = field(default_factory=list)

    @property
    def flagged_count(self) -> int:
        return sum(1 for column in self.columns if column.mixed)


@dataclass
class DbTable:
    """A table or view listed in a database's pick list."""

    name: str  # as shown, e.g. "orders" or "sales.orders"
    kind: str  # "table" | "view"
    row_count: int | None  # None = not known (a server's view, or never analysed)
    columns: list[str] = field(default_factory=list)
    select: str = ""  # the SQL that reads it, e.g. SELECT * FROM "sales"."orders"
    estimated: bool = False  # True: row_count is the server's estimate ("about 12,000 rows")


@dataclass
class PendingDb:
    """A database waiting for the user to tick tables. Memory only. Either an uploaded SQLite
    file (an in-memory, read-only sqlite3 connection, closed on Import, Cancel or when the
    workspace goes) or a server (`server` set, no connection kept: each step connects again)."""

    token: str
    file_name: str  # "shop.db", or the server's label "erp on 192.168.1.20"
    connection: object | None  # sqlite3.Connection; None for a server
    tables: list[DbTable]
    server: DbServer | None = None

    @property
    def stem(self) -> str:
        """shop.db → shop; a server → its database name, e.g. erp"""
        return self.server.database if self.server else self.file_name.rsplit(".", 1)[0]


class Workspace:
    """One user's data: a private DuckDB database plus the list of loaded datasets."""

    def __init__(self, memory_mb: int):
        self.connection = duckdb.connect(":memory:")
        self.connection.execute(f"SET memory_limit = '{int(memory_mb)}MB'")
        self.connection.execute("SET temp_directory = ''")  # never spill data to disk
        self.memory_limit_bytes = int(memory_mb) * MB
        # One request at a time per user; DuckDB connections are not shared between threads.
        self.lock = threading.RLock()
        self.datasets: dict[int, Dataset] = {}
        self._next_id = 1
        self.last_seen = time.monotonic()
        # Updated after each load / remove, so other users can read it without waiting.
        self.memory_used_bytes = 0
        # The link screens' latest checks, for their preview tables: {draft id: LinkCheck}.
        # One per open link screen, so two browser tabs don't mix up their previews.
        self.link_drafts: dict = {}
        self.suggestion_cache: tuple = ((), [])
        self.queries: dict[int, SavedQuery] = {}
        self._next_query_id = 1
        # The query screens' latest questions, for their result tables: {draft id: QuerySpec}.
        self.query_drafts: dict = {}
        # The latest result shown: (draft id, spec, plan, rows), so sort / page clicks in its
        # table don't run the query again.
        self.query_result_cache: tuple | None = None  # (dataset ids it was made for, suggestions)
        self.charts: dict[int, SavedChart] = {}
        self._next_chart_id = 1
        # Ids are never reused, so a tile pointing at a removed chart can't show another one.
        self.dashboards: dict[int, Dashboard] = {}
        self._next_dashboard_id = 1
        self.notice = ""  # a problem to show on the next page (after a redirect)
        self.done_notice: list[str] = []  # good news for the next page, e.g. a recipe applied
        # A recipe read but not applied yet (recipe.read_recipe's result), waiting for the
        # user to match its datasets.
        self.pending_recipe: dict | None = None
        self.pending_databases: dict[str, PendingDb] = {}
        self._next_pending = 1

    def new_dashboard_id(self) -> int:
        dashboard_id = self._next_dashboard_id
        self._next_dashboard_id += 1
        return dashboard_id

    def new_chart_id(self) -> int:
        chart_id = self._next_chart_id
        self._next_chart_id += 1
        return chart_id

    def chart_query(self, chart: SavedChart) -> QuerySpec | None:
        """The question a chart draws: its saved query's, or its own."""
        if chart.query_id is not None:
            saved = self.queries.get(chart.query_id)
            return saved.spec if saved else None
        return chart.own_query

    def charts_of_query(self, query_id: int) -> list[SavedChart]:
        return [chart for chart in self.charts.values() if chart.query_id == query_id]

    def charts_of(self, dataset_ids) -> list[SavedChart]:
        """Every chart drawing from these datasets (through a saved query or its own)."""
        ids = set(dataset_ids)
        found = []
        for chart in self.charts.values():
            spec = self.chart_query(chart)
            if spec is not None and spec.dataset_id in ids:
                found.append(chart)
        return found

    def remove_query(self, query_id: int) -> list[str]:
        """Remove a saved query, its charts and any dataset made from it (with everything
        built on that). Returns the names removed, the query's own name not included."""
        removed = []
        for dataset in [d for d in self.datasets.values() if d.query_id == query_id]:
            if dataset.dataset_id in self.datasets:  # not already gone with an earlier one
                removed += self.remove_dataset(dataset.dataset_id)
        charts = self.charts_of_query(query_id)
        for chart in charts:
            del self.charts[chart.chart_id]
        self.queries.pop(query_id, None)
        return removed + [chart.name for chart in charts]

    def new_dataset_id(self) -> int:
        dataset_id = self._next_id
        self._next_id += 1
        return dataset_id

    def new_query_id(self) -> int:
        query_id = self._next_query_id
        self._next_query_id += 1
        return query_id

    def queries_of(self, dataset_ids) -> list[SavedQuery]:
        ids = set(dataset_ids)
        return [query for query in self.queries.values() if query.spec.dataset_id in ids]

    def unique_name(self, wanted: str, ignore_id: int | None = None) -> str:
        taken = {
            dataset.name.casefold()
            for dataset in self.datasets.values()
            if dataset.dataset_id != ignore_id
        }
        name, number = wanted, 2
        while name.casefold() in taken:
            name = f"{wanted} ({number})"
            number += 1
        return name

    def sources_of(self, dataset: Dataset) -> list[int]:
        """The datasets this one is built from: a combined dataset's parts, or the dataset
        of the query a query-dataset was made from, a JSON child's parent. [] for an uploaded
        file."""
        if dataset.parent_id is not None:
            return [dataset.parent_id]
        if dataset.link is not None:
            return dataset.link.source_ids()
        if dataset.query_id is not None:
            query = self.queries.get(dataset.query_id)
            return [query.spec.dataset_id] if query else []
        return []

    def dependants(self, dataset_id: int) -> list[Dataset]:
        """Datasets built (directly or through others) on this one, combined ones and ones
        made from a query on it, each listed after the datasets it is built from."""
        found: dict[int, Dataset] = {}
        changed = True
        while changed:
            changed = False
            for dataset in self.datasets.values():
                if dataset.dataset_id in found or dataset.dataset_id == dataset_id:
                    continue
                sources = set(self.sources_of(dataset))
                if dataset_id in sources or sources & found.keys():
                    found[dataset.dataset_id] = dataset
                    changed = True
        ordered: dict[int, Dataset] = {}
        while len(ordered) < len(found):
            ready = [
                dataset
                for key, dataset in found.items()
                if key not in ordered
                and set(self.sources_of(dataset)) & found.keys() <= ordered.keys()
            ]
            if not ready:  # a loop; never expected, as the screens refuse it
                logger.error("Datasets depend on each other: %s", list(found))
                ready = [dataset for key, dataset in found.items() if key not in ordered]
            ordered.update((dataset.dataset_id, dataset) for dataset in ready)
        return list(ordered.values())

    def built_on_query(self, query_id: int) -> set[int]:
        """Ids of the datasets made from this query and everything built on them: the
        query may not ask about any of these (it would be built on itself)."""
        ids = set()
        for dataset in self.datasets.values():
            if dataset.query_id == query_id:
                ids.add(dataset.dataset_id)
                ids |= {d.dataset_id for d in self.dependants(dataset.dataset_id)}
        return ids

    def remove_dataset(self, dataset_id: int) -> list[str]:
        """Remove a dataset, every combined dataset built on it and their queries.
        Returns the names removed."""
        removed = [*reversed(self.dependants(dataset_id)), self.datasets[dataset_id]]
        removed_ids = [dataset.dataset_id for dataset in removed]
        queries = self.queries_of(removed_ids)
        charts = self.charts_of(removed_ids)
        self.query_result_cache = None
        for chart in charts:
            del self.charts[chart.chart_id]
        for query in queries:
            del self.queries[query.query_id]
        for dataset in removed:
            kind = "VIEW" if dataset.link else "TABLE"
            self.connection.execute(f'DROP {kind} IF EXISTS "{dataset.table_name}"')
            del self.datasets[dataset.dataset_id]
        self.refresh_memory()
        return (
            [dataset.name for dataset in removed]
            + [query.name for query in queries]
            + [chart.name for chart in charts]
        )

    def refresh_memory(self) -> None:
        """Re-measure this database's memory. Call while holding `lock`."""
        try:
            row = self.connection.execute(
                "SELECT coalesce(sum(memory_usage_bytes), 0) FROM duckdb_memory()"
            ).fetchall()[0]
            self.memory_used_bytes = int(row[0])
        except duckdb.Error:
            logger.exception("Could not read DuckDB memory use")

    def add_pending(
        self, file_name: str, connection, tables: list, server: DbServer | None = None
    ) -> PendingDb:
        token = str(self._next_pending)
        self._next_pending += 1
        pending = PendingDb(token, file_name, connection, tables, server)
        self.pending_databases[token] = pending
        return pending

    def drop_pending(self, token: str) -> None:
        pending = self.pending_databases.pop(token, None)
        if pending is not None and pending.connection is not None:
            pending.connection.close()

    def close(self) -> None:
        with self.lock:
            for token in list(self.pending_databases):
                self.drop_pending(token)
            self.datasets.clear()
            self.connection.close()


class WorkspaceStore:
    """All workspaces in this process, keyed by a random id kept in the user's session."""

    def __init__(self):
        self._workspaces: dict[str, Workspace] = {}
        self._lock = threading.Lock()

    def create(self) -> tuple[str, Workspace]:
        workspace_id = secrets.token_urlsafe(16)
        workspace = Workspace(conf.get("SESSION_MEMORY_MB"))
        with self._lock:
            self._workspaces[workspace_id] = workspace
        logger.info("Workspace %s… created", workspace_id[:6])
        return workspace_id, workspace

    def get(self, workspace_id: str | None) -> Workspace | None:
        if not workspace_id:
            return None
        with self._lock:
            workspace = self._workspaces.get(workspace_id)
        if workspace is not None:
            workspace.last_seen = time.monotonic()
        return workspace

    def discard(self, workspace_id: str | None) -> bool:
        with self._lock:
            workspace = self._workspaces.pop(workspace_id, None) if workspace_id else None
        if workspace is None:
            return False
        workspace.close()
        logger.info("Workspace %s… cleared", workspace_id[:6])
        return True

    def sweep_idle(self) -> int:
        """Delete workspaces nobody touched for IDLE_CLEAR_MINUTES. Returns how many."""
        limit = conf.get("IDLE_CLEAR_MINUTES") * 60
        now = time.monotonic()
        with self._lock:
            idle = [key for key, ws in self._workspaces.items() if now - ws.last_seen > limit]
        for workspace_id in idle:
            self.discard(workspace_id)
        return len(idle)

    def total_memory_bytes(self) -> int:
        with self._lock:
            workspaces = list(self._workspaces.values())
        return sum(workspace.memory_used_bytes for workspace in workspaces)

    def __len__(self) -> int:
        return len(self._workspaces)


store = WorkspaceStore()


def memory_status() -> dict:
    """How full the memory budget is: {"used_mb", "budget_mb", "percent", "busy", "warn"}."""
    # Only My Insights' own data counts, so other programs on the server never block uploads.
    budget_mb = conf.get("TOTAL_MEMORY_MB") or psutil.virtual_memory().total / MB / 2
    used_mb = store.total_memory_bytes() / MB
    percent = 100 * used_mb / budget_mb
    return {
        "used_mb": round(used_mb, 1),
        "budget_mb": round(budget_mb),
        "percent": round(percent),
        "busy": percent >= 100,
        "warn": percent >= conf.get("MEMORY_WARN_PERCENT"),
    }


def check_capacity() -> None:
    """Refuse new uploads when the memory budget is used up. Raises ServerBusy."""
    store.sweep_idle()
    status = memory_status()
    if status["busy"]:
        logger.warning("Upload refused, memory budget full: %s", status)
        raise ServerBusy(
            "The server is busy (its memory is full). Please try again in a few minutes."
        )


# ---------- session glue ----------


def current_workspace(request, create: bool = False) -> tuple[Workspace | None, bool]:
    """This browser's workspace, and whether an older one was wiped for being idle."""
    store.sweep_idle()
    workspace_id = request.session.get(SESSION_KEY)
    workspace = store.get(workspace_id)
    expired = workspace_id is not None and workspace is None
    if expired:
        del request.session[SESSION_KEY]
    if workspace is None and create:
        workspace_id, workspace = store.create()
        request.session[SESSION_KEY] = workspace_id
    return workspace, expired


def clear_workspace(request) -> bool:
    return store.discard(request.session.pop(SESSION_KEY, None))


def forget_on_logout(sender, request, user, **kwargs):
    """Signal receiver: the host app logged a user out, so wipe their data."""
    if request is not None and hasattr(request, "session"):
        clear_workspace(request)
