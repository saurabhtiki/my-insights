# Plan: Phase 8b, Import from a SQLite database file (my-insights)

**Status: done.** 352 tests pass (19 new in `tests/test_db_loading.py`). (Phase 8a plan: `plan_phase8a.md`.)
Built as agreed. Small additions: a BLOB cell becomes the text "(binary, N bytes)"; a foreign key
with no column (`REFERENCES shops`) uses the primary key; keys over several columns are left to
the normal suggestions; uploading the same file again keeps one pick list; a failed import keeps the pick list so the user can untick and retry. Known gaps: waiting files are not counted in the memory banner, and counting rows of a heavy view can be slow.

## Context
Phase 8a (JSON files + API) is done. Users also keep data in databases. Phase 8b lets them
upload a SQLite file and bring its tables in. Live servers (Postgres/MySQL) move to Phase 8c,
so 8b needs no network, no password and no DuckDB add-on. `.duckdb` files are skipped for now
(they can't be read from memory, and we never write uploads to disk).

## Goal (one line)
Drop `shop.db` in the normal Upload box, untick tables you don't want, click Import. Each table
or view becomes a normal dataset; the database's foreign keys are the top link suggestions.

Example: `shop.db` has `orders(id, customer_id → customers.id, amount)`, `customers(id, city)` and
a view `big_orders` →
- pick list: ☑ orders (3 rows, 3 columns) ☑ customers (2 rows, 2 columns) ☑ big_orders *view*
- Import → **shop – orders**, **shop – customers**, **shop – big_orders**
- Relate screen, first suggestion: `shop – orders.customer_id = shop – customers.id` ("from the database")

## Decisions (agreed after grilling)
| # | Topic | Decision |
|---|-------|----------|
| 1 | Scope | SQLite files only (`.db`, `.sqlite`, `.sqlite3`). Postgres/MySQL = Phase 8c. `.duckdb` skipped. |
| 2 | Where | The same Upload files box. After upload, a table pick list appears under it. |
| 3 | Pick list | Tables and views (views marked "view"), each with rows and columns, all ticked. Import loads the ticked ones. All or nothing. |
| 4 | Mixed upload | CSV/Excel/JSON in the same upload load at once; each `.db` shows its own pick list. |
| 5 | No data at rest | Read with Python's `sqlite3` `deserialize(bytes)`, read-only, only `SELECT`. The bytes wait in the in-memory workspace until Import/Cancel/logout, then are dropped. |
| 6 | Types | Same text → type check as CSV (`write_raw` + `_build_dataset`), since SQLite columns can hold mixed values. Same limits and messages. |
| 7 | Names | "file – table"; one table → just "file". `unique_name` for clashes. |
| 8 | Foreign keys | From `PRAGMA foreign_key_list`, shown first on the Relate screen ("from the database"); user accepts. Not auto-saved. Only for tables imported together. |
| 9 | Refresh | None; upload the file again. |
| 10 | Switch | `ALLOW_DB_IMPORT` (True). Off → `.db` refused with a clear message. |

## What we build
1. **`my_insights/db_loading.py` (new)**
   - `open_sqlite(data: bytes) -> sqlite3.Connection`: `deserialize`, then `PRAGMA query_only=ON`.
     Not a SQLite file → `UploadRejected("This is not a SQLite database file.")`.
   - `list_tables(conn) -> list[DbTable]` (name, kind table/view, rows, columns) from `sqlite_master`.
   - `load_tables(workspace, name, label, conn, picked) -> list[Dataset]`: per table, stream rows
     in batches into `loading.write_raw`, then `loading._build_dataset`; errors prefixed
     `Table “x”:`; on failure remove the loaded ones (same as `_load_excel`, `loading.py:178-195`).
   - `foreign_keys(conn, picked)` → list of (table, column, ref_table, ref_column); stored on the
     workspace with dataset ids after import.
2. **`loading.py`**: `load_file` routes `.db/.sqlite/.sqlite3` to "pending database" (returns a
   pending pick instead of datasets); the unsupported-type message names SQLite.
3. **`workspace.py`**: `pending_databases: dict[token, PendingDb]` (bytes + file name), and
   `db_links: list[...]` for foreign keys. Cleared with the workspace.
4. **`linking.py`**: `suggest_links` puts `db_links` first (next to the JSON family suggestions,
   `linking.py:524-534`), labelled "from the database".
5. **Views/URLs**: upload view renders the pick-list partial for each pending DB;
   `db_import` (POST ticked tables + token) and `db_cancel`. Same CSRF/lock/`check_capacity`
   as `api_import`.
6. **Templates (HTMX, Alpine, DaisyUI)**: pick-list partial with checkboxes, Select all/none,
   Import and Cancel; tooltips on each control; upload hint and `accept` attr name `.db`.
7. **`conf.py`**: `ALLOW_DB_IMPORT = True`.
8. **Sample**: `docs/samples/shop.db` (orders, customers, a view, a foreign key).
9. **Docs**: `docs/plan.md` (this plan), `docs/project.md` line for 8b/8c.

## Tests (`tests/test_db_loading.py`)
- Tables and views listed with counts; import only ticked ones; names "file – table" / single "file".
- Types: dates, leading-zero codes, mixed column behave like CSV.
- Foreign key is the first suggestion; a key to a table not imported is ignored.
- Errors: not a SQLite file, empty database, nothing ticked, row/column limits, all or nothing.
- Mixed upload: CSV loads, `.db` waits for the pick. Cancel drops the bytes.
- Read-only: no write possible; nothing written to disk (`tmp_path` check).
- `ALLOW_DB_IMPORT=False` refuses `.db`.
- Run `uv run pytest`; browser check with `shop.db` in the preview.

## End of phase
Update "Current phase" in `CLAUDE.md`, then run `/code-review` and `/simplify`.
