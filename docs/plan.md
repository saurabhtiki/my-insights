# Plan — Phase 3: Build a query

**Status: done (10-11-2026).** 134 tests pass (45 new). Notes at the bottom.

## Goal (in one line)
Pick a dataset, drop fields into boxes (Group by, Values, Filters, Sort), see the answer table live.

Example: `Sales` → Group by **Month, Branch** · Values **Sum of Amount, Count of rows** ·
Filter **Date = this financial year** · Sort **Sum of Amount, biggest first** → a table with
one row per month and branch, plus a Total row.

## Decisions (agreed)
| # | Decision |
|---|----------|
| 1 | Screen: a builder panel on the left (fields, then boxes **Group by**, **Values**, **Filters**, **Sort**; it scrolls on its own) and the result on the right. On small screens the panel slides in with **Edit query**; on big screens drag its edge to resize (remembered; double-click resets). Each filter has **Clear** to empty its picks but keep it. Drag a field in, or click **+** on a box (works on phones). Drag inside a box to reorder. With **Group by** and **Values** both empty, the result lists the matching rows themselves (all fields), e.g. Filter Department is one of IT → every IT row. |
| 2 | A **query** is a saved question on one dataset (not a new dataset). Many per dataset. Listed on home under its dataset: Open / Edit / Remove. Charts (Phase 4) use queries. |
| 3 | Values: Sum, Average, Count, Count unique, Min, Max, Median + **Count of rows**. Numbers start as Sum; text/dates as Count unique. |
| 4 | Date group-by: Day, Week, Month (default), Quarter, Year, Financial year (Apr–Mar), FY quarter. Sorted by time, not A–Z. |
| 5 | Filters (all must be true — AND): text is one of / is not one of / contains / blank / not blank; number = ≠ > < between blank; date between or quick pick (Today, This month, Last month, This FY, Last FY, Last N days). |
| 6 | Filters can also use a value total ("Sum of Amount > 1,00,000"). |
| 7 | Calculated fields by formula: `[Column]`, `+ - * / ( )`, numbers, `ROUND`, `ABS`, `IF`, text join `&`, `YEAR`, `MONTH`, `DAYS_BETWEEN`. Anything else refused, naming it. Divide by 0 → blank. |
| 8 | A formula with `SUM/AVG/COUNT…` is a **total formula** (e.g. `SUM([Profit]) / SUM([Sales])`), Values only. The formula box has a "Row or total?" tip with both examples. |
| 9 | Live result on every change, up to 100,000 rows, paged (`django-datatable`), **Total** row. No group → one total row; no values → unique groups list. |
| 10 | Sort by any group or value, A→Z / Z→A, many levels. **Top N**. No sort → group order. |
| 11 | Indian grouping (12,34,567.89), ₹ and 0–2 decimals per value; averages 2, counts 0. Dates dd-mm-yyyy. |
| 12 | Removing a dataset removes its queries (confirm lists them). A missing column shows a clear message, not a crash. |
| 13 | Not now: pivot (across), OR filters, query as a dataset. |
| 14 | Formula builder: click/drag chips (fields, signs, functions) into the box; typing allowed; live check with examples or "did you mean". |
| 15 | Calculated fields belong to the dataset and are shared by its queries (ƒ mark). Removing one used by a query asks first. |
| 16 | **Save** suggests a name ("Sum of Amount by Month, Branch"); warns on leaving unsaved; names unique. |
| 17 | "Is one of" tick list: top 500 values with counts + search for any value. |
| 18 | Same column can be a value twice; each value can be renamed. |
| 19 | Date quick picks are worked out on the day the query runs. |
| 20 | Blank groups show as **(blank)**, sorted last. |
| 21 | IF / IFS (else-if) / SWITCH, each with a click form; conditions use AND / OR / NOT. |

## What we build
1. **Engine (`querying.py`)** — `QuerySpec` (dataset, group-by fields, values, filters, sort, top N,
   calculated fields). Builds SQL itself from the spec — never runs typed SQL. Quotes every name.
2. **Formula reader** — calculated fields like `Profit = [Sales] - [Cost]`; only allowed words,
   columns and operators; clear error naming the bad part.
3. **Query screen** — field list + zones, HTMX live result (`django-datatable`), name + Save.
4. **Home / dataset page** — "New query" button, saved queries list, edit / remove.

## Tests
- Each aggregation, date grouping (month, quarter, financial year), each filter type, sort, top N.
- Formula: good formulas, division by zero, bad input refused with a clear message, no SQL injection.
- Totals row, no-group (one total row), no-values (distinct list), query on a combined dataset.
- Removing a dataset removes its queries (confirm lists them). Sessions stay isolated.

## Not in this phase
Charts, dashboards, export, AI, recipes.

## Notes carried from earlier phases
- DuckDB "block disk access" is off. This phase must build SQL itself, never run user/AI-written SQL.
- Combined datasets are views; querying them works the same as an uploaded dataset.

## Notes from building (what changed vs. the plan)
- **Total** is a line under the table (the shared table has no footer row). It covers the rows that
  pass the filters, and with Top N or a total filter only the groups shown. An average there is
  the average of all those rows, not of the group averages.
- Formulas also allow `TODAY()`, `MIN`, `MAX`, `MEDIAN`, `COUNT_UNIQUE` and `TRUE` / `FALSE`.
  Text comparisons ignore upper / lower case, like Excel. A formula that only uses whole numbers
  shows no decimals.
- A calculated field can use other calculated fields; one that uses itself, or that would grow
  too large, is refused with a message.
- The "is one of" tick list loads when it is opened (not on every change); ticked values are kept
  while searching.
- Result columns with more than 100 different values get a "contains" box instead of a tick list.
- Sorting / paging the result table reuses the rows just shown (no second run).
- Drag and drop uses SortableJS (from a CDN, like the other libraries in the fallback base).
