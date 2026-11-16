# AWB Dash spec reference (dashboard.json, version 1; runtime 0.2.0)

One JSON file describes the whole dashboard. `static/awb-dash.js` renders it; `scripts/check_dashboard.py`
checks it against the data. Unknown keys are errors in both.

```json
{
  "version": 1,
  "title": "…", "description": "one sentence: what this answers",
  "sources":  { "<name>": { "url": "data/x.csv" } },
  "time":     { "field": "date", "default": "30d", "presets": ["7d", "30d", "90d", "all"] },
  "filters":  ["team", "model"],
  "measures": { "<name>": { "agg": "sum", "field": "cost_usd", "format": "usd", "label": "Spend" } },
  "panels":   [ { "id": "spend", "type": "kpi", "title": "Spend", "measure": "cost" } ],
  "theme": "auto", "refresh": 60, "colors": { "model": { "gpt-4.1": "#2a78d6" } }
}
```

## sources

| Key | Meaning |
|---|---|
| `url` | Relative path the app serves: `data/x.csv`, `data/x.json` or an app.py route `api/x`. Never absolute, never another host (CSP). |
| `rows` | Inline array of row objects. Small data only (< 2,000 rows). |
| `csv` | Inline CSV text. |
| `format` | `csv` or `json` when the URL has no extension. |
| `types` | Override inferred types: `{"user_id": "string", "ts": "time"}`. |
| `timeField` | This source's time column when it differs from `time.field`. |

Types are inferred: numbers; times when every value looks like `2026-11-14` or `2026-11-14T09:30:00Z`
(UTC; no offset means UTC); everything else is a string. JSON may be an array of rows or `{"rows": [...]}`.
Browser limit 300,000 rows per source; above ~100,000 pre-aggregate in app.py. The first source is the default.

## time

`field` (time column), `default` preset, `presets` shown in the picker, `grain` (`auto` or a grain below), `anchor`
(`data`, the default: "last 30d" ends at the newest row; `now`: ends now; use for live data).
Presets: `1h 6h 24h 7d 14d 30d 90d 180d 1y mtd qtd ytd all`. The picker also has a custom date range, and dragging
across a line/area time chart sets one. KPIs compare with the previous period of the same length.

## filters

Fields shown as multi-select menus above the panels: `"team"` or `{"field": "team", "label": "Team"}`.
Use 1-4 low-cardinality string fields. A filter applies to every source that has the field.
Clicking a bar, slice, cell, row, waffle legend or meter row adds a **cross-filter** on that panel's category
(click again to remove). It shows as a chip. The clicked panel highlights instead of filtering itself.

## measures (the semantic layer)

Define every number once; panels refer to measures by name. Inline aggregates also work anywhere a measure
is expected: `"sum(cost_usd)"`, `"count()"`, `"count_distinct(user)"`, `"avg(x)"`, `"p95(latency_ms)"`.
Prefer named measures: they carry label, format and direction.

| Key | Meaning |
|---|---|
| `agg` | `sum avg min max count count_distinct median p50 p75 p90 p95 p99 wavg first last` |
| `field` | Numeric column (any column for `count_distinct`, `first`, `last`; none for `count`). |
| `weight` | For `wavg`: weighted average, e.g. latency weighted by `requests`. |
| `where` | Count only matching rows: `{"event": "signup"}`, `{"status": ["500", "503"]}`, `{"latency_ms": {"gt": 300}}` (`gt gte lt lte ne`). Several keys must all match (AND). A date string compares as a time: `{"created_at": {"gte": "2026-10-20"}}`. |
| `ratio` | `["numerator", "denominator"]` measure names. Computed after grouping, so it stays right under every filter. No `agg`. |
| `format` | `number int decimal usd pct ms s bytes tokens`. `pct` expects a 0-1 fraction. |
| `label` | Axis, legend, tooltip and table name. |
| `good` | `up` (default) or `down`. Colors KPI deltas and decides which side of a threshold is bad. |
| `thresholds` | `{"warning": n, "serious": n, "critical": n}` (any subset). Shows status icon + label in KPIs, tables, meters. With `good: "down"`, values above are bad; with `up`, below. |
| `scale` | Multiply the result: `{"agg": "sum", "field": "requests", "scale": 0.005}` is an error budget of 0.5% of requests. |

Blank cells are null: every agg skips them except `count()`, which counts rows. An average CSAT over a column
that is half blank is the average of the answered ones; say so in the label ("CSAT, rated tickets").

