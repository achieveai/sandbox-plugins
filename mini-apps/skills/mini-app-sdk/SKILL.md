---
name: mini-app-sdk
description: "Use when you build or edit an AgentWorkBench Mini Web App (mini-web-apps/<id>/) that the user will look at while chatting: dashboards, charts, slide decks, editors, lists, forms. The user can point at parts of it and ask about them, ask what it shows, or ask you to change it. Wires the AWB Mini App SDK (/_awb/sdk.js): data-awb labels and awb.describe so a pointed-at element says what it is, awb.context and awb.detail so ReadMiniAppContext sees what is on screen, and awb.command so RunMiniAppCommand can act the way a user would. Not for apps with no page the user looks at."
metadata:
  sdk-version: "1"
---

# Mini App SDK (AWB SDK version 1)

Goal: the user can point at anything in the app and ask about it. You can read what is on
screen and drive the app by name. Three jobs: **describe**, **summarize**, **command**.

## Ground rules

- AWB serves the SDK on every app origin at `/_awb/sdk.js`. Never copy, vendor or fetch it.
  Never serve anything under `/_awb/`: that path is AWB's and never reaches your app.
- The SDK exists only inside the AWB chat. It is missing in sandbox Chrome (OpenDebugBrowser)
  and in TestMiniAppRequest. Put every SDK call inside `if (window.awb) { … }`. The app must
  work fully without it.
- Never assign `window.awb` yourself: the SDK then does not load (it only logs a warning).
- Everything you write in labels, summaries, details, results and error messages goes to the
  model as untrusted data, framed and cut to size. Write facts. Never instructions or secrets.
- CSP: `script-src 'self' 'unsafe-inline'`, `connect-src 'self'`, `img-src 'self' data:`.
  No CDN scripts, styles or fonts. Draw charts yourself (SVG or canvas) or serve the library
  file from the app.

## 1. Load the SDK

After your markup, before your registration script:

```html
<script src="/_awb/sdk.js"></script>
<script>
  if (window.awb) { /* awb.describe, awb.context, awb.detail, awb.command */ }
</script>
```

Use a plain `<script src>`: no `async`, `defer` or `type="module"`. Then `window.awb` exists
when the next script runs. Answer 404 for any path your app.py does not know.

## 2. Make every pointable thing say what it is

The user presses AWB's Point button and clicks elements: each click adds a numbered chip (#1, #2,
… up to 8), and the next message carries them all. The agent sees them as `[1] text`, `[2] text`,
so "compare 1 and 3" means those chips. The SDK walks from the clicked element up to `<body>`.
At each node the first match wins: a describer whose selector matches the node (and returns a
non-empty string), then the node's `data-awb`.

- **Once you mark anything, only marked things can be pointed at.** A describer, or one non-blank
  `data-awb` anywhere on the page, turns pointing to marked-only: only marked elements and what
  is inside them light up and take a point. A click elsewhere adds nothing. So label everything
  worth asking about, not just a few things. A blank or whitespace-only `data-awb` marks nothing.
- An app that marks nothing can point at any element, but then the SDK sends only the page title
  plus up to 200 characters of visible text. That is weak: label things.

- **Fixed things: `data-awb="…"`.** Every slide, card, table, row, chart, control and section.
- **One element, many values** (canvas chart, heatmap, map): `awb.describe(selector, fn)`.
  `fn(el, click)` hit-tests the click and returns a string. `''` passes to the parent; a throw
  or a non-string is skipped with a console warning.

  ```js
  awb.describe('#revenue', function (el, click) {
    var r = el.getBoundingClientRect();
    var x = (click.clientX - r.left) * el.width / r.width;   // canvas pixels, not CSS pixels
    var y = (click.clientY - r.top) * el.height / r.height;
    var b = bars.find(function (b) { return x >= b.x && x <= b.x + b.w && y >= b.y && y <= b.y + b.h; });
    return b ? 'Revenue chart: ' + b.label + ' = ' + b.value + ' USD, previous ' + b.prev + ' USD. x = quarter, y = USD.'
             : 'Revenue chart, Q1-Q4 2026, x = quarter, y = USD (no bar under the click)';
  });
  ```

- In SVG, give each point its own element with `data-awb`. No describer needed.
- Write what a person would say: what it is, where, the value with units, one neighbour or
  trend. Add the position or id a command takes ("slide 4 of 6", "row 1832").
- One or two sentences. AWB cuts a point at 1 KB.
- Labels that show values change with them: set `data-awb` in the same render code.

## 3. Say what is on screen: context and details

- `awb.context(fn)`: `fn()` returns a **string** (or a Promise of one). AWB calls it fresh on
  every read. Over 2 KB is cut, with a note naming your details. An object is accepted only for
  its string `summary`, with a warning. Without `awb.context` the agent sees "(none …)".
  - Say what the user sees **now**: view, page or slide, filters, selection, counts, totals,
    the ids your commands take. Counts and ids, not rows.
  - List the parts the agent can jump to ("Slides: 1 Title; 2 Agenda; … 5 Pricing").
