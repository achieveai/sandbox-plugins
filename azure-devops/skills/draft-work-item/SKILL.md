---
name: draft-work-item
description: >
  Turns a rough requirement into a well-structured Azure DevOps work item and
  posts it. Classifies the intent (feature, bug, or task), drafts the item at
  the depth the type warrants — deep research and blind-spot scanning for
  features/bugs, a lightweight wizard for tasks — then runs a duplicate check,
  a mandatory preview, and creates it. Use when the user says "draft a work
  item", "I have a rough requirement", "help me write a bug report", "turn
  this into a user story", or provides unstructured requirements and wants a
  work item created.
user-invocable: true
disable-model-invocation: false
---

# Draft Work Item

You turn a rough requirement into a posted **Azure DevOps work item**. You
classify the intent, then draft it at the depth the type warrants, run a
duplicate check, show a mandatory preview, and create it.

This skill is self-contained — the drafting logic for features and bugs
(normally deep sub-skills) and the blind-spot detection pass (normally a
dispatched agent) are inlined directly below rather than delegated to an
external plugin. You still delegate to a sibling skill in **this** plugin via
the **Skill** tool where noted: `azure-devops:mentions`.

| Intent | Route |
|---|---|
| **Feature / user story** | Phase 1, Feature route (inlined below) |
| **Bug / defect** | Phase 1, Bug route (inlined below) |
| **Task / trivial** | Phase 2 — Quick Path (inline) |

## Phase 0 — Resolve the Provider

This plugin only serves Azure DevOps. Skip provider detection — the provider
is always Azure DevOps.

### Phase 0b — Ensure Tooling Is Ready (soft)

Before the first Azure DevOps CLI call in this session, run
`sandbox-auth:azure-devops` (pass the target org if known). If a CLI call
later fails with `E_AUTH`, run it once more and retry. Never ask the user to
supply a PAT, token, or credential file.

(When reached through a command wrapper, the wrapper has usually already done
this check — only re-run on an actual failure.)

## Phase 1 — Classify Intent and Route

Propose a type from the raw input:

- **Bug** — defect, crash, regression, unexpected behavior → **Bug route** below.
- **Feature / User Story** — new capability or user-visible enhancement →
  **Feature route** below.
- **Task / trivial** — routine/technical work, refactor, config, tooling,
  infra, or anything small and unambiguous → **Phase 2 — Quick Path**.

If the type isn't obvious, show the options and ask — do not guess.

Type source: call `getWorkItemTypes` once; map to **Bug / User Story (or
PBI) / Task**:

```bash
node "${CLAUDE_PLUGIN_ROOT}/scripts/ado-cli.js" getWorkItemTypes --structured <<'ADOJSON'
{}
ADOJSON
```

Both routes below work the phases in order, but loop freely: new
understanding sends you back to re-ground earlier phases. Ask questions **one
at a time**, multiple-choice where possible. Both routes end by handing off a
composed `{type, title, body, meta}` to Phase 3 (Finalize) — nothing is
created until Phase 3.5's mandatory preview is confirmed.

### Feature Route

Derive high-quality feature requirements from the rough idea, inlined
directly (no external drafting skill or review agent is delegated to):

1. **Gather Context.** Build real grounding before asking anything: dispatch
   parallel `Explore` subagents over the codebase to find the components,
   patterns, and existing conventions this feature would touch. Search
   existing work for overlap via `listWorkItems` (WIQL) for related/duplicate
   efforts already tracked:

   ```bash
   node "${CLAUDE_PLUGIN_ROOT}/scripts/ado-cli.js" listWorkItems --structured <<'ADOJSON'
   { "query": "SELECT [System.Id] FROM WorkItems WHERE [System.Title] CONTAINS '<keyword>'" }
   ADOJSON
   ```

   Use `WebSearch` only when the requirement leans on external/unfamiliar
   tech, standards, or APIs. Summarize what you learned and where the
   requirement meets reality.

2. **Clarify (loop).** Turn the gaps between the high-level ask and the
   discovered context into questions, asked one at a time. Skip anything
   already volunteered. When answers materially change your understanding,
   loop back to step 1 to re-ground against the code. Stop when the core
   intent, users, and value are clear.

