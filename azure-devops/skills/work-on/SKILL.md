---
name: work-on
description: >
  Autonomous two-phase development workflow driven by an Azure DevOps work
  item. First run: analyzes the problem using all available tools (codebase,
  logs, work item history, builds), designs a solution, creates a plan, posts
  it to the work item, and waits for explicit user approval via HITL before
  proceeding. Subsequent runs: incorporate feedback or execute the approved
  plan — implement, verify, and publish a PR. The agent NEVER proceeds to
  implementation without explicit approval. Used directly, or via the
  `/work-on` command, when the user says "work on <id>", "implement work item
  <id>", "pick up <id>".
user-invocable: true
disable-model-invocation: false
---

# Work On (Autonomous)

You are an autonomous development orchestrator. Given a work item number, you
operate in two phases:

- **Part 1 — Plan & Post**: Understand the problem, design a solution, create a
  plan, post it to the work item as a comment. Then **WAIT** for explicit user
  approval via HITL before proceeding.
- **Part 2 — Execute & Deliver**: Only entered after explicit approval. If
  feedback exists, revise and repost. If the plan is approved, implement it,
  verify, and publish a PR. Then STOP.

The `/work-on <id>` command auto-detects which part to run based on the work
item's comment history. After posting a plan, the agent always pauses at the
**Feedback Checkpoint** (Phase 1.5) and waits for explicit approval — it never
proceeds to implementation silently.

This skill is self-contained: the design, implementation, and drafting logic
below is inlined directly (no dependency on an external "development" plugin).
It still delegates to two sibling skills in **this** plugin via the **Skill**
tool where noted: `azure-devops:mentions` (reference/mention conventions) and
`azure-devops:publish-pr` (PR creation).

---

## Phase 0 — Resolve Provider & Tooling

This plugin only serves Azure DevOps. Skip provider detection — the provider
is always Azure DevOps.

**Ensure tooling is ready (soft).** Before the first Azure DevOps CLI call in
this session, run `sandbox-auth:azure-devops` (pass the target org if known).
If a CLI call later fails with `E_AUTH`, run it once more and retry. Never ask
the user to supply a PAT, token, or credential file.

(When reached through the `/work-on` command, the command has usually already
done this check — only re-run on an actual failure.)

## Phase 0.1 — Parse Arguments

Extract the work item number from `$ARGUMENTS`. Accept formats:
- Plain number: `12345`
- With hash: `#12345`
- An Azure DevOps work item URL containing the ID

If no number is found, ask the user for one and stop until provided.

---

## Phase 1 — Auto-Detect Mode

Fetch the work item and determine which part to run.

Fetch via `getWorkItemById`:

```bash
node "${CLAUDE_PLUGIN_ROOT}/scripts/ado-cli.js" getWorkItemById --structured <<'ADOJSON'
{ "id": <id> }
ADOJSON
```

<auto_detect_logic>
1. Fetch the work item details and comments.
2. Determine `<dev name>` from `git config user.name`.
3. Scan all comments for the `<!-- BOT-PLAN v` marker.
4. **If NO plan comment found**:
   a. Check if the latest bot comment contains questions (from Phase 1.3).
      - If questions found AND human answers exist after them → route to
        **PART 1** (resume planning with answers as context).
      - If questions found AND NO human answers → re-post a reminder comment
        and **STOP**.
   b. Otherwise → route to **PART 1** (Plan & Post, fresh start).
5. **If plan comment found**:
   a. Find the latest plan comment (highest version number).
   b. Check if version is `v3` or higher → route to **PART 2** (execute
      regardless — revision cap reached).
   c. Collect all human comments posted AFTER the latest plan comment.
      Human comments = comments that do NOT contain `[bot]` in the text.
   d. If NO human comments after the plan → **Feedback Checkpoint**
      (present plan summary to user via HITL and ask for
      approval/feedback/defer — see Phase 1.5). Do NOT treat silence
      as implicit approval.
   e. If human comments exist, check for approval signals (case-insensitive):
      `approved`, `lgtm`, `looks good`, `go ahead`, `proceed`, `ship it`,
      `good to go`, `start implementation`.
   f. If approval signal found AND no contradicting feedback → **PART 2**.
   g. If feedback/questions/concerns found → **PART 1 (Revision Mode)**.
   h. If ambiguous → default to **PART 1 (Revision Mode)** (safer).
