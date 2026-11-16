---
name: dashboard
description: "Use when the user wants a dashboard, report page, KPI board, metrics view, infographic or chart page as an AgentWorkBench Mini Web App: app or product analytics (funnels, retention, events), LLM or API usage and spend across teams, service health, SLOs, latency and incidents, cost, sales or any CSV/JSON table they want to explore. Builds it from one dashboard.json spec rendered by the vendored AWB Dash runtime (ECharts 6.1, no build step): linked filters, cross-filtering, drill-down, time presets, dark mode, table views and full ReadMiniAppContext / RunMiniAppCommand wiring come for free. Includes a validator that checks the spec against the real data. Not for a one-off chart in chat or a static image."
metadata:
  sdk-version: "1"
  runtime-version: "0.2.0"
---

# Dashboards as Mini Apps (AWB Dash 0.2.0)

You write **one JSON file** (`dashboard.json`) and data. The runtime draws it, links every panel to the filters,
and wires the AWB SDK. You do not write chart code. Do it the same way every time: the steps below, in order.

Files in this skill (`${CLAUDE_SKILL_DIR}`):
- `assets/app/` - the app template: `app.py`, `index.html`, `static/` (runtime, CSS, ECharts, licenses). Copy as is.
- `reference/spec.md` - **every key**. Read it before writing a spec.
- `reference/choosing.md` - which panel answers which question, layout rules, domain recipes.
- `reference/examples/` - three complete specs: `llm-usage.json`, `service-slo.json`, `product-funnel.json`
  (+ `product-funnel.routes.py`). `make_sample_data.py` makes their data.
- `reference/custom-types.md` - JS panel types, only when no built-in type can answer the question.
- `scripts/check_dashboard.py` - the validator. Stdlib Python.

## 1. Set up the app

```sh
mkdir -p mini-web-apps/<id> && cp -r ${CLAUDE_SKILL_DIR}/assets/app/. mini-web-apps/<id>/
mkdir -p mini-web-apps/<id>/data && chmod +x mini-web-apps/<id>/app.py
```
Put data files in `data/`. Never edit `static/awb-dash.js`, `awb-dash.css` or `echarts.min.js`. `static/custom.js`
is an empty hook for custom panel types; leave it empty unless you need one.
Data from a live system: an app.py route (step 3). Only relative URLs; the CSP blocks other hosts.

## 2. Profile the data first

```sh
cd mini-web-apps/<id> && python3 ${CLAUDE_SKILL_DIR}/scripts/check_dashboard.py --profile-csv data/<file>.csv
```
Write down: the time field and its span and grain, the category fields and how many values each has, the numeric
fields, and what one row is (an event? a day per user? a 15-minute bucket per service?). The row grain decides the
measures: a pre-aggregated row needs `sum` of counts, `wavg` of latencies, never `avg` of rates.

## 3. Shape the data (only if needed)

