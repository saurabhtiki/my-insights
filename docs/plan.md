# Plan: Phase 8c, Import from a live Postgres / MySQL database (my-insights)

**Status: done.** 412 tests pass (60 new in `tests/test_db_connect.py`; 6 live tests in
`tests/test_db_live.py` passed against Postgres 17 and MySQL 8.4 in Docker). (Phase 8b plan: `plan_phase8b.md`.)

Built as agreed, with these changes:
- **No separate Test button**: Connect lists the tables, which is the test.
- **Server tables start unticked** (a server may have hundreds); SQLite files stay all ticked.
- **Settings**: `DB_CONNECT_SECONDS` (15) and `DB_QUERY_SECONDS` (300) instead of one
  `DB_TIMEOUT_SECONDS`, so a big import isn't cut off by the connect wait.
- **Safety (code review)**: the host must be a plain name or IP (no socket path or host list),
  a password is required, and Postgres never reads this server's own `~/.pgpass`. Without
  these, a user could reach the host app's own database with no password.
- **MySQL**: `BOOLEAN` is really 0/1, so it loads as a number. Without "Require SSL" it still
  encrypts when the server offers it.
- **Refresh** shares `query_dataset.swap_in_fresh` with the API Refresh.

Known gaps: Import and Refresh hold the user's workspace lock while reading the server (like
file uploads), so that user's other tabs wait. Date-times are read to the second.

## Context
Phase 8b lets users upload a SQLite file. Most company data sits on a live Postgres or MySQL
server. Phase 8c lets a user connect to one, tick tables (or write a SELECT), import them, and
Refresh later. Data still lives in memory only; the password too.

## Goal (one line)
Open **From database**, fill in the server details, click **Connect**, tick tables or write a
SELECT, click **Import**. Each becomes a normal dataset with a **Refresh** button.

Example: Postgres `erp` on `192.168.1.20`, user `report` →
- pick list: ☑ orders (about 12,000 rows) ☑ customers (about 800 rows) ☐ sales.targets *view*
- Own query: name `pune_orders`, `SELECT * FROM orders WHERE city = 'Pune'`
- Import → **erp – orders**, **erp – customers**, **pune_orders**
- Relate screen, first suggestion: `orders.customer_id = customers.id` ("from the database")
- Next day: Refresh on **erp – orders** fetches today's rows.

## Decisions (agreed after grilling)
| # | Topic | Decision |
|---|-------|----------|
| 1 | Who connects | The user types the details in a form (no admin presets). |
| 2 | Driver | Python drivers: `psycopg` (Postgres) and `PyMySQL` (MySQL), as optional extras `my-insights[postgres]` / `[mysql]`. Missing driver → a clear message saying what to install. Rows go through the same text → type checks as CSV/SQLite. |
| 3 | Where | A third home tab **From database** (next to Upload files and From API): type, host, port, database, user, password, Require SSL; **Test** and **Connect**. Then the same pick list as SQLite, plus an Own query box. |
| 4 | Schemas | Every schema the user can read, except system ones. Names: `orders` for `public` (Postgres) / the chosen database (MySQL), `sales.orders` for others. |
| 5 | Row counts | Quick estimate from the server's statistics ("about 12,000 rows"). The real MAX_ROWS check happens during import. |
| 6 | Own query | Optional: Name + one SELECT (or WITH … SELECT). Several statements or anything else are refused before sending. Runs in a **read-only transaction** with a time limit. |
| 7 | Read-only | Every read (tables too) runs in a read-only transaction. The tooltip also advises a read-only database user. |
| 8 | Refresh | Yes, like API. The connection details (with password) stay in the session's memory only: never saved, exported, logged or put in a recipe. Refresh re-reads the table / re-runs the query. Logout/timeout forgets them. |
| 9 | Network | Allowed by default (company servers are usually 10.x / 192.168.x). New settings `ALLOW_DB_CONNECT` (True) and `DB_ALLOWED_HOSTS` ([] = any; when set, only those hosts). |
| 10 | SSL | Default "use if available"; a **Require SSL** tick box for cloud databases. No certificate upload. |
| 11 | Foreign keys | From `information_schema`, same as 8b: top link suggestions, only between tables imported together. |
| 12 | Names | "database – table"; Own query uses the name typed. `unique_name` for clashes. All or nothing per Import. |
| 13 | Tests | Fake database for normal tests; live tests run only when `MY_INSIGHTS_TEST_POSTGRES` / `MY_INSIGHTS_TEST_MYSQL` is set. One run against Postgres and MySQL in Docker. |

## What we build
1. **`my_insights/db_connect.py` (new)**
   - `DbServer` (in `workspace.py`): kind, host, port, database, user, password, require_ssl. Memory only, `repr` hides the password.
   - `connect(server)`: checks `ALLOW_DB_CONNECT`, `DB_ALLOWED_HOSTS`, the driver is installed;
     connects with `DB_TIMEOUT_SECONDS`; friendly messages for wrong password, unknown host,
     refused port, unknown database, SSL required (logged first, never with the password).
   - `list_tables(server)`: tables + views with estimated rows and columns
     (Postgres `pg_class.reltuples`, MySQL `information_schema.tables.table_rows`).
   - `check_select(sql)`: one statement, starts with SELECT / WITH, else refused.
   - Fetch rows in batches inside a read-only transaction with a statement time limit
     (Postgres `SET TRANSACTION READ ONLY` + `statement_timeout`; MySQL `START TRANSACTION READ ONLY` + `max_execution_time`).
2. **`db_loading.py`**: split the SQLite-only parts so `_load_table` and `_add_foreign_keys` work from any "source" (SQLite file or server): one small `rows(table)` / `foreign_keys(table)` interface. `PendingDb` also holds a live server pick list (no open connection kept: connect per action).
3. **`workspace.py`**: `DbServer`; `Dataset.db_source` (server + table or query) for Refresh.
4. **Refresh**: `dataset_refresh` handles `db_source` the same way as `api_source` (re-read, same names, types re-checked).
5. **Views/URLs**: `db_test`, `db_connect` (shows pick list), reuse `db_import` / `db_cancel` with the Own query fields. Same CSRF / lock / `check_capacity`.
6. **Templates (HTMX, Alpine, DaisyUI)**: From database tab form (port fills 5432 / 3306 when the type changes), pick list partial reused with "about N rows", Own query box; tooltips on every field.
7. **`conf.py`**: `ALLOW_DB_CONNECT`, `DB_ALLOWED_HOSTS`, `DB_TIMEOUT_SECONDS` (30).
8. **`pyproject.toml`**: extras `postgres = ["psycopg[binary]"]`, `mysql = ["PyMySQL"]`; dev group gets both.
9. **Docs**: `docs/project.md` 8c line, README settings, this plan.

## Tests (`tests/test_db_connect.py`)
- Form → server details; password never in logs, repr, recipe or export.
- Host not in `DB_ALLOWED_HOSTS` refused; switched off refused; driver missing message.
- `check_select`: SELECT/WITH pass; `DELETE`, `SELECT 1; DROP TABLE x`, empty refused.
- Fake server: listing with estimates and schema names, import ticked tables + own query, types like CSV, row limit, all or nothing, foreign key first suggestion, Refresh re-reads.
- Connection errors become friendly messages (wrong password, unknown host, timeout).
- Live (skipped unless env var set): Postgres and MySQL in Docker — list, import, FK, read-only (a write is refused), Refresh.
- Run `uv run pytest`; browser check in the preview against the Docker Postgres.

## End of phase
Update "Current phase" in `CLAUDE.md`, then run `/code-review` and `/simplify`.