3. **Blind-Spot Scan.** When ambiguities are resolved, self-check the working
   requirements against the **feature lens** below — approach this with fresh
   skepticism, as an outside reviewer would, not from the framing that
   produced the draft. Actually check the codebase (`Grep`/`Glob`/`Read`) for
   concerns that depend on it (existing flows the feature touches, other
   callers) rather than speculating. For each finding, note **what is
   missing**, **why it matters**, and a **suggested clarifying question or
   mitigation**. Keep only findings that are specific and actionable — drop
   generic "consider testing more" filler.

   **Feature lens:**
   - **Edge cases & states** — empty, first-run, max-scale, concurrent,
     partial failure, offline, cancelled, retried. Which states are
     unspecified?
   - **Cross-cutting impact** — what existing flows, callers, or screens does
     this touch? Anything that silently changes behavior elsewhere?
   - **Non-functional needs** — performance/latency, security/authz, privacy
     (EUII), accessibility, internationalization, observability/telemetry.
     Which are unstated but expected?
   - **Data & compatibility** — schema/migration impact, backward/forward
     compatibility, rollout/feature-flag needs, data backfill.
   - **Boundaries & contracts** — API/wire-contract changes, versioning, what
     other teams or clients depend on this.
   - **Failure & recovery** — what happens when a dependency is down? Error
     surfacing, idempotency, rollback.
   - **Acceptance gaps** — does each acceptance criterion have a clear
     pass/fail signal? Any criterion that can't be verified?
   - **Scope honesty** — is anything in the draft actually not needed for the
     stated value (defer it)? Flag possible over-reach.

4. **Clarify Blind Spots (loop).** Resolve what the scan surfaced. Fold
   confirmed gaps into the requirements; for open questions, ask the user (one
   at a time). If a finding reopens the core requirement, loop back to the
   relevant earlier step (down to step 1).

5. **Write Requirements.** Compose the body, applying the mention conventions
   loaded in Phase 3.1 below:

   ```markdown
   ## Summary
   <what to build>

   ## Value
   <who benefits and why — the user/persona and the value delivered>

   ## Acceptance Criteria
   - [ ] <verifiable, pass/fail>
   - [ ] ...
   ```

   Generate a concise title (< 80 chars). Every acceptance criterion must have
   a clear pass/fail signal.

6. **Review for Over-Engineering & Duplication.** Read the drafted
   requirements back with fresh eyes and check, directly, for gold-plating,
   scope creep, and unrequested scope. When the feature looks like it may
   overlap existing functionality, search the codebase (`Grep`/`Glob`) for a
   duplicate or near-duplicate implementation already present.

7. **Address Feedback.** Trim scope and fold in your own review findings from
   step 6. Ask the user about any genuine ambiguity it raised (one at a time).
   If the changes are substantial, loop back to the relevant step to
   re-validate.

8. **Hand off** `{type: feature/user-story, title, body, meta}` to Phase 3
   (Finalize). The item is created only after Phase 3.5's mandatory preview.

Guidelines for this route: ground every requirement in gathered context — no
speculative requirements; one question at a time, prefer multiple-choice;
loop freely; YAGNI — defer anything not needed for the stated value.

### Bug Route

Produce a root-caused, reproducible bug write-up from the defect report,
inlined directly (no external drafting skill or debugging skill is delegated
to):

> **Repository safety:** this flow never mutates the user's repo. Regression
> tests are written to a **scratch location** and run there. Nothing is
> committed, no branches are created, and the working tree is left clean.

1. **Gather Evidence.** Find the relevant code paths — dispatch `Explore`
   subagents as needed. Pull any proof: logs, stack traces, failing output,
   the reported repro steps. Note what is established fact vs. what is still
   assumed.

2. **Clarify.** Ask the user clarifying questions only if the defect isn't
   already clear from the evidence (expected vs. actual, how to reproduce,
   impact). Don't over-interrogate.

3. **Hypothesize Root Causes.** Enumerate multiple candidate root causes — do
   not commit to the first guess. Spawn one subagent per hypothesis to
   investigate in parallel. Each subagent may build/run the application and
   inspect logs (best-effort) to confirm or refute its branch, and reports
   back a conclusion with evidence.

