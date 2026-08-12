---
name: babysit-pr
description: >
  This skill should be used when the user asks to "babysit a PR", "babysit my pull request", "monitor my PR", "watch my pull request", "keep my PR green", "fix PR build failures automatically", "handle PR review comments", or wants autonomous Azure DevOps PR monitoring that fixes build breaks, test failures, code coverage gaps, and review comments on a polling loop.
user-invocable: true
disable-model-invocation: false
---

# Babysit PR

Autonomous PR babysitter. Monitor an Azure DevOps pull request in a continuous
loop, collecting issues and delegating fixes to the `azure-devops:babysit-pr-worker` agent
each iteration. Act without asking for user confirmation on individual fixes —
fix, push, and re-check.

## Mutation & Privacy Policy

<mutation_privacy_policy>
- **Ordinary mutations — just do them.** Invoking this workflow, or an explicit user request, authorizes the creates, updates, comments, commits, and pushes it performs by name. No extra gate is added on top. This NEVER bypasses an existing gate: confirmation prompts, mandatory previews, hard checkpoints, and `approvalSource` verifications stay exactly as written, and where a local rule is stricter, the local rule wins.
- **Read-only discovery is not a mutation.** `ado-cli.js list --json`, `--help`, and any list/get/query method may be called freely at any time — no confirmation, even for methods this plugin does not otherwise name.
- **Comments are append-only.** NEVER invoke `manageWorkItemComment` with `action: "update"` or `"delete"`, and never edit or delete a comment already posted on a work item or PR. To correct or supersede something, post a NEW comment. This limits comments only — a work item's own fields and state are still updated normally when the workflow or the user calls for it.
- **Confirm the exceptional first** — name the action and the resource, and proceed only on an affirmative. The named destructive or irreversible methods (`mergePullRequest`, `runPipeline`, `deletePackageVersion`, `rotateSecrets`, `manageSecurityPolicies`, overwrite-style `createOrUpdateWikiPage`) always require a fresh, explicit confirmation, regardless of any gate already passed. For anything outside this workflow's stated scope, any mutating CLI method this plugin does not reference by name, or a broad/bulk write across many items at once, confirmation is required only when the user did not ask for that action and no hard gate in this workflow already authorized it — once a checkpoint or `approvalSource` gate has passed, autonomous loops continue without further prompting.
- **No PAT surface.** Never ask the user for a PAT, token, or credential file, and never pass `--pat`. Auth is `sandbox-auth:azure-devops` only. On every successful call the CLI's stderr prints one exact line, `[Auth] Auth type: none, PAT: not set` — reading it is a separate, mandatory check, made in addition to the exit-code/stdout branching that already determined success. A non-zero exit still branches on exit code and stdout as usual and is not guaranteed to carry this banner at all. If a successful call ever reports a PAT or any other auth type, STOP and treat it as a credential leak instead of continuing.
- **Never dump the environment.** No `env`, `printenv`, or `echo "$HTTP_PROXY"`. Proxy variables carry a per-sandbox credential — test presence (`[ -n "$HTTPS_PROXY" ]`), never print the value.
- **Never paste raw payloads.** Summarize CLI output, request bodies, and build/test logs; never copy them wholesale into chat, ADO comments, or state files. Redact any `token`/`pat`/`password`/`secret`/`authorization` field before quoting it.
- **Thread resolution** follows this plugin's existing review-thread rules — nothing in this block changes them.
</mutation_privacy_policy>

## Entry

1. **Identify the PR** — Use the PR number from `$ARGUMENTS`. If none provided,
   detect from the current branch using `listPullRequests` filtered by source
   branch:

```bash
node "${CLAUDE_PLUGIN_ROOT}/scripts/ado-cli.js" listPullRequests --structured <<'ADOJSON'
{ "repository": "<repository name>", "status": "active" }
ADOJSON
```

   Match the returned PRs against the current branch's `sourceRefName`
   (`refs/heads/<current-branch>`). If multiple PRs match, list them and ask
   the user to pick one.