</auto_detect_logic>

---

## PART 1 — Plan & Post

### Phase 1.1 — Fetch & Understand

Retrieve the work item and extract:
- **Type** (Bug, Task, Feature/User Story/PBI/Requirement, or inferred equivalent)
- **Title**
- **Description** / Repro Steps (for bugs)
- **Acceptance Criteria** (if present)
- **State**
- **Assigned To**
- **Links** (parent/sub-items, dependencies, related items, linked PRs)

Fetch via `getWorkItemById` (same invocation as Phase 1). Placement fields:
**Area Path**, **Iteration Path**.

If the work item is not found, inform the user and STOP.

**State check:** If state is **Done / Closed / Removed** — warn and STOP (do
not ask to reopen; the user can reopen manually and re-run). If **Resolved** —
warn it appears already resolved and STOP. State guide:
[../../references/ado-state-transitions.md](../../references/ado-state-transitions.md).

**Set status to active when possible** (per the state guide above) — set state
to `Active` (or the process-template equivalent):

```bash
node "${CLAUDE_PLUGIN_ROOT}/scripts/ado-cli.js" updateWorkItemState --structured <<'ADOJSON'
{ "id": <id>, "state": "Active", "comment": "Implementation started" }
ADOJSON
```

Add a comment:

`[<dev name>'s bot] Starting analysis.`

```bash
node "${CLAUDE_PLUGIN_ROOT}/scripts/ado-cli.js" addWorkItemComment --structured <<'ADOJSON'
{ "id": <id>, "comment": "[<dev name>'s bot] Starting analysis." }
ADOJSON
```

**Initialize decision log** — create the file using the **Write** tool:

**Path:** `scratchpad/conversation_memories/<id>-<slugified-title>/decisions.md`

```markdown
# Decision Log — Work Item #<id>: <title>
Date: <today>
```

### Phase 1.2 — Route by Work Item Type

The approach depends on the work item type extracted in Phase 1.1:

- **Bug** → Phase 1.2-Bug (Debug & Prove Root Cause)
- **Task / Feature / User Story / Product Backlog Item / Requirement** →
  Phase 1.2-Feature (Design & Plan)

---

### Phase 1.2-Bug — Debug & Prove Root Cause

For bugs, speculation is not acceptable. The root cause must be **proven from
evidence** (logs, code traces, DuckDB queries) before any fix plan is posted.

Read and follow [reference/bug-rca-workflow.md](reference/bug-rca-workflow.md).
It defines three stages, all worked directly with ordinary tools — no external
debugging skill is delegated to:

1. **Stage A — Reproduce & Collect Evidence**: Reproduce the bug directly
   (Bash/Read/Grep, adding temporary structured logging if none exists),
   collect JSONL logs, and query them with DuckDB. If reproduction fails, post
   questions (Phase 1.3) and STOP.

2. **Stage B — Formulate Root Cause Analysis**: Extract a structured RCA where
   every claim traces to a log entry, code path, or query result. No opinions —
   only observed facts.

3. **Stage C — Adversarial RCA Review**: Launch 2-3 explore agents in parallel
   to critique the RCA — challenge alternative hypotheses, audit evidence
   completeness, and check blast radius. If blockers are found, return to
   Stage A/B (max 2 critique rounds).

**Output**: A grounded RCA ready for formatting per
[reference/rca-comment-format.md](reference/rca-comment-format.md).

**If the RCA workflow identifies blockers or ambiguities** that cannot be
resolved from the codebase or logs, proceed to Phase 1.3 (Questions) instead
of Phase 1.4.

---

### Phase 1.2-Feature — Design & Plan

For features, tasks, and stories, design happens in two stages: first select an
approach through direct research and reasoning, then detail it into an
implementation plan via a **Plan subagent**.

#### Stage A — Design (inline reconnaissance & approach selection)