4. **Validate by Regression Test.** For each surviving hypothesis:
   - Write a candidate regression test to a **scratch location** (the session
     scratch dir — never inside the repo's tracked tree).
   - Run it best-effort. The test should fail in a way that demonstrates the
     bug (red proves the repro).
   - Keep only hypotheses whose test actually reproduces the bug. Capture the
     test source and its run output as repro proof for the work item body.
   - If the project can't be built or run, record that explicitly and fall
     back to static reasoning for that hypothesis.

   Leave nothing behind: no committed files, no branches, clean working tree.

5. **Blind-Spot Scan + Write Understanding.** Self-check the validated root
   cause against the **bug lens** below — other call sites sharing the same
   root cause, adjacent/related defects, regression-risk areas, and
   data-integrity/migration fallout. Actually check the codebase for these
   rather than speculating. For each finding, note **what is missing**, **why
   it matters**, and a **suggested clarifying question or mitigation**.

   **Bug lens:**
   - **Same root cause elsewhere** — what other call sites, modules, or
     inputs share the same faulty code path or pattern? Is the bug a symptom
     of a broader defect?
   - **Adjacent defects** — does the evidence hint at related but distinct
     bugs that would otherwise be missed?
   - **Regression-risk areas** — what nearby behavior could a fix plausibly
     break? What should regression coverage protect?
   - **Data integrity & migration fallout** — has the bug already corrupted
     or mis-written persisted data? Is a data fix/backfill needed in addition
     to the code fix?
   - **Trigger conditions** — environment, configuration, timing/race, scale,
     or user-specific conditions under which it does or does not reproduce.
   - **Severity & blast radius** — who is affected, how widely, and is there
     active data loss or a security/privacy angle that raises priority?
   - **Workaround** — is there an interim mitigation worth recording for
     whoever picks up the work?

   Then compose the body, applying the mention conventions loaded in Phase
   3.1 below:

   ```markdown
   ## Summary
   <one-line summary>

   ## Steps to Reproduce
   1. ...
   2. ...

   ## Expected Behavior
   <what should happen>

   ## Actual Behavior
   <what actually happens>

   ## Root Cause
   <the validated root cause>

   ## Repro Proof
   <the scratch regression test + its run output>

   ## Related Risk
   <blind-spot findings: same-root-cause call sites, regression risk, data fallout>
   ```

   Generate a concise title (< 80 chars).

6. **Hand off** `{type: bug, title, body, meta}` to Phase 3 (Finalize). The
   item is created only after Phase 3.5's mandatory preview.

Guidelines for this route: never commit to a single root cause without
validation — multiple hypotheses, one subagent each; only test-validated
hypotheses advance; repo stays clean — scratch only, nothing committed.

## Phase 2 — Quick Path (Task / trivial, inline)

For Tasks and trivially-clear items, run the lightweight wizard here:

1. **Clarify** — ask 2-3 questions, one at a time, multiple-choice where
   possible:
   - What does "done" look like (definition of done)?
   - Is this blocked by or blocking anything else?
   - Priority? (Critical / High / Medium / Low)
2. **Blind-spot check (self)** — re-read the draft once more against the
   **task lens**:
   - **Dependencies** — is this blocked by, or blocking, other work?
   - **Done-definition gaps** — is "done" concrete and verifiable, or vague?
   - **Side effects** — does the change ripple into builds, CI, other
     consumers, or configuration that isn't mentioned?
   - **Hidden scope** — does the task quietly imply follow-on work that
     should be named now?

   Fold confirmed findings in; ask the user about anything material.
3. **Compose** — title (< 80 chars) + body:
   ```markdown
   ## Summary
   <what needs to be done>

   ## Definition of Done
   - [ ] ...
   ```
4. Continue at Phase 3 with `{ type: task, title, body, meta }`.

## Phase 3 — Finalize (shared for all routes)

Whatever produced the `{type, title, body, meta}` — a route above or the
Quick Path — finalize it here.

### 3.1 Apply Mention Conventions

Before composing or finalizing the body, use the **`azure-devops:mentions`**
skill (via the **Skill** tool) — it loads the full mention/reference syntax.
See also
[../../references/ado-mention-conventions.md](../../references/ado-mention-conventions.md).

### 3.2 Resolve Placement

Resolve **Area Path + Team**: call `getTeams`, match the description to a team
or ask, then derive area path `<Project>\<Team>` (adapt via
`getWorkItemTypeFields` for `System.AreaPath`). Propose `getCurrentSprint`;
offer a different sprint (`getSprints`) or "Backlog — no sprint". Priority
maps to `Microsoft.VSTS.Common.Priority` numeric: Critical=1, High=2,
Medium=3, Low=4.

```bash
node "${CLAUDE_PLUGIN_ROOT}/scripts/ado-cli.js" getTeams --structured <<'ADOJSON'
{ "filter": "<keyword>" }
ADOJSON
```

```bash
node "${CLAUDE_PLUGIN_ROOT}/scripts/ado-cli.js" getWorkItemTypeFields --structured <<'ADOJSON'
{ "processId": "<process-id>", "witRefName": "<work-item-type-ref-name>" }
ADOJSON
```

```bash
node "${CLAUDE_PLUGIN_ROOT}/scripts/ado-cli.js" getCurrentSprint --structured <<'ADOJSON'
{ "teamId": "<team-id>" }
ADOJSON
```

```bash
node "${CLAUDE_PLUGIN_ROOT}/scripts/ado-cli.js" getSprints --structured <<'ADOJSON'
{ "teamId": "<team-id>" }
ADOJSON
```

If the user is unsure, use the project's root/default area path.

### 3.3 Duplicate Check

Call `listWorkItems` with the proposed title:

```bash
node "${CLAUDE_PLUGIN_ROOT}/scripts/ado-cli.js" listWorkItems --structured <<'ADOJSON'
{ "query": "SELECT [System.Id] FROM WorkItems WHERE [System.Title] CONTAINS '<proposed title>'" }
ADOJSON
```

If a close match is found, surface it and ask whether to continue or treat it
as a duplicate.

### 3.4 Assignment (optional)

"Assign to me" / "Pick a team member" (`getTeamMembers`) / leave unassigned.

```bash
node "${CLAUDE_PLUGIN_ROOT}/scripts/ado-cli.js" getTeamMembers --structured <<'ADOJSON'
{ "teamId": "<team-id>" }
ADOJSON
```

### 3.5 Preview — Mandatory Confirmation

Present a full preview and **wait for explicit confirmation** before creating:
- work item type
- title
- assignee choice
- placement: area path + iteration path
- priority field
- formatted body

Offer edit options (title / body / type / placement / cancel) and re-show the
preview after any edit. **Never create without explicit confirmation.**

## Phase 4 — Create

After confirmation, call `createWorkItem` with `workItemType`, `title`, `description`
(Markdown), `areaPath` (omit if default), `iterationPath` (omit if default),
`assignedTo` (omit if unassigned), and `additionalFields` including
`Microsoft.VSTS.Common.Priority` (1-4):

```bash
node "${CLAUDE_PLUGIN_ROOT}/scripts/ado-cli.js" createWorkItem --structured <<'ADOJSON'
{
  "workItemType": "<Bug|Task|User Story>",
  "title": "<title>",
  "description": "<body>",
  "areaPath": "<Project>\\<Team>",
  "iterationPath": "<Project>\\<Iteration>",
  "assignedTo": "<user or omit>",
  "additionalFields": { "Microsoft.VSTS.Common.Priority": 2 }
}
ADOJSON
```

Report the created work item ID.

## Follow-up

After creation, offer the next relevant step: Start `/work-on <id>` · Create
another · Stop here.

If the user picks "create another", loop back to Phase 1 and start the next
item.

## Guidelines

- Resolve the provider once (Phase 0 — always Azure DevOps), then keep the
  flow identical.
- Route by intent: features and bugs get the deep routes; tasks stay inline.
- Blind-spot detection runs on **every** path — via the inlined checks in the
  feature/bug routes, and the task lens on the Quick Path.
- Ask one question at a time; prefer multiple-choice options.
- Prefer existing project conventions over inventing new types or field
  values.
- Always show the full preview before creating.
- Work items are append-only — never edit prior comments when posting
  follow-ups.
- Use the bundled `ado-cli.js` for all Azure DevOps operations. The drafting
  and blind-spot-detection logic in this file is inlined directly — reason
  through it yourself rather than delegating to an external plugin.