Recipes:
- Share meeting a target: `"p1": {"agg": "count", "where": {"priority": "P1"}}`,
  `"p1_ok": {"agg": "count", "where": {"priority": "P1", "first_response_min": {"lte": 30}}}`,
  `"p1_met": {"ratio": ["p1_ok", "p1"], "format": "pct", "thresholds": {"warning": 0.9, "critical": 0.8}}`.
- Before / after a change: `"frt_before": {"agg": "median", "field": "first_response_min", "where": {"created_at": {"lt": "2026-10-20"}}}`
  and `"frt_after"` with `gte`; then `{"type": "bar", "x": "queue", "y": ["frt_before", "frt_after"]}`.

Pitfalls: percentiles of pre-aggregated percentiles are not percentiles (say "avg of p95" in the label or use
`wavg`). A rate is a `ratio` of two sums, never an `avg` of per-row rates. `count()` counts rows, not users.

## Dimensions

A dimension is a field name. Time fields take a grain or a part after a colon:
`date` (auto grain from the range), `date:day`, `ts:hour`, `date:week` (weeks start Monday), `month`, `quarter`,
`year`, `15m`; parts: `ts:hour_of_day`, `date:weekday`, `date:month_of_year`.
Categories past a panel's `limit` fold into "Other". Colors follow the value (Support is always the same color,
whatever the filter): the 8 most frequent values of a field get the 8 palette colors, the rest are gray.

## Panels

Common keys: `id` (required in practice: letters, digits, `-`, `_`; the agent reads and drives panels by id),
`type`, `title`, `subtitle`, `note` (small print under the panel), `w` (columns of 12; phones stack to 12),
`h` (rows of ~110 px), `source` (default: the first), `crossfilter` (false: clicks do nothing),
`where` (only rows matching this, e.g. `{"model": "gpt-4.1"}`; filters and the time range still apply), `period` (`range`, the default: follow the time
picker; or a preset such as `mtd` or `all`: this panel keeps that window whatever the picker says, and its KPI
compares with the period before it). Say so in the subtitle when a panel ignores the picker.

| type | Required | Optional | Default w×h | Use for |
|---|---|---|---|---|
| `kpi` | `measure` | `compare` (`previous`/`none`), `sparkline`, `target` | 3×1 | Headline number + delta + sparkline |
| `callout` | `measure`, `text` | `icon`, `compare` | 4×1 | Infographic hero number with a sentence; `{{measure}}` in text |
| `line` | `x`, `y` | `series`, `limit` (6), `marks`, `events`, `highlight`, `brush`, `drill`, `cumulative` | 8×3 | Trend over time; `cumulative` = running total (burn-up) |
| `area` | `x`, `y` | + `stack`, `normalize` | 8×3 | Volume over time; stacked composition; `normalize` = share |
| `bar` | `x`, `y` | `series`, `limit` (12), `stack`, `normalize`, `orientation`, `sort` (`value`/`label`/`none`), `marks`, `highlight`, `drill`, `labels` | 6×3 | Compare categories; turns horizontal for many or long labels |
| `donut` | `dim`, `measure` | `limit` (6) | 4×3 | One whole, ≤ 6 parts, at a glance |
| `waffle` | `dim`, `measure` | `limit` (5) | 4×2 | Infographic share: 100 squares |
| `scatter` | `by`, `x`, `y` | `size`, `color` (≤ 3 values), `limit`, `labels` (5) | 6×3 | Two measures per entity (volume vs unit cost) |
| `heatmap` | `x`, `y`, `measure` | `limit`, `labels` | 6×3 | Two categories, or weekday × hour |
| `treemap` | `dims` (1-2), `measure` | `limit` (10) | 6×3 | Hierarchical share (app › model) |
| `funnel` | `steps` | `cohort` (user field) | 6×3 | Ordered conversion: `[{"label": "Visit", "measure": "visitors"}, …]` |
| `budget` | `measure`, `budget` | | 6×3 | Cumulative use vs a budget, projected to the period's end (with `period: "mtd"`) |
| `flow` | `path` (2+ category fields), `measure` | `limit` (8 per level) | 12×4 | Sankey: how a total splits from one category to the next |
| `retention` | `user`, `time`, `start`, `return` | `grain` (week), `periods` (8), `cohorts` (12) | 12×4 | Cohort table: `start`/`return` are `where` objects |
| `meter` | `measure` | `by`, `max`, `target`, `limit`, `sort` (`value`/`label`/`worst`) | 4×2 | One ratio vs a limit, or a list (SLO per service) |
| `table` | `measures` | `dims`, `sort`, `order`, `limit` (50), `bars`, `drill` | 12×4 | Exact values; click a header to sort |
| `text` | `text` | | 4×2 | Notes, `**bold**`, `- bullets`, `{{measure}}` values |
| `section` | `title` | `text` | 12 | Heading that groups the panels after it |
| `echarts` | `query`, `option` | `filterOn` | 6×3 | Escape hatch, see below |

