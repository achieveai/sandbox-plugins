#!/usr/bin/env python3
# Mini App SDK example (AWB SDK version 1): a slide deck the user can point at and the agent can drive.
# Manifest: {"id":"deck","name":"Q3 Business Review","entry":"app.py","runtime":"python3"}; chmod +x app.py.
# Shows: data-awb labels, a canvas chart describer (hit-test), context, details, gotoSlide, setNotes (saved).
import json
import os
import sys

STATE = os.environ.get("STATE_DIR") or os.getcwd()
NOTES_FILE = os.path.join(STATE, "notes.json")
DEFAULT_NOTES = {
    "1": "Welcome. This review covers Q1-Q3 and the plan for Q4.",
    "4": "Q3 fell to 41k because EMEA renewals paused after the June price change. "
         "We expect most of them to renew in Q4, which is why Q4 is forecast at 57k.",
    "5": "Three tiers. Team is the one most customers pick.",
}


def load_notes():
    try:
        with open(NOTES_FILE, encoding="utf-8") as f:
            return {**DEFAULT_NOTES, **json.load(f)}
    except (OSError, ValueError):
        return dict(DEFAULT_NOTES)


def reply(status, content_type, body):
    out = sys.stdout.buffer
    out.write(f"Status: {status}\nContent-Type: {content_type}\n\n".encode())
    out.write(body.encode("utf-8"))
    out.flush()


def script_json(value):
    # JSON inside <script>: "</" would end the script element.
    return json.dumps(value).replace("</", "<\\/")


