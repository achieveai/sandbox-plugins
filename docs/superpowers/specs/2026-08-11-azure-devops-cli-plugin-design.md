# `azure-devops` plugin — CLI-native migration of the ADO workflow

**Date:** 2026-08-11 · **Status:** Approved. Ready to implement. No open decisions.
**Source:** `B:\sources\claude_plugins`, plugin `ado/` v3.1.5 — **read-only, never modified**
**Target:** this repo (`B:\sources\sandbox-plugins`), new directory `azure-devops/`, published at
**v1.0.0** in `.claude-plugin/marketplace.json` alongside `sandbox` and `sandbox-auth`

---

## Executive summary

**Goal.** Port the whole ADO workflow into this marketplace as a self-contained plugin. Transform the
transport. Do not rewrite the workflows.

**Architecture.** CLI-native. Skills shell out to a bundled `ado-cli.js`. Nothing else.

- No MCP server. No port. No listener. No `mcp/server.json`.
- **No adapter, wrapper, or client library.** The CLI is self-sufficient.
- **No test suite.** No `tests/`, no `package.json`, no runner.
- One short-lived process per call. No shared state.

**Invocation.** One form, everywhere:

```bash
node "${CLAUDE_PLUGIN_ROOT}/scripts/ado-cli.js" <method> --structured <<'ADOJSON'
{ "id": 12345 }
ADOJSON
```

**Workflows included — all of them.** 8 skills, 3 agents, 5 commands (the 6th, `setup-ado-mcp`, is
dropped). Same phases, same gates, same decision logic as the source plugin.

**Auth.** `sandbox-auth` only. The egress proxy injects the credential server-side.

- Warm up once per session via `sandbox-auth:azure-devops`.
- **`--pat` is never passed.** No PAT env var is ever read. No credential file. No PAT UI.
- `AZURE_DEVOPS_ORG_URL` + `AZURE_DEVOPS_PROJECT` are still required — configuration, not credentials.

**Key safety rules.**

