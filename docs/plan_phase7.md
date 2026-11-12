# Plan — Phase 7: Tile colours and smart notes (my-insights)

**Status: done.** 283 tests pass (16 new, `tests/test_notes.py`). (Phase 6 plan: `plan_phase6.md`.)
Built as agreed, with these changes:
- **Colours are fixed Tailwind colours** (blue-600, violet-600, …), not the host's theme colours:
  a theme may have no purple, so "Purple" would not be purple. Soft = see-through tint.
- **Host Tailwind must scan the whole `my_insights` folder** (README step 5): the colour class
  names live in `dashboard.py`. The demo's `tools/tailwind.input.css` does this.
- Size marks: `# ` Heading, `## ` Large, `-# ` Small. A numbered list keeps its first number.
- The editor's preview is drawn by the server (`note_preview`), so it matches the tile exactly.
- On a Strong card the edit bar and editor stay plain, and warnings / badges use neutral
  styles, so they stay readable.
- Code review fixes: a note's value datasets add their fields to the filter bar; a value right
  after a link is kept out of the link; value names are cleaned the same way in the editor and
  on the server; KPI choices are worked out once; the preview loads when ✎ opens, not on load.

## Context
Phase 6 (export) is done, 267 tests. KPI and Note tiles all look the same (white card, blue
number), notes are plain text, and a note can't show a number from the data. The HTML file
stays a fixed snapshot (decided: no filtering inside the file).

## Goal (one line)
KPI and Note tiles get a colour; notes get simple formatting and live values that follow the
dashboard's filters.

Examples:
- "Returns" KPI on a soft red card, "Total sales" on a soft green card.
- A note: **Avg selling price** is [Avg selling price] → "Avg selling price is ₹ 1,245.50";
  click the Delhi bar → "Avg selling price is ₹ 1,380.00".

## Decisions (agreed after grilling)
| # | Topic | Decision |
|---|-------|----------|
| 1 | Which tiles get a colour | KPI and Note only (charts have their own colours). |
| 2 | Colour choice | Swatches: None, Blue, Purple, Teal, Green, Orange, Red, Grey. Each **Soft** (light tint, normal text) or **Strong** (full colour, readable text picked for you). DaisyUI theme colours, so dark mode works. |
| 3 | Where you set it | Edit mode: a 🎨 button next to the size menus on KPI / Note tiles; swatches + Soft/Strong switch; saves at once like the size menu. |
| 4 | KPI number colour | Blue on a plain or Soft card (as now); the card's text colour on a Strong card. |
| 5 | Note formatting | Plain text box + toolbar + live preview underneath. Toolbar adds marks: `**bold**`, `_italic_`, `__underline__`, size (Small / Normal / Large / Heading: `-# `, `## `, `# `), `- ` bullets, `1. ` numbers. Links stay clickable. Nothing else (no text colours, no tables). |
| 6 | Safety | The text is escaped first, then only these marks become styles, so pasted `<script>` stays text. No new library. Old plain notes look the same. |
| 7 | Live values: source | "Insert value" uses the KPI picker: a saved query's value, worked out as its grand total exactly like a KPI. |
| 8 | Live values: in the text | Token `[[Avg of Selling price]]`. A list under the box shows each inserted value with ✕ remove and ✎ rename (rename updates the text). A token with no matching value shows "⚠ unknown value [[…]]". |
| 9 | Live values: filters | Follow dashboard filters and chart clicks like a KPI; "Not filtered by …" badge when the value's data lacks the field; the note reloads only when a filter on its fields changes. |
| 10 | Removed source | Only that spot shows "⚠ value removed"; the rest of the note shows. |
| 11 | Number format | Same as the KPI / query (₹, lakh grouping, decimals). No extra options. |
| 12 | Limits | ≤ 10 values per note; text ≤ 2,000 characters. |
| 13 | HTML file | Colours, formatting and values as at download time (fixed snapshot; no filtering in the file). |

## What we build (after grilling)
1. **Model (`workspace.py`)**: `DashboardTile.color` (e.g. `green`), `DashboardTile.tone`
   (`soft` | `strong`), `DashboardTile.note_values: list[NoteValue]` where
   `NoteValue(key, query_id, value_key, label)`.
2. **Logic (`dashboard.py`)**
   - `TILE_COLORS` swatch list and `tile_color_class(tile)` → DaisyUI classes.
   - `render_note(text, values)` → safe HTML: escape, then marks → tags, tokens → values.
   - Note values: reuse `kpi_spec` / `_kpi` per value via `tile_query`; collect "Not filtered"
     badges and fields so the note reloads on filter changes.
3. **Views (`dashboard_views.py`)**: `layout_save` reads colour / tone / note values; an
   "Insert value" list from `kpi_choices`.
4. **Templates**: colour swatch menu in the tile edit bar; note toolbar + live preview
   (Alpine); KPI / note tiles use the colour classes; `dashboard_export.html` too.
5. **Docs**: this file's status, `CLAUDE.md` phase line.

## Reuse
`kpi_choices`, `kpi_spec`, `_kpi`, `tile_query`, `querying.format_value`, the grid's
`data-fields` reload, `tileEditor()` in `dashboard_grid.html`, `urlize`.

## Tests
- Note marks: bold / italic / underline / sizes / bullets / numbers → right tags; `<script>`
  and `**<b onclick>**` stay text; links still clickable; old plain notes unchanged.
- Values: token → formatted value; changes with a global filter and a click filter; value
  from a dataset without the field → "Not filtered" badge; removed query → "value removed";
  more than 10 values refused with a clear message.
- Colours: saved and shown; unknown colour ignored; shows in the HTML file.
- Note tile reloads when its value's field filter changes.
- Older tests all still pass.

## Verification
`uv run pytest`, `ruff`; restart demo server (port 8010); colour a KPI and a note, format a
note, insert a value, apply a filter and click a bar, export HTML; screenshots at desktop and
phone width, light and dark. Then `/code-review`.

### After-use fixes (user feedback)
- The Add tile box now has the full note editor (toolbar, Insert value, preview) and a Card colour
  picker for KPI and Note, so a tile can be set up before it is added (fields `t0_*`).
- Colour picker moved to `partials/color_picker.html` (used by the tile's 🎨 menu and the Add box).
  Soft / Strong stays greyed out with "Pick a colour first" until a colour is picked.
- Demo: `tailwind.css?v=<file time>` (`demo/context.py`), so browsers never keep an old CSS file
  (old CSS had no swatch classes: the 🎨 menu showed one tiny dot).
