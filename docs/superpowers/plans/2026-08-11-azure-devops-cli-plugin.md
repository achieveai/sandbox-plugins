
# Azure DevOps CLI Plugin Port — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. **Deviation from standard writing-plans:** this project has an approved design decision of **NO NEW TEST SUITE** (see spec D2). Every task substitutes a validation/smoke/static-check step (grep, `claude plugin validate`, direct CLI invocation, diff-against-source) for the usual "write failing test" cycle. Do not author test files.

**Goal:** Port the MCP-based `ado` plugin (`B:\sources\claude_plugins\ado`, v3.1.5) into a new CLI-native plugin `azure-devops` (v1.0.0) inside `B:\sources\sandbox-plugins`, replacing every Azure DevOps MCP tool call with a direct `node "${CLAUDE_PLUGIN_ROOT}/scripts/ado-cli.js" <method> --structured` invocation, removing all PAT/MCP surface, and inlining the two skills (`work-on`, `draft-work-item`) that currently delegate to the absent `development` plugin — with zero redesign of workflow, phases, gates, or prompts.

**Architecture:** Vendor the pre-built `ado-cli.js` bundle unmodified as the plugin's only execution engine (spawned as a child process, `--structured` mode, JSON on stdin, branch on exit code). Every skill/agent/command becomes documentation + orchestration around that one invocation surface — no adapter layer, no MCP server, no PAT. Authentication is delegated entirely to the existing `sandbox-auth:azure-devops` skill (already in this marketplace) via a session warm-up rule in the plugin's `CLAUDE.md`.

**Tech Stack:** Node.js (bundled CLI, esbuild output, no new deps), Markdown (skills/agents/commands/CLAUDE.md/README.md), JSON (plugin.json, marketplace.json, provenance.json), `.mjs` helper scripts (`ado-api.mjs`, `scan.mjs`, `classify.mjs`, `state.mjs`) for `work-my-backlog`.

## Global Constraints