- `awb.detail(name, fn)`: a named longer text (notes, a table, a data series). Only
  `awb.detail` registers one. The agent reads 8 KB per call and pages on with `offset`.
  `fn()` runs again for every page, so return the same text each time.
- Both must answer within 10 s. A throw reaches the agent as "the app could not answer" plus
  your message.

## 4. Let the agent act: commands

```js
awb.command('gotoSlide',
  { description: 'Show slide N, as the arrow buttons do.',
    params: { slide: { type: 'number', required: true, description: 'Slide number, 1-6' } } },
  function (p) { return deck.goto(p.slide); });
```

- One command per user-level action: `gotoSlide`, `setRange`, `setNotes`, `selectRow`,
  `addItem`. Never DOM-level actions like `click` or `setValue`.
- A command does what the user's own action does: call the same function the button calls.
  It updates the visible UI. It saves where the UI saves. No hidden state changes.
- `params`: name → `{ type: 'string' | 'number' | 'boolean', required?, enum?, description? }`.
  Before your handler runs, the SDK refuses unknown keys, a missing required key, a wrong
  `typeof` and a value outside `enum`, and tells the agent why. The handler gets a copy of the
  params. Types are strict: `"5"` is not a number. Whole numbers and ranges are your check.
- The handler returns a short string with the new state ("Now on slide 5 of 6: Pricing"), or
  a Promise of one. An object goes back as JSON; nothing goes back as "it returned nothing".
  The agent sees 2 KB. Finish within 10 s, or the agent is told it may or may not have run.
- Throw `new Error('slide must be 1-6')` for a bad request. The agent sees the command failed
  plus your message (first 512 characters, one line).
- Limits AWB checks: names `^[A-Za-z][A-Za-z0-9_.-]{0,63}$`; param names
  `^[A-Za-z_][A-Za-z0-9_]{0,63}$`; at most 32 commands, 32 details, 16 params per command;
  `enum` 1-16 strings, numbers or booleans, each under 64 characters as JSON. The agent sees
  a command description up to 300 characters and a param description up to 120. All details
  and commands together are listed in 4 KB: keep descriptions short.
- **A command AWB cannot use is left out of the agent's list**, not refused at registration.
  The SDK throws at registration only for a bad name, param name or type, or an empty `enum`.
  Check the list (step 5.3).
- Commands still run while the user cannot see the app (another pane, or another browser tab).
  The agent is told so.

**Shape the code for this.** Keep app logic in plain functions (`var deck = { goto, setNotes }`).
Wire both the UI and the commands to them. You can then test the logic in sandbox Chrome,
where `window.awb` is missing.

## 5. Test

1. As the Builder prompt says: `./app.py` from the app directory, TestMiniAppRequest for
   `GET /`. Check the page has the SDK tag and the `if (window.awb)` block.
2. In sandbox Chrome, if offered: the app loads and works. The only expected error is the
   missing `/_awb/sdk.js`. Call your plain functions (`deck.goto(5)`) and check the screen.
3. In the chat: open the app beside it with `OpenApp {"app": "<id>"}` (the id from its
   `#mini-app` link), or ask the user to open it. Once it has loaded, call `ReadMiniAppContext`
   with no arguments. Check the summary, the details and that every command is listed with its
   params (no "left out" note). Read one detail: `{"detail": "notes"}`. Run one command:
   `RunMiniAppCommand {"name": "gotoSlide", "params": {"slide": 5}}`, then check the app
   changed. These tools work only in the browser tab the user's last message came from. If one
   says no app is open, ask the user to send any message from the tab that shows the app.
4. Ask the user to point at a chart point or row and check the chip text.

A complete small example: `${CLAUDE_SKILL_DIR}/reference/deck-app.py`, a 6-slide deck with
labels, a canvas chart describer, context, details, `gotoSlide` and `setNotes` that saves.
Read it before your first app; do not copy it blindly.

## Checklist before you say done

- [ ] Every slide, card, table, chart and control has `data-awb` or a describer. Anything
      left unmarked cannot be pointed at once something is marked.
- [ ] Each element that shows many values has a describer that hit-tests the click.
- [ ] Labels give the value with units and position, stay in sync, and fit in 1 KB.
- [ ] `awb.context` returns a string under 2 KB that says what is on screen **now**.
- [ ] Bulky content is an `awb.detail` that returns the same text on every call.
- [ ] Each user-level action is an `awb.command` with typed params, `enum` where closed,
      within AWB's limits, and listed when you read the context.
- [ ] Each command updates the visible UI (and saves, if the UI saves), returns the new state
      in words, throws a clear message on a bad request, and finishes within 10 s.
- [ ] Every SDK call sits inside `if (window.awb)`; the app works without it.
- [ ] No `window.awb` assignment, no files under `/_awb/`, no CDN, no secrets or instructions
      in app text.