2. **Validate** — Use `getPullRequest` to confirm the PR exists. Display a
   status summary: title, source → target branch, reviewer votes, build status,
   active comment count.

```bash
node "${CLAUDE_PLUGIN_ROOT}/scripts/ado-cli.js" getPullRequest --structured <<'ADOJSON'
{ "repository": "<repository name>", "pullRequestId": <pr_id>, "include": ["reviewers", "policies", "workItems"] }
ADOJSON
```

3. **Detect build tool** — Inspect the repo root to determine the project type:
   - `.sln` or `.csproj` → `dotnet build` / `dotnet test`
   - `package.json` → `npm run build` / `npm test`
   - `pom.xml` → `mvn compile` / `mvn test`
   - `Makefile` → `make` / `make test`
   - None of the above → skip local build, rely on CI results only
4. **Detect area path** (for deferred work item creation) — Resolve once at
   entry so the worker can create work items without interactive prompts:
   1. Check if the PR has linked work items (from `getPullRequest` response).
      If yes, call `getWorkItemById` on one and extract its `System.AreaPath`.

```bash
node "${CLAUDE_PLUGIN_ROOT}/scripts/ado-cli.js" getWorkItemById --structured <<'ADOJSON'
{ "id": <work_item_id> }
ADOJSON
```

   2. If no linked work items, call `getTeams` and attempt to match team names
      against the PR's changed file paths or branch name.

```bash
node "${CLAUDE_PLUGIN_ROOT}/scripts/ado-cli.js" getTeams --structured <<'ADOJSON'
{ "filter": "<candidate team name>" }
ADOJSON
```

   3. If no match, leave area path as `null` (project default will be used).
   Store the resolved area path to pass to the worker agent in Step 4.

---

## The Babysit Loop

Repeat until an exit condition is met:

### Step 1: Check PR Status

Use `getPullRequest` to check:
- Merge status (merged? completed? abandoned?)
- Reviewer votes (approved? waiting? rejected?)
- Build/CI status (succeeded? failed? in progress?)

```bash
node "${CLAUDE_PLUGIN_ROOT}/scripts/ado-cli.js" getPullRequest --structured <<'ADOJSON'
{ "repository": "<repository name>", "pullRequestId": <pr_id>, "include": ["reviewers", "policies", "workItems"] }
ADOJSON
```

<exit_check>
**Exit if**: PR is merged, completed, or abandoned. Also exit if all builds are
green, all comment threads are resolved, and at least one reviewer has approved.
</exit_check>

### Step 2: Collect All Issues

Gather every actionable issue into a structured checklist. The checklist drives
the entire iteration — nothing gets fixed unless it's on the list.

#### 2a. Merge Conflicts
- Check `getPullRequest` merge status for conflicts
- If conflicts exist, identify which files are conflicting
- This is the **highest priority** — nothing else can proceed until conflicts
  are resolved

#### 2b. PR Policy / Evaluation Checklist Failures
- Check `getPullRequest` response for policy evaluation fields: `mergeStatus`,
  `isDraft`, `autoCompleteSetBy`, reviewer vote counts, and any evaluation
  criteria in the response. Also check for linked work items (work item linking
  policy).
- For each failing or incomplete policy, capture: policy name, current status,
  what's needed
- **Actionable** policies (create todos): work item linking (can link one),
  comment resolution (address comments), draft status (can publish)
- **Informational** policies (note but skip): needs more reviewers, needs
  specific reviewer approval — these require human action

#### 2c. Build Breaks
- Check build/CI status from the PR
- If failed, retrieve build logs and error messages
- Summarize the core issue (compilation error, missing reference, config problem)

#### 2d. Test Failures
- Check test run results from CI
- For each failing test, capture: test name, error message, stack trace

#### 2e. Code Coverage Gaps
- Check if a coverage policy is failing on the PR
- If so, identify which code blocks lack coverage from the coverage report

#### 2f. Code Review Comments
- Use `getPullRequestComments` to fetch all comment threads:

