#!/usr/bin/env python3
"""Check an AWB Dash dashboard.json against its data before you open it in a browser.

Usage (from the app directory):
  python3 check_dashboard.py [dashboard.json]            check the spec; exit 1 on any ERROR
  python3 check_dashboard.py --profile [dashboard.json]  also print every source's fields (type, distinct, examples)
  python3 check_dashboard.py --profile-csv data/x.csv    profile one data file before writing a spec

Reads the schema from static/awb-dash.js (between SCHEMA-BEGIN and SCHEMA-END), so the checker and the renderer
agree. Loads every source with a relative "url" (CSV or JSON) from the app directory, inline "rows" or "csv".
A source served by app.py (an API route) is loaded by running ./app.py with PATH_INFO, as the host would.
Stdlib only.
"""
import csv
import difflib
import io
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?$")
INLINE_RE = re.compile(r"^([a-z_0-9]+)\(([^()]*)\)$")
ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
MAX_ROWS = 300000
SERIES_COLORS = 8

errors, warnings = [], []


def err(where, msg):
    errors.append(f"ERROR   {where}: {msg}")


def warn(where, msg):
    warnings.append(f"WARNING {where}: {msg}")


def hint(name, choices):
    m = difflib.get_close_matches(str(name), list(choices), n=1, cutoff=0.6)
    return f' Did you mean "{m[0]}"?' if m else (" Known: " + ", ".join(list(choices)[:20]) + ".")