PAGE = r'''<!doctype html>
<html><head><meta charset="utf-8"><title>Q3 2026 Business Review</title>
<style>
body{font:16px system-ui,sans-serif;margin:0;background:#f4f4f6;color:#222}
header{display:flex;gap:8px;align-items:center;padding:8px 12px;background:#fff;border-bottom:1px solid #ddd}
.slide{display:none;margin:16px auto;max-width:720px;min-height:340px;background:#fff;border:1px solid #ccc;border-radius:8px;padding:24px}
.slide.on{display:block}
#notes-box{max-width:720px;margin:0 auto 16px}
textarea{width:100%;box-sizing:border-box;height:90px;font:inherit}
</style></head><body>
<header>
  <button id="prev" data-awb="Button: previous slide">&larr;</button>
  <span id="pos" data-awb="Slide counter"></span>
  <button id="next" data-awb="Button: next slide">&rarr;</button>
</header>
<main>
<section class="slide" data-title="Title" data-awb="Slide 1 of 6: title, Q3 2026 Business Review">
  <h1>Q3 2026 Business Review</h1><p>Sales team, October 2026</p></section>
<section class="slide" data-title="Agenda" data-awb="Slide 2 of 6: agenda">
  <h2>Agenda</h2><ol><li>Highlights</li><li>Revenue</li><li>Pricing</li><li>Next steps</li></ol></section>
<section class="slide" data-title="Highlights" data-awb="Slide 3 of 6: highlights">
  <h2>Highlights</h2><ul>
  <li data-awb="Highlight: 212 new customers in Q1-Q3 2026">212 new customers</li>
  <li data-awb="Highlight: churn 3.1 percent, down from 4.0">Churn 3.1%</li></ul></section>
<section class="slide" data-title="Revenue by quarter" data-awb="Slide 4 of 6: revenue by quarter, bar chart Q1-Q4 2026">
  <h2>Revenue by quarter</h2><canvas id="revenue" width="600" height="260"></canvas>
  <p data-awb="Footnote: Q4 2026 is a forecast">Q4 is a forecast.</p></section>
<section class="slide" data-title="Pricing" data-awb="Slide 5 of 6: pricing, three tiers">
  <h2>Pricing</h2><ul>
  <li data-awb="Pricing tier: Starter, 9 USD per user per month">Starter: $9</li>
  <li data-awb="Pricing tier: Team, 19 USD per user per month, most chosen">Team: $19</li>
  <li data-awb="Pricing tier: Enterprise, custom price">Enterprise: custom</li></ul></section>
<section class="slide" data-title="Next steps" data-awb="Slide 6 of 6: next steps">
  <h2>Next steps</h2><ul><li>Win back paused EMEA renewals</li><li>Launch annual Team plan</li></ul></section>
</main>
<div id="notes-box"><label>Speaker notes<textarea id="notes"></textarea></label></div>
<script>
var CSRF = __CSRF__, NOTES = __NOTES__;
var QUARTERS = ['Q1', 'Q2', 'Q3', 'Q4'], REVENUE = [48000, 52000, 41000, 57000], bars = [];
var slides = Array.prototype.slice.call(document.querySelectorAll('.slide'));
function usd(n) { return n.toLocaleString('en-US') + ' USD'; }

// App logic in plain functions: the UI and the agent's commands call the same code.
var deck = {
  current: 1,
  count: slides.length,
  title: function (n) { return slides[n - 1].dataset.title; },
  goto: function (n) {
    if (!(Number.isInteger(n) && n >= 1 && n <= deck.count)) throw new Error('slide must be 1-' + deck.count);
    deck.current = n;
    slides.forEach(function (s, i) { s.classList.toggle('on', i === n - 1); });
    document.getElementById('pos').textContent = n + ' / ' + deck.count;
    var notes = document.getElementById('notes');
    notes.value = NOTES[n] || '';
    notes.setAttribute('data-awb', 'Speaker notes for slide ' + n + ' (' + deck.title(n) + ')');  // label kept in sync
    return 'Now on slide ' + n + ' of ' + deck.count + ': ' + deck.title(n);
  },
  setNotes: function (n, text) {
    deck.goto(n);                       // show the slide being changed, as a user editing it would see
    NOTES[n] = text;
    document.getElementById('notes').value = text;
    return fetch('notes', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': CSRF },
      body: JSON.stringify({ slide: n, text: text }) })
      .then(function (r) {
        if (!r.ok) throw new Error('saving failed: HTTP ' + r.status);
        return 'Notes of slide ' + n + ' (' + deck.title(n) + ') replaced and saved, ' + text.length + ' chars.';
      });
  },
};

function drawChart() {
  var c = document.getElementById('revenue'), g = c.getContext('2d'), pad = 30, w = (c.width - pad * 2) / 4;
  g.clearRect(0, 0, c.width, c.height); bars = [];
  for (var i = 0; i < 4; i++) {
    var h = REVENUE[i] / 70000 * (c.height - pad * 2), x = pad + i * w + 8, y = c.height - pad - h;
    g.fillStyle = i === 3 ? '#9bb7d4' : '#2f6fb0'; g.fillRect(x, y, w - 16, h);
    g.fillStyle = '#222'; g.fillText(QUARTERS[i] + '  ' + REVENUE[i] / 1000 + 'k', x + 4, c.height - pad + 14);
    bars.push({ x: x, y: y, w: w - 16, h: h, i: i });
  }
}

function describeChart(el, click) {
  var r = el.getBoundingClientRect();
  var x = (click.clientX - r.left) * el.width / r.width, y = (click.clientY - r.top) * el.height / r.height;
  var b = bars.find(function (b) { return x >= b.x && x <= b.x + b.w && y >= b.y && y <= b.y + b.h; });
  if (!b) return 'Slide 4 revenue chart, Q1-Q4 2026, x = quarter, y = revenue in USD (no bar under the click)';
  var i = b.i, prev = i > 0 ? ', ' + Math.round((REVENUE[i] / REVENUE[i - 1] - 1) * 100) + '% vs ' +
    QUARTERS[i - 1] + ' (' + usd(REVENUE[i - 1]) + ')' : '';
  return 'Slide 4 revenue chart point: ' + QUARTERS[i] + ' 2026 = ' + usd(REVENUE[i]) + prev +
    (i === 3 ? ', forecast' : '') + '. x = quarter, y = revenue in USD.';
}

function summary() {
  var n = deck.current;
  return 'Slide deck "Q3 2026 Business Review", ' + deck.count + ' slides. Showing slide ' + n + ': ' + deck.title(n) +
    '. Slides: ' + slides.map(function (s, i) { return (i + 1) + ' ' + s.dataset.title; }).join('; ') +
    '. Speaker notes on this slide: ' + (NOTES[n] ? NOTES[n].length + ' chars (detail "notes")' : 'none') + '.';
}

document.getElementById('prev').onclick = function () { if (deck.current > 1) deck.goto(deck.current - 1); };
document.getElementById('next').onclick = function () { if (deck.current < deck.count) deck.goto(deck.current + 1); };
document.getElementById('notes').onchange = function (e) {
  deck.setNotes(deck.current, e.target.value).catch(function (err) { console.warn(err); });
};
drawChart();
deck.goto(1);
</script>
<script src="/_awb/sdk.js"></script>
<script>
if (window.awb) {
  awb.describe('#revenue', describeChart);
  awb.context(summary);
  awb.detail('notes', function () { return NOTES[deck.current] || '(no notes on this slide)'; });
  awb.detail('slide-text', function () { return slides[deck.current - 1].innerText; });
  awb.detail('revenue-data', function () {
    return QUARTERS.map(function (q, i) { return q + ' 2026: ' + usd(REVENUE[i]) + (i === 3 ? ' (forecast)' : ''); }).join('\n');
  });
  awb.command('gotoSlide', { description: 'Show slide N, as the arrow buttons do.',
    params: { slide: { type: 'number', required: true, description: 'Slide number, 1-6' } } },
    function (p) { return deck.goto(p.slide); });
  awb.command('setNotes', { description: 'Replace the speaker notes of slide N and save them. Shows that slide.',
    params: { slide: { type: 'number', required: true, description: 'Slide number, 1-6' },
              text: { type: 'string', required: true, description: 'The full new notes' } } },
    function (p) { return deck.setNotes(p.slide, p.text); });
}
</script>
</body></html>'''

method = os.environ.get("REQUEST_METHOD", "GET")
path = os.environ.get("PATH_INFO") or "/"
if method == "GET" and path == "/":
    page = PAGE.replace("__CSRF__", script_json(os.environ.get("SANDBOX_APP_CSRF_TOKEN", "")))
    reply("200 OK", "text/html; charset=utf-8", page.replace("__NOTES__", script_json(load_notes())))
elif method == "POST" and path == "/notes":
    # The host already checked the CSRF token; do not check it again.
    try:
        data = json.loads(sys.stdin.buffer.read(int(os.environ.get("CONTENT_LENGTH") or 0)) or b"{}")
        slide, text = int(data["slide"]), str(data["text"])
        if not 1 <= slide <= 6:
            raise ValueError("slide must be 1-6")
    except (ValueError, KeyError, TypeError) as err:
        reply("400 Bad Request", "application/json", json.dumps({"error": str(err)}))
        raise SystemExit
    notes = load_notes()
    notes[str(slide)] = text
    tmp = NOTES_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(notes, f)
    os.replace(tmp, NOTES_FILE)
    reply("200 OK", "application/json", json.dumps({"ok": True}))
else:
    reply("404 Not Found", "text/plain", "no such page")
