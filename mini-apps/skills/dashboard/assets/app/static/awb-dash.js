/* AWB Dash 0.1.0 - declarative dashboards for AgentWorkBench Mini Apps.
 * One JSON spec -> filters, time range, cross-filtering, drill-down, 18 panel types, light/dark themes,
 * table views, and AWB Mini App SDK wiring (point at any mark, ReadMiniAppContext, RunMiniAppCommand).
 * Needs echarts.min.js (vendored, 6.1.0) loaded first. No build step, no eval, no network beyond the app origin.
 * Public API: AwbDash.render(target, specOrUrl) -> Promise<Dashboard>; AwbDash.registerType(name, impl);
 *             AwbDash.kit (load, aggregate, format, theme) for custom panels.
 */
(function () {
  'use strict';
  var VERSION = '0.2.0';

  /* SCHEMA-BEGIN (read by scripts/check_dashboard.py; keep it plain JSON) */
  var SCHEMA = {
    "top": { "version": "int", "title": "string", "description": "string", "sources": "sources", "measures": "measures",
      "time": "time", "filters": "filters", "panels": "panels", "theme": ["auto", "light", "dark"], "refresh": "int",
      "colors": "object" },
    "common": { "id": "id", "type": "string", "title": "string", "subtitle": "string", "w": "int", "h": "int",
      "source": "source", "note": "string", "crossfilter": "bool", "period": "period", "where": "where" },
    "types": {
      "kpi":       { "req": ["measure"], "opt": { "measure": "measure", "compare": ["previous", "none"], "sparkline": "bool", "target": "number" }, "w": 3, "h": 1 },
      "callout":   { "req": ["measure", "text"], "opt": { "measure": "measure", "text": "string", "icon": "icon", "compare": ["previous", "none"] }, "w": 4, "h": 1 },
      "line":      { "req": ["x", "y"], "opt": { "x": "dim", "y": "measures", "series": "dim", "limit": "int", "marks": "marks", "events": "events", "highlight": "string", "brush": "bool", "drill": "dims", "cumulative": "bool" }, "w": 8, "h": 3 },
      "area":      { "req": ["x", "y"], "opt": { "x": "dim", "y": "measures", "series": "dim", "limit": "int", "stack": "bool", "normalize": "bool", "marks": "marks", "events": "events", "highlight": "string", "brush": "bool", "drill": "dims", "cumulative": "bool" }, "w": 8, "h": 3 },
      "bar":       { "req": ["x", "y"], "opt": { "x": "dim", "y": "measures", "series": "dim", "limit": "int", "stack": "bool", "normalize": "bool", "orientation": ["auto", "vertical", "horizontal"], "sort": ["value", "label", "none"], "marks": "marks", "highlight": "string", "drill": "dims", "labels": "bool" }, "w": 6, "h": 3 },
      "donut":     { "req": ["dim", "measure"], "opt": { "dim": "dim", "measure": "measure", "limit": "int" }, "w": 4, "h": 3 },
      "scatter":   { "req": ["by", "x", "y"], "opt": { "by": "dim", "x": "measure", "y": "measure", "size": "measure", "color": "dim", "limit": "int", "labels": "int" }, "w": 6, "h": 3 },
      "heatmap":   { "req": ["x", "y", "measure"], "opt": { "x": "dim", "y": "dim", "measure": "measure", "limit": "int", "labels": "bool" }, "w": 6, "h": 3 },
      "treemap":   { "req": ["dims", "measure"], "opt": { "dims": "dims", "measure": "measure", "limit": "int" }, "w": 6, "h": 3 },
      "funnel":    { "req": ["steps"], "opt": { "steps": "steps", "cohort": "field" }, "w": 6, "h": 3 },
      "budget":    { "req": ["measure", "budget"], "opt": { "measure": "measure", "budget": "budget" }, "w": 6, "h": 3 },
      "flow":      { "req": ["path", "measure"], "opt": { "path": "dims", "measure": "measure", "limit": "int" }, "w": 12, "h": 4 },
      "retention": { "req": ["user", "time", "start", "return"], "opt": { "user": "field", "time": "field", "start": "where", "return": "where", "grain": ["day", "week", "month"], "periods": "int", "cohorts": "int" }, "w": 12, "h": 4 },
      "table":     { "req": ["measures"], "opt": { "dims": "dims", "measures": "measures", "sort": "string", "order": ["asc", "desc"], "limit": "int", "bars": "bool", "drill": "dims" }, "w": 12, "h": 4 },
      "meter":     { "req": ["measure"], "opt": { "measure": "measure", "by": "dim", "max": "number", "target": "number", "limit": "int", "sort": ["value", "label", "worst"] }, "w": 4, "h": 2 },
      "waffle":    { "req": ["dim", "measure"], "opt": { "dim": "dim", "measure": "measure", "limit": "int" }, "w": 4, "h": 2 },
      "text":      { "req": ["text"], "opt": { "text": "string" }, "w": 4, "h": 2 },
      "section":   { "req": ["title"], "opt": { "text": "string" }, "w": 12, "h": 0 },
      "echarts":   { "req": ["query", "option"], "opt": { "query": "query", "option": "object", "filterOn": "dim" }, "w": 6, "h": 3 }
    },
    "aggs": ["sum", "avg", "min", "max", "count", "count_distinct", "median", "p50", "p75", "p90", "p95", "p99", "wavg", "first", "last"],
    "formats": ["number", "int", "decimal", "usd", "pct", "ms", "s", "bytes", "tokens"],
    "grains": ["15m", "hour", "day", "week", "month", "quarter", "year"],
    "parts": ["hour_of_day", "weekday", "month_of_year"],
    "presets": ["1h", "6h", "24h", "7d", "14d", "30d", "90d", "180d", "1y", "mtd", "qtd", "ytd", "all"],
    "icons": ["chart", "users", "cost", "clock", "bolt", "alert", "check", "server", "cpu", "globe", "chat", "funnel", "target", "rocket", "shield", "star"]
  };
  /* SCHEMA-END */

  var DAY = 86400000, HOUR = 3600000;
  var GRAIN_MS = { '15m': 900000, hour: HOUR, day: DAY, week: 7 * DAY, month: 30 * DAY, quarter: 91 * DAY, year: 365 * DAY };
  var WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  var MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  var OTHER = 'Other';
  var MAX_ROWS = 300000;

  /* ------------------------------------------------------------------ small utils */
  function h(tag, attrs, kids) {
    var el = document.createElement(tag);
    if (attrs) for (var k in attrs) {
      var v = attrs[k];
      if (v == null || v === false) continue;
      if (k === 'text') el.textContent = v;
      else if (k === 'class') el.className = v;
      else if (k.slice(0, 2) === 'on') el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? '' : v);
    }
    (kids || []).forEach(function (c) { if (c != null) el.appendChild(typeof c === 'string' ? document.createTextNode(c) : c); });
    return el;
  }
  function esc(s) { return String(s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function cut(s, n) { s = String(s); return s.length > n ? s.slice(0, n - 1) + '…' : s; }
  function arr(x) { return x == null ? [] : Array.isArray(x) ? x : [x]; }
  function fail(msg) { throw new Error(msg); }
  function closest(name, list) {
    var best = null, bd = 1e9;
    list.forEach(function (c) { var d = lev(String(name).toLowerCase(), String(c).toLowerCase()); if (d < bd) { bd = d; best = c; } });
    return best != null && bd <= Math.max(2, Math.floor(String(name).length / 3)) ? best : null;
  }
  function lev(a, b) {
    var m = [], i, j;
    for (i = 0; i <= b.length; i++) m[i] = [i];
    for (j = 0; j <= a.length; j++) m[0][j] = j;
    for (i = 1; i <= b.length; i++) for (j = 1; j <= a.length; j++)
      m[i][j] = Math.min(m[i - 1][j] + 1, m[i][j - 1] + 1, m[i - 1][j - 1] + (b[i - 1] === a[j - 1] ? 0 : 1));
    return m[b.length][a.length];
  }
  function didYouMean(name, list) { var c = closest(name, list); return c ? ' Did you mean "' + c + '"?' : ' Known: ' + list.slice(0, 20).join(', ') + '.'; }

  /* ------------------------------------------------------------------ formatting */
  var compactFmt = new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 1 });
  function num(v, dp) { return v.toLocaleString('en-US', { maximumFractionDigits: dp, minimumFractionDigits: 0 }); }
  function format(v, f) {
    if (v == null || typeof v !== 'number' || !isFinite(v)) return '–';
    var a = Math.abs(v);
    switch (f) {
      case 'usd': return (v < 0 ? '-$' : '$') + (a >= 10000 ? compactFmt.format(a) : a >= 100 ? num(a, 0) : a >= 1 || a === 0 ? num(a, 2) : Number(a.toPrecision(2)).toString());
      case 'pct': { var p = v * 100, ap = Math.abs(p); return num(p, ap >= 99 && ap < 100 ? 2 : ap < 1 && ap > 0 ? 2 : 1) + '%'; }
      case 'ms': return a >= 1000 ? num(v / 1000, 2) + ' s' : num(v, a < 10 ? 1 : 0) + ' ms';
      case 's': return a >= 3600 ? num(v / 3600, 1) + ' h' : a >= 60 ? num(v / 60, 1) + ' min' : num(v, 1) + ' s';
      case 'bytes': { var u = ['B', 'KB', 'MB', 'GB', 'TB'], i = 0; while (a >= 1024 && i < 4) { a /= 1024; v /= 1024; i++; } return num(v, 1) + ' ' + u[i]; }
      case 'tokens': return a >= 1000 ? compactFmt.format(v) : num(v, 0);
      case 'int': return a >= 1e6 ? compactFmt.format(v) : num(Math.round(v), 0);
      case 'decimal': return num(v, 2);
      default: return a >= 10000 ? compactFmt.format(v) : num(v, a < 10 ? 2 : a < 100 ? 1 : 0);
    }
  }
  function niceCeil(v) {
    if (!(v > 0)) return 1;
    var e = Math.pow(10, Math.floor(Math.log10(v))), f = v / e;
    return (f <= 1 ? 1 : f <= 1.2 ? 1.2 : f <= 1.5 ? 1.5 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 3 ? 3 : f <= 4 ? 4 : f <= 5 ? 5 : f <= 6 ? 6 : f <= 8 ? 8 : 10) * e;
  }
  function formatDelta(cur, prev, f) {
    if (cur == null || prev == null || !isFinite(cur) || !isFinite(prev)) return null;
    if (f === 'pct') { var pp = (cur - prev) * 100; return { v: pp, text: (pp >= 0 ? '+' : '') + num(pp, Math.abs(pp) < 1 ? 2 : 1) + ' pp' }; }
    if (prev === 0) return cur === 0 ? { v: 0, text: '0%' } : null;
    var r = (cur - prev) / Math.abs(prev);
    return { v: r, text: (r >= 0 ? '+' : '') + num(r * 100, Math.abs(r) < 0.1 ? 1 : 0) + '%' };
  }
  function pad(n) { return n < 10 ? '0' + n : '' + n; }
  function fmtTime(t, grain) {
    var d = new Date(t);
    var Y = d.getUTCFullYear(), M = d.getUTCMonth(), D = d.getUTCDate();
    switch (grain) {
      case 'year': return '' + Y;
      case 'quarter': return 'Q' + (Math.floor(M / 3) + 1) + ' ' + Y;
      case 'month': return MONTHS[M] + ' ' + Y;
      case 'week': return 'wk of ' + MONTHS[M] + ' ' + D;
      case 'day': return MONTHS[M] + ' ' + D + ', ' + Y;
      default: return MONTHS[M] + ' ' + D + ' ' + pad(d.getUTCHours()) + ':' + pad(d.getUTCMinutes()) + ' UTC';
    }
  }
  function isoDay(t) { var d = new Date(t); return d.getUTCFullYear() + '-' + pad(d.getUTCMonth() + 1) + '-' + pad(d.getUTCDate()); }

  /* ------------------------------------------------------------------ data loading */
  var TIME_RE = /^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?$/;
  function parseTime(s) {
    if (typeof s === 'number') return s;
    if (s.length === 10) return Date.parse(s + 'T00:00:00Z');
    var t = s.replace(' ', 'T');
    if (!/(Z|[+-]\d{2}:?\d{2})$/.test(t)) t += 'Z';
    return Date.parse(t);
  }
  function parseCsv(text) {
    var rows = [], row = [], f = '', q = false, i = 0, c, n = text.length;
    if (text.charCodeAt(0) === 0xfeff) i = 1;
    for (; i < n; i++) {
      c = text[i];
      if (q) {
        if (c === '"') { if (text[i + 1] === '"') { f += '"'; i++; } else q = false; }
        else f += c;
      } else if (c === '"') q = true;
      else if (c === ',') { row.push(f); f = ''; }
      else if (c === '\n' || c === '\r') {
        if (c === '\r' && text[i + 1] === '\n') i++;
        row.push(f); f = ''; if (row.length > 1 || row[0] !== '') rows.push(row); row = [];
      } else f += c;
    }
    if (f !== '' || row.length) { row.push(f); rows.push(row); }
    if (!rows.length) return [];
    var head = rows[0].map(function (x) { return x.trim(); });
    return rows.slice(1).map(function (r) { var o = {}; for (var j = 0; j < head.length; j++) o[head[j]] = r[j] === undefined ? '' : r[j]; return o; });
  }
  function inferTypes(rows, overrides) {
    var fields = {}, names = rows.length ? Object.keys(rows[0]) : [];
    names.forEach(function (name) {
      if (overrides && overrides[name]) { fields[name] = { type: overrides[name] }; return; }
      var isNum = true, isTime = true, seen = 0;
      for (var i = 0; i < rows.length && seen < 1000; i++) {
        var v = rows[i][name];
        if (v === '' || v == null) continue;
        seen++;
        if (typeof v === 'number') { isTime = false; continue; }
        if (typeof v === 'boolean') { isNum = false; isTime = false; break; }
        var s = String(v);
        if (isNum && !(s.trim() !== '' && isFinite(Number(s)))) isNum = false;
        if (isTime && !TIME_RE.test(s)) isTime = false;
        if (!isNum && !isTime) break;
      }
      fields[name] = { type: seen === 0 ? 'string' : isTime && !isNum ? 'time' : isNum ? 'number' : 'string' };
    });
    names.forEach(function (name) {
      var t = fields[name].type;
      if (t === 'string') return;
      for (var i = 0; i < rows.length; i++) {
        var v = rows[i][name];
        if (v === '' || v == null) rows[i][name] = null;
        else if (t === 'number') rows[i][name] = typeof v === 'number' ? v : Number(v);
        else if (t === 'time') rows[i][name] = parseTime(v);
      }
    });
    names.forEach(function (name) {
      if (fields[name].type !== 'string') return;
      for (var i = 0; i < rows.length; i++) { var v = rows[i][name]; rows[i][name] = v == null ? '' : String(v); }
    });
    return fields;
  }
  function loadSource(name, def) {
    var p;
    if (Array.isArray(def.rows)) p = Promise.resolve(def.rows.map(function (r) { return Object.assign({}, r); }));
    else if (typeof def.csv === 'string') p = Promise.resolve(parseCsv(def.csv));
    else if (typeof def.url === 'string') {
      if (/^[a-z]+:|^\/\//i.test(def.url)) return Promise.reject(new Error('source "' + name + '": url must be relative to the app (got ' + def.url + ').'));
      p = fetch(def.url, { credentials: 'same-origin' }).then(function (r) {
        if (!r.ok) throw new Error('source "' + name + '": GET ' + def.url + ' answered ' + r.status + '.');
        var isCsv = def.format === 'csv' || (!def.format && (/\.csv(\?|$)/i.test(def.url) || /text\/csv/.test(r.headers.get('content-type') || '')));
        return isCsv ? r.text().then(parseCsv) : r.json().then(function (j) {
          var rows = Array.isArray(j) ? j : j && Array.isArray(j.rows) ? j.rows : null;
          if (!rows) throw new Error('source "' + name + '": JSON must be an array of row objects or {"rows": [...]}.');
          return rows;
        });
      });
    } else return Promise.reject(new Error('source "' + name + '" needs "url", "rows" or "csv".'));
    return p.then(function (rows) {
      if (rows.length > MAX_ROWS) throw new Error('source "' + name + '" has ' + rows.length + ' rows; the browser limit is ' + MAX_ROWS + '. Aggregate on the server (app.py) first.');
      var fields = inferTypes(rows, def.types);
      var timeField = def.timeField || null;
      return { name: name, rows: rows, fields: fields, timeField: timeField, def: def };
    });
  }

  /* ------------------------------------------------------------------ time */
  function bucket(t, grain) {
    var d = new Date(t);
    switch (grain) {
      case '15m': return t - (t % 900000);
      case 'hour': return t - (t % HOUR);
      case 'day': return Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate());
      case 'week': { var day = Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()); return day - ((d.getUTCDay() + 6) % 7) * DAY; }
      case 'month': return Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), 1);
      case 'quarter': return Date.UTC(d.getUTCFullYear(), Math.floor(d.getUTCMonth() / 3) * 3, 1);
      case 'year': return Date.UTC(d.getUTCFullYear(), 0, 1);
    }
    return t;
  }
  function nextBucket(t, grain) {
    var d = new Date(t);
    if (grain === 'month') return Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + 1, 1);
    if (grain === 'quarter') return Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + 3, 1);
    if (grain === 'year') return Date.UTC(d.getUTCFullYear() + 1, 0, 1);
    return t + GRAIN_MS[grain];
  }
  function autoGrain(span, step) {
    var g = span <= 2 * DAY ? (span <= 6 * HOUR ? '15m' : 'hour') : span <= 120 * DAY ? 'day' : span <= 2 * 365 * DAY ? 'week' : 'month';
    var order = SCHEMA.grains;
    while (GRAIN_MS[g] < step && order.indexOf(g) < order.length - 1) g = order[order.indexOf(g) + 1];
    return g;
  }

  /* ------------------------------------------------------------------ dimensions & measures */
  function parseDim(dash, src, spec, where) {
    if (typeof spec !== 'string' || !spec) fail(where + ': a dimension is a field name string such as "team" or "date:week".');
    var parts = spec.split(':'), field = parts[0], mod = parts[1];
    var f = src.fields[field];
    if (!f) fail(where + ': source "' + src.name + '" has no field "' + field + '".' + didYouMean(field, Object.keys(src.fields)));
    if (f.type === 'time') {
      if (mod && SCHEMA.parts.indexOf(mod) >= 0) {
        var fn = mod === 'hour_of_day' ? function (t) { return new Date(t).getUTCHours(); }
          : mod === 'weekday' ? function (t) { return (new Date(t).getUTCDay() + 6) % 7; } : function (t) { return new Date(t).getUTCMonth(); };
        var labels = mod === 'hour_of_day' ? null : mod === 'weekday' ? WEEKDAYS : MONTHS;
        return { spec: spec, field: field, kind: 'part', key: function (r) { return r[field] == null ? null : fn(r[field]); },
          label: function (v) { return labels ? labels[v] : pad(v) + ':00'; }, count: mod === 'hour_of_day' ? 24 : mod === 'weekday' ? 7 : 12 };
      }
      if (mod && SCHEMA.grains.indexOf(mod) < 0) fail(where + ': "' + mod + '" is not a time grain or part. Use one of ' + SCHEMA.grains.concat(SCHEMA.parts).join(', ') + '.');
      var grain = mod || dash.grain(src);
      return { spec: spec, field: field, kind: 'time', grain: grain, key: function (r) { return r[field] == null ? null : bucket(r[field], grain); },
        label: function (v) { return fmtTime(v, grain); } };
    }
    if (mod) fail(where + ': "' + field + '" is not a time field, so ":' + mod + '" does not apply.');
    var isNum = f.type === 'number';
    return { spec: spec, field: field, kind: 'cat', numeric: isNum, key: function (r) { var v = r[field]; return v == null || v === '' ? '(blank)' : isNum ? v : v; },
      label: function (v) { return String(v); } };
  }

  var INLINE_RE = /^([a-z_0-9]+)\(([^()]*)\)$/;
  function compileMeasure(name, def, srcFields, where) {
    var m = { name: name, label: def.label || name, format: def.format || 'number', agg: def.agg, field: def.field, weight: def.weight,
      where: def.where || null, ratio: def.ratio || null, good: def.good || 'up', thresholds: def.thresholds || null, description: def.description || '',
      scale: def.scale == null ? null : def.scale };
    if (m.scale != null && typeof m.scale !== 'number') fail(where + ': "scale" is a number the value is multiplied by (0.005 for 0.5% of requests).');
    if (m.ratio) {
      if (!Array.isArray(m.ratio) || m.ratio.length !== 2) fail(where + ': "ratio" is ["numerator measure", "denominator measure"].');
      return m;
    }
    if (SCHEMA.aggs.indexOf(m.agg) < 0) fail(where + ': agg "' + m.agg + '" is not supported.' + didYouMean(m.agg, SCHEMA.aggs));
    if (m.agg !== 'count' && !m.field) fail(where + ': agg "' + m.agg + '" needs "field".');
    if (m.agg === 'wavg' && !m.weight) fail(where + ': agg "wavg" needs "weight" (a numeric field such as requests).');
    if (m.format && SCHEMA.formats.indexOf(m.format) < 0) fail(where + ': format "' + m.format + '" is not supported.' + didYouMean(m.format, SCHEMA.formats));
    return m;
  }
  function inlineMeasure(expr) {
    var mm = INLINE_RE.exec(expr.replace(/\s+/g, ''));
    if (!mm) return null;
    var agg = mm[1], field = mm[2] || undefined;
    return { agg: agg, field: field, label: agg === 'count' && !field ? 'Rows' : agg.replace('_', ' ') + ' of ' + field };
  }
  function makeAcc(m) {
    switch (m.agg) {
      case 'sum': return { s: 0, n: 0, add: function (v) { if (v != null) { this.s += v; this.n++; } }, val: function () { return this.s; } };
      case 'avg': return { s: 0, n: 0, add: function (v) { if (v != null) { this.s += v; this.n++; } }, val: function () { return this.n ? this.s / this.n : null; } };
      case 'min': return { v: null, add: function (v) { if (v != null && (this.v == null || v < this.v)) this.v = v; }, val: function () { return this.v; } };
      case 'max': return { v: null, add: function (v) { if (v != null && (this.v == null || v > this.v)) this.v = v; }, val: function () { return this.v; } };
      case 'count': return { n: 0, add: function () { this.n++; }, val: function () { return this.n; } };
      case 'count_distinct': return { set: new Set(), add: function (v) { if (v != null && v !== '') this.set.add(v); }, val: function () { return this.set.size; } };
      case 'first': return { v: null, add: function (v) { if (this.v == null) this.v = v; }, val: function () { return this.v; } };
      case 'last': return { v: null, add: function (v) { if (v != null) this.v = v; }, val: function () { return this.v; } };
      case 'wavg': return { s: 0, w: 0, add: function (v, w) { if (v != null && w != null) { this.s += v * w; this.w += w; } }, val: function () { return this.w ? this.s / this.w : null; } };
      default: { // percentiles
        var q = m.agg === 'median' ? 0.5 : Number(m.agg.slice(1)) / 100;
        return { a: [], add: function (v) { if (v != null) this.a.push(v); }, val: function () {
          var a = this.a; if (!a.length) return null; a.sort(function (x, y) { return x - y; });
          var i = (a.length - 1) * q, lo = Math.floor(i), hi = Math.ceil(i); return a[lo] + (a[hi] - a[lo]) * (i - lo); } };
      }
    }
  }
  // A date string in a where comparison compares as a time: {"created_at": {"gte": "2026-10-20"}}.
  function cmpVal(x) { return typeof x === 'string' && TIME_RE.test(x) ? parseTime(x) : x; }
  function wherePass(where, r) {
    for (var k in where) {
      var want = where[k], v = r[k];
      if (Array.isArray(want)) { if (want.indexOf(v) < 0 && want.map(String).indexOf(String(v)) < 0) return false; }
      else if (want && typeof want === 'object') {
        if (want.gt != null && !(v > cmpVal(want.gt))) return false;
        if (want.gte != null && !(v >= cmpVal(want.gte))) return false;
        if (want.lt != null && !(v < cmpVal(want.lt))) return false;
        if (want.lte != null && !(v <= cmpVal(want.lte))) return false;
        if (want.ne != null && String(v) === String(want.ne)) return false;
      } else if (String(v) !== String(want)) return false;
    }
    return true;
  }
  // Aggregate rows into groups. dims: dim objects; ms: measure objects (ratios expand to their parts).
  function aggregate(rows, dims, ms, measureLookup) {
    var base = [], seen = {};
    function need(m) {
      if (seen[m.name]) return; seen[m.name] = 1;
      if (m.ratio) { m.ratio.forEach(function (n) { need(measureLookup(n)); }); }
      else base.push(m);
    }
    ms.forEach(need);
    var groups = new Map(), order = [];
    for (var i = 0; i < rows.length; i++) {
      var r = rows[i], keys = [], skip = false;
      for (var d = 0; d < dims.length; d++) { var k = dims[d].key(r); if (k == null) { skip = true; break; } keys.push(k); }
      if (skip) continue;
      var gk = keys.length === 1 ? keys[0] : keys.join('\u0001');
      var g = groups.get(gk);
      if (!g) { g = { keys: keys, accs: base.map(makeAcc) }; groups.set(gk, g); order.push(g); }
      for (var b = 0; b < base.length; b++) {
        var m = base[b];
        if (m.where && !wherePass(m.where, r)) continue;
        g.accs[b].add(m.field ? r[m.field] : 1, m.weight ? r[m.weight] : null);
      }
    }
    return order.map(function (g) {
      var vals = {};
      base.forEach(function (m, i) { var v = g.accs[i].val(); vals[m.name] = m.scale != null && v != null ? v * m.scale : v; });
      function value(m) {
        if (m.name in vals) return vals[m.name];
        var a = value(measureLookup(m.ratio[0])), b = value(measureLookup(m.ratio[1]));
        var q = a == null || b == null || b === 0 ? null : a / b;
        return (vals[m.name] = q != null && m.scale != null ? q * m.scale : q);
      }
      ms.forEach(value);
      return { keys: g.keys, v: vals };
    });
  }

  /* ------------------------------------------------------------------ theme */
  var themeSeq = 0;
  function readTheme(root) {
    var cs = getComputedStyle(root);
    function v(n) { return cs.getPropertyValue(n).trim(); }
    var t = { surface: v('--surface-1'), page: v('--page'), text: v('--text-primary'), text2: v('--text-secondary'), muted: v('--text-muted'),
      grid: v('--grid'), axis: v('--axis'), accent: v('--accent'), other: v('--other'), seq: [v('--seq-lo'), v('--seq-mid'), v('--seq-hi')],
      series: [1, 2, 3, 4, 5, 6, 7, 8].map(function (i) { return v('--s' + i); }),
      status: { good: v('--st-good'), warning: v('--st-warning'), serious: v('--st-serious'), critical: v('--st-critical') },
      dark: cs.colorScheme === 'dark' || v('color-scheme') === 'dark', font: v('--font') || 'system-ui, sans-serif' };
    var axis = { axisLine: { lineStyle: { color: t.axis } }, axisTick: { show: false }, axisLabel: { color: t.muted, fontSize: 11 },
      splitLine: { lineStyle: { color: t.grid, width: 1 } }, nameTextStyle: { color: t.muted } };
    var theme = {
      color: t.series, backgroundColor: 'transparent', textStyle: { fontFamily: t.font, color: t.text2 },
      categoryAxis: Object.assign({}, axis, { splitLine: { show: false } }), valueAxis: Object.assign({}, axis, { axisLine: { show: false } }),
      timeAxis: Object.assign({}, axis, { splitLine: { show: false } }),
      legend: { textStyle: { color: t.text2 }, icon: 'roundRect', itemWidth: 12, itemHeight: 8 },
      tooltip: { backgroundColor: t.surface, borderColor: t.axis, textStyle: { color: t.text, fontSize: 12 }, extraCssText: 'box-shadow:0 6px 18px rgba(0,0,0,.18);border-radius:8px;' },
      line: { symbol: 'circle', symbolSize: 6, lineStyle: { width: 2 } }, bar: { itemStyle: { borderRadius: 0 } }
    };
    t.name = 'awbd-' + (++themeSeq);
    echarts.registerTheme(t.name, theme);
    return t;
  }

  /* ------------------------------------------------------------------ icons (24px stroke icons) */
  var ICONS = {
    chart: 'M4 20V10M10 20V4M16 20v-7M22 20H2', users: 'M16 20v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2M9 10a4 4 0 1 0 0-8 4 4 0 0 0 0 8M22 20v-2a4 4 0 0 0-3-3.9M16 2.1a4 4 0 0 1 0 7.8',
    cost: 'M12 2v20M17 6H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6', clock: 'M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20M12 6v6l4 2',
    bolt: 'M13 2 3 14h9l-1 8 10-12h-9l1-8z', alert: 'M12 9v4M12 17h.01M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z',
    check: 'M22 11.1V12a10 10 0 1 1-5.9-9.1M22 4 12 14l-3-3', server: 'M2 4h20v6H2zM2 14h20v6H2zM6 7h.01M6 17h.01',
    cpu: 'M4 4h16v16H4zM9 9h6v6H9zM9 1v3M15 1v3M9 20v3M15 20v3M20 9h3M20 14h3M1 9h3M1 14h3',
    globe: 'M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20M2 12h20M12 2a15 15 0 0 1 0 20 15 15 0 0 1 0-20',
    chat: 'M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z', funnel: 'M22 3H2l8 9.5V19l4 2v-8.5L22 3z',
    target: 'M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20M12 18a6 6 0 1 0 0-12 6 6 0 0 0 0 12M12 14a2 2 0 1 0 0-4 2 2 0 0 0 0 4',
    rocket: 'M4.5 16.5c-1.5 1.3-2 5-2 5s3.7-.5 5-2c.7-.8.7-2.1-.1-2.9a2.2 2.2 0 0 0-2.9-.1zM12 15l-3-3a22 22 0 0 1 2-4A12.9 12.9 0 0 1 22 2c0 2.7-.8 7.5-6 11a22.4 22.4 0 0 1-4 2z',
    shield: 'M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z', star: 'M12 2l3.1 6.3 6.9 1-5 4.9 1.2 6.8L12 17.8 5.8 21l1.2-6.8-5-4.9 6.9-1z'
  };
  function icon(name) {
    var ns = 'http://www.w3.org/2000/svg', svg = document.createElementNS(ns, 'svg'), p = document.createElementNS(ns, 'path');
    svg.setAttribute('viewBox', '0 0 24 24'); svg.setAttribute('fill', 'none'); svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('stroke-width', '2'); svg.setAttribute('stroke-linecap', 'round'); svg.setAttribute('stroke-linejoin', 'round'); svg.setAttribute('aria-hidden', 'true');
    p.setAttribute('d', ICONS[name] || ICONS.chart); svg.appendChild(p); return svg;
  }

  /* ------------------------------------------------------------------ status from thresholds */
  function statusOf(m, v) {
    var t = m.thresholds; if (!t || v == null) return null;
    var lowerIsBad = m.good === 'up'; // for "up is good" measures, below the threshold is bad
    function past(x) { return x != null && (lowerIsBad ? v < x : v > x); }
    if (past(t.critical)) return 'critical';
    if (past(t.serious)) return 'serious';
    if (past(t.warning)) return 'warning';
    return 'good';
  }
  function statusEl(dash, st) {
    if (!st) return null;
    var lab = { good: 'OK', warning: 'Warning', serious: 'Serious', critical: 'Critical' }[st];
    return h('span', { class: 'awbd-status' }, [h('i', { style: 'background:' + dash.theme.status[st] }), lab]);
  }

  /* ------------------------------------------------------------------ panel types */
  var TYPES = {};

  function linkFilter(p, dim, value) {
    if (!dim || dim.kind === 'time') return;
    var dash = p.dash;
    if (p.drillDims && p.level < p.drillDims.length - 1) { dash.setFilter(dim.field, [String(value)], true); p.level++; dash.update(); return; }
    if (p.spec.crossfilter === false) return;
    dash.toggleFilter(dim.field, String(value));
  }
  // Nothing to draw: say so instead of an empty plot, and tell the agent the same.
  function emptyState(p) {
    p.disposeChart();
    var msg = 'No data in ' + p.dash.scopeText().replace(/^Showing /, '') + (p.periodSel() ? ' (this panel: ' + p.periodSel() + ')' : '') + '.';
    p.body.replaceChildren(h('div', { class: 'awbd-empty', text: msg }));
    p.table = { columns: ['note'], rows: [[msg]] }; p.summaryText = 'no data'; return true;
  }
  function selectedSet(p, dim) { return dim && p.dash.filters[dim.field] ? p.dash.filters[dim.field] : null; }

  function baseOption(p) {
    return { useUTC: true, animationDuration: 300, aria: { enabled: true, label: { enabled: true, description: p.summaryText || undefined } },
      textStyle: { fontFamily: p.dash.theme.font } };
  }
  function measureAxis(m, extra) {
    return Object.assign({ type: 'value', axisLabel: { formatter: function (v) { return format(v, m.format); } }, splitNumber: 4 }, extra || {});
  }
  function tooltipRow(color, name, value, line) {
    var key = line ? '<span style="display:inline-block;width:12px;height:2px;background:' + color + ';vertical-align:middle;margin-right:6px"></span>'
      : '<span style="display:inline-block;width:8px;height:8px;border-radius:2px;background:' + color + ';margin-right:6px"></span>';
    return '<div style="display:flex;gap:12px;justify-content:space-between"><span>' + key + esc(name) + '</span><b>' + esc(value) + '</b></div>';
  }
  function markLines(p, m) {
    var data = [];
    arr(p.spec.marks).forEach(function (mk) { if (mk && typeof mk.y === 'number') data.push({ yAxis: mk.y, label: { formatter: (mk.label ? mk.label + ' ' : '') + format(mk.y, m.format), position: 'insideEndTop', color: p.dash.theme.text2 } }); });
    var rg = p.range();
    arr(p.spec.events).forEach(function (ev) { var et = ev && ev.x ? parseTime(String(ev.x)) : null; if (et != null && et > rg.from && et < rg.to) data.push({ xAxis: parseTime(String(ev.x)), label: { formatter: ev.label || '', position: 'insideEndTop', color: p.dash.theme.text2 } }); });
    return data.length ? { symbol: 'none', silent: true, lineStyle: { color: p.dash.theme.muted, type: 'solid', width: 1 }, data: data } : undefined;
  }

  // Shared: categories with "Other" folding. Returns a dim whose key maps the tail to Other, and the kept list.
  function foldDim(p, rows, dim, m, limit) {
    if (dim.kind !== 'cat') return { dim: dim, kept: null };
    var totals = aggregate(rows, [dim], [m], p.dash.measure);
    totals.sort(function (a, b) { return (b.v[m.name] || 0) - (a.v[m.name] || 0); });
    if (totals.length <= limit) return { dim: dim, kept: totals.map(function (t) { return t.keys[0]; }) };
    var keep = new Set(totals.slice(0, limit - 1).map(function (t) { return t.keys[0]; }));
    var folded = Object.assign({}, dim, { key: function (r) { var k = dim.key(r); return k == null ? null : keep.has(k) ? k : OTHER; } });
    return { dim: folded, kept: Array.from(keep).concat([OTHER]), folded: totals.length - limit + 1 };
  }

  function seriesChart(p, kind) {
    var s = p.spec, dash = p.dash, src = p.src;
    var x = p.dimFromSpec(p.drillDims ? p.drillDims[p.level] : s.x, 'x');
    var ms = p.measures(s.y, 'y');
    if (ms.length > 1) {
      var f0 = ms[0].format; ms.forEach(function (m) { if (m.format !== f0) fail('y: measures "' + ms[0].name + '" (' + f0 + ') and "' + m.name + '" (' + m.format + ') use different units. One chart has one axis: split them into two panels.'); });
      if (s.series) fail('use either several y measures or "series", not both.');
    }
    var m = ms[0], rows = p.rows();
    var ser = s.series ? p.dim(s.series, 'series') : null;
    if (ser && ser.kind !== 'cat') fail('series: "' + s.series + '" must be a category field (team, model, region).');
    var limitDefault = kind === 'bar' && !ser ? 12 : 6;
    var fx = x.kind === 'cat' ? foldDim(p, rows, x, m, s.limit || (kind === 'bar' ? 12 : 20)) : { dim: x, kept: null };
    var fs = ser ? foldDim(p, rows, ser, m, s.limit && ser ? s.limit : limitDefault) : null;
    var dims = [fx.dim].concat(fs ? [fs.dim] : []);
    var groups = aggregate(rows, dims, ms, dash.measure);
    if (!groups.some(function (g) { return ms.some(function (mm) { return g.v[mm.name] != null; }); })) return emptyState(p);
    var isTime = x.kind === 'time';
    var horizontal = false;
    // x categories
    var cats;
    if (isTime) {
      var tr = p.range(); var lo = bucket(tr.from, x.grain), hi = tr.to;
      if (!isFinite(lo)) { var ks = groups.map(function (g) { return g.keys[0]; }); lo = Math.min.apply(null, ks); hi = Math.max.apply(null, ks) + 1; }
      cats = []; for (var t = lo; t < hi && cats.length < 2000; t = nextBucket(t, x.grain)) cats.push(t);
    } else if (x.kind === 'part') { cats = []; for (var i = 0; i < x.count; i++) cats.push(i); }
    else {
      var totals = {}; groups.forEach(function (g) { totals[g.keys[0]] = (totals[g.keys[0]] || 0) + (g.v[m.name] || 0); });
      cats = Object.keys(totals).map(function (k) { return fx.kept ? fx.kept.find(function (c) { return String(c) === k; }) : k; }).filter(function (c) { return c !== undefined; });
      var sort = s.sort || (x.numeric ? 'label' : 'value');
      if (sort === 'value') cats.sort(function (a, b) { return a === OTHER ? 1 : b === OTHER ? -1 : totals[b] - totals[a]; });
      else if (sort === 'label') cats.sort(function (a, b) { return a === OTHER ? 1 : b === OTHER ? -1 : x.numeric ? a - b : String(a).localeCompare(String(b)); });
      if (kind === 'bar') horizontal = s.orientation === 'horizontal' || (s.orientation !== 'vertical' && (cats.length > 7 || cats.some(function (c) { return String(c).length > 12; })));
    }
    var additive = function (mm) { return !mm.ratio && ['sum', 'count', 'count_distinct'].indexOf(mm.agg) >= 0; };
    var idx = new Map(); cats.forEach(function (c, i) { idx.set(c, i); });
    var series = [];
    function mk(name, colorKey, getter) {
      var data = cats.map(function () { return null; });
      groups.forEach(function (g) { if (getter(g)) { var i = idx.get(g.keys[0]); if (i != null) data[i] = getter(g); } });
      return { name: name, color: colorKey, data: data };
    }
    if (fs) {
      var keys = fs.kept || Array.from(new Set(groups.map(function (g) { return g.keys[1]; })));
      keys.forEach(function (k) {
        var ss = mk(String(k), dash.colorOf(ser.field, k), function (g) { return g.keys[1] === k ? { v: g.v[m.name] } : null; });
        series.push(ss);
      });
    } else ms.forEach(function (mm, i) { series.push(mk(mm.label, dash.theme.series[i % 8], function (g) { return { v: g.v[mm.name] }; })); });
    series.forEach(function (ss) { ss.data = ss.data.map(function (d) { return d ? d.v : (isTime && additive(m) ? 0 : null); }); });
    if (s.cumulative) {
      if (!isTime) fail('"cumulative" needs a time x, e.g. "date:day".');
      if (!additive(m)) fail('"cumulative" adds values up, so y must be a sum or count measure; "' + m.name + '" is ' + (m.ratio ? 'a ratio' : m.agg) + '.');
      var lastT = dash.dataMax;
      series.forEach(function (ss) { var run = 0; ss.data = ss.data.map(function (v, i) { if (cats[i] > lastT) return null; run += v || 0; return run; }); });
    }
    if (s.normalize) {
      cats.forEach(function (c, i) { var tot = 0; series.forEach(function (ss) { tot += ss.data[i] || 0; }); series.forEach(function (ss) { ss.data[i] = tot ? (ss.data[i] || 0) / tot : null; }); });
    }
    var fmtM = s.normalize ? 'pct' : m.format; p.lastFormat = fmtM;
    // Buckets the data only partly covers (a week cut by the range or by the data's first/last day) read low for sums.
    var partial = [];
    if (isTime && additive(m) && !s.cumulative && !s.normalize && ['day', 'week', 'month', 'quarter', 'year'].indexOf(x.grain) >= 0 && cats.length > 2) {
      var rg2 = p.range(), covLo = Math.max(rg2.from, dash.dataMin), covHi = Math.min(rg2.to, dash.dataMax + dash.step);
      [0, cats.length - 1].forEach(function (i) { if (cats[i] < covLo - 1 || nextBucket(cats[i], x.grain) > covHi + 1) partial.push(i); });
    }
    var isPartial = function (i) { return partial.indexOf(i) >= 0; };
    var sel = selectedSet(p, x), selSer = ser ? selectedSet(p, ser) : null;
    var hl = s.highlight != null ? String(s.highlight) : null;
    var th = dash.theme;
    var catLabel = function (c) { return c === OTHER ? OTHER : x.label(c); };
    var ec = series.map(function (ss, si) {
      var color = hl && ss.name !== hl ? th.other : ss.color;
      var dimSeries = selSer && !selSer.has(ss.name) && ss.name !== OTHER;
      var o = { name: ss.name, type: kind === 'bar' ? 'bar' : 'line', color: color, emphasis: { focus: 'series' }, barMaxWidth: 36,
        data: ss.data.map(function (v, i) {
          var dimmed = (sel && !sel.has(String(cats[i]))) || dimSeries;
          var val = isTime ? [cats[i], v] : v;
          return dimmed ? { value: val, itemStyle: { opacity: 0.3 } } : val;
        }) };
      if (kind !== 'bar') { o.showSymbol = false; o.symbolSize = 6; o.lineStyle = { width: 2 }; if (hl && ss.name === hl) o.z = 5; }
      if (kind === 'area') o.areaStyle = { opacity: s.stack || s.normalize ? 0.85 : 0.18 };
      if ((s.stack || s.normalize) && (kind !== 'line')) o.stack = 'all';
      if (kind === 'bar') {
        o.itemStyle = { borderRadius: horizontal ? [0, 4, 4, 0] : [4, 4, 0, 0], borderColor: th.surface, borderWidth: series.length > 1 ? 1 : 0 };
        if (o.stack) o.itemStyle.borderRadius = 0;
        var showLabels = s.labels !== false && series.length === 1 && cats.length <= 15;
        if (showLabels) o.label = { show: true, position: horizontal ? 'right' : 'top', color: th.text2, fontSize: 11, formatter: function (pr) { var v = Array.isArray(pr.value) ? pr.value[1] : pr.value; return format(v, fmtM); } };
      }
      if (kind !== 'bar' && series.length > 1 && series.length <= 4 && !s.stack && !s.normalize) o.endLabel = { show: true, formatter: function (pr) { return pr.seriesName; }, color: th.text2, fontSize: 11 };
      if (si === 0) o.markLine = markLines(p, m);
      if (si === 0 && partial.length) o.markArea = { silent: true, itemStyle: { color: th.grid, opacity: 0.5 }, label: { show: true, position: 'insideTop', color: th.muted, fontSize: 10, formatter: 'partial' },
        data: partial.map(function (i) { return [{ xAxis: cats[i] }, { xAxis: Math.min(nextBucket(cats[i], x.grain), cats[cats.length - 1] + GRAIN_MS[x.grain]) }]; }) };
      return o;
    });
    var dayish = x.grain === 'day' || x.grain === 'week';
    var catAxis = isTime ? { type: 'time', axisLabel: { hideOverlap: true, color: th.muted, formatter: { year: '{yyyy}', month: dayish ? '{MMM} {d}' : '{MMM} {yyyy}', day: '{MMM} {d}', hour: '{HH}:{mm}', minute: '{HH}:{mm}' } } } : { type: 'category', data: cats.map(catLabel), axisLabel: { hideOverlap: true, color: th.muted, width: horizontal ? 110 : 90, overflow: 'truncate' } };
    if (!isTime && horizontal) catAxis.inverse = true;
    var valAxis = measureAxis({ format: fmtM }, s.normalize ? { max: 1 } : null);
    var opt = baseOption(p);
    var endRoom = ec.some(function (o) { return o.endLabel; }) ? 90 : 16;
    Object.assign(opt, {
      grid: { left: 8, right: horizontal ? 48 : endRoom, top: series.length > 1 ? 34 : 14, bottom: 6, containLabel: true },
      legend: series.length > 1 ? { top: 0, left: 0, type: 'scroll' } : { show: false },
      tooltip: { trigger: kind === 'bar' && series.length === 1 ? 'item' : 'axis', axisPointer: { type: kind === 'bar' ? 'shadow' : 'line', lineStyle: { color: th.axis } }, confine: true,
        formatter: function (ps) {
          ps = arr(ps); if (!ps.length) return '';
          var head = isTime ? fmtTime(Array.isArray(ps[0].value) ? ps[0].value[0] : ps[0].axisValue, x.grain) + (isPartial(ps[0].dataIndex) ? ' (partial ' + x.grain + ')' : '') : ps[0].name;
          return '<div style="margin-bottom:4px;color:' + th.text2 + '">' + esc(head) + '</div>' + ps.map(function (pr) {
            var v = Array.isArray(pr.value) ? pr.value[1] : pr.value; if (v && typeof v === 'object') v = Array.isArray(v.value) ? v.value[1] : v.value;
            return tooltipRow(pr.color, pr.seriesName, format(v, fmtM), kind !== 'bar');
          }).join('');
        } },
      xAxis: horizontal ? valAxis : catAxis, yAxis: horizontal ? catAxis : valAxis, series: ec
    });
    if (isTime && kind !== 'bar' && s.brush !== false) {
      opt.brush = { xAxisIndex: 0, brushType: 'lineX', brushMode: 'single', transformable: false, throttleType: 'debounce', throttleDelay: 200,
        brushStyle: { color: 'rgba(85,152,231,0.15)', borderColor: th.accent, borderWidth: 1 }, outOfBrush: { colorAlpha: 1 } };
      opt.toolbox = { show: false, feature: { brush: { type: ['lineX'] } } };
    }
    p.draw(opt);
    if (opt.brush) {
      p.ec.dispatchAction({ type: 'takeGlobalCursor', key: 'brush', brushOption: { brushType: 'lineX', brushMode: 'single' } });
      p.onEc('brushEnd', function (e) {
        var a = e.areas && e.areas[0]; if (!a || !a.coordRange) return;
        var r = a.coordRange; if (Array.isArray(r[0])) r = r[0];
        if (r[1] - r[0] < GRAIN_MS[x.grain] * 0.5) return;
        dash.setTime({ from: bucket(r[0], x.grain), to: nextBucket(bucket(r[1], x.grain), x.grain) });
      });
    }
    p.onEc('click', function (e) {
      if (isTime) return;
      var c = cats[e.dataIndex];
      if (c === undefined || c === OTHER) return;
      linkFilter(p, x, c);
    });
    // Agent-facing summary and table.
    p.table = { columns: [x.spec].concat(fs ? [ser.spec] : []).concat([fmtLabel(m, s.normalize)]), rows: [] };
    series.forEach(function (ss) { ss.data.forEach(function (v, i) { if (v == null) return; p.table.rows.push([catLabel(cats[i])].concat(fs ? [ss.name] : []).concat([format(v, fmtM)])); }); });
    if (!fs && ms.length > 1) p.table = { columns: [x.spec].concat(ms.map(function (mm) { return mm.label; })), rows: cats.map(function (c, i) { return [catLabel(c)].concat(series.map(function (ss) { return format(ss.data[i], fmtM); })); }) };
    p.summaryText = summarizeSeries(series, cats, catLabel, fmtM, isTime, x, m) + (partial.length ? '; partial ' + x.grain + ': ' + partial.map(function (i) { return catLabel(cats[i]); }).join(', ') : '');
    p.pointAt = function (px, py) {
      var v = p.ec.convertFromPixel({ seriesIndex: 0 }, [px, py]); if (!v) return '';
      var i;
      if (isTime) { var tv = v[0]; i = 0; cats.forEach(function (c, j) { if (Math.abs(c - tv) < Math.abs(cats[i] - tv)) i = j; }); }
      else { i = Math.round(horizontal ? v[1] : v[0]); }
      if (i == null || i < 0 || i >= cats.length) return '';
      return catLabel(cats[i]) + ': ' + series.map(function (ss) { return ss.name + ' ' + format(ss.data[i], fmtM); }).join(', ');
    };
  }
  function fmtLabel(m, norm) { return norm ? m.label + ' (share)' : m.label; }
  function summarizeSeries(series, cats, catLabel, f, isTime, x, m) {
    if (!cats.length) return 'no data';
    if (isTime) {
      return series.slice(0, 6).map(function (ss) {
        var vals = ss.data.filter(function (v) { return v != null; }); if (!vals.length) return ss.name + ': no data';
        var last = ss.data[ss.data.length - 1], first = vals[0], peak = -Infinity, pi = 0;
        ss.data.forEach(function (v, i) { if (v != null && v > peak) { peak = v; pi = i; } });
        return (series.length > 1 || ss.name !== m.label ? ss.name + ': ' : '') + 'first ' + format(first, f) + ', last ' + format(last, f) + ', peak ' + format(peak, f) + ' at ' + catLabel(cats[pi]);
      }).join('; ') + ' (' + catLabel(cats[0]) + ' to ' + catLabel(cats[cats.length - 1]) + ', by ' + x.grain + ')';
    }
    var tot = cats.map(function (c, i) { var t = 0; series.forEach(function (ss) { t += ss.data[i] || 0; }); return { c: c, t: t }; });
    var ranked = tot.slice().sort(function (a, b) { return b.t - a.t; });
    return ranked.slice(0, 5).map(function (r) { return catLabel(r.c) + ' ' + format(r.t, f); }).join(', ') + (cats.length > 5 ? ' (' + cats.length + ' of them)' : '');
  }

  TYPES.line = { render: function (p) { seriesChart(p, 'line'); } };
  TYPES.area = { render: function (p) { seriesChart(p, 'area'); } };
  TYPES.bar = { render: function (p) { seriesChart(p, 'bar'); } };

  TYPES.kpi = {
    render: function (p) {
      var s = p.spec, m = p.measure(s.measure, 'measure'), dash = p.dash;
      var cur = p.total(m), prevRows = s.compare !== 'none' ? p.rows({ previous: true }) : null;
      var prev = prevRows && prevRows.length ? aggregate(prevRows, [], [m], dash.measure)[0] : null;
      var pv = prev ? prev.v[m.name] : null;
      var d = formatDelta(cur, pv, m.format);
      var good = d && d.v !== 0 ? (d.v > 0) === (m.good !== 'down') : null;
      var st = statusOf(m, cur);
      var box = h('div', { class: 'awbd-kpi' }, [
        h('div', { class: 'v', text: format(cur, m.format) }),
        h('div', { class: 'd' }, [d ? h('span', { class: good == null ? '' : good ? 'good' : 'bad', text: (d.v > 0 ? '▲ ' : d.v < 0 ? '▼ ' : '') + d.text }) : null,
          d ? 'vs previous ' + dash.rangeLabel(true) : null, s.target != null ? 'target ' + format(s.target, m.format) : null, statusEl(dash, st)])
      ]);
      var t = p.src.timeField && s.sparkline !== false ? p.src.timeField : null;
      if (t) {
        var g = dash.grain(p.src), dim = { field: t, kind: 'time', grain: g, key: function (r) { return r[t] == null ? null : bucket(r[t], g); } };
        var pts = aggregate(p.rows(), [dim], [m], dash.measure).sort(function (a, b) { return a.keys[0] - b.keys[0]; });
        if (pts.length > 1) box.appendChild(spark(pts.map(function (q) { return q.v[m.name]; }), dash.theme));
        p.table = { columns: [t + ':' + g, m.label], rows: pts.map(function (q) { return [fmtTime(q.keys[0], g), format(q.v[m.name], m.format)]; }) };
      } else p.table = { columns: [m.label], rows: [[format(cur, m.format)]] };
      p.body.replaceChildren(box);
      p.summaryText = m.label + ' ' + format(cur, m.format) + (d ? ' (' + d.text + ' vs previous ' + dash.rangeLabel(true) + ')' : '') + (st ? ', status ' + st : '') + (s.target != null ? ', target ' + format(s.target, m.format) : '');
    }
  };
  function spark(vals, th) {
    var ns = 'http://www.w3.org/2000/svg', svg = document.createElementNS(ns, 'svg'), W = 200, H = 34;
    var v = vals.map(function (x) { return x == null ? 0 : x; }), lo = Math.min.apply(null, v), hi = Math.max.apply(null, v), span = hi - lo || 1;
    var pts = v.map(function (x, i) { return (i / (v.length - 1) * W).toFixed(1) + ',' + (H - 3 - (x - lo) / span * (H - 6)).toFixed(1); });
    svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H); svg.setAttribute('preserveAspectRatio', 'none'); svg.setAttribute('aria-hidden', 'true');
    var pl = document.createElementNS(ns, 'polyline');
    pl.setAttribute('points', pts.join(' ')); pl.setAttribute('fill', 'none'); pl.setAttribute('stroke', th.accent); pl.setAttribute('stroke-width', '2');
    pl.setAttribute('vector-effect', 'non-scaling-stroke'); pl.setAttribute('stroke-linejoin', 'round'); svg.appendChild(pl); return svg;
  }

  TYPES.callout = {
    render: function (p) {
      var s = p.spec, m = p.measure(s.measure, 'measure'), dash = p.dash, cur = p.total(m);
      var text = fillTemplate(p, s.text, { value: format(cur, m.format) });
      var d = null;
      if (s.compare === 'previous') { var pr = p.rows({ previous: true }); if (pr.length) d = formatDelta(cur, aggregate(pr, [], [m], dash.measure)[0].v[m.name], m.format); }
      p.body.replaceChildren(h('div', { class: 'awbd-callout' }, [h('div', { class: 'ic' }, [icon(s.icon || 'chart')]),
        h('div', { class: 'v', text: format(cur, m.format) }), h('div', { class: 't', text: text + (d ? ' (' + d.text + ' vs previous ' + dash.rangeLabel(true) + ')' : '') })]));
      p.table = { columns: [m.label], rows: [[format(cur, m.format)]] };
      p.summaryText = format(cur, m.format) + ' ' + text;
    }
  };
  function fillTemplate(p, text, extra) {
    return String(text || '').replace(/\{\{\s*([A-Za-z0-9_().]+)\s*\}\}/g, function (all, name) {
      if (extra && name in extra) return extra[name];
      try { var m = p.measure(name, 'text'); return format(p.total(m), m.format); } catch (e) { return '{{' + name + '?}}'; }
    });
  }

  TYPES.donut = {
    render: function (p) {
      var s = p.spec, dash = p.dash, th = dash.theme, dim = p.dim(s.dim, 'dim'), m = p.measure(s.measure, 'measure'), rows = p.rows();
      if (dim.kind === 'time') fail('dim: a donut needs a category field, not time.');
      var fd = foldDim(p, rows, dim, m, s.limit || 6);
      var g = aggregate(rows, [fd.dim], [m], dash.measure).sort(function (a, b) { return a.keys[0] === OTHER ? 1 : b.keys[0] === OTHER ? -1 : b.v[m.name] - a.v[m.name]; });
      var total = g.reduce(function (t, q) { return t + (q.v[m.name] || 0); }, 0), sel = selectedSet(p, dim);
      var data = g.map(function (q) { var k = q.keys[0]; return { name: dim.label(k), value: q.v[m.name], key: k, itemStyle: { color: k === OTHER ? th.other : dash.colorOf(dim.field, k), opacity: sel && !sel.has(String(k)) ? 0.3 : 1 } }; });
      var opt = baseOption(p);
      Object.assign(opt, {
        tooltip: { trigger: 'item', confine: true, formatter: function (pr) { return tooltipRow(pr.color, pr.name, format(pr.value, m.format) + ' · ' + format(total ? pr.value / total : null, 'pct')); } },
        legend: { show: false },
        title: { text: format(total, m.format), subtext: m.label, left: 'center', top: 'center', textStyle: { color: th.text, fontSize: 20, fontWeight: 650 }, subtextStyle: { color: th.muted }, itemGap: 2 },
        series: [{ type: 'pie', radius: ['52%', '74%'], padAngle: 1, itemStyle: { borderColor: th.surface, borderWidth: 2, borderRadius: 3 },
          label: { color: th.text2, formatter: function (pr) { return pr.name + '\n' + format(total ? pr.value / total : null, 'pct'); }, fontSize: 11 }, labelLine: { lineStyle: { color: th.axis } }, data: data }]
      });
      p.draw(opt);
      p.onEc('click', function (e) { if (e.data && e.data.key !== OTHER) linkFilter(p, dim, e.data.key); });
      p.table = { columns: [dim.spec, m.label, 'share'], rows: data.map(function (d) { return [d.name, format(d.value, m.format), format(total ? d.value / total : null, 'pct')]; }) };
      p.summaryText = 'total ' + format(total, m.format) + '; ' + data.slice(0, 6).map(function (d) { return d.name + ' ' + format(d.value, m.format) + ' (' + format(total ? d.value / total : null, 'pct') + ')'; }).join(', ');
    }
  };

  TYPES.scatter = {
    render: function (p) {
      var s = p.spec, dash = p.dash, th = dash.theme, by = p.dim(s.by, 'by');
      var mx = p.measure(s.x, 'x'), my = p.measure(s.y, 'y'), msz = s.size ? p.measure(s.size, 'size') : null, col = s.color ? p.dim(s.color, 'color') : null;
      var ms = [mx, my].concat(msz ? [msz] : []);
      var dims = [by].concat(col ? [col] : []);
      var g = aggregate(p.rows(), dims, ms, dash.measure);
      if (s.limit) g = g.sort(function (a, b) { return (b.v[(msz || my).name] || 0) - (a.v[(msz || my).name] || 0); }).slice(0, s.limit);
      var smax = msz ? Math.max.apply(null, g.map(function (q) { return q.v[msz.name] || 0; })) || 1 : 1;
      var colors = col ? Array.from(new Set(g.map(function (q) { return q.keys[1]; }))) : [null];
      if (colors.length > 3) fail('color: "' + s.color + '" has ' + colors.length + ' values; scatter colors stay readable up to 3. Filter it or drop "color".');
      var sel = selectedSet(p, by), nLabels = s.labels == null ? 5 : s.labels;
      var ranked = g.slice().sort(function (a, b) { return (b.v[(msz || my).name] || 0) - (a.v[(msz || my).name] || 0); }).slice(0, nLabels).map(function (q) { return q.keys[0]; });
      var series = colors.map(function (c, ci) {
        return { type: 'scatter', name: c == null ? my.label : String(c), color: c == null ? th.series[0] : dash.colorOf(col.field, c),
          itemStyle: { borderColor: th.surface, borderWidth: 2, opacity: 0.9 }, emphasis: { scale: 1.4 },
          data: g.filter(function (q) { return c == null || q.keys[1] === c; }).map(function (q) {
            var k = q.keys[0];
            return { name: by.label(k), key: k, value: [q.v[mx.name], q.v[my.name], msz ? q.v[msz.name] : null],
              symbolSize: msz ? 8 + Math.sqrt((q.v[msz.name] || 0) / smax) * 30 : 10,
              itemStyle: sel && !sel.has(String(k)) ? { opacity: 0.25 } : undefined,
              label: { show: ranked.indexOf(k) >= 0, formatter: by.label(k), position: 'right', color: th.text2, fontSize: 11 } };
          }) };
      });
      var opt = baseOption(p);
      Object.assign(opt, {
        grid: { left: 8, right: 24, top: colors.length > 1 ? 34 : 24, bottom: 24, containLabel: true },
        legend: colors.length > 1 ? { top: 0, left: 0 } : { show: false },
        tooltip: { trigger: 'item', confine: true, formatter: function (pr) { var v = pr.value; return '<div style="margin-bottom:4px"><b>' + esc(pr.name) + '</b></div>' + tooltipRow(pr.color, mx.label, format(v[0], mx.format)) + tooltipRow(pr.color, my.label, format(v[1], my.format)) + (msz ? tooltipRow(pr.color, msz.label, format(v[2], msz.format)) : ''); } },
        xAxis: measureAxis(mx, { name: mx.label, nameLocation: 'middle', nameGap: 26, scale: true, splitLine: { show: true, lineStyle: { color: th.grid } } }),
        yAxis: measureAxis(my, { name: my.label, nameLocation: 'end', scale: true }),
        series: series
      });
      p.draw(opt);
      p.onEc('click', function (e) { if (e.data) linkFilter(p, by, e.data.key); });
      p.table = { columns: [by.spec].concat(col ? [col.spec] : []).concat(ms.map(function (m) { return m.label; })), rows: g.map(function (q) { return [by.label(q.keys[0])].concat(col ? [q.keys[1]] : []).concat(ms.map(function (m) { return format(q.v[m.name], m.format); })); }) };
      p.summaryText = g.length + ' ' + by.field + ' points; ' + ranked.map(function (k) { var q = g.find(function (z) { return z.keys[0] === k; }); return by.label(k) + ' (' + mx.label + ' ' + format(q.v[mx.name], mx.format) + ', ' + my.label + ' ' + format(q.v[my.name], my.format) + ')'; }).join('; ');
    }
  };

  TYPES.heatmap = {
    render: function (p) {
      var s = p.spec, dash = p.dash, th = dash.theme, x = p.dim(s.x, 'x'), y = p.dim(s.y, 'y'), m = p.measure(s.measure, 'measure'), rows = p.rows();
      var fx = foldDim(p, rows, x, m, s.limit || 30), fy = foldDim(p, rows, y, m, s.limit || 20);
      var g = aggregate(rows, [fx.dim, fy.dim], [m], dash.measure);
      function cats(d, fd, i) {
        if (d.kind === 'part') { var a = []; for (var k = 0; k < d.count; k++) a.push(k); return a; }
        var set = Array.from(new Set(g.map(function (q) { return q.keys[i]; })));
        if (d.kind === 'time' || d.numeric) set.sort(function (a, b) { return a - b; });
        else if (fd.kept) set = fd.kept.filter(function (k) { return set.indexOf(k) >= 0; });
        return set;
      }
      var xs = cats(x, fx, 0), ys = cats(y, fy, 1);
      var data = g.map(function (q) { return [xs.indexOf(q.keys[0]), ys.indexOf(q.keys[1]), q.v[m.name]]; });
      var vals = data.map(function (d) { return d[2]; }).filter(function (v) { return v != null; });
      var lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals);
      var opt = baseOption(p);
      Object.assign(opt, {
        grid: { left: 8, right: 16, top: 8, bottom: 40, containLabel: true },
        tooltip: { trigger: 'item', confine: true, formatter: function (pr) { return '<div style="margin-bottom:4px">' + esc(x.label(xs[pr.value[0]])) + ' · ' + esc(y.label(ys[pr.value[1]])) + '</div>' + tooltipRow(th.accent, m.label, format(pr.value[2], m.format)); } },
        xAxis: { type: 'category', data: xs.map(function (v) { return x.label(v); }), splitArea: { show: false }, axisLabel: { hideOverlap: true, color: th.muted } },
        yAxis: { type: 'category', inverse: true, data: ys.map(function (v) { return y.label(v); }), axisLabel: { color: th.muted, width: 100, overflow: 'truncate' } },
        visualMap: { min: isFinite(lo) ? lo : 0, max: isFinite(hi) ? hi : 1, calculable: false, orient: 'horizontal', left: 'center', bottom: 0, itemHeight: 120, itemWidth: 10,
          inRange: { color: th.dark ? [th.seq[0], th.seq[2]] : [th.seq[0], th.seq[2]] }, textStyle: { color: th.muted }, formatter: function (v) { return format(v, m.format); } },
        series: [{ type: 'heatmap', data: data, itemStyle: { borderColor: th.surface, borderWidth: 2, borderRadius: 3 }, label: { show: !!s.labels, formatter: function (pr) { return format(pr.value[2], m.format); }, fontSize: 10 }, emphasis: { itemStyle: { borderColor: th.text, borderWidth: 1 } } }]
      });
      p.draw(opt);
      p.onEc('click', function (e) { var yv = ys[e.value[1]]; if (y.kind === 'cat' && yv !== OTHER) linkFilter(p, y, yv); else if (x.kind === 'cat') linkFilter(p, x, xs[e.value[0]]); });
      p.table = { columns: [x.spec, y.spec, m.label], rows: data.map(function (d) { return [x.label(xs[d[0]]), y.label(ys[d[1]]), format(d[2], m.format)]; }) };
      var top = data.slice().sort(function (a, b) { return (b[2] || 0) - (a[2] || 0); }).slice(0, 3);
      p.summaryText = xs.length + '×' + ys.length + ' cells; highest ' + top.map(function (d) { return x.label(xs[d[0]]) + '/' + y.label(ys[d[1]]) + ' ' + format(d[2], m.format); }).join(', ') + '; lowest ' + format(lo, m.format);
    }
  };

  TYPES.treemap = {
    render: function (p) {
      var s = p.spec, dash = p.dash, th = dash.theme, dims = arr(s.dims).map(function (d, i) { return p.dim(d, 'dims[' + i + ']'); }), m = p.measure(s.measure, 'measure');
      if (dims.length < 1 || dims.length > 2) fail('dims: a treemap takes one or two category fields.');
      var rows = p.rows(), f0 = foldDim(p, rows, dims[0], m, s.limit || 10);
      var g = aggregate(rows, [f0.dim].concat(dims.slice(1)), [m], dash.measure), tree = new Map();
      g.forEach(function (q) {
        var k = q.keys[0], node = tree.get(k);
        if (!node) { node = { name: dims[0].label(k), key: k, value: 0, children: dims.length > 1 ? [] : undefined, itemStyle: { color: k === OTHER ? th.other : dash.colorOf(dims[0].field, k) } }; tree.set(k, node); }
        node.value += q.v[m.name] || 0;
        if (dims.length > 1) node.children.push({ name: dims[1].label(q.keys[1]), value: q.v[m.name] || 0, key: k });
      });
      var data = Array.from(tree.values()).sort(function (a, b) { return b.value - a.value; }), total = data.reduce(function (t, n) { return t + n.value; }, 0);
      var opt = baseOption(p);
      Object.assign(opt, {
        tooltip: { confine: true, formatter: function (pr) { return '<div style="margin-bottom:4px">' + esc((pr.treePathInfo || []).slice(1).map(function (x) { return x.name; }).join(' › ')) + '</div>' + tooltipRow(pr.color, m.label, format(pr.value, m.format) + ' · ' + format(total ? pr.value / total : null, 'pct')); } },
        series: [{ type: 'treemap', roam: false, nodeClick: false, breadcrumb: { show: false }, width: '100%', height: '100%', top: 0, left: 0,
          label: { show: true, formatter: function (pr) { return pr.name + '\n' + format(pr.value, m.format); }, color: '#fff', fontSize: 11 },
          upperLabel: { show: dims.length > 1, height: 18, color: '#fff', fontSize: 11 },
          itemStyle: { borderColor: th.surface, borderWidth: 2, gapWidth: 2 }, levels: [{ itemStyle: { borderWidth: 0, gapWidth: 3 } }, { colorSaturation: [0.35, 0.6], itemStyle: { borderColorSaturation: 0.6, gapWidth: 1 } }],
          data: data }]
      });
      p.draw(opt);
      p.onEc('click', function (e) { var k = e.data && e.data.key; if (k != null && k !== OTHER) linkFilter(p, dims[0], k); });
      p.table = { columns: dims.map(function (d) { return d.spec; }).concat([m.label]), rows: g.map(function (q) { return q.keys.map(function (k, i) { return dims[i] ? dims[i].label(k) : k; }).concat([format(q.v[m.name], m.format)]); }) };
      p.summaryText = 'total ' + format(total, m.format) + '; ' + data.slice(0, 5).map(function (n) { return n.name + ' ' + format(n.value, m.format) + ' (' + format(total ? n.value / total : null, 'pct') + ')'; }).join(', ');
    }
  };

  TYPES.funnel = {
    render: function (p) {
      var s = p.spec, dash = p.dash, th = dash.theme;
      if (!Array.isArray(s.steps) || s.steps.length < 2) fail('steps: list at least two steps, e.g. [{"label":"Visit","measure":"visitors"}, ...].');
      var ms = s.steps.map(function (st, i) { return p.measure(st.measure, 'steps[' + i + '].measure'); });
      var frows = p.rows();
      if (s.cohort) {
        // Cohort funnel: the users whose first step happened in the window, followed through every later step at any time.
        var uf = s.cohort, src = p.src, tf = src.timeField, rg = p.range(), m0 = ms[0];
        if (!src.fields[uf]) fail('cohort: no field "' + uf + '".' + didYouMean(uf, Object.keys(src.fields)));
        if (m0.ratio) fail('steps[0].measure: a cohort funnel needs a count measure for its first step, not a ratio.');
        var all = p.rows({ noTime: true }), first = new Map();
        all.forEach(function (r) { if (m0.where && !wherePass(m0.where, r)) return; var u = r[uf], t = tf ? r[tf] : 0; if (u == null) return; if (!first.has(u) || t < first.get(u)) first.set(u, t); });
        var users = new Set(); first.forEach(function (t, u) { if (!tf || (t >= rg.from && t < rg.to)) users.add(u); });
        frows = all.filter(function (r) { return users.has(r[uf]); });
        p.cohortNote = format(users.size, 'int') + ' users whose first "' + (s.steps[0].label || m0.label) + '" is in the window; later steps counted at any time';
      }
      var tot = aggregate(frows, [], ms, dash.measure)[0], vals = ms.map(function (m) { return tot ? tot.v[m.name] || 0 : 0; });
      if (!vals[0] && !vals.some(Boolean)) return emptyState(p);
      var labels = s.steps.map(function (st, i) { return st.label || ms[i].label; });
      var ramp = th.dark ? ['#5598e7', '#3987e5', '#2a78d6', '#256abf', '#1c5cab', '#184f95'] : ['#2a78d6', '#3987e5', '#5598e7', '#6da7ec', '#86b6ef', '#86b6ef'];
      var opt = baseOption(p);
      Object.assign(opt, {
        grid: { left: 8, right: 150, top: 8, bottom: 8, containLabel: true },
        tooltip: { trigger: 'item', confine: true, formatter: function (pr) { var i = pr.dataIndex; return '<div style="margin-bottom:4px">' + esc(labels[i]) + '</div>' + tooltipRow(pr.color, 'count', format(vals[i], ms[i].format)) + tooltipRow(pr.color, 'of first step', format(vals[0] ? vals[i] / vals[0] : null, 'pct')) + (i ? tooltipRow(pr.color, 'of previous step', format(vals[i - 1] ? vals[i] / vals[i - 1] : null, 'pct')) : ''); } },
        xAxis: { type: 'value', show: false, max: vals[0] || 1 }, yAxis: { type: 'category', inverse: true, data: labels, axisLine: { show: false }, axisLabel: { color: th.text2, fontSize: 12 } },
        series: [{ type: 'bar', barWidth: '62%', data: vals.map(function (v, i) { return { value: v, itemStyle: { color: ramp[Math.min(i, ramp.length - 1)], borderRadius: [0, 4, 4, 0] } }; }),
          showBackground: true, backgroundStyle: { color: th.grid, borderRadius: [0, 4, 4, 0] },
          label: { show: true, position: 'right', color: th.text2, fontSize: 11, formatter: function (pr) { var i = pr.dataIndex; return format(vals[i], ms[i].format) + '  ' + format(vals[0] ? vals[i] / vals[0] : null, 'pct') + (i ? '  (' + format(vals[i - 1] ? vals[i] / vals[i - 1] : null, 'pct') + ' step)' : ''); } } }]
      });
      p.draw(opt);
      p.table = { columns: ['step', 'count', 'of first', 'of previous'], rows: vals.map(function (v, i) { return [labels[i], format(v, ms[i].format), format(vals[0] ? v / vals[0] : null, 'pct'), i ? format(vals[i - 1] ? v / vals[i - 1] : null, 'pct') : '']; }) };
      p.summaryText = labels.map(function (l, i) { return l + ' ' + format(vals[i], ms[i].format) + (i ? ' (' + format(vals[i - 1] ? vals[i] / vals[i - 1] : null, 'pct') + ' of previous)' : ''); }).join(' → ') + '; overall ' + format(vals[0] ? vals[vals.length - 1] / vals[0] : null, 'pct') + (s.cohort ? ' (' + p.cohortNote + ')' : '');
    }
  };

  // Budget pace: cumulative use in the window against a budget, with a straight-line projection to the window's end.
  // With "period": "mtd" (or qtd/ytd) the window is the current month and the projection runs to the month's end.
  TYPES.budget = {
    render: function (p) {
      var s = p.spec, dash = p.dash, th = dash.theme, src = p.src;
      var m = p.measure(s.measure, 'measure');
      if (m.ratio || ['sum', 'count'].indexOf(m.agg) < 0) fail('measure: a budget adds use up, so it needs a sum or count measure; "' + m.name + '" is ' + (m.ratio ? 'a ratio' : m.agg) + '.');
      if (!src.timeField) fail('a budget panel needs a source with a time field.');
      var rows = p.rows(), budget;
      if (typeof s.budget === 'number') budget = s.budget;
      else { var bm = p.measure(s.budget, 'budget'); budget = aggregate(rows, [], [bm], dash.measure).map(function (g) { return g.v[bm.name]; })[0]; }
      if (!(budget > 0)) fail('budget: must be a positive number or a measure with a positive total in the window (got ' + budget + ').');
      var rg = p.range(), sel = p.periodSel() || dash.timeSel;
      var end = sel === 'mtd' ? nextBucket(rg.from, 'month') : sel === 'qtd' ? nextBucket(rg.from, 'quarter') : sel === 'ytd' ? nextBucket(rg.from, 'year') : rg.to;
      var lastT = Math.min(dash.dataMax + dash.step, rg.to), span = end - rg.from, elapsed = Math.max(1, lastT - rg.from);
      var grain = span <= 3 * DAY ? 'hour' : span <= 120 * DAY ? 'day' : 'week';
      var dim = { field: src.timeField, kind: 'time', grain: grain, key: function (r) { return r[src.timeField] == null ? null : bucket(r[src.timeField], grain); } };
      var byT = new Map(); aggregate(rows, [dim], [m], dash.measure).forEach(function (g) { byT.set(g.keys[0], g.v[m.name] || 0); });
      var cum = [], run = 0;
      for (var t = bucket(rg.from, grain); t < lastT; t = nextBucket(t, grain)) { run += byT.get(t) || 0; cum.push([t, run]); }
      var used = run, projected = end > lastT ? used / elapsed * span : used;
      var pctUsed = used / budget, pctProj = projected / budget, expected = Math.min(1, elapsed / span);
      var st = pctProj >= 1 ? 'critical' : pctProj >= 0.9 ? 'warning' : 'good';
      var verdict = st === 'critical' ? (end > lastT ? 'over budget at this pace' : 'over budget') : st === 'warning' ? 'close to budget' : 'within budget';
      var wl = sel === 'mtd' ? 'Month to date' : sel === 'qtd' ? 'Quarter to date' : sel === 'ytd' ? 'Year to date' : 'In the window';
      var head = wl + ' ' + format(used, m.format) + ' of ' + format(budget, m.format) + ' (' + format(pctUsed, 'pct') + ')' + (end > lastT ? ' · projected ' + format(projected, m.format) + ' (' + format(pctProj, 'pct') + ')' : '') + ' · ' + verdict;
      var opt = baseOption(p);
      var lineEnd = cum.length ? cum[cum.length - 1] : [rg.from, 0];
      Object.assign(opt, {
        title: { text: head, left: 0, top: 0, textStyle: { fontSize: 13, fontWeight: 600, color: th.text } },
        grid: { left: 8, right: 24, top: 36, bottom: 28, containLabel: true },
        legend: { show: true, bottom: 0, left: 0, data: ['Used', 'Projected', 'Even pace'] },
        tooltip: { trigger: 'axis', confine: true, formatter: function (ps) { ps = arr(ps); return '<div style="margin-bottom:4px;color:' + th.text2 + '">' + esc(fmtTime(ps[0].value[0], grain)) + '</div>' + ps.map(function (pr) { return tooltipRow(pr.color, pr.seriesName, format(pr.value[1], m.format), true); }).join(''); } },
        xAxis: { type: 'time', min: rg.from, max: end, axisLabel: { hideOverlap: true, color: th.muted, formatter: { year: '{yyyy}', month: '{MMM} {d}', day: '{MMM} {d}', hour: '{HH}:{mm}' } } },
        yAxis: measureAxis(m, { max: niceCeil(Math.max(used, budget, projected) * 1.04) }),
        series: [
          { name: 'Used', type: 'line', showSymbol: false, data: cum, color: th.accent, lineStyle: { width: 2 }, areaStyle: { opacity: 0.12 },
            markLine: { symbol: 'none', silent: true, lineStyle: { color: th.status.critical, type: 'solid', width: 1 }, data: [{ yAxis: budget, label: { formatter: 'Budget ' + format(budget, m.format), position: 'insideStartTop', color: th.text2 } }] } },
          { name: 'Projected', type: 'line', showSymbol: false, data: end > lastT ? [lineEnd, [end, projected]] : [], color: th.status[st], lineStyle: { width: 2, type: 'dashed' } },
          { name: 'Even pace', type: 'line', showSymbol: false, data: [[rg.from, 0], [end, budget]], color: th.muted, lineStyle: { width: 1, type: 'dotted' } }
        ]
      });
      p.lastFormat = m.format;
      p.draw(opt);
      p.table = { columns: [src.timeField + ':' + grain, 'cumulative ' + m.label], rows: cum.map(function (c) { return [fmtTime(c[0], grain), format(c[1], m.format)]; }) };
      p.table.rows.push(['budget', format(budget, m.format)]);
      if (end > lastT) p.table.rows.push(['projected at ' + fmtTime(end - 1, 'day'), format(projected, m.format)]);
      p.summaryText = head + '; ' + format(expected, 'pct') + ' of the period has passed (status ' + st + ')';
      p.pointAt = function (px) { var v = p.ec.convertFromPixel({ seriesIndex: 0 }, [px, 0]); if (!v) return ''; var best = null; cum.forEach(function (c) { if (!best || Math.abs(c[0] - v[0]) < Math.abs(best[0] - v[0])) best = c; }); return best ? fmtTime(best[0], grain) + ': used ' + format(best[1], m.format) + ' of ' + format(budget, m.format) : ''; };
    }
  };

  // Flow (Sankey): how a measure splits from one category to the next along "path", e.g. ["channel", "furthest_step"].
  TYPES.flow = {
    render: function (p) {
      var s = p.spec, dash = p.dash, th = dash.theme;
      var m = p.measure(s.measure, 'measure');
      if (m.ratio || ['sum', 'count', 'count_distinct'].indexOf(m.agg) < 0) fail('measure: a flow splits a total, so it needs a sum or count measure.');
      if (!Array.isArray(s.path) || s.path.length < 2) fail('path: list two or more category fields, left to right: ["channel", "furthest_step"].');
      var rows = p.rows(), limit = Math.min(s.limit || 8, 12);
      var dims = s.path.map(function (d, i) { var dm = p.dim(d, 'path[' + i + ']'); if (dm.kind !== 'cat') fail('path[' + i + ']: "' + d + '" must be a category field.'); return foldDim(p, rows, dm, m, limit).dim; });
      var SEP = '⁣';
      var nodeName = function (lvl, v) { return String(v) + SEP + lvl; }, plain = function (n) { return String(n).split(SEP)[0]; };
      var nodes = new Map(), links = new Map(), table = [];
      aggregate(rows, dims, [m], dash.measure).forEach(function (g) {
        var v = g.v[m.name] || 0; if (!v) return;
        table.push(g.keys.map(String).concat([format(v, m.format)]));
        g.keys.forEach(function (k, i) { var n = nodeName(i, k); nodes.set(n, { lvl: i, key: k, v: (nodes.get(n) || { v: 0 }).v + v }); });
        for (var i = 0; i < g.keys.length - 1; i++) { var lk = nodeName(i, g.keys[i]) + '\u0001' + nodeName(i + 1, g.keys[i + 1]); links.set(lk, (links.get(lk) || 0) + v); }
      });
      var sel = dims.map(function (d) { return selectedSet(p, d); });
      if (!links.size) return emptyState(p);
      var nodeList = Array.from(nodes.entries()).map(function (e) {
        var col = dash.colorOf(dims[e[1].lvl].field, e[1].key), ss = sel[e[1].lvl], dimmed = ss && !ss.has(String(e[1].key));
        return { name: e[0], depth: e[1].lvl, itemStyle: { color: col, borderColor: th.surface, opacity: dimmed ? 0.3 : 1 } };
      });
      var linkList = Array.from(links.entries()).map(function (e) { var ab = e[0].split('\u0001'); return { source: ab[0], target: ab[1], value: e[1] }; });
      var opt = baseOption(p);
      Object.assign(opt, {
        tooltip: { trigger: 'item', confine: true, formatter: function (pr) {
          if (pr.dataType === 'edge') return esc(plain(pr.data.source)) + ' → ' + esc(plain(pr.data.target)) + ': <b>' + esc(format(pr.data.value, m.format)) + '</b>';
          return esc(plain(pr.name)) + ': <b>' + esc(format(pr.value, m.format)) + '</b>'; } },
        series: [{ type: 'sankey', left: 8, right: 150, top: 8, bottom: 8, nodeWidth: 12, nodeGap: 10, draggable: false, layoutIterations: 32,
          emphasis: { focus: 'adjacency' }, data: nodeList, links: linkList,
          label: { color: th.text, fontSize: 12, formatter: function (pr) { return plain(pr.name) + '  ' + format(pr.value, m.format); } },
          lineStyle: { color: 'source', opacity: 0.35, curveness: 0.5 } }]
      });
      p.lastFormat = m.format;
      p.draw(opt);
      p.onEc('click', function (e) { if (e.dataType !== 'node') return; var n = nodes.get(e.name); if (n && n.key !== OTHER) linkFilter(p, dims[n.lvl], n.key); });
      p.table = { columns: s.path.concat([m.label]), rows: table };
      var top = linkList.slice().sort(function (a, b) { return b.value - a.value; }).slice(0, 6);
      p.summaryText = 'largest flows: ' + top.map(function (l) { return plain(l.source) + ' → ' + plain(l.target) + ' ' + format(l.value, m.format); }).join(', ');
      p.pointAt = null;
    }
  };

  TYPES.retention = {
    render: function (p) {
      var s = p.spec, dash = p.dash, th = dash.theme, src = p.src;
      [s.user, s.time].forEach(function (f, i) { if (!src.fields[f]) fail((i ? 'time' : 'user') + ': no field "' + f + '".' + didYouMean(f, Object.keys(src.fields))); });
      if (src.fields[s.time].type !== 'time') fail('time: "' + s.time + '" is not a time field.');
      var grain = s.grain || 'week', step = GRAIN_MS[grain], periods = s.periods || 8;
      // Retention ignores the dashboard time range for return events (cohorts start inside the range).
      var range = p.range(), all = p.rows({ noTime: true });
      var start = new Map();
      all.forEach(function (r) { if (wherePass(s.start, r)) { var t = r[s.time], u = r[s.user]; if (t != null && (!start.has(u) || t < start.get(u))) start.set(u, t); } });
      var cohorts = new Map();
      start.forEach(function (t, u) { if (t < range.from || t >= range.to) return; var c = bucket(t, grain); if (!cohorts.has(c)) cohorts.set(c, { size: 0, back: [] }); cohorts.get(c).size++; });
      var seen = new Set();
      all.forEach(function (r) {
        if (!wherePass(s['return'], r)) return;
        var u = r[s.user], t0 = start.get(u); if (t0 == null || t0 < range.from || t0 >= range.to) return;
        var k = Math.floor((r[s.time] - t0) / step); if (k < 1 || k > periods) return;
        var key = u + '|' + k; if (seen.has(key)) return; seen.add(key);
        var c = cohorts.get(bucket(t0, grain)); c.back[k] = (c.back[k] || 0) + 1;
      });
      var keys = Array.from(cohorts.keys()).sort(function (a, b) { return a - b; }).slice(-(s.cohorts || 12));
      if (!keys.length) return emptyState(p);
      // A period is observable only up to the last return event in the data (not the last event of any kind).
      var maxT = -Infinity; all.forEach(function (r) { if (wherePass(s['return'], r) && r[s.time] > maxT) maxT = r[s.time]; });
      var table = h('table', { class: 'awbd-table' }), thead = h('tr', null, [h('th', { text: 'Cohort (' + grain + ')' }), h('th', { class: 'num', text: 'Users' })]);
      for (var k = 1; k <= periods; k++) thead.appendChild(h('th', { class: 'num', text: grain[0].toUpperCase() + k }));
      table.appendChild(h('thead', null, [thead]));
      var tb = h('tbody'), rowsOut = [], avg = [];
      keys.forEach(function (c) {
        var co = cohorts.get(c), tr = h('tr', null, [h('td', { text: fmtTime(c, grain === 'week' ? 'week' : grain) }), h('td', { class: 'num', text: format(co.size, 'int') })]), out = [fmtTime(c, grain), co.size];
        for (var k = 1; k <= periods; k++) {
          var mature = c + (k + 1) * step <= maxT + DAY, r = mature && co.size ? (co.back[k] || 0) / co.size : null;
          if (r != null) { avg[k] = avg[k] || []; avg[k].push(r); }
          var td = h('td', { class: 'heat', text: r == null ? '' : format(r, 'pct') });
          if (r != null) { td.style.background = mix(th.seq[0], th.seq[2], Math.min(1, r / 0.8)); td.style.color = r > 0.4 ? (th.dark ? '#0b0b0b' : '#fff') : th.text; }
          tr.appendChild(td); out.push(r == null ? '' : format(r, 'pct'));
        }
        tb.appendChild(tr); rowsOut.push(out);
      });
      table.appendChild(tb);
      p.body.replaceChildren(h('div', { class: 'awbd-tablewrap' }, [table]));
      var cols = ['cohort', 'users']; for (k = 1; k <= periods; k++) cols.push(grain + ' ' + k);
      p.table = { columns: cols, rows: rowsOut };
      var mean = function (a) { return a && a.length ? a.reduce(function (x, y) { return x + y; }, 0) / a.length : null; };
      p.summaryText = keys.length + ' cohorts by ' + grain + '; average retention ' + [1, 2, 4, periods].filter(function (k, i, a) { return k <= periods && a.indexOf(k) === i; }).map(function (k) { return grain + ' ' + k + ' ' + format(mean(avg[k]), 'pct'); }).join(', ');
    }
  };
  function mix(a, b, t) {
    function rgb(hx) { hx = hx.replace('#', ''); return [0, 2, 4].map(function (i) { return parseInt(hx.slice(i, i + 2), 16); }); }
    var x = rgb(a), y = rgb(b); return 'rgb(' + x.map(function (v, i) { return Math.round(v + (y[i] - v) * t); }).join(',') + ')';
  }

  TYPES.table = {
    render: function (p) {
      var s = p.spec, dash = p.dash, th = dash.theme;
      var dimSpecs = p.drillDims ? [p.drillDims[p.level]] : arr(s.dims);
      var dims = dimSpecs.map(function (d, i) { return p.dim(d, 'dims[' + i + ']'); }), ms = p.measures(s.measures, 'measures');
      var g = aggregate(p.rows(), dims, ms, dash.measure);
      var sortKey = p.sortKey || s.sort || ms[0].name, order = p.sortOrder || s.order || 'desc';
      var si = dims.findIndex(function (d) { return d.spec === sortKey || d.field === sortKey; });
      var sm = ms.find(function (m) { return m.name === sortKey || m.label === sortKey; });
      if (si < 0 && !sm) fail('sort: "' + sortKey + '" is not one of this table\'s dims or measures.');
      g.sort(function (a, b) {
        var x = si >= 0 ? a.keys[si] : a.v[sm.name], y = si >= 0 ? b.keys[si] : b.v[sm.name];
        if (x == null) return 1; if (y == null) return -1;
        var c = typeof x === 'number' && typeof y === 'number' ? x - y : String(x).localeCompare(String(y));
        return order === 'asc' ? c : -c;
      });
      var total = g.length; g = g.slice(0, s.limit || 50);
      var maxes = {}; ms.forEach(function (m) { maxes[m.name] = Math.max.apply(null, g.map(function (q) { return Math.abs(q.v[m.name] || 0); })) || 1; });
      var sel = dims[0] ? selectedSet(p, dims[0]) : null;
      var head = h('tr');
      function th_(label, key, isNum) {
        var arrow = sortKey === key ? (order === 'asc' ? ' ▲' : ' ▼') : '';
        return h('th', { class: isNum ? 'num' : null, scope: 'col', tabindex: '0', 'aria-sort': sortKey === key ? (order === 'asc' ? 'ascending' : 'descending') : null, text: label + arrow,
          onclick: function () { p.sortOrder = p.sortKey === key && order === 'desc' ? 'asc' : 'desc'; p.sortKey = key; p.update(); },
          onkeydown: function (e) { if (e.key === 'Enter') e.target.click(); } });
      }
      dims.forEach(function (d) { head.appendChild(th_(d.spec, d.spec, d.numeric)); });
      ms.forEach(function (m) { head.appendChild(th_(m.label, m.name, true)); });
      var tb = h('tbody');
      g.forEach(function (q) {
        var k0 = q.keys[0];
        var tr = h('tr', { tabindex: dims.length ? '0' : null, class: sel ? (sel.has(String(k0)) ? 'sel' : 'dim') : null,
          'data-awb': (p.spec.title || p.id) + ' row: ' + q.keys.map(function (k, i) { return dims[i].label(k); }).join(' / ') + ': ' + ms.map(function (m) { return m.label + ' ' + format(q.v[m.name], m.format); }).join(', '),
          onclick: function () { if (dims.length) linkFilter(p, dims[0], k0); }, onkeydown: function (e) { if (e.key === 'Enter') e.currentTarget.click(); } });
        dims.forEach(function (d, i) { tr.appendChild(h('td', { class: d.numeric ? 'num' : null, text: d.label(q.keys[i]) })); });
        ms.forEach(function (m) {
          var v = q.v[m.name], st = statusOf(m, v), td = h('td', { class: 'num' }, [format(v, m.format)]);
          if (s.bars !== false && !st && v != null && v >= 0) td.appendChild(h('span', { class: 'db', style: 'width:' + Math.round(40 * Math.abs(v) / maxes[m.name]) + 'px', 'aria-hidden': 'true' }));
          if (st) { td.appendChild(document.createTextNode(' ')); td.appendChild(statusEl(dash, st)); }
          tr.appendChild(td);
        });
        tb.appendChild(tr);
      });
      p.body.replaceChildren(h('div', { class: 'awbd-tablewrap' }, [h('table', { class: 'awbd-table' }, [h('thead', null, [head]), tb])]));
      p.table = { columns: dims.map(function (d) { return d.spec; }).concat(ms.map(function (m) { return m.label; })), rows: g.map(function (q) { return q.keys.map(function (k, i) { return dims[i].label(k); }).concat(ms.map(function (m) { return format(q.v[m.name], m.format); })); }) };
      p.summaryText = total + ' rows' + (total > g.length ? ' (top ' + g.length + ' shown)' : '') + ', sorted by ' + (sm ? sm.label : sortKey) + ' ' + order + '; first: ' + p.table.rows.slice(0, 3).map(function (r) { return r.join(' / '); }).join('; ');
    },
    noTableView: true
  };

  TYPES.meter = {
    render: function (p) {
      var s = p.spec, dash = p.dash, th = dash.theme, m = p.measure(s.measure, 'measure');
      var by = s.by ? p.dim(s.by, 'by') : null, rows = p.rows();
      var items = by ? aggregate(rows, [by], [m], dash.measure).map(function (q) { return { k: q.keys[0], name: by.label(q.keys[0]), v: q.v[m.name] }; }) : [{ k: null, name: m.label, v: p.total(m) }];
      var max = s.max != null ? s.max : m.format === 'pct' ? 1 : Math.max.apply(null, items.map(function (i) { return i.v || 0; })) || 1;
      var sort = s.sort || (m.thresholds ? 'worst' : 'value');
      items.sort(function (a, b) { var c = (a.v || 0) - (b.v || 0); return sort === 'label' ? String(a.name).localeCompare(String(b.name)) : sort === 'worst' ? (m.good === 'down' ? -c : c) : -c; });
      items = items.slice(0, s.limit || 12);
      var sel = by ? selectedSet(p, by) : null, lo = s.max == null && m.format === 'pct' && m.thresholds ? Math.max(0, Math.min.apply(null, items.map(function (i) { return i.v == null ? 1 : i.v; }).concat([s.target != null ? s.target : 1])) - 0.02) : 0;
      lo = m.format === 'pct' && lo > 0.5 ? Math.floor(lo * 100) / 100 : 0;
      function pos(v) { return Math.max(0, Math.min(1, (v - lo) / ((max - lo) || 1))) * 100; }
      var list = h('div', { class: 'awbd-meters' });
      items.forEach(function (it) {
        var st = statusOf(m, it.v);
        var row = h('div', { class: 'awbd-meter' + (sel && !sel.has(String(it.k)) ? ' dim' : ''), tabindex: by ? '0' : null,
          'data-awb': (p.spec.title || p.id) + ': ' + it.name + ' ' + format(it.v, m.format) + (s.target != null ? ', target ' + format(s.target, m.format) : '') + (st ? ', status ' + st : ''),
          onclick: function () { if (by) linkFilter(p, by, it.k); }, onkeydown: function (e) { if (e.key === 'Enter') e.currentTarget.click(); } }, [
          h('span', { class: 'nm', text: it.name, title: it.name }),
          h('div', { class: 'tr', role: 'meter', 'aria-valuemin': String(lo), 'aria-valuemax': String(max), 'aria-valuenow': String(it.v), 'aria-label': it.name }, [
            h('div', { class: 'fl', style: 'width:' + pos(it.v || 0) + '%;' + (st && st !== 'good' ? 'background:' + th.status[st] : '') }),
            s.target != null ? h('div', { class: 'tg', style: 'left:calc(' + pos(s.target) + '% - 1px)', title: 'target ' + format(s.target, m.format) }) : null]),
          h('span', { class: 'fg' }, [format(it.v, m.format), statusEl(dash, st)])]);
        list.appendChild(row);
      });
      p.body.replaceChildren(list);
      if (lo > 0) list.appendChild(h('div', { class: 'awbd-ps', text: 'Scale starts at ' + format(lo, m.format) + '.' }));
      p.table = { columns: [by ? by.spec : 'measure', m.label, 'status'], rows: items.map(function (i) { return [i.name, format(i.v, m.format), statusOf(m, i.v) || '']; }) };
      p.summaryText = items.map(function (i) { var st = statusOf(m, i.v); return i.name + ' ' + format(i.v, m.format) + (st && st !== 'good' ? ' (' + st + ')' : ''); }).join(', ') + (s.target != null ? '; target ' + format(s.target, m.format) : '');
    },
    noTableView: true
  };

  TYPES.waffle = {
    render: function (p) {
      var s = p.spec, dash = p.dash, th = dash.theme, dim = p.dim(s.dim, 'dim'), m = p.measure(s.measure, 'measure'), rows = p.rows();
      var fd = foldDim(p, rows, dim, m, s.limit || 5);
      var g = aggregate(rows, [fd.dim], [m], dash.measure).sort(function (a, b) { return a.keys[0] === OTHER ? 1 : b.keys[0] === OTHER ? -1 : b.v[m.name] - a.v[m.name]; });
      var total = g.reduce(function (t, q) { return t + Math.max(0, q.v[m.name] || 0); }, 0);
      // Largest-remainder rounding to exactly 100 cells.
      var raw = g.map(function (q) { return total ? Math.max(0, q.v[m.name] || 0) / total * 100 : 0; }), cells = raw.map(Math.floor), left = 100 - cells.reduce(function (a, b) { return a + b; }, 0);
      raw.map(function (r, i) { return { i: i, f: r - Math.floor(r) }; }).sort(function (a, b) { return b.f - a.f; }).slice(0, left).forEach(function (o) { cells[o.i]++; });
      var sel = selectedSet(p, dim), grid = h('div', { class: 'cells', role: 'img', 'aria-label': g.map(function (q, i) { return dim.label(q.keys[0]) + ' ' + cells[i] + ' of 100'; }).join(', ') }), legend = h('div', { class: 'awbd-legend' });
      g.forEach(function (q, i) {
        var k = q.keys[0], color = k === OTHER ? th.other : dash.colorOf(dim.field, k), dimmed = sel && !sel.has(String(k));
        for (var c = 0; c < cells[i]; c++) grid.appendChild(h('i', { style: 'background:' + color + (dimmed ? ';opacity:.3' : '') }));
        legend.appendChild(h('div', { class: 'it' + (dimmed ? ' dim' : ''), tabindex: '0', 'data-awb': (p.spec.title || p.id) + ': ' + dim.label(k) + ' ' + format(q.v[m.name], m.format) + ' (' + format(total ? q.v[m.name] / total : null, 'pct') + ')',
          onclick: function () { if (k !== OTHER) linkFilter(p, dim, k); }, onkeydown: function (e) { if (e.key === 'Enter') e.currentTarget.click(); } },
          [h('span', { class: 'sw', style: 'background:' + color }), h('span', { text: dim.label(k) }), h('span', { class: 'pc', text: format(total ? q.v[m.name] / total : null, 'pct') })]));
      });
      p.body.replaceChildren(h('div', { class: 'awbd-waffle' }, [grid, legend]));
      p.table = { columns: [dim.spec, m.label, 'share'], rows: g.map(function (q) { return [dim.label(q.keys[0]), format(q.v[m.name], m.format), format(total ? q.v[m.name] / total : null, 'pct')]; }) };
      p.summaryText = 'each square = 1% of ' + format(total, m.format) + '; ' + p.table.rows.map(function (r) { return r[0] + ' ' + r[2]; }).join(', ');
    },
    noTableView: true
  };

  TYPES.text = {
    render: function (p) {
      var box = h('div', { class: 'awbd-md' }), list = null;
      String(p.spec.text || '').split('\n').forEach(function (line) {
        var t = fillTemplate(p, line.trim());
        if (!t) { list = null; return; }
        var bullet = /^[-*] /.test(t);
        var el = h(bullet ? 'li' : 'p'); inlineMd(el, bullet ? t.slice(2) : t);
        if (bullet) { if (!list) { list = h('ul'); box.appendChild(list); } list.appendChild(el); } else { list = null; box.appendChild(el); }
      });
      p.body.replaceChildren(box);
      p.summaryText = cut(box.textContent.replace(/\s+/g, ' '), 400);
      p.table = null;
    },
    noTableView: true
  };
  function inlineMd(el, t) { t.split(/(\*\*[^*]+\*\*)/).forEach(function (part) { if (/^\*\*[^*]+\*\*$/.test(part)) el.appendChild(h('strong', { text: part.slice(2, -2) })); else if (part) el.appendChild(document.createTextNode(part)); }); }

  TYPES.section = { render: function () {}, noTableView: true };

  // Escape hatch: a raw ECharts option fed by a query. dataset.source is [header, ...rows].
  TYPES.echarts = {
    render: function (p) {
      var s = p.spec, dash = p.dash, q = s.query || {};
      var dims = arr(q.dims).map(function (d, i) { return p.dim(d, 'query.dims[' + i + ']'); }), ms = p.measures(q.measures, 'query.measures');
      var g = aggregate(p.rows(), dims, ms, dash.measure);
      if (q.sort) { var sm = ms.find(function (m) { return m.name === q.sort; }); if (sm) g.sort(function (a, b) { return (b.v[sm.name] || 0) - (a.v[sm.name] || 0); }); else g.sort(function (a, b) { return a.keys[0] < b.keys[0] ? -1 : 1; }); }
      if (q.limit) g = g.slice(0, q.limit);
      var header = dims.map(function (d) { return d.spec; }).concat(ms.map(function (m) { return m.name; }));
      var src = [header].concat(g.map(function (r) { return r.keys.map(function (k, i) { return dims[i].kind === 'time' ? k : dims[i].label(k); }).concat(ms.map(function (m) { return r.v[m.name]; })); }));
      var opt = Object.assign(baseOption(p), JSON.parse(JSON.stringify(s.option)));
      opt.dataset = { source: src };
      if (!opt.tooltip) opt.tooltip = { trigger: 'item', confine: true };
      if (!opt.grid) opt.grid = { left: 8, right: 16, top: 30, bottom: 8, containLabel: true };
      p.draw(opt);
      if (s.filterOn) { var fd = p.dim(s.filterOn, 'filterOn'), fi = header.indexOf(fd.spec); p.onEc('click', function (e) { var v = Array.isArray(e.value) ? e.value[fi] : e.name; if (v != null) linkFilter(p, fd, v); }); }
      p.table = { columns: header, rows: src.slice(1).map(function (r) { return r.map(function (v, i) { return i < dims.length ? (dims[i].kind === 'time' ? fmtTime(v, dims[i].grain) : v) : format(v, ms[i - dims.length].format); }); }) };
      p.summaryText = g.length + ' rows; first: ' + p.table.rows.slice(0, 3).map(function (r) { return r.join(' / '); }).join('; ');
    }
  };

  /* ------------------------------------------------------------------ Panel */
  function Panel(dash, spec, index) {
    this.dash = dash; this.spec = spec; this.index = index; this.id = spec.id || 'p' + (index + 1);
    this.level = 0; this.drillDims = Array.isArray(spec.drill) && spec.drill.length > 1 ? spec.drill : null;
    this.showTable = false; this.handlers = [];
  }
  Panel.prototype.mount = function (grid) {
    var s = this.spec, t = SCHEMA.types[s.type], self = this;
    var w = Math.max(1, Math.min(12, s.w || (t ? t.w : 6))), hh = s.h != null ? s.h : (t ? t.h : 3);
    if (s.type === 'section') {
      this.el = h('div', { class: 'awbd-section', 'data-awb': 'Section: ' + (s.title || '') }, [h('h2', { text: s.title || '' }), s.text ? h('p', { text: s.text }) : null]);
      grid.appendChild(this.el); return;
    }
    this.titleEl = h('h3', { class: 'awbd-pt', id: 'awbd-t-' + this.id, text: s.title || '' });
    this.crumb = h('div', { class: 'awbd-crumb' });
    this.tools = h('div', { class: 'awbd-tools' });
    if (!(TYPES[s.type] && TYPES[s.type].noTableView) && s.type !== 'kpi' && s.type !== 'callout') {
      this.tableBtn = h('button', { class: 'awbd-tool', type: 'button', 'aria-pressed': 'false', title: 'Show the data as a table', text: 'Table', onclick: function () { self.setTableView(!self.showTable); } });
      this.tools.appendChild(this.tableBtn);
    }
    this.body = h('div', { class: 'awbd-body', style: 'height:' + (hh * 110 - (hh > 1 ? 40 : 30)) + 'px' });
    this.el = h('section', { class: 'awbd-panel', 'data-panel': this.id, 'aria-labelledby': 'awbd-t-' + this.id, style: 'grid-column: span ' + w },
      [h('div', { class: 'awbd-ph' }, [h('div', null, [this.titleEl, s.subtitle ? h('p', { class: 'awbd-ps', text: s.subtitle }) : null, this.crumb]), this.tools]), this.body,
        s.note ? h('p', { class: 'awbd-ps', text: s.note, style: 'margin-top:6px' }) : null]);
    grid.appendChild(this.el);
  };
  Panel.prototype.setTableView = function (on) { this.showTable = !!on; if (this.tableBtn) { this.tableBtn.setAttribute('aria-pressed', String(this.showTable)); this.tableBtn.textContent = this.showTable ? 'Chart' : 'Table'; } this.update(); };
  Panel.prototype.update = function () {
    var s = this.spec, self = this;
    if (s.type === 'section') { this.summaryText = s.text || ''; return; }
    this.error = null;
    try {
      var impl = TYPES[s.type];
      if (!impl) fail('type "' + s.type + '" is not a panel type.' + didYouMean(s.type, Object.keys(TYPES)));
      this.src = this.dash.source(s.source, 'source');
      checkKeys(s);
      this.renderCrumb();
      if (this.showTable) { impl.render(this); this.disposeChart(); this.renderTable(); }
      else impl.render(this);
    } catch (e) {
      this.disposeChart();
      this.error = e.message;
      this.summaryText = 'error: ' + e.message;
      this.body.replaceChildren(h('div', { class: 'awbd-err', role: 'alert', text: 'Panel "' + this.id + '": ' + e.message }));
      if (window.console) console.warn('[awb-dash] panel ' + this.id + ':', e);
    }
    this.el.setAttribute('data-awb', cut((s.title || this.id) + ' (' + s.type + ' panel "' + this.id + '"): ' + (this.summaryText || ''), 900));
    if (this.ec && this.summaryText) this.ec.setOption({ aria: { label: { description: (s.title || '') + '. ' + this.summaryText } } });
    void self;
  };
  Panel.prototype.renderCrumb = function () {
    var self = this; this.crumb.replaceChildren();
    if (!this.drillDims) return;
    this.drillDims.slice(0, this.level + 1).forEach(function (d, i) {
      var field = String(self.drillDims[i]).split(':')[0], val = i < self.level ? self.dash.filters[field] : null;
      if (i) self.crumb.appendChild(document.createTextNode(' › '));
      if (i < self.level) self.crumb.appendChild(h('button', { type: 'button', text: field + (val ? ' = ' + Array.from(val).join(', ') : ''), onclick: function () { self.drillUp(i); } }));
      else self.crumb.appendChild(document.createTextNode('by ' + field));
    });
  };
  Panel.prototype.drillUp = function (level) {
    for (var i = this.level - 1; i >= level; i--) this.dash.clearFilter(String(this.drillDims[i]).split(':')[0], true);
    this.level = level; this.dash.update();
  };
  Panel.prototype.renderTable = function () {
    var t = this.table || { columns: [], rows: [] };
    var table = h('table', { class: 'awbd-table' }, [h('thead', null, [h('tr', null, t.columns.map(function (c, i) { return h('th', { class: i ? 'num' : null, scope: 'col', text: c }); }))]),
      h('tbody', null, t.rows.slice(0, 500).map(function (r) { return h('tr', null, r.map(function (c, i) { return h('td', { class: i ? 'num' : null, text: c }); })); }))]);
    this.body.replaceChildren(h('div', { class: 'awbd-tablewrap' }, [table]));
  };
  Panel.prototype.draw = function (opt) {
    if (!this.chartEl || !this.body.contains(this.chartEl)) {
      this.disposeChart();
      this.chartEl = h('div', { class: 'awbd-chart' });
      this.body.replaceChildren(this.chartEl);
    }
    if (!this.ec) {
      this.ec = echarts.init(this.chartEl, this.dash.theme.name, { renderer: 'canvas' });
      var self = this;
      this.ec.on('mouseover', function (e) { self.hover = e; });
      this.ec.on('globalout', function () { self.hover = null; });
    }
    this.handlers.forEach(function (hd) { this.ec.off(hd[0], hd[1]); }, this); this.handlers = [];
    this.ec.setOption(opt, true);
  };
  Panel.prototype.onEc = function (ev, fn) { this.ec.on(ev, fn); this.handlers.push([ev, fn]); };
  Panel.prototype.disposeChart = function () { if (this.ec) { this.ec.dispose(); this.ec = null; } this.chartEl = null; this.pointAt = null; };
  Panel.prototype.rows = function (opts) { return this.dash.rowsFor(this.src, this, opts || {}); };
  // A panel with "period" (e.g. "mtd", "all") keeps that window whatever the time picker says.
  Panel.prototype.periodSel = function () { var pr = this.spec.period; return pr && pr !== 'range' ? pr : null; };
  Panel.prototype.range = function (previous) { return this.dash.range(this.src, previous, this.periodSel()); };
  Panel.prototype.dim = function (spec, where) { return parseDim(this.dash, this.src, spec, where); };
  Panel.prototype.dimFromSpec = Panel.prototype.dim;
  Panel.prototype.measure = function (name, where) { return this.dash.resolveMeasure(name, this.src, where); };
  Panel.prototype.measures = function (list, where) {
    var self = this, a = arr(list);
    if (!a.length) fail(where + ': list at least one measure.');
    return a.map(function (n, i) { return self.measure(n, where + (a.length > 1 ? '[' + i + ']' : '')); });
  };
  Panel.prototype.total = function (m) { var g = aggregate(this.rows(), [], [m], this.dash.measure); return g.length ? g[0].v[m.name] : null; };
  Panel.prototype.describe = function (click) {
    var head = (this.spec.title || this.id) + ' (' + this.spec.type + ', panel "' + this.id + '")';
    var hv = this.hover, text = '';
    if (hv && hv.componentType === 'series') {
      var v = hv.value, val = Array.isArray(v) ? v[v.length > 2 && this.spec.type === 'heatmap' ? 2 : 1] : v;
      text = (hv.seriesName && hv.seriesType !== 'pie' && hv.seriesType !== 'treemap' ? hv.seriesName + ' · ' : '') + (hv.name || (Array.isArray(v) && typeof v[0] === 'number' && v[0] > 1e11 ? fmtTime(v[0], 'day') : '')) + (val != null && typeof val === 'number' ? ': ' + format(val, (this.lastFormat || 'number')) : '');
      if (this.spec.type === 'scatter' && Array.isArray(v)) text = hv.name + ': x ' + v[0] + ', y ' + v[1];
    }
    if (!text && this.pointAt && click && this.chartEl) {
      var r = this.chartEl.getBoundingClientRect();
      try { text = this.pointAt(click.clientX - r.left, click.clientY - r.top); } catch (e) { text = ''; }
    }
    return cut(head + ': ' + (text || this.summaryText || '') + '. ' + this.dash.scopeText(), 1000);
  };
  function checkKeys(s) {
    var t = SCHEMA.types[s.type]; if (!t) return;
    Object.keys(s).forEach(function (k) {
      if (k in SCHEMA.common || k in t.opt) return;
      fail('unknown key "' + k + '" for a ' + s.type + ' panel.' + didYouMean(k, Object.keys(t.opt).concat(Object.keys(SCHEMA.common))));
    });
    t.req.forEach(function (k) { if (s[k] == null || s[k] === '') fail('a ' + s.type + ' panel needs "' + k + '".'); });
  }

  /* ------------------------------------------------------------------ Dashboard */
  function Dashboard(root, spec) {
    this.root = root; this.spec = spec; this.filters = {}; this.timeSel = null; this.colorMaps = {}; this.panels = [];
    var self = this;
    this.measure = function (n) { return self.measures[n] || self.inline[n]; };
  }
  Dashboard.prototype.init = function () {
    var self = this, spec = this.spec;
    if (!spec || typeof spec !== 'object') fail('the spec must be a JSON object.');
    if (!spec.sources || typeof spec.sources !== 'object' || !Object.keys(spec.sources).length) fail('"sources" must name at least one data source.');
    if (!Array.isArray(spec.panels) || !spec.panels.length) fail('"panels" must list at least one panel.');
    Object.keys(spec).forEach(function (k) { if (!(k in SCHEMA.top)) fail('unknown top-level key "' + k + '".' + didYouMean(k, Object.keys(SCHEMA.top))); });
    var names = Object.keys(spec.sources);
    return Promise.all(names.map(function (n) { return loadSource(n, spec.sources[n]); })).then(function (list) {
      self.sources = {}; list.forEach(function (s) { self.sources[s.name] = s; });
      self.defaultSource = names[0];
      var tf = spec.time && spec.time.field;
      list.forEach(function (s) {
        if (!s.timeField && tf && s.fields[tf]) s.timeField = tf;
        if (s.timeField && (!s.fields[s.timeField] || s.fields[s.timeField].type !== 'time')) fail('source "' + s.name + '": time field "' + s.timeField + '" is missing or not a date/time column.');
      });
      if (tf && !list.some(function (s) { return s.timeField; })) fail('time.field "' + tf + '" is not a date/time column in any source.' + didYouMean(tf, Object.keys(list[0].fields)));
      self.computeTimeBounds();
      self.measures = {}; self.inline = {};
      Object.keys(spec.measures || {}).forEach(function (n) {
        var d = spec.measures[n];
        if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(n)) fail('measure name "' + n + '": use letters, digits and _.');
        self.measures[n] = compileMeasure(n, d, null, 'measures.' + n);
      });
      Object.keys(self.measures).forEach(function (n) { var m = self.measures[n]; if (m.ratio) m.ratio.forEach(function (r) { if (!self.measures[r]) fail('measures.' + n + '.ratio: no measure "' + r + '".' + didYouMean(r, Object.keys(self.measures))); }); });
      self.timeSel = spec.time && spec.time['default'] ? spec.time['default'] : (self.hasTime ? '30d' : 'all');
      self.themePref = spec.theme || 'auto';
      self.build();
      self.update();
      if (spec.refresh && spec.refresh >= 10) self.timer = setInterval(function () { self.reload(); }, spec.refresh * 1000);
      return self;
    });
  };
  Dashboard.prototype.reload = function () {
    var self = this, names = Object.keys(this.spec.sources).filter(function (n) { return self.spec.sources[n].url; });
    this.panels.forEach(function (p) { if (p.el) p.el.classList.add('loading'); });
    return Promise.all(names.map(function (n) { return loadSource(n, self.spec.sources[n]); })).then(function (list) {
      list.forEach(function (s) { var old = self.sources[s.name]; s.timeField = old.timeField; self.sources[s.name] = s; });
      self.computeTimeBounds(); self.update();
    }).catch(function (e) { console.warn('[awb-dash] refresh failed', e); }).then(function () { self.panels.forEach(function (p) { if (p.el) p.el.classList.remove('loading'); }); });
  };
  Dashboard.prototype.computeTimeBounds = function () {
    var lo = Infinity, hi = -Infinity, step = Infinity, self = this;
    Object.keys(this.sources).forEach(function (n) {
      var s = self.sources[n]; if (!s.timeField) return;
      var ts = new Set();
      s.rows.forEach(function (r) { var t = r[s.timeField]; if (t == null) return; if (t < lo) lo = t; if (t > hi) hi = t; if (ts.size < 5000) ts.add(t); });
      var sorted = Array.from(ts).sort(function (a, b) { return a - b; });
      for (var i = 1; i < sorted.length; i++) { var d = sorted[i] - sorted[i - 1]; if (d > 0 && d < step) step = d; }
      s.step = isFinite(step) ? step : DAY;
    });
    this.hasTime = isFinite(lo);
    this.dataMin = lo; this.dataMax = hi; this.step = isFinite(step) ? step : DAY;
    this.anchor = (this.spec.time && this.spec.time.anchor === 'now') ? Date.now() : hi + this.step;
  };
  Dashboard.prototype.range = function (src, previous, override) {
    if (!this.hasTime) return { from: -Infinity, to: Infinity };
    var sel = override || this.timeSel, to = this.anchor, from;
    if (sel && typeof sel === 'object') { from = sel.from; to = sel.to; }
    else if (sel === 'all') { from = this.dataMin; to = this.anchor; }
    else if (sel === 'mtd' || sel === 'qtd' || sel === 'ytd') { from = bucket(to - 1, sel === 'mtd' ? 'month' : sel === 'qtd' ? 'quarter' : 'year'); }
    else { var mm = /^(\d+)([hdy])$/.exec(sel || '30d'); var n = Number(mm[1]), u = mm[2]; from = to - n * (u === 'h' ? HOUR : u === 'd' ? DAY : 365 * DAY); }
    if (previous) { var span = to - from; return { from: from - span, to: from }; }
    return { from: from, to: to };
  };
  Dashboard.prototype.grain = function (src) {
    var r = this.range(src), span = (isFinite(r.to) ? r.to : this.dataMax) - (isFinite(r.from) ? r.from : this.dataMin);
    var g = this.spec.time && this.spec.time.grain;
    return g && g !== 'auto' ? g : autoGrain(span, src && src.step || this.step);
  };
  Dashboard.prototype.rangeLabel = function (short) {
    var sel = this.timeSel;
    if (!this.hasTime) return 'all data';
    if (sel && typeof sel === 'object') return short ? 'period' : fmtTime(sel.from, 'day') + ' – ' + fmtTime(sel.to - 1, 'day');
    var names = { '1h': 'last hour', '6h': 'last 6 hours', '24h': 'last 24 hours', '7d': 'last 7 days', '14d': 'last 14 days', '30d': 'last 30 days', '90d': 'last 90 days', '180d': 'last 180 days', '1y': 'last year', mtd: 'month to date', qtd: 'quarter to date', ytd: 'year to date', all: 'all time' };
    if (short) return sel === 'all' ? 'period' : (/^\d/.test(sel) ? sel : 'period');
    return names[sel] || sel;
  };
  Dashboard.prototype.rowsFor = function (src, panel, opts) {
    var self = this, r = opts.noTime || !src.timeField ? null : this.range(src, opts.previous, panel && panel.periodSel ? panel.periodSel() : null), tf = src.timeField;
    var pw = panel && panel.spec.where && !opts.noWhere ? panel.spec.where : null;
    var own = {};
    if (panel && panel.spec.crossfilter !== false) {
      // A panel is not filtered by the selection made in itself (it highlights instead), except where it drilled.
      var dims = [].concat(arr(panel.spec.x), arr(panel.spec.dim), arr(panel.spec.by), arr(panel.spec.dims), arr(panel.spec.path), arr(panel.spec.series), arr(panel.spec.y && panel.spec.type === 'heatmap' ? panel.spec.y : null));
      if (panel.drillDims) dims = [panel.drillDims[panel.level]];
      dims.forEach(function (d) { if (typeof d === 'string') own[d.split(':')[0]] = 1; });
    }
    var active = Object.keys(this.filters).filter(function (f) { return src.fields[f] && !own[f]; });
    if (!r && !active.length && !pw) return src.rows;
    return src.rows.filter(function (row) {
      if (pw && !wherePass(pw, row)) return false;
      if (r) { var t = row[tf]; if (t == null || t < r.from || t >= r.to) return false; }
      for (var i = 0; i < active.length; i++) { if (!self.filters[active[i]].has(String(row[active[i]]))) return false; }
      return true;
    });
  };
  Dashboard.prototype.source = function (name, where) {
    var n = name || this.defaultSource, s = this.sources[n];
    if (!s) fail(where + ': no source "' + n + '".' + didYouMean(n, Object.keys(this.sources)));
    return s;
  };
  Dashboard.prototype.resolveMeasure = function (name, src, where) {
    if (typeof name !== 'string' || !name) fail(where + ': a measure is a name from "measures" or an inline aggregate such as "sum(cost_usd)".');
    var m = this.measures[name] || this.inline[name];
    if (!m) {
      var d = inlineMeasure(name);
      if (!d) fail(where + ': no measure "' + name + '".' + didYouMean(name, Object.keys(this.measures)) + ' Inline form: sum(field), count(), count_distinct(field), avg(field), p95(field).');
      m = compileMeasure(name, d, null, where); this.inline[name] = m;
    }
    var self = this;
    (function check(mm) {
      if (mm.ratio) { mm.ratio.forEach(function (r) { check(self.measure(r)); }); return; }
      [mm.field, mm.weight].concat(Object.keys(mm.where || {})).forEach(function (f) {
        if (f && !src.fields[f]) fail(where + ': measure "' + mm.name + '" uses field "' + f + '", which source "' + src.name + '" lacks.' + didYouMean(f, Object.keys(src.fields)));
      });
      if (mm.field && ['count', 'count_distinct', 'first', 'last'].indexOf(mm.agg) < 0 && src.fields[mm.field].type !== 'number')
        fail(where + ': measure "' + mm.name + '" takes ' + mm.agg + ' of "' + mm.field + '", which is ' + src.fields[mm.field].type + ', not a number. Use count_distinct or another field.');
    })(m);
    return m;
  };
  Dashboard.prototype.colorOf = function (field, value) {
    var map = this.colorMaps[field];
    if (!map) {
      map = this.colorMaps[field] = new Map();
      var fixed = this.spec.colors && this.spec.colors[field];
      if (fixed) Object.keys(fixed).forEach(function (k) { map.set(k, fixed[k]); });
      // Pre-assign by frequency in the full data so colors never depend on the current filter.
      var counts = new Map(), self = this;
      Object.keys(this.sources).forEach(function (n) { var s = self.sources[n]; if (!s.fields[field]) return; s.rows.forEach(function (r) { var v = String(r[field]); counts.set(v, (counts.get(v) || 0) + 1); }); });
      var ranked = Array.from(counts.entries()).sort(function (a, b) { return b[1] - a[1]; });
      ranked.forEach(function (e) { if (!map.has(e[0]) && map.size < 8) map.set(e[0], map.size); });
    }
    var slot = map.get(String(value));
    if (slot == null) return this.theme.other;
    return typeof slot === 'number' ? this.theme.series[slot] : slot;
  };
  Dashboard.prototype.setFilter = function (field, values, silent) {
    if (!values || !values.length) delete this.filters[field]; else this.filters[field] = new Set(values.map(String));
    if (!silent) this.update();
  };
  Dashboard.prototype.toggleFilter = function (field, value) {
    var s = this.filters[field];
    if (s && s.has(value)) { s.delete(value); if (!s.size) delete this.filters[field]; }
    else { if (!s) s = this.filters[field] = new Set(); s.add(value); }
    this.update();
  };
  Dashboard.prototype.clearFilter = function (field, silent) { delete this.filters[field]; if (!silent) this.update(); };
  Dashboard.prototype.clearAll = function () { this.filters = {}; this.panels.forEach(function (p) { p.level = 0; }); this.update(); };
  Dashboard.prototype.setTime = function (sel) { this.timeSel = sel; this.update(); };
  Dashboard.prototype.scopeText = function () {
    var self = this, f = Object.keys(this.filters).map(function (k) { return k + ' = ' + Array.from(self.filters[k]).join(' | '); });
    return 'Showing ' + this.rangeLabel() + (f.length ? '; filters: ' + f.join('; ') : '; no filters');
  };

  Dashboard.prototype.build = function () {
    var self = this, spec = this.spec, root = this.root;
    root.classList.add('awbd');
    root.replaceChildren();
    this.theme = readTheme(root);
    root.appendChild(h('header', { class: 'awbd-head' }, [h('h1', { class: 'awbd-title', text: spec.title || 'Dashboard' }), spec.description ? h('p', { class: 'awbd-desc', text: spec.description }) : null]));
    this.bar = h('div', { class: 'awbd-bar', role: 'toolbar', 'aria-label': 'Dashboard filters' });
    root.appendChild(this.bar);
    this.grid = h('div', { class: 'awbd-grid' });
    root.appendChild(this.grid);
    var ids = {};
    spec.panels.forEach(function (ps, i) {
      if (!ps || typeof ps !== 'object') fail('panels[' + i + '] must be an object.');
      var p = new Panel(self, ps, i);
      if (ids[p.id]) fail('panel id "' + p.id + '" is used twice.'); ids[p.id] = 1;
      p.mount(self.grid); self.panels.push(p);
    });
    var ro = new ResizeObserver(function () { self.panels.forEach(function (p) { if (p.ec) p.ec.resize(); }); });
    this.panels.forEach(function (p) { if (p.body) ro.observe(p.body); });
    var mq = window.matchMedia('(prefers-color-scheme: dark)');
    if (mq.addEventListener) mq.addEventListener('change', function () { self.retheme(); });
    if (this.themePref !== 'auto') this.setTheme(this.themePref, true);
    document.addEventListener('click', function (e) { if (!self.bar.contains(e.target)) self.closePops(); });
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape') self.closePops(); });
    this.wireAwb();
  };
  Dashboard.prototype.closePops = function () { this.bar.querySelectorAll('.awbd-pop').forEach(function (p) { p.hidden = true; }); this.bar.querySelectorAll('[aria-expanded]').forEach(function (b) { b.setAttribute('aria-expanded', 'false'); }); };
  Dashboard.prototype.setTheme = function (pref, noUpdate) {
    this.themePref = pref;
    if (pref === 'auto') document.documentElement.removeAttribute('data-theme'); else document.documentElement.setAttribute('data-theme', pref);
    this.retheme(noUpdate);
  };
  Dashboard.prototype.retheme = function (noUpdate) {
    this.theme = readTheme(this.root);
    this.panels.forEach(function (p) { p.disposeChart(); });
    if (!noUpdate) this.update();
  };
  Dashboard.prototype.renderBar = function () {
    var self = this, spec = this.spec, bar = this.bar;
    bar.replaceChildren();
    if (this.hasTime) {
      var presets = (spec.time && spec.time.presets) || ['7d', '30d', '90d', 'all'];
      var pop = h('div', { class: 'awbd-pop', hidden: true, role: 'menu' });
      presets.forEach(function (pr) {
        pop.appendChild(h('button', { class: 'awbd-opt', type: 'button', role: 'menuitemradio', 'aria-checked': String(self.timeSel === pr), onclick: function () { self.closePops(); self.setTime(pr); } },
          [h('span', { class: 'chk', text: self.timeSel === pr ? '✓' : '' }), h('span', { text: self.rangeLabel.call({ timeSel: pr, hasTime: true }) })]));
      });
      var cur = this.range(), fromIn = h('input', { type: 'date', value: isoDay(cur.from), 'aria-label': 'From' }), toIn = h('input', { type: 'date', value: isoDay(cur.to - 1), 'aria-label': 'To' });
      pop.appendChild(h('div', { class: 'foot' }, [fromIn, '–', toIn, h('button', { class: 'awbd-btn', type: 'button', text: 'Apply', onclick: function () {
        var f = Date.parse(fromIn.value + 'T00:00:00Z'), t = Date.parse(toIn.value + 'T00:00:00Z') + DAY; if (f < t) { self.closePops(); self.setTime({ from: f, to: t }); } } })]));
      var btn = h('button', { class: 'awbd-btn', type: 'button', 'aria-haspopup': 'true', 'aria-expanded': 'false', 'data-awb': 'Time range: ' + this.rangeLabel(),
        onclick: function (e) { e.stopPropagation(); var open = pop.hidden; self.closePops(); pop.hidden = !open; btn.setAttribute('aria-expanded', String(open)); } },
        [icon('clock'), h('span', { text: this.rangeLabel() }), h('span', { class: 'k', text: '▾' })]);
      btn.querySelector('svg').setAttribute('width', '16'); btn.querySelector('svg').setAttribute('height', '16');
      bar.appendChild(h('div', { class: 'awbd-ctl' }, [btn, pop]));
    }
    arr(spec.filters).forEach(function (f) {
      var field = typeof f === 'string' ? f : f.field, label = (f && f.label) || field;
      var src = Object.keys(self.sources).map(function (n) { return self.sources[n]; }).find(function (s) { return s.fields[field]; });
      if (!src) { bar.appendChild(h('span', { class: 'awbd-err', text: 'filter "' + field + '": no source has this field.' })); return; }
      var counts = new Map(); src.rows.forEach(function (r) { var v = String(r[field]); counts.set(v, (counts.get(v) || 0) + 1); });
      var values = Array.from(counts.keys()).sort(function (a, b) { return counts.get(b) - counts.get(a); });
      var sel = self.filters[field], pop = h('div', { class: 'awbd-pop', hidden: true });
      var list = h('div', { role: 'group', 'aria-label': label });
      function fill(q) {
        list.replaceChildren();
        values.filter(function (v) { return !q || v.toLowerCase().indexOf(q) >= 0; }).slice(0, 200).forEach(function (v) {
          var on = sel && sel.has(v);
          list.appendChild(h('button', { class: 'awbd-opt', type: 'button', role: 'menuitemcheckbox', 'aria-checked': String(!!on), onclick: function (e) { e.stopPropagation(); self.toggleFilter(field, v); } },
            [h('span', { class: 'chk', text: on ? '✓' : '' }), h('span', { text: v }), h('span', { class: 'n', text: format(counts.get(v), 'int') })]));
        });
      }
      if (values.length > 8) pop.appendChild(h('input', { type: 'search', placeholder: 'Search ' + label, 'aria-label': 'Search ' + label, oninput: function (e) { fill(e.target.value.toLowerCase()); } }));
      pop.appendChild(list); fill('');
      if (sel) pop.appendChild(h('div', { class: 'foot' }, [h('button', { class: 'awbd-btn', type: 'button', text: 'Clear', onclick: function () { self.closePops(); self.clearFilter(field); } })]));
      var txt = sel ? (sel.size === 1 ? Array.from(sel)[0] : sel.size + ' selected') : 'All';
      var btn = h('button', { class: 'awbd-btn', type: 'button', 'aria-haspopup': 'true', 'aria-expanded': 'false', 'aria-pressed': sel ? 'true' : 'false', 'data-awb': 'Filter ' + label + ': ' + (sel ? Array.from(sel).join(', ') : 'all'),
        onclick: function (e) { e.stopPropagation(); var open = pop.hidden; self.closePops(); pop.hidden = !open; btn.setAttribute('aria-expanded', String(open)); } },
        [h('span', { class: 'k', text: label + ':' }), h('span', { text: cut(txt, 24) }), h('span', { class: 'k', text: '▾' })]);
      var ctl = h('div', { class: 'awbd-ctl', 'data-field': field }, [btn, pop]);
      bar.appendChild(ctl);
      if (self.openPop === field) { pop.hidden = false; btn.setAttribute('aria-expanded', 'true'); }
    });
    // Chips for cross-filters on fields without a control.
    var declared = arr(spec.filters).map(function (f) { return typeof f === 'string' ? f : f.field; });
    var chips = h('div', { class: 'awbd-chips', 'aria-live': 'polite' });
    Object.keys(this.filters).filter(function (k) { return declared.indexOf(k) < 0; }).forEach(function (k) {
      chips.appendChild(h('span', { class: 'awbd-chip', 'data-awb': 'Cross-filter ' + k + ' = ' + Array.from(self.filters[k]).join(', ') }, [k + ' = ' + cut(Array.from(self.filters[k]).join(', '), 40),
        h('button', { type: 'button', 'aria-label': 'Remove filter ' + k, text: '×', onclick: function () { self.clearFilter(k); } })]));
    });
    bar.appendChild(chips);
    bar.appendChild(h('span', { class: 'awbd-spacer' }));
    if (Object.keys(this.filters).length || (this.timeSel && typeof this.timeSel === 'object'))
      bar.appendChild(h('button', { class: 'awbd-btn', type: 'button', text: 'Reset', 'data-awb': 'Button: reset filters and time range', onclick: function () { self.timeSel = (spec.time && spec.time['default']) || '30d'; self.clearAll(); } }));
    var themes = { auto: 'Theme: auto', light: 'Theme: light', dark: 'Theme: dark' };
    bar.appendChild(h('button', { class: 'awbd-btn', type: 'button', text: themes[this.themePref], 'data-awb': 'Button: switch light / dark theme (now ' + this.themePref + ')',
      onclick: function () { self.setTheme({ auto: 'light', light: 'dark', dark: 'auto' }[self.themePref]); } }));
  };
  Dashboard.prototype.update = function () {
    var self = this;
    // Remember which filter popover was open so a toggle keeps it open.
    var open = this.bar.querySelector('.awbd-pop:not([hidden])'); this.openPop = open && open.parentNode.getAttribute('data-field');
    this.renderBar();
    this.panels.forEach(function (p) { p.update(); });
    if (this.onUpdate) this.onUpdate(this);
    void self;
  };

  /* ------------------------------------------------------------------ AWB SDK wiring */
  Dashboard.prototype.contextText = function () {
    var self = this, lines = [];
    lines.push('Dashboard "' + (this.spec.title || 'Dashboard') + '". ' + this.scopeText() + '.');
    if (this.hasTime) lines.push('Data spans ' + fmtTime(this.dataMin, 'day') + ' to ' + fmtTime(this.dataMax, 'day') + ' (UTC).');
    lines.push('Panels (id: summary):');
    this.panels.forEach(function (p) {
      if (p.spec.type === 'section') { lines.push('## ' + (p.spec.title || '')); return; }
      lines.push('- ' + p.id + ' [' + p.spec.type + '] ' + (p.spec.title || '') + (p.level ? ' (drilled to ' + p.drillDims[p.level] + ')' : '') + ': ' + cut(p.summaryText || '', 260));
    });
    var fs = arr(this.spec.filters).map(function (f) { return typeof f === 'string' ? f : f.field; });
    lines.push('Filterable fields: ' + (fs.join(', ') || 'none') + '. Each panel id is also a detail with its full data table.');
    return lines.join('\n');
  };
  Dashboard.prototype.wireAwb = function () {
    var awb = window.awb, self = this;
    if (!awb) return;
    try {
      awb.describe('.awbd-chart', function (el, click) {
        var host = el.closest('.awbd-panel'), p = host && self.panels.find(function (x) { return x.id === host.getAttribute('data-panel'); });
        return p ? p.describe(click) : '';
      });
      awb.context(function () { return self.contextText(); });
      var n = 0;
      this.panels.forEach(function (p) {
        if (p.spec.type === 'section' || n >= 28 || !/^[A-Za-z][A-Za-z0-9_.-]{0,63}$/.test(p.id)) return; n++;
        awb.detail(p.id, function () {
          if (p.error) return 'Panel ' + p.id + ' failed: ' + p.error;
          var t = p.table || { columns: [], rows: [] };
          return (p.spec.title || p.id) + ' (' + p.spec.type + '). ' + self.scopeText() + '.\n' + t.columns.join('\t') + '\n' + t.rows.slice(0, 2000).map(function (r) { return r.join('\t'); }).join('\n');
        });
      });
      awb.detail('spec', function () { return JSON.stringify(self.spec, null, 1); });
      awb.detail('fields', function () {
        return Object.keys(self.sources).map(function (n) { var s = self.sources[n]; return n + ' (' + s.rows.length + ' rows): ' + Object.keys(s.fields).map(function (f) { return f + ':' + s.fields[f].type; }).join(', '); }).join('\n')
          + '\nMeasures: ' + Object.keys(self.measures).map(function (k) { var m = self.measures[k]; return k + ' (' + (m.ratio ? m.ratio.join('/') : m.agg + (m.field ? ' ' + m.field : '')) + ', ' + m.format + ')'; }).join(', ');
      });
      var fieldsAll = arr(this.spec.filters).map(function (f) { return typeof f === 'string' ? f : f.field; });
      awb.command('setTimeRange', { description: 'Set the dashboard time range, as the time picker does.',
        params: { range: { type: 'string', required: true, description: 'A preset (' + SCHEMA.presets.join(', ') + ') or YYYY-MM-DD..YYYY-MM-DD' } } },
        function (pr) { self.setTime(parseRange(pr.range)); return self.scopeText(); });
      awb.command('setFilter', { description: 'Filter every panel to some values of a field (empty values clears it), as the filter menus and chart clicks do.',
        params: { field: { type: 'string', required: true, description: 'Field name, e.g. ' + (fieldsAll.slice(0, 4).join(', ') || 'team') }, values: { type: 'string', required: true, description: 'Values separated by |, or empty to clear' } } },
        function (pr) {
          if (!Object.keys(self.sources).some(function (n) { return self.sources[n].fields[pr.field]; })) throw new Error('no field "' + pr.field + '".' + didYouMean(pr.field, Object.keys(self.sources[self.defaultSource].fields)));
          var vals = pr.values.split('|').map(function (v) { return v.trim(); }).filter(Boolean);
          self.setFilter(pr.field, vals); return self.scopeText();
        });
      awb.command('clearFilters', { description: 'Remove every filter and drill-down (keeps the time range).', params: {} }, function () { self.clearAll(); return self.scopeText(); });
      awb.command('drill', { description: 'Drill a panel with "drill" levels into one value (or up with value empty), as clicking a bar does.',
        params: { panel: { type: 'string', required: true, description: 'Panel id' }, value: { type: 'string', required: true, description: 'Value at the current level; empty goes up one level' } } },
        function (pr) {
          var p = self.panel(pr.panel); if (!p.drillDims) throw new Error('panel "' + p.id + '" has no drill levels.');
          if (!pr.value) { if (p.level) p.drillUp(p.level - 1); }
          else { if (p.level >= p.drillDims.length - 1) throw new Error('panel "' + p.id + '" is at its deepest level.'); self.setFilter(String(p.drillDims[p.level]).split(':')[0], [pr.value], true); p.level++; self.update(); }
          return p.id + ': ' + p.summaryText;
        });
      awb.command('showTable', { description: 'Show a chart panel as a data table, or back as a chart.',
        params: { panel: { type: 'string', required: true, description: 'Panel id' }, on: { type: 'boolean', required: true } } },
        function (pr) { var p = self.panel(pr.panel); p.setTableView(pr.on); return p.id + (pr.on ? ' shows its table' : ' shows its chart'); });
      awb.command('focusPanel', { description: 'Scroll to a panel and outline it for the user.', params: { panel: { type: 'string', required: true, description: 'Panel id' } } },
        function (pr) { var p = self.panel(pr.panel); self.panels.forEach(function (x) { if (x.el) x.el.classList.remove('focus'); }); p.el.classList.add('focus'); p.el.scrollIntoView({ block: 'center', behavior: 'smooth' }); return p.id + ': ' + p.summaryText; });
      awb.command('setTheme', { description: 'Switch the dashboard theme.', params: { theme: { type: 'string', required: true, enum: ['auto', 'light', 'dark'] } } },
        function (pr) { self.setTheme(pr.theme); return 'Theme ' + pr.theme; });
    } catch (e) { console.warn('[awb-dash] AWB SDK wiring failed:', e); }
  };
  Dashboard.prototype.panel = function (id) {
    var p = this.panels.find(function (x) { return x.id === id; });
    if (!p || p.spec.type === 'section') throw new Error('no panel "' + id + '".' + didYouMean(id, this.panels.map(function (x) { return x.id; })));
    return p;
  };
  function parseRange(s) {
    s = String(s || '').trim();
    if (SCHEMA.presets.indexOf(s) >= 0) return s;
    var m = /^(\d{4}-\d{2}-\d{2})\s*\.\.\s*(\d{4}-\d{2}-\d{2})$/.exec(s);
    if (!m) throw new Error('range must be a preset (' + SCHEMA.presets.join(', ') + ') or YYYY-MM-DD..YYYY-MM-DD.');
    var f = Date.parse(m[1] + 'T00:00:00Z'), t = Date.parse(m[2] + 'T00:00:00Z') + DAY;
    if (!(f < t)) throw new Error('the range ends before it starts.');
    return { from: f, to: t };
  }

  /* ------------------------------------------------------------------ public API */
  function render(target, specOrUrl) {
    var root = typeof target === 'string' ? document.querySelector(target) : target;
    if (!root) return Promise.reject(new Error('AwbDash.render: no element ' + target));
    var specP = typeof specOrUrl === 'string' ? fetch(specOrUrl, { credentials: 'same-origin' }).then(function (r) {
      if (!r.ok) throw new Error('GET ' + specOrUrl + ' answered ' + r.status + '.');
      return r.text().then(function (t) { try { return JSON.parse(t); } catch (e) { throw new Error(specOrUrl + ' is not valid JSON: ' + e.message); } });
    }) : Promise.resolve(specOrUrl);
    return specP.then(function (spec) { var d = new Dashboard(root, spec); window.awbDashboard = d; return d.init(); }).catch(function (e) {
      root.classList.add('awbd');
      root.replaceChildren(h('div', { class: 'awbd-err', role: 'alert', text: 'Dashboard error: ' + e.message }));
      if (window.awb) { try { window.awb.context(function () { return 'The dashboard failed to load: ' + e.message; }); } catch (x) { /* ignore */ } }
      console.error('[awb-dash]', e);
      throw e;
    });
  }
  window.AwbDash = {
    version: VERSION, render: render, schema: SCHEMA,
    registerType: function (name, impl) { if (TYPES[name]) throw new Error('panel type "' + name + '" exists'); TYPES[name] = impl; SCHEMA.types[name] = { req: impl.req || [], opt: impl.opt || {}, w: impl.w || 6, h: impl.h || 3 }; },
    kit: { parseCsv: parseCsv, inferTypes: inferTypes, aggregate: aggregate, format: format, formatDelta: formatDelta, fmtTime: fmtTime, bucket: bucket, el: h, icon: icon }
  };
})();
