# `azure-devops` plugin — CLI-native **port** of the `ado` plugin

**Date:** 2026-08-11 · **Status:** Approved. Ready to implement. No open decisions.
**Source:** `B:\sources\claude_plugins`, plugin `ado/` v3.1.5 — **read-only, never modified**
**Target:** this repo (`B:\sources\sandbox-plugins`), new directory `azure-devops/`, published at
**v1.0.0** in `.claude-plugin/marketplace.json` alongside `sandbox` and `sandbox-auth`

---

## Executive summary

**Goal.** **Port** `claude_plugins/ado` into this marketplace as a self-contained plugin, with
`ado-cli.js` as the execution context. Change the transport. Keep the plugin.

**Porting principle — the governing rule of this spec.**

> This is a **port, not a redesign.** The source plugin's skill/agent/command inventory, workflow
> boundaries, prompts, orchestration, state machines, references, and behavior are preserved exactly,
> wherever they do not depend on the MCP server or the `development` plugin.
>
> **Only these transform:** MCP tool call / MCP setup → direct `node ado-cli.js` invocation; stale CLI
> method names; auth warming; `development:*` delegation → inlined behavior; component naming;
> privacy and portability.
>
> **Nothing else.** No new workflow decomposition. No generic or extracted skills. No redesigned
> responsibilities. No renamed phases. If a change is not on the transform list above, it is out of
> scope — even if it looks like an improvement.