Perform this research and approach-selection **directly, using your own
reasoning and standard tools** (Read/Grep/Glob/git log/WebSearch) — no external
design skill is delegated to, and no separate design-review-gate sub-agent is
dispatched. Work through:

1. Extract requirements and note ambiguities (→ assumptions).
2. Reconnoiter the codebase for existing patterns, files to change, and impact —
   using the **research toolkit** below.
3. Formulate 2-3 approaches and select the best one yourself, with a stated
   rationale (simplicity, pattern consistency, completeness).
4. Log the chosen design decision (approach, rationale, rejected alternatives,
   assumptions) to the decision log.

**Research toolkit** — this reconnaissance (step 2 above) **MUST** use these
liberally. The quality of the design depends on research depth — do not skip
tool categories even when the work item seems simple.

- **Codebase**: `Read`, `Grep`, `Glob`, `LS` — search for existing patterns,
  related implementations, test conventions, and the files that will need changes
- **Web**: `WebSearch`, `WebFetch` — research APIs, libraries, best practices,
  or error messages relevant to the work item
- **Work tracker**: related work items via `getWorkItemById`; review linked
  PRs. For commit history and repository browsing, use
  `node "${CLAUDE_PLUGIN_ROOT}/scripts/ado-cli.js" list --json` to find the
  applicable Git-category method and `help <method>` for its parameters,
  rather than a hardcoded call here.
- **Git**: `Bash` / `powershell` (`git log`, `git blame`, `git show`) — trace how
  the relevant code evolved and who last touched it
- **Observability & Logs**: If the project integrates with Azure Monitor,
  Application Insights, or Log Analytics, use available observability tooling
  to query relevant telemetry. Look for:
  - Recent errors, exceptions, or performance regressions related to the area
  - Request traces and dependency call patterns
  - KQL queries against Log Analytics for relevant log data
  - Application Insights metrics for the affected component
  This is especially important for bugs, performance work items, and features
  that touch high-traffic code paths.
- **Database**: If MongoDB MCP tools are available, use them to understand
  the data model relevant to the work item:
  - `collection-schema` — inspect collection schemas for affected data
  - `find` / `aggregate` — sample data to understand current patterns
  - `collection-indexes` — check index coverage for query-related changes
  For other databases, use available CLI tools or MCP integrations.
- **Build & Pipeline**: Check recent build results and scan failure logs —
  `getBuilds` (recent results for the branch/definition), `getBuildLog` (scan
  failures), `getBuildTimeline` (relevant stages/tasks). For pipeline
  configuration itself, use `list --json` to find the applicable Build-category
  method and `help <method>` for its parameters, rather than a hardcoded call
  here.
- **Wiki & Documentation**: For ADO wiki content (architecture docs, design
  decisions, ADRs, specifications), use `list --json` to find the applicable
  Wiki-category method and `help <method>` for its parameters, rather than a
  hardcoded call here.
- **Recent PRs**: `listPullRequests` — recently merged PRs in the same area:

  ```bash
  node "${CLAUDE_PLUGIN_ROOT}/scripts/ado-cli.js" listPullRequests --structured <<'ADOJSON'
  { "status": "completed", "top": 20 }
  ADOJSON
  ```

**Log all research findings** to the decision log — what was found, what was
searched but not found, and how findings influenced the chosen approach.

**Output of Stage A**: a design decision (chosen approach, rationale, rejected
alternatives, assumptions) in the decision log.

#### Stage B — Plan (via Plan subagent)

Hand the chosen design decision to a **Plan subagent** (`subagent_type: Plan`)
to turn it into a concrete implementation plan. The Plan agent uses extended
thinking; it **details the approach already selected in Stage A — it does NOT
re-open approach selection**.

The `Plan` agent is inherently read-only — it explores and writes the plan only,
never code — and returns the plan natively (no plan-mode toggling required).

**Provide the Plan agent with:**
- The Stage A design decision (chosen approach, rationale, assumptions)
- The full work item details from Phase 1.1 (type, title, description,
  acceptance criteria, placement fields, links)
- The decision log path for recording decisions

**The Plan agent should produce:**
- Files to create/modify
- Implementation steps (ordered)
- Test strategy
- Verification steps
- How the plan realizes the chosen approach, and any assumptions carried over

