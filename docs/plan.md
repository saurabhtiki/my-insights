# Plan: Phase 9a, Recipes: save and reuse your setup (my-insights)

**Status: done.** 443 tests pass (31 new in `tests/test_recipe.py`). (Phase 8c plan: `plan_phase8c.md`.)
Added after: the match screen lists each recipe dataset's needed columns with their type, and offers an empty CSV template with those headings (`recipe/template/?id=`). Extra columns in a file are ignored.

Built as agreed, with these changes:
- **"Needs" are worked out when saving**: each file dataset in the recipe lists the columns
  the setup reads from it (through formulas, links, combined datasets, queries and dashboard
  filters). The match screen checks those; a dataset nothing uses can be skipped.
- **Double check at Apply**: after building, every query is planned again; any problem
  undoes everything (old work kept), so a wrong report can't slip through.
- **Guessing**: same name, then the name without "(2)" / extension, then a similar name or
  similar columns with every needed column (e.g. "sales" → "sales_dec").
- **No demo hook**: the demo uses the .json download; the hooks are covered by tests with
  fake host functions.
- CSS rebuilt (`tools/build_css.ps1`) for the new `file-input` / `select` classes.

- **Code review fixes**: a recipe can come from anyone, so chart / tile settings are checked
  like the screens check them (field keys must look like `g1`, colours like `#1f77b4`, unknown
  chart types / widths / tones reset). Without this a crafted colour could run script in the
  page. Deep nesting, odd picks and odd host values give messages, not errors.
- **Renamed lookup columns**: "Name (items)" becomes "Name (items_dec)" when items is matched to
  items_dec; queries, formulas, links and dashboard filters follow the new name.
- **Types**: a needed column read as another type (e.g. Qty now text) blocks Apply:
  "In “sales”: “Qty” is now text (was a whole number)."
- **Two tabs**: the match form carries the recipe's token; picks for an older recipe are refused.

Known gaps: loaded datasets the recipe doesn't use keep their own calculated fields. Charts
already broken when saved stay flagged.

## Context
Today everything a user builds (links, calculated fields, queries, charts, dashboards) is lost on
logout. `project.md` promises **recipes**: a small JSON with the setup but **no data**, stored by
the host app (Stock Audit) through hooks. Recipes are split in two (agreed):
- **9a (this plan)**: save / load a recipe, match it to the data, works for uploaded files.
- **9b (later)**: remember API / database sources, secrets encrypted (host opt-in), one-click re-import.

## Goal (one line)
Click **Save recipe** → get `stock_review.json` (or the host saves it). Next month upload the new
files, click **Load recipe**, confirm the matches, and all queries, charts and dashboards come back.

Example:
- Nov: upload `sales_nov.csv` (dataset "sales") + `items.xlsx`, link them, build "Margin by city"
  chart and "Monthly review" dashboard → **Save recipe**.
- Dec: upload `sales_dec.csv` → **Load recipe** → match screen:
  `Recipe "sales" → [sales_dec ▼]` (guessed), `Recipe "items" → [items ▼]` → **Apply**.
- If `sales_dec` has no `Cost` column: the match row shows a red "missing: Cost", **Apply** stays
  off, and nothing changes until a file with `Cost` is uploaded or picked.

## Decisions (agreed)
| # | Topic | Decision |
|---|-------|----------|
| 1 | Scope | Whole workspace: links, calculated fields, query-datasets, queries, charts, all dashboards. |
| 2 | Matching | Auto-guess by name, then a confirm screen with a dropdown per recipe dataset. |
| 3 | Storage | Host hooks (setting) **plus** plain `.json` download / upload when the host sets none. |
| 4 | Load from host | Host shows its own list; its link opens `/recipe/load/?id=7`; add-on calls the host's load hook. |
| 5 | Apply | **Replace** current queries / charts / dashboards / combined datasets (warning first). Uploaded data stays. |
| 6 | Missing columns | **Stop with an error.** Apply is refused until every column the recipe uses is there, so no wrong report is ever shown. Message names each gap, e.g. "“sales” is missing: Cost, Qty. Upload a file with these columns or pick another dataset." Nothing is changed when refused. Extra columns are fine. |
| 7 | API / DB | 9a: recipe holds **no** URLs, hosts, headers, SQL or secrets; those datasets match by name like files. 9b adds sources with encrypted secrets (`RECIPE_KEEP_SECRETS`, off by default). |

