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
    # Memory one user's data may use. A load that needs more stops with a clear message.
    "SESSION_MEMORY_MB": 1024,
    # Memory all users' data together may use. None = half of the server's RAM.
    "TOTAL_MEMORY_MB": None,
    "MEMORY_WARN_PERCENT": 80,  # show a "memory is high" banner above this share of the budget
    "IDLE_WARN_MINUTES": 25,
    "IDLE_CLEAR_MINUTES": 30,
}


def get(name: str):
    """One setting, from the host's MY_INSIGHTS block or the default."""
    return getattr(settings, "MY_INSIGHTS", {}).get(name, DEFAULTS[name])
