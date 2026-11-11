# My Insights

Reusable Django add-on: users upload CSV / Excel files and explore them. Data lives in memory
only (DuckDB) and is never saved to disk or the database.

## Add it to a host app

1. Install: `uv add "my-insights @ git+<repo-url>"`
2. `INSTALLED_APPS += ["django.contrib.humanize", "django_datatable", "my_insights"]`
3. `urlpatterns += [path("insights/", include("my_insights.urls"))]`
4. Settings (all optional; defaults in `my_insights/conf.py`):
   ```python
   MY_INSIGHTS = {
       "BASE_TEMPLATE": "base.html",  # needs title / extra_head / content blocks, htmx 2,
       # Alpine 3, DaisyUI 5 and django-datatable's CSS/JS
       "MAX_FILE_MB": 20,
       "MAX_ROWS": 500_000,
       "SESSION_MEMORY_MB": 150,
       "TOTAL_MEMORY_MB": 350,  # e.g. a 1 GB server
   }
   ```
5. Tailwind: add `@source ".../my_insights/templates";` to the host's Tailwind input file.
6. Run the server as **one process** (threads are fine), e.g. waitress. Users' data lives in
   that process's memory.

Logging out (Django's `user_logged_out` signal) wipes that user's data.

## Work on it

```bash
uv sync
uv run pytest
uv run python manage.py migrate
uv run python manage.py serve --port 8010     # demo site: http://127.0.0.1:8010
.\tools\build_css.ps1                         # after changing CSS classes in templates
```
