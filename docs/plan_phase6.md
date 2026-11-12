# Plan — Phase 6: Export (my-insights)

**Status: done.** 267 tests pass (27 new). Built as planned, with these changes:
- **Excel writer is xlsxwriter**, not openpyxl: 3× faster (2 lakh rows × 4 columns in 4 s).
  openpyxl stays as a test-only tool for reading files back.
- **Excel has a size limit** (agreed during build): the file is built in memory, never on disk,
  and 2 lakh × 4 cells took about 285 MB. New setting `EXCEL_MAX_CELLS` (default 10 lakh cells).
  Bigger answers get "Too big for Excel, download CSV". CSV streams from DuckDB in batches of
  5,000 rows, so it has no such limit.
- The query screen exports its current answer (the draft), so unsaved queries work too.
- The datatable's own CSV button is hidden with CSS on result tables and Table tiles.
- Export errors show a short page with "← Back" (a download link can't show a toast).
- Code review fixes: export plans a copy of the draft; a Top N above the Excel limit is refused,
  not silently cut; the CSV stream's cursor is opened inside its error handling; Across with no
  Group by has no duplicate Total row.
- Known gaps / follow-ups:
  - The datatable's CSV button stays hidden with CSS (decided: no `csv=False` package option).
  - The HTML file's table is a plain `<table>` (a static snapshot can't use the server-side
    datatable). This is an exception to the "use django-datatable" rule. It has its own small
    search box and ⤓ CSV button that work inside the file, on the rows it holds (max 500).
- Fixed after review: the HTML file fades the other bars / slices so the clicked one stands
  out; Excel numbers at or below −1 lakh get their own lakh format (−12,34,567); a saved query
  is "Not saved" only when it really differs from the saved copy (switching to the Chart tab
  no longer greys out "Add to dashboard"); dashboard filters have a Clear button next to
  Apply; the HTML file lists every dashboard filter (an empty one as "nothing picked, not used").

## Context
Phase 5 (dashboards) is done, 240 tests. Users can build queries, charts and dashboards, but
can't take them out of the app. Today the only download is the datatable's CSV link: it is cut at
100,000 rows, the Table tile's link is broken (it downloads the dashboard page) and ignores click
filters. Phase 6 adds proper exports. Recipes (save/load JSON) and AI come later.

## Goal (one line)
Query results → CSV / Excel (full rows, formula-safe), every chart → PNG, dashboard → one HTML file.

Examples:
- "Sales by Branch" with 3 lakh rows → `Sales by Branch.xlsx`, all 3 lakh rows, ₹ format, Total row.
- Dashboard "Monthly review" with `Branch = Delhi` clicked → `Monthly review.html` showing Delhi only.
- A customer name `=HYPERLINK(...)` is saved as `'=HYPERLINK(...)` so Excel won't run it.

## Decisions (agreed)
| # | Decision |
|---|----------|
| 1 | **Full result**: exports run the query again without the 100,000 preview cap. New setting `EXPORT_ROW_CAP` (default = `MAX_ROWS`, 2,000,000). Excel stops at 10,48,575 rows and notes it. |
| 2 | **Excel cells are real values**: numbers + number format (Indian grouping, ₹ when the value uses it, decimals), dates as real dates shown `dd-mm-yyyy`. Bold header, frozen top row, autofilter, Total row at the bottom (from `result.totals`). |
| 3 | **About sheet** in Excel: query name, datasets, filters used (incl. click filters), export time, "cut at N rows" if so. CSV stays plain data (no extra lines, so it opens cleanly anywhere). |
| 4 | **Across** exports wide, like the screen (Branch \| Apr \| May …). |
| 5 | **Formula escaping** in CSV and Excel: text starting `= + - @` (tab/CR too) gets a leading `'`. Real numbers like `-5` are left alone. Reuse `django_datatable.formula_safe`. |
| 6 | **Export buttons**: one "⤓ Export ▾" menu (CSV · Excel) on the query result and on each Table tile, with tooltips. The datatable's own CSV link is turned off on these tables (if the package has no switch, leave it and note it — no package edits here). |
| 7 | **Table tile export uses what you see**: saved global filters + current click filters (sent from the page like tile loads do). Fixes the broken tile CSV. |
| 8 | **Chart PNG**: keep the query-screen button; add a small ⤓ PNG button on each dashboard chart tile that downloads only that chart (event carries the tile id). |
| 9 | **Dashboard → HTML**: "⤓ HTML" button (view mode). One self-contained file, built on the server: title, export time, filter chips (saved + click), tiles in a simple CSS grid with the same widths. Charts are live ECharts (hover works) loaded from the CDN — **needs internet** to open. KPI and Note tiles as on screen. Table tiles show formatted text, first 500 rows, with "showing 500 of N" note. |
| 10 | Not now: dashboard → Excel workbook, SVG charts, PDF, offline HTML, recipes. |

