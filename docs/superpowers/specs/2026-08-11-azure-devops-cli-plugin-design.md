# `azure-devops` domain plugin: CLI-native migration of the ADO workflow

**Date:** 2026-08-11
**Status:** Approved design — ready for implementation
**Source repo:** `B:\sources\claude_plugins`, plugin `ado/` (version `3.1.5`) — read-only input, **not modified**
**Destination:** this repo, `B:\sources\sandbox-plugins`, new plugin directory `azure-devops/`
**Target marketplace release:** `azure-devops` v1.0.0, published in `.claude-plugin/marketplace.json` alongside the existing `sandbox` and `sandbox-auth` entries

## Problem

`claude_plugins/ado` is a mature Azure DevOps workflow plugin (8 skills, 3 agents, 6 commands) built
entirely on the `@achieveai/azuredevops-mcp` MCP server: every skill/agent instructs the model to call
named MCP tools (`getWorkItemById`, `createWorkItem`, `addWorkItemComment`, `listPullRequests`, …), and
auth is configured via `ado:setup-ado-mcp`, which writes a PAT or interactive/Entra credential into an
MCP server config.

None of that fits `sandbox-plugins`:

- There is no MCP server registration surface inside a sandboxed session; this marketplace is
  CLI/skill-native (see `sandbox-auth`'s own design — a Python probe script + skills, not an MCP
  server).
- The sandbox's egress proxy injects auth transparently; agents never hold or configure a PAT. A
  `setup-ado-mcp`-style "write a token into a config file" skill is actively wrong in this environment.
- `sandbox-plugins` has no `development` plugin, and the source plugin's `ado-work-on` /
  `ado-draft-work-item` skills are thin wrappers that delegate to `development:work-on` /
  `development:draft-work-item` for the actual orchestration engine.

