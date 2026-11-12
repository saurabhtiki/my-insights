# Plan: Phase 9b, Recipes remember API / database sources (my-insights)

**Status: done.** 454 tests pass (11 new in `tests/test_recipe_sources.py`). (Phase 9a plan: `plan_phase9a.md`.)
Built as planned; the "no data loaded, upload first" warning is hidden when every row is fetched.

## Goal (one line)
A recipe can bring back API and database datasets by fetching them again, so next month the user
does not re-upload or re-type them.

Example:
- Nov: import "sales" from `https://api.shop.in/orders?page=1&api_key=abc123` (header
  `Authorization: Bearer xyz`) and "stock" from PostgreSQL `erp on 192.168.1.20`, build a dashboard,
  **Save recipe**.
- Dec: Load recipe → match screen rows "sales (API)" and "stock (database)" already say
  `↻ Fetch again` → **Apply** → fetched, checked, built. Nothing to type.

## Decisions (agreed)
| # | Topic | Decision |
|---|-------|----------|
| 1 | What is saved | The **full connection, as is** (internal app, keep it simple): API URL, method, headers, body, rows path; DB kind, host, port, database, user, password, SSL, SQL. **No encryption.** |
| 2 | Setting | `RECIPE_KEEP_SECRETS = True` by default. A host can set `False`: then header values, the DB password and key-like URL parts (`api_key=`, `token=` …) are saved empty and asked on the match screen. |
| 3 | Where | Same in both: downloaded `.json` and host-stored recipes. |
| 4 | Re-import | Match dropdown gets `↻ Fetch again from saved API / database`, picked by default. **Apply** fetches, then checks columns, then builds. Any failure (wrong password, server down, missing column) → nothing changes, the error names the dataset. The user can still pick an uploaded dataset instead. |

⚠ A saved recipe file then holds passwords and API keys in plain text: keep it inside the company,
like the `.env` file. The README will say this in one line.

## Smaller choices (decided, say if you disagree)
- **Safety on load**: every fetch goes through the same rules as a manual import:
  `ALLOW_API_IMPORT`, `API_BLOCK_INTERNAL`, `API_ALLOWED_HOSTS`, `ALLOW_DB_CONNECT`,
  `DB_ALLOWED_HOSTS`, read-only SQL check. If a rule now blocks it, the row offers only
  "pick an uploaded dataset".
- **Wrong / changed password**: the error says so and the row shows a password box to type the new
  one, then Apply again (same boxes as the `False` setting).
- **One API, many lists**: lists from one API import (and their JSON child lists) are fetched once
  together and matched by their path, e.g. "sales – orders", "sales – items".
- **SQLite / Excel database files** (no server): unchanged, matched by name like uploaded files.
- Secrets are never written to logs or shown back on screen (boxes are password fields, the source
  badge shows host / address without keys).
- Recipe version stays `1`; the new `"connection"` key is optional, so 9a recipes still load.

## What we build
1. **`my_insights/recipe_sources.py` (new)**
   - `describe(dataset, keep_secrets) -> dict | None`: the saved connection.
   - `blank_secrets(...)`: only for `keep_secrets=False`; blanks key-like parts, lists what to ask.
   - `fetch_for_recipe(workspace, entry, typed) -> list[Dataset]`: rebuilds `ApiSource` / `DbSource`,
     runs the normal import path (API fetch + `load_parsed`, `db_loading` read).
2. **`recipe.py`**: `to_recipe` adds `"connection"` to source entries; `_check` validates it
   (types, size, known kinds); `apply_recipe` fetches "↻" rows **first**, checks columns on the
   fetched data, and on any failure removes what it just fetched and refuses.
3. **`recipe_views.py`**: match form takes typed secrets (`secret_<id>_<name>`), never echoed back.
4. **Match screen template**: "↻ Fetch again" option, source badge (API / database + host),
   password boxes only when something is missing, tooltips on each.
5. **`conf.py`**: `RECIPE_KEEP_SECRETS = True`.
6. **Docs**: README (setting, what is saved, the plain-text warning), `project.md` recipe section
   (replace the "never contains API addresses" line).

## Tests (`tests/test_recipe_sources.py`)
- Default: recipe holds URL, headers, DB login and SQL; loading it fetches with no typing.
- `False`: no `abc123` / `xyz` / password in the text; `api_key=` empty; boxes asked; typed values work.
- Apply with fetch (fake API server / fake DB reader): datasets, queries, dashboard come back.
- Fetch fails (wrong password, blocked host, missing column) → refused, workspace unchanged,
  message names the dataset.
- Safety: recipe pointing at an internal / not-allowed host is blocked when the rule is on.
- Old 9a recipes still load. Secrets never appear in logs.
- Browser check in the demo: save, load, Apply, dashboard back.

## End of phase
Update "Current phase" in `my-insights/CLAUDE.md`, then `/code-review` and `/simplify`.
