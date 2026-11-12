# Plan: Phase 10, AI assistant drafts a query (my-insights)

**Status: done.** 496 tests pass (37 new in `tests/test_ai_assist.py`). (Phase 9b plan: `plan_phase9b.md`.)
Built as planned, with these changes:
- The AI answers in its own simple shape (field names, `agg`, `op`, indexes), not the
  screen's internal keys; `ai_assist.to_draft` checks it and builds the QuerySpec. So
  `recipe.py` was not changed.
- A dropped connection from the service is a plain message (found in the browser check).

## Context
`docs/project.md` item 7 (AI assistant) is the only feature not built yet. Users who don't know
the builder should be able to type a question in plain English and get the query screen
filled in for them, then check it and run it themselves.

Example: user types "monthly sales by branch for this FY, as a line chart"
→ query screen opens with Group by `Month of Date`, `Branch` · Values `Sum of Amount` ·
Filter `Date this FY` · Chart tab set to **line**. User looks, presses Run / Save.

## Decisions (agreed)
| # | Topic | Decision |
|---|-------|----------|
| 1 | What AI returns | Builder settings as JSON (groups, values, filters, sorts, top N), **never SQL**. Screen opens pre-filled; user reviews before anything is saved. |
| 2 | Providers | One "OpenAI-style" format (`POST {address}/chat/completions`). Works with OpenAI, Claude's compatible endpoint, Groq, OpenRouter, Ollama. Plain HTTP, no new package. |
| 3 | What AI sees | Dataset names, column names, types, and calculated field names only. **No values, no rows.** |
| 4 | API key | User types address + key + model each session. Kept in the in-memory workspace only (not Django session, not DB, not logs, never shown back). Cleared on logout / idle clear. |
| 5 | Scope | Query + chart type. Can use existing calculated fields; does **not** create new ones. |
| 6 | Dataset | Optional "Dataset (hint)" dropdown above the question. Blank → AI picks; chosen → told to AI as a strong hint. User can switch on the filled screen. |
| 8 | Column hints | Optional **"Notes for AI"** box (max 1,000 chars), e.g. `Item code = "Item no" column`, `Sale value = "Net Amt"`. Kept in memory for the session and sent with every question until changed. Names only, never data. |
| 7 | Model | "Load models" button calls `{address}/models` to fill a dropdown; a text box works if that fails. |

## How it works (flow)
1. **AI panel** (new page `/insights/ai/`, link in the top nav, hidden if `ALLOW_AI=False`):
   - Settings card: Address, API key (password box), Model (dropdown + Load models), Save.
     Shows "Connected: gpt-4o-mini @ api.openai.com" once saved, with a Forget button.
   - Question card: Dataset hint (optional), text box, **Draft query** button (htmx, spinner).
2. Server builds a prompt: the allowed JSON shape + aggregations / date parts / filter ops
   (from `querying.AGGREGATIONS`, `DATE_PARTS`, filter ops) + each dataset's columns and types
   (from `querying.field_catalog`), plus the hint and question.
3. Reply → JSON → `recipe._build(QuerySpec, …)` + `recipe._clean_query` (reuse; make them
   public helpers) → `querying.plan_query` checks every field exists and fits the aggregation.
4. OK → saved as a query draft (`query_views._remember_draft`) → redirect to
   `/insights/query/?ai=<draft_id>`, which opens the pre-filled screen with a banner
   "Drafted by AI from: '…'. Check it, then Run." and the chart type preset.
5. Bad reply (not JSON, unknown column, wrong aggregation) → one clear message naming the
   problem ("AI used a column that doesn't exist: 'Revenue'"); the question stays in the box.
   No automatic retries.

## Smaller choices (decided, say if you disagree)
- Safety: the address goes through the same checks as API import (`API_BLOCK_INTERNAL`,
  `API_ALLOWED_HOSTS`), so Ollama on localhost works by default. HTTPS not forced.
- Limits: `AI_TIMEOUT_SECONDS = 60`, reply size capped (e.g. 64 KB), question max 1,000 chars.
- Errors from the service (401 wrong key, 404 wrong model, timeout) → plain message, e.g.
  "The AI service said the key is wrong (401)." Logged without key or headers.
- Formula-like filter values are just values; nothing the AI writes runs as SQL (same as today).
- **Short memory (last 5 turns)**: the AI remembers the last 5 questions and its own JSON answers,
  in the in-memory workspace only (gone on logout / idle clear). So follow-ups work:
  "sales by department" → screen fills; then "show avg also" → same query plus Average of Sales.
  The AI panel stays visible on the query screen (small box under the AI banner) for follow-ups.
  A **New topic** button clears the memory; switching the dataset hint doesn't clear it.
  The memory holds only questions and JSON settings, never data, so decision 3 still holds.

## What we build
1. **`my_insights/ai_assist.py` (new)**: `AiSettings` dataclass; `list_models(settings)`;
   `build_prompt(workspace, question, dataset_hint)`; `ask(settings, prompt) -> str`;
   `draft_from_reply(workspace, reply) -> (QuerySpec, chart_type)` raising `AiError` with
   user-facing text. Uses `urllib` like `api_import.py`; reuses its URL safety check.
2. **`my_insights/ai_views.py` (new)**: `ai_page`, `ai_settings_save`, `ai_forget`,
   `ai_models`, `ai_draft` (htmx partials).
3. **`workspace.py`**: `ai_settings: AiSettings | None` and `ai_history: list` (max 5 turns) and `ai_notes: str`
   on the workspace (memory only); `ai_new_topic` view clears it.
4. **`recipe.py`**: expose `build_spec(value)` / `clean_query(spec)` wrappers for reuse.
5. **`query_views.py`**: `_start_spec` accepts `?ai=<draft_id>` (draft + chart type + banner).
6. **Templates**: `ai.html`, `partials/ai_settings.html`, `partials/ai_result.html`; nav link;
   DaisyUI cards, tooltips on every box/button ("Your key stays in this session's memory only").
7. **`conf.py`**: `ALLOW_AI = True`, `AI_TIMEOUT_SECONDS = 60`.
8. **`urls.py`**: `ai/`, `ai/settings/`, `ai/forget/`, `ai/models/`, `ai/draft/`.
9. **Docs**: README (setting, what is sent to the AI), project.md item 7 wording if needed.

## Tests (`tests/test_ai_assist.py`)
Fake AI server (local HTTP or patched `urlopen`), no real AI calls:
- Prompt holds column names/types, **no row values** (check a known sample value is absent).
- Good reply → pre-filled query screen with right groups/values/filters + chart type.
- Dataset hint is passed; AI may still pick another dataset.
- Notes for AI: sent with every question, kept across questions, cleared on logout; editable.
- Bad replies: not JSON, JSON in ``` fences (accepted), unknown column, bad aggregation,
  unknown dataset → clear message, nothing saved.
- Service errors: 401, 404, timeout, huge reply → clear messages.
- Key never appears in logs, page HTML, or recipe output; Forget clears it; idle clear clears it.
- `ALLOW_AI=False` hides nav link and returns 404; blocked internal address refused when rule on.
- Load models: list parsed; failure → text box still works.
- Memory: follow-up sends earlier turns; only last 5 kept; New topic clears; no row values in it.
- Browser check in the demo with a fake/real endpoint: ask, screen fills, Run, Save.

## End of phase
Move current `docs/plan.md` to `docs/plan_phase9b.md`, write this plan to `docs/plan.md`.
After build: update "Current phase" in `my-insights/CLAUDE.md`, run `/code-review` and `/simplify`.
