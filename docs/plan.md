# Plan — Phase 5: Dashboard (my-insights)

**Status: done.** 240 tests pass (35 new). Built as planned, with these notes:
- KPI = the value's grand total over the rows passing the query's row filters. Group by,
  Across, sorts, Top N and total filters are left out.
- A filter still waiting for values (e.g. "is one of" with nothing ticked) shows "waiting"
  and is not applied.
- Edit mode saves on every change (drag, size, KPI text, note text). The grid then redraws.
- The chart script moved to `partials/chart_script.html`, shared by the query screen and dashboards.
- Follow-up fixes:
  - Tiles are packed: each one spans as many tiny grid rows as its height. They are placed
    in order: each goes to the highest free spot that is not above the tile before it, so
    dragging a KPI right after a ⅔ table puts it beside the table. Starts snap to the width
    (a ⅓ tile starts at the left, middle or right third). A ⅓ tile still fills the space
    under a short KPI next to a tall table.
  - Dragging uses Sortable's own drag, so the page scrolls when you hold a tile near the
    top or bottom edge.
  - Only the size menus auto-save on "change". Sortable fires its own "change" event while
    you drag, and that used to save and redraw the grid mid-drag, dropping the tile in
    the wrong place.
  - KPI and note texts are edited with ✎ in Edit mode (Save / Cancel), not shown twice.
  - "Add to dashboard" always shows on the query result. It is greyed out, with a tooltip,
    until the query is saved.
- Known gaps for later:
  - A Table tile's CSV download ignores click filters (Phase 6, Export).
  - Changing a tile's size in Edit mode reloads every tile.

## Context
Phase 4.5 is done (205 tests). Users can save queries and charts. Phase 5 lets them put those
side by side on a dashboard, filter everything at once, and click a chart to filter the rest.
HTML export moves to Phase 6 (Export), and recipes (save/load JSON) come in a later phase.

## Goal (one line)
Named dashboards of chart, table, KPI and note tiles, with global filters and click-to-filter.

Examples:
- "Monthly review": KPI "₹ 1.2 Cr Total sales" · chart "Sales by Month" · table "Top customers".
- Global filter *Date this FY* → every tile with a "Date" field shows this FY only.
- Click the **Delhi** bar → the other tiles show Delhi only; chip `Branch = Delhi ✕` clears it.

## Decisions
| # | Decision |
|---|----------|
| 1 | Scope: tiles, layout, global filters, click-to-filter. HTML export → Phase 6. Recipes → later. |
| 2 | **Many named dashboards** per session (names unique), listed on home in their own section with Open / Remove. Gone at session end like everything else. |
| 3 | **4 tile types**: 📊 Chart (a saved chart), ▤ Table (a saved query), KPI (one number), 📝 Note (text). |
| 4 | Chart / Table / KPI tiles **link** to the saved chart / query (by id), not a copy. Editing the chart updates the tile. A removed chart / query leaves a flagged tile: "This chart was removed". |
| 5 | **Layout**: 12-column grid. Drag to move (SortableJS). Each tile has a width (⅓, ½, ⅔, full) and height (short, tall). Phone width = every tile full width. |
| 6 | **View mode / Edit mode**. View = tiles + filter bar. "Edit" shows drag handles, the size menu, "＋ Add tile" and remove buttons. "Done" saves the layout. |
| 7 | **Global filters** match tiles **by field name**. Each one is added to the tile's query as an extra "All of these" rule. A tile without that field (or where the field's type differs) is not filtered and shows a badge: "Not filtered by Branch (no such field)". |
| 8 | Filter picker lists every field of the dashboard's datasets, merged by name. Its type comes from the first dataset that has it. Same options as the query builder (is one of, between, this FY…). Global filters **are saved** with the dashboard. |
| 9 | **Click-to-filter**: clicking a bar / point / slice filters every **other** tile on that field. The clicked chart is not filtered; it highlights the picked item. Clicking another item on the same chart replaces its filter. Clicking the same item or the chip's ✕ clears it. Filters from different charts combine (AND). |
| 10 | Date parts turn into a date range: "Apr 2026" → 1–30 Apr 2026, "FY 2026-27" → 1 Apr 2026 – 31 Mar 2027 (also week, quarter, year, FY quarter). A stacked segment filters on its bar **and** its colour. Click filters are **temporary** (not saved). |
| 11 | **KPI tile**: pick a saved query and one of its values. It shows the grand total (ignores Group by) in the value's own format (₹ 1.2 Cr, 12,34,567), with a title (default: the value's name) and an optional small line under it. No "vs last month" yet. |
| 12 | **Table tile**: the query's result in a django-datatable table, 10 rows a page, sortable, Across columns like the query screen, and a link that opens the query. |
| 13 | **Note tile**: plain text. Line breaks are kept and `https://…` links become clickable. No other formatting. |
| 14 | **"Add to dashboard ▾"** button on the chart and saved-query screens: lists the dashboards and "New dashboard…". |
| 15 | Each tile loads on its own (HTMX `hx-trigger="load"`), so a slow tile doesn't hold up the others. A filter change reloads only the tiles it affects. |
| 16 | Not now: HTML export, recipes, KPI comparisons, rich text notes, free-form resize, more than 1 click filter per chart. |

