# Custom panel types (last resort)

Use one only when no built-in panel, derived source or `note` can answer the question. The validator cannot
check what custom code draws, so keep it small (under ~60 lines) and test it in a browser.

Put it in `static/custom.js` (index.html loads it before rendering):

```js
AwbDash.registerType('target-gap', {
  req: ['measure', 'target'], opt: { measure: 'measure', target: 'number' }, w: 6, h: 3,  // keys the spec may use
  render: function (p) {
    var m = p.measure(p.spec.measure, 'measure');          // a named or inline measure
    var by = p.dim('team', 'by');                            // a dimension: {field, kind, key(row), label(v)}
    var groups = AwbDash.kit.aggregate(p.rows(), [by], [m], p.dash.measure);  // [{keys: ['Support'], v: {cost: 12.5}}]
    if (!groups.length) { p.body.textContent = 'No data in this range.'; p.summaryText = 'no data'; return; }
    p.draw({                                                  // any ECharts option; the theme is applied
      xAxis: { type: 'category', data: groups.map(function (g) { return g.keys[0]; }) },
      yAxis: { type: 'value' },
      series: [{ type: 'bar', data: groups.map(function (g) {
        return { value: g.v[m.name], itemStyle: { color: p.dash.colorOf('team', g.keys[0]) } }; }) }]
    });
    p.onEc('click', function (e) { p.dash.toggleFilter('team', String(e.name)); });   // cross-filter
    p.table = { columns: ['team', m.label], rows: groups.map(function (g) { return [g.keys[0], AwbDash.kit.format(g.v[m.name], m.format)]; }) };
    p.summaryText = groups.length + ' teams; target ' + p.spec.target;   // what the chat agent reads
  }
});
```

The panel object `p`:

| Member | What it gives |
|---|---|
| `p.spec` | The panel's JSON. |
| `p.rows()` | Rows after the time range (or the panel's `period`), the panel `where` and the filters. **Use this**, never the raw source, or the panel ignores the filters. |
| `p.rows({previous: true})` | The previous period of the same length. `{noTime: true}`: ignore the time range. |
| `p.range()` | `{from, to}` epoch ms of the window this panel shows. |
| `p.measure(name, where)`, `p.dim(spec, where)` | Resolve like built-ins; errors show as a red card with `where`. |
| `p.total(m)` | One aggregated value over `p.rows()`. |
| `p.draw(option)` | Draw ECharts in the panel. `p.ec` is the chart afterwards; `p.onEc(event, fn)` binds a handler. |
| `p.body` | The panel's element, for HTML instead of a chart. |
| `p.dash.theme` | `{series[8], accent, text, text2, muted, grid, axis, surface, other, status{good,warning,serious,critical}, seq[3], dark}`. |
| `p.dash.colorOf(field, value)` | The entity's color, the same as in every other panel. |
| `p.dash.toggleFilter(field, value)` | Cross-filter like a built-in click. |
| `p.summaryText` | Set it: the line the agent reads. |
| `p.table` | Set it: `{columns, rows}` of strings for the Table toggle and the agent's detail. |
| `p.pointAt` | Optional `function (x, y)` returning what is under a click at panel pixel x, y. |

`AwbDash.kit`: `aggregate`, `format(v, fmt)`, `formatDelta`, `fmtTime(ms, grain)`, `bucket(ms, grain)`,
`parseCsv`, `inferTypes`, `el(tag, attrs, children)`, `icon(name)`.

The `echarts` panel type (in spec.md) needs no JS: a query plus option JSON. Prefer it for a one-off chart form.