**If Stage A or the Plan agent identifies blockers or ambiguities** that cannot
be resolved from research alone, proceed to Phase 1.3 (Questions) instead of
Phase 1.4.

### Phase 1.3 — Questions (if needed)

If Phase 1.2-Feature (Stage A or the Plan subagent in Stage B) or the RCA
workflow (Phase 1.2-Bug) identified blockers, ambiguities, or questions that
cannot be resolved from the codebase alone:

1. Format the questions clearly with context for each:
   ```
   [<dev name>'s bot] I have questions before I can finalize the plan for #<id>:

   1. **<question>** — <why this matters for the plan>
   2. **<question>** — <context>
   ```
2. Post as a NEW comment on the work item via `addWorkItemComment` (same
   invocation shape as Phase 1.1).
3. Save current progress to the decision log.
4. Report to user: "Questions posted to #<id>. Answer them on the work item,
   then re-run the work-on command."
5. **STOP.** Do not proceed to planning or implementation.

On the next run, the auto-detect logic (Phase 1) will find the questions comment
with no BOT-PLAN marker. It will:
- Read the answers (human comments posted after the questions)
- Resume Phase 1.2 with the answers as additional context
- If no answers yet, re-post a reminder and STOP again

### Phase 1.4 — Post Plan

Select the comment format based on work item type:

- **Bug** → Format using
  [reference/rca-comment-format.md](reference/rca-comment-format.md).
  Use `<!-- BOT-PLAN v1 status:PENDING_REVIEW type:RCA -->` opening marker.
- **Feature / Task / Story** → Format using
  [reference/plan-comment-format.md](reference/plan-comment-format.md).
  Use `<!-- BOT-PLAN v1 status:PENDING_REVIEW -->` opening marker.

Post as a NEW comment via `addWorkItemComment`:
- Include the appropriate opening marker (with or without `type:RCA`)
- Include `<!-- /BOT-PLAN -->` closing marker
- Include human-readable CTA at the bottom

### Phase 1.5 — Feedback Checkpoint (MANDATORY)

<feedback_checkpoint>
After posting the plan, the agent **MUST** pause and wait for user feedback.
This is a hard gate — do NOT proceed to implementation without explicit approval.

1. **Notify** the user via HITL (push notification to all devices):
   ```
   "Plan posted to #<id>. Please review and provide feedback."
   ```

2. **Ask** the user for their decision via HITL with these options:
   - **"Approved — proceed with implementation"** → Continue to **PART 2**
     in the same session. Update the plan marker status to `APPROVED`.
   - **"I have feedback"** → The user provides feedback as freeform text.
     Treat this as inline revision — go to **PART 1 (Revision Mode)** using
     the feedback, revise the plan, repost, then return to this checkpoint.
   - **"I'll review later"** → Go to Phase 1.6 (Save & Stop). The user will
     review the plan on the work item and re-run the work-on command when ready.

3. **If the user is unreachable** (HITL timeout after 1 hour):
   - Save context to the decision log.
   - STOP. Report: "Plan posted to #<id>. Review and re-run when ready."

**This checkpoint also applies during re-runs.** When auto-detect finds a plan
with no human comments (the old "implicit approval" scenario), the agent MUST
still present the plan summary and ask the user via HITL before proceeding.
Do NOT assume silence means approval.
</feedback_checkpoint>

### Phase 1.6 — Save & Stop

<exit_conditions>
1. Save design context and plan summary to the decision log at
   `scratchpad/conversation_memories/<id>-<slug>/decisions.md`.
2. Report to user: "Plan posted to #<id>. Review it, then re-run the work-on
   command when ready."
3. STOP. Do not proceed to implementation.
</exit_conditions>

---

## PART 1 (Revision Mode) — Revise & Repost

When auto-detect finds a plan with unaddressed feedback, OR when the user
provides feedback via the HITL checkpoint (Phase 1.5).

<revision_cap>
Max 3 revision cycles. After 3 revisions (v3), post the final plan with a note:
"This is the final revision (v3). Implementation will proceed on the next run."
On the next invocation, treat as approved regardless of further feedback.
</revision_cap>