## What we build
1. **Dependency**: move `openpyxl` from dev to runtime (`uv add openpyxl`; write-only mode for speed).
2. **Full-result fetch (`querying.py`)**
   - `run_query(connection, plan, top_n, cap=None)`: when `cap` is given, `run_query` and `_spread_across` cut to it instead of `row_cap()`. `plan_query(..., cap=...)` already exists.
   - New setting `EXPORT_ROW_CAP` in `conf.py`.
3. **Export logic (new `exporting.py`)**
   - `export_result(workspace, query, extra_filters)` → `QueryResult` with the export cap.
   - `export_columns(result)`: the wide column list (Across included), same order as the screen.
   - `csv_response(name, result)`: via `django_datatable.rows_csv_response` (BOM, streaming, formula-safe).
   - `excel_response(name, result, about)`: openpyxl write-only workbook; `excel_number_format(column)` maps our format (₹, decimals, Indian grouping, %) to an Excel format string such as `[>=10000000]##\,##\,##\,##0;[>=100000]##\,##\,##0;##,##0` with ₹ prefix.
   - `safe_filename(name)`.
4. **Views (new `export_views.py`)** and urls
   - `query/<id>/export/<csv|xlsx>/` (also works for the unsaved draft via `?draft=`).
   - `dashboard/<id>/tile/<tile_id>/export/<csv|xlsx>/` — takes `clicks` like `tile_view`, uses `dashboard.build_tile` filters.
   - `dashboard/<id>/export/html/` — POST with `clicks` from `Alpine.store("dash").json()`; loops `dashboard.tiles` through `build_tile`.
   - Errors (missing query, removed source, too big) → logged, then a clear message (toast), never a broken file.
5. **Templates**
   - `partials/export_menu.html` (DaisyUI dropdown, tooltips) used in `query_result.html` area and `dashboard_tile.html` Table tiles.
   - PNG button on chart tiles in `dashboard_tile.html`; `chart_script.html` `downloadPng` listens for its tile id only.
   - `dashboard_export.html`: standalone page (own `<!doctype>`, CDN DaisyUI/Tailwind + ECharts 5.6.0, `chart_script` inlined, `json_script` for options, autoescaped text).
   - "⤓ HTML" button in `dashboard.html` toolbar.
6. **Docs**: this file's status, then the `CLAUDE.md` phase line.

## Reuse
- `django_datatable.formula_safe`, `rows_csv_response`.
- `querying.plan_query` (has `cap`), `run_query`, `format_value`, `indian_number`, `QueryResult.columns/totals/truncated`.
- `dashboard.build_tile`, `read_clicks`, `TileView`; `dashboard_views._grid_context` widths.
- `query_views._table_rows` / Across column logic for the wide layout.
- `partials/chart_script.html` (`insightChart`, `addChartFormats`, `downloadPng`).
- `conf.get`, `tests/conftest.py` helpers (`csv_bytes`, `insights_settings`, `workspace`).

## Tests (`tests/test_exporting.py`, `tests/test_export_views.py`)
- **Formula safety**: `=1+1`, `+91…`, `-x`, `@sum`, tab-start → escaped in CSV and Excel; `-5`, `+1,000` numbers untouched.
- **Full rows**: with `PREVIEW_ROW_CAP=10` a 50-row query exports 50; with `EXPORT_ROW_CAP=20` exports 20 and About sheet says "cut".
- **Excel**: numbers are numeric cells, dates are date cells, number format has ₹ / decimals, header frozen, Total row matches `result.totals`; About sheet lists filters.
- **Across**: wide columns match the screen.
- **CSV**: BOM, header labels, same values as Excel.
- **Tile export**: global + click filters applied (Delhi only); clicked chart's own filter skipped as on screen; removed source → clear message.
- **Dashboard HTML**: one file, has every tile title, KPI value, note text escaped (`<script>` stays text), chart options as JSON, table capped at 500 with note, filter chips shown.
- **Views**: download headers / filenames, sessions isolated, bad id → 404 message.
- Older tests all still pass.

## Verification
- `uv run pytest`, then `ruff`.
- Restart demo server (`my-insights-demo`, port 8010), load the CSV.
- Export a query to CSV and Excel; open the Excel with openpyxl and check types/formats; check a `=` cell.
- On "Monthly review": click a bar, export a Table tile (filtered), download a chart PNG, export HTML and open it in the browser pane (charts hover).
- Screenshot the menus at desktop and phone width.
- Then `/code-review`, fix findings, update this file's status and the CLAUDE.md phase line.
