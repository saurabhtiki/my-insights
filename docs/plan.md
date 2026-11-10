# Plan — Phase 1: Foundation + Upload & Profile

**Status: done (10-11-2026).** 57 tests pass. Notes at the bottom.

## Goal (in one line)
A user opens My Insights, uploads 1+ CSV/Excel files, and sees a clean profile of each file.
Nothing is saved to disk or database.

Example: upload `stock_jan.csv` → page shows "Item Code — text — 0 blanks — e.g. A101, A102".

## Decisions (agreed)
| # | Decision |
|---|----------|
| 1 | Build here, not in live Stock Audit. Stock Audit installs it from git in a later phase. |
| 2 | No login in the add-on. Each browser is told apart by its session cookie. |
| 3 | One waitress process, many threads (like Stock Audit). Fine for 10–15 users. |
| 4 | Tailwind: standalone `tailwindcss.exe` + `build_css.ps1`, same as Stock Audit. DaisyUI 5. |
| 5 | Files: CSV, XLSX, XLSM. `.xls` → "Please save as .xlsx". |
| 6 | Excel: every non-empty sheet becomes its own dataset. |
| 7 | Unclear dates read as dd-mm-yyyy (03-04-2026 = 3 April). |
| 8 | Header = first non-blank row. |
| 9 | One odd value in a column → whole column is text, flagged, tooltip shows examples + row. |
| 10 | Idle 25 min → warning with **Keep my data**; idle 30 min → wiped. **Clear my data** button always visible. Host logout also wipes (logout signal). |
| 11 | One folder + one git repo per reusable app inside `Apps/`. |
| 12 | `Apps/CLAUDE.md` = shared rules; each app's `CLAUDE.md` = its "Current phase". |
| 13 | 1 GB server: demo/Stock Audit override limits (below). Add-on defaults stay as in project.md. |
| 14 | DuckDB never writes temp files. Too big → clear message, not a crash. |
| 15 | Excel read with `python-calamine` (low memory, values only, macros ignored). |

## Folder layout
```
Apps/
  CLAUDE.md                 shared rules for all reusable apps
  my-insights/              own git repo, installable from git
    pyproject.toml
    CLAUDE.md               "Current phase" only
    my_insights/            the add-on (shipped)
    demo/                   tiny test site (not shipped)
    tests/
    tools/                  tailwindcss.exe (not in git), build_css.ps1
    docs/project.md, docs/plan.md
```

## Settings block
```python
MY_INSIGHTS = {  # add-on defaults      # demo / 1 GB server
    "MAX_FILE_MB": 100,  #                        20
    "MAX_ROWS": 2_000_000,  #                        500_000
    "MAX_COLUMNS": 300,
    "PREVIEW_ROW_CAP": 100_000,
    "SESSION_MEMORY_MB": 1024,  #                        150
    "TOTAL_MEMORY_MB": None,  # None = no cap          350
    "IDLE_WARN_MINUTES": 25,
    "IDLE_CLEAR_MINUTES": 30,
}
```

## What we build
1. **Skeleton** — `uv` project, Django 5.2, Python 3.12, settings `base/development/test/production`,
   waitress `serve` command, HTMX + Alpine + DaisyUI, `django-datatable` from git.
2. **Engine** — one in-memory DuckDB per session (dict keyed by session id, thread-safe lock).
   Per-session memory limit, total budget check ("Server is busy"), no temp directory.
   Cleanup: idle sweep, Clear button, logout signal.
3. **Upload screen** — drag-and-drop, multiple files. Read from memory only.
   Detect types (number / text / date / true-false) and dates. Trim spaces, skip blank rows.
   Reject with a named reason: corrupt, too big, too many rows/columns, empty/duplicate column name, `.xls`.
4. **Profile screen** — one card per dataset (name, rows, columns, memory used).
   Column table via `django-datatable` `RowsTable`: name, type, blanks, samples, flag.
   First 100 rows preview. Rename / remove dataset. Tooltips on every badge and flag.
5. **Idle warning** — Alpine timer banner at 25 min with Keep my data.

## Tests
- Good CSV / XLSX / XLSM → right types and counts; multi-sheet → one dataset per sheet.
- Each reject rule → right message (names the column).
- dd-mm date rule; mixed column → text + flag.
- Two sessions can't see each other's data.
- Clear button, idle timeout and logout signal all wipe data.
- Over-limit → friendly message; no files written to disk during upload.

## Not in this phase
Joins, query builder, charts, dashboards, export, AI assistant, recipes, Indian number formatting.

## Notes from building (what changed vs. the plan)
- Added `fsspec`: DuckDB needs it to read a file straight from memory (no disk).
- `TOTAL_MEMORY_MB = None` now means "half the server's RAM, counting only My Insights' data".
  Measuring the whole machine's RAM refused uploads whenever *other* programs used memory.
- One memory number per user is stored after each load/remove, so pages never wait for
  another user's upload to finish.
- A column mixing dates with and without a time (e.g. `03-04-2026` and `03-04-2026 10:30`)
  is one "Date & time" column, not a mixed one.
- CSV row numbers in "odd value" tooltips skip blank lines (DuckDB drops them). Excel rows are exact.
- DuckDB's "block all disk access" switch also blocks reading from memory, so it is off.
  Phase 3 (query builder) must build SQL itself and never run user/AI-written SQL as-is.