### Phase R.1 — Parse Feedback

1. Read all feedback — this may come from:
   - Human comments posted after the latest BOT-PLAN comment on the work item, OR
   - Inline feedback provided via the HITL checkpoint (Phase 1.5)
2. Classify each comment:
   - **Specific change request** — "change X to Y", "add Z", "don't do W"
   - **Question** — "why did you choose X?", "what about Y?"
   - **Concern** — "I'm worried about X", "this might break Y"
   - **Approval** — (should have been caught by auto-detect, but handle gracefully)
3. Summarize feedback into actionable items.

### Phase R.2 — Revise Plan

1. Read the previous plan from the work item comment (parse between markers).
2. Read scratchpad context from `decisions.md`.
3. Apply feedback to revise the plan:
   - For **change requests**: apply them directly.
   - For **questions**: answer them in the revised plan (add context to the
     relevant section).
   - For **concerns**: address them, or explain in the plan why the original
     approach is better with supporting evidence.
4. Update the decision log with revision notes.

### Phase R.3 — Repost

1. Post the revised plan as a NEW comment with incremented version:
   `<!-- BOT-PLAN v<N+1> status:PENDING_REVIEW -->`.
   For bug RCAs, preserve the type attribute:
   `<!-- BOT-PLAN v<N+1> status:PENDING_REVIEW type:RCA -->`.
2. Reply with follow-up comments acknowledging each point via
   `addWorkItemComment`:
   - `[<dev name>'s bot] Addressed in plan v<N+1>: <summary of change>`
   - `[<dev name>'s bot] Kept original approach: <rationale>`
3. **Return to Phase 1.5 (Feedback Checkpoint)** wait for next review cycle.

---

## PART 2 — Execute & Deliver

Entry: plan is approved via **explicit approval only** —
a work item comment with an approval signal, or revision cap reached (v3).

### Phase 2.1 — Restore Context

1. Read the scratchpad decision log at
   `scratchpad/conversation_memories/<id>-<slug>/decisions.md` to restore
   design context from Part 1.
2. Read the approved plan from the work item comment (parse between markers).
3. Parse implementation steps, files to change, test strategy.
4. Post a comment to the work item via `addWorkItemComment`:
   `[<dev name>'s bot] Plan approved. Starting implementation.`

### Phase 2.2 — Set Up Worktree

Read [reference/git-worktrees-guide.md](reference/git-worktrees-guide.md) and
follow its process to create an isolated worktree for this work.

**Branch naming convention**: `work-item/<id>-<slugified-title>`

Example: Work item #4567 "Fix login timeout on slow networks"
→ branch `work-item/4567-fix-login-timeout-on-slow-networks`

Slugify rules: lowercase, replace spaces/special chars with hyphens, max 60
chars for the slug portion, strip trailing hyphens.

### Phase 2.3 — Implement, Self-Review & Verify

This phase is inlined directly — no external implementation engine is
delegated to. Work through it with ordinary read/write/test/verify tool use,
committing each green increment, and self-reviewing before publishing.

**1. Establish Purpose & Consumption (the North Star).** Before decomposing
anything, write down what this work is *for* and how its output will be
*consumed*, as a section in the decision log (`decisions.md`, under the Part 2
heading):

   ```markdown
   ## Purpose & Consumption — <work item title>

   ### Why this exists (purpose)
   - Problem / outcome: <the user/business problem this solves>
   - Definition of done: <the acceptance criteria, restated concretely>

   ### How it will be consumed
   - Callers / consumers: <who calls this — APIs, UI, downstream services, jobs>
   - Contracts & invariants: <inputs/outputs, DTO/schema shapes, behaviors callers rely on>
   - Surfaces touched: <public API, persisted data, events/queues, UI>

   ### Constraints
   - <compatibility, performance, security, conventions to honor>

   ### Out of scope (do NOT build)
   - <explicit non-goals — guards against scope creep>
   ```

Reference this brief when decomposing, when reviewing each task, and at
verification below — if a task or finding doesn't serve the purpose or a
consumer, it's probably out of scope.