## What we build
1. **Model (`workspace.py`)**
   - `DashboardTile(tile_id, kind, chart_id, query_id, value_key, title, subtitle, text, width, height)`.
   - `Dashboard(dashboard_id, name, tiles: list[DashboardTile], filters: list[FilterItem])`. Tile order = list order.
   - `Workspace.dashboards` and `new_dashboard_id()`.
   - `remove_query` / `remove_dataset` / chart remove **keep** the tiles. A tile whose source is missing shows the "removed" message.
2. **Dashboard logic (new `dashboard.py`)**
   - `dashboard_fields(workspace, dashboard)`: fields merged by name, each with its type.
   - `tile_spec(workspace, tile, filters, click_filters)`: a copy of the tile's query with the matching filters added. Also returns the names of the filters it couldn't use, for the badge.
   - `click_filter(query, plan, group_key, label)`: turns a clicked item into a `FilterItem` (`in` for plain fields, `between` dates for date parts).
   - `kpi_value(...)`: runs the query with no groups and formats the value with `format_value`.
3. **Views (new `dashboard_views.py`)** and urls under `dashboard/`
   - list / create / rename / remove.
   - Open (view and edit).
   - `tile/<id>/` (draws one tile).
   - add / remove tile.
   - layout save: order, width and height from the Sortable list.
   - filter add / change / remove.
   - The add-to-dashboard POST used by the chart and query screens.
   - Click filters travel as a small JSON list in the page (Alpine state). They go along with each tile request (`hx-vals`) and are never stored.
4. **Templates**
   - `dashboard.html`: filter bar with chips, the tile grid, the edit toggle.
   - `partials/dashboard_tile.html`: one partial per tile kind.
   - `partials/add_tile_dialog.html`.
   - An "Add to dashboard" dropdown in `query_screen.html` and `chart_panel.html`.
   - A dashboards section in `workspace_panel.html`.
   - Use DaisyUI cards, dropdowns, badges and modals, and put tooltips on every button.
5. **Chart script**
   - Move `insightChart()` out of `query.html` into a shared partial, so the query screen and the dashboard both use it.
   - Add an optional click handler that dispatches `insights-chart-click` with the group key and label.
   - Add highlighting of the picked item.
6. **Docs**: this file, then the `CLAUDE.md` phase line at the end.

## Reuse
- `querying.plan_query`, `run_query`, `format_value` and `indian_number`. `FILTER_OPS` / `DEFAULT_OP` and `_row_condition`, through the normal plan.
- `querying.DATE_PARTS`, `date_range` and `financial_year_start`, for date ranges from clicks.
- `charting.build_chart`, `fit_to_query` and `broken_reason`.
- `query_views._result_table` and `_table_rows`, plus `django_datatable.RowsTable`, for Table tiles.
- `partials/query_filter_row.html` and the `filter_values` view, for the global filter editor.
- SortableJS (already loaded) for drag to move. `views.panel_response`, for home list updates.

## Tests (`tests/test_dashboard.py`, `tests/test_dashboard_views.py`)
- **Model**
  - Create, rename and remove a dashboard. Names are unique.
  - Tiles keep their order.
  - Removing a chart or query flags the tile and doesn't crash it.
- **Global filters**
  - Added to tiles that have the field.
  - Skipped (with the badge reason) when the field is missing or its type differs.
  - Two datasets on one dashboard.
  - Saved with the dashboard.
- **Click filters**
  - A plain value becomes `in`.
  - Month, quarter, FY and week become the right first and last day.
  - A stacked segment gives 2 filters.
  - The clicked chart itself is not filtered.
  - Filters from 2 charts combine.
  - Not saved.
- **KPI**: grand total for sum, avg and count unique. Format follows the value's number format.
- **Table**: paging and sort work, and Across columns show.
- **Note**: text is escaped, line breaks are kept, links become clickable, and `<script>` stays text.
- **Views**
  - Every page and tile loads.
  - Layout save keeps order and sizes.
  - Add to dashboard from the chart and query screens, including "New dashboard…".
  - Sessions are isolated.
- Older tests all still pass.

## Verification
- `uv run pytest`, then ruff.
- Restart the demo server (`my-insights-demo`, port 8010) and load the CSV.
- Build the "Monthly review" dashboard (KPI, chart, table, note) and drag a tile.
- Add *Date this FY*.
- Click a bar and check the other tiles filter, then clear the chip.
- Screenshot at desktop and phone width.
- Then `/code-review`, fix what it finds, and update this file's status and the CLAUDE.md phase line.