```bash
node "${CLAUDE_PLUGIN_ROOT}/scripts/ado-cli.js" getPullRequestComments --structured <<'ADOJSON'
{ "repository": "<repository name>", "pullRequestId": <pr_id>, "status": "active" }
ADOJSON
```

- Filter to **active (unresolved)** threads only
- For each thread, capture: comment text, file path, line number, reviewer name
- Skip threads already replied to in a previous iteration
- For each new thread, check for the `[BLOCKER]` tag from the reviewer
  (absence means non-blocking). Apply stricter criteria to BLOCKERs: Won't
  Fix on a BLOCKER requires stronger justification.
- Pre-classify before delegating using the evaluation steps in
  `references/review-reception-protocol.md` (verify, YAGNI check, context
  check) and `references/review-thread-state-machine.md` for state transitions.

<comment_disposition>
**For each comment, classify into one of three dispositions:**

1. **Resolve** — The comment identifies a real issue. Fix the code, prioritizing
   code quality and codebase health over ease. The fix should be what's best for
   the codebase long-term, not the quickest way to make the comment go away.

2. **Won't Fix (reply)** — The suggestion is out of scope, already addressed,
   or would introduce a regression. Reply with a technical rationale explaining
   why. Use when the issue is minor or the rationale is self-evident.

