# My Insights

Reusable Django add-on: users upload CSV / Excel files and explore them. Data lives in memory
only (DuckDB) and is never saved to disk or the database.

## Add it to a host app

1. Install: `uv add "my-insights @ git+<repo-url>"`. For "From database", add the driver you
   need: `"my-insights[postgres] @ git+<repo-url>"` and/or `[mysql]` (or both: `[postgres,mysql]`).
   Without one, users see a message naming what to install.
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
       "DB_ALLOWED_HOSTS": ["erp.local"],  # only these Postgres / MySQL servers ([] = any)
       # Recipes (optional): store them in the host instead of a .json download.
       "RECIPE_SAVE": "stock_audit.recipes.save",
       "RECIPE_LOAD": "stock_audit.recipes.load",
       # API / database datasets in a recipe are fetched again on Apply. True (default)
       # keeps headers, keys and the DB password in PLAIN TEXT in the recipe; False saves
       # them empty and asks the user on load.
       "RECIPE_KEEP_SECRETS": True,
       # "Ask AI" (default on): users type an OpenAI-style address, their own key and a model.
       "ALLOW_AI": True,
       "AI_TIMEOUT_SECONDS": 60,
   }
   ```
   ⚠ With `RECIPE_KEEP_SECRETS` on, a recipe file holds passwords and API keys as plain text:
   keep it inside the company, like your `.env` file.
   Recipe hooks, e.g. in `stock_audit/recipes.py`:
   ```python
   def save(request, recipe: dict, name: str) -> str:
       saved = Recipe.objects.create(owner=request.user, name=name, body=recipe)
       return f"Saved “{name}”."            # shown to the user

   def load(request, recipe_id: str) -> dict | None:
       row = Recipe.objects.filter(owner=request.user, pk=recipe_id).first()
       return row.body if row else None     # None → "That recipe was not found."
   ```
   Your own page lists the user's recipes and links each to
   `{% url 'my_insights:recipe_load' %}?id=<pk>`.
5. Tailwind: add `@source ".../my_insights";` (the whole package folder: templates and the
   `.py` files, where the tile colour classes live) to the host's Tailwind input file.
6. Run the server as **one process** (threads are fine), e.g. waitress. Users' data lives in
   that process's memory.

Logging out (Django's `user_logged_out` signal) wipes that user's data.

**Ask AI** sends the AI service: dataset names, column names and types, the user's
"Notes for AI", the question and the last 5 questions with the AI's answers. Never a value or
a row. The key stays in the user's in-memory session only. The address follows
`API_BLOCK_INTERNAL` / `API_ALLOWED_HOSTS`, like Import from API.

## Work on it

```bash
uv sync
uv run pytest
uv run python manage.py migrate
uv run python manage.py serve --port 8010     # demo site: http://127.0.0.1:8010
.\tools\build_css.ps1                         # after changing CSS classes in templates
```
