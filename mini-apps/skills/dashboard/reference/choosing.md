# Choosing panels and laying them out

## Question -> panel

| The question | Panel | Notes |
|---|---|---|
| What is the number now, and is it up or down? | `kpi` | 3-4 per row, w 3, h 1. Delta vs the previous period is automatic. |
| One big number with a sentence (infographic) | `callout` | `{{measure}}` in `text`; pick an `icon`. |
| How did it change over time? | `line` | `x: "date:day"` (or `ts:hour`). ≤ 6 series; more fold into "Other". |
| How much in total over time, and from whom? | `area` + `stack` | `normalize` for shares over time. |
| Which category is biggest? | `bar` | Sorted by value; turns horizontal for many or long labels. |
| Share of one whole, ≤ 6 parts | `donut` or `waffle` | More parts: a horizontal `bar`. |
| Two measures per entity (volume vs unit cost) | `scatter` | `size` for a third; `color` only with ≤ 3 values. |
| When / where does it happen? (weekday × hour, service × region) | `heatmap` | Sequential color; `labels` for small grids. |
| Hierarchical share (app › model) | `treemap` | 1-2 dims. |
| Are we within the limit? (per service) | `meter` with `by` | `target` tick, `thresholds` on the measure, `sort: "worst"`. |
| Will we stay within the budget? | `budget` | `period: "mtd"` for monthly money; a measure `budget` for SLO error budgets. |
| How do users move through steps? | `funnel` with `cohort` | Steps are `count_distinct` measures with `where`. |
| Where do they come from and end up? | `flow` | Sankey; needs one row per entity with a value per level. |
| Do users come back? | `retention` | `start` / `return` are `where` objects. |
| Running total (burn-up) | `line` + `cumulative` | Sum or count measures only. |
| Did we meet the target? (share within an SLA) | `kpi` / `meter` / `line` of a ratio | Two counts (one with `where`) and a `ratio`; `thresholds` for status. |
| Did a change help? (before vs after a date) | `bar` or `table` with two measures | Each measure has a `where` on the time field; add an `events` line to the trend. |
| Exact values to look up or sort | `table` | `bars: true` for in-cell bars; `drill` for levels. |
| Context the chart cannot say | `text` or a panel `note` | Short. Facts, not instructions. |

Never: two y-axes (split into two panels), pie with > 6 slices, 3-D, a line over unordered categories,
an average of rates, color without a label for status.

## Layout

- 12 columns. Rows fill left to right in spec order. Make each row add up to 12 and give its panels one `h`.
  Common rows: 4 × kpi (3+3+3+3, h 1); 6 + 6 (h 3); 8 + 4 (h 3); 12 (h 3-4).
- `h` is ~110 px per unit: kpi/callout 1, charts 3, tables 3-4, flow/retention 4.
- Order: KPIs, then the main question, then the "why" (breakdowns), then detail tables.
- `section` panels (w 12, no height) name the question the next rows answer. 2-4 sections on a page.
- 8-16 panels is typical. More is a second dashboard.
- Phones stack every panel to full width; nothing to do.

## Wording

- Title = what it shows ("Spend by team"). Subtitle = how to read it or what to click ("Click a team to see its
  users"). Units come from the measure's `format`; do not repeat them in titles.
- Say "request-weighted mean of p95" when you average pre-aggregated percentiles.
- Say when a panel ignores the picker: "Always the current month".

## Domain recipes

**LLM / API usage and spend** (`examples/llm-usage.json`): KPIs spend, requests, active users, error rate ·
`budget` with `period: "mtd"` + spend share `waffle` · stacked daily spend by model with `events` for changes ·
spend by team with `drill: ["team", "user"]` · `scatter` volume vs cost per request by user · weekday × team
`heatmap` · `treemap` app › model · models `table`. Measures: cost (sum, usd, good down), requests, tokens,
users (count_distinct), error_rate (ratio), cost_per_req (ratio), latency (wavg by requests).

**Service health and SLOs** (`examples/service-slo.json`): KPIs error rate, error budget used, p95, requests ·
section "SLO status" with three `meter`s by service (error rate vs limit, p95 vs SLO, budget used) · `budget`
with `budget: "error_budget"` (`scale: 0.005` of requests) · error rate and p95 by service over time with SLO
`marks` · service × region and hour-of-day heatmaps · traffic `area` by region · scorecard `table`.
Rows are 15-minute buckets: sums for counts, `wavg` for latency, ratios for rates.

**Product analytics** (`examples/product-funnel.json`): KPIs visitors and step rates · `funnel` with `cohort` ·
step conversion by channel (`bar` with several pct measures) · `flow` channel → furthest step from an app.py
route · weekly activation rate with an `events` marker for a launch · `retention` by signup week ·
country `table`. Events are one row per event: `count_distinct(user_id)` with `where`, never `count()`.