3. **Won't Fix (defer with work item)** — The comment raises a valid concern
   that **cannot be ignored** but is genuinely out of scope for this PR. The
   worker creates a tracked work item via `createWorkItem` (using the area path
   detected at entry), then replies with:
   `Won't Fix: Deferred to #<work-item-id> for follow-up`.
   Use this path when:
   - The issue is a `[BLOCKER]` that you cannot address within the PR's scope
   - The comment identifies a systemic problem (e.g., "this pattern is broken
     everywhere") that needs its own PR
   - The fix would require changes outside the files touched by this PR
   - The reviewer flagged a pre-existing issue exposed by (but not caused by)
     this PR
   - The concern is architecturally significant and deserves dedicated attention

**Disposition priority**: Default to **Resolve**. Only use Won't Fix when there
is a clear, defensible technical reason. Between the two Won't Fix options,
use "defer with work item" when the issue is important enough that dropping it
would be negligent.
</comment_disposition>

### Step 3: Plan the Iteration

<planning_gate>
**Do NOT proceed to fixes until this planning step is complete.** The plan is
the single source of truth for what the worker agent will do this iteration.
</planning_gate>

Convert all collected issues into a concrete, trackable plan using TodoWrite.
Create one todo item per actionable unit of work. Each todo must be specific
enough that progress can be verified after the worker finishes.

#### Todo format by issue type

**Merge conflicts** (highest priority):
```
[ ] Resolve merge conflict: <file path> (e.g., "Resolve merge conflict: src/Services/UserService.cs")
```

**PR policy failures** (only actionable ones):
```
[ ] Fix policy: <policy name> — <what's needed> (e.g., "Fix policy: work item linking — associate a work item to this PR")
```

**Build breaks:**
```
[ ] Fix build: <root cause summary> (e.g., "Fix build: missing using for System.Linq in UserService.cs")
```

**Test failures** — one todo per failing test:
```
[ ] Fix test: <FullyQualifiedTestName> — <brief error> (e.g., "Fix test: PaymentTests.ChargeCard_InvalidCvv — expected 400 got 500")
```

**Code coverage gaps** — one todo per uncovered block:
```
[ ] Add coverage: <method or file> (e.g., "Add coverage: OrderProcessor.HandleRefund — lines 45-62")
```

**Review comments** — one todo per active thread, stating the intended disposition:
```
[ ] Comment thread <ID>: Resolve — <what to fix> (e.g., "Comment thread 42: Resolve — add null check on userInput per reviewer")
[ ] Comment thread <ID>: Won't Fix — <rationale> (e.g., "Comment thread 58: Won't Fix — style preference, no functional impact")
[ ] Comment thread <ID>: Won't Fix (defer) — <issue summary> → create work item (e.g., "Comment thread 63: Won't Fix (defer) — systemic error handling gap across all controllers → create work item")
```

<todo_specificity>
**Every todo must be specific and actionable.** Each item should describe exactly
what needs to happen — not "fix the issue" but "add null check for `user`
parameter in `UserService.Process()` at line 45". The worker agent should be able
to act on each todo without needing to re-analyze the problem.
</todo_specificity>

#### Planning checklist

1. Create all todo items via TodoWrite before continuing.
2. Review the full plan — verify no issues were missed, no threads skipped
   without reason, and priorities are correct:
   **Merge conflicts → BLOCKERs → build breaks → test failures → coverage →
   non-blocking comments**
3. Display the plan to the conversation as a numbered summary so progress is
   visible.

Only after the plan is finalized, proceed to Step 4.

### Step 4: Delegate to Worker Agent

Spawn the `azure-devops:babysit-pr-worker` agent with:
- The todo plan from Step 3 (the full list of items to address)
- Repository name, PR number, source branch name, and target branch name
- Detected build/test commands
- List of previously addressed comment thread IDs (to avoid re-work)
- Pre-classification notes from Step 2f (YAGNI flags, context gaps, verification results)
- **ADO context**: project name, resolved area path (from Entry step 4, or
  `null` if not resolved). The worker uses this to create work items for
  deferred Won't Fix items without interactive prompts.

**Worker review-reception rules** — instruct the worker to follow
`references/review-reception-protocol.md` in autonomous mode (no user
confirmation). Pass the pre-classification notes so the worker can act on them.

The worker agent addresses each todo item, performs self-review, build
verification, commit, and push for this iteration. Wait for it to complete.

After the worker finishes, update each todo item's status (completed or failed)
based on the worker's report. Log any items that could not be resolved with the
worker's explanation.

If there are no issues (everything is green but awaiting reviewer action), skip
to Step 5.

### Step 5: Wait & Re-poll

Report a brief status summary: which todos were completed, which failed, and
what is still pending (or that everything is clean and waiting on reviewers).
Then wait **15 minutes** before looping back to Step 1.

---

## Exit Conditions

<exit_conditions>
Stop the loop when:
- PR is merged, completed, or abandoned
- All builds green + all comment threads resolved + reviewer approved
- All remaining issues classified as Won't Fix with no new issues for two
  consecutive iterations (nothing left to act on — waiting on reviewer action)
- User interrupts with "stop" or "enough"
- After 20 iterations without full resolution, stop and report status to user
</exit_conditions>

When exiting, display a final summary: total iterations, issues fixed, comments
addressed, current PR status.

---

## ADO Reference Conventions

Use the `azure-devops:mentions` skill before composing any comment or reply.
It loads the full mention syntax reference. Key rules:
- Use `#<id>` when referencing work items in comments or replies
- Use `!<id>` when referencing PRs in work item discussions
- Prefix all bot replies with `[<developer name>'s bot]`

## Guidelines

- Act autonomously — do not ask for user confirmation on each fix
- Maintain a running log of addressed comment thread IDs across iterations to
  prevent duplicate work
- If the worker agent reports it could not fix something, note it in the status
  summary but do not retry the same issue next iteration unless the context has
  changed (e.g., new comments on the same thread)
- Use the bundled `ado-cli.js` for all ADO operations, git/bash for local ops
- Follow `references/review-reception-protocol.md` for all reviewer feedback
  evaluation — verify before implementing, push back when wrong, fix in
  priority order (blocking → simple → complex)

## Tools

- **Azure DevOps CLI methods (via `ado-cli.js --structured`)**: `getPullRequest`, `getPullRequestComments`,
  `getPullRequestFileChanges`, `listPullRequests`, `getWorkItemById`, `getTeams`
- **Agent**: `azure-devops:babysit-pr-worker` (spawned each iteration — uses `createWorkItem`
  directly for deferred Won't Fix items)