- Source `B:\sources\claude_plugins\ado` (v3.1.5) is **read-only** — never modify, stage, or commit anything there. (D-source-immutable, V8)
- Target is a **new** directory `azure-devops/` at the root of `B:\sources\sandbox-plugins`, sibling to `sandbox/` and `sandbox-auth/`. Version starts at **1.0.0** (R11/D7).
- All Azure DevOps calls go through `node "${CLAUDE_PLUGIN_ROOT}/scripts/ado-cli.js" <method> --structured` with a **quoted heredoc** (`<<'ADOJSON' ... ADOJSON`) JSON body on stdin. Never `--input`, never `--input -`. `${CLAUDE_PLUGIN_ROOT}` stays double-quoted, never exported, never derived via `dirname "$0"`. Branch only on exit code + stdout; non-empty stderr is NOT a failure signal (it always carries an auth banner + Node's DEP0169 deprecation warning, even on success). (Canonical invocation form, 5 rules)
- **No adapter/wrapper/client library. No MCP server. No PAT anywhere** (no env var, no `--pat` flag, no credential file) — sandbox-auth is the only auth path. (D3, D5)
- **No dependency on the `development` plugin** — `work-on` and `draft-work-item` inline the logic they used to delegate to, same phases/gates, executed via ordinary agent reasoning instead of named sub-skill delegation. (R6)
- Component (skill/agent/command) **basenames are unprefixed** — no `azure-devops-` or `ado-` prefix (e.g. `ado-work-on` → `work-on`). Exactly 4 filenames keep the `ado-` prefix because they are reference/script files, not components: `ado-cli.js`, `ado-api.mjs`, `ado-mention-conventions.md`, `ado-state-transitions.md`. (D4/R1)
- **Full mapped workflow port**: all 7 source skills, 3 agents, 5 commands (the 8th skill/6th command, `setup-ado-mcp`, is dropped — R10). Same phases, same gates, same decision logic, same prompts. No redesign.
- **No test suite.** Verification = validation + review (offline, credential-free): `claude plugin validate`, greps, direct CLI smoke calls, diff-against-source. (D2)
- Every deviation from the source text must trace to a named rule R1–R12 (spec Appendix/Transform Rules section). An unexplained diff found during Task 17's fidelity review is a defect, not a judgment call.
- Agent frontmatter uses `modelintelligence` + `effort` fields — **not** `model:` (R12).

---

## Execution Map (one screen)

| # | Task | Deliverable | Depends on | Verify |
|---|------|-------------|-----------|--------|
| 1 | Scaffold + vendor CLI | `.claude-plugin/plugin.json`, `scripts/ado-cli.js`, `references/provenance.json`, `references/method-catalog.md` | — | CLI smoke (bare/`list --json`/`docs --out`/`help`), hash match |
| 2 | Verbatim references + mentions skill | `references/ado-mention-conventions.md`, `references/review-reception-protocol.md`, `skills/mentions/SKILL.md` | 1 | diff = 0 vs source (except path renames) |
| 3 | review-thread-state-machine.md (R7) + error-codes.md | `references/review-thread-state-machine.md`, `references/error-codes.md` | 1 | grep for 2 R7 fixes present, grep for stale sync-note absent |
| 4 | ado-api.mjs (R9) + verbatim scan/classify/state | `skills/work-my-backlog/scripts/{ado-api,scan,classify,state}.mjs` | 1 | grep: zero PAT/BEARER/getAuthHeader references |
| 5 | work-my-backlog SKILL.md port | `skills/work-my-backlog/SKILL.md` | 4 | grep: zero `AZURE_DEVOPS_PAT`/`ado:`/prefixed refs |
| 6 | publish-pr SKILL.md port | `skills/publish-pr/SKILL.md` | 1 | grep: zero `ado:`/MCP wording; 3 canonical invocations present |
| 7 | babysit-pr SKILL.md port | `skills/babysit-pr/SKILL.md` | 1,3 | same pattern |
| 8 | work-items SKILL.md port (R8) | `skills/work-items/SKILL.md` | 1 | grep: `searchWorkItems` = 0 hits |
| 9 | Agents port (3 files) | `agents/{assistant,pr-tender,babysit-pr-worker}.md` | 1,3 | grep: `model:` = 0 hits, `modelintelligence`/`effort` present, `skills: [mentions]` |
| 10 | Commands port (5 files) | `commands/{work-on,publish-pr,babysit-pr,draft-work-item,work-my-backlog}.md` | 9 | grep: `ado:`/`setup-ado-mcp` = 0 hits |
| 11 | work-on inline rewrite (R6) | `skills/work-on/SKILL.md` + `skills/work-on/reference/*.md` (6 files) | 1,3 | grep: `development:`/`gh:` = 0 hits; phase headers match source count |
| 12 | draft-work-item inline rewrite (R6) | `skills/draft-work-item/SKILL.md` | 1,2 | grep: `development:`/`gh:`/`searchWorkItems` = 0 hits |
| 13 | CLAUDE.md authoring (R3) | `azure-devops/CLAUDE.md` | 1 | grep: contains warm-up rule, mutation table, append-only rule; zero PAT mentions except negative |
| 14 | README.md authoring | `azure-devops/README.md` | 1–13 | manual: skills table row count = 8, error table row count = 9 |
| 15 | Marketplace + root docs | `.claude-plugin/marketplace.json`, root `README.md` | 1–14 | `python -m json.tool` or `ConvertFrom-Json` parses clean; 3-entry `plugins` array |
| 16 | Release gates V1–V8 | none (verification only) | 1–15 | see Task 16 body |
| 17 | Fidelity diff + commit + push + fresh-clone | git history, remote | 1–16 | fresh clone re-passes V1/V2/V7 |

Tasks 2–10 have no interdependencies beyond Task 1 (CLI must exist so paths/invocations are well-formed) and can be done in any order or in parallel by different reviewers. Tasks 11–12 are the largest content tasks — isolate them for dedicated review. Tasks 13–15 need 1–12 to reference truthfully. Tasks 16–17 are release gates and must run last.

---

## Global Substitution Table (apply to every ported skill/agent/command; referenced by short name below)

| ID | Find | Replace |
|----|------|---------|
| G1 | `ado:ado-<x>` or `ado:<x>` (namespace prefix on cross-plugin references) | `azure-devops:<x>` (with `<x>` also de-prefixed per component mapping, e.g. `ado:ado-mentions` → `azure-devops:mentions`) |
| G2 | Component basenames `ado-work-on`, `ado-publish-pr`, `ado-babysit-pr`, `ado-babysit-pr-worker`, `ado-work-items`, `ado-draft-work-item`, `ado-work-my-backlog`, `ado-mentions`, `ado-devops-assistant`, `ado-pr-tender` | `work-on`, `publish-pr`, `babysit-pr`, `babysit-pr-worker`, `work-items`, `draft-work-item`, `work-my-backlog`, `mentions`, `assistant`, `pr-tender` respectively |
| G3 | "Azure DevOps MCP tools" / "ADO MCP" / "the ADO MCP server" / "MCP tool(s)" (prose, not a rename of a method) | "the bundled `ado-cli.js`" |
| G4 | A bare backtick method-name step (e.g. "Use `createWorkItem` with …") | Same wording kept, followed immediately by a canonical-invocation code block naming that method and the fields already described in the surrounding prose (worked examples in Tasks 6–12) |
| G5 | Any block instructing "if MCP tools unavailable, automatically use `ado:setup-ado-mcp` (`/setup-ado-mcp`), then retry" | Replaced with: "Before the first Azure DevOps CLI call in a session, run `sandbox-auth:azure-devops` (pass the target org if known). If a CLI call fails with `E_AUTH`, run it once and retry. Never ask the user to supply a PAT, token, or credential file." |
| G6 | Any reference to `setup-ado-mcp` / `/setup-ado-mcp` | Removed entirely (R10) |
| G7 | `searchWorkItems` | Per R8: drop the clause where it's offered as an alternative to `listWorkItems`; do not substitute a different method name (exact sites: Task 8, Task 12) |

---

### Task 1: Scaffold plugin + vendor CLI bundle + provenance + method catalog

**Files:**
- Create: `azure-devops/.claude-plugin/plugin.json`
- Create: `azure-devops/scripts/ado-cli.js` (verbatim byte-copy of `B:\sources\claude_plugins\ado\scripts\ado-cli.js`, 3,551,897 bytes, 87,719 lines)
- Create: `azure-devops/references/provenance.json`
- Create: `azure-devops/references/method-catalog.md` (generated, not hand-authored)
- Create empty directories: `azure-devops/{agents,commands,references,skills}`

**Interfaces:**
- Produces: `${CLAUDE_PLUGIN_ROOT}/scripts/ado-cli.js` — the path every other task's canonical-invocation code blocks reference.
- Produces: `references/provenance.json` schema consumed by Task 16 (V2/V8 hash check) and Task 17 (fresh-clone re-verify).

- [ ] **Step 1: Copy the bundle byte-for-byte**

```powershell
New-Item -ItemType Directory -Force "B:\sources\sandbox-plugins\azure-devops\scripts" | Out-Null
Copy-Item "B:\sources\claude_plugins\ado\scripts\ado-cli.js" "B:\sources\sandbox-plugins\azure-devops\scripts\ado-cli.js"
```

- [ ] **Step 2: Write `plugin.json`**

```json
{
  "name": "azure-devops",
  "description": "CLI-native Azure DevOps workflows: work items, PRs, review response, and backlog triage via a bundled ado-cli.js — no MCP server, no PAT (sandbox-auth only).",
  "version": "1.0.0"
}
```

- [ ] **Step 3: Compute hash + write `provenance.json`**

```powershell
$hash = (Get-FileHash "B:\sources\sandbox-plugins\azure-devops\scripts\ado-cli.js" -Algorithm SHA256).Hash
$size = (Get-Item "B:\sources\sandbox-plugins\azure-devops\scripts\ado-cli.js").Length
```

Write `azure-devops/references/provenance.json`:
```json
{
  "file": "scripts/ado-cli.js",
  "origin": "claude_plugins/ado/scripts/ado-cli.js (v3.1.5, esbuild bundle)",
  "sizeBytes": <value of $size, must equal 3551897>,
  "sha256": "<value of $hash>",
  "copiedOn": "2026-08-11",
  "note": "Vendored unmodified. Do not hand-edit. Regenerate provenance.json (this file) whenever the bundle is re-vendored from a newer source build."
}
```

- [ ] **Step 4: Smoke-test the vendored CLI (offline, no env vars)**

```powershell
cd "B:\sources\sandbox-plugins\azure-devops\scripts"
node ado-cli.js
```
Expected: exit **1**, usage text printed (verified against both the vendored copy and `claude_plugins/ado/scripts/ado-cli.js` directly — bare invocation with no method argument exits 1 by design, not 0; only `list --json`/`docs --out`/`help <method>` exit 0 offline). Usage text includes `--pat <token>` in the flag description — this is a leftover flag description in the shared banner text, not a plugin PAT surface; the plugin itself never passes `--pat`.

```powershell
node ado-cli.js list --json
```
Expected: exit 0, JSON array of `{name, category, description}` objects; contains `listWorkItems` and `getWorkItemById`.

```powershell
node ado-cli.js help getWorkItemById
```
Expected: exit 0, Input/Output/Example sections printed; stderr carries the DEP0169 deprecation warning (this is normal, not a failure).

- [ ] **Step 5: Generate `method-catalog.md`**

```powershell
$tmp = New-Item -ItemType Directory -Force "$env:TEMP\ado-docs-gen"
node ado-cli.js docs --out $tmp.FullName
```
Expected: exit 0, exactly **131 files** (130 `<method>-reference.md` + 1 `README.md` index). Do not derive the "130 methods" count from file count minus 1 without checking — always subtract exactly 1 for the index.

Copy `$tmp\README.md` to `azure-devops/references/method-catalog.md` (rename on copy), then delete `$tmp` and its contents. This file is **regenerated every release** (Task 16/17), not a one-time hand-edit — note this in a one-line comment at the top of the copied file: `<!-- generated by: node scripts/ado-cli.js docs --out <dir>; regenerate every release, do not hand-edit -->`.

- [ ] **Step 6: Commit**

```bash
git add azure-devops/.claude-plugin/plugin.json azure-devops/scripts/ado-cli.js azure-devops/references/provenance.json azure-devops/references/method-catalog.md
git commit -m "feat(azure-devops): scaffold plugin, vendor ado-cli.js bundle"
```

---

### Task 2: Verbatim references + mentions skill

**Files:**
- Create: `azure-devops/references/ado-mention-conventions.md` (verbatim copy of `ado/references/ado-mention-conventions.md`)
- Create: `azure-devops/references/review-reception-protocol.md` (verbatim copy of `ado/references/review-reception-protocol.md`)
- Create: `azure-devops/skills/mentions/SKILL.md` (copy of `ado/skills/ado-mentions/SKILL.md`, R4 verbatim except frontmatter `name: ado-mentions` → `name: mentions` and directory rename)

**Interfaces:**
- Consumes: nothing.
- Produces: `azure-devops:mentions` skill name and `references/ado-mention-conventions.md` path — referenced by Tasks 6, 7, 8, 9, 11, 12.

- [ ] **Step 1: Copy the two reference files verbatim** (byte-identical, no text edits — these are pure documentation with no MCP/PAT/prefix content):

```powershell
Copy-Item "B:\sources\claude_plugins\ado\references\ado-mention-conventions.md" "B:\sources\sandbox-plugins\azure-devops\references\ado-mention-conventions.md"
Copy-Item "B:\sources\claude_plugins\ado\references\review-reception-protocol.md" "B:\sources\sandbox-plugins\azure-devops\references\review-reception-protocol.md"
```

- [ ] **Step 2: Port the mentions skill**

Copy `ado/skills/ado-mentions/SKILL.md` (58 lines) to `azure-devops/skills/mentions/SKILL.md`. Edit only the frontmatter `name:` field: `ado-mentions` → `mentions`. Body (Quick summary table, URL patterns table, "load `references/ado-mention-conventions.md`" instruction) is unchanged — the reference path is the same relative path (`references/ado-mention-conventions.md`) in both trees.

- [ ] **Step 3: Verify**

```powershell
Compare-Object (Get-Content "B:\sources\claude_plugins\ado\references\ado-mention-conventions.md") (Get-Content "B:\sources\sandbox-plugins\azure-devops\references\ado-mention-conventions.md")
Compare-Object (Get-Content "B:\sources\claude_plugins\ado\references\review-reception-protocol.md") (Get-Content "B:\sources\sandbox-plugins\azure-devops\references\review-reception-protocol.md")
```
Expected: both produce empty output (no differences).

```powershell
Select-String -Path "B:\sources\sandbox-plugins\azure-devops\skills\mentions\SKILL.md" -Pattern "^name:"
```
Expected: `name: mentions`.

- [ ] **Step 4: Commit**

```bash
git add azure-devops/references/ado-mention-conventions.md azure-devops/references/review-reception-protocol.md azure-devops/skills/mentions/
git commit -m "feat(azure-devops): port references and mentions skill verbatim"
```

---

### Task 3: review-thread-state-machine.md (R7) + new error-codes.md

**Files:**
- Create: `azure-devops/references/review-thread-state-machine.md` (from `ado/references/review-thread-state-machine.md`, 258 lines, with 2 R7 fixes)
- Create: `azure-devops/references/error-codes.md` (new file, content from spec Appendix B verbatim)

**Interfaces:**
- Produces: the restated Rule 1 text — Task 9 (babysit-pr-worker.md `[Resolve]` step) and Task 13 (CLAUDE.md) both cross-reference this exact wording, so it must match verbatim across all three files.

- [ ] **Step 1: Copy the file, then apply exactly 2 text replacements**

Copy `ado/references/review-thread-state-machine.md` to `azure-devops/references/review-thread-state-machine.md` unchanged first, then edit:

Fix A — lines 1-4 (dangling sync note). Replace:
```
> **Synchronized reference** — this lifecycle is copied into the code-reviewer and ADO plugins. The copies must remain byte-identical.
```
with:
```
> This lifecycle document describes the PR-review thread conventions this plugin's `babysit-pr-worker` agent follows; it originated as a shared reference with a `code-reviewer` plugin that is not part of this marketplace.
```

Fix B — Rule 1 (around line 180). Replace:
```
1. **Only the reviewer closes threads** — the developer never resolves or closes threads in ADO
```
with:
```
1. **Threads are closed by the reviewer, or by the autonomous babysit worker once it has applied and verified the requested change.** Interactive/assistive flows never resolve threads on the developer's behalf.
```

Everything else in the file (states table, ASCII transition diagrams, Blocker Classification, Developer-Side/Reviewer-Side Transitions, remaining 11 rules, Question Thread Lifecycle) is copied verbatim — no other edits.

- [ ] **Step 2: Create `references/error-codes.md`** with the exact exit-code and error-name tables from the spec's Appendix B (exit codes 0/1/2/3/4 with their meanings; error vocabulary E_AUTH/E_NOT_FOUND/E_VALIDATION/E_UNKNOWN_METHOD/E_CONFIG/E_RATE_LIMIT/E_BLOCKED/E_UPSTREAM/E_TRANSPORT with one-line descriptions each). Header note: "This is the shared error vocabulary surfaced by `ado-cli.js`'s structured output — not a distinct runtime type. Referenced by CLAUDE.md's retry-on-E_AUTH rule and by babysit-pr-worker's blocker classification."

- [ ] **Step 3: Verify**

```powershell
Select-String -Path "B:\sources\sandbox-plugins\azure-devops\references\review-thread-state-machine.md" -Pattern "code-reviewer and ADO plugins"
```
Expected: no matches (stale sync note removed).

```powershell
Select-String -Path "B:\sources\sandbox-plugins\azure-devops\references\review-thread-state-machine.md" -Pattern "applied and verified"
```
Expected: 1 match (Fix B present).

- [ ] **Step 4: Commit**

```bash
git add azure-devops/references/review-thread-state-machine.md azure-devops/references/error-codes.md
git commit -m "fix(azure-devops): resolve review-thread-state-machine contradiction (R7), add error-codes reference"
```

---

### Task 4: ado-api.mjs port (R9) + verbatim scan/classify/state.mjs

**Files:**
- Create: `azure-devops/skills/work-my-backlog/scripts/ado-api.mjs` (rewrite of `ado/skills/ado-work-my-backlog/scripts/ado-api.mjs`, 497 lines)
- Create: `azure-devops/skills/work-my-backlog/scripts/scan.mjs`, `classify.mjs`, `state.mjs` (verbatim copies — confirmed via grep, zero PAT/BEARER/getAuthHeader/process.env references in these three)

**Interfaces:**
- Consumes: `${CLAUDE_PLUGIN_ROOT}/scripts/ado-cli.js` (Task 1).
- Produces: unchanged exported function signatures — `resolveConfig(repoRoot)`, `getDevIdentity(cwd)`, `getCurrentSprint()`, `querySprintWorkItems()`, `fetchWorkItemChangedDate()`, `fetchWorkItemDetails()`, `fetchWorkItemComments()`, `fetchWorkItemFull()`, `extractLinkedPrIds()`, `fetchPrDetails()`, `isActivePr()`, `fetchUnresolvedThreads()`, `fetchBuildStatus()`, `fetchBuildFailureLogs()`, `fetchPrContext()`, `parseGitRemote()`, `apiUrl()`. Task 5 (work-my-backlog SKILL.md) assumes these names are unchanged.

- [ ] **Step 1: Copy `scan.mjs`, `classify.mjs`, `state.mjs` verbatim** (no transport logic in these three — already confirmed PAT-free):

```powershell
Copy-Item "B:\sources\claude_plugins\ado\skills\ado-work-my-backlog\scripts\scan.mjs" "B:\sources\sandbox-plugins\azure-devops\skills\work-my-backlog\scripts\scan.mjs"
Copy-Item "B:\sources\claude_plugins\ado\skills\ado-work-my-backlog\scripts\classify.mjs" "B:\sources\sandbox-plugins\azure-devops\skills\work-my-backlog\scripts\classify.mjs"
Copy-Item "B:\sources\claude_plugins\ado\skills\ado-work-my-backlog\scripts\state.mjs" "B:\sources\sandbox-plugins\azure-devops\skills\work-my-backlog\scripts\state.mjs"
```

- [ ] **Step 2: Rewrite `ado-api.mjs`'s transport**

Remove `getAuthHeader()` entirely (the function that reads `AZURE_DEVOPS_PAT`/`AZURE_DEVOPS_PERSONAL_ACCESS_TOKEN`/`AZURE_DEVOPS_BEARER_TOKEN` and throws if none set).

Replace `adoFetch(url, options)`'s body (direct `fetch()` with Basic/Bearer auth header) with a helper that spawns the CLI:

```js
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const ADO_CLI_PATH = path.resolve(
  fileURLToPath(import.meta.url),
  '..', '..', '..', '..', 'scripts', 'ado-cli.js'
);

function callAdoCli(method, params) {
  return new Promise((resolve, reject) => {
    const child = spawn('node', [ADO_CLI_PATH, method, '--structured'], {
      stdio: ['pipe', 'pipe', 'pipe'],
    });
    let stdout = '';
    let stderr = '';
    child.stdout.on('data', (d) => { stdout += d; });
    child.stderr.on('data', (d) => { stderr += d; }); // captured for diagnostics only — never treated as failure
    child.on('close', (code) => {
      if (code === 0) {
        try { resolve(JSON.parse(stdout)); }
        catch (e) { reject(new Error(`ado-cli.js returned non-JSON stdout for ${method}: ${stdout.slice(0, 500)}`)); }
      } else {
        reject(new Error(`ado-cli.js ${method} exited ${code}: ${stdout || stderr}`));
      }
    });
    child.stdin.write(JSON.stringify(params));
    child.stdin.end();
  });
}
```

Every former `adoFetch(apiUrl(...), {...})` call site in this file becomes `await callAdoCli('<matchingMethodName>', { ...params matching the method's Zod schema... })`. Map each existing REST call to its CLI method by cross-checking `references/method-catalog.md` (Task 1) for the method whose description matches the endpoint being hit (e.g. the sprint-work-items WIQL query call → `listWorkItems` or `getSprintWorkItems`, whichever the catalog documents for that endpoint shape — verify field names against that method's `help <method>` output rather than guessing).

Fix the **second PAT path**: `fetchBuildFailureLogs()` currently calls `getAuthHeader()` directly (bypassing `adoFetch()`), at the header line `Authorization: getAuthHeader()`. Rewrite this function's body to also go through `callAdoCli('getBuildLog', { ...same params... })` (or the catalog's equivalent build-log method) instead of a raw authenticated fetch.

Delete the `-preview` API-version retry-on-400 quirk logic from `adoFetch()` — that was compensating for direct REST calls; the CLI handles API versioning internally. `apiUrl()` can be deleted if nothing else references it after the rewrite; if `resolveConfig()`/`parseGitRemote()` still need it for non-network purposes (e.g. constructing org URLs for logging), keep those two functions as-is (they don't touch auth).

- [ ] **Step 3: Verify no PAT surface remains**

```powershell
Select-String -Path "B:\sources\sandbox-plugins\azure-devops\skills\work-my-backlog\scripts\ado-api.mjs" -Pattern "AZURE_DEVOPS_PAT|AZURE_DEVOPS_PERSONAL_ACCESS_TOKEN|AZURE_DEVOPS_BEARER_TOKEN|getAuthHeader|Authorization"
```
Expected: 0 matches.

```powershell
node --check "B:\sources\sandbox-plugins\azure-devops\skills\work-my-backlog\scripts\ado-api.mjs"
```
Expected: exit 0 (syntax check only — no network call attempted).

- [ ] **Step 4: Commit**

```bash
git add azure-devops/skills/work-my-backlog/scripts/
git commit -m "feat(azure-devops): port ado-api.mjs onto ado-cli.js transport, remove PAT surface (R9)"
```

---

### Task 5: work-my-backlog SKILL.md port

**Files:**
- Create: `azure-devops/skills/work-my-backlog/SKILL.md` (from `ado/skills/ado-work-my-backlog/SKILL.md`, 215 lines)

**Interfaces:**
- Consumes: `azure-devops:work-on`, `azure-devops:babysit-pr-worker` (component names from Tasks 11, 9).

- [ ] **Step 1: Copy then apply the Global Substitution Table (G1, G2)** plus these file-specific fixes:

Frontmatter: `name: ado-work-my-backlog` → `name: work-my-backlog`.

Header comment referencing `/loop 15m /ado-work-my-backlog` → `/loop 15m /work-my-backlog`.

**R9 SKILL.md prose fix** (Phase 0.1, "Run the Scan" section) — this is the residual-PAT finding: the line
```
Auth: requires `AZURE_DEVOPS_PAT` environment variable (or `AZURE_DEVOPS_BEARER_TOKEN`)
```
must become:
```
Auth: none required from you. Run `sandbox-auth:azure-devops` once at session start (see CLAUDE.md); the scan script authenticates through the bundled `ado-cli.js`, never via a PAT.
```

"When NOT to Use" section: `/ado-work-on <id>` → `/work-on <id>`; `/ado-babysit-pr <pr-id>` → `/babysit-pr <pr-id>`; `/ado-draft-work-item` → `/draft-work-item`; `/ado-work-items` → `/work-items`.

Any `ado:ado-work-on` agent-spawn reference → `azure-devops:work-on`; `ado:ado-babysit-pr-worker` → `azure-devops:babysit-pr-worker`.

Any `ado:setup-ado-mcp` reference → apply G5/G6 (replace with sandbox-auth warm-up rule, or remove if merely a fallback mention already covered by CLAUDE.md).

- [ ] **Step 2: Verify**

```powershell
Select-String -Path "...\skills\work-my-backlog\SKILL.md" -Pattern "AZURE_DEVOPS_PAT|AZURE_DEVOPS_BEARER_TOKEN|AZURE_DEVOPS_PERSONAL_ACCESS_TOKEN"
Select-String -Path "...\skills\work-my-backlog\SKILL.md" -Pattern "ado:|ado-work-on|ado-babysit-pr|ado-draft-work-item|ado-work-items|setup-ado-mcp"
```
Expected: both return 0 matches.

- [ ] **Step 3: Commit**

```bash
git add azure-devops/skills/work-my-backlog/SKILL.md
git commit -m "feat(azure-devops): port work-my-backlog skill (R2, R8-adjacent PAT-prose fix, R9)"
```

---

### Task 6: publish-pr SKILL.md port

**Files:**
- Create: `azure-devops/skills/publish-pr/SKILL.md` (from `ado/skills/ado-publish-pr/SKILL.md`, 177 lines)

- [ ] **Step 1: Copy then apply G1–G5** plus these exact call-site conversions (worked example for Step 1.3, then apply identically to the rest):

Before:
```
Use `createWorkItem` with the confirmed type, title, and description.
```
After:
```
Use `createWorkItem` with the confirmed type, title, and description:

```bash
node "${CLAUDE_PLUGIN_ROOT}/scripts/ado-cli.js" createWorkItem --structured <<'ADOJSON'
{ "type": "<Bug|Task|User Story>", "title": "<confirmed title>", "description": "<confirmed description>" }
ADOJSON
```
```

Apply the same pattern (wording unchanged, canonical-invocation block appended, fields drawn from that step's own already-described parameters) to these remaining call sites in this file:
- Step 2.3: `createPullRequest` (fields: source branch, target branch, title, description).
- Step 2.4: `createLink` (fields: work item id, PR id/url, link type).

Phase-3 delegation lines: `ado:ado-pr-tender` → `azure-devops:pr-tender`; `ado:ado-babysit-pr` → `azure-devops:babysit-pr`.

"ADO Reference Conventions" section: `ado:ado-mentions` → `azure-devops:mentions`.

Guidelines line: "use Azure DevOps MCP tools for all ADO operations" → "use the bundled `ado-cli.js` for all ADO operations" (G3).

Any MCP-prerequisite/auto-setup block → G5/G6.

- [ ] **Step 2: Verify**

```powershell
Select-String -Path "...\skills\publish-pr\SKILL.md" -Pattern "ado:|MCP|setup-ado-mcp"
```
Expected: 0 matches.

```powershell
(Select-String -Path "...\skills\publish-pr\SKILL.md" -Pattern "ado-cli\.js" -AllMatches).Matches.Count
```
Expected: >= 3 (one per converted call site).

- [ ] **Step 3: Commit**

```bash
git add azure-devops/skills/publish-pr/SKILL.md
git commit -m "feat(azure-devops): port publish-pr skill onto CLI invocations (R1, R2)"
```

---

### Task 7: babysit-pr SKILL.md port

**Files:**
- Create: `azure-devops/skills/babysit-pr/SKILL.md` (from `ado/skills/ado-babysit-pr/SKILL.md`, 277 lines)

**Interfaces:**
- Consumes: `azure-devops:babysit-pr-worker` (Task 9), `references/review-thread-state-machine.md` + `references/review-reception-protocol.md` (Tasks 2, 3) — same relative reference paths as source, unchanged.

- [ ] **Step 1: Copy then apply G1–G5**, converting these call sites (worked example for the Entry section, then apply identically):

Before:
```
Entry: detect from current branch using `listPullRequests`
```
After:
```
Entry: detect from current branch using `listPullRequests`:

```bash
node "${CLAUDE_PLUGIN_ROOT}/scripts/ado-cli.js" listPullRequests --structured <<'ADOJSON'
{ "sourceRefName": "refs/heads/<current-branch>", "status": "active" }
ADOJSON
```
```

Apply identically to: `getPullRequest` (entry step 2, loop step 1 — reuse the same block), `getWorkItemById` (entry step 4), `getTeams` (entry step 4), `getPullRequestComments` (loop step 2f).

"Spawn the `ado:ado-babysit-pr-worker` agent" → "Spawn the `azure-devops:babysit-pr-worker` agent".

"ADO Reference Conventions": `ado:ado-mentions` → `azure-devops:mentions`.

Tools section at file bottom — rewrite the header from "Azure DevOps MCP: getPullRequest, getPullRequestComments, ..." to "Azure DevOps CLI methods (via `ado-cli.js --structured`): getPullRequest, getPullRequestComments, ..." — method-name list unchanged.

- [ ] **Step 2: Verify**

```powershell
Select-String -Path "...\skills\babysit-pr\SKILL.md" -Pattern "ado:|MCP|setup-ado-mcp"
```
Expected: 0 matches.

- [ ] **Step 3: Commit**

```bash
git add azure-devops/skills/babysit-pr/SKILL.md
git commit -m "feat(azure-devops): port babysit-pr skill onto CLI invocations (R1, R2)"
```

---

### Task 8: work-items SKILL.md port (R8)

**Files:**
- Create: `azure-devops/skills/work-items/SKILL.md` (from `ado/skills/ado-work-items/SKILL.md`, 66 lines)

- [ ] **Step 1: Copy then apply G1–G5.** Convert call sites: Create (`getCurrentSprint`, `createWorkItem`), Query (line 27 — R8 fix, see below), Update (`getWorkItemById`, `updateWorkItem`/`updateWorkItemState`), Link (`createLink`), Sprint Management (`getSprints`/`getCurrentSprint`, `getSprintWorkItems`, `updateWorkItem`).

**R8 fix (line 27)** — before:
```
2. Run `listWorkItems` or `searchWorkItems`.
```
after:
```
2. Run `listWorkItems`:

```bash
node "${CLAUDE_PLUGIN_ROOT}/scripts/ado-cli.js" listWorkItems --structured <<'ADOJSON'
{ "wiql": "<query matching the described filters>" }
ADOJSON
```
```
(`searchWorkItems` does not exist among the CLI's 130 methods; drop the clause rather than substitute a different name — per R8.)

`ado:ado-mentions` → `azure-devops:mentions`. Guidelines line "Use the Azure DevOps MCP tools (listWorkItems, createWorkItem, updateWorkItem, etc.)" → "Use the bundled `ado-cli.js` (listWorkItems, createWorkItem, updateWorkItem, etc.)".

- [ ] **Step 2: Verify**

```powershell
Select-String -Path "...\skills\work-items\SKILL.md" -Pattern "searchWorkItems"
```
Expected: 0 matches.

```powershell
Select-String -Path "...\skills\work-items\SKILL.md" -Pattern "ado:|MCP"
```
Expected: 0 matches.

- [ ] **Step 3: Commit**

```bash
git add azure-devops/skills/work-items/SKILL.md
git commit -m "fix(azure-devops): port work-items skill, drop stale searchWorkItems reference (R8)"
```

---

### Task 9: Agents port (3 files)

**Files:**
- Create: `azure-devops/agents/assistant.md` (from `ado/agents/ado-devops-assistant.md`, 56 lines)
- Create: `azure-devops/agents/pr-tender.md` (from `ado/agents/ado-pr-tender.md`, 81 lines)
- Create: `azure-devops/agents/babysit-pr-worker.md` (from `ado/agents/ado-babysit-pr-worker.md`, 299 lines)

**Interfaces:**
- Produces: `azure-devops:pr-tender`, `azure-devops:babysit-pr-worker` component names consumed by Tasks 6, 7, 10.
- Consumes: `references/review-thread-state-machine.md` (Task 3) — babysit-pr-worker's `[Resolve]` step must match Task 3's Fix B wording.

- [ ] **Step 1: assistant.md** — frontmatter `name: ado-devops-assistant` → `name: assistant`; keep `modelintelligence: 1`, `effort: xhigh`; `skills: [ado-mentions]` → `skills: [mentions]`. Routing table: `ado:ado-work-items` → `azure-devops:work-items`, `ado:ado-publish-pr` → `azure-devops:publish-pr`, `ado:ado-pr-tender` → `azure-devops:pr-tender`. `<bot_identity>` tag kept verbatim. Sprint/team direct-handling lines ("Handle directly using getSprints, getCurrentSprint, getSprintWorkItems / getTeams, getTeamMembers") → append "(via the bundled `ado-cli.js`)"; no per-call code block required here since this is a summary line, not a step-by-step call site (this is the one exception to G4 — a routing-table summary reference, not an executable step). Guidelines: "Use Azure DevOps MCP tools for all DevOps operations" → G3 substitution.

- [ ] **Step 2: pr-tender.md** — frontmatter `name: ado-pr-tender` → `name: pr-tender`; `skills: [ado-mentions]` → `skills: [mentions]`. Convert workflow call sites (`listPullRequests`, `getPullRequest`, `getPullRequestComments`, `getPullRequestFileChanges`, `getAllPullRequestChanges`, `replyToComment`, `updatePullRequestThread`, `getWorkItemById`, `addWorkItemComment`) using the same worked-example pattern as Task 6/7. Keep `<do_not_resolve>Do NOT resolve comment threads — let the reviewer resolve them.</do_not_resolve>` **verbatim, unchanged** — R7(b) explicitly preserves this for the interactive tender (it never resolves). Reference link `references/review-thread-state-machine.md` — path unchanged. Tools list header: MCP → CLI wording (G3).

- [ ] **Step 3: babysit-pr-worker.md** — frontmatter `name: ado-babysit-pr-worker` → `name: babysit-pr-worker`; keep `modelintelligence: 5`, `effort: high`; `skills: [ado-mentions]` → `skills: [mentions]`. Convert call sites (`updatePullRequestThread`, `createWorkItem`, `replyToComment`, plus the Fix Issues / Build & Test / Commit & Push section's method mentions). `ado:ado-babysit-pr` → `azure-devops:babysit-pr` (do-not-use note); any `ado:ado-draft-work-item` mention → `azure-devops:draft-work-item`. `<blocker_policy>`, Self-Review section, `<max_retries>3</max_retries>` kept verbatim.

**R7(b) explicit addition** to the `[Resolve]` step — insert this sentence (matching Task 3's Fix B wording exactly) immediately before the existing "After all code fixes and replies are posted, resolve comment threads…" line:
```
Only resolve a thread after the fix has been applied AND verified (self-review + build/test pass) for it — never resolve on intent alone.
```
This makes the precondition explicit rather than merely implied by step ordering.

- [ ] **Step 4: Verify (all 3 files)**

```powershell
Select-String -Path "...\agents\*.md" -Pattern "^model:"
```
Expected: 0 matches (frontmatter uses `modelintelligence`/`effort`, never `model:`).

```powershell
Select-String -Path "...\agents\*.md" -Pattern "modelintelligence:|effort:"
```
Expected: 2 matches per file (6 total).

```powershell
Select-String -Path "...\agents\*.md" -Pattern "ado-mentions|ado:"
```
Expected: 0 matches.

```powershell
Select-String -Path "...\agents\pr-tender.md" -Pattern "do_not_resolve"
```
Expected: 1 match, text unchanged from source.

```powershell
Select-String -Path "...\agents\babysit-pr-worker.md" -Pattern "applied AND verified"
```
Expected: 1 match.

- [ ] **Step 5: Commit**

```bash
git add azure-devops/agents/
git commit -m "feat(azure-devops): port 3 agents (assistant, pr-tender, babysit-pr-worker) — R1, R2, R7, R12"
```

---

### Task 10: Commands port (5 files)

**Files:**
- Create: `azure-devops/commands/work-on.md` (from `ado/commands/ado-work-on.md`, 11 lines)
- Create: `azure-devops/commands/publish-pr.md` (from `ado/commands/ado-publish-pr.md`, 8 lines)
- Create: `azure-devops/commands/babysit-pr.md` (from `ado/commands/ado-babysit-pr.md`, 8 lines)
- Create: `azure-devops/commands/draft-work-item.md` (from `ado/commands/ado-draft-work-item.md`, 11 lines)
- Create: `azure-devops/commands/work-my-backlog.md` (from `ado/commands/ado-work-my-backlog.md`, 10 lines)
- **Drop**: `ado/commands/setup-ado-mcp.md` (R10 — not ported; the target has no MCP server to set up)

**Interfaces:**
- Consumes: component names from Tasks 5–9, 11, 12.

- [ ] **Step 1: Port each of the 5 files.** Each is a thin dispatcher; the only edits are (a) skill/agent reference renames (G1: e.g. "Load and execute the **ado:ado-work-on** skill (`skills/ado-work-on/SKILL.md`)." → "Load and execute the **azure-devops:work-on** skill (`skills/work-on/SKILL.md`).") and (b) filename-in-prose updates matching the new directory layout. No frontmatter exists on command files (per repo convention — plain Markdown, filename = command name), so no frontmatter edits apply.

- [ ] **Step 2: Verify**

```powershell
Select-String -Path "...\commands\*.md" -Pattern "ado:|ado-work-on|ado-publish-pr|ado-babysit-pr|ado-draft-work-item|ado-work-my-backlog|setup-ado-mcp"
```
Expected: 0 matches across all 5 files.

```powershell
Get-ChildItem "...\commands\" -Filter "*.md" | Measure-Object
```
Expected: 5 files (setup-ado-mcp.md absent).

- [ ] **Step 3: Commit**

```bash
git add azure-devops/commands/
git commit -m "feat(azure-devops): port 5 commands, drop setup-ado-mcp (R10)"
```

---

### Task 11: work-on inline rewrite (R6) + reference guides

**Files:**
- Create: `azure-devops/skills/work-on/SKILL.md` (rewrite — starts from `development/skills/work-on/SKILL.md`, 640 lines, ADO column only; NOT copied from the 42-line `ado/skills/ado-work-on/SKILL.md` thin wrapper, which is discarded)
- Create: `azure-devops/skills/work-on/reference/bug-rca-workflow.md`, `decision-log-guide.md`, `plan-comment-format.md`, `rca-comment-format.md` (verbatim, from `development/skills/work-on/reference/`)
- Create: `azure-devops/skills/work-on/reference/git-worktrees-guide.md`, `branch-completion-guide.md` (verbatim, from `development/reference/`)
- Create: `azure-devops/references/ado-state-transitions.md` (verbatim, from `development/skills/work-on/reference/ado-state-transitions.md` — top-level `references/`, not `skills/work-on/reference/`, per the file inventory)
- **Do not copy**: `development/skills/work-on/reference/issue-lifecycle.md` (GitHub-only column, not applicable)

**Interfaces:**
- Consumes: `azure-devops:mentions` (Task 2), `references/ado-state-transitions.md` (this task, self-produced), `sandbox-auth:azure-devops` (session warm-up, Task 13's CLAUDE.md).
- Produces: `azure-devops:work-on` skill invoked by `commands/work-on.md` (Task 10) and `skills/work-my-backlog/SKILL.md` (Task 5).

- [ ] **Step 1: Start from `development/skills/work-on/SKILL.md` and collapse every `| GitHub | Azure DevOps |` table to the ADO column only** (drop the GitHub column and its header cell in each table — do this table-by-table, do not merge or reorder the surrounding phases). Every phase header (Phase 0, 0.1, 1, 1.1, 1.2, 1.2-Bug, 1.2-Feature Stage A, 1.2-Feature Stage B, 1.3, 1.4, 1.5, 1.6, PART 1 Revision Mode R.1–R.3, PART 2 Phase 2.1–2.5, Error Handling, Reference Conventions, Decision Log, Task Decomposition, Guidelines) is kept, unrenamed, unmerged, unreordered.

- [ ] **Step 2: Phase 0 — replace provider auto-detect with a no-op single-provider statement.** Before (paraphrased): "Detect explicit provider from args, else infer from git remote (github.com → GitHub, dev.azure.com/visualstudio.com → Azure DevOps)." After:
```
This plugin only serves Azure DevOps. Skip provider detection — the provider is always Azure DevOps.
```
Phase 0.1 (Parse Arguments) is otherwise unchanged.

- [ ] **Step 3: Replace the tooling-readiness block.** Before (paraphrased): "GitHub → verify `gh` CLI / MCP, else run `gh:setup-gh-mcp`. Azure DevOps → verify ADO MCP tools, else run `ado:setup-ado-mcp`." After (G5, same wording as CLAUDE.md's warm-up rule — Task 13):
```
Before the first Azure DevOps CLI call in this session, run `sandbox-auth:azure-devops` (pass the target org if known). If a CLI call later fails with `E_AUTH`, run it once more and retry. Never ask the user to supply a PAT, token, or credential file.
```

- [ ] **Step 4: Convert every remaining ADO-column method mention to canonical invocations** (G4), using the worked-example pattern from Task 6.

**Exception, tied to Appendix A's Tier-1 removal list** — in the Stage A "research toolkit" tables (related work items / build & pipeline / wiki & documentation / recent PRs), do **not** hardcode `getDefinitions`, `listWikis`, `getWikiPageContent`, `getCommitHistory`, `browseRepository`, or `getFileContent` as literal method names — the spec's Appendix A confirms none of these six are referenced by any other ported file, and hardcoding them here would make them Tier 1 by accident with no other justification. Instead, genericize those specific rows to category-level guidance, e.g.:
```
Browse repo structure and commit history, and check ADO wiki content, as needed — use `node "${CLAUDE_PLUGIN_ROOT}/scripts/ado-cli.js" list --json` to find the applicable Git/Wiki-category method and `help <method>` for its parameters, rather than a hardcoded call here.
```
Keep `getWorkItemById` (related work items) and `listPullRequests` (recently merged PRs) as ordinary Tier-1 canonical invocations — they are already referenced elsewhere.

- [ ] **Step 5: PART 1 Revision Mode (R.1–R.3, 3-revision cap) and PART 2 Execute & Deliver (2.1–2.5)** — copy unchanged aside from the method-invocation and reference-path conversions already covered by Steps 4/1.

- [ ] **Step 6: Reference Conventions section** — `ado:ado-mentions` → `azure-devops:mentions`. Reference paths: `reference/issue-lifecycle.md` (drop — GitHub-only) becomes no entry; `reference/ado-state-transitions.md` reference path becomes `../../references/ado-state-transitions.md` (one level up from `skills/work-on/reference/` to `references/`, matching the new top-level location) — update the relative path accordingly, since this file moves out of the per-skill `reference/` folder into the shared top-level `references/` folder while the other 6 guides stay per-skill.

- [ ] **Step 7: Copy the 6 reference guides + ado-state-transitions.md verbatim**

```powershell
$devWorkOnRef = "B:\sources\claude_plugins\development\skills\work-on\reference"
$devTopRef = "B:\sources\claude_plugins\development\reference"
$target = "B:\sources\sandbox-plugins\azure-devops\skills\work-on\reference"
New-Item -ItemType Directory -Force $target | Out-Null
Copy-Item "$devWorkOnRef\bug-rca-workflow.md","$devWorkOnRef\decision-log-guide.md","$devWorkOnRef\plan-comment-format.md","$devWorkOnRef\rca-comment-format.md" $target
Copy-Item "$devTopRef\git-worktrees-guide.md","$devTopRef\branch-completion-guide.md" $target
Copy-Item "$devWorkOnRef\ado-state-transitions.md" "B:\sources\sandbox-plugins\azure-devops\references\ado-state-transitions.md"
```

- [ ] **Step 8: Verify**

```powershell
Select-String -Path "...\skills\work-on\SKILL.md" -Pattern "development:|gh:|GitHub|setup-ado-mcp|setup-gh-mcp"
```
Expected: 0 matches.

```powershell
(Select-String -Path "...\skills\work-on\SKILL.md" -Pattern "^#|^##|^###" -AllMatches).Matches.Count
```
Expected: equal to the phase/section header count in the source `development/skills/work-on/SKILL.md` (same command, run against the source file, to diff counts — they must match exactly, confirming no phase was merged/split/dropped).

> **Note for integration (fix round 1, this branch only):** the command above is
> fence-blind — it counts any line starting with `#` even inside a fenced code
> block (e.g. a template the skill instructs the agent to write into
> `decisions.md`), so a heading-like example line inside a code fence is
> indistinguishable from a real phase header. It happened to catch a real
> defect once (headings flattened to satisfy this exact count) and could just
> as easily reward the same workaround again. Prefer a fence-aware count when
> this gate is centralized for Task 16/17, e.g. (PowerShell, strips fenced
> ranges before counting):
> ```powershell
> $lines = Get-Content "...\skills\work-on\SKILL.md"
> $inFence = $false
> ($lines | Where-Object {
>   if ($_ -match '^```') { $inFence = -not $inFence; return $false }
>   -not $inFence -and $_ -match '^#|^##|^###'
> }).Count
> ```
> Verified fence-aware for this branch: 35 = 35 (headings restored under
> `### Phase 2.3`; the fenced Purpose & Consumption template is indented 3
> spaces, matching `development/skills/implement/SKILL.md`'s own convention,
> so its `##`/`###` lines don't start at column 0 and are excluded from either
> count form). Not applied as a blanket edit to Step 8 above since other
> ported files may rely on the simpler command as-is; flagging for Task 16/17
> to decide whether to centralize the fence-aware version.

```powershell
Get-ChildItem "...\skills\work-on\reference\" -Filter "*.md" | Measure-Object
```
Expected: 6 files. `issue-lifecycle.md` absent.

- [ ] **Step 9: Commit**

```bash
git add azure-devops/skills/work-on/ azure-devops/references/ado-state-transitions.md
git commit -m "feat(azure-devops): inline work-on skill from development plugin, ADO column only (R6)"
```

---

### Task 12: draft-work-item inline rewrite (R6)

**Files:**
- Create: `azure-devops/skills/draft-work-item/SKILL.md` (rewrite — starts from `development/skills/draft-work-item/SKILL.md`, 192 lines, ADO column only; NOT copied from the 40-line `ado/skills/ado-draft-work-item/SKILL.md` thin wrapper, which is discarded)

**Interfaces:**
- Consumes: `azure-devops:mentions` (Task 2), `references/ado-mention-conventions.md` (Task 2 — this file already lives natively under the ADO plugin, verbatim copy, not sourced from `development/`).

- [ ] **Step 1: Start from `development/skills/draft-work-item/SKILL.md`.** Phase 0 (provider resolution) → same no-op single-provider statement as Task 11 Step 2. Phase 0b (tooling) → same G5 warm-up-rule replacement as Task 11 Step 3.

- [ ] **Step 2: Phase 1 (classify & route)** — the source routes to `development:draft-feature` / `development:draft-bug` for the full path, or handles Quick-Path inline. Per R6, inline the `draft-feature`/`draft-bug` logic (same classification questions, same drafting steps) directly into this file's Phase 1 rather than naming an external skill delegation — same content, executed via ordinary agent reasoning.

- [ ] **Step 3: Phase 2 (Quick Path wizard)** — the source dispatches `development:blind-spot-detector` for a gap-check pass. Inline this as an explicit "review your draft for these gap categories" instruction using the same categories that skill checks (do not invent new categories — carry over the exact list from `development:blind-spot-detector`'s SKILL.md if distinct from Quick Path's own list; if you cannot locate a categories list distinct from what's already in Phase 2, treat blind-spot-detector's role here as "re-read the draft once more against Phase 2's own checklist before finalizing" and say so explicitly rather than inventing categories).

- [ ] **Step 4: Phase 3 (Finalize, sub-phases 3.1–3.5)** — convert method mentions (G4). **R8 fix** in the Duplicate Check table (3.x): before —
```
Call `searchWorkItems` with the proposed title
```
after —
```
Call `listWorkItems` with the proposed title:

```bash
node "${CLAUDE_PLUGIN_ROOT}/scripts/ado-cli.js" listWorkItems --structured <<'ADOJSON'
{ "wiql": "SELECT [System.Id] FROM WorkItems WHERE [System.Title] CONTAINS '<proposed title>'" }
ADOJSON
```
```

- [ ] **Step 5: Phase 4 (Create) and Follow-up** — convert `createWorkItem` call site (G4). `reference/gh-mention-conventions.md` (GitHub, drop). `reference/ado-mention-conventions.md` reference → path becomes `../../references/ado-mention-conventions.md` (top-level shared references, Task 2 — not a per-skill `reference/` copy, since this file already lives at the shared top level).

- [ ] **Step 6: Verify**

```powershell
Select-String -Path "...\skills\draft-work-item\SKILL.md" -Pattern "development:|gh:|GitHub|searchWorkItems|setup-ado-mcp"
```
Expected: 0 matches.

- [ ] **Step 7: Commit**

```bash
git add azure-devops/skills/draft-work-item/
git commit -m "feat(azure-devops): inline draft-work-item skill from development plugin, fix stale searchWorkItems (R6, R8)"
```

---

### Task 13: CLAUDE.md authoring (R3)

**Files:**
- Create: `azure-devops/CLAUDE.md`

**Interfaces:**
- Consumes: Task 3's Fix B wording (must match verbatim), Task 1's canonical invocation path.
- Produces: the warm-up rule text every skill's G5 substitution references — must match verbatim across all consuming files (Tasks 5–12).

- [ ] **Step 1: Write the file with these exact sections** (condensed from the source's 27-line `ado/CLAUDE.md`, plus 3 new jobs assigned by R3 — this file is allowed to exceed the repo convention's "~120 lines max" guidance because R3 explicitly assigns it more jobs than an ordinary plugin CLAUDE.md; keep it table-dense, not prose-padded):

```markdown
# Azure DevOps Plugin

## Canonical Invocation Form

Every Azure DevOps operation in this plugin goes through:

```bash
node "${CLAUDE_PLUGIN_ROOT}/scripts/ado-cli.js" <method> --structured <<'ADOJSON'
{ ...params matching the method's schema, see references/method-catalog.md... }
ADOJSON
```

Rules:
1. Always pass `--structured`.
2. JSON body via stdin only. Never `--input`, and never `--input -` (fails with `ENOENT` trying to open a file literally named `-`).
3. Use a quoted heredoc (`<<'ADOJSON' ... ADOJSON`) — not `echo '...' |`, which breaks on apostrophes and does not neutralize `$`/backticks.
4. `${CLAUDE_PLUGIN_ROOT}` is substituted by Claude Code before the shell runs. Keep it double-quoted. Never `export` it or derive it via `dirname "$0"`.
5. Branch only on exit code and stdout. Non-empty stderr is not a failure — it always carries an auth banner and Node's DEP0169 deprecation warning, even on success.

## Authentication — sandbox-auth only, zero PAT surface

Before the first Azure DevOps CLI call in a session, run `sandbox-auth:azure-devops` (pass the target org if known). If a CLI call fails with `E_AUTH`, run it once and retry. **Never ask the user to supply a PAT, token, or credential file** — this plugin has no PAT interface anywhere.

## Proxy and CA preconditions (session-level, not per-call)

| Variable | Requirement |
|---|---|
| `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY` | Uppercase only — no lowercase fallback. Read natively by the CLI's HTTP client. |
| `NODE_EXTRA_CA_CERTS` | Must be set **before** `node` starts, from whichever CA bundle var is already present (e.g. a Python/curl CA var). Node ignores `REQUESTS_CA_BUNDLE` / `SSL_CERT_FILE` / `CURL_CA_BUNDLE`. |

## Mutation Policy (3 tiers — enforced by instruction, not code)

| Tier | Scope | Rule |
|---|---|---|
| 1 | Ordinary/referenced methods | Proceed normally. |
| 2 | `manageWorkItemComment` update/delete | **Forbidden.** Never invoke. Post a new comment instead. |
| 3 | Destructive methods (e.g. `mergePullRequest`) or any out-of-scope method not named in any ported file | **Confirm with the user first.** |

## Comments Are Append-Only

**NEVER delete, update, or edit existing Azure DevOps work item or PR comments.** Always post NEW comments. This preserves the full conversation history and audit trail. Revised plans, follow-up answers, and status updates are all new comments — never edits to previous ones. This rule applies to all skills and agents in this plugin.

## Review Thread Resolution

Threads are closed by the reviewer, or by the autonomous babysit worker once it has applied and verified the requested change. Interactive/assistive flows never resolve threads on the developer's behalf. See `references/review-thread-state-machine.md`.

## Privacy

Never dump the environment (`env`, `printenv`, `echo "$HTTP_PROXY"`-style output) into chat, logs, or comments. Never reference a PAT/token env var name outside this negative instruction.
```

- [ ] **Step 2: Verify**

```powershell
Select-String -Path "...\azure-devops\CLAUDE.md" -Pattern "sandbox-auth:azure-devops"
Select-String -Path "...\azure-devops\CLAUDE.md" -Pattern "applied and verified"
Select-String -Path "...\azure-devops\CLAUDE.md" -Pattern "NEVER delete, update, or edit"
Select-String -Path "...\azure-devops\CLAUDE.md" -Pattern "AZURE_DEVOPS_PAT|--pat "
```
Expected: first 3 return 1+ match each; last returns 0 matches (no PAT variable/flag named outside the Privacy section's own negative instruction, which itself does not name the specific var).

- [ ] **Step 3: Commit**

```bash
git add azure-devops/CLAUDE.md
git commit -m "docs(azure-devops): author CLAUDE.md — invocation form, auth warm-up, mutation policy (R3)"
```

---

### Task 14: README.md authoring

**Files:**
- Create: `azure-devops/README.md`

- [ ] **Step 1: Write the file mirroring `sandbox-auth/README.md`'s structure** (Quick start / skills table / canonical invocation form / error table / license), populated with this plugin's actual content:

- Quick start: `/plugin install azure-devops@sandbox-plugins-marketplace`, then `sandbox-auth:azure-devops` once per session.
- Skills table (8 rows): `work-on`, `publish-pr`, `babysit-pr`, `work-items`, `draft-work-item`, `work-my-backlog`, `mentions` — 7 skills — plus note that 3 agents (`assistant`, `pr-tender`, `babysit-pr-worker`) and 5 commands exist alongside them (do not miscount — 7 skills, not 8; the 8th source skill `setup-ado-mcp` was dropped per R10).
- Canonical invocation form section — copy Task 13's "Canonical Invocation Form" block verbatim (same 5 rules), since this is the README's job to explain to a human reader, while CLAUDE.md's job is to enforce it for the agent.
- Error table — copy `references/error-codes.md` (Task 3) content, exit codes 0–4 + 9 named errors (E_AUTH, E_NOT_FOUND, E_VALIDATION, E_UNKNOWN_METHOD, E_CONFIG, E_RATE_LIMIT, E_BLOCKED, E_UPSTREAM, E_TRANSPORT).
- Explicit banner: "No PAT. No MCP server. No adapter/wrapper library. Sandbox-auth only."
- License section (match `sandbox-auth/README.md`'s license wording).

- [ ] **Step 2: Verify**

```powershell
(Select-String -Path "...\azure-devops\README.md" -Pattern "^\| \`" -AllMatches).Matches.Count
```
Manually confirm: skills table has 7 data rows, error table has 9 data rows (count them against the pattern match count — table row markup may vary, so a manual line-count check of the two tables is acceptable here in place of a single automated assertion).

- [ ] **Step 3: Commit**

```bash
git add azure-devops/README.md
git commit -m "docs(azure-devops): author plugin README"
```

---

### Task 15: Marketplace + root docs update

**Files:**
- Modify: `B:\sources\sandbox-plugins\.claude-plugin\marketplace.json`
- Modify: `B:\sources\sandbox-plugins\README.md`

- [ ] **Step 1: Append the third `plugins` array entry** (after the existing `sandbox-auth` and `sandbox` entries), matching their exact field shape:

```json
{
  "name": "azure-devops",
  "source": "./azure-devops",
  "description": "CLI-native Azure DevOps workflows — work items, PR publish/babysit/tend, and backlog triage — via a bundled ado-cli.js executed as a child process. No MCP server, no PAT interface; authenticates through sandbox-auth:azure-devops.",
  "version": "1.0.0",
  "category": "development",
  "tags": ["azure-devops", "work-items", "pull-requests", "code-review", "backlog"],
  "keywords": ["ado-cli", "azure-devops-work-items", "azure-devops-pr", "backlog-triage", "code-review-response"]
}
```

- [ ] **Step 2: Update root `README.md`**

In `## Available Plugins`, add a new subsection after `### sandbox-auth (v2.1.1)`:
```markdown
### azure-devops (v1.0.0)

CLI-native Azure DevOps workflows: work items, PR publish/babysit/tend, review-thread response,
and backlog triage. Every operation runs through a bundled `ado-cli.js`, spawned as a child
process — no MCP server, no PAT interface. Authenticates via `sandbox-auth:azure-devops`.
```

In `## Extensibility`, change:
```
This marketplace is expected to grow with plugins named for what they do — for example `email`,
`collaboration`, `azure-devops` — that happen to run inside a sandboxed agent session.
```
to:
```
This marketplace is expected to grow with plugins named for what they do — for example `email`,
`collaboration` — that happen to run inside a sandboxed agent session. `azure-devops` is one such
plugin, already present above, not merely hypothetical.
```

In `## Repository Structure`, add a line for the new directory:
```
├── azure-devops/           # CLI-native Azure DevOps plugin
```
(insert between the `sandbox-auth/` and `scratchpad/` lines, matching alphabetical-ish existing order).

- [ ] **Step 3: Verify**

```powershell
Get-Content "B:\sources\sandbox-plugins\.claude-plugin\marketplace.json" -Raw | ConvertFrom-Json | Select-Object -ExpandProperty plugins | Measure-Object
```
Expected: `Count` = 3.

```powershell
(Get-Content "B:\sources\sandbox-plugins\.claude-plugin\marketplace.json" -Raw | ConvertFrom-Json).plugins[2].name
```
Expected: `azure-devops`.

- [ ] **Step 4: Commit**

```bash
git add .claude-plugin/marketplace.json README.md
git commit -m "docs: register azure-devops plugin in marketplace and root README"
```

---

### Task 16: Release verification gates V1–V8

No files created or modified in this task — verification only, run in the order below. Any failure blocks Task 17.

- [ ] **V1 — Plugin validates**
```powershell
claude plugin validate "B:\sources\sandbox-plugins\azure-devops"
```
Expected: exit 0 / "valid" output, no schema errors.

- [ ] **V2 — Bundled CLI intact + catalog fresh**
```powershell
cd "B:\sources\sandbox-plugins\azure-devops\scripts"
node ado-cli.js                     # expect exit 1, usage text printed (bare invocation with no method argument exits 1 by design; see Task 1 Step 4)
node ado-cli.js list --json         # expect exit 0, JSON array
$tmp2 = New-Item -ItemType Directory -Force "$env:TEMP\ado-docs-verify"
node ado-cli.js docs --out $tmp2.FullName   # expect exit 0, 131 files
node ado-cli.js help getWorkItemById # expect exit 0
```
Then confirm `provenance.json`'s recorded size/hash still match:
```powershell
$hash2 = (Get-FileHash "ado-cli.js" -Algorithm SHA256).Hash
```
Expected: equals the value stored in `references/provenance.json` (Task 1).
Diff the freshly generated `$tmp2\README.md` against `references/method-catalog.md` (ignoring the one-line generator comment added in Task 1 Step 5):
```powershell
Compare-Object (Get-Content "$tmp2\README.md") (Get-Content "...\references\method-catalog.md" | Select-Object -Skip 1)
```
Expected: empty (byte-identical after the comment line). Delete `$tmp2` afterward.

- [ ] **V3 — Method references + Tier 1 resolve**
```powershell
$methods = (node ado-cli.js list --json | ConvertFrom-Json).name
Select-String -Path "..\..\azure-devops\**\*.md" -Pattern 'ado-cli\.js.*--structured' -AllMatches
```
Manually cross-check every method name appearing after `ado-cli.js` in a canonical-invocation block against `$methods` — every one must be a real, current method name. Zero stale names (confirms Task 8/12's `searchWorkItems` fix held).

- [ ] **V4 — Self-containment + naming (2 greps)**
```powershell
Select-String -Path "azure-devops\**\*.md" -Pattern "ado:|development:|gh:|code-reviewer:|debugging:"
```
Expected: 0 matches (no external-plugin namespace references).
```powershell
Select-String -Path "azure-devops\**\*.md" -Pattern "azure-devops-|(?<!\w)ado-(?!cli\.js|api\.mjs|mention-conventions\.md|state-transitions\.md)"
```
Expected: 0 matches except the 4 allowed filenames (`ado-cli.js`, `ado-api.mjs`, `ado-mention-conventions.md`, `ado-state-transitions.md`). Also run a second, colon-free grep specifically for `skills: [ado-mentions]` / `- ado-mentions` (the one cross-reference a colon-based grep misses per R12):
```powershell
Select-String -Path "azure-devops\agents\*.md" -Pattern "ado-mentions"
```
Expected: 0 matches.

- [ ] **V5 — Privacy scan (2 greps)**
```powershell
Select-String -Path "azure-devops\**\*" -Pattern "AZURE_DEVOPS_PAT|AZURE_DEVOPS_PERSONAL_ACCESS_TOKEN|AZURE_DEVOPS_BEARER_TOKEN|--pat "
```
```powershell
Select-String -Path "azure-devops\**\*" -Pattern "env\b|printenv|echo \`"\`$HTTP"
```
Expected for both greps: 0 matches, **except** hits that fall in one of these three places (same list for both patterns — see spec Appendix C, V5):
1. CLAUDE.md's / README's own negative-instruction prose *outside* the tagged block (explaining no PAT path / no env dump exists — recheck neither names an actual var value). Currently zero-hit at commit `27cc8bd` — both README's tier summary and everything in CLAUDE.md outside the tag trip neither probe; retained as a permissive allowance in case such prose is reintroduced outside the tag.
2. `references/method-catalog.md`'s generated usage banner (first grep only — the banner names no env dump). Currently the only non-block hit: 1 occurrence, secret probe only.
3. Inside a file's `<mutation_privacy_policy>...</mutation_privacy_policy>` span — **only** once V6(b) below confirms that span hashes identical to the canonical text across all 10 tag-wrapped occurrences tree-wide (`CLAUDE.md`'s canonical copy + 9 skill/agent restatements). A hit outside a verified span, or in a block whose hash differs, is still a finding. Currently accounts for the remaining hits: 10 of 11 secret-probe hits, all 10 env-probe hits.
No hit anywhere may be an actual secret value, a working `--pat` recommendation, or an instruction to print/echo a real proxy value — the block's own text only prohibits those things.

- [ ] **V6 — Port fidelity review (human read, 5 checks)** — see Task 17 (this check is executed there, against the full file set, since it needs every prior task complete). Two additional required sub-checks, run branch-wide, every release — documented commands below, not yet backed by a committed test harness (the execution/hashing scripts referenced are currently ephemeral under `.superpowers/temp/`; promoting them to tracked, repo-committed scripts is future work):

  - [ ] **V6(a) — Heredoc terminator / sentinel gate.**
    ```powershell
    Select-String -Path "azure-devops\**\*.md" -Pattern "^[ \t]+ADOJSON[ \t]*$"
    ```
    Expected: 0 matches (every quoted-heredoc terminator sits at column 0 — an indented one silently swallows the terminator and everything after it into the JSON body; this is exactly C-1 from Task 16). Then extract every fenced ` ```bash ` block containing `ado-cli.js <<'ADOJSON'`, stub the CLI call, append a sentinel line, and run under `bash`. Expected: every real invocation block prints its sentinel (the 2 `CLAUDE.md`/`README.md` `<method>`-placeholder template blocks are excluded — they cannot execute and aren't canonical invocations).

  - [ ] **V6(b) — Mutation & Privacy Policy block identity + coverage.**
    ```powershell
    Select-String -Path "azure-devops\**\*.md" -Pattern "<mutation_privacy_policy>" | Select-Object -ExpandProperty Path
    ```
    Expected: exactly 10 files — `CLAUDE.md` (the canonical copy) plus 9 restatements: `work-on`, `publish-pr`, `babysit-pr`, `work-items`, `work-my-backlog`, `draft-work-item`, `assistant`, `pr-tender`, `babysit-pr-worker`. `README.md` (references the block by name but carries no tag-wrapped copy), `mentions`, and the 5 `commands/*.md` wrappers must show 0 (they cannot mutate). Then extract each file's `<mutation_privacy_policy>...</mutation_privacy_policy>` span and hash it (documented step; no committed script performs this yet). Expected: a single distinct SHA-256 across all 10. A total other than 10, a restatement count other than 9, or more than one distinct hash, is a finding — and invalidates V5's block exception until fixed.

- [ ] **V7 — Marketplace/docs consistent**
```powershell
Get-Content "B:\sources\sandbox-plugins\.claude-plugin\marketplace.json" -Raw | ConvertFrom-Json | Out-Null
```
Expected: no parse error, exit 0.
```powershell
Select-String -Path "B:\sources\sandbox-plugins\README.md" -Pattern "azure-devops \(v1\.0\.0\)"
```
Expected: 1 match.

- [ ] **V8 — Source repo untouched**
```powershell
cd "B:\sources\claude_plugins"
git status --porcelain -- ado/
```
Expected: empty output (no tracked changes under `ado/`; the source repo's ambient untracked scratch artifacts outside `ado/` are pre-existing and irrelevant to this check).
```powershell
$srcHash = (Get-FileHash "B:\sources\claude_plugins\ado\scripts\ado-cli.js" -Algorithm SHA256).Hash
$dstHash = (Get-FileHash "B:\sources\sandbox-plugins\azure-devops\scripts\ado-cli.js" -Algorithm SHA256).Hash
$srcHash -eq $dstHash
```
Expected: `True`.

(No commit in this task — verification only. Any failure here means: go back, fix the offending task, re-commit there, then re-run V1–V8.)

---

### Task 17: Port-fidelity diff review + final commit + push + fresh-clone verification

**Files:** none created; git operations only.

- [ ] **Step 1: Diff every ported file against its source counterpart** (V6, the 5 checks):

*Note: this Step 1 (a)-(e) is the concrete fidelity-diff execution of spec Appendix C's V6(c) (phase/gate
preservation) and V6(e) (port fidelity diff-to-rule mapping) specifically. Spec V6(a) (canonical-invocation
heredoc form + terminator/sentinel gate) and V6(b) (mutation-policy block identity + coverage) run as
their own standing checks in Task 16 above; V6(d) (error handling branches on exit code/stdout) is
reviewed inline while reading each file below. The two "(a)-(e)" lists label different things by design —
this Step's letters are a diff-execution breakdown, not a restatement of the spec's V6(a)-(e) gate list.*

(a) Confirm every phase/section header name in `work-on`/`draft-work-item` matches its `development/` source 1:1 (already counted in Task 11 Step 8 / re-confirm for Task 12 the same way).
(b) Confirm every skill/agent/command frontmatter `name:` matches the unprefixed target name from the component mapping table.
(c) Confirm every reference file path resolves (no dangling `references/...` or `skills/.../reference/...` link).
(d) Confirm every agent's `skills:` list entries resolve to an existing `skills/<name>/SKILL.md`.
(e) For each of the 15 ported skill/agent/command files, run a textual diff against its source and manually confirm every hunk maps to one of R1–R12:

```powershell
$pairs = @(
  @("ado\skills\ado-publish-pr\SKILL.md","azure-devops\skills\publish-pr\SKILL.md"),
  @("ado\skills\ado-babysit-pr\SKILL.md","azure-devops\skills\babysit-pr\SKILL.md"),
  @("ado\skills\ado-work-items\SKILL.md","azure-devops\skills\work-items\SKILL.md"),
  @("ado\skills\ado-work-my-backlog\SKILL.md","azure-devops\skills\work-my-backlog\SKILL.md"),
  @("ado\skills\ado-mentions\SKILL.md","azure-devops\skills\mentions\SKILL.md"),
  @("ado\agents\ado-devops-assistant.md","azure-devops\agents\assistant.md"),
  @("ado\agents\ado-pr-tender.md","azure-devops\agents\pr-tender.md"),
  @("ado\agents\ado-babysit-pr-worker.md","azure-devops\agents\babysit-pr-worker.md"),
  @("ado\references\review-thread-state-machine.md","azure-devops\references\review-thread-state-machine.md"),
  @("ado\references\ado-mention-conventions.md","azure-devops\references\ado-mention-conventions.md"),
  @("ado\references\review-reception-protocol.md","azure-devops\references\review-reception-protocol.md")
)
foreach ($p in $pairs) {
  Compare-Object (Get-Content "B:\sources\claude_plugins\$($p[0])") (Get-Content "B:\sources\sandbox-plugins\$($p[1])")
}
```
For the two verbatim pairs (`ado-mention-conventions.md`, `review-reception-protocol.md`) expect empty output. For all others expect only the diffs already accounted for by name in that file's task above — no unexplained hunks. `work-on`/`draft-work-item` are diffed against their `development/` source instead (no `ado/` counterpart to diff against, since the source was a thin wrapper — diff structurally by phase-header count as in Task 11 Step 8/Task 12, not line-by-line, since the source files are of very different sizes by design, R6).

- [ ] **Step 2: Final commit** (only if any stray uncommitted change remains from the review pass — should be none if every task committed as it went):
```bash
git status --porcelain
```
Expected: empty (all prior task commits already cover everything). If non-empty, stage and commit with a message describing the residual fix, referencing which rule it corresponds to.

- [ ] **Step 3: Push**
```bash
git push origin <branch-name>
```
Expected: exit 0.

- [ ] **Step 4: Fresh-clone verification**
```powershell
$fresh = New-Item -ItemType Directory -Force "$env:TEMP\sandbox-plugins-fresh-verify"
git clone <remote-url> $fresh.FullName
cd $fresh.FullName
claude plugin validate "$($fresh.FullName)\azure-devops"          # re-run V1
node "$($fresh.FullName)\azure-devops\scripts\ado-cli.js" list --json   # re-run V2 subset
Get-Content "$($fresh.FullName)\.claude-plugin\marketplace.json" -Raw | ConvertFrom-Json | Out-Null  # re-run V7
```
Expected: all three exit 0 / parse clean, confirming the pushed history is complete and self-contained (no file left uncommitted). Delete `$fresh` afterward.

- [ ] **Step 5: Report completion** — no further commit; this task's deliverable is the verified, pushed state itself.

---

## Self-Review (performed against this plan before delivery)

**Spec coverage:** R1 (Task 5–12 global substitution G2), R2 (Tasks 6–12 G4/worked examples), R3 (Task 13), R4 (Task 2), R5 (Tasks 6–8 "port 1:1" framing), R6 (Tasks 11–12), R7 (Task 3 both fixes + Task 9 Step 3 restatement), R8 (Task 8, Task 12 Step 4), R9 (Task 4, Task 5's PAT-prose fix), R10 (Task 10 drop, referenced in G6 throughout), R11 (Task 1 Step 2, version 1.0.0), R12 (Task 9). D1–D9 constraints are all named in Global Constraints. V1–V8 are all Task 16 steps, V6 elaborated in Task 17 Step 1. No spec section found without a home task.

**Placeholder scan:** every task step names exact file paths, exact before/after text, exact commands, and exact expected output — no "TBD", "add appropriate handling", or "similar to Task N" shorthand without the actual content repeated. Where full-file reproduction was impractical (Tasks 11/12's 640/192-line source bodies), the plan gives the exact structural transform rules, the exact phase list to preserve, and worked examples for the trickiest conversions, rather than leaving "figure it out" gaps.

**Naming/method consistency:** component names (`work-on`, `publish-pr`, `babysit-pr`, `babysit-pr-worker`, `work-items`, `draft-work-item`, `work-my-backlog`, `mentions`, `assistant`, `pr-tender`) are used identically across every task that references them (Tasks 5–12 cross-check clean). Method names (`listWorkItems`, `createWorkItem`, `getPullRequest`, etc.) are never invented — every one is either carried over unchanged from source or, where R8 required a fix, replaced per the spec's exact prescribed resolution (drop-clause, not substitute-name).

**Contradiction check:** Task 3's Fix B wording, Task 9 Step 3's restated sentence, and Task 13's "Review Thread Resolution" section all state the R7(b) rule identically — verified no drift between the three copies. Task 6/7's "port 1:1" framing does not contradict Task 9's agent-level `<do_not_resolve>`-preserved-verbatim instruction, since pr-tender (interactive) and babysit-pr-worker (autonomous) are explicitly split by mode, not merged.

---

## Execution Handoff

Plan complete. Two execution options:

**1. Subagent-Driven (recommended)** — dispatch a fresh subagent per task, review between tasks, fast iteration. REQUIRED SUB-SKILL: `superpowers:subagent-driven-development`.

**2. Inline Execution** — execute tasks in this session using `executing-plans`, batch execution with checkpoints. REQUIRED SUB-SKILL: `superpowers:executing-plans`.
