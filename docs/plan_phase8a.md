# Plan: Phase 8a, JSON files and Import from API (my-insights)

**Status: done.** 328 tests pass (45 new: `tests/test_json_loading.py`, `tests/test_api_import.py`).
(Phase 7 plan: `plan_phase7.md`.) Built as agreed, with these changes:
- **Test** shows a summary instead of a 5-row table: the row count, where the rows were found,
  every column with its first value, and the child lists. This avoids a second data table that
  can't sort or page.
- **Refresh** is on the dataset card (home page) only.
- New setting `API_TIMEOUT_SECONDS` (30). Labels and logs never show the URL's `?query` part,
  because it may hold a key.
- The child link is suggested as main = child and lookup = parent (`_parent_row → _row`),
  because each parent row is unique.
- **8a.1 (added after):** with "Rows at" empty, *every* list of objects in the reply (or JSON
  file) becomes its own dataset, e.g. `{"data": {"orders": […], "shops": […]}}` →
  "sales – orders" and "sales – shops". A single list keeps the plain name ("sales").
  "Rows at" still loads just one list. Single values (`"status": "ok"`) are left out.
  Refresh updates every list from that API together; a gone list keeps its data with
  "Not in the latest API reply", a new list is added. Test shows each list found.
- Known gap: DNS rebinding (the host's address changing between the check and the connect)
  is not caught. It is marked `shortcut:` in `api_import.py`.

## Context
Today the only way to bring data in is CSV or Excel. Users also have data as JSON files and
in web APIs that reply with JSON. Phase 8a adds both of these. Database import
(SQLite, DuckDB, Postgres, MySQL) comes in Phase 8b.

## Goal (one line)
Upload `.json` / `.jsonl` files, or fetch JSON from an API. Nested data becomes normal
columns and child datasets that work with queries, charts and dashboards.

Example: `orders.json` →
```json
[{"id": 1, "customer": {"name": "Ravi", "city": "Pune"}, "tags": ["new", "vip"],
  "items": [{"sku": "A1", "qty": 2}, {"sku": "B7", "qty": 1}]}]
```
- **orders** has the columns: `_row`, `id`, `customer.name`, `customer.city`, `tags` ("new, vip")
- **orders – items** has the columns: `_parent_row`, `sku`, `qty`. It has 2 rows.
- The Relate screen's top suggestion is `orders._row = orders – items._parent_row`.

## Decisions (agreed after grilling)
| # | Topic | Decision |
|---|-------|----------|
| 1 | File types | `.json` (whole file) and `.jsonl` (one record per line). |
| 2 | Where the rows are | A list: each item is a row. An object: use its largest list of objects and show "Rows taken from data.orders". No list at all: the object is 1 row. |
| 3 | Objects inside objects | Become dotted columns (`customer.address.city`), at most 3 levels deep. Anything deeper is kept as JSON text. A missing key leaves a blank. |
| 4 | Lists of plain values | One text column: `new, vip`. |
| 5 | Lists of objects | A child dataset "orders – items" with `_parent_row`. The main dataset gets `_row`. There is no automatic combined dataset; the link is the top suggestion on the Relate screen. Only one level of children: a list inside a child stays JSON text. |
| 6 | Types | Every value goes through the same text → type check as CSV, so dates, leading-zero codes and mixed columns behave the same way. |
| 7 | API form location | Home page tabs: **Upload files \| From API**. |
| 8 | API form | URL, Method (GET/POST), Headers (one `Name: value` per line), Body (JSON, POST only), Rows at (optional path such as `data.results`; empty = the auto-find from #2), Dataset name. Every field has a tooltip. **Test** shows the first 5 rows before Import. |
| 9 | Remembering the key | URL, headers and body are kept only in the in-memory workspace, so **Refresh** works. They are wiped on logout or timeout, and never go into the database, recipes or exports. |
| 10 | Safety | On by default; `ALLOW_API_IMPORT: False` turns it off. Internal and localhost addresses are blocked unless listed in `API_ALLOWED_HOSTS`. Timeout 30 s, size limit `MAX_FILE_MB`, http and https allowed. |
| 11 | Refresh | Keeps the dataset's ID and name and replaces only the data, so queries, charts and dashboards keep working. A missing column is named in the message. Child datasets refresh too. |
| 12 | Out of scope | Page-by-page APIs, OAuth login, scheduled refresh, XML. |
| 13 | Removing a main dataset | Its child datasets go with it. The confirm message lists them. |
| 14 | Refresh brings different lists | A new list becomes a new child dataset. A list that is gone keeps its old data and shows "Not in the latest API reply". |

## What we build
1. **`my_insights/json_loading.py` (new)**
   - `load_json(workspace, name, label, data: bytes, rows_at="")`: parse the JSON (`json` /
     line by line for `.jsonl`), find the rows (#2), and flatten them (#3–#5). Each table is
     written to `RAW_TABLE` as text and then built with `loading._build_dataset` (reused as is),
     so all the CSV checks and messages still apply.
   - Clear errors: "This is not valid JSON (line 12: missing comma)", "No rows found at
     data.results", too many rows or columns (the existing limits).
   - All of a file or nothing (the same as the Excel sheets): if a child fails, the main
     dataset is removed too.
2. **`loading.py`**: `load_file` sends `.json` / `.jsonl` to `load_json`, and the error message
   for unsupported types names JSON too.
3. **`workspace.py`**
   - `Dataset.parent_id: int | None` (set on child datasets) and
     `Dataset.api_source: ApiSource | None` (url, method, headers, body, rows_at; memory only).
   - `remove_dataset` also removes child datasets (#13).
   - `replace_dataset_data(dataset_id, …)` for Refresh. It keeps the ID and name, then
     checks the queries for missing columns (#11, #14).
4. **`my_insights/api_import.py` (new)**: `fetch_json(source) -> bytes` using Python's
   built-in `urllib`, so no new package. It checks the address: the host must resolve to a
   public IP or be in `API_ALLOWED_HOSTS`, the check runs again after redirects, and it
   enforces the timeout and the size cap. Errors are user-friendly: "The API replied 401 (not
   authorised): check the key in Headers", "The server took longer than 30 seconds".
5. **`linking.py`**: `suggest_links` puts the parent and child pair `_row = _parent_row` first.
6. **`conf.py`**: `ALLOW_API_IMPORT` (True) and `API_ALLOWED_HOSTS` ([]).
7. **Views and URLs (`views.py`)**: `api_test` (preview of 5 rows), `api_import`, and
   `dataset_refresh`. CSRF and the workspace lock work like `upload`.
8. **Templates (HTMX, Alpine and DaisyUI, with tooltips)**: tabs on `home.html`, an API form
   partial, a Refresh button and an "API" badge on the dataset card and page, a "Child of
   orders" note, and the upload hint text names JSON.

## Tests (`tests/test_json_loading.py`, `tests/test_api_import.py`)
- Flat list, `.jsonl`, wrapped object (auto-find and `rows_at`), a single object becoming 1 row.
- Nesting: dotted columns, the 3-level limit, plain lists joined into text, a list of objects
  becoming a child dataset with `_parent_row`, a list in a list kept as JSON text.
- Types: dates, leading-zero codes and mixed columns work the same as in CSV.
- Errors: bad JSON, an empty list, no rows at the path, the row and column limits, all or nothing.
- Link suggestion: the parent and child pair is first.
- API (with a fake opener, no real network): GET and POST with headers and body, a blocked
  private address and localhost, a blocked redirect to a private address, the allow list,
  timeout, too large, HTTP 401 and 500 messages, a switched-off setting.
- Refresh: the ID and name are kept and a query still runs; a missing column is named; a new
  list adds a child; a list that is gone keeps its data and shows the note.
- Removing a parent removes its children.
- Run the whole suite: `uv run pytest`. Check the home tabs and the API form in the browser preview.

## End of phase
Update "Current phase" in `CLAUDE.md`, then run `/code-review` and `/simplify`.