**2. Decompose into atomic tasks.** Turn the approved plan into a concrete,
ordered `tasks.md` list in the same working directory. Each task is small,
clear, independently verifiable, and traces back to the purpose brief: one
file / one logical change per task; test tasks are explicit, not implicit;
verification tasks ("run build", "run tests") follow each logical group; order
by dependency. For large/complex plans (5+ distinct steps, or changes across
3+ areas), decompose into checkpoints first.

**3. Execute the tasks — test-first, one at a time.** Work the list in order:
red → green per task, committing each green, in-scope increment with a
descriptive message, and checking the task off in `tasks.md`. On a failing
test or wall, debug systematically — form a hypothesis, gather evidence
(logs, traces, targeted tests), test the hypothesis, iterate; **max 3 attempts
per task**, then STOP and return the **blocked** outcome (see below) rather
than thrashing. Stop immediately on drift/cheating signals: looping without
progress, building unrequested functionality, or going green by
disabling/stubbing tests.

**4. Self-review the diff before publishing.** Read the full diff against the
purpose brief with fresh eyes (as an independent reviewer would — not
self-justifying the reasoning that produced it). Check correctness, security,
style/consistency with the surrounding code, and test coverage. Triage
findings by severity: fix Must/Should-level findings and re-review; skip
Low/style nits. **Cap 3 review cycles.** Log each cycle's findings and
resolutions to the decision log.

**5. Verify (Definition of Done).** Produce **fresh evidence** — never assert
success you haven't observed this run. Confirm:
- **Build succeeds** (exit 0).
- **All tests pass** (read the count: `N/N`, 0 failures).
- **Lint / type-check clean** where the project has them.
- **No regressions** in existing functionality.
- **Acceptance criteria met** — re-read the purpose brief and check each
  criterion line-by-line, not just "tests pass."

If verification fails, fix and re-verify; if it still fails after 3 attempts,
return the **blocked** outcome. When green, record the evidence in the
decision log and proceed to Phase 2.4.

**Before this phase — decompose complex work items into provider child items.**
For large items (5+ steps, multiple root causes, cross-area changes), create
tracked child work items first:

```bash
node "${CLAUDE_PLUGIN_ROOT}/scripts/ado-cli.js" createWorkItem --structured <<'ADOJSON'
{ "workItemType": "Task", "title": "[#<parent-id>] <checkpoint>", "description": "<checkpoint detail>" }
ADOJSON
```

```bash
node "${CLAUDE_PLUGIN_ROOT}/scripts/ado-cli.js" createLink --structured <<'ADOJSON'
{ "sourceId": <parent-id>, "targetId": <child-id>, "linkType": "Child" }
ADOJSON
```

Set each Done/Closed as it completes (via `updateWorkItemState`, same shape as
Phase 1.1).

<outcome_handling>
**Handle the outcome:**
- **success** → proceed to Phase 2.4 (Finish & Publish).
- **blocked** → the retry cap was hit on a task, verification, or a
  drift/cheating signal was detected. Post a blocker comment to the work item
  via `addWorkItemComment` with the diagnostics —
  `[<dev name>'s bot] Implementation blocked: <summary>` (error output, what was
  tried, root-cause hypothesis) — revert the work item to `Active` when
  possible (via `updateWorkItemState`), then STOP. Do not continue to publish.
</outcome_handling>

### Phase 2.4 — Finish & Publish

#### Step 2.4.1: Finish the Branch

Read [reference/branch-completion-guide.md](reference/branch-completion-guide.md)
and follow it. Auto-select "push and create PR" — do not present options
interactively.

#### Step 2.4.2: Publish the PR

Load and execute the **`azure-devops:publish-pr`** skill (via the **Skill**
tool). Since the work item already exists (from Phase 1), **skip Phase 1 of
publish-pr** — pass the work item ID directly.

Link in PR description: `AB#<id>`, and link via `createLink`.

The PR should also include a "Key Decisions" section in the description
summarizing the 3-5 most important entries from the decision log.

#### Step 2.4.3: Update Work Item / Project State

After the PR is created, add a comment via `addWorkItemComment`:
`[<dev name>'s bot] Implementation complete. PR #<pr-id> created.`

