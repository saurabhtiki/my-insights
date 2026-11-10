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
class Dataset:
    dataset_id: int
    name: str
    table_name: str
    source_file: str
    row_count: int
    columns: list[ColumnProfile]

    @property
    def flagged_count(self) -> int:
        return sum(1 for column in self.columns if column.mixed)


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

    def new_dataset_id(self) -> int:
        dataset_id = self._next_id
        self._next_id += 1
        return dataset_id

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

    def remove_dataset(self, dataset_id: int) -> None:
        dataset = self.datasets.pop(dataset_id)
        self.connection.execute(f'DROP TABLE IF EXISTS "{dataset.table_name}"')
        self.refresh_memory()

    def refresh_memory(self) -> None:
        """Re-measure this database's memory. Call while holding `lock`."""
        try:
            row = self.connection.execute(
                "SELECT coalesce(sum(memory_usage_bytes), 0) FROM duckdb_memory()"
            ).fetchall()[0]
            self.memory_used_bytes = int(row[0])
        except duckdb.Error:
            logger.exception("Could not read DuckDB memory use")

    def close(self) -> None:
        with self.lock:
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