Details:
- `y` / `measures` take one name or a list. Several measures in one chart must share a `format`: one chart,
  one axis. A dual axis is refused. Use `series` **or** several `y`, not both.
- `marks`: horizontal reference lines `[{"y": 0.995, "label": "SLO"}]`. `events`: vertical lines
  `[{"x": "2026-10-17", "label": "Price change"}]`.
- `highlight`: one `series` value in color, the rest gray (the emphasis form).
- `drill`: category levels, top first, first = `x` (or the table's dim): `["team", "user"]`. A click filters to the
  value and shows the next level; a breadcrumb goes back.
- `meter`: for `pct` measures the scale is 0-1, zoomed to the interesting range when thresholds exist; set `max` for
  other units. `target` draws a tick.
- `retention`: `{"type": "retention", "user": "user_id", "time": "date", "start": {"event": "signup"},
  "return": {"event": "active"}, "grain": "week", "periods": 8}`. Cohorts start inside the time range; immature
  cells stay blank.
- `funnel` steps are usually `count_distinct` measures with `where`, e.g.
  `"signups": {"agg": "count_distinct", "field": "user_id", "where": {"event": "signup"}}`. Always set
  `"cohort": "user_id"` on event data: the funnel then follows the users whose first step falls in the time range,
  through later steps at any time. Without it each step counts only events inside the range, and a short range shows
  0 visits next to 27 subscriptions.
- `budget`: `{"type": "budget", "measure": "cost", "budget": 900, "period": "mtd"}` shows month-to-date spend, the
  projected month end at the current pace, an even-pace line and a status (within, close to, over budget). `budget`
  is a number or a measure: an SLO error budget is `{"type": "budget", "measure": "errors", "budget":
  "error_budget"}` with `"error_budget": {"agg": "sum", "field": "requests", "scale": 0.005}` over the picked range.
- `flow`: `{"type": "flow", "path": ["channel", "furthest_step"], "measure": "users"}`. Rows need one value per
  level, so per-entity facts (a user's furthest step) come from a derived source, usually an app.py route (see
  SKILL.md). Clicking a node cross-filters on that level's field.
- `cumulative` (line/area): running total of a sum or count measure over a time x; future buckets stay empty.
- Icons: `chart users cost clock bolt alert check server cpu globe chat funnel target rocket shield star`.

### Escape hatch: `echarts`

For a chart no built-in type draws (radar, boxplot, a forecast band). The renderer runs the query with the
dashboard's time range and filters, then puts the result in `option.dataset.source` (`[header, ...rows]`; header =
dim names then measure names). Write ECharts option JSON that reads the dataset through `encode`:

```json
{ "id": "cost-box", "type": "echarts", "title": "Cost by app",
  "query": { "dims": ["app"], "measures": ["cost"], "sort": "cost", "limit": 10 },
  "option": { "xAxis": { "type": "category" }, "yAxis": { "type": "value" },
              "series": [{ "type": "bar", "encode": { "x": "app", "y": "cost" } }] },
  "filterOn": "app" }
```

Options are JSON only (no functions). Use it rarely: you lose the built-in formatting, summaries and colors by
entity. For anything more custom, register a panel type in JS (`reference/custom-types.md`).

## What the agent sees (AWB SDK, automatic)

- `ReadMiniAppContext` summary: title, time range, filters, one line per panel (`id [type] title: key numbers`).
- Details: one per panel id (its data table as TSV), plus `spec` and `fields`.
- Commands: `setTimeRange {range}`, `setFilter {field, values "a|b"}`, `clearFilters`, `drill {panel, value}`,
  `showTable {panel, on}`, `focusPanel {panel}`, `setTheme {theme}`.
- Pointing: every panel, table row, meter row and legend item is labeled; a click on a chart names the mark under
  the pointer (date and every series value, or the bar, slice or cell) plus the active filters.