The plugin also bundles `ado/scripts/ado-cli.js` — an 87,719-line esbuild bundle of the same
TypeScript project's CLI entry point, exposing the same 131 tools as the MCP server through a
`node ado-cli.js <method>` command-line contract. This CLI is the mechanism that makes a CLI-native
port possible: it is functionally 1:1 with the MCP tools the skills already reference (same
`registerTools()` function, same Zod schemas), but it ships as an untracked, unversioned prototype
with no test coverage, no mutation safeguards, and an auth surface narrower than what the skills
assume (see [Known limitations](#known-limitations-carried-into-v100) — this spec closes the safety
gaps and documents the provenance gap rather than silently ignoring either).

## Goals

- Publish a new, self-contained `azure-devops` domain plugin in this marketplace at v1.0.0, matching
  the domain name this repo's own `README.md` already names as an expected future addition.
- Preserve the full existing ADO workflow — all skills, agents, commands, and their behavior — by
  **transforming** MCP tool calls into CLI adapter calls, not rewriting the workflows from scratch.
- Make authentication sandbox-auth-only: reuse the existing `sandbox-auth:azure-devops` +
  `sandbox-auth:egress-auth` skills for the proxy-injected auth handshake. No PAT configuration
  surface, no MCP server, no credential file.
- Ship `ado-cli.js` as the bundled execution engine, invoked through one new shared adapter
  (`invoke-ado-cli.mjs`) rather than duplicated ad hoc `child_process` calls per skill/script.
- Make the plugin self-contained: no dependency on a `development` (or any other) plugin that does not
  exist in this repo.
- Introduce a mutation policy that keeps every mutation the current skills already perform working
  without new confirmation prompts, while adding a hard technical gate on the small set of actions that
  are destructive, out-of-scope, or already forbidden by the plugin's own rules.
- Introduce a stable, named error-mapping layer so every skill gets consistent, actionable failures
  instead of raw CLI stderr/exit codes.
- Add real test coverage (unit, static, workflow) where today there is none.
- Update this repo's marketplace registry, README, and docs to reflect the new plugin.
- Leave `claude_plugins` completely unmodified.

## Non-goals

- Do not add an MCP server, `mcp/server.json`, or any Express/Node server process. `web-research.md`'s
  "Plugin Architecture for Sandbox" section recommends exactly this — **that recommendation is
  overridden by this spec.** The design is CLI-native: a single generic adapter shells out to the
  bundled `ado-cli.js` and returns structured results. No listener, no port, no server lifecycle.
- Do not build a PAT-entry, device-code, or any other credential-collection UI. Authentication is
  100% delegated to `sandbox-auth`.
- Do not fully re-implement the generic `development:work-on` / `development:draft-work-item`
  orchestration engines (design-review gates, TDD execution loop, blind-spot detection, bug RCA
  workflow) inside this plugin. Reproducing that framework is out of scope for an ADO-to-CLI
  migration; see [Workflow mapping — `azure-devops-work-on` and `azure-devops-draft-work-item`](#workflow-mapping)
  for the inline-not-delegate resolution actually adopted.
- Do not close the `ado-cli.js` provenance gap (no git history, no publisher, no independent version)
  as part of v1.0.0 — it is documented as a known limitation, not blocked on.
- Do not migrate MCP-specific artifacts: `launch-ado-mcp.sh`, `examples/azure-devops.env.example`,
  `examples/claude/.mcp.json.example`, `examples/copilot/mcp-config.json.example`, or the
  `setup-ado-mcp` skill/command. These describe a setup path this plugin does not have.
- Do not create the plugin's git commit history retroactively or squash/rewrite `claude_plugins`
  history — this spec only adds this one document.
- Do not push anything; this spec's only repository action is committing itself.

## Architecture

### CLI-native, no MCP server

```
   agent (skill/agent prompt)
        │  Skill tool invocation → generic method call
        ▼
   invoke-ado-cli.mjs  (shared adapter, one per plugin — not one per tool)
        │  1. resolve proxy/CA env (sandbox-auth already set HTTP(S)_PROXY, REQUESTS_CA_BUNDLE, …)
        │  2. spawn: node <plugin>/scripts/ado-cli.js <method> --structured --input <tmp.json>
        │  3. parse stdout JSON / map exit code → named error
        ▼
   ado-cli.js  (bundled, self-contained; same registerTools() as the MCP server)
        │  HTTPS to dev.azure.com / *.visualstudio.com, through the sandbox's MITM egress proxy
        ▼
   Azure DevOps REST API
```

There is no long-lived process, no port, no `mcp/server.json`, and no tool-call protocol beyond a
single `child_process.spawn` per invocation. This is a deliberate, permanent override of
`web-research.md`'s "Plugin Architecture for Sandbox" section, which recommended standing up an
Express-based MCP server (`mcp/server.json` + a Node HTTP listener) for this exact use case. That
recommendation does not fit a sandbox session (no port to bind usefully, no client to register the
server with, and it reintroduces exactly the MCP-server dependency this migration removes) and is
explicitly not followed.

### Authentication: sandbox-auth-only, proxy-injected, zero PAT surface

The plugin does not implement any auth logic of its own. It reuses, unmodified:

- `sandbox-auth:azure-devops` — picks the probe URL (specific target resource, else org project-list
  `https://dev.azure.com/<ORG>/_apis/projects?api-version=7.0`, else the VSSPS profile fallback
  `https://app.vssps.visualstudio.com/_apis/profile/profiles/me?api-version=7.0`) and hands off to
  `egress-auth`.
- `sandbox-auth:egress-auth` — owns the wire contract: HTTP `511` with an `auth_pending` marker means
  poll-until-Allow/Deny; HTTP `403 {"error":"denied","reason":...}` means stop and report; on success
  the sandbox's egress proxy has already recorded the grant, and every subsequent HTTP call from this
  sandbox session (through the proxy) carries the injected credential automatically. The agent process
  never sees a token.

**Every `azure-devops-*` skill's first ADO call goes through this path.** Migration replaces the source
plugin's `ado:setup-ado-mcp` auto-setup rule (`CLAUDE.md` "MCP Prerequisite — Auto-Setup") with:

> Before the first Azure DevOps CLI call in a session, run `sandbox-auth:azure-devops` (pass the
> target org if known). If a CLI call fails with `E_AUTH`, run it once and retry. Never ask the user
> to supply a PAT, token, or credential file — this plugin has no PAT interface.

**How `ado-cli.js` reaches the proxy.** `ado-cli.js`'s only two reachable auth modes from the CLI's
`invocationConfig()` are `--pat <token>` (Basic-shape header) and no-flag `auth:"none"` (zero
`Authorization` header — "a proxy injects it"). `invoke-ado-cli.mjs` always uses the no-flag mode:
it never passes `--pat`, and it does not read `AZURE_DEVOPS_PAT` / `AZURE_DEVOPS_PERSONAL_ACCESS_TOKEN`
/ `AZURE_DEVOPS_BEARER_TOKEN` from the environment or forward them. The org base URL is still required
(`AZURE_DEVOPS_ORG_URL` or equivalent, per `ado-cli.js`'s own missing-env-var check, exit code 4) —
that is configuration, not a credential, and is set the same way `sandbox-auth:azure-devops` already
expects an org/target to be known.

**Node-specific proxy/CA wiring (new, required — not covered by any existing skill).** `sandbox-auth`'s
existing helpers are Python (`requests` honors `HTTP_PROXY`/`HTTPS_PROXY` and `REQUESTS_CA_BUNDLE` /
`SSL_CERT_FILE` / `CURL_CA_BUNDLE` automatically). `ado-cli.js` runs under Node, and Node's `fetch`
(undici) does **not** read `HTTP_PROXY`/`HTTPS_PROXY` by default, and does not honor
`REQUESTS_CA_BUNDLE`/`SSL_CERT_FILE` at all — those are Python/curl-specific. `invoke-ado-cli.mjs`
must therefore explicitly set, in the child process environment it spawns `ado-cli.js` into:

- `NODE_EXTRA_CA_CERTS` = the same MITM CA path the sandbox already exposes via
  `REQUESTS_CA_BUNDLE`/`SSL_CERT_FILE` (read whichever of those two is set; do not hardcode a path).
- `NODE_USE_ENV_PROXY=1` — enables undici's built-in `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY` support
  (available on the Node 24 line; the audit's test environment was Node v24.16.0). Do not depend on
  a third-party proxy-agent package — this plugin stays dependency-free.
- Pass `HTTP_PROXY`/`HTTPS_PROXY` through from the parent environment unchanged (already set by
  `sandbox-auth`'s egress-auth flow; do not rewrite or strip the embedded per-sandbox token).

This is a genuine new requirement this migration must validate empirically during implementation (see
[Validation and release gates](#validation-and-release-gates), gate V1): if `ado-cli.js`'s HTTPS client
does not, in practice, honor `NODE_EXTRA_CA_CERTS`/`NODE_USE_ENV_PROXY` for every code path it uses
(some HTTP clients bypass undici's global dispatcher), `invoke-ado-cli.mjs` must fall back to an
explicit proxy agent configuration and the exact mechanism must be recorded in the adapter's own
header comment — but the two-env-var approach is the first thing to try, and is what the design
assumes.

### Component and file responsibilities

```
azure-devops/
├── .claude-plugin/plugin.json          new — name "azure-devops", version "1.0.0"
├── CLAUDE.md                           adapted from ado/CLAUDE.md (see Compatibility rules)
├── README.md                           new — plugin-level description, mirrors sandbox-auth/README.md's shape
├── references/
│   ├── ado-mention-conventions.md      copied verbatim from ado/references/
│   ├── review-reception-protocol.md    copied verbatim from ado/references/
│   ├── review-thread-state-machine.md  copied with one accuracy fix (see Compatibility rule 7)
│   ├── method-catalog.md               new, generated — see Method catalog approach
│   └── error-codes.md                  new — the named error taxonomy (see Error handling)
├── scripts/
│   ├── ado-cli.js                      bundled verbatim from ado/scripts/ado-cli.js
│   ├── invoke-ado-cli.mjs              new — the shared adapter (see below)
│   └── provenance.json                 new — records the exact copy's origin (see Known limitations)
├── skills/
│   ├── azure-devops-work-on/SKILL.md          from ado-work-on (inline-not-delegate, see mapping)
│   ├── azure-devops-publish-pr/SKILL.md       from ado-publish-pr
│   ├── azure-devops-babysit-pr/SKILL.md       from ado-babysit-pr
│   ├── azure-devops-draft-work-item/SKILL.md  from ado-draft-work-item (inline-not-delegate)
│   ├── azure-devops-work-items/SKILL.md       from ado-work-items
│   ├── azure-devops-work-my-backlog/
│   │   ├── SKILL.md                            from ado-work-my-backlog
│   │   └── scripts/{ado-api.mjs,classify.mjs,scan.mjs,state.mjs}  adapted (see mapping)
│   └── azure-devops-mentions/SKILL.md          from ado-mentions
├── agents/
│   ├── azure-devops-babysit-pr-worker.md      from ado-babysit-pr-worker
│   ├── azure-devops-pr-tender.md              from ado-pr-tender
│   └── azure-devops-devops-assistant.md       from ado-devops-assistant
├── commands/
│   ├── azure-devops-work-on.md
│   ├── azure-devops-publish-pr.md
│   ├── azure-devops-babysit-pr.md
│   ├── azure-devops-draft-work-item.md
│   └── azure-devops-work-my-backlog.md
│                                        (no setup-ado-mcp command — removed, see Non-goals)
└── tests/
    ├── unit/                           classify.mjs, state.mjs, invoke-ado-cli.mjs mutation-policy unit tests
    ├── static/                         method-name cross-check, MCP-reference grep, plugin.json/manifest validity
    └── workflow/                       scripted end-to-end skill-flow tests against a mocked ado-cli.js
```

`invoke-ado-cli.mjs` responsibilities (the one new piece of runtime code every skill/script goes
through):

1. **`invoke(method, params, opts)`** — the single generic entry point. No per-tool wrapper functions;
   `params` is passed straight through as the JSON body `ado-cli.js` expects (never as CLI flags).
2. Resolve the path to the bundled `${CLAUDE_PLUGIN_ROOT}/scripts/ado-cli.js` and spawn
   `node ado-cli.js <method> --structured --input -` (stdin), writing `JSON.stringify(params)` to stdin
   — never via `--input <file>` from untrusted content and never by string-interpolating `params` into
   a shell command.
3. Set the child environment per [Authentication](#authentication-sandbox-auth-only-proxy-injected-zero-pat-surface)
   above; never set `--pat`.
4. Apply the [mutation policy](#mutation-policy) before spawning: BLOCKED methods/params raise
   `E_BLOCKED` without ever invoking the CLI; CONFIRM-gated methods require the caller to have already
   obtained explicit confirmation (the adapter takes a `confirmed: true` flag it will not proceed
   without — it does not itself prompt, since HITL prompting is the skill/agent's job, not the
   adapter's).
5. Parse stdout as JSON on exit code 0; on nonzero exit, map the exit code and any parsed error JSON
   to the [named error taxonomy](#error-handling) and return `{ok: false, code, error}` — never throw
   a raw `Error` with CLI stderr text as the only signal.
6. Return a single stable shape: `{ok: true, data} | {ok: false, code, error: {message, detail?}}`.

### Method catalog approach

No hand-maintained per-tool wrapper catalog. `references/method-catalog.md` is **generated, not
authored**: produced by running the bundled CLI's own introspection —
`node scripts/ado-cli.js list --json` for the method/category list and
`node scripts/ado-cli.js docs --out <dir>` for per-method parameter docs — and rendering the result
into the reference file at build/release time. Skills reference method names by name (e.g.
`getWorkItemById`, `addWorkItemComment`) exactly as the source plugin's skills already do; the catalog
file is the cross-check surface, not a new abstraction layer skills call through.

**Regeneration is a release gate, not a one-time step** — see gate V2: every release must regenerate
`method-catalog.md` from the bundled `ado-cli.js` version actually shipped and diff it against the
version-controlled copy, so the catalog never silently drifts from the bundled CLI.

## Data flow

1. A user or the model invokes an `azure-devops-*` skill (directly, or via its command).
2. The skill's first ADO touchpoint checks/establishes auth via `sandbox-auth:azure-devops` (soft —
   only runs the full handshake if a prior call actually failed with `E_AUTH`, mirroring the source
   plugin's existing "soft, retry-on-failure" pattern rather than forcing a handshake on every call).
3. The skill calls `invoke-ado-cli.mjs`'s `invoke(method, params)` for each ADO operation it needs,
   exactly where the source skill currently names an MCP tool.
4. `invoke-ado-cli.mjs` applies the mutation policy, spawns `ado-cli.js` with the sandbox's
   proxy/CA env, and the CLI makes the actual HTTPS call through the MITM egress proxy to
   `dev.azure.com` / `<org>.visualstudio.com`.
5. The egress proxy injects the real credential server-side (per `sandbox-auth`'s architecture); the
   plugin and the CLI process never hold or see it.
6. `ado-cli.js` returns JSON on stdout; `invoke-ado-cli.mjs` normalizes it to `{ok, data|error}`; the
   skill continues its existing prompt logic (post a comment, format a table, etc.) exactly as before,
   substituting the CLI adapter's return shape for what used to be an MCP tool result.
7. Comments/PR updates flow back to Azure DevOps the same way — through `ado-cli.js` → the proxy →
   ADO — never through a separate direct-`fetch` path. This closes the one place the source plugin
   bypassed the tool layer: `ado-work-my-backlog`'s `scripts/ado-api.mjs` (see
   [Compatibility rule 9](#compatibility-and-migration-rules)).

## Security and privacy

- **No PAT ever exists in this plugin's process, config, or logs.** `invoke-ado-cli.mjs` never reads
  `AZURE_DEVOPS_PAT`/`AZURE_DEVOPS_PERSONAL_ACCESS_TOKEN`/`AZURE_DEVOPS_BEARER_TOKEN`, never passes
  `--pat`, and never writes an auth record file. This deliberately makes unreachable the plaintext
  auth-record persistence path the audit found in `ado-cli.js`'s bundled `EntraAuthHandler`
  (`getAuthRecordDir()` → `~/.azuredevops-mcp/`) — that code exists inside the bundle but is never
  exercised because the CLI is only ever invoked with the no-credential `auth:"none"` path.
  `provenance.json` (see [Known limitations](#known-limitations-carried-into-v100)) records this as an
  explicit, intentional non-use of that code path, not an oversight.
- **No secrets in logs.** `invoke-ado-cli.mjs` must never log the raw `params` JSON body or CLI stdout
  at a level enabled by default if either could contain a token (they should not, given (1) above, but
  defensively: redact any field named `token`/`pat`/`password`/`secret`/`authorization` before any debug
  log write).
- **Egress proxy env vars pass through unmodified**, never persisted to disk, never echoed in error
  messages returned to the model (an error message may say "auth failed" but must not include the
  proxy token value).
- **Append-only comments is a security/integrity property, not just a UX rule** — see
  [Mutation policy](#mutation-policy): it is enforced as a hard adapter-level block, not left to prompt
  discipline.

## Mutation policy

The approved decision is that the user's explicit task intent (invoking an `azure-devops-*` skill/
command) already authorizes the ordinary mutations that skill performs — no additional confirmation
gate is added on top of what the source plugin already does today. Extra confirmation is required only
for actions that are exceptional: destructive, irreversible, or outside what any migrated skill
actually uses. This is enforced as a three-tier classification inside `invoke-ado-cli.mjs`, not as
prose:

**Tier 1 — Ordinary (always allowed, no new gate).** Every method any migrated skill currently calls
via its MCP-tool name, with its current usage pattern preserved exactly: `getWorkItemById`,
`createWorkItem`, `updateWorkItem`, `updateWorkItemState`, `addWorkItemComment`, `createLink`,
`listWorkItems`, `searchWorkItems` (pending the stale-reference fix in
[Compatibility rule 8](#compatibility-and-migration-rules)), `listPullRequests`, `createPullRequest`,
`getPullRequestById`, `addPullRequestComment`, `getBuilds`, `getBuildLog`, `getDefinitions`,
`getBuildTimeline`, `listWikis`, `getWikiPageContent`, `getTeams`, `getTeamMembers`, `getSprints`,
`getCurrentSprint`, `getSprintWorkItems`, `getWorkItemTypes`, `getWorkItemTypeFields`,
`getCommitHistory`, `browseRepository`, `getFileContent`, and the other read/ordinary-write methods the
skill files reference by name. Existing skills that already ask the user to confirm before creating/
updating (e.g. `azure-devops-work-items`' "always confirm before making changes", `azure-devops-draft-
work-item`'s mandatory preview) keep that prompt-level confirmation exactly as-is — it is unaffected by
this policy layer.

**Tier 2 — Hard-BLOCKED (technical gate, not a prompt).** `manageWorkItemComment` with
`action: "update"` or `action: "delete"` is rejected by `invoke-ado-cli.mjs` before the CLI is even
spawned, returning `E_BLOCKED`. This turns the plugin's existing "Comments Are Append-Only" rule
(`CLAUDE.md`, carried forward verbatim — see [Compatibility rule 3](#compatibility-and-migration-rules))
from a convention the model is asked to follow into something the adapter physically prevents, closing
the exact gap the audit flagged (`manageWorkItemComment action:"update"` directly contradicting the
plugin's own append-only rule with no technical enforcement).

**Tier 3 — Confirm-gated (destructive or out-of-scope).** Any method matching either condition below
requires the caller to pass `confirmed: true` to `invoke()` (obtained by the skill/agent presenting an
explicit HITL confirmation first) or the adapter returns `E_BLOCKED` with a message naming what
confirmation is required:

- **Destructive/irreversible by nature**, regardless of whether a migrated skill currently calls it:
  `mergePullRequest`, `deletePackageVersion`, `rotateSecrets`, `manageSecurityPolicies`, `runPipeline`,
  overwrite-style `createOrUpdateWikiPage`, and any bulk create/update/delete method.
- **Out-of-scope catch-all**: any method not referenced by name in any migrated skill/agent/command file
  (cross-checked against `method-catalog.md`). If a future prompt asks the model to call an ADO method
  no shipped skill uses, it is exceptional by definition and gets the same confirm gate as Tier 3 above,
  even if the method itself looks benign — this is what "extra confirmation required only for
  exceptional destructive/out-of-scope actions" means concretely.

The classification tables (which exact methods are Tier 1 vs. Tier 3-destructive) live in
`invoke-ado-cli.mjs` as data, not scattered across skills, so a single audit of that one file tells you
the entire mutation policy.

## Error handling

`ado-cli.js` exit codes (0 success; 1 generic/tool error; 2 invalid input/Zod validation failure; 3
unknown method; 4 missing required env vars) plus ADO's own REST error JSON shape (`message`,
`typeName`, `typeKey`, `errorCode`, `eventId`) are mapped, inside `invoke-ado-cli.mjs`, to a small
named taxonomy documented in `references/error-codes.md`:

| Code | Meaning | Typical source |
|---|---|---|
| `E_AUTH` | Not authenticated / auth rejected by ADO | HTTP 401/403 from ADO, or a proxy `403 denied` |
| `E_NOT_FOUND` | Target resource does not exist | HTTP 404, or ADO `typeKey` indicating missing work item/PR |
| `E_VALIDATION` | Input failed schema validation | CLI exit code 2 |
| `E_UNKNOWN_METHOD` | Method name not recognized by the bundled CLI | CLI exit code 3 |
| `E_CONFIG` | Required environment/config missing (e.g. org URL) | CLI exit code 4 |
| `E_RATE_LIMIT` | Throttled by ADO | HTTP 429, `Retry-After` present (CLI's own `withAuthRetry` already retries with backoff before this ever surfaces) |
| `E_BLOCKED` | Mutation policy refused the call | Tier 2 hard block, or Tier 3 without `confirmed: true` |
| `E_UPSTREAM` | ADO returned a well-formed error this taxonomy doesn't have a more specific code for | Any other 4xx/5xx with a parsed ADO error body |
| `E_TRANSPORT` | Could not reach ADO at all (proxy/network/CA failure) | CLI exit code 1 with no parseable ADO error body, or a spawn-level failure |

Every `invoke()` return value uses the stable contract `{ok: false, code, error: {message, detail?}}`
on failure — skills branch on `code`, not on parsing free-text stderr. This is new: today the source
skills have no structured error handling at all beyond "the MCP call failed."

## Compatibility and migration rules

1. **Namespace change only.** Every skill/agent/command directory and file basename stays identical to
   the source `ado` plugin; only the plugin-namespace prefix changes, `ado:` → `azure-devops:`, and each
   skill/command/agent's own file-basename prefix changes `ado-` → `azure-devops-` (e.g.
   `ado-work-on/SKILL.md` → `azure-devops-work-on/SKILL.md`) so names stay unambiguous inside a
   multi-plugin marketplace. No skill's actual procedure, phase structure, or decision logic is
   rewritten — only the tool-call layer and namespace strings change.
2. **MCP tool call → CLI adapter call, mechanically.** Every place a skill/agent currently says "call
   `<toolName>`" (an MCP tool) becomes "call `invoke-ado-cli.mjs`'s `invoke('<toolName>', {...})`" with
   the same parameters, in the same order, for the same purpose. Method names themselves are unchanged
   (`getWorkItemById` stays `getWorkItemById`) — only the calling convention changes.
3. **`CLAUDE.md` rules carried forward, one strengthened.** The "Comments Are Append-Only" rule is
   copied verbatim and now also technically enforced (Mutation policy Tier 2). The "MCP Prerequisite —
   Auto-Setup" rule is replaced with the sandbox-auth rule in
   [Authentication](#authentication-sandbox-auth-only-proxy-injected-zero-pat-surface) above — same
   "soft, auto-retry-on-failure" shape, different mechanism.
4. **`ado-mentions` → `azure-devops-mentions`, copied verbatim.** No content changes; only the namespace
   prefix.
5. **`ado-publish-pr`, `ado-babysit-pr`, `ado-work-items`, `ado-work-my-backlog` migrate 1:1** — same
   phases, same decision logic, MCP tool names replaced by adapter calls per rule 2. `ado-work-items`'
   line 27 stale `searchWorkItems` reference is fixed per rule 8, not otherwise changed.
6. **`ado-work-on` and `ado-draft-work-item`: inline-not-delegate.** These two skills' only ADO-specific
   content is the "Azure DevOps" column of their `GitHub | Azure DevOps` tables and their provider-
   resolution/tooling-setup phases — everything else is delegated to `development:work-on` /
   `development:draft-work-item`, neither of which exists in this repo, and which in turn delegate to
   `development:autonomous-design`, `development:implement`, `development:blind-spot-detector`,
   `development:draft-feature`, `development:draft-bug`, `debugging:debug-with-logs`,
   `debugging:systematic-debugging`, and `code-reviewer:pr-review` — none of which exist here either.
   Reimplementing that entire generic framework is out of scope (see Non-goals). Instead:
   - `azure-devops-work-on` keeps the full phase structure verbatim (Phase 0 provider resolution
     becomes a no-op single-provider statement; Phase 1 auto-detect mode; Phase 1.1 fetch & understand;
     Phase 1.2 route by type; the mandatory Phase 1.5 feedback checkpoint; Part 2 execute & deliver;
     Error Handling; Reference Conventions; Decision Log) with every ADO-column cell kept and every
     `development:X` delegation point replaced by **inline instructions performing the same step
     directly** using the agent's own reasoning and standard tools (Read/Write/Edit/Grep/Glob/Bash/git),
     rather than a call to a named external skill that would not resolve. Concretely: "Stage A — Design
     via `development:autonomous-design`" becomes "research the codebase (Read/Grep/Glob/git
     log/WebSearch), propose 2-3 approaches, pick one with a stated rationale, note it in the decision
     log — no external design-review-gate sub-agent dispatch"; "Phase 2.3 — Implement via
     `development:implement`" becomes "implement the approved plan directly with ordinary
     read/write/test/verify tool use, committing each increment, and self-review the diff before
     publishing" — same outcome contract (success/blocked), same gates (HITL feedback checkpoint,
     revision cap v3, append-only comments), no dependency on an unresolvable skill name.
   - `azure-devops-draft-work-item` gets the same treatment: `development:draft-feature`/
     `development:draft-bug`/`development:blind-spot-detector` delegation points become inline
     "ask the type-appropriate clarifying questions, then run a self-check pass for dependencies/done-
     definition gaps/hidden scope before the mandatory preview" — the Quick Path (Task/trivial) already
     has no external dependency and is unchanged.
   - Provider-agnostic tables collapse to their Azure DevOps column only (the GitHub column and its
     `gh:*` references are dropped — this plugin only ships the ADO side).
   - Reference files these two skills currently reach via `development`'s own `reference/` directory
     (bug RCA workflow, plan/RCA comment formats, decision-log guide, git-worktree/branch-completion
     guides) are copied into `azure-devops-work-on/reference/` as generic, provider-neutral guides
     (their content does not mention GitHub/ADO specifics beyond the tables this rule already
     collapses); the two genuinely ADO-specific reference files
     (`ado-state-transitions.md`, `ado-mention-conventions.md`) are the ones this plugin already ships
     natively — reuse those, don't re-derive them.
7. **`review-thread-state-machine.md`'s synchronization note is corrected, not carried forward
   verbatim.** The source file opens with "this lifecycle is copied into the code-reviewer and ADO
   plugins; the copies must remain byte-identical" — `code-reviewer` does not exist in this repo, so
   that claim is now inaccurate and dangling. The migrated copy replaces that note with: "this
   lifecycle document describes the PR-review thread conventions this plugin's `azure-devops-babysit-
   pr-worker` agent follows; it originated as a shared reference with a `code-reviewer` plugin that is
   not part of this marketplace." The state machine content itself (states, transitions, blocker
   rules, question lifecycle) is otherwise unchanged. Separately, `azure-devops-babysit-pr-worker`'s
   own `[Resolve]` step (which calls `updatePullRequestThread` with a terminal status) is preserved
   exactly as the source agent already behaves — that is the actual, tested behavior of the worker
   today, and this migration's job is transformation, not a behavioral audit of a pre-existing
   inconsistency between it and Rule #1 ("the developer never resolves or closes threads"). This
   inconsistency is pre-existing in the source plugin, is unrelated to the CLI migration, and is
   recorded here as a known carried-over documentation inconsistency rather than silently resolved one
   way or the other.
8. **Stale method-name references are fixed, not guessed at blindly.** `ado-work-items/SKILL.md` line 27
   references `searchWorkItems` as a query method; the audit found this name does not match any of the
   CLI's 131 real methods. Because this spec does not itself enumerate the full method catalog, the
   concrete fix is a **validation requirement, not a name substituted here**: during implementation,
   cross-check every method name appearing in every migrated skill/agent/command file (including this
   one, `ado-draft-work-item`'s Duplicate Check table, and `ado-work-my-backlog`) against
   `node scripts/ado-cli.js list --json`'s real output, and correct any name that does not match to the
   closest real equivalent (e.g. a WIQL-based query method) before release. This is gate V3 below.
9. **`ado-work-my-backlog/scripts/ado-api.mjs`'s direct PAT auth is removed, not migrated as-is.**
   The source script's `getAuthHeader()` reads `AZURE_DEVOPS_PAT` / `AZURE_DEVOPS_PERSONAL_ACCESS_TOKEN`
   / `AZURE_DEVOPS_BEARER_TOKEN` directly and builds its own `Authorization` header for its own
   `adoFetch()` calls — a second, independent path to ADO that bypasses the CLI/adapter entirely and
   directly contradicts the sandbox-auth-only decision. The migrated `azure-devops-work-my-backlog/
   scripts/ado-api.mjs` removes `getAuthHeader()` and `adoFetch()`'s direct-`fetch` transport entirely
   and routes every one of its calls through `invoke-ado-cli.mjs`'s `invoke()` instead (the scanner's
   `-preview` API-version retry quirk for on-prem orgs and its default `api-version: "7.2"` become
   parameters passed to `invoke()`, not header/URL construction of its own). `scan.mjs`, `classify.mjs`,
   and `state.mjs` are otherwise unchanged — they consume `ado-api.mjs`'s return values, not its
   transport.
10. **Examples and MCP-only artifacts are dropped**, per Non-goals: `examples/`, `launch-ado-mcp.sh`,
    `setup-ado-mcp` skill and command.
11. **Version starts fresh at 1.0.0.** The source plugin's `3.1.5` lineage is not continued —
    `azure-devops/.claude-plugin/plugin.json` starts at `"version": "1.0.0"`, matching the "marketplace
    v1.0.0 release" decision and the same pattern this repo already used for `sandbox` (which started
    at `1.0.0` rather than continuing `claude_plugins`' numbering).

## Known limitations carried into v1.0.0

`ado-cli.js` ships bundled, unmodified, as the execution engine, with these known, deliberately
unresolved gaps documented rather than hidden:

- **No independent provenance.** The bundle has no git history of its own in the source repo (it is
  untracked there), no publisher/package identity, and no version pin beyond "whatever the source
  repo's working tree currently has." `scripts/provenance.json` records, at copy time: the exact
  byte size and a content hash of the copied `ado-cli.js`, the date it was copied, and the fact that
  its origin is an untracked build artifact in `claude_plugins/ado/scripts/`, not a published,
  versioned release of the underlying TypeScript project. This does not block v1.0.0 — the safety
  gaps (mutation confirmation, auth surface, test coverage) are what this spec actually closes; the
  provenance gap is a supply-chain honesty note for future maintainers, not a functional defect.
- **Auth surface is narrower than the MCP server's.** Only `--pat` and no-flag `auth:"none"` are
  reachable from the CLI; Entra/az-cli/interactive/on-prem auth types the MCP server supports are
  structurally unreachable here. This is not a regression introduced by this migration — the sandbox-
  auth-only design only ever needed the no-flag path anyway — but it means this plugin cannot support
  an on-prem ADO Server auth mode that requires interactive login, should that ever be needed.
- **The bundle is 87,719 lines** and cannot be meaningfully code-reviewed line-by-line as part of this
  migration; its behavior is validated through the test strategy below (real-method-catalog cross-
  checks, mocked workflow tests, and the exit-code/error-mapping unit tests), not through manual audit
  of the bundle's internals.

## Test strategy

**Static tests** (`tests/static/`):
- Every `.claude-plugin/plugin.json`, and every skill/agent/command frontmatter block, parses and has
  the required fields (`name`, `description` at minimum for skills; matching name/model fields for
  agents).
- Grep-based cross-reference check: no file under `azure-devops/` references `ado:`, `development:`,
  `gh:`, `code-reviewer:`, `debugging:` namespaces (mirrors the same check the prior `sandbox-plugins`
  seed spec used for its own self-containment gate) — confirms the inline-not-delegate migration
  (rule 6) actually removed every external-plugin dependency, not just the obvious ones.
- Method-name cross-check (gate V3): every bare method name referenced in any skill/agent/command
  markdown file matches an entry in a freshly generated `node scripts/ado-cli.js list --json`.
- `references/method-catalog.md` is byte-identical to what regenerating it from the bundled
  `ado-cli.js` produces right now (catches drift between the shipped CLI and the checked-in catalog).

**Unit tests** (`tests/unit/`):
- `invoke-ado-cli.mjs`'s mutation-policy classifier: Tier 1 methods pass through un-gated; the Tier 2
  `manageWorkItemComment` update/delete case returns `E_BLOCKED` without spawning a process; Tier 3
  destructive methods and any not-in-catalog method require `confirmed: true` and return `E_BLOCKED`
  otherwise.
- `invoke-ado-cli.mjs`'s error mapping: each of exit codes 0-4, an ADO 401/403/404/429 JSON body, and a
  spawn-level failure (e.g. missing `node` or missing bundle path) map to the correct named code from
  the [error taxonomy](#error-handling) table, exercised against a stubbed child process (no real
  network or real CLI invocation needed for this layer).
- `classify.mjs` and `state.mjs` (currently zero coverage per the audit): unit tests for the BOT-PLAN
  stage classifier (fresh/revise/implement-approved/revision-cap-reached) and state persistence
  round-trips under `.ai/work-my-backlog/`.
- `ado-api.mjs` post-migration (rule 9): confirms it no longer exports/uses `getAuthHeader()` or a
  direct `fetch`-based `adoFetch()`, and that its ADO calls route through a mocked `invoke-ado-cli.mjs`.

**Workflow tests** (`tests/workflow/`):
- Scripted, mocked end-to-end runs (a stub `ado-cli.js` replacement returning canned JSON per method)
  for at least: `azure-devops-work-items` create/query/update/link/sprint paths, `azure-devops-publish-
  pr`, `azure-devops-babysit-pr`'s comment-disposition handling, and `azure-devops-work-on`'s auto-detect
  routing (fresh vs. revision vs. execute) — confirming the BOT-PLAN marker parsing and feedback-
  checkpoint gating still work identically to the source plugin's documented behavior.
- A real (non-mocked), opt-in integration smoke test — gated behind an env var so it does not run by
  default — that runs `sandbox-auth:azure-devops` against a real test org, then a single `invoke()`
  read call (`getWorkItemById` or the org project-list probe), to validate the Node proxy/CA wiring
  from [Authentication](#authentication-sandbox-auth-only-proxy-injected-zero-pat-surface) actually
  works end-to-end, not just in unit isolation.

## Marketplace and docs changes

- **`.claude-plugin/marketplace.json`**: append a third entry to the `plugins` array —
  `{"name": "azure-devops", "source": "./azure-devops", "description": "<one paragraph covering: CLI-
  native Azure DevOps workflow automation (work items, PRs, backlog processing) via the bundled ado-cli,
  sandbox-auth-only authentication, no MCP server, no PAT interface>", "version": "1.0.0", "category":
  "development", "tags": ["azure-devops", "ado", "cli", "work-items", "pull-requests", "backlog",
  "sandbox"], "keywords": ["azure-devops-cli", "ado-work-items", "ado-pull-requests", "ado-backlog",
  "sandbox-auth", "work-on", "draft-work-item", "babysit-pr"]}`, matching the existing two entries'
  style and field shape exactly (no schema change needed — the registry is already extensible per the
  prior seed spec).
- **Root `README.md`**: add an "azure-devops (v1.0.0)" section under "Available Plugins" (or the
  equivalent existing heading), describing the plugin in the same short-paragraph style as the existing
  `sandbox`/`sandbox-auth` entries, and update the "Extensibility" section's mention of `azure-devops`
  as a hypothetical future plugin to instead point at this real one.
- **New `azure-devops/README.md`**: mirrors `sandbox-auth/README.md`'s shape — skills table (name,
  purpose), exit/error-code table (the named taxonomy above), the `${CLAUDE_PLUGIN_ROOT}`
  placeholder-not-a-shell-variable caveat (carried forward since `invoke-ado-cli.mjs` uses it too), and
  an explicit "No PAT, no MCP server — sandbox-auth only" statement up top.
- **`azure-devops/CLAUDE.md`**: per Compatibility rule 3.
- No changes to `CLAUDE.md`, `.gitattributes`, or `.gitignore` at the repo root are required by this
  plugin — it introduces no new file types those don't already cover (`.mjs`/`.md`/`.json` are already
  handled).

## Validation and release gates

Before the `azure-devops` plugin's initial implementation is considered release-ready:

- **V1 — Proxy/CA wiring validated for real.** The `NODE_EXTRA_CA_CERTS`/`NODE_USE_ENV_PROXY` approach
  in [Authentication](#authentication-sandbox-auth-only-proxy-injected-zero-pat-surface) is exercised
  against a real sandbox session and a real (or realistic test) ADO org, confirming `ado-cli.js`'s
  actual HTTP client (not just Node's `fetch` in isolation) honors both. If it does not, the fallback
  approach is implemented and this section of the spec is updated before release, not silently patched
  around at runtime.
- **V2 — Method catalog matches the shipped bundle.** `references/method-catalog.md` is regenerated
  from `node scripts/ado-cli.js list --json`/`docs` against the exact bundled `ado-cli.js` being
  released and is byte-identical to the checked-in copy (test in `tests/static/`).
- **V3 — No stale method-name references.** Every method name in every skill/agent/command file
  resolves against the regenerated catalog; `ado-work-items/SKILL.md`'s `searchWorkItems` reference
  (and any other mismatch found by the same check) is corrected before release.
- **V4 — Self-containment holds.** The `ado:`/`development:`/`gh:`/`code-reviewer:`/`debugging:`
  namespace grep (Compatibility rule 6, static test) returns zero hits anywhere under `azure-devops/`.
- **V5 — No PAT surface exists.** Grep the entire plugin tree for `AZURE_DEVOPS_PAT`,
  `AZURE_DEVOPS_PERSONAL_ACCESS_TOKEN`, `AZURE_DEVOPS_BEARER_TOKEN`, and `--pat`; the only hits allowed
  are the negative documentation of this rule itself (this spec, the plugin's own README/CLAUDE.md
  explaining that no PAT path exists) — zero hits in any script or skill instructing a call with a
  credential.
- **V6 — Mutation policy tests pass.** All Tier 1/2/3 unit tests (Test strategy, Unit tests) pass,
  including the `manageWorkItemComment` update/delete hard-block and the Tier 3 confirm-gate.
- **V7 — All test suites green.** Static, unit, and mocked-workflow tests all pass; the opt-in real
  integration smoke test has been run at least once against a real org during implementation (not
  required to run in CI by default, since it needs a live sandbox + org).
- **V8 — Marketplace/docs consistency.** `marketplace.json` parses as valid JSON, the new entry's
  `source` (`./azure-devops`) resolves to a real directory, and `README.md`'s new section and updated
  Extensibility reference are present.
- **V9 — Source repo untouched.** `claude_plugins` has zero uncommitted changes as a result of this
  work; `ado/scripts/ado-cli.js` and everything else in the source `ado/` plugin remain exactly as they
  were before this spec was written.

## Non-goals (recap)

See [Non-goals](#non-goals) above for the complete list — restated here for scan-ability: no MCP
server, no PAT/credential UI, no re-implementation of `development`'s generic design/implement/blind-
spot engines (inline-not-delegate instead), no closing of the `ado-cli.js` provenance gap, no migration
of MCP-only artifacts (`launch-ado-mcp.sh`, `examples/`, `setup-ado-mcp`), no source-repo history
rewrite, no push.
