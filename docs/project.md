# My Insights: Project Requirements and User Flow

## Purpose
My Insights is a reusable Django add-on. Users upload 1 or more tabular data sets (CSV or Excel), explore it, and build their own queries, charts, and dashboards without writing SQL. It plugs into host Django applications (for example, Stock Audit).

## Core principles
- **No data at rest:** uploaded data lives in memory only. It is never stored in the database or on disk.
- **Dynamic:** any dataset, any columns, no schema setup.
- **Self-service:** users build everything themselves.
- **Reusable:** configured per host app through a single settings block.
- **Add-on scope:** data in → build → output. The add-on stores nothing. Users, permissions, and storage belong to the host app.

## Engine
- DuckDB, in memory only. Nothing is written to disk.
- One session = one in-memory database. It is deleted on logout or timeout.
- Designed for 1–5 concurrent users per server. The app warns when memory use is high.

## User flow

1. **Upload**
   The user uploads one or more CSV, Excel or JSON (`.json` / `.jsonl`) files, or imports JSON from a web API (GET/POST, headers, body; fetched on click, with a **Refresh** button). Nested JSON objects become dotted columns (`customer.city`), and lists of objects become linked child datasets. SQLite database files (`.db`) go in the same box: the user ticks the tables to import, and foreign keys become link suggestions. Or the user connects to a live PostgreSQL or MySQL server ("From database": host, database, user, password), ticks tables or writes one SELECT, and imports them read-only; a **Refresh** button reads them again. The password stays in the session's memory only. The app detects headers, data types, and date formats, then shows a profile of each dataset (columns, types, null counts, sample values). See "Bad data" below.

2. **Relate datasets**
   The app suggests joins between datasets based on column names, types, and value overlap. The user accepts, edits, or adds joins and chooses the join type.

3. **Build a query**
   The user builds the query by dragging fields into zones (dimensions, measures, filters, sort) or by filling in a form. They can add date ranges, group-by fields, aggregations, and calculated fields. A calculated field is a new column made from a formula, for example `Profit = Sales - Cost`. Results appear in a table with a live preview.

4. **Visualize**
   The user turns the result into a chart: bar, horizontal bar,Stacked Bar / Column Charts, line, area, pie, Donut ,catter, or combo with multiple axes. Charts update as the query changes.

5. **Dashboard(HTML)**
   The user combines charts, query results, text/notes, and KPI blocks into a dashboard. Global filters apply to every tile, and clicking a chart element filters the other tiles. Layout can be rearranged.

6. **Export**
   Results export to CSV or Excel, and charts export as images,dashboards in HTML. Cells starting with `=`, `+`, `-`, or `@` are escaped so they cannot run as formulas when opened in Excel.

7. **AI assistant (optional)**
   The user adds their own AI provider endpoint and API key, selects a model, and describes the request in plain language. The AI drafts a query. It only sees dataset structure (column names and types), not the data. The user reviews every query before it runs. The key lives in the session only, never in the database. Optional "Notes for AI" map the user's words to columns (e.g. Item code = "Item no"), and the AI remembers the last 5 questions so follow-ups work ("show average also"). It also picks a chart type.

## Recipes and host hooks
- A **recipe** is a small JSON description of the whole setup: links, calculated fields, queries, charts, and dashboards. It never contains data or sample values. For API / database datasets it remembers where they came from (address, headers, server, SQL), so Apply fetches them again.
- **Save recipe** downloads a `.json` file. A host app can store recipes itself instead, through two settings: `RECIPE_SAVE` → `save(request, recipe, name)` and `RECIPE_LOAD` → `load(request, recipe_id)`. The host shows its own list and links to `/insights/recipe/load/?id=7`.
- Example: Stock Audit's "Save recipe" stores the JSON in its own table; next month the user clicks it in Stock Audit's list.
- To reuse a recipe, the user uploads the new files and loads the recipe. A match screen guesses which dataset is which (same name, then similar name with the right columns) and lets the user change it.
- Every column the recipe uses must be there. If one is missing, the screen names it (e.g. "“sales” is missing: Cost.") and Apply stays off, so no wrong report is ever shown.
- Applying replaces the current queries, charts, dashboards and combined datasets; uploaded data stays. It is all or nothing.
- Secrets (API headers, keys in the address, the database password) are kept in plain text by default (`RECIPE_KEEP_SECRETS = True`, internal use). Set it to False and they are saved empty and asked on the match screen. Fetching from a recipe follows the same safety rules as a manual import.

## Security
- The API key is kept in the session only.
- Each session's data is isolated from other users.
- Excel macros are ignored.
- API import: internal and localhost addresses are blocked (unless the host allows them), and there are timeout and size limits. API keys and headers stay in the in-memory session only.
- Exports escape formula-like cells (see Export).

## Bad data
- **Fixed automatically:** extra spaces are trimmed, dates are detected, and blank rows are skipped.
- **Rejected with a specific message:** corrupt or unreadable files, files over the limits, and duplicate or empty column names (the message names the problem and the column).
- Mixed-type columns are kept as text, and the profile flags them.

## Key limits (configurable)
- Max file size: 100 MB per file
- Max rows per dataset: 2,000,000
- Max columns: 300
- Result preview cap: 100,000 rows

## Localization
English (India) formatting: lakh/crore number grouping, ₹ currency option, dd-mm-yyyy dates, and April to March financial year presets.