| Rule | Detail |
|---|---|
| Comments are append-only | Never `manageWorkItemComment` with `update`/`delete`. Post a new one. |
| Confirm destructive + out-of-scope | [Mutation policy](#mutation-policy) Tier 3. |
| Never print the environment | Proxy vars carry a token. Check presence, never value. |
| Never read stderr as failure | It is non-empty on **success**. Branch on exit code + stdout. |
| Never pass `--input -` | Not a stdin sentinel. It opens a file literally named `-`. |

**Files.** `plugin.json`, `CLAUDE.md`, `README.md`, 6 references, 2 scripts, 8 skills, 3 agents,
5 commands. Full tree: [File inventory](#file-inventory).

**Release gates.** V1 plugin validates · V2 CLI intact · V3 method refs resolve · V4 self-contained ·
V5 privacy scan · V6 workflow review · V7 marketplace/docs · V8 source untouched. All offline, all
credential-free. Detail: [Appendix C](#appendix-c--release-verification-detail).

## Decisions

| # | Question | Decision |
|---|---|---|
| D1 | Transport | Direct `ado-cli.js` invocation. No adapter. |
| D2 | Tests | None. Verification is validation + review, **not** a test suite under another name. |
| D3 | Who resolves PR threads | Autonomous worker **may**, after applying **and verifying**. Interactive tender **never**. |
| D4 | Routing agent name | **`ado-assistant`** — named exception to prefixing. Not `azure-devops-devops-assistant` (stutters), not `azure-devops-assistant`. |
| D5 | Auth | `sandbox-auth` only. Zero PAT surface. |
| D6 | `work-on` / `draft-work-item` | Inline the `development:*` steps. Do not reimplement that framework. |
| D7 | Version | Fresh at `1.0.0`. Do not continue `3.1.5`. |
| D8 | Mutation policy enforcement | Instruction-level in `CLAUDE.md`. No runtime block exists. |

## What changes / What does not change

**Changes:**

- Transport: MCP tool call → `node ado-cli.js <method> --structured` with JSON on stdin.
- Auth: `ado:setup-ado-mcp` (writes a PAT into an MCP config) → `sandbox-auth`. Deleted, not ported.
- Namespace: `ado:` → `azure-devops:`; file basenames `ado-*` → `azure-devops-*` (except D4).
- `work-on` / `draft-work-item`: `development:*` delegations become inline instructions.
- `ado-api.mjs`: direct `fetch` + PAT header removed; it spawns the CLI instead.
- Dropped: `examples/`, `launch-ado-mcp.sh`, `setup-ado-mcp` skill + command.
- Version resets to `1.0.0`.

**Does not change:**

- Method names. `getWorkItemById` stays `getWorkItemById`. Same params, same order, same meaning.
- Every skill's phases, gates, decision logic, and outcome contracts.
- The HITL feedback checkpoint, the v3 revision cap, append-only comments.
- Agent frontmatter shape (`modelintelligence` + `effort`, **not** `model`).
- `claude_plugins`. Nothing is committed, staged, edited, or squashed there. The bundle is **copied
  out**, never moved.

## Problem

The source plugin (8 skills, 3 agents, 6 commands) is built entirely on the
`@achieveai/azuredevops-mcp` MCP server. Nothing about that fits here:

- No MCP server registration surface exists in a sandboxed session. This marketplace is CLI/skill-native.
- The egress proxy injects auth transparently. A "write a token into a config file" skill is actively wrong here.
- There is no `development` plugin here, and `ado-work-on` / `ado-draft-work-item` are thin wrappers on it.

What makes the port possible: `ado/scripts/ado-cli.js`, an 87,719-line esbuild bundle of the same
TypeScript project exposing the same 130 tools through a `node ado-cli.js <method>` contract — same
`registerTools()`, same Zod schemas. **Self-sufficient:** no wrapper, no per-tool shim.

**Why not MCP.** `web-research.md`'s "Plugin Architecture for Sandbox" section recommends an Express
MCP server (`mcp/server.json` + a Node HTTP listener) for exactly this. **Overridden, permanently.**
No port to bind usefully, no client to register with, and it reintroduces the dependency this
migration removes.

## Canonical invocation form

One shape everywhere — one form to review, one form to fix if the CLI contract changes. Five rules,
each for a verified reason:

1. **`--structured`, always.** It makes stdout a single parseable JSON document.
2. **JSON on stdin. `--input` is never passed.** Stdin is already the default body source. **Never
   `--input -`** — the CLI's hand-rolled arg parser has no `-` sentinel and calls
   `fs.readFileSync("-")`, failing `ENOENT ... open '<cwd>/-'`.
3. **Quoted heredoc (`<<'ADOJSON'`), not `echo '<json>' |`.** Not stylistic. ADO content routinely
   contains apostrophes — work item titles, comment bodies, error text. A single-quoted `echo` breaks
   on the first one. An *unquoted* heredoc expands `$` and backticks inside comment text. The quoted
   heredoc passes bytes through untouched, which also removes the injection surface. For large bodies,
   write a file and redirect (`… --structured < body.json`) — still stdin, still no `--input`.
4. **`${CLAUDE_PLUGIN_ROOT}` is a Claude Code placeholder, not a shell variable.** Substituted before
   the shell sees it. Keep it double-quoted so paths with spaces survive. Never `export` it, never
   compute it with `dirname "$0"`, never hardcode an absolute path.
5. **Branch on exit code and stdout only.** See [Error handling](#error-handling).

## Authentication and environment

### Handshake

Reused from `sandbox-auth`, unmodified:

- **`sandbox-auth:azure-devops`** — picks the probe URL: specific target resource, else org project
  list `https://dev.azure.com/<ORG>/_apis/projects?api-version=7.0`, else VSSPS profile fallback
  `https://app.vssps.visualstudio.com/_apis/profile/profiles/me?api-version=7.0`. Hands off to
  `egress-auth`.
- **`sandbox-auth:egress-auth`** — owns the wire contract. HTTP `511` with an `auth_pending` marker →
  poll until Allow/Deny. HTTP `403 {"error":"denied","reason":...}` → stop and report. On success the
  proxy has recorded the grant; every later call from this session carries the credential
  automatically. The agent process never sees a token.

**The `CLAUDE.md` rule that replaces "MCP Prerequisite — Auto-Setup":**

> Before the first Azure DevOps CLI call in a session, run `sandbox-auth:azure-devops` (pass the target
> org if known). If a CLI call fails with `E_AUTH`, run it once and retry. Never ask the user to supply
> a PAT, token, or credential file — this plugin has no PAT interface.

Same soft, retry-on-failure shape the source plugin already uses. Different mechanism.

### Auth modes

`ado-cli.js`'s `invocationConfig()` exposes only two reachable modes: `--pat <token>` (Basic-shape
header), and no-flag `auth:"none"` (zero `Authorization` header — "a proxy injects it"). **This plugin
always uses no-flag.** `ado-cli.js` exits `4` with
`"AZURE_DEVOPS_ORG_URL and AZURE_DEVOPS_PROJECT must be set"` if either is missing.

### Proxy and CA — verified against the bundle, 2026-08-11

`sandbox-auth`'s existing helpers are Python; `requests` honors the proxy and CA vars automatically.
Node does not, so the mechanism was established against the bundle's actual HTTP stack rather than
assumed. `grep -cE '(^|[^.\w])fetch\('` → **0**. No undici anywhere. Every call goes
`AzureDevOpsService` → `azure-devops-node-api` `WebApi` → `typed-rest-client` `HttpClient` →
`https.request` with a `tunnel` proxy agent. So almost nothing needs wiring:

| Concern | Status | Action |
|---|---|---|
| `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY` | Read natively by `typed-rest-client` (`_getProxy()`, `_isMatchInBypassProxyList()`), which builds the `tunnel` agent itself | Inherit. Do **not** rewrite, re-export, or strip the embedded token. |
| Lowercase `http_proxy` etc. | **Not read.** Hardcoded uppercase `EnvironmentVariables` enum, no fallback | Export the uppercase form if only lowercase is set. |
| `NODE_USE_ENV_PROXY` | **Inapplicable.** Undici/`fetch`-level switch; this bundle never uses undici | Omit. Harmless but misleading. |
| `NODE_EXTRA_CA_CERTS` | **Required.** Node ignores `REQUESTS_CA_BUNDLE`/`SSL_CERT_FILE`/`CURL_CA_BUNDLE` entirely. Extends the root store process-wide, covering `https.request` and `tunnel` TLS sockets alike | Set from whichever Python/curl var is present, **before** `node` starts — Node reads it only at process start. |

**Both are session-level preconditions, not per-call work.** With no adapter to normalize them, the
`CLAUDE.md` warm-up preamble checks and exports them **once per session**, alongside the auth warm-up.
Both checks are idempotent no-ops in a correctly configured sandbox.

## Data flow

1. A user or the model invokes an `azure-devops-*` skill, directly or via its command.
2. First ADO touchpoint runs the session warm-up: proxy/CA precondition check, then
   `sandbox-auth:azure-devops` (soft — full handshake only if a call actually failed with an auth
   error).
3. The skill invokes `node ado-cli.js <method> --structured` with JSON on stdin, exactly where the
   source skill named an MCP tool. One process per operation.
4. `ado-cli.js` calls `dev.azure.com` / `<org>.visualstudio.com` over HTTPS through the MITM egress
   proxy, using the settings its own stack reads from the inherited environment.
5. The proxy injects the real credential server-side. Neither the plugin nor the CLI process sees it.
6. The skill reads exit code + stdout (never stderr) and continues its existing prompt logic unchanged
   — post a comment, format a table — substituting the CLI result for the MCP tool result.
7. Writes flow back the same way. **No direct-`fetch` path exists anywhere**, closing the one place the
   source plugin bypassed the tool layer ([R9](#r9--ado-apimjs-remove-direct-pat-auth)).

## Security and privacy

- **No PAT ever exists in this plugin's process, config, or logs.** Nothing reads `AZURE_DEVOPS_PAT` /
  `..._PERSONAL_ACCESS_TOKEN` / `..._BEARER_TOKEN`, passes `--pat`, or writes an auth record. That
  deliberately makes the bundle's plaintext `EntraAuthHandler` persistence path (`getAuthRecordDir()` →
  `~/.azuredevops-mcp/`) **unreachable** — the code exists but is never exercised. `provenance.json`
  records that as intentional. Gate V5 keeps it true.
- **Never dump the environment.** `HTTP_PROXY`/`HTTPS_PROXY` embed a per-sandbox token as basic-auth
  userinfo. Skills invoke `node …` and let the child inherit them. **No file may `echo`, `env`, or
  `printenv` the environment** as a diagnostic, and no failure report may quote those values. A
  diagnostic that must confirm a proxy tests *presence* (`[ -n "$HTTPS_PROXY" ]`), never the value.
  **The likeliest accidental-disclosure path in the design** — there is no adapter to redact for you.
- **No secrets in transcripts.** Skills summarize; they never echo raw bodies or raw output wholesale.
  Redact any field named `token`/`pat`/`password`/`secret`/`authorization` before quoting it anywhere.
- **The stderr auth banner is an invariant.** `ado-cli.js` prints `[Auth] Auth type: none, PAT: not set`
  every run. Under this design it is *always* exactly that. Anything else means a PAT leaked — stop.
  Either way it is CLI noise and must not surface in a user-facing error.
- **Append-only comments** — see [Mutation policy](#mutation-policy). A `CLAUDE.md` prohibition and a
  V6 review item, not a runtime block.

## Mutation policy

Invoking an `azure-devops-*` skill already authorizes the ordinary mutations that skill performs. No
new gate is added on top of what the source plugin does today. Extra confirmation applies only to the
exceptional: destructive, irreversible, or outside what any migrated skill uses.

**Enforced by instruction, not by code (D8).** No adapter, so no code path can refuse a call. The tiers
live in `azure-devops/CLAUDE.md`, are restated at the step inside each skill that could trip them, and
are checked by V6. Same kind of enforcement the source plugin already relies on for its append-only
rule — not a regression from a working control, but not a technical control either. A deliberate trade
of a runtime guarantee for removing a whole layer of code. Recorded in
[Known limitations](#known-limitations). Written imperative, to be copied verbatim into `CLAUDE.md`:

| Tier | Scope | Rule |
|---|---|---|
| **1 — Ordinary** | Exactly the methods migrated files reference by name | Proceed. No new gate. |
| **2 — Forbidden** | `manageWorkItemComment` with `action: "update"` or `"delete"` | **Never invoke**, by any skill, agent, command, or script. To correct a comment, post a new one. |
| **3 — Confirm first** | Destructive, or out-of-scope | Ask explicitly, naming the action and the resource, before invoking. Proceed only on an affirmative. |

- **Tier 3, destructive by nature** — regardless of whether a migrated skill calls it:
  `mergePullRequest`, `deletePackageVersion`, `rotateSecrets`, `manageSecurityPolicies`, `runPipeline`,
  overwrite-style `createOrUpdateWikiPage`, any bulk create/update/delete.
- **Tier 3, out-of-scope catch-all** — any method not referenced by name in any migrated file
  (cross-checked against `method-catalog.md`). A method no shipped skill uses is exceptional by
  definition, even if it looks benign.
- **Tier 1 is a definition, not a hand-maintained list.** It *is* the referenced set. V3 checks that
  the `CLAUDE.md` list equals what the files reference, so it cannot drift — either direction is a
  release-time finding. Enumeration + two encoded corrections:
  [Appendix A](#appendix-a--tier-1-method-inventory).
- Tier 2's method name appears in no migrated file except as the subject of the prohibition, so V3 and
  V6 both surface any reintroduction.
- Skills that already confirm before creating/updating (`azure-devops-work-items`' "always confirm
  before making changes", `azure-devops-draft-work-item`'s mandatory preview) keep those prompts
  exactly as-is. This layer does not touch them.

## Error handling

Skills read the exit code and stdout directly. `references/error-codes.md` maps what the CLI returns to
a small named vocabulary, so every skill reacts the same way and instructions can say "on `E_AUTH`, …"
instead of restating exit-code trivia at each call site. **The names are shared vocabulary, not a
runtime type** — nothing constructs an `E_AUTH` object.

| Exit | Meaning |
|---|---|
| 0 | Success |
| 1 | Generic / tool error |
| 2 | Invalid input, Zod validation failure |
| 3 | Unknown method |
| 4 | Missing required env vars |

ADO's own REST error JSON (`message`, `typeName`, `typeKey`, `errorCode`, `eventId`) appears in stdout
for upstream failures.

**Reading the result correctly.** Branch on **exit code and stdout only**. Every invocation writes the
auth banner to stderr, and Node emits a `DEP0169` `url.parse()` deprecation warning from a bundled
dependency — **both appear on successful, exit-0 runs**. stdout stays clean JSON under `--structured`.
A skill that treats non-empty stderr as failure fails 100% of its calls. stderr is diagnostic context
for a failure the exit code has already established, never the failure signal itself.

Full name → condition → response table: [Appendix B](#appendix-b--error-vocabulary).

## File inventory

```
azure-devops/
├── .claude-plugin/plugin.json          new — name "azure-devops", version "1.0.0"
├── CLAUDE.md                           adapted from ado/CLAUDE.md — see R3
├── README.md                           new — mirrors sandbox-auth/README.md
├── references/
│   ├── ado-mention-conventions.md      verbatim from ado/references/
│   ├── review-reception-protocol.md    verbatim from ado/references/
│   ├── review-thread-state-machine.md  two fixes — see R7
│   ├── ado-state-transitions.md        from development/skills/work-on/reference/ — NOT ado/ — see R6
│   ├── method-catalog.md               generated, not authored — see Appendix C
│   └── error-codes.md                  new — Appendix B
├── scripts/
│   ├── ado-cli.js                      verbatim from ado/scripts/ado-cli.js
│   └── provenance.json                 new — origin, byte size, content hash, copy date
├── skills/
│   ├── azure-devops-work-on/
│   │   ├── SKILL.md                    from ado-work-on — inline-not-delegate, R6
│   │   └── reference/                  generic guides from development/skills/work-on/reference/
│   ├── azure-devops-publish-pr/SKILL.md
│   ├── azure-devops-babysit-pr/SKILL.md
│   ├── azure-devops-draft-work-item/SKILL.md   inline-not-delegate, R6
│   ├── azure-devops-work-items/SKILL.md
│   ├── azure-devops-work-my-backlog/
│   │   ├── SKILL.md
│   │   └── scripts/{ado-api.mjs,classify.mjs,scan.mjs,state.mjs}   see R9
│   └── azure-devops-mentions/SKILL.md
├── agents/
│   ├── azure-devops-babysit-pr-worker.md
│   ├── azure-devops-pr-tender.md
│   └── ado-assistant.md                from ado-devops-assistant — D4 exception
└── commands/
    └── azure-devops-{work-on,publish-pr,babysit-pr,draft-work-item,work-my-backlog}.md
```

**Deliberately absent:** `tests/`, `package.json`, `scripts/invoke-ado-cli.mjs`, `mcp/`, `examples/`,
`launch-ado-mcp.sh`, any `setup-ado-mcp` component. The only two files under `scripts/` are the
vendored bundle and its provenance record.

## Migration rules

| # | Rule |
|---|---|
| **R1** | **Namespace + basename prefix only.** `ado:` → `azure-devops:`; `ado-x` → `azure-devops-x` (`ado-work-on/SKILL.md` → `azure-devops-work-on/SKILL.md`), so names stay unambiguous in a multi-plugin marketplace. No procedure, phase structure, or decision logic is rewritten. **Exception (D4):** mechanical prefixing produces a stuttering `azure-devops-devops-assistant`; the routing agent ships as **`ado-assistant`** — the only bare `ado-` component name, carried in V4 as an explicit allowed name. |
| **R2** | **MCP tool call → direct CLI call, mechanically.** Every "call `<toolName>`" becomes `node "${CLAUDE_PLUGIN_ROOT}/scripts/ado-cli.js" <toolName> --structured` with the same parameters, same order, same purpose, per the [canonical form](#canonical-invocation-form). Method names unchanged. Nothing to import. |
| **R3** | **`CLAUDE.md` carries forward and gains three jobs.** "Comments Are Append-Only" copied verbatim. "MCP Prerequisite — Auto-Setup" replaced per [Authentication](#authentication-and-environment). Because there is no adapter, it is now also the single home for (a) the canonical invocation form, (b) the session warm-up preamble, (c) the full three-tier mutation policy. One always-loaded file replaces the "one file tells you the whole policy" property the adapter used to give. |
| **R4** | `ado-mentions` → `azure-devops-mentions`, copied verbatim. Prefix only. |
| **R5** | `ado-publish-pr`, `ado-babysit-pr`, `ado-work-items`, `ado-work-my-backlog` migrate **1:1** — same phases, same decision logic. Only R2 and R8 touch them. |
| **R8** | **Stale method names fixed.** `ado-work-items/SKILL.md` line 27 and `ado-draft-work-item`'s Duplicate Check table both name **`searchWorkItems`, which does not exist** among the 130 real methods. Replacement is **`listWorkItems`** — the catalog calls it the *"preferred structured work item query path. Usually 1 WIQL call plus 1 batched hydrate call for up to 200 returned items,"* exactly what the stale name reached for. Line 27 already names `listWorkItems` alongside it, so **drop the `or searchWorkItems` clause** rather than substitute. Per call site, consider `getMyWorkItems` (assigned-to-me) or `getQueryResults` (saved queries) instead of a blind swap. General cross-check is V3. |
| **R10** | Drop MCP-only artifacts: `examples/`, `launch-ado-mcp.sh`, `setup-ado-mcp` skill + command. |
| **R11** | `plugin.json` starts at `"version": "1.0.0"`. The `3.1.5` lineage is not continued — same pattern this repo used for `sandbox`. |

### R6 — `work-on` / `draft-work-item`: inline, do not delegate

Their only ADO-specific content is the "Azure DevOps" column of their `GitHub | Azure DevOps` tables
plus the provider-resolution/tooling phases. Everything else delegates to `development:work-on` /
`development:draft-work-item`, absent here — which in turn delegate to `development:autonomous-design`,
`:implement`, `:blind-spot-detector`, `:draft-feature`, `:draft-bug`, `debugging:debug-with-logs`,
`debugging:systematic-debugging`, `code-reviewer:pr-review`. Also absent. Reimplementing that framework
is out of scope.

- **Keep the full phase structure verbatim:** Phase 0 provider resolution (→ a no-op single-provider
  statement), 1 auto-detect mode, 1.1 fetch & understand, 1.2 route by type, **the mandatory Phase 1.5
  feedback checkpoint**, Part 2 execute & deliver, Error Handling, Reference Conventions, Decision Log.
  Every ADO-column cell kept.
- **Replace each `development:X` delegation with inline instructions doing the same step**, using the
  agent's own reasoning and standard tools (Read/Write/Edit/Grep/Glob/Bash/git) rather than a name that
  would not resolve:
  - `autonomous-design` → research the codebase (Read/Grep/Glob/git log/WebSearch), propose 2-3
    approaches, pick one with a stated rationale, note it in the decision log. No design-review-gate
    sub-agent dispatch.
  - `implement` → implement the approved plan directly with ordinary read/write/test/verify tool use,
    commit each increment, self-review the diff before publishing.
  - `draft-feature` / `draft-bug` / `blind-spot-detector` → ask the type-appropriate clarifying
    questions, then self-check for dependency, done-definition, and hidden-scope gaps before the
    mandatory preview. The Quick Path (Task/trivial) has no external dependency — unchanged.
- **Same outcome contract** (success/blocked), **same gates** (HITL checkpoint, v3 revision cap,
  append-only comments). No unresolvable skill names remain.
- Provider-agnostic tables collapse to the ADO column. The GitHub column and its `gh:*` refs drop.
- **Copy in the generic reference guides** these skills reach via `development`'s `reference/` — bug RCA
  workflow, plan/RCA comment formats, decision-log guide, git-worktree and branch-completion guides —
  into `azure-devops-work-on/reference/`. Provider-neutral beyond the tables above.
- **`ado-state-transitions.md` also comes from `development`, not `ado/`.** Verified: the source
  `references/` holds exactly three files (`ado-mention-conventions.md`, `review-reception-protocol.md`,
  `review-thread-state-machine.md`). This one lives at
  `development/skills/work-on/reference/ado-state-transitions.md`, consumed by `development:work-on`
  when its provider is ADO. `azure-devops-work-on` absorbs that behavior, so copy it in. Only
  `ado-mention-conventions.md` is genuinely native to the source plugin.

### R7 — `review-thread-state-machine.md`: two fixes

**(a) Dangling sync note.** The source opens with "this lifecycle is copied into the code-reviewer and
ADO plugins; the copies must remain byte-identical." `code-reviewer` does not exist here, so the claim
dangles. Replace with: *"this lifecycle document describes the PR-review thread conventions this
plugin's `azure-devops-babysit-pr-worker` agent follows; it originated as a shared reference with a
`code-reviewer` plugin that is not part of this marketplace."*

**(b) Rule 1 — three-way contradiction, resolved (D3).** The two agents contradict **each other**, not
merely a shared doc:

| File | Says |
|---|---|
| `review-thread-state-machine.md` L180, Rule 1 | *"Only the reviewer closes threads — the developer never resolves or closes threads in ADO."* |
| `agents/ado-babysit-pr-worker.md`, `[Resolve]` | Calls `updatePullRequestThread` with `fixed` / `wontFix` / `byDesign` / `closed`. |
| `agents/ado-pr-tender.md`, `<do_not_resolve>` | *"Do NOT resolve comment threads — let the reviewer resolve them."* |

**Split by mode, not by actor.** The worker runs unattended, and leaving finished work marked open
defeats the loop it exists to run. A human is present for the tender; resolution is theirs.

- Rule 1 → *"Threads are closed by the reviewer, or by the autonomous babysit worker once it has
  applied **and verified** the requested change. Interactive/assistive flows never resolve threads on
  the developer's behalf."*
- Worker's `[Resolve]` step kept, precondition explicit: **only after applied and verified.** Never on
  intent. Never on "will fix."
- Tender's `<do_not_resolve>` kept **verbatim** — now consistent rather than in tension.
- Restated in `azure-devops/CLAUDE.md` so it loads every session, not only when the file is read.

### R9 — `ado-api.mjs`: remove direct PAT auth

`ado-work-my-backlog/scripts/ado-api.mjs`'s `getAuthHeader()` reads all three PAT env vars and builds
its own `Authorization` header for `adoFetch()` — a second, independent path to ADO that bypasses the
CLI and contradicts D5.

- Remove `getAuthHeader()` and `adoFetch()`'s direct-`fetch` transport entirely.
- **Spawn the bundled CLI instead:** `child_process.spawn('node', [adoCliPath, method, '--structured'])`,
  JSON body to the child's stdin, stream closed, result from stdout, branch on exit code. Same contract
  the skills use, expressed in JS rather than shell.
- The `-preview` API-version retry quirk for on-prem orgs and the default `api-version: "7.2"` become
  JSON body parameters, not header/URL construction.
- **`fetchBuildFailureLogs()` calls `getAuthHeader()` directly, outside `adoFetch()`** — rewrite it too.
  Removing `adoFetch()` alone leaves a live PAT read behind.
- `scan.mjs`, `classify.mjs`, `state.mjs` unchanged — they consume return values, not transport.

The one place invocation is code rather than instruction. **Not an adapter:** local to `ado-api.mjs`,
exports nothing invocation-related, imported by no other file for that purpose.

### R12 — Agent frontmatter

Verified: all three source agents share `name`, `description`, `user-invocable: true`,
`disable-model-invocation: false`, `modelintelligence` (`5` for the babysit worker, `1` for the other
two), `effort` (`high`/`xhigh`), and `skills: [ado-mentions]`. Three consequences:

- **`skills: - ado-mentions` → `skills: - azure-devops-mentions` in all three.** R4 renames the
  directory; miss this and all three agents silently lose the skill at load time. **This is the one
  cross-reference V4's namespace grep cannot catch** — `ado-mentions` carries no colon. Hence V4's
  second, colon-free check.
- **`name` must match the filename** — so `name: ado-assistant` in `agents/ado-assistant.md` (D4), not
  `azure-devops-devops-assistant`.
- **Do not add a `model` field.** These agents use `modelintelligence` + `effort`. An earlier draft
  required "matching name/model fields," which every migrated agent would violate. This differs from
  the frontmatter example in this repo's root `CLAUDE.md` (`model:`/`tools:`/`permissionMode:`); the
  source shape is preserved, because rewriting model-selection semantics is not part of a transport
  migration.

## Known limitations

Carried into v1.0.0 deliberately. Documented rather than hidden.

| Limitation | Detail |
|---|---|
| **No independent provenance** | The bundle is untracked in the source repo (`claude_plugins/ado/scripts/`) — no git history, no publisher/package identity, no version pin beyond "whatever the working tree has." `provenance.json` records byte size, content hash, copy date, and that its origin is an untracked build artifact rather than a published release. A supply-chain honesty note for future maintainers, not a functional defect. Does not block v1.0.0. |
| **Narrower auth surface** | Only `--pat` and no-flag are reachable. Entra / az-cli / interactive / on-prem modes the MCP server supported are structurally unavailable. Not a regression — this design only ever needed no-flag — but on-prem interactive login is impossible. |
| **Bundle is unreviewable** | 87,719 lines; it cannot be meaningfully reviewed line-by-line as part of this migration. Treated as a vendored third-party dependency: pinned by hash, exercised via its own introspection, trusted at its documented interface. |
| **Mutation policy is not enforced** | D8. A determined or confused model could invoke `manageWorkItemComment action:"delete"` despite the prohibition. The direct cost of the no-adapter architecture, accepted knowingly: the source plugin has the same property today, the alternative was a whole runtime layer, and the highest-risk case is bounded and visible in ADO history. Stated so a maintainer reading "hard-blocked" in an old draft does not assume a control that does not exist. |
| **CA trust is unverified** | The transport is statically established and `NODE_EXTRA_CA_CERTS` is the correct mechanism, but every release check is offline by design, so nothing reaches the network. The first real call in a real sandbox is where CA trust is proven. Failure symptom: TLS validation error surfacing as `E_TRANSPORT`. Fallback: `requestOptions.ca` / explicit-agent configuration. Update this spec before release rather than patching at runtime. |

## Marketplace and docs changes

- **`.claude-plugin/marketplace.json`** — append a third entry to `plugins`, matching the existing two
  entries' style and field shape exactly (no schema change; the registry is already extensible):
  `{"name": "azure-devops", "source": "./azure-devops", "description": "<one paragraph covering:
  CLI-native Azure DevOps workflow automation (work items, PRs, backlog processing) via the bundled
  ado-cli, sandbox-auth-only authentication, no MCP server, no PAT interface>", "version": "1.0.0",
  "category": "development", "tags": ["azure-devops","ado","cli","work-items","pull-requests",
  "backlog","sandbox"], "keywords": ["azure-devops-cli","ado-work-items","ado-pull-requests",
  "ado-backlog","sandbox-auth","work-on","draft-work-item","babysit-pr"]}`
- **Root `README.md`** — add an "azure-devops (v1.0.0)" section under **Available Plugins**, same
  short-paragraph style as `sandbox`/`sandbox-auth`. Update the **Extensibility** mention of
  `azure-devops` from hypothetical future plugin to this real one.
- **New `azure-devops/README.md`** — mirrors `sandbox-auth/README.md`: skills table, exit-code /
  error-name table, the [canonical invocation form](#canonical-invocation-form) including the
  `${CLAUDE_PLUGIN_ROOT}`-is-a-placeholder caveat (now load-bearing, since every skill writes that path
  by hand), and "No PAT, no MCP server, no adapter — sandbox-auth only" up top.
- **`azure-devops/CLAUDE.md`** — per R3.
- **No root-level changes required.** `.gitattributes`, `.gitignore`, root `CLAUDE.md` already cover
  `.mjs` / `.md` / `.json`.

## Release verification

These replace what an earlier draft called a test strategy. **This plugin ships no test suite** (D2).
What it ships is workflow markdown plus a vendored bundle; what follows is validation and review
appropriate to that — not the test suite under another name. Every check is **offline and
credential-free**, so all can run in ordinary CI or by hand. Detail:
[Appendix C](#appendix-c--release-verification-detail).

| Gate | Check |
|---|---|
| **V1 — Plugin validates** | `claude plugin validate ./azure-devops` passes. Manifest parses; every frontmatter block well-formed with `name` matching the containing directory (skills) or filename basename (agents, incl. `ado-assistant`). No `model` field expected (R12). |
| **V2 — Bundled CLI intact** | Against the exact shipped `ado-cli.js`: bare usage, `list --json`, `docs --out <tmp>`, and `help <method>` for a sample all exit `0` with no env vars and no network. `provenance.json` size + hash match. `method-catalog.md` byte-identical to a fresh regeneration, so it cannot silently drift. |
| **V3 — Method refs + Tier 1 resolve** | Every bare method name in every migrated file matches an entry in a fresh `list --json`. `searchWorkItems` (→ `listWorkItems`, R8) and `getPullRequestById` (→ `getPullRequest`) corrected, plus anything else this finds. The Tier 1 list in `CLAUDE.md` equals the referenced set. |
| **V4 — Self-containment** | Zero hits under `azure-devops/` for `ado:`, `development:`, `gh:`, `code-reviewer:`, `debugging:` — confirming R6 removed every external dependency, not just the obvious ones. Colon-free `ado-` grep clean outside its allowed names. |
| **V5 — Privacy scan** | No PAT surface, no environment disclosure. Two greps. |
| **V6 — Workflow instruction review** | The human read no automation can replace. Done last, against the finished tree. |
| **V7 — Marketplace/docs** | `marketplace.json` parses; the new entry's `source` resolves to a real directory; README's new section and updated Extensibility reference present. |
| **V8 — Source repo untouched** | `claude_plugins` has zero uncommitted changes; `ado/scripts/ado-cli.js` and everything else in the source `ado/` plugin remain exactly as before. |

---

# Appendices

## Appendix A — Tier 1 method inventory

**A verified snapshot, not the authority.** Produced by grepping the source plugin's markdown for
camelCase method names, 2026-08-11. It can under-count names appearing only in prose or a table cell
the pattern missed — `addPullRequestComment`, `getWorkItemTypeFields`, `assignWorkItem`,
`addChildWorkItem`, `getQueryResults` are all real catalog methods a migrated skill may legitimately
reference. **Implementation must recompute the set from the actually-migrated files** rather than
transcribing this, and let V3 reconcile. This list exists to fix two concrete errors and to pin the
*definition* of Tier 1 — not to freeze its membership.

- **Referenced by migrated skills/agents today (21):** `addWorkItemComment`, `createLink`,
  `createPullRequest`, `createWorkItem`, `getAllPullRequestChanges`, `getCurrentSprint`,
  `getPullRequest`, `getPullRequestComments`, `getPullRequestFileChanges`, `getSprints`,
  `getSprintWorkItems`, `getTeamMembers`, `getTeams`, `getWorkItemById`, `getWorkItemTypes`,
  `listPullRequests`, `listWorkItems`, `replyToComment`, `updatePullRequestThread`, `updateWorkItem`,
  `updateWorkItemState`.
- **Required by `ado-api.mjs` after R9 (6)** — reached today via its own direct REST calls:
  `getWorkItemComments`, `getWorkItemsBatch`, `getBuilds`, `getPullRequestBuilds`, `getBuildTimeline`,
  `getBuildLog`.
- **Correction 1 — `getPullRequestById` does not exist.** An earlier draft listed it in Tier 1. The
  real method is **`getPullRequest`**, also what the source files reference. No `...ById` variant
  exists for pull requests.
- **Correction 2 — `searchWorkItems` does not exist.** See R8.
- **Removed from Tier 1:** `getDefinitions`, `listWikis`, `getWikiPageContent`, `getCommitHistory`,
  `browseRepository`, `getFileContent`. No migrated file or `ado-api.mjs` path uses them. Leaving them
  in Tier 1 made the same method simultaneously "always allowed" and "gated because no skill
  references it." They fall under Tier 3's out-of-scope catch-all.

## Appendix B — Error vocabulary

| Name | Meaning | How the skill recognizes it | What the skill does |
|---|---|---|---|
| `E_AUTH` | Not authenticated / auth rejected | HTTP 401/403 from ADO in the result body, or a proxy `403 denied` | Run `sandbox-auth:azure-devops` once, retry once, then report |
| `E_NOT_FOUND` | Target resource does not exist | HTTP 404, or an ADO `typeKey` naming a missing work item/PR | Report the specific ID. Do not retry. |
| `E_VALIDATION` | Input failed schema validation | Exit code 2 | Fix the body. `node ado-cli.js help <method>` prints the schema. |
| `E_UNKNOWN_METHOD` | Method not recognized by the bundled CLI | Exit code 3 | Stop — the skill references a method that does not exist. A V3 finding. |
| `E_CONFIG` | Required environment/config missing | Exit code 4 | Report which of `AZURE_DEVOPS_ORG_URL` / `AZURE_DEVOPS_PROJECT` is unset. Do not retry. |
| `E_RATE_LIMIT` | Throttled by ADO | HTTP 429 with `Retry-After` | Wait, retry once. The CLI's `withAuthRetry` already backs off before this surfaces. |
| `E_BLOCKED` | [Mutation policy](#mutation-policy) forbids the call | Tier 2, or Tier 3 without confirmation — recognized *before* invoking | Do not invoke. Explain; for Tier 3, ask. |
| `E_UPSTREAM` | ADO returned a well-formed error with no more specific code | Any other 4xx/5xx with a parsed ADO error body | Surface ADO's own `message` verbatim |
| `E_TRANSPORT` | Could not reach ADO at all | Exit code 1 with no parseable ADO error body, or `node` failing to start | Check proxy/CA preconditions once, then report. **Never quote the proxy URL.** |

## Appendix C — Release verification detail

**Method catalog is generated, not authored.** No hand-maintained per-tool wrapper catalog.
`references/method-catalog.md` is produced by running the bundled CLI's own introspection —
`list --json` for the method/category list, `docs --out <dir>` for per-method parameter docs — and
rendering the result at release time. Skills reference method names directly, as the source skills
already do; the catalog is a cross-check surface, not a layer skills call through. Both subcommands run
with **zero credentials and zero network** (verified: both exit `0` with `AZURE_DEVOPS_ORG_URL` and
`AZURE_DEVOPS_PROJECT` unset), so V2 and V3 run in ordinary CI with no secrets. **Regeneration is a
per-release item, not a one-time step** (V2), so the catalog cannot silently drift from the shipped CLI.

**Counting caveat.** The real catalog is **130 methods** across 10 categories: Work Items 18, Boards &
Sprints 10, Projects 10, Git 25, Testing 14, DevSecOps 13, Artifacts 12, AI-Assisted 12, Wiki 5,
Build 11. `docs --out <dir>` emits **131 files** — it also writes an index `README.md` alongside the
130 `<method>-reference.md` files. **Do not derive the method count from a file count.**

**V5 — two greps over the whole plugin tree.** First: `AZURE_DEVOPS_PAT`,
`AZURE_DEVOPS_PERSONAL_ACCESS_TOKEN`, `AZURE_DEVOPS_BEARER_TOKEN`, `--pat`. The only permitted hits are
this rule's own negative documentation (README / `CLAUDE.md` explaining that no PAT path exists) — zero
hits in any instruction or script that would supply a credential, and in particular `ado-api.mjs` must
contain no `getAuthHeader`. Second: no file may instruct an `env` / `printenv` / `echo "$HTTP_PROXY"` /
`echo "$HTTPS_PROXY"` style environment dump.

**V4 — allowed bare `ado-` names.** The agent `ado-assistant` (D4/R1's recorded exception), and the
filenames `ado-cli.js`, `ado-api.mjs`, `ado-mention-conventions.md`, `ado-state-transitions.md`.
Everything else is a finding. In particular every agent's `skills:` list must name
`azure-devops-mentions`, not `ado-mentions`.

**V6 — workflow instruction review.** A human read of every migrated skill, agent, and command,
checking four things no automated check can. Deliberately last, so it runs against the finished tree.
It carries the most weight in a plugin whose behavior *is* its instructions.

- (a) Every former MCP tool call became a correct [canonical invocation](#canonical-invocation-form) —
  `--structured` present, JSON on stdin, no `--input`, quoted heredoc, quoted `${CLAUDE_PLUGIN_ROOT}`.
- (b) The [mutation policy](#mutation-policy) is restated at each step that could trip it, and no file
  invokes a Tier 2 method or a Tier 3 method without first asking.
- (c) R6's inline-not-delegate rewrites preserve every phase, gate, and outcome contract of the
  originals.
- (d) Error handling branches on exit code and stdout, never on stderr being non-empty.

## Appendix D — Verified facts

All confirmed against the live source on 2026-08-11 by execution and inspection, not inferred from
documentation.

| Fact | Value |
|---|---|
| Source plugin components | 8 skills, 3 agents, 6 commands; MCP-based v3.1.5 |
| Bundle size | 87,719 lines / 3,551,897 bytes |
| Method count | **130** (not 131 — see Appendix C) |
| `fetch(` occurrences in bundle | **0** — no undici |
| HTTP stack | `typed-rest-client` + `tunnel` over `https.request` |
| Proxy env names read | Hardcoded uppercase enum only; no lowercase fallback |
| Reachable auth modes | `--pat`, no-flag `auth:"none"` |
| Exit codes | 0 / 1 / 2 / 3 / 4 as tabled |
| Missing env error | Exit 4, `"AZURE_DEVOPS_ORG_URL and AZURE_DEVOPS_PROJECT must be set"` |
| stderr on success | Auth banner + `DEP0169` — always non-empty |
| `--input -` | Fails `ENOENT ... open '<cwd>/-'` |
| `list --json` / `docs --out` | Exit 0 with no credentials and no network |
| Source `ado/references/` | Exactly 3 files; `ado-state-transitions.md` is **not** among them |
| Agent frontmatter | `modelintelligence` + `effort`; no `model:` field |
| `marketplace.json` entry shape | `name, source, description, version, category, tags, keywords` |
| Root README targets | `## Available Plugins` L29; Extensibility mention L51 |