def load_schema(app_dir):
    for cand in (os.path.join(app_dir, "static", "awb-dash.js"),
                 os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets", "app", "static", "awb-dash.js")):
        if os.path.isfile(cand):
            text = open(cand, encoding="utf-8").read()
            m = re.search(r"SCHEMA-BEGIN[^\n]*\n\s*var SCHEMA = (\{.*?\});\s*\n\s*/\* SCHEMA-END", text, re.S)
            if m:
                return json.loads(m.group(1))
    sys.exit("check_dashboard: cannot find static/awb-dash.js with a SCHEMA block. Run from the app directory.")


def infer(rows, overrides=None):
    fields = {}
    names = list(rows[0].keys()) if rows else []
    for n in names:
        if overrides and n in overrides:
            fields[n] = {"type": overrides[n]}
            continue
        is_num = is_time = True
        seen = 0
        for r in rows:
            v = r.get(n)
            if v is None or v == "":
                continue
            seen += 1
            if seen > 1000:
                break
            if isinstance(v, bool):
                is_num = is_time = False
                break
            if isinstance(v, (int, float)):
                is_time = False
                continue
            s = str(v)
            if is_num:
                try:
                    float(s)
                except ValueError:
                    is_num = False
            if is_time and not TIME_RE.match(s):
                is_time = False
            if not is_num and not is_time:
                break
        t = "string" if seen == 0 else "time" if is_time and not is_num else "number" if is_num else "string"
        vals = [r.get(n) for r in rows if r.get(n) not in (None, "")]
        distinct = set(map(str, vals))
        info = {"type": t, "distinct": len(distinct), "examples": sorted(distinct)[:4] if t == "string" else [], "blank": len(rows) - len(vals)}
        if t == "number":
            nums = [float(v) for v in vals]
            info["min"], info["max"] = (min(nums), max(nums)) if nums else (None, None)
        if t == "time":
            info["min"], info["max"] = (min(map(str, vals)), max(map(str, vals))) if vals else (None, None)
        fields[n] = info
    return fields


def load_rows(app_dir, name, d):
    where = f"sources.{name}"
    if not isinstance(d, dict):
        err(where, 'must be an object with "url", "rows" or "csv".')
        return None
    if isinstance(d.get("rows"), list):
        return d["rows"]
    if isinstance(d.get("csv"), str):
        return list(csv.DictReader(io.StringIO(d["csv"])))
    url = d.get("url")
    if not isinstance(url, str):
        err(where, 'needs "url" (relative, e.g. "data/usage.csv"), "rows" or "csv".')
        return None
    if re.match(r"^[a-z]+:|^//|^/", url, re.I):
        err(where, f'url "{url}" must be relative to the app (no scheme, no leading /): the app CSP only allows its own origin.')
        return None
    path = url.split("?")[0]
    full = os.path.join(app_dir, path)
    if os.path.isfile(full):
        raw = open(full, "rb").read()
    else:
        entry = os.path.join(app_dir, "app.py")
        if not os.path.isfile(entry):
            err(where, f'"{url}" is not a file in the app directory and there is no app.py to serve it.')
            return None
        env = dict(os.environ, REQUEST_METHOD="GET", PATH_INFO="/" + path, QUERY_STRING=url.partition("?")[2])
        r = subprocess.run([sys.executable, entry], cwd=app_dir, env=env, capture_output=True, timeout=30)
        head, _, raw = r.stdout.partition(b"\n\n")
        if b"200" not in head.split(b"\n")[0]:
            err(where, f'GET {url} through app.py answered "{head.splitlines()[0].decode(errors="replace") if head else "nothing"}". stderr: {r.stderr.decode(errors="replace")[-300:]}')
            return None
    if len(raw) > 6 * 1024 * 1024:
        warn(where, f"{len(raw) // 1024} KB; the host caps one response at 8 MiB. Aggregate in app.py.")
    is_csv = d.get("format") == "csv" or (not d.get("format") and path.lower().endswith(".csv"))
    text = raw.decode("utf-8-sig", errors="replace")
    if is_csv:
        return list(csv.DictReader(io.StringIO(text)))
    try:
        j = json.loads(text)
    except ValueError as e:
        err(where, f'"{url}" is not JSON ({e}); set "format": "csv" if it is CSV.')
        return None
    rows = j if isinstance(j, list) else j.get("rows") if isinstance(j, dict) else None
    if not isinstance(rows, list):
        err(where, 'JSON must be an array of row objects or {"rows": [...]}.')
        return None
    return rows


class Checker:
    def __init__(self, schema, spec, sources):
        self.S, self.spec, self.sources = schema, spec, sources
        self.measures = spec.get("measures") or {}
        self.custom = set()

    def source(self, p, where):
        name = p.get("source") or next(iter(self.sources), None)
        if name not in self.sources:
            err(where, f'no source "{name}".' + hint(name, self.sources))
            return None
        return self.sources[name]

    def field(self, src, f, where, want=None):
        if not isinstance(f, str) or not f:
            err(where, "needs a field name string.")
            return None
        if f not in src["fields"]:
            err(where, f'source "{src["name"]}" has no field "{f}".' + hint(f, src["fields"]))
            return None
        t = src["fields"][f]["type"]
        if want and t not in want:
            err(where, f'field "{f}" is {t}; this needs {" or ".join(want)}.')
        return src["fields"][f]

    def dim(self, src, d, where, kinds=("cat", "time", "part")):
        if not isinstance(d, str) or not d:
            err(where, 'a dimension is a field name string such as "team" or "date:week".')
            return None
        field, _, mod = d.partition(":")
        f = self.field(src, field, where)
        if not f:
            return None
        if f["type"] == "time":
            kind = "part" if mod in self.S["parts"] else "time"
            if mod and mod not in self.S["parts"] and mod not in self.S["grains"]:
                err(where, f'":{mod}" is not a grain or part. Use one of {", ".join(self.S["grains"] + self.S["parts"])}.')
        else:
            kind = "cat"
            if mod:
                err(where, f'"{field}" is {f["type"]}, not a time field, so ":{mod}" does not apply.')
        if kind not in kinds:
            err(where, f'"{d}" is a {"time" if kind == "time" else kind} dimension; this needs {" or ".join(kinds)}.')
        if kind == "cat" and f["type"] == "number" and f["distinct"] > 50:
            warn(where, f'"{field}" is a number with {f["distinct"]} distinct values; as a category it makes {f["distinct"]} bars. Did you mean a measure?')
        return {"field": field, "kind": kind, "distinct": f["distinct"] if kind == "cat" else None}

    def measure(self, src, name, where, seen=None):
        if not isinstance(name, str) or not name:
            err(where, 'a measure is a name from "measures" or an inline aggregate like "sum(cost_usd)".')
            return None
        m = self.measures.get(name)
        if m is None:
            mm = INLINE_RE.match(name.replace(" ", ""))
            if not mm:
                err(where, f'no measure "{name}".' + hint(name, self.measures) + " Inline form: sum(field), count(), count_distinct(field), avg(field), p95(field).")
                return None
            m = {"agg": mm.group(1), "field": mm.group(2) or None}
            self.check_measure_def(f"{where} ({name})", m, inline=True)
        seen = seen or set()
        if name in seen:
            err(where, f'measure "{name}" refers to itself through "ratio".')
            return None
        seen.add(name)
        if m.get("ratio"):
            for r in m["ratio"] if isinstance(m["ratio"], list) else []:
                self.measure(src, r, where + f' (ratio of "{name}")', seen)
            return m
        for key in ("field", "weight"):
            f = m.get(key)
            if f:
                info = self.field(src, f, f'{where} (measure "{name}".{key})')
                if info and key == "field" and m.get("agg") not in ("count", "count_distinct", "first", "last") and info["type"] != "number":
                    err(where, f'measure "{name}" takes {m.get("agg")} of "{f}", which is {info["type"]}, not a number. Use count_distinct or another field.')
        for f in (m.get("where") or {}):
            self.field(src, f, f'{where} (measure "{name}".where)')
        return m

    def check_measure_def(self, where, m, inline=False):
        if not isinstance(m, dict):
            err(where, "a measure is an object.")
            return
        allowed = {"label", "agg", "field", "weight", "where", "ratio", "format", "good", "thresholds", "description", "scale"}
        for k in m:
            if k not in allowed:
                err(where, f'unknown key "{k}".' + hint(k, allowed))
        if m.get("ratio") is not None:
            r = m["ratio"]
            if not (isinstance(r, list) and len(r) == 2 and all(isinstance(x, str) for x in r)):
                err(where, '"ratio" is ["numerator measure", "denominator measure"].')
            elif any(x not in self.measures for x in r):
                for x in r:
                    if x not in self.measures:
                        err(where, f'ratio names "{x}", which is not in "measures".' + hint(x, self.measures))
            if m.get("agg"):
                err(where, 'a ratio measure takes no "agg".')
        else:
            agg = m.get("agg")
            if agg not in self.S["aggs"]:
                err(where, f'agg "{agg}" is not supported.' + hint(agg, self.S["aggs"]))
            elif agg != "count" and not m.get("field"):
                err(where, f'agg "{agg}" needs "field".')
            if agg == "wavg" and not m.get("weight"):
                err(where, 'agg "wavg" needs "weight" (a numeric field such as requests).')
        if m.get("scale") is not None and (isinstance(m["scale"], bool) or not isinstance(m["scale"], (int, float))):
            err(where, '"scale" is a number the value is multiplied by (0.005 for 0.5% of requests).')
        if m.get("format") and m["format"] not in self.S["formats"]:
            err(where, f'format "{m["format"]}" is not supported.' + hint(m["format"], self.S["formats"]))
        if m.get("good") and m["good"] not in ("up", "down"):
            err(where, '"good" is "up" or "down".')
        th = m.get("thresholds")
        if th is not None:
            if not isinstance(th, dict) or any(k not in ("warning", "serious", "critical") for k in th):
                err(where, '"thresholds" is {"warning": n, "serious": n, "critical": n} (any subset).')
            elif not m.get("good"):
                warn(where, 'has thresholds but no "good"; "up" is assumed, so values BELOW a threshold are bad. Set "good": "down" for error rates and latency.')
        if m.get("format") == "pct" and not m.get("ratio") and m.get("agg") in ("sum", "count"):
            warn(where, 'format "pct" expects a 0-1 fraction; a sum or count is rarely one. Use a "ratio" measure.')

    def fmt_of(self, name):
        m = self.measures.get(name)
        if m is None:
            return "number"
        return m.get("format", "number")

    def check_type(self, where, kind, v, src, p):
        S = self.S
        if isinstance(kind, list):
            if v not in kind:
                err(where, f'"{v}" is not one of {", ".join(map(str, kind))}.')
            return
        if kind == "int" and not (isinstance(v, int) and not isinstance(v, bool) and v >= 0):
            err(where, "must be a whole number.")
        elif kind == "number" and not (isinstance(v, (int, float)) and not isinstance(v, bool)):
            err(where, "must be a number.")
        elif kind == "bool" and not isinstance(v, bool):
            err(where, "must be true or false.")
        elif kind in ("string",) and not isinstance(v, str):
            err(where, "must be a string.")
        elif kind == "icon" and v not in S["icons"]:
            err(where, f'icon "{v}" is not built in.' + hint(v, S["icons"]))
        elif kind == "measure" and src:
            self.measure(src, v, where)
        elif kind == "measures" and src:
            for i, n in enumerate(v if isinstance(v, list) else [v]):
                self.measure(src, n, f"{where}[{i}]" if isinstance(v, list) else where)
        elif kind == "dim" and src:
            self.dim(src, v, where)
        elif kind == "dims" and src:
            if not isinstance(v, list) or not v:
                err(where, "must be a list of dimension strings.")
            else:
                for i, d in enumerate(v):
                    self.dim(src, d, f"{where}[{i}]")
        elif kind == "field" and src:
            self.field(src, v, where)
        elif kind == "where" and src:
            if not isinstance(v, dict) or not v:
                err(where, 'must be an object such as {"event": "signup"}.')
            else:
                for f in v:
                    self.field(src, f, where)
        elif kind == "steps" and src:
            if not isinstance(v, list) or len(v) < 2:
                err(where, 'list at least two steps: [{"label": "Visit", "measure": "visitors"}, ...].')
            else:
                for i, st in enumerate(v):
                    if not isinstance(st, dict) or "measure" not in st:
                        err(f"{where}[{i}]", 'each step is {"label": "...", "measure": "..."}.')
                    else:
                        self.measure(src, st["measure"], f"{where}[{i}].measure")
        elif kind == "marks":
            if not isinstance(v, list) or any(not isinstance(x, dict) or not isinstance(x.get("y"), (int, float)) for x in v):
                err(where, 'is a list like [{"y": 0.999, "label": "SLO"}].')
        elif kind == "events":
            if not isinstance(v, list) or any(not isinstance(x, dict) or not isinstance(x.get("x"), str) for x in v):
                err(where, 'is a list like [{"x": "2026-10-17", "label": "Launch"}].')
        elif kind == "query" and src:
            if not isinstance(v, dict):
                err(where, 'is {"dims": [...], "measures": [...]}.')
            else:
                for i, d in enumerate(v.get("dims") or []):
                    self.dim(src, d, f"{where}.dims[{i}]")
                for i, n in enumerate(v.get("measures") or []):
                    self.measure(src, n, f"{where}.measures[{i}]")
        elif kind == "period":
            if v != "range" and v not in S["presets"]:
                err(where, f'"{v}" is not "range" or a preset ({", ".join(S["presets"])}).')
        elif kind == "budget" and src:
            if isinstance(v, bool) or not (isinstance(v, (int, float)) and v > 0 or isinstance(v, str)):
                err(where, "is a positive number (900) or the name of a measure (an error budget such as 0.5% of requests).")
            elif isinstance(v, str):
                self.measure(src, v, where)
        elif kind == "object" and not isinstance(v, dict):
            err(where, "must be an object.")

    def panel(self, i, p, ids):
        S = self.S
        where = f"panels[{i}]"
        if not isinstance(p, dict):
            err(where, "must be an object.")
            return
        pid = p.get("id")
        where = f'panels[{i}] (id "{pid}")' if pid else where
        if not pid:
            warn(where, 'has no "id"; give every panel a short id such as "spend-trend" (the agent reads and drives panels by id).')
        elif not ID_RE.match(str(pid)):
            err(where, "id must start with a letter and use letters, digits, ., _ or - (up to 64).")
        elif pid in ids:
            err(where, f'id "{pid}" is used twice.')
        ids.add(pid)
        t = p.get("type")
        if t in self.custom:
            warn(where, f'custom type "{t}" (static/*.js registerType): only id/title are checked; open it in a browser.')
            return
        if t not in S["types"]:
            err(where, f'type "{t}" is not a panel type.' + hint(t, S["types"]))
            return
        ts = S["types"][t]
        for k in p:
            if k not in S["common"] and k not in ts["opt"]:
                err(where, f'unknown key "{k}" for a {t} panel.' + hint(k, list(ts["opt"]) + list(S["common"])))
        for k in ts["req"]:
            if p.get(k) in (None, ""):
                err(where, f'a {t} panel needs "{k}".')
        if t != "section" and not p.get("title"):
            warn(where, 'has no "title".')
        w = p.get("w")
        if w is not None and not (isinstance(w, int) and 1 <= w <= 12):
            err(where, '"w" is a column span from 1 to 12.')
        hh = p.get("h")
        if hh is not None and not (isinstance(hh, int) and 0 <= hh <= 8):
            err(where, '"h" is a height in rows (1 row is about 110 px), 1 to 8.')
        if t == "section":
            return
        src = self.source(p, where)
        for k, kind in ts["opt"].items():
            if k in p and p[k] is not None:
                self.check_type(f"{where}.{k}", kind, p[k], src, p)
        if not src:
            return
        self.semantics(where, t, p, src)

    def semantics(self, where, t, p, src):
        def dinfo(d):
            if not isinstance(d, str):
                return None
            f = src["fields"].get(d.partition(":")[0])
            if not f:
                return None
            return {"kind": "time" if f["type"] == "time" and d.partition(":")[2] not in self.S["parts"] else "part" if f["type"] == "time" else "cat", "distinct": f["distinct"]}
        if t in ("line", "area", "bar"):
            ys = p.get("y") if isinstance(p.get("y"), list) else [p.get("y")]
            fmts = {self.fmt_of(y) for y in ys if isinstance(y, str)}
            if len(fmts) > 1:
                err(where, f'y mixes units ({", ".join(sorted(fmts))}). One chart has one axis: split it into two panels.')
            if len(ys) > 1 and p.get("series"):
                err(where, 'use either several "y" measures or "series", not both.')
            x = dinfo(p.get("x"))
            if t in ("line", "area") and x and x["kind"] == "cat":
                warn(where, f'a {t} over a category ("{p.get("x")}") implies an order between categories. Use "bar", or a time field for x.')
            if not src.get("timeField") and x and x["kind"] == "time":
                pass
            s = dinfo(p.get("series"))
            if s:
                if s["kind"] != "cat":
                    err(where + ".series", "must be a category field.")
                elif s["distinct"] > SERIES_COLORS and not p.get("limit"):
                    warn(where + ".series", f'"{p["series"]}" has {s["distinct"]} values; the top 5 stay and the rest fold into "Other". Set "limit" (max 8) or filter.')
            if p.get("highlight") is not None and not p.get("series"):
                warn(where, '"highlight" names a series value; it needs "series".')
            if t == "bar" and x and x["kind"] == "cat" and x["distinct"] > 30 and not p.get("drill"):
                warn(where, f'x "{p.get("x")}" has {x["distinct"]} values; the top {p.get("limit") or 12} stay and the rest fold into "Other". Consider a table.')
            if p.get("normalize") and t == "line":
                warn(where, '"normalize" fits area or bar (shares of a whole), not line.')
            if p.get("drill"):
                if not isinstance(p["drill"], list) or len(p["drill"]) < 2:
                    err(where + ".drill", 'list two or more category fields, top level first: ["team", "user"].')
                elif p["drill"][0] != p.get("x"):
                    warn(where + ".drill", f'the first drill level ("{p["drill"][0]}") should equal x ("{p.get("x")}").')
        if t == "donut":
            d = dinfo(p.get("dim"))
            if d and d["kind"] != "cat":
                err(where + ".dim", "a donut needs a category field.")
            if d and d["kind"] == "cat" and d["distinct"] > 6 and not p.get("limit"):
                warn(where, f'"{p["dim"]}" has {d["distinct"]} values; a donut shows 5 plus "Other". For comparing many parts use a horizontal bar.')
        if t == "waffle":
            d = dinfo(p.get("dim"))
            if d and d["kind"] != "cat":
                err(where + ".dim", "a waffle needs a category field.")
        if t == "scatter":
            c = dinfo(p.get("color"))
            if c and c["distinct"] > 3:
                err(where + ".color", f'"{p["color"]}" has {c["distinct"]} values; scatter colors stay readable up to 3. Drop "color" or filter.')
        if t == "kpi" and not src.get("timeField") and p.get("compare") == "previous":
            warn(where, 'compare "previous" needs a time field on this source; there is none, so no delta shows.')
        if t == "meter":
            m = self.measures.get(p.get("measure")) or {}
            if m.get("format") != "pct" and p.get("max") is None:
                warn(where, 'a meter of a non-percentage measure scales to the largest value; set "max" to the real limit.')
        if t == "funnel" and not p.get("cohort"):
            dflt = (self.spec.get("time") or {}).get("default") if isinstance(self.spec.get("time"), dict) else None
            if src.get("timeField") and p.get("period") != "all" and not (dflt == "all" and not p.get("period")):
                warn(where, 'counts each step\'s events inside the time window, so a short window under-counts early steps (users who visited before it). Set "cohort": "<user id field>" to follow the users who started in the window.')
            elif src.get("timeField"):
                warn(where, 'has no "cohort": once someone picks a short time range, each step counts only events inside it. Set "cohort": "<user id field>".')
        if t == "budget":
            m = self.measures.get(p.get("measure")) or {}
            if m and (m.get("ratio") or m.get("agg") not in ("sum", "count")):
                err(where + ".measure", "a budget adds use up; it needs a sum or count measure.")
            if not src.get("timeField"):
                err(where, "a budget panel needs a source with a time field.")
        if t == "flow":
            for i, d in enumerate(p.get("path") or []):
                dd = dinfo(d)
                if dd and dd["kind"] != "cat":
                    err(f"{where}.path[{i}]", f'"{d}" must be a category field.')
        if t in ("line", "area") and p.get("cumulative"):
            for y in (p.get("y") if isinstance(p.get("y"), list) else [p.get("y")]):
                m = self.measures.get(y) if isinstance(y, str) else None
                if m and (m.get("ratio") or m.get("agg") not in ("sum", "count")):
                    err(where + ".cumulative", f'adds values up; "{y}" is not a sum or count measure.')
        if t == "retention":
            f = src["fields"].get(p.get("time"))
            if f and f["type"] != "time":
                err(where + ".time", f'"{p["time"]}" is {f["type"]}, not a date/time field.')
        if t == "heatmap":
            for k in ("x", "y"):
                d = dinfo(p.get(k))
                if d and d["kind"] == "cat" and d["distinct"] > 40:
                    warn(f"{where}.{k}", f'"{p[k]}" has {d["distinct"]} values; only the top {30 if k == "x" else 20} show.')
        if t == "table":
            ms = p.get("measures") if isinstance(p.get("measures"), list) else [p.get("measures")]
            dims = p.get("dims") or []
            if p.get("sort") and p["sort"] not in ms and p["sort"] not in dims and p["sort"] not in [self.measures.get(m, {}).get("label") for m in ms if isinstance(m, str)]:
                err(where + ".sort", f'"{p["sort"]}" is not one of this table\'s dims or measures.')
        if t in ("kpi", "callout"):
            m = self.measures.get(p.get("measure")) or {}
            if m.get("format") == "pct" and m.get("agg") in ("sum",):
                warn(where, "a pct KPI from a sum is unusual; use a ratio measure.")

    def layout(self, panels):
        """Rows fill left to right in spec order; a section starts a new row. Gaps and mixed heights look broken."""
        S = self.S
        rows, cur = [], []
        for p in panels:
            if not isinstance(p, dict):
                continue
            t = S["types"].get(p.get("type")) or {"w": 6, "h": 3}
            if p.get("type") == "section":
                if cur:
                    rows.append(cur)
                cur = []
                continue
            w = p.get("w") or t["w"]
            hh = p.get("h") if p.get("h") is not None else t["h"]
            if not isinstance(w, int) or not isinstance(hh, int):
                continue
            if sum(x[1] for x in cur) + w > 12:
                rows.append(cur)
                cur = []
            cur.append((p.get("id"), w, hh))
        if cur:
            rows.append(cur)
        for i, r in enumerate(rows):
            total = sum(x[1] for x in r)
            ids = ", ".join(str(x[0]) for x in r)
            if total < 12 and i < len(rows) - 1:
                warn("layout", f"the row with {ids} fills {total} of 12 columns, so a gap shows. Make the widths in each row add up to 12.")
            elif total < 12 and len(r) > 1:
                warn("layout", f"the last row ({ids}) fills {total} of 12 columns. Widen a panel so the row adds up to 12.")
            hs = {x[2] for x in r}
            if len(hs) > 1:
                warn("layout", f"the row with {ids} mixes heights {sorted(hs)}; shorter panels stretch to the tallest. Give panels in a row the same \"h\".")

    def run(self):
        S, spec = self.S, self.spec
        for k in spec:
            if k not in S["top"]:
                err("top level", f'unknown key "{k}".' + hint(k, S["top"]))
        for name, m in self.measures.items():
            if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
                err(f"measures.{name}", "name uses letters, digits and _ only.")
            self.check_measure_def(f"measures.{name}", m)
        time = spec.get("time")
        if time is not None:
            if not isinstance(time, dict):
                err("time", 'is {"field": "date", "default": "30d"}.')
            else:
                for k in time:
                    if k not in ("field", "default", "presets", "grain", "anchor"):
                        err("time", f'unknown key "{k}".' + hint(k, ["field", "default", "presets", "grain", "anchor"]))
                tf = time.get("field")
                if tf and not any(s.get("timeField") for s in self.sources.values()):
                    any_src = next(iter(self.sources.values()), None)
                    if any_src:
                        f = any_src["fields"].get(tf)
                        err("time.field", f'"{tf}" is not a date/time column in any source.' + ("" if f else hint(tf, any_src["fields"])) + (f' It is {f["type"]}; dates must look like 2026-11-14 or 2026-11-14T09:30:00Z.' if f else ""))
                for k in ("default",):
                    v = time.get(k)
                    if v is not None and v not in S["presets"]:
                        err(f"time.{k}", f'"{v}" is not a preset ({", ".join(S["presets"])}).')
                for v in time.get("presets") or []:
                    if v not in S["presets"]:
                        err("time.presets", f'"{v}" is not a preset ({", ".join(S["presets"])}).')
                if time.get("grain") and time["grain"] not in S["grains"] + ["auto"]:
                    err("time.grain", f'"{time["grain"]}" is not a grain.' + hint(time["grain"], S["grains"]))
        for i, f in enumerate(spec.get("filters") or []):
            field = f if isinstance(f, str) else f.get("field") if isinstance(f, dict) else None
            srcs = [s for s in self.sources.values() if field in s["fields"]]
            if not srcs:
                any_src = next(iter(self.sources.values()), {"fields": {}})
                err(f"filters[{i}]", f'no source has field "{field}".' + hint(field, any_src["fields"]))
            elif srcs[0]["fields"][field]["type"] != "string":
                warn(f"filters[{i}]", f'"{field}" is {srcs[0]["fields"][field]["type"]}; filter menus list every distinct value.')
            elif srcs[0]["fields"][field]["distinct"] > 200:
                warn(f"filters[{i}]", f'"{field}" has {srcs[0]["fields"][field]["distinct"]} values; the menu shows a search box and the first 200.')
        panels = spec.get("panels")
        if not isinstance(panels, list) or not panels:
            err("panels", "list at least one panel.")
            return
        ids = set()
        for i, p in enumerate(panels):
            self.panel(i, p, ids)
        n = sum(1 for p in panels if isinstance(p, dict) and p.get("type") != "section")
        if n > 28:
            warn("panels", f"{n} panels; the agent gets a data detail for the first 28 only. Split into two dashboards.")
        if n > 20:
            warn("panels", f"{n} panels (sections not counted) is a lot for one page; 8-16 is typical. Cut the ones no question needs.")
        self.layout(panels)
        kpis = [p for p in panels if isinstance(p, dict) and p.get("type") == "kpi"]
        if len(kpis) > 6:
            warn("panels", f"{len(kpis)} KPI tiles; keep the headline row to 3-6.")


def to_ms(v):
    s = str(v).strip()
    if not TIME_RE.match(s):
        return None
    s = s.replace(" ", "T")
    if len(s) == 10:
        s += "T00:00:00"
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return int(d.timestamp() * 1000)


DAY = 86400000


def period_start(ms, months):
    d = datetime.fromtimestamp(ms / 1000, timezone.utc)
    m0 = (d.month - 1) // months * months
    return int(datetime(d.year, m0 + 1, 1, tzinfo=timezone.utc).timestamp() * 1000)


def where_pass(w, r):
    for k, want in (w or {}).items():
        v = r.get(k)
        if isinstance(want, list):
            if str(v) not in [str(x) for x in want]:
                return False
        elif isinstance(want, dict):
            dated = any(isinstance(want.get(op), str) and TIME_RE.match(want[op]) for op in ("gt", "gte", "lt", "lte"))
            if dated:
                fv = to_ms(v) if v not in (None, "") else None
            else:
                try:
                    fv = float(v)
                except (TypeError, ValueError):
                    fv = None
            for op, fn in (("gt", lambda a, b: a > b), ("gte", lambda a, b: a >= b), ("lt", lambda a, b: a < b), ("lte", lambda a, b: a <= b)):
                if want.get(op) is not None and (fv is None or not fn(fv, to_ms(want[op]) if dated else float(want[op]))):
                    return False
            if want.get("ne") is not None and str(v) == str(want["ne"]):
                return False
        elif str(v) != str(want):
            return False
    return True


def default_view(spec, sources, rawrows, schema):
    """Count each panel's rows under the default time range, as the browser first shows it."""
    time = spec.get("time") if isinstance(spec.get("time"), dict) else {}
    lo = hi = step = None
    for name, src in sources.items():
        tf = src.get("timeField")
        if not tf:
            continue
        ts = sorted({t for t in (to_ms(r.get(tf)) for r in rawrows[name]) if t is not None})
        if ts:
            lo = ts[0] if lo is None else min(lo, ts[0])
            hi = ts[-1] if hi is None else max(hi, ts[-1])
            diffs = [b - a for a, b in zip(ts[:5000], ts[1:5001]) if b > a]
            if diffs:
                step = min(diffs) if step is None else min(step, min(diffs))
    if hi is None:
        return
    anchor = hi + (step or DAY)
    measures = spec.get("measures") or {}

    def rng(sel):
        if sel == "all":
            return lo, anchor
        if sel in ("mtd", "qtd", "ytd"):
            return period_start(anchor - 1, {"mtd": 1, "qtd": 3, "ytd": 12}[sel]), anchor
        m = re.match(r"^(\d+)([hdy])$", sel or "30d")
        if not m:
            return lo, anchor
        n, u = int(m.group(1)), m.group(2)
        return anchor - n * {"h": 3600000, "d": DAY, "y": 365 * DAY}[u], anchor

    dflt = time.get("default") or "30d"
    before = len(errors) + len(warnings)
    for i, p in enumerate(spec.get("panels") or []):
        if not isinstance(p, dict) or p.get("type") in ("section", "text") or p.get("type") not in schema["types"]:
            continue
        name = p.get("source") or next(iter(sources), None)
        src = sources.get(name)
        if not src:
            continue
        tf = src.get("timeField")
        sel = p.get("period") if p.get("period") not in (None, "range") else dflt
        a, b = rng(sel)
        rows = [r for r in rawrows[name] if where_pass(p.get("where"), r)]
        if tf and p.get("type") != "retention" and not p.get("cohort"):
            rows = [r for r in rows if (lambda t: t is not None and a <= t < b)(to_ms(r.get(tf)))]
        where = f'panels[{i}] (id "{p.get("id")}")'
        when = f"the default view ({sel})" if tf else "the default view"
        if not rows:
            err(where, f'has no rows in {when}. Pick another time default, a panel "period", or fix "where".')
            continue
        names = []
        for k in ("measure", "y", "measures", "size", "x"):
            v = p.get(k)
            names += [v] if isinstance(v, str) else [x for x in v if isinstance(x, str)] if isinstance(v, list) else []
        names += [st.get("measure") for st in p.get("steps") or [] if isinstance(st, dict)]
        for n in names:
            m = measures.get(n)
            if isinstance(m, dict) and m.get("where") and not any(where_pass(m["where"], r) for r in rows):
                warn(where, f'measure "{n}" (where {json.dumps(m["where"])}) matches no rows in {when}, so it shows 0 or blank.')
    if len(errors) + len(warnings) == before:
        print(f"default view ({dflt}): every panel has rows and every filtered measure matches some.")


def profile(name, rows, fields):
    print(f"source {name}: {len(rows)} rows")
    for f, info in fields.items():
        extra = ""
        blank = info.get("blank", 0)
        if info["type"] == "string":
            extra = f'{info["distinct"]} distinct, e.g. ' + ", ".join(map(repr, info["examples"]))
        elif info["type"] in ("number", "time"):
            extra = f'{info.get("min")} .. {info.get("max")}' + (f', {info["distinct"]} distinct' if info["type"] == "number" else "")
        if blank:
            extra += f"; {blank} blank (skipped by every agg except count())"
        print(f"  {f:24} {info['type']:7} {extra}")


def main(argv):
    show = "--profile" in argv
    argv = [a for a in argv if a != "--profile"]
    if argv and argv[0] == "--profile-csv":
        for path in argv[1:]:
            rows = list(csv.DictReader(open(path, encoding="utf-8-sig")))
            profile(path, rows, infer(rows))
        return 0
    spec_path = argv[0] if argv else "dashboard.json"
    app_dir = os.path.dirname(os.path.abspath(spec_path))
    schema = load_schema(app_dir)
    try:
        spec = json.load(open(spec_path, encoding="utf-8"))
    except ValueError as e:
        print(f"ERROR   {spec_path}: not valid JSON: {e}")
        return 1
    except OSError as e:
        print(f"ERROR   {spec_path}: {e}")
        return 1
    if not isinstance(spec, dict):
        print("ERROR   top level: the spec is a JSON object.")
        return 1
    sources, rawrows = {}, {}
    srcdefs = spec.get("sources")
    if not isinstance(srcdefs, dict) or not srcdefs:
        err("sources", 'name at least one source: {"usage": {"url": "data/usage.csv"}}.')
        srcdefs = {}
    tf = (spec.get("time") or {}).get("field") if isinstance(spec.get("time"), dict) else None
    for name, d in srcdefs.items():
        rows = load_rows(app_dir, name, d)
        if rows is None:
            continue
        if len(rows) > MAX_ROWS:
            err(f"sources.{name}", f"{len(rows)} rows; the browser limit is {MAX_ROWS}. Aggregate in app.py first.")
        elif len(rows) > 100000:
            warn(f"sources.{name}", f"{len(rows)} rows; every filter change re-aggregates in the browser. Pre-aggregate to the grain you chart.")
        if not rows:
            warn(f"sources.{name}", "has no rows.")
        allowed = {"url", "rows", "csv", "format", "types", "timeField"}
        for k in d if isinstance(d, dict) else []:
            if k not in allowed:
                err(f"sources.{name}", f'unknown key "{k}".' + hint(k, allowed))
        fields = infer(rows, (d or {}).get("types"))
        tfield = (d or {}).get("timeField") or (tf if tf in fields else None)
        if tfield and fields.get(tfield, {}).get("type") != "time":
            if tfield in fields:
                err(f"sources.{name}", f'time field "{tfield}" is {fields[tfield]["type"]}; dates must look like 2026-11-14 or 2026-11-14T09:30:00Z.')
            else:
                err(f"sources.{name}.timeField", f'no field "{tfield}".' + hint(tfield, fields))
            tfield = None
        sources[name] = {"name": name, "fields": fields, "timeField": tfield, "rows": len(rows)}
        rawrows[name] = rows
        if show:
            profile(name, rows, fields)
    ck = Checker(schema, spec, sources)
    static = os.path.join(app_dir, "static")
    for fn in os.listdir(static) if os.path.isdir(static) else []:
        if fn.endswith(".js") and fn not in ("awb-dash.js", "echarts.min.js"):
            ck.custom |= set(re.findall(r"registerType\(\s*['\"]([A-Za-z0-9_-]+)['\"]", open(os.path.join(static, fn), encoding="utf-8", errors="replace").read()))
    ck.run()
    if not errors:
        default_view(spec, sources, rawrows, schema)
    for line in errors + warnings:
        print(line)
    n = len([p for p in spec.get("panels") or [] if isinstance(p, dict)])
    print(f"{'FAIL' if errors else 'OK'}: {len(errors)} error(s), {len(warnings)} warning(s), {n} panel(s), {len(sources)} source(s).")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