**Naming.** The plugin name already namespaces every component as
`azure-devops:{skill|agent|command}`. **Component names therefore never repeat `azure-devops` or
`ado`.** Skills are `work-on`, `publish-pr`, `babysit-pr`, `draft-work-item`, `work-items`,
`work-my-backlog`, `mentions`. Agents are `assistant`, `pr-tender`, `babysit-pr-worker`. Commands
match their skills. Full old → new table: [Component mapping](#component-mapping).

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

**Workflows included — all of them.** 7 skills, 3 agents, 5 commands. The 8th skill and 6th command
(`setup-ado-mcp`) are dropped because they configure an MCP server that does not exist here. Same
phases, same gates, same decision logic, same prompts as the source plugin.

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

**Files.** `plugin.json`, `CLAUDE.md`, `README.md`, 6 references, 2 scripts, 7 skills, 3 agents,
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
| D4 | Component naming | The plugin name namespaces everything. **Component names carry no `azure-devops` or `ado` prefix.** Routing agent is **`assistant`** → `azure-devops:assistant`. **This supersedes the earlier `ado-assistant` decision** and its "named exception to prefixing" rationale — there is no prefixing rule left to except. |
| D5 | Auth | `sandbox-auth` only. Zero PAT surface. |
| D6 | `work-on` / `draft-work-item` | Inline the `development:*` steps. Do not reimplement that framework, and do not redesign around it. |
| D7 | Version | Fresh at `1.0.0`. Do not continue `3.1.5`. |
| D8 | Mutation policy enforcement | Instruction-level in `CLAUDE.md`. No runtime block exists. |
| D9 | Scope discipline | Port, not redesign. See the porting principle above. |

## Component mapping

Exact old → new. Every source component is accounted for. Directory and file basenames drop the
`ado-` prefix; nothing else about them changes.

| Kind | Source (`claude_plugins/ado`) | This plugin | Invoked as | Transform applied |
|---|---|---|---|---|
| Skill | `skills/ado-work-on/` | `skills/work-on/` | `azure-devops:work-on` | R2, R6 |
| Skill | `skills/ado-publish-pr/` | `skills/publish-pr/` | `azure-devops:publish-pr` | R2 |
| Skill | `skills/ado-babysit-pr/` | `skills/babysit-pr/` | `azure-devops:babysit-pr` | R2 |
| Skill | `skills/ado-draft-work-item/` | `skills/draft-work-item/` | `azure-devops:draft-work-item` | R2, R6, R8 |
| Skill | `skills/ado-work-items/` | `skills/work-items/` | `azure-devops:work-items` | R2, R8 |
| Skill | `skills/ado-work-my-backlog/` | `skills/work-my-backlog/` | `azure-devops:work-my-backlog` | R2, R9 |
| Skill | `skills/ado-mentions/` | `skills/mentions/` | `azure-devops:mentions` | none — verbatim |
| Skill | `skills/setup-ado-mcp/` | **dropped** | — | R10 |
| Agent | `agents/ado-devops-assistant.md` | `agents/assistant.md` | `azure-devops:assistant` | R2, R12 |
| Agent | `agents/ado-pr-tender.md` | `agents/pr-tender.md` | `azure-devops:pr-tender` | R2, R7, R12 |
| Agent | `agents/ado-babysit-pr-worker.md` | `agents/babysit-pr-worker.md` | `azure-devops:babysit-pr-worker` | R2, R7, R12 |
| Command | `commands/ado-work-on.md` | `commands/work-on.md` | `/work-on` | R2 |
| Command | `commands/ado-publish-pr.md` | `commands/publish-pr.md` | `/publish-pr` | R2 |
| Command | `commands/ado-babysit-pr.md` | `commands/babysit-pr.md` | `/babysit-pr` | R2 |
| Command | `commands/ado-draft-work-item.md` | `commands/draft-work-item.md` | `/draft-work-item` | R2 |
| Command | `commands/ado-work-my-backlog.md` | `commands/work-my-backlog.md` | `/work-my-backlog` | R2 |
| Command | `commands/setup-ado-mcp.md` | **dropped** | — | R10 |
| Reference | `references/ado-mention-conventions.md` | same name | — | none — verbatim |
| Reference | `references/review-reception-protocol.md` | same name | — | none — verbatim |
| Reference | `references/review-thread-state-machine.md` | same name | — | R7 |
| Reference | *(from `development`)* `ado-state-transitions.md` | `references/ado-state-transitions.md` | — | copied in, R6 |
| Script | `scripts/ado-cli.js` | same name | — | none — byte-identical |
| Script | `skills/ado-work-my-backlog/scripts/ado-api.mjs` | `skills/work-my-backlog/scripts/ado-api.mjs` | — | R9 |
| Script | `…/scripts/{classify,scan,state}.mjs` | same names | — | none — verbatim |

**Reference and script *filenames* keep their `ado-` prefix.** They are not components, are not
namespaced, and are referenced by path from ported prompts. Renaming them would be a redesign edit
with no benefit and would break the verbatim-copy property. Only skills, agents, and commands — the
things the namespace prefixes — lose the prefix.

## What changes / What does not change

**Changes — the complete transform list. Nothing outside it:**

- Transport: MCP tool call → `node ado-cli.js <method> --structured` with JSON on stdin.
- Auth: `ado:setup-ado-mcp` (writes a PAT into an MCP config) → `sandbox-auth`. Dropped, not ported.
- Naming: `ado:` → `azure-devops:`; component basenames drop the `ado-` prefix entirely (D4).
- Stale CLI method names corrected (R8).
- `work-on` / `draft-work-item`: `development:*` delegations inlined — same steps, same order.
- `ado-api.mjs`: direct `fetch` + PAT header removed; it spawns the CLI instead.
- Dropped: `examples/`, `launch-ado-mcp.sh`, `setup-ado-mcp` skill + command.
- Version resets to `1.0.0`.

**Does not change:**

- **The inventory.** Same skills, same agents, same commands. Nothing merged, split, extracted, or
  added.
- **The workflow boundaries.** Which skill owns which job is exactly as the source plugin has it.
- **The prompts.** Wording, headings, phase names, tables, and examples are carried over as-is except
  where a transform above touches the line.
- **The orchestration and state machines.** Phase order, branch conditions, retry and revision caps,
  thread lifecycle, sub-agent dispatch shape.
- **Method names.** `getWorkItemById` stays `getWorkItemById`. Same params, same order, same meaning.
- The HITL feedback checkpoint, the v3 revision cap, append-only comments.
- Agent frontmatter shape (`modelintelligence` + `effort`, **not** `model`).
- `claude_plugins`. Nothing is committed, staged, edited, or squashed there. The bundle is **copied
  out**, never moved.


## Problem

The source plugin (8 skills, 3 agents, 6 commands) is built entirely on the
`@achieveai/azuredevops-mcp` MCP server. The workflows are good; the transport is unusable here:

- No MCP server registration surface exists in a sandboxed session. This marketplace is CLI/skill-native.
- The egress proxy injects auth transparently. A "write a token into a config file" skill is actively wrong here.
- There is no `development` plugin here, and `ado-work-on` / `ado-draft-work-item` delegate to it.

So the problem is narrow: **swap the transport and the two external dependencies, keep everything
else.** What makes that possible: `ado/scripts/ado-cli.js`, an 87,719-line esbuild bundle of the same
TypeScript project exposing the same 130 tools through a `node ado-cli.js <method>` contract — same
`registerTools()`, same Zod schemas. **Self-sufficient:** no wrapper, no per-tool shim. Because the
tool surface is identical, the prompts that call it need only their call syntax changed.

**Why not MCP.** `web-research.md`'s "Plugin Architecture for Sandbox" section recommends an Express
MCP server (`mcp/server.json` + a Node HTTP listener) for exactly this. **Overridden, permanently.**
No port to bind usefully, no client to register with, and it reintroduces the dependency this port
removes.

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

1. A user or the model invokes an `azure-devops:*` skill, directly or via its command.
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
- **The stderr auth banner is an invariant on success.** `ado-cli.js` prints
  `[Auth] Auth type: none, PAT: not set` on every exit-0 call — auth initializes, and this line
  prints, before any method runs. Under this design it is *always* exactly that on success. A
  non-zero exit is not guaranteed to carry it (e.g. exit 4, missing env vars, fails before auth
  initializes) — that case is out of scope for this invariant. Enforcement is selective, not
  uniform: direct/shell CLI invocations stay instruction-only (`CLAUDE.md` rule 5, no adapter, no
  runtime block), but `ado-api.mjs` — the one code-level transport (R9) — validates the banner on
  every successful call and rejects with a distinct `AuthInvariantError` if it's ever missing or
  wrong, rethrown ahead of ordinary fallback handling in every catch path in that module so it can
  never be silently swallowed. Its message is fixed and generic — it never quotes the raw banner or
  stderr — so a leaked credential still cannot reach a log line or a user-facing error through this
  path.
- **Append-only comments** — see [Mutation policy](#mutation-policy). A `CLAUDE.md` prohibition and a
  V6 review item, not a runtime block.

## Mutation policy

Invoking an `azure-devops:*` skill already authorizes the ordinary mutations that skill performs. No
new gate is added on top of what the source plugin does today. Extra confirmation applies only to the
exceptional: destructive, irreversible, or outside what any ported skill uses.

**Enforced by instruction, not by code (D8).** No adapter, so no code path can refuse a call. The tiers
live in `azure-devops/CLAUDE.md`, are restated at the step inside each skill that could trip them, and
are checked by V6. Same kind of enforcement the source plugin already relies on for its append-only
rule — not a regression from a working control, but not a technical control either. A deliberate trade
of a runtime guarantee for removing a whole layer of code. Recorded in
[Known limitations](#known-limitations). Written imperative, to be copied verbatim into `CLAUDE.md`:

| Tier | Scope | Rule |
|---|---|---|
| **1 — Ordinary** | Exactly the methods ported files reference by name | Proceed. No new gate. |
| **2 — Forbidden** | `manageWorkItemComment` with `action: "update"` or `"delete"` | **Never invoke**, by any skill, agent, command, or script. To correct a comment, post a new one. |
| **3 — Confirm first** | Destructive, or out-of-scope | Ask explicitly, naming the action and the resource, before invoking. Proceed only on an affirmative. |

- **Tier 3, destructive by nature** — regardless of whether a ported skill calls it:
  `mergePullRequest`, `deletePackageVersion`, `rotateSecrets`, `manageSecurityPolicies`, `runPipeline`,
  overwrite-style `createOrUpdateWikiPage`, any bulk create/update/delete.
- **Tier 3, out-of-scope catch-all** — any method not referenced by name in any ported file
  (cross-checked against `method-catalog.md`). A method no shipped skill uses is exceptional by
  definition, even if it looks benign.
- **Tier 1 is a definition, not a hand-maintained list.** It *is* the referenced set. V3 checks that
  Appendix A's inventory equals what the files reference, so it cannot drift — either direction is a
  release-time finding. `CLAUDE.md` carries Tier 1's behavioural definition inside the canonical
  `<mutation_privacy_policy>` block (bullet 1), not an enumeration; the enumeration, plus two encoded
  corrections, lives in [Appendix A](#appendix-a--tier-1-method-inventory).
- Tier 2's method name appears in no ported file except as the subject of the prohibition, so V3 and
  V6 both surface any reintroduction.
- Skills that already confirm before creating/updating (`work-items`' "always confirm
  before making changes", `draft-work-item`'s mandatory preview) keep those prompts
  exactly as-is. This layer does not touch them.

**Concrete restatement mechanism.** A plugin-root `CLAUDE.md` is documentation, not a guaranteed
always-loaded context file (see I-1 in the Task 16 release-gate history) — so the tiers plus the
Security-and-privacy bullets live as a canonical, tag-wrapped `<mutation_privacy_policy>` block in
`CLAUDE.md` itself, and are additionally restated byte-identical in every skill/agent whose mutation
surface could trip them. That restatement set is exactly the components Appendix A's Tier-1 set names as
referencing an ordinary mutation, plus the three agents — 9 components total: `work-on`, `publish-pr`,
`babysit-pr`, `work-items`, `work-my-backlog`, `draft-work-item`, `assistant`, `pr-tender`,
`babysit-pr-worker`. `mentions` and the 5 `commands/*.md` dispatch wrappers carry no copy, deliberately —
they have zero CLI mutation surface, and an 11th copy would be policy text in a file that cannot trip it.
`README.md` references the block by name but does not itself carry a tag-wrapped copy. V6(b) verifies
that there are exactly 10 tag-wrapped `<mutation_privacy_policy>` occurrences tree-wide — the 1 canonical
copy in `CLAUDE.md` plus the 9 restatements, named above — and that all 10 hash identical (single
distinct SHA-256).

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
│   ├── provenance.json                 new — origin, byte size, content hash, copy date
│   ├── method-catalog.md               generated, not authored — see Appendix C
│   └── error-codes.md                  new — Appendix B
├── scripts/
│   └── ado-cli.js                      verbatim from ado/scripts/ado-cli.js
├── skills/
│   ├── work-on/
│   │   ├── SKILL.md                    from ado-work-on — inline-not-delegate, R6
│   │   └── reference/                  generic guides from development/skills/work-on/reference/
│   ├── publish-pr/SKILL.md             from ado-publish-pr
│   ├── babysit-pr/SKILL.md             from ado-babysit-pr
│   ├── draft-work-item/SKILL.md        from ado-draft-work-item — inline-not-delegate, R6
│   ├── work-items/SKILL.md             from ado-work-items
│   ├── work-my-backlog/
│   │   ├── SKILL.md                    from ado-work-my-backlog
│   │   └── scripts/{ado-api.mjs,classify.mjs,scan.mjs,state.mjs}   see R9
│   └── mentions/SKILL.md               from ado-mentions, verbatim
├── agents/
│   ├── babysit-pr-worker.md            from ado-babysit-pr-worker.md
│   ├── pr-tender.md                    from ado-pr-tender.md
│   └── assistant.md                    from ado-devops-assistant.md — D4
└── commands/
    └── {work-on,publish-pr,babysit-pr,draft-work-item,work-my-backlog}.md
```

Same count, same names, same responsibilities as the source — minus the prefix, minus
`setup-ado-mcp`. Full correspondence: [Component mapping](#component-mapping).

**Deliberately absent:** `tests/`, `package.json`, `scripts/invoke-ado-cli.mjs`, `mcp/`, `examples/`,
`launch-ado-mcp.sh`, any `setup-ado-mcp` component. The only two files under `scripts/` are the
vendored bundle and its provenance record.

## Port rules

Each rule is a member of the transform list in the [porting principle](#executive-summary). Nothing
outside these rules changes.

| # | Rule |
|---|---|
| **R1** | **Namespace only. No basename prefix.** `ado:` → `azure-devops:`, and component basenames simply **drop** `ado-`: `skills/ado-work-on/` → `skills/work-on/`, `agents/ado-pr-tender.md` → `agents/pr-tender.md`, `commands/ado-publish-pr.md` → `commands/publish-pr.md`. The plugin name already qualifies every component as `azure-devops:<name>`, so re-stating it in the name is pure stutter and `ado-` is a stale namespace. `ado-devops-assistant` → **`assistant`** (`azure-devops:assistant`) falls straight out of the same rule — no exception needed (D4). Reference and script *filenames* (`ado-cli.js`, `ado-api.mjs`, `ado-mention-conventions.md`, `ado-state-transitions.md`) are not components, are not namespaced, and keep their names. No procedure, phase structure, or decision logic is rewritten. |
| **R2** | **MCP tool call → direct CLI call, mechanically.** Every "call `<toolName>`" becomes `node "${CLAUDE_PLUGIN_ROOT}/scripts/ado-cli.js" <toolName> --structured` with the same parameters, same order, same purpose, per the [canonical form](#canonical-invocation-form). Method names unchanged. Nothing to import. Surrounding prompt text is left alone. |
| **R3** | **`CLAUDE.md` carries forward and gains three jobs.** "Comments Are Append-Only" copied verbatim. "MCP Prerequisite — Auto-Setup" replaced per [Authentication](#authentication-and-environment). Because there is no adapter, it is now also the single home for (a) the canonical invocation form, (b) the session warm-up preamble, (c) the full three-tier mutation policy. One always-loaded file replaces the "one file tells you the whole policy" property the adapter used to give. |
| **R4** | `ado-mentions` → `mentions`, copied **verbatim**. Directory rename only; body untouched. |
| **R5** | `ado-publish-pr`, `ado-babysit-pr`, `ado-work-items`, `ado-work-my-backlog` port **1:1** — same phases, same decision logic, same wording. Only R1, R2, R8 and (for the backlog skill) R9 touch them. |
| **R8** | **Stale method names fixed.** `ado-work-items/SKILL.md` line 27 and `ado-draft-work-item`'s Duplicate Check table both name **`searchWorkItems`, which does not exist** among the 130 real methods. Replacement is **`listWorkItems`** — the catalog calls it the *"preferred structured work item query path. Usually 1 WIQL call plus 1 batched hydrate call for up to 200 returned items,"* exactly what the stale name reached for. Line 27 already names `listWorkItems` alongside it, so **drop the `or searchWorkItems` clause** rather than substitute. Per call site, consider `getMyWorkItems` (assigned-to-me) or `getQueryResults` (saved queries) instead of a blind swap. General cross-check is V3. |
| **R10** | Drop MCP-only artifacts: `examples/`, `launch-ado-mcp.sh`, `setup-ado-mcp` skill + command. They exist only to configure the MCP server. |
| **R11** | `plugin.json` starts at `"version": "1.0.0"`. The `3.1.5` lineage is not continued — same pattern this repo used for `sandbox`. |

### R6 — `work-on` / `draft-work-item`: inline, do not delegate

The most invasive rule, and still not a redesign: the phase structure is preserved exactly and only
the delegation *targets* change.

Their only ADO-specific content is the "Azure DevOps" column of their `GitHub | Azure DevOps` tables
plus the provider-resolution/tooling phases. Everything else delegates to `development:work-on` /
`development:draft-work-item`, absent here — which in turn delegate to `development:autonomous-design`,
`:implement`, `:blind-spot-detector`, `:draft-feature`, `:draft-bug`, `debugging:debug-with-logs`,
`debugging:systematic-debugging`, `code-reviewer:pr-review`. Also absent. Reimplementing that framework
is out of scope; so is restructuring these skills to avoid it.

- **Keep the full phase structure verbatim:** Phase 0 provider resolution (→ a no-op single-provider
  statement), 1 auto-detect mode, 1.1 fetch & understand, 1.2 route by type, **the mandatory Phase 1.5
  feedback checkpoint**, Part 2 execute & deliver, Error Handling, Reference Conventions, Decision Log.
  Every ADO-column cell kept. No phase is renamed, merged, split, or reordered.
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
  into `skills/work-on/reference/`. Provider-neutral beyond the tables above.
- **`ado-state-transitions.md` also comes from `development`, not `ado/`.** Verified: the source
  `references/` holds exactly three files (`ado-mention-conventions.md`, `review-reception-protocol.md`,
  `review-thread-state-machine.md`). This one lives at
  `development/skills/work-on/reference/ado-state-transitions.md`, consumed by `development:work-on`
  when its provider is ADO. `work-on` absorbs that behavior, so copy it in under its original filename.
  Only `ado-mention-conventions.md` is genuinely native to the source plugin.

### R7 — `review-thread-state-machine.md`: two fixes

**(a) Dangling sync note.** The source opens with "this lifecycle is copied into the code-reviewer and
ADO plugins; the copies must remain byte-identical." `code-reviewer` does not exist here, so the claim
dangles. Replace with: *"this lifecycle document describes the PR-review thread conventions this
plugin's `babysit-pr-worker` agent follows; it originated as a shared reference with a `code-reviewer`
plugin that is not part of this marketplace."*

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

This is the one place the port resolves a contradiction rather than carrying it. It is in scope because
the two source files cannot both be ported faithfully — they disagree.

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
- **Function names, exports, and call sites stay as they are.** This is a transport swap inside existing
  functions, not a refactor.
- `scan.mjs`, `classify.mjs`, `state.mjs` unchanged — they consume return values, not transport.

The one place invocation is code rather than instruction. **Not an adapter:** local to `ado-api.mjs`,
exports nothing invocation-related, imported by no other file for that purpose.

### R12 — Agent frontmatter

Verified: all three source agents share `name`, `description`, `user-invocable: true`,
`disable-model-invocation: false`, `modelintelligence` (`5` for the babysit worker, `1` for the other
two), `effort` (`high`/`xhigh`), and `skills: [ado-mentions]`. Three consequences:

- **`skills: - ado-mentions` → `skills: - mentions` in all three.** R4 renames the directory; miss this
  and all three agents silently lose the skill at load time. **This is the one cross-reference V4's
  namespace grep cannot catch** — `ado-mentions` carries no colon. Hence V4's second, colon-free check.
- **`name` must match the filename** — `name: assistant` in `agents/assistant.md`, `name: pr-tender` in
  `agents/pr-tender.md`, `name: babysit-pr-worker` in `agents/babysit-pr-worker.md`. No prefixes (D4).
- **Do not add a `model` field.** These agents use `modelintelligence` + `effort`. An earlier draft
  required "matching name/model fields," which every ported agent would violate. This differs from
  the frontmatter example in this repo's root `CLAUDE.md` (`model:`/`tools:`/`permissionMode:`); the
  source shape is preserved, because rewriting model-selection semantics is not part of a transport
  port.
- `description` bodies carry over as written. They are the trigger text the source plugin tuned.

## Known limitations

Carried into v1.0.0 deliberately. Documented rather than hidden.

| Limitation | Detail |
|---|---|
| **No independent provenance** | The bundle is untracked in the source repo (`claude_plugins/ado/scripts/`) — no git history, no publisher/package identity, no version pin beyond "whatever the working tree has." `provenance.json` records byte size, content hash, copy date, and that its origin is an untracked build artifact rather than a published release. A supply-chain honesty note for future maintainers, not a functional defect. Does not block v1.0.0. |
| **Narrower auth surface** | Only `--pat` and no-flag are reachable. Entra / az-cli / interactive / on-prem modes the MCP server supported are structurally unavailable. Not a regression — this design only ever needed no-flag — but on-prem interactive login is impossible. |
| **Bundle is unreviewable** | 87,719 lines; it cannot be meaningfully reviewed line-by-line as part of this port. Treated as a vendored third-party dependency: pinned by hash, exercised via its own introspection, trusted at its documented interface. |
| **Mutation policy is not enforced** | D8. A determined or confused model could invoke `manageWorkItemComment action:"delete"` despite the prohibition. The direct cost of the no-adapter architecture, accepted knowingly: the source plugin has the same property today, the alternative was a whole runtime layer, and the highest-risk case is bounded and visible in ADO history. Stated so a maintainer reading "hard-blocked" in an old draft does not assume a control that does not exist. |
| **CA trust is unverified** | The transport is statically established and `NODE_EXTRA_CA_CERTS` is the correct mechanism, but every release check is offline by design, so nothing reaches the network. The first real call in a real sandbox is where CA trust is proven. Failure symptom: TLS validation error surfacing as `E_TRANSPORT`. Fallback: `requestOptions.ca` / explicit-agent configuration. Update this spec before release rather than patching at runtime. |

## Marketplace and docs changes

- **`.claude-plugin/marketplace.json`** — append a third entry to `plugins`, matching the existing two
  entries' style and field shape exactly (no schema change; the registry is already extensible):
  `{"name": "azure-devops", "source": "./azure-devops", "description": "<one paragraph covering:
  CLI-native Azure DevOps workflow automation (work items, PRs, backlog processing) via the bundled
  ado-cli, sandbox-auth-only authentication, no MCP server, no PAT interface>", "version": "1.0.0",
  "category": "development", "tags": ["azure-devops","ado","cli","work-items","pull-requests",
  "backlog","sandbox"], "keywords": ["azure-devops","ado-cli","work-items","pull-requests",
  "backlog","sandbox-auth","work-on","draft-work-item","babysit-pr"]}`

  Keywords are marketplace search terms, not component names — the D4/R1 naming rule does not apply
  to them.
- **Root `README.md`** — add an "azure-devops (v1.0.0)" section under **Available Plugins**, same
  short-paragraph style as `sandbox`/`sandbox-auth`. Update the **Extensibility** mention of
  `azure-devops` from hypothetical future plugin to this real one.
- **New `azure-devops/README.md`** — mirrors `sandbox-auth/README.md`: skills table, exit-code /
  error-name table, the [canonical invocation form](#canonical-invocation-form) including the
  `${CLAUDE_PLUGIN_ROOT}`-is-a-placeholder caveat (now load-bearing, since every skill writes that path
  by hand), and "No PAT, no MCP server, no adapter — sandbox-auth only" up top.
- **`azure-devops/CLAUDE.md`** — per R3.
- **One narrow root-level change is required, and only this one.** `.gitattributes`, `.gitignore`, root
  `CLAUDE.md` already cover `.mjs` / `.md` / `.json`, but the vendored bundle needs an explicit line this
  repo's existing rules don't provide: `azure-devops/scripts/ado-cli.js binary` appended to the root
  `.gitattributes`. Reason: this repo's blanket `* text=auto`, combined with a checkout machine's
  `core.autocrlf=true`, normalizes the bundle's line endings on checkout — silently invalidating
  `provenance.json`'s pinned SHA-256 and breaking the byte-identical vendoring guarantee that V2/V8 and
  Task 17's fresh-clone check depend on. Verified: `git check-attr` + a forced `git checkout --` of the
  file reproduce the source's exact hash only with this rule in place. No other root-level file changes
  are in scope.

## Release verification

These replace what an earlier draft called a test strategy. **This plugin ships no test suite** (D2).
What it ships is workflow markdown plus a vendored bundle; what follows is validation and review
appropriate to that — not the test suite under another name. Every check is **offline and
credential-free**, so all can run in ordinary CI or by hand. Detail:
[Appendix C](#appendix-c--release-verification-detail).

| Gate | Check |
|---|---|
| **V1 — Plugin validates** | `claude plugin validate ./azure-devops` passes. Manifest parses; every frontmatter block well-formed with `name` matching the containing directory (skills) or filename basename (agents). No component name contains `azure-devops` or `ado` (D4/R1). No `model` field expected (R12). |
| **V2 — Bundled CLI intact** | Against the exact shipped `ado-cli.js`: bare usage exits `1` by design (usage/error text, no method given); `list --json`, `docs --out <tmp>`, and `help <method>` for a sample all exit `0` with no env vars and no network. `provenance.json` size + hash match. `method-catalog.md` byte-identical to a fresh regeneration, so it cannot silently drift. |
| **V3 — Method refs + Tier 1 resolve** | Every bare method name in every ported file matches an entry in a fresh `list --json`. `searchWorkItems` (→ `listWorkItems`, R8) and `getPullRequestById` (→ `getPullRequest`) corrected, plus anything else this finds. The Tier-1 inventory in Appendix A equals the referenced set; `CLAUDE.md` carries the behavioural definition, not an enumeration. |
| **V4 — Self-containment + naming** | Zero hits under `azure-devops/` for `ado:`, `development:`, `gh:`, `code-reviewer:`, `debugging:` — confirming R6 removed every external dependency, not just the obvious ones. Zero hits for `azure-devops-` in any component name, path, or `skills:` entry. Colon-free `ado-` grep clean outside the four allowed filenames. |
| **V5 — Privacy scan** | No PAT surface, no environment disclosure. Two greps, with a narrow, hash-gated exception for the `<mutation_privacy_policy>` block's own prohibition text — see Appendix C and V6(b). |
| **V6 — Port fidelity review** | The human read no automation can replace: every ported file still does what its source did. Plus two required sub-checks (documented commands, not yet a committed test harness): (a) every canonical-invocation heredoc terminates at column 0 and executes clean, (b) all 10 mutation-policy occurrences (`CLAUDE.md`'s canonical copy + 9 skill/agent restatements) are present and hash-identical. Done last, against the finished tree. |
| **V7 — Marketplace/docs** | `marketplace.json` parses; the new entry's `source` resolves to a real directory; README's new section and updated Extensibility reference present. |
| **V8 — Source repo untouched** | `claude_plugins` has zero uncommitted changes; `ado/scripts/ado-cli.js` and everything else in the source `ado/` plugin remain exactly as before. |

---

# Appendices

## Appendix A — Tier 1 method inventory

**A verified snapshot, not the authority.** Produced by grepping the source plugin's markdown for
camelCase method names, 2026-08-11. It can under-count names appearing only in prose or a table cell
the pattern missed — `addPullRequestComment`, `getWorkItemTypeFields`, `assignWorkItem`,
`addChildWorkItem`, `getQueryResults` are all real catalog methods a ported skill may legitimately
reference. **Implementation must recompute the set from the actually-ported files** rather than
transcribing this, and let V3 reconcile. This list exists to fix two concrete errors and to pin the
*definition* of Tier 1 — not to freeze its membership.

- **Referenced by ported skills/agents today (21):** `addWorkItemComment`, `createLink`,
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
  `browseRepository`, `getFileContent`. No ported file or `ado-api.mjs` path uses them. Leaving them
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
`AZURE_DEVOPS_PERSONAL_ACCESS_TOKEN`, `AZURE_DEVOPS_BEARER_TOKEN`, `--pat`. Second: no file may instruct
an `env` / `printenv` / `echo "$HTTP_PROXY"` / `echo "$HTTPS_PROXY"` style environment dump. The same
three exceptions apply to both greps:

1. This rule's own negative documentation outside the tagged block (README / `CLAUDE.md` prose
   explaining that no PAT path exists and that no environment dump is permitted, not itself wrapped in
   `<mutation_privacy_policy>` tags). **Currently zero-hit** — as of the block's latest content (commit
   `27cc8bd`), both README's tier summary and everything in `CLAUDE.md` outside the tag trip neither
   probe; the exception is retained as a permissive allowance in case negative prose is reintroduced
   outside the tag, not because it currently accounts for any observed hit.
2. The generated `references/method-catalog.md`'s reproduction of the CLI's own usage banner — required
   to remain present verbatim by V2's byte-identity check, and not itself a plugin PAT surface since the
   plugin never passes `--pat`. (Applies only to the first grep; the banner names no environment dump.)
   Currently the only non-block hit: 1 occurrence, first grep only.
3. **Hash-gated `<mutation_privacy_policy>` block exception.** A hit that falls inside the span between a
   file's `<mutation_privacy_policy>` and `</mutation_privacy_policy>` tags — the canonical copy in
   `CLAUDE.md`, or one of its 9 restatements in every mutating skill/agent, see
   [Mutation policy](#mutation-policy) — is permitted **only once V6(b) has confirmed that file's span
   hashes identical to the canonical text** (a single distinct SHA-256 across all 10 tag-wrapped
   occurrences). A hit outside a verified span, or inside a block whose hash differs from the canonical
   one, is **not** covered by this exception and is a finding, exactly as before this block existed. This
   exception is deliberately narrow, not a blanket allowance: the block's own text is exclusively a
   *prohibition* of `--pat` / `env` / `printenv` / proxy-value-printing — it never contains an actual
   secret, a working PAT recommendation, or an instruction to print a real value. V5 must still fail on
   any of those, block or no block, anywhere in the tree. **Currently accounts for every other hit:** 10
   of 11 secret-probe hits and all 10 env-probe hits fall inside a tag span.

Zero hits in any instruction or script that would supply a credential, and in particular `ado-api.mjs`
must contain no `getAuthHeader`.

**V4 — naming checks, two greps.**

- **Prefixed component names.** Zero hits for `azure-devops-` and zero for `ado-` in any directory
  name under `skills/`, any filename under `agents/` or `commands/`, any frontmatter `name:`, and any
  frontmatter `skills:` entry. Every agent's `skills:` list must name **`mentions`**, not
  `ado-mentions` and not `azure-devops-mentions`. This is the check R12 flags as uncatchable by the
  namespace grep — `ado-mentions` carries no colon.
- **Allowed bare `ado-` strings, whole tree.** Exactly four filenames: `ado-cli.js`, `ado-api.mjs`,
  `ado-mention-conventions.md`, `ado-state-transitions.md` — plus the `ado-*` marketplace keywords and
  prose that names the *source* plugin's components. Anything else is a finding. There is **no allowed
  bare `ado-` component name**; the earlier `ado-assistant` exception is withdrawn (D4).

**V6 — port fidelity review.** A human read of every ported skill, agent, and command, checking five
things no automated check can. Deliberately last, so it runs against the finished tree. It carries the
most weight in a plugin whose behavior *is* its instructions.

- (a) Every former MCP tool call became a correct [canonical invocation](#canonical-invocation-form) —
  `--structured` present, JSON on stdin, no `--input`, quoted heredoc, quoted `${CLAUDE_PLUGIN_ROOT}`,
  and the heredoc's terminator line (`ADOJSON` for a `<<'ADOJSON'` opener) sitting at column 0 with no
  leading whitespace. A quoted, non-`<<-` heredoc only recognizes an exact, unindented terminator match —
  an indented one silently folds itself and every following line into the JSON body instead of ending it
  (this exact defect: C-1, Task 16). Required, every release, by both: a static grep for an indented
  terminator (`^[ \t]+ADOJSON[ \t]*$` across every `.md` file, expect 0 matches) **and** executing every
  such fenced block against a stubbed CLI with a trailing sentinel line, confirming the sentinel prints.
  This is a release-checklist requirement, not a claim that a committed test harness already runs it —
  the extraction/stub/sentinel script that performs the execution check currently lives, uncommitted, at
  `.superpowers/temp/exec-fences.mjs`; promoting it to a tracked, repo-committed script is future work, not
  yet done. A block whose sentinel does not print is a finding regardless of what the static grep alone
  shows — the two documentation-template blocks in `CLAUDE.md`/`README.md` that use a literal `<method>`
  placeholder are excluded, since they cannot execute and are not canonical invocations.
- (b) The [mutation policy](#mutation-policy) is restated at each step that could trip it, and no file
  invokes a Tier 2 method or a Tier 3 method without first asking. Required sub-check, every release:
  `CLAUDE.md` carries the canonical `<mutation_privacy_policy>...</mutation_privacy_policy>` block, and
  every mutating skill/agent restates it byte-identical — exactly 9 restatement files (`work-on`,
  `publish-pr`, `babysit-pr`, `work-items`, `work-my-backlog`, `draft-work-item`, `assistant`,
  `pr-tender`, `babysit-pr-worker`; `mentions` and the 5 `commands/*.md` wrappers carry none, since they
  cannot mutate), for 10 tag-wrapped occurrences tree-wide (1 canonical + 9 restatements). All 10
  extracted spans must hash to a single, identical SHA-256 — the file-count check is a one-line
  `Select-String`, runnable by hand every release; the span-extraction-and-hash step is documented intent
  here and in the plan, not yet a committed script (same ephemeral status as V6(a)'s execution check). A
  restatement count other than 9, a total
  other than 10, or more than one distinct hash among the 10, is a finding — and is what V5's hash-gated
  exception (above) depends on to stay narrow.
- (c) R6's inline-not-delegate rewrites preserve every phase, gate, and outcome contract of the
  originals.
- (d) Error handling branches on exit code and stdout, never on stderr being non-empty.
- (e) **Port fidelity.** Diff each file against its source counterpart. Every difference must map to a
  named rule (R1-R12). An unexplained difference is an unauthorized redesign and is a finding — no
  matter how much it improves the file.

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
