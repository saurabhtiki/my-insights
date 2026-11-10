# Plan — Phase 2: Relate datasets (links / joins)

**Status: done (10-11-2026).** 89 tests pass (32 new). Notes at the bottom.

## Goal (in one line)
Link two datasets by shared column(s) to make a new combined dataset, with no extra memory.

Example: `Sales` (Item Code, Qty) + `Items` (Code, Name, Price) → new dataset
"Sales + Items" (Item Code, Qty, Name, Price). It shows up like any other dataset.

## Decisions (agreed)
| # | Decision |
|---|----------|
| 1 | A link makes a **new combined dataset**, saved as a DuckDB **view** (a stored recipe, no copy of data). |
| 2 | Join types, plain names + tooltips: **Keep all main rows** (left, default), **Only matches** (inner), **Keep everything** (full). No right join. |
| 3 | Link on any number of column pairs (**+ Add column**). e.g. Item Code + Store. |
| 4 | Ignore upper/lower case by default (tick-box to turn off). If the two column types differ, compare as text. |
| 5 | Suggestions: similar name + compatible type + real shared values; show "940 of 1,000 rows match (94%)"; only ≥ 30% match, best first. |
| 6 | Main dataset = many rows (Sales). **Lookup list** = parent (Items). Lookup link column(s) **must be unique** (as a combination) — else refused, naming examples + rows. So totals never double. |
| 7 | Same column name on both sides → `Name` and `Name (Items)`. Link column appears once. |
| 8 | Preview before **Create**: match %, row count, warnings, first 100 rows. Result over MAX_ROWS → refused. |
| 9 | Combined datasets can be linked again (chains: Sales + Items + Stores). |
| 10 | Removing a source also removes datasets built on it — confirm lists them first. |
| 11 | **Edit link** on a combined dataset reopens the screen with its settings. |
| 12 | Home: **Link datasets** button (2+ datasets) + hint cards ("Sales and Items share Item Code, 94% → Link"). |
| 13 | Blank link values never match; preview counts them ("15 sales have no Item Code"). |
| 14 | One combined dataset holds **many lookups**, each linked to the main dataset (star) or to an earlier lookup (hierarchy: Item → Sub-category → Category). |

## What we build
1. **Engine (`linking.py`)** — `LinkSpec` (main id, lookup id, column pairs, join type, ignore case).
   - `check_link(spec)` → match stats, unique check, blanks, result row count, problems.
   - `create_link(spec, name)` → `CREATE VIEW` + profile (types copied from sources, blanks/samples recomputed).
   - `suggest_links(workspace)` → ranked suggestions per dataset pair.
   - Dataset gets `link` (its spec) and `depends_on` ids; remove cascades.
2. **Link screen** — pick main + lookup, column pairs (+ Add column), join type, ignore-case tick-box.
   HTMX live preview: stats, warnings, first 100 rows (`django-datatable`). Name box + **Create**.
3. **Home** — Link datasets button, suggestion hints, "combined" badge on cards.
4. **Dataset page** — "Built from Sales + Items" line, **Edit link**, remove confirm lists dependants.

## Tests
- Left / inner / full give right row counts and blanks.
- Multi-column link; ignore-case on/off; number vs text compared as text.
- Duplicate lookup key → refused, names value + rows. Over MAX_ROWS → refused.
- Name clash → `Name (Items)`. Chain of 3. Edit link. Remove source cascades.
- Suggestions find `Item Code` ↔ `item_code`; skip pairs under 30%.
- View adds ~no memory. Two sessions stay isolated.

## Not in this phase
Query builder, charts, dashboards, export, AI, recipes (the link spec is kept so recipes can save it later).

## Notes carried from Phase 1
- DuckDB "block disk access" is off (it blocks reading from memory). Phase 3 must build SQL itself,
  never run user/AI-written SQL as-is.
- CSV row numbers in tooltips skip blank lines; Excel rows are exact.

## Notes from building (what changed vs. the plan)
- Duplicate message names the value and how often it repeats ("A101 (2 times)"), not file row
  numbers: the typed table does not keep file row numbers. Search for the value in the Rows tab.
- Match % = rows **with a link value** that match (blanks counted separately), same as the hints.
- "Keep everything": the link column shows the lookup's value for unmatched lookup rows.
- Suggestions only look at text and whole-number columns, need unique values on the lookup side,
  and a one-to-one pair keeps the bigger dataset as main. Cached until the dataset list changes.
- Editing a link rebuilds every dataset built on it inside one DuckDB transaction; if one would
  break, nothing changes and the message names it.
- Each open link screen (browser tab) keeps its own preview (`draft` id, last 5 kept).