## What we build
1. **`my_insights/recipe.py` (new)**
   - `to_recipe(workspace) -> dict`: `{"my_insights_recipe": 1, "sources": [...], "combined": [...],
     "query_datasets": [...], "queries": [...], "charts": [...], "dashboards": [...]}`.
     Sources = uploaded / API / DB / JSON-child datasets: name + column names/kinds + calculated
     fields only. Ids are recipe-local. Never any rows, samples, `api_source`, `db_source`.
   - `read_recipe(raw: bytes) -> dict`: size limit (`RECIPE_MAX_KB`), valid JSON, version, known
     keys and types; specific messages ("This file is not a My Insights recipe", "Recipe version 2
     needs a newer My Insights"). Logged before shown.
   - `guess_matches(recipe, workspace)`: exact name (casefold), then name without " (2)" / file
     extension; each loaded dataset used once.
   - `missing_columns(recipe, matches)`: per recipe dataset, the columns it **uses** (in links,
     calculated-field formulas, queries, filters, dashboard filters) that the matched dataset lacks.
     Any gap, or any recipe dataset left unmatched → `apply_recipe` refuses (checked again on the
     server at Apply, not just in the screen).
   - `apply_recipe(workspace, recipe, matches) -> list[str]` (report lines): clear old derived
     datasets (`Workspace.remove_dataset`), queries, charts, dashboards; copy calculated fields;
     rebuild combined datasets with `linking.save_link`; query-datasets with
     `query_dataset.make_query_dataset`; then queries, charts, dashboards with new ids (id map
     recipe → workspace). Dataclasses ↔ dict via small `asdict` / build helpers.
2. **`workspace.py`**: `Workspace.pending_recipe: dict | None` (waiting for the match screen).
3. **`recipe_views.py` (new) + `urls.py`**
   - `recipe_save` (POST, name field): host `RECIPE_SAVE` hook → message; else download `<name>.json`.
   - `recipe_upload` (POST file) and `recipe_load` (GET `?id=`, host `RECIPE_LOAD` hook) → store
     `pending_recipe` → match screen.
   - `recipe_match` (GET screen / POST Apply), `recipe_cancel`. Same CSRF + workspace lock.
   - Hook errors: logged, user sees "Could not save / load the recipe: …".
4. **Templates (HTMX, Alpine, DaisyUI)**: "Recipe" menu in top bar (Save recipe dialog with name,
   Load recipe file box); match screen partial (one row per recipe dataset, dropdown, red "missing:
   Cost, Qty" badge, Apply disabled with a tooltip saying why, "Upload more files" link); home banner "Recipe waiting: match now". Tooltips
   on every button. After Apply: report list on the home page.
5. **`conf.py`**: `RECIPE_SAVE` (dotted path or None), `RECIPE_LOAD` (dotted path or None),
   `RECIPE_MAX_KB` (512). Hook signatures: `save(request, recipe: dict, name: str) -> str`
   (message shown), `load(request, recipe_id: str) -> dict`.
6. **Demo**: a tiny demo hook (saves to demo's own model or a dict) to try the host path.
7. **Docs**: archive current `plan.md` → `plan_phase8c.md`; this plan → `plan.md`; README settings
   and hook example; `project.md` recipe section (match screen, replace).

## Reuse
`linking.save_link` / `refresh_view`, `query_dataset.make_query_dataset`, `dashboard.create` /
`add_tile`, `Workspace.remove_dataset` / `unique_name`, existing broken messages in
`querying.py` (plan.problems), `charting.broken_reason`, `dashboard.py:495` KPI message.

## Tests (`tests/test_recipe.py`)
- Round trip: build workspace → recipe → fresh workspace with same files → apply → same queries,
  charts, dashboards, links, calculated fields (ids remapped, tiles point at right items).
- Recipe holds no data: sample values, API URL/headers, DB host/password never in the JSON.
- Matching: exact, case, "(2)", unmatched left empty; missing columns listed.
- Apply replaces old work; uploaded data kept.
- Missing used column (in query, formula, link, filter) → Apply refused with a message naming
  dataset + columns; workspace unchanged. Missing unused column → allowed. Unmatched → refused.
- Bad files: not JSON, too big, wrong version, wrong types → specific messages.
- Hooks: fake save/load functions called; hook error → friendly message; no hooks → download.
- Views: CSRF, lock, cancel. Run `uv run pytest`; browser check in preview (save, upload new file, apply).

## End of phase
Update "Current phase" in `my-insights/CLAUDE.md`, then `/code-review` and `/simplify`.
