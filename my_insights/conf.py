"""The add-on's settings. A host overrides any of them with one block in settings.py:

MY_INSIGHTS = {"MAX_FILE_MB": 20, "TOTAL_MEMORY_MB": 350}
"""

from django.conf import settings

DEFAULTS = {
    # Template the pages extend. It must have `title`, `extra_head` and `content` blocks and
    # load htmx 2, Alpine 3, DaisyUI 5 and django-datatable's CSS / JS.
    "BASE_TEMPLATE": "my_insights/base.html",
    "MAX_FILE_MB": 100,
    "MAX_FILES_PER_UPLOAD": 10,
    "MAX_ROWS": 2_000_000,
    "MAX_COLUMNS": 300,
    "PREVIEW_ROW_CAP": 100_000,
    # Most rows a CSV / Excel export holds. None = MAX_ROWS. (Excel itself stops at 1,048,575.)
    "EXPORT_ROW_CAP": None,
    # Most cells (rows × columns) an Excel export holds. Excel files are built in memory
    # (never on disk): 10 lakh cells take about 350 MB while building. Bigger → use CSV.
    "EXCEL_MAX_CELLS": 1_000_000,
    # Memory one user's data may use. A load that needs more stops with a clear message.
    "SESSION_MEMORY_MB": 1024,
    # Memory all users' data together may use. None = half of the server's RAM.
    "TOTAL_MEMORY_MB": None,
    "MEMORY_WARN_PERCENT": 80,  # show a "memory is high" banner above this share of the budget
    # "From API" import: fetch JSON from a web address.
    "ALLOW_API_IMPORT": True,
    # True = refuse addresses inside the company network or on this server (localhost, 10.x,
    # 192.168.x …) unless their host name or IP is in API_ALLOWED_HOSTS, e.g. ["erp.local"].
    # Turn on when users you don't fully trust can import: they could otherwise reach
    # internal services through this server.
    "API_BLOCK_INTERNAL": False,
    "API_ALLOWED_HOSTS": [],
    "API_TIMEOUT_SECONDS": 30,
    # Upload a SQLite database file (.db, .sqlite, .sqlite3) and pick its tables. Read in
    # memory only, never written to disk.
    "ALLOW_DB_IMPORT": True,
    # "From database": read tables from a live PostgreSQL / MySQL server. Needs the driver:
    # uv add "my-insights[postgres]" or "my-insights[mysql]". Every read is read-only.
    "ALLOW_DB_CONNECT": True,
    # [] = any server. Otherwise only these host names / IPs, e.g. ["erp.local", "10.0.0.5"].
    "DB_ALLOWED_HOSTS": [],
    "DB_CONNECT_SECONDS": 15,  # wait this long for the server to answer
    # Longest one read may run on the server (a table import or an own query), then it stops.
    "DB_QUERY_SECONDS": 300,
    "IDLE_WARN_MINUTES": 25,
    "IDLE_CLEAR_MINUTES": 30,
}


def get(name: str):
    """One setting, from the host's MY_INSIGHTS block or the default."""
    return getattr(settings, "MY_INSIGHTS", {}).get(name, DEFAULTS[name])