- Over ~100,000 rows, or one response over 6 MB: aggregate in an app.py route to the grain you chart.
- A per-entity fact (a user's furthest funnel step, first-seen date): derive it in an app.py route.
  Model: `reference/examples/product-funnel.routes.py`. The source is then `{"url": "api/users", "format": "json"}`.
- Never precompute a number that must follow the filters into a static file. If the runtime cannot compute it,
  that is a gap: say so in the panel's `note`.

## 4. Plan the page on paper

Answer, in this order (details in `reference/choosing.md`):
1. **The 3-6 questions** the user asked or implied. Each section of the page answers one.
2. **Headline row**: 3-4 `kpi` tiles (w 3, h 1) for the numbers they check first.
3. **One panel per question** from the table in choosing.md. One chart, one axis, one measure unit.
4. **Exact values**: end with a `table` (w 12).
5. **Layout**: each row's `w` adds up to 12 and its panels share one `h`. `section` panels start new rows.
6. **Time default**: the window that answers the main question (`30d` for usage, `all` for a week of
   SLO data, `all` for funnel history). Never a window the data does not reach.

## 5. Write dashboard.json

- Start from the closest example in `reference/examples/`. Keep its structure; change names, fields, text.
- Define every number once in `measures` (label, format, `good`, `thresholds`). A rate is a `ratio` of two sums.
- Give every panel a short `id`, a `title` that says what it shows, and a `subtitle` that says how to read it or
  what to click. Say when a panel ignores the time picker (`period`).
- Targets ("P1 answered within 30 min") are a share: two `count` measures, one with `where`, and a `ratio`.
  Before/after a date: two measures whose `where` compares the time field (`{"created_at": {"gte": "2026-10-20"}}`),
  shown side by side in one `bar` or `table`. Recipes: `reference/choosing.md`.
- Built-ins cover the usual special needs: `budget` (spend vs budget, projection; SLO error budget burn),
  `flow` (Sankey), `funnel` with `"cohort"`, `retention`, `cumulative` lines, panel `where` and `period`.
- Status colors only through `thresholds` (they come with an icon and a label). Never invent colors for series:
  the runtime colors by entity. Fix a brand color with top-level `colors` only when the user asks.

## 6. Validate and repair (loop, at most 3 rounds)

```sh
cd mini-web-apps/<id> && python3 ${CLAUDE_SKILL_DIR}/scripts/check_dashboard.py --profile dashboard.json
```
- Fix every `ERROR`; the message names the key and suggests the right one. Re-run.
- Treat these `WARNING`s as errors: no rows / a measure matches no rows in the default view, a funnel without
  `cohort`, layout gaps or mixed heights, mixed units. Fix the others or know why not.
- Exit 0 and no such warnings, then go on. Still failing after 3 rounds: simplify the panel (fewer series, a
  table) instead of guessing.

## 7. Check it renders

1. `REQUEST_METHOD=GET PATH_INFO=/dashboard.json ./app.py | head -3` (or TestMiniAppRequest) answers
   `Status: 200 OK`; so do `/` and each `data/...` or `api/...` URL. In Git Bash on Windows prefix
   `MSYS_NO_PATHCONV=1`, or MSYS rewrites `PATH_INFO` into a Windows path and every URL answers 404.
2. If sandbox Chrome is offered (OpenDebugBrowser): open it, look at the whole page in light and dark. Expect one
   console 404 (`/_awb/sdk.js`, outside the chat). A red "Panel ..." card is a spec error: read it, fix, reload.
3. In the chat: open the app (`OpenApp`), then `ReadMiniAppContext`. You see a summary line per panel, one detail
   per panel id (its data table) plus `spec` and `fields`, and the commands below. Run one:
   `RunMiniAppCommand {"name": "setTimeRange", "params": {"range": "7d"}}`, then read the context again.

## What you get without writing code

- Filters bar: time presets, custom dates, multi-select menus, filter chips, Reset, Theme (auto/light/dark).
- Click a bar, slice, cell, row, legend item, meter row or Sankey node: cross-filter (click again to clear).
  `drill` panels go one level down with a breadcrumb. Drag across a line or area to zoom time.
- Every chart has a Table toggle; tables sort by header. Empty results say "No data in ..." instead of a blank.
- AWB commands: `setTimeRange {range}`, `setFilter {field, values "a|b"}`, `clearFilters`, `drill {panel, value}`,
  `showTable {panel, on}`, `focusPanel {panel}`, `setTheme {theme}`. Pointing at a chart names the mark under
  the cursor with its values and the active filters.
- Accessibility: titled regions, ARIA chart descriptions from the summaries, table views, status icon + label,
  keyboard-usable controls, `forced-colors` and reduced-motion support.

## Performance limits

300,000 rows per source in the browser (filters re-aggregate everything; keep under ~100,000 for snappy
clicks). 8 MiB per response, 30 s per app.py request. Up to ~20 panels; the agent gets details for the first 28.
`Cache-Control: no-store` on Mini Apps means the 1.1 MB ECharts file loads on every open.

## When the spec cannot say it

Ask in this order: a different built-in panel; a derived source in app.py; a panel `note` that names the gap;
only then a custom type (`reference/custom-types.md`) or the `echarts` escape hatch (`reference/spec.md`).
A custom type must use `p.rows()` and `p.range()` so it follows the filters and the time picker.

## Before you say done

- [ ] `check_dashboard.py` exits 0 with none of the warnings listed in step 6.
- [ ] Every panel answers one of the planned questions; titles say what, subtitles say how to read it.
- [ ] Each row adds up to 12 columns with one height; the first row holds the KPIs.
- [ ] No panel shows "No data" in the default view; a funnel on event data has `cohort`.
- [ ] Rates are ratios; percentiles of pre-aggregated rows are labeled as such ("request-weighted mean of p95").
- [ ] You looked at it (sandbox Chrome or the chat) or read it through `ReadMiniAppContext`, and said which.