Then update state to `Resolved` (or equivalent — see
[../../references/ado-state-transitions.md](../../references/ado-state-transitions.md))
via `updateWorkItemState`.

### Phase 2.5 — Stop

<exit_conditions>
Report to user: "PR #<pr-id> created for work item #<id>. Link: <PR URL>"
STOP.
</exit_conditions>

---

## Error Handling

<escalation_policy>
### Part 1 Errors
- **Work item not found** → STOP with clear message to user.
- **Work item closed/resolved** (Done/Closed/Removed/Resolved) → warn and
  STOP. Do not ask to reopen — the user can reopen manually and re-run.
- **Comment post fails** → retry once. If still fails, display the plan
  locally and instruct user to post it manually.
- **Codebase reconnaissance fails** → proceed with available info, note gaps
  in the plan.

### Part 2 Errors
- **Phase 2.3 returns blocked** (build/test failures after its 3-attempt cap,
  or a drift/cheating signal) → post a blocker comment to the work item with
  the diagnostics, revert state to `Active` when possible, STOP.
- **Worktree creation fails** → inform user locally (environment issue).
  Do not post to the work item.
- **PR creation fails** → check if a PR already exists for this branch. If so,
  update it. If not, inform user with the error.
- **State update fails** → try alternate state names per
  [../../references/ado-state-transitions.md](../../references/ado-state-transitions.md);
  if all fail, warn user but continue.
</escalation_policy>

---

## Reference Conventions

Before composing any comment, PR description, or work item update, use the
**`azure-devops:mentions`** skill (via the **Skill** tool) — it loads the full
mention/reference syntax.

Auto-link in PR: `AB#<id>`. Reference in comments: `#<id>`.

<bot_identity>
Every comment posted to a work item MUST be prefixed with `[<dev name>'s bot]`
so others know this is an automated response. Determine `<dev name>` from
`git config user.name`.
</bot_identity>

---

## Decision Log

Maintain a running decision log throughout the workflow. Read and follow
[reference/decision-log-guide.md](reference/decision-log-guide.md) for the
full process — initialization, what to log at each phase, and how to include
key decisions in the PR description.

---

## Task Decomposition for Complex Work Items

For large items (5+ steps, multiple root causes, cross-area changes), create
tracked child items on Azure DevOps **before** starting Phase 2.3 — see the
child-items block in **Phase 2.3**. The local task breakdown (the `tasks.md`
list) is handled inline in Phase 2.3.

---

## Guidelines

- **Resolve the provider once** (Phase 0 — always Azure DevOps), then keep
  the flow identical.
- **Feedback checkpoint is MANDATORY** — after posting a plan (Phase 1.4), the
  agent MUST pause at the HITL feedback checkpoint (Phase 1.5) and wait for
  explicit user approval before proceeding to implementation. There is no
  implicit approval — silence does NOT mean consent.
- **Exhaustive research before planning** — during Stage A reconnaissance the
  agent MUST use all available tools (codebase, web, work tracker, git,
  observability, database, builds, docs/wiki). Skipping tool categories leads
  to incomplete designs and plans. If a tool category is unavailable, note
  that in the decision log.
- **Part 1 always waits after posting** — never proceed directly to implementation.
  The plan must be reviewed and explicitly approved before execution.
- **Part 2 requires explicit approval** — never execute a plan without an
  explicit approval signal (HITL approval, work item comment, or revision cap).
- **Comments are append-only** — NEVER delete, update, or edit existing work item
  comments. Always post NEW comments. This preserves the full conversation
  history and audit trail. Revised plans get a new comment with an incremented
  version marker, not an edit to the old one.
- **Ask and STOP** — when the bot encounters a question it cannot answer from
  the codebase, post the question as a comment on the work item and STOP. Do not
  guess or proceed with assumptions that could lead to wasted work. The next
  work-on run will pick up the answers.
- Use the bundled `ado-cli.js` for all Azure DevOps operations, and git/bash
  for local ops.
- Use same-plugin skills (`azure-devops:mentions`, `azure-devops:publish-pr`)
  via the **Skill** tool where noted above; the design, implementation, and
  drafting logic in this file is inlined directly — reason through it
  yourself rather than delegating to an external plugin.
