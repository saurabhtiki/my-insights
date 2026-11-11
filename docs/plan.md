# Plan — Phase 4.5: Query builder extras (my-insights)

**Status: done.** 205 tests pass (27 new). Built as planned, with these notes:
- With Across, the chart and "Use as dataset" use the table's own cells, so Top N and total
  filters apply to them too (one row per group and across value).
- The dataset made from a query is called "<query name> (result)" and gets a "Query result" badge.
- Saving the query copies its answer again. Combined datasets built on it are rebuilt too.
- "Use as dataset" is refused when the server's memory budget is full (same as an upload).
- Chart colours made from a date field now go in time order (Apr, May, Jun…).

## Context
Phase 3 left 3 query builder items as "Not now": **pivot (across)**, **OR filters**, **query as a
dataset**. Phase 4 (charts) is done, 178 tests. We build these before Phase 5 (Dashboard) so
dashboards get them from day one. First build step: copy this plan into `my-insights/docs/plan.md`
and set "Current phase" in `my-insights/CLAUDE.md` to Phase 4.5.

## Goal (one line)
Spread a field across the top, match "any of" filters, and use a saved query as a new dataset.

Examples:
- Sum of Amount, Branch down, **Month across** → `Branch | Apr 2026 | May 2026 | … | Total`.
- Date in FY 2026-27 AND **any of** (Branch = Delhi, Amount > 1,00,000).
- Save "Top customers" → **Use as dataset** → new query: monthly sales of those customers.

## Decisions
| # | Decision |
|---|----------|
| 1 | **Across zone** under Group by: holds 1 group field (date parts allowed). Its values become columns; each value field makes one column per across value (`Apr 2026 · Sum of Amount`). |
| 2 | Across is done in Python on the grouped result (no DuckDB PIVOT), so number formats, date labels and order (dates in time order) stay as today. Cap: 50 across columns (shown in a note, extra ones dropped, not merged). |
| 3 | Totals are **asked from DuckDB, not added up**, so averages and unique counts are right: a Total column (per row: group by down fields only), a totals row per across column (group by across only), and the corner = today's overall total. |
| 4 | With Across: sort by a value = by its row Total; Top N = top rows by that Total. Applied after the pivot; the SQL runs without the Top N limit. |
| 5 | Charts keep using the plain (long) result. The across field just becomes an ordinary group, so the chart's auto mapping makes it "Colour by". Saved charts keep working. |
| 6 | **OR filters: two boxes.** "All of these must match" (AND, today's list) and "Any one of these" (OR). SQL: `all1 AND all2 AND (any1 OR any2)`. Each filter has a ⇄ button to move it to the other box. Each condition is wrapped in its own brackets (fixes the "between" parts). Same 2 boxes for total filters (HAVING). An empty Any box adds nothing. |
| 7 | **Use as dataset** (button on a saved query): makes a dataset "📋 <query name>" from the query's full result (no 100k preview cap; the 2,000,000 row limit applies). Columns = the result's labels (`Branch`, `Month`, `Sum of Amount`), made unique. Across is ignored here (long form), so the columns don't change when the data does. |
| 8 | Stored as an in-memory DuckDB table (CTAS with the query's params; views can't hold params). When the query is saved again, the table is rebuilt; if a column it relied on is gone, the existing "missing field" messages show. Memory only, so it's still "no data at rest". |
| 9 | Removing the query removes its dataset and everything built on it (the confirm lists them). A query-dataset can be joined (Relate) and used for further query-datasets; no loops (a query can't be built on its own dataset). |
| 10 | Not now: more than 1 across field, nested OR groups, editing a query-dataset's data. |

## What we build
1. **Model (`workspace.py`)**
   - `QuerySpec.across: GroupItem | None`.
   - `FilterItem.any_of: bool = False`.
   - `Dataset.query_id: int | None` (the source query).
   - `dependants()` / `remove_query()` / `remove_dataset()` follow `query_id`.
2. **SQL (`querying.py`)**
   - `plan_query`: the across group joins the GROUP BY as key `a0`.
   - Filters split into all-of / any-of lists for both WHERE and HAVING. `_row_condition` and `_compare` results are wrapped in brackets.
   - Records mode uses the same split.
   - New `pivot_result(plan, result, …)`: builds dynamic `plan.columns` (`p{n}_{value key}`) and the rows, and runs the 2 extra totals queries through the existing `run_query`.
3. **Query as dataset (new `query_dataset.py`)**
   - `build_query_dataset(workspace, query_id)`: CTAS `ds_{id}`.
   - Profiles are copied from the plan's column kinds, with blanks and samples via `loading.fill_samples`. Pattern from `linking._build_view`.
   - Rebuilt from `query_save` when a dataset exists for that query.
4. **Views (`query_views.py`)**
   - `_read_form` reads `across_column` / `across_part` and `{key}_any`.
   - `_apply_action` gets `add_any_filter`, `move_filter` and `set_across` / `clear_across`.
   - `_result_table` uses the pivoted columns.
   - New POST `query/<id>/as-dataset/`.
5. **Templates**
   - `partials/query_screen.html`: an Across drop zone, the 2 filter boxes with the ⇄ button, and a "Use as dataset" button. Tooltips on all of these.
   - Dataset lists show "From query <name>" instead of a file name.
   - Use DaisyUI cards and badges, like the existing zones.
6. **Docs**: `docs/plan.md`, then the `CLAUDE.md` phase line at the end.

## Reuse
- `run_query`, `quote_name`, `_row_condition`, `_compare`, `DATE_PARTS`, and `format_value` / `indian_number`.
- `linking._build_view` (profiles pattern) and `loading.fill_samples`.
- `django_datatable.RowsTable`, through `_result_table`.
- `charting.default_chart` / `fit_to_query` (they get the long result, unchanged).

## Tests (`tests/test_pivot.py`, `tests/test_or_filters.py`, `tests/test_query_dataset.py`)
- **Pivot**
  - Columns per across value × value field.
  - Date across stays in time order.
  - The cap of 50 shows a note.
  - Total column and totals row are right for sum, avg and count unique (not added up).
  - Sort and Top N work by row total.
  - The chart still draws with across as Colour by.
- **OR filters**
  - all + any SQL is right and has its brackets.
  - "between" inside OR works.
  - An empty any box does nothing.
  - The ⇄ move works, and HAVING any-of works.
  - Saved queries reload both boxes.
- **Query as dataset**
  - The columns and row count match the result.
  - It works beyond the 100k preview cap.
  - It rebuilds on query save.
  - The missing field message shows.
  - Removing the query cascades (confirm lists it), it can join in Relate, there are no self loops, and sessions are isolated.
- Older tests all still pass.

## Verification
- `uv run pytest`, then ruff.
- Restart the demo server (`my-insights-demo`, port 8010), load the CSV, build Branch × Month across, add an OR filter, and use as dataset → a new query on it. Screenshot at desktop and phone width.
- Then `/code-review`, fix what it finds, and update docs/plan.md status and the CLAUDE.md phase line.
