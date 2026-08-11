# `azure-devops` domain plugin: CLI-native migration of the ADO workflow

**Date:** 2026-08-11
**Status:** Approved design, self-review applied and architecture clarified 2026-08-11 — **ready for
implementation**; all previously open decisions are now [recorded](#recorded-decisions)
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
TypeScript project's CLI entry point, exposing the same 130 tools as the MCP server through a
`node ado-cli.js <method>` command-line contract. This CLI is the mechanism that makes a CLI-native
port possible: it is functionally 1:1 with the MCP tools the skills already reference (same
`registerTools()` function, same Zod schemas), and it is **self-sufficient** — it needs no wrapper,
no adapter, and no per-tool shim. Skills and agents invoke it directly. It does, however, ship as an
untracked, unversioned prototype with an auth surface narrower than what the skills assume (see
[Known limitations](#known-limitations-carried-into-v100) — this spec documents those gaps rather than
silently ignoring them).

## Goals

- Publish a new, self-contained `azure-devops` domain plugin in this marketplace at v1.0.0, matching
  the domain name this repo's own `README.md` already names as an expected future addition.
- Preserve the full existing ADO workflow — all skills, agents, commands, and their behavior — by
  **transforming** MCP tool calls into direct `ado-cli.js` invocations, not rewriting the workflows
  from scratch.
- Make authentication sandbox-auth-only: reuse the existing `sandbox-auth:azure-devops` +
  `sandbox-auth:egress-auth` skills for the proxy-injected auth handshake. No PAT configuration
  surface, no MCP server, no credential file.
- Ship `ado-cli.js` as the bundled execution engine, invoked **directly** by skills and agents. No
  adapter, no wrapper module, no per-tool helper functions — the CLI's own
  `node ado-cli.js <method> --structured` + JSON-on-stdin contract *is* the interface.
- Make the plugin self-contained: no dependency on a `development` (or any other) plugin that does not
  exist in this repo.
- State a mutation policy that keeps every mutation the current skills already perform working without
  new confirmation prompts, while requiring explicit confirmation for the small set of actions that are
  destructive, irreversible, or outside what any migrated skill actually uses.
- Document a stable, named error vocabulary so every skill reacts to CLI failures consistently instead
  of improvising on raw stderr.
- Update this repo's marketplace registry, README, and docs to reflect the new plugin.
- Leave `claude_plugins` completely unmodified.

## Non-goals

- Do not add an MCP server, `mcp/server.json`, or any Express/Node server process. `web-research.md`'s
  "Plugin Architecture for Sandbox" section recommends exactly this — **that recommendation is
  overridden by this spec.** The design is CLI-native: skills shell out to the bundled `ado-cli.js`
  and read its structured JSON. No listener, no port, no server lifecycle.
- **Do not build an adapter, wrapper, or client library around `ado-cli.js`.** An earlier draft of this
  spec introduced a shared `invoke-ado-cli.mjs` module that every skill would call through. That layer
  is removed: `ado-cli.js` is self-sufficient, its CLI contract is already stable and structured, and a
  wrapper would add a second thing to version, document, and keep in sync with the bundle for no
  capability the CLI does not already provide. Skills invoke `node ado-cli.js <method> --structured`
  directly.
- **Do not build a test suite.** No `tests/` directory, no `package.json`, no test runner, no unit /
  workflow / integration test files. This plugin is markdown workflow instructions plus a vendored
  third-party binary bundle; release confidence comes from the
  [release verification](#release-verification) checks, which are validation and review steps, not
  tests.
- Do not build a PAT-entry, device-code, or any other credential-collection UI. Authentication is
  100% delegated to `sandbox-auth`.
- Do not fully re-implement the generic `development:work-on` / `development:draft-work-item`
  orchestration engines (design-review gates, TDD execution loop, blind-spot detection, bug RCA
  workflow) inside this plugin. Reproducing that framework is out of scope for an ADO-to-CLI
  migration; see [Compatibility rule 6 — inline-not-delegate](#compatibility-and-migration-rules)
  for the inline-not-delegate resolution actually adopted.
- Do not close the `ado-cli.js` provenance gap (no git history, no publisher, no independent version)
  as part of v1.0.0 — it is documented as a known limitation, not blocked on.
- Do not migrate MCP-specific artifacts: `launch-ado-mcp.sh`, `examples/azure-devops.env.example`,
  `examples/claude/.mcp.json.example`, `examples/copilot/mcp-config.json.example`, or the
  `setup-ado-mcp` skill/command. These describe a setup path this plugin does not have.
- Do not modify `claude_plugins` in any way. The source `ado` plugin — including the untracked
  `ado/scripts/ado-cli.js` — is read-only input to this migration; nothing is committed, staged,
  rewritten, or squashed there. The bundle is **copied out**, never moved or edited in place.

## Architecture

### CLI-native, no MCP server, no adapter

```
   agent (skill/agent prompt)
        │  Bash tool invocation, per the canonical invocation form below
        ▼
   node ${CLAUDE_PLUGIN_ROOT}/scripts/ado-cli.js <method> --structured   < JSON on stdin
        │  (no wrapper module; the sandbox has already set HTTP(S)_PROXY / NO_PROXY and the CA path,
        │   and the session's first ADO call has already warmed sandbox-auth)
        ▼
   ado-cli.js  (bundled, self-contained; same registerTools() as the MCP server)
        │  HTTPS to dev.azure.com / *.visualstudio.com, through the sandbox's MITM egress proxy
        ▼
   Azure DevOps REST API
```

There is no long-lived process, no port, no `mcp/server.json`, no adapter module, and no tool-call
protocol beyond one short-lived process per call. This is a deliberate, permanent override of
`web-research.md`'s "Plugin Architecture for Sandbox" section, which recommended standing up an
Express-based MCP server (`mcp/server.json` + a Node HTTP listener) for this exact use case. That
recommendation does not fit a sandbox session (no port to bind usefully, no client to register the
server with, and it reintroduces exactly the MCP-server dependency this migration removes) and is
explicitly not followed.

### Canonical invocation form

Every skill, agent, and command uses exactly this shape, so there is one form to review and one form
to fix if the CLI contract ever changes:

```bash
node "${CLAUDE_PLUGIN_ROOT}/scripts/ado-cli.js" <method> --structured <<'ADOJSON'
{ "id": 12345 }
ADOJSON
```

Five rules make this form correct, each for a verified reason:

1. **`--structured`, always.** It is what makes stdout a single parseable JSON document.
2. **JSON on stdin, and `--input` is never passed.** Stdin is already the default body source when
   `--input` is absent; the CLI's own usage text documents the contract as
   `echo '<json>' | azuredevops-cli <method> [--structured]`. **Never pass `--input -`** — the CLI's
   hand-rolled arg parser has no `-`-means-stdin sentinel and will literally `fs.readFileSync("-")`,
   failing with `ENOENT ... open '<cwd>/-'` (verified 2026-08-11).
3. **A *quoted* heredoc (`<<'ADOJSON'`), not `echo '<json>' |`.** This is not stylistic. ADO content
   routinely contains apostrophes (work item titles, comment bodies, error text); a single-quoted
   `echo` argument breaks on the first one, and an unquoted heredoc would expand `$` and backticks
   inside comment text. The quoted heredoc passes the body through byte-for-byte with no shell
   interpretation, which also removes the injection surface entirely. For bodies large enough to be
   unwieldy inline, write the JSON to a file and redirect it (`… --structured < body.json`) — still
   stdin, still no `--input`.
4. **`${CLAUDE_PLUGIN_ROOT}` is a Claude Code placeholder, not a shell variable.** It is substituted
   before the command reaches the shell. Keep it double-quoted so the substituted path survives spaces.
   Do not attempt to `export` it, compute it with `dirname "$0"`, or hardcode an absolute path.
5. **Branch on the exit code and stdout only.** See [Error handling](#error-handling) — stderr is
   non-empty on *successful* runs and must never be read as a failure signal.

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
`Authorization` header — "a proxy injects it"). **This plugin always uses the no-flag mode: `--pat` is
never passed by any skill, agent, command, or script**, and nothing in the plugin reads or forwards
`AZURE_DEVOPS_PAT` / `AZURE_DEVOPS_PERSONAL_ACCESS_TOKEN` / `AZURE_DEVOPS_BEARER_TOKEN`. Both
`AZURE_DEVOPS_ORG_URL` **and** `AZURE_DEVOPS_PROJECT` are still required (`ado-cli.js` exits `4` with
`"AZURE_DEVOPS_ORG_URL and AZURE_DEVOPS_PROJECT must be set"` if either is missing) — those are
configuration, not credentials, and are set the same way `sandbox-auth:azure-devops` already expects an
org/target to be known.

**Node-specific proxy/CA wiring (verified against the bundle, 2026-08-11).** `sandbox-auth`'s existing
helpers are Python (`requests` honors `HTTP_PROXY`/`HTTPS_PROXY` and `REQUESTS_CA_BUNDLE` /
`SSL_CERT_FILE` / `CURL_CA_BUNDLE` automatically). `ado-cli.js` runs under Node, so the mechanism had
to be established for its actual HTTP stack rather than assumed. **Static verification of the bundle
found zero uses of global `fetch`/undici** (`grep -cE '(^|[^.\w])fetch\('` → `0`): every Azure DevOps
call goes `AzureDevOpsService` → `azure-devops-node-api`'s `WebApi` → `typed-rest-client`'s
`HttpClient` → Node's `https.request` with a `tunnel`-package proxy agent. This determines the wiring —
and, importantly for the no-adapter design, it means **almost nothing needs wiring at all**:

- **Proxy: handled natively by the CLI's own stack; nothing to do.** `typed-rest-client`'s `HttpClient`
  reads `HTTP_PROXY`, `HTTPS_PROXY`, and `NO_PROXY` from `process.env` itself (`_getProxy()` /
  `_isMatchInBypassProxyList()`) and builds a `tunnel` agent from them. The sandbox sets these before
  any skill runs and the Bash tool inherits them, so a plain `node ado-cli.js …` invocation is already
  proxied. Do not rewrite, re-export, or strip the embedded per-sandbox basic-auth token.
  - **Caveat — uppercase only.** `typed-rest-client` reads *only* the uppercase names (they are a hard-
    coded `EnvironmentVariables` enum: `"HTTP_PROXY"`, `"HTTPS_PROXY"`, `"NO_PROXY"`); it does **not**
    fall back to lowercase `http_proxy`/`https_proxy`/`no_proxy`.
  - **`NODE_USE_ENV_PROXY` is not applicable and must not be relied on.** It is an undici/`fetch`-level
    switch; this bundle never uses undici, so setting it would have no effect on any real call path.
    Setting it is harmless but misleading — omit it.
- **CA trust: the one thing that may need setting.** `NODE_EXTRA_CA_CERTS` must point at the same MITM
  CA the sandbox already exposes via `REQUESTS_CA_BUNDLE`/`SSL_CERT_FILE`, because Node does not honor
  the Python/curl CA variables at all. It extends Node's default root store process-wide, covering
  `https.request` and the `tunnel` agent's TLS sockets alike, and Node reads it **only at process
  start** — so it must already be in the environment when `node` is launched.

**Both of the above are session-level environment preconditions, not per-call work.** With no adapter
to normalize them, they are checked and, if necessary, exported **once per session** as part of the
same first-call warm-up that runs `sandbox-auth:azure-devops` (see the migrated `CLAUDE.md` rule
below), not re-derived inside every skill. The migrated `CLAUDE.md` carries the check as a short
preamble: if `HTTPS_PROXY` is unset but `https_proxy` is set, export the uppercase form; if
`NODE_EXTRA_CA_CERTS` is unset but `REQUESTS_CA_BUNDLE` or `SSL_CERT_FILE` is set, export it from
whichever is present. Both are idempotent no-ops in a correctly configured sandbox.

### Component and file responsibilities

```
azure-devops/
├── .claude-plugin/plugin.json          new — name "azure-devops", version "1.0.0"
├── CLAUDE.md                           adapted from ado/CLAUDE.md (see Compatibility rules)
├── README.md                           new — plugin-level description, mirrors sandbox-auth/README.md's shape
├── references/
│   ├── ado-mention-conventions.md      copied verbatim from ado/references/
│   ├── review-reception-protocol.md    copied verbatim from ado/references/
│   ├── review-thread-state-machine.md  copied with two fixes (see Compatibility rule 7)
│   ├── ado-state-transitions.md        copied from claude_plugins/development/skills/work-on/
│   │                                   reference/ (NOT from ado/ — see Compatibility rule 6)
│   ├── method-catalog.md               new, generated — see Method catalog approach
│   └── error-codes.md                  new — the named error vocabulary (see Error handling)
├── scripts/
│   ├── ado-cli.js                      bundled verbatim from ado/scripts/ado-cli.js
│   └── provenance.json                 new — records the exact copy's origin (see Known limitations)
├── skills/
│   ├── azure-devops-work-on/
│   │   ├── SKILL.md                            from ado-work-on (inline-not-delegate, see mapping)
│   │   └── reference/                          generic guides copied from development/skills/
│   │                                           work-on/reference/ (see Compatibility rule 6)
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
│   └── ado-assistant.md                       from ado-devops-assistant (named exception, rule 1)
└── commands/
    ├── azure-devops-work-on.md
    ├── azure-devops-publish-pr.md
    ├── azure-devops-babysit-pr.md
    ├── azure-devops-draft-work-item.md
    └── azure-devops-work-my-backlog.md
                                         (no setup-ado-mcp command — removed, see Non-goals)
```

**There is no `tests/` directory and no `package.json`.** See
[Non-goals](#non-goals) and [Release verification](#release-verification): this plugin is workflow
markdown plus a vendored bundle, and it is validated by inspection and by the CLI's own introspection
subcommands rather than by an authored test suite.

**There is no `scripts/invoke-ado-cli.mjs`.** The only two files under `scripts/` are the vendored
bundle and its provenance record. Skills call the bundle directly per the
[canonical invocation form](#canonical-invocation-form).

### Method catalog approach

No hand-maintained per-tool wrapper catalog. `references/method-catalog.md` is **generated, not
authored**: produced by running the bundled CLI's own introspection —
`node scripts/ado-cli.js list --json` for the method/category list and
`node scripts/ado-cli.js docs --out <dir>` for per-method parameter docs — and rendering the result
into the reference file at build/release time. Skills reference method names by name (e.g.
`getWorkItemById`, `addWorkItemComment`) exactly as the source plugin's skills already do; the catalog
file is the cross-check surface, not a new abstraction layer skills call through.

Both introspection subcommands run with **zero credentials and zero network access** — verified
2026-08-11: `list --json` and `docs --out` both exit `0` with `AZURE_DEVOPS_ORG_URL` and
`AZURE_DEVOPS_PROJECT` unset. Gates V2 and V3 are therefore runnable in ordinary CI with no secrets.

**Counting caveat for the generator.** The real catalog is **130 methods** across 10 categories
(Work Items 18, Boards & Sprints 10, Projects 10, Git 25, Testing 14, DevSecOps 13, Artifacts 12,
AI-Assisted 12, Wiki 5, Build 11) — verified from `list --json`. Note that `docs --out <dir>` emits
**131 files**, because it also writes an index `README.md` alongside the 130 `<method>-reference.md`
files; do not derive the method count from a file count.

**Regeneration is a release-verification item, not a one-time step** — see V2: every release must
regenerate `method-catalog.md` from the bundled `ado-cli.js` version actually shipped and diff it
against the version-controlled copy, so the catalog never silently drifts from the bundled CLI.


## Data flow

1. A user or the model invokes an `azure-devops-*` skill (directly, or via its command).
2. The skill's first ADO touchpoint runs the session warm-up: confirm the proxy/CA environment
   preconditions (uppercase proxy vars, `NODE_EXTRA_CA_CERTS`) and establish auth via
   `sandbox-auth:azure-devops` (soft — only runs the full handshake if a prior call actually failed
   with an auth error, mirroring the source plugin's existing "soft, retry-on-failure" pattern rather
   than forcing a handshake on every call).
3. The skill invokes `node ado-cli.js <method> --structured` with a JSON body on stdin, per the
   [canonical invocation form](#canonical-invocation-form), exactly where the source skill currently
   names an MCP tool. One process per operation; no shared state between calls.
4. `ado-cli.js` makes the actual HTTPS call through the MITM egress proxy to `dev.azure.com` /
   `<org>.visualstudio.com`, using the proxy settings its own `typed-rest-client` stack reads from the
   inherited environment.
5. The egress proxy injects the real credential server-side (per `sandbox-auth`'s architecture); the
   plugin and the CLI process never hold or see it.
6. `ado-cli.js` returns JSON on stdout and an exit code; the skill reads both (never stderr) and
   continues its existing prompt logic (post a comment, format a table, etc.) exactly as before,
   substituting the CLI's structured result for what used to be an MCP tool result.
7. Comments/PR updates flow back to Azure DevOps the same way — through `ado-cli.js` → the proxy →
   ADO — never through a separate direct-`fetch` path. This closes the one place the source plugin
   bypassed the tool layer: `ado-work-my-backlog`'s `scripts/ado-api.mjs` (see
   [Compatibility rule 9](#compatibility-and-migration-rules)).

## Security and privacy

- **No PAT ever exists in this plugin's process, config, or logs.** No skill, agent, command, or script
  reads `AZURE_DEVOPS_PAT`/`AZURE_DEVOPS_PERSONAL_ACCESS_TOKEN`/`AZURE_DEVOPS_BEARER_TOKEN`, passes
  `--pat`, or writes an auth record file. This deliberately makes unreachable the plaintext
  auth-record persistence path the audit found in `ado-cli.js`'s bundled `EntraAuthHandler`
  (`getAuthRecordDir()` → `~/.azuredevops-mcp/`) — that code exists inside the bundle but is never
  exercised because the CLI is only ever invoked with the no-credential `auth:"none"` path.
  `provenance.json` (see [Known limitations](#known-limitations-carried-into-v100)) records this as an
  explicit, intentional non-use of that code path, not an oversight. Gate V5 is the check that keeps it
  true.
- **No secrets in transcripts.** Skills must not echo raw request bodies or raw CLI output back to the
  user wholesale; they summarize. Defensively, any field named `token`/`pat`/`password`/`secret`/
  `authorization` appearing in a CLI result is redacted before it is quoted anywhere.
- **Egress proxy env vars are inherited, never printed.** `HTTP_PROXY`/`HTTPS_PROXY` in a sandbox embed
  a per-sandbox token as basic-auth userinfo. Skills invoke `node ado-cli.js …` and let the child
  inherit them; **no skill may `echo`, `env`, `printenv`, or otherwise dump the environment** as a
  diagnostic step, and no failure report may quote those two values. If a diagnostic genuinely needs to
  confirm a proxy is configured, it checks *presence* (`[ -n "$HTTPS_PROXY" ]`), never the value. This
  is the single most likely accidental-disclosure path in the whole design and is called out
  explicitly because there is no adapter layer to redact on the plugin's behalf.
- **The CLI's own stderr banner is benign but must not be forwarded blindly.** `ado-cli.js` prints
  `[Auth] Auth type: none, PAT: not set` to stderr on every invocation (verified 2026-08-11). It
  discloses no secret — and under this design it is always exactly that string, since `--pat` is never
  passed — but it is CLI-internal noise that should not surface to the user as part of an error
  message. It is also a useful invariant: if that banner ever reads anything other than
  `Auth type: none, PAT: not set`, a PAT has leaked into the invocation and the run must stop.

- **Append-only comments** — see [Mutation policy](#mutation-policy). With no adapter, this is enforced
  as a `CLAUDE.md` prohibition and a release-verification review item rather than a runtime block; the
  trade-off is stated plainly there rather than glossed over.

## Mutation policy

The approved decision is that the user's explicit task intent (invoking an `azure-devops-*` skill/
command) already authorizes the ordinary mutations that skill performs — no additional confirmation
gate is added on top of what the source plugin already does today. Extra confirmation is required only
for actions that are exceptional: destructive, irreversible, or outside what any migrated skill
actually uses.

**How it is enforced, stated honestly.** With no adapter, there is no code path that can refuse a call.
The policy lives in `azure-devops/CLAUDE.md` as a standing rule that applies to every ADO invocation in
the session, restated at the relevant step inside each skill that can trigger it, and checked at
release time by the [workflow-instruction review](#release-verification) (V6). It is instruction-level
enforcement, and the same kind of enforcement the source plugin already relies on for its existing
"Comments Are Append-Only" rule — not a regression from a working technical control, but not a
technical control either. This is a deliberate trade of a runtime guarantee for the removal of a whole
layer of code; it is recorded as such in
[Known limitations](#known-limitations-carried-into-v100) rather than presented as equivalent.

The three tiers below are therefore written as rules the model must follow, phrased in the imperative
so they can be copied verbatim into `CLAUDE.md`.

**Tier 1 — Ordinary (proceed without asking).** Tier 1 is defined as *exactly* the set of methods
the migrated skills/agents/commands reference by name, plus the methods the migrated `ado-api.mjs`
needs after Compatibility rule 9 rehomes its transport. It is not a curated wish-list — it is that set,
enumerated. Verified against the live source plugin and `list --json` on 2026-08-11:

- *Referenced by migrated skills/agents today:* `addWorkItemComment`, `createLink`, `createPullRequest`,
  `createWorkItem`, `getAllPullRequestChanges`, `getCurrentSprint`, `getPullRequest`,
  `getPullRequestComments`, `getPullRequestFileChanges`, `getSprints`, `getSprintWorkItems`,
  `getTeamMembers`, `getTeams`, `getWorkItemById`, `getWorkItemTypes`, `listPullRequests`,
  `listWorkItems`, `replyToComment`, `updatePullRequestThread`, `updateWorkItem`, `updateWorkItemState`.
- *Required by the migrated `ado-api.mjs` backlog scanner* (which today reaches these through its own
  direct REST calls and must reach them through `ado-cli.js` instead): `getWorkItemComments`,
  `getWorkItemsBatch`, `getBuilds`, `getPullRequestBuilds`, `getBuildTimeline`, `getBuildLog`.

Two corrections this list encodes, both found by cross-checking the live catalog:

- **`getPullRequestById` does not exist.** An earlier draft of this spec listed it in Tier 1. The real
  method is **`getPullRequest`**, which is also what the source skills/agents actually reference. There
  is no `...ById` variant for pull requests.
- **`searchWorkItems` does not exist** — see [Compatibility rule 8](#compatibility-and-migration-rules).

Methods that an earlier draft listed in Tier 1 but which **no** migrated skill, agent, command, or
`ado-api.mjs` path actually uses — `getDefinitions`, `listWikis`, `getWikiPageContent`,
`getCommitHistory`, `browseRepository`, `getFileContent` — are **removed from Tier 1** and fall under
Tier 3's out-of-scope catch-all. Leaving them in Tier 1 created a direct contradiction: the same method
was simultaneously "always allowed" and "gated because no skill references it."

Existing skills that already ask the user to confirm before creating/updating (e.g.
`azure-devops-work-items`' "always confirm before making changes", `azure-devops-draft-work-item`'s
mandatory preview) keep that prompt-level confirmation exactly as-is — it is unaffected by this policy
layer.

**Tier 1 is derived, not hand-maintained.** Because Tier 1 is by definition "the referenced set," the
method-reference check in [release verification](#release-verification) (V3) is what keeps it honest:
the same cross-check that validates method names against the catalog also confirms that the Tier 1
list written into `CLAUDE.md` equals the set of names actually referenced across the plugin. A method
added to a skill but not to Tier 1 (or the reverse) is a finding at release time.

**The enumeration above is a verified snapshot, not the authority.** It was produced by grepping the
source plugin's markdown for camelCase method names on 2026-08-11, so it can under-count names that
appear only in prose or in a table cell the pattern missed (`addPullRequestComment`,
`getWorkItemTypeFields`, `assignWorkItem`, `addChildWorkItem`, and `getQueryResults` are all real
catalog methods that a migrated skill may legitimately end up referencing). Implementation must
recompute the set from the actually-migrated files rather than transcribing this list, and let V3
reconcile the two. The list is here to fix the two concrete errors it corrects and to pin the
*definition* of Tier 1 — not to freeze its membership.



**Tier 2 — Forbidden (never invoke).** `manageWorkItemComment` with `action: "update"` or
`action: "delete"` must never be invoked by any skill, agent, command, or script in this plugin. This
carries forward the plugin's existing "Comments Are Append-Only" rule (`CLAUDE.md`, copied verbatim —
see [Compatibility rule 3](#compatibility-and-migration-rules)) and closes the gap the audit flagged
(the method's `update`/`delete` actions directly contradict the plugin's own append-only rule) at the
strongest level available without a code layer: the method name appears in no migrated file except as
the subject of the prohibition, so V6's review and V3's method-reference check both surface any
reintroduction. To correct a comment, post a new one.

**Tier 3 — Confirm first (destructive or out-of-scope).** Any method matching either condition below
requires an explicit HITL confirmation from the user *before* the invocation is made — the skill states
what it is about to do and to which resource, and proceeds only on an affirmative answer:

- **Destructive/irreversible by nature**, regardless of whether a migrated skill currently calls it:
  `mergePullRequest`, `deletePackageVersion`, `rotateSecrets`, `manageSecurityPolicies`, `runPipeline`,
  overwrite-style `createOrUpdateWikiPage`, and any bulk create/update/delete method.
- **Out-of-scope catch-all**: any method not referenced by name in any migrated skill/agent/command file
  (cross-checked against `method-catalog.md`). If a future prompt asks the model to call an ADO method
  no shipped skill uses, it is exceptional by definition and gets the same confirmation as Tier 3 above,
  even if the method itself looks benign — this is what "extra confirmation required only for
  exceptional destructive/out-of-scope actions" means concretely.

The three tiers live in **one place** — `azure-devops/CLAUDE.md` — rather than scattered across skills,
so a single read of that one file tells you the entire mutation policy. Skills restate only the tier
rule that applies at the specific step where they could trip it.

## Error handling

Skills read `ado-cli.js`'s exit code and stdout directly. `references/error-codes.md` documents the
mapping from what the CLI actually returns to a small named vocabulary, so every skill reacts the same
way to the same failure and error-handling instructions can say "on `E_AUTH`, …" instead of restating
exit-code trivia at each call site. **The names are documentation and shared vocabulary, not a runtime
type** — nothing constructs an `E_AUTH` object; the skill recognizes the condition and follows the rule.

CLI exit codes: 0 success; 1 generic/tool error; 2 invalid input/Zod validation failure; 3 unknown
method; 4 missing required env vars. ADO's own REST error JSON (`message`, `typeName`, `typeKey`,
`errorCode`, `eventId`) appears in stdout for upstream failures.

| Name | Meaning | How the skill recognizes it | What the skill does |
|---|---|---|---|
| `E_AUTH` | Not authenticated / auth rejected | HTTP 401/403 from ADO in the result body, or a proxy `403 denied` | Run `sandbox-auth:azure-devops` once, retry once, then report |
| `E_NOT_FOUND` | Target resource does not exist | HTTP 404, or an ADO `typeKey` naming a missing work item/PR | Report the specific ID that was not found; do not retry |
| `E_VALIDATION` | Input failed schema validation | Exit code 2 | Fix the request body; `node ado-cli.js help <method>` prints the schema |
| `E_UNKNOWN_METHOD` | Method not recognized by the bundled CLI | Exit code 3 | Stop — the skill references a method that does not exist; a V3 finding |
| `E_CONFIG` | Required environment/config missing | Exit code 4 | Report which of `AZURE_DEVOPS_ORG_URL` / `AZURE_DEVOPS_PROJECT` is unset; do not retry |
| `E_RATE_LIMIT` | Throttled by ADO | HTTP 429 with `Retry-After` | Wait and retry once (the CLI's own `withAuthRetry` already backs off before this surfaces) |
| `E_BLOCKED` | [Mutation policy](#mutation-policy) forbids the call | Tier 2, or Tier 3 without confirmation — recognized *before* invoking | Do not invoke; explain, and for Tier 3 ask for confirmation |
| `E_UPSTREAM` | ADO returned a well-formed error with no more specific code | Any other 4xx/5xx with a parsed ADO error body | Surface ADO's own `message` verbatim |
| `E_TRANSPORT` | Could not reach ADO at all | Exit code 1 with no parseable ADO error body, or `node` failing to start | Check the proxy/CA preconditions once, then report — never quote the proxy URL |

**Reading the result correctly.** Branch on **exit code and stdout only**. Verified 2026-08-11: every
invocation writes an `[Auth] Auth type: none, PAT: not set` banner to stderr, and Node emits a
`DEP0169` `url.parse()` deprecation warning from a bundled dependency — **both appear on successful,
exit-0 runs**. stdout stays clean JSON under `--structured`. A skill that treats non-empty stderr as
failure will fail 100% of its calls. stderr is diagnostic context for a failure that the exit code has
already established, never the failure signal itself.

## Compatibility and migration rules

1. **Namespace change only, with one named exception.** Every skill/agent/command directory and file
   basename stays identical to the source `ado` plugin; only the plugin-namespace prefix changes,
   `ado:` → `azure-devops:`, and each skill/command/agent's own file-basename prefix changes
   `ado-` → `azure-devops-` (e.g. `ado-work-on/SKILL.md` → `azure-devops-work-on/SKILL.md`) so names
   stay unambiguous inside a multi-plugin marketplace. No skill's actual procedure, phase structure, or
   decision logic is rewritten — only the tool-call layer and namespace strings change.
   - **Exception: `ado-devops-assistant` becomes `ado-assistant`, not
     `azure-devops-devops-assistant`.** Mechanical prefixing produces a stuttering name; the generic
     routing agent ships as **`ado-assistant`**. This is a deliberate, recorded exception (see
     [Recorded decisions](#recorded-decisions)) and the only component in the plugin that keeps a bare
     `ado-` prefix, so V4's colon-free grep carries it as an explicit allowed name rather than treating
     it as an unmigrated reference.
2. **MCP tool call → direct CLI invocation, mechanically.** Every place a skill/agent currently says
   "call `<toolName>`" (an MCP tool) becomes an invocation of
   `node "${CLAUDE_PLUGIN_ROOT}/scripts/ado-cli.js" <toolName> --structured` with the same parameters,
   in the same order, for the same purpose, supplied as a JSON body on stdin per the
   [canonical invocation form](#canonical-invocation-form). Method names themselves are unchanged
   (`getWorkItemById` stays `getWorkItemById`) — only the calling convention changes. There is no
   wrapper to call and no import to add.
3. **`CLAUDE.md` rules carried forward, and it gains three responsibilities.** The "Comments Are
   Append-Only" rule is copied verbatim. The "MCP Prerequisite — Auto-Setup" rule is replaced with the
   sandbox-auth rule in
   [Authentication](#authentication-sandbox-auth-only-proxy-injected-zero-pat-surface) above — same
   "soft, auto-retry-on-failure" shape, different mechanism. Because there is no adapter, the migrated
   `azure-devops/CLAUDE.md` is now also the single home for (a) the
   [canonical invocation form](#canonical-invocation-form), (b) the session warm-up preamble
   (proxy/CA precondition check + sandbox-auth), and (c) the full three-tier
   [mutation policy](#mutation-policy). Keeping all three in one always-loaded file is what replaces
   the "one file tells you the whole policy" property the adapter used to provide.
4. **`ado-mentions` → `azure-devops-mentions`, copied verbatim.** No content changes; only the namespace
   prefix.
5. **`ado-publish-pr`, `ado-babysit-pr`, `ado-work-items`, `ado-work-my-backlog` migrate 1:1** — same
   phases, same decision logic, MCP tool names replaced by direct CLI invocations per rule 2.
   `ado-work-items`' line 27 stale `searchWorkItems` reference is fixed per rule 8, not otherwise
   changed.
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
     collapses). **Correction:** an earlier draft of this rule claimed `ado-state-transitions.md` and
     `ado-mention-conventions.md` were "the two genuinely ADO-specific reference files this plugin
     already ships natively — reuse those, don't re-derive them." That is only half true, verified
     2026-08-11: the source `ado` plugin's `references/` contains exactly three files
     (`ado-mention-conventions.md`, `review-reception-protocol.md`, `review-thread-state-machine.md`).
     **`ado-state-transitions.md` is not among them** — it lives in the `development` plugin, at
     `claude_plugins/development/skills/work-on/reference/ado-state-transitions.md`, and is consumed by
     `development:work-on` when its provider is Azure DevOps. Since `azure-devops-work-on` is absorbing
     that skill's ADO-specific behavior, this file must be **copied in from `development`** like the
     other reference files above, not "reused" from a source `ado` directory that never had it. The
     [file tree](#component-and-file-responsibilities) lists it under `references/` accordingly. Only
     `ado-mention-conventions.md` is genuinely already native to the source plugin.

7. **`review-thread-state-machine.md` is corrected in two places, not carried forward verbatim.**

   *(a) The dangling synchronization note.* The source file opens with "this lifecycle is copied into
   the code-reviewer and ADO plugins; the copies must remain byte-identical" — `code-reviewer` does not
   exist in this repo, so that claim is now inaccurate and dangling. The migrated copy replaces that
   note with: "this lifecycle document describes the PR-review thread conventions this plugin's
   `azure-devops-babysit-pr-worker` agent follows; it originated as a shared reference with a
   `code-reviewer` plugin that is not part of this marketplace."

   *(b) Rule 1, which is now resolved.* Verified 2026-08-11, the source plugin ships **three** mutually
   inconsistent statements about who resolves threads — the two agents contradict *each other*, not
   merely a shared reference doc:
   - `references/review-thread-state-machine.md` line 180, Rule 1: *"**Only the reviewer closes
     threads** — the developer never resolves or closes threads in ADO."*
   - `agents/ado-babysit-pr-worker.md` (`### [Resolve] — Resolve Addressed Threads`): explicitly calls
     `updatePullRequestThread` with `status: "fixed"` / `"wontFix"` / `"byDesign"` / `"closed"`.
   - `agents/ado-pr-tender.md` (its `<do_not_resolve>` block): *"Do NOT resolve comment threads — let
     the reviewer resolve them."*

   **Decision (recorded — see [Recorded decisions](#recorded-decisions)): the split is by mode, not by
   actor.** The autonomous babysit worker **may** resolve a thread, but only *after* it has applied the
   requested change **and** verified it — the worker is operating unattended, and leaving resolved work
   marked open defeats the loop it exists to run. The interactive PR tender **never** resolves; a human
   is present, and resolution is theirs.

   All three files are made consistent with that decision:
   - `review-thread-state-machine.md` Rule 1 is rewritten to: *"Threads are closed by the reviewer, or
     by the autonomous babysit worker once it has applied **and verified** the requested change.
     Interactive/assistive flows never resolve threads on the developer's behalf."*
   - `azure-devops-babysit-pr-worker`'s `[Resolve]` step is kept, with its precondition made explicit:
     resolve only after the change is applied and verified — never on intent, never on "will fix."
   - `azure-devops-pr-tender`'s `<do_not_resolve>` block is kept **verbatim and unchanged**; it is now
     consistent with Rule 1 rather than in tension with it.

   The decision is restated in `azure-devops/CLAUDE.md` so the rule is loaded in every session, not
   only when the reference file happens to be read.

8. **Stale method-name references are fixed, with the replacement now identified.**
   `ado-work-items/SKILL.md` line 27 (`` 2. Run `listWorkItems` or `searchWorkItems`. ``) and
   `ado-draft-work-item/SKILL.md`'s Duplicate Check table both reference `searchWorkItems`, which does
   not exist among the CLI's 130 real methods (verified against `list --json`, 2026-08-11).

   **The replacement is `listWorkItems`** — the CLI's own catalog describes it as the *"Preferred
   structured work item query path. Usually 1 WIQL call plus 1 batched hydrate call for up to 200
   returned items"*, i.e. exactly the WIQL-based query capability the stale name was reaching for. Note
   that `ado-work-items/SKILL.md` line 27 already names `listWorkItems` alongside it, so the fix is to
   drop the `or searchWorkItems` clause rather than substitute a different method. Two adjacent methods
   are worth considering per call site instead of a blind substitution: `getMyWorkItems` (assigned-to-me
   slice) and `getQueryResults` (saved team queries) — pick per the surrounding intent.

   The general cross-check remains a release-verification item (V3): during implementation, verify
   **every** method name appearing in **every** migrated skill/agent/command file against a freshly
   generated `node scripts/ado-cli.js list --json`, and correct any other mismatch found.
   `searchWorkItems` and `getPullRequestById` (see [Mutation policy](#mutation-policy)) are the two
   already known.

9. **`ado-work-my-backlog/scripts/ado-api.mjs`'s direct PAT auth is removed, not migrated as-is.**
   The source script's `getAuthHeader()` reads `AZURE_DEVOPS_PAT` / `AZURE_DEVOPS_PERSONAL_ACCESS_TOKEN`
   / `AZURE_DEVOPS_BEARER_TOKEN` directly and builds its own `Authorization` header for its own
   `adoFetch()` calls — a second, independent path to ADO that bypasses the CLI entirely and directly
   contradicts the sandbox-auth-only decision. The migrated `azure-devops-work-my-backlog/
   scripts/ado-api.mjs` removes `getAuthHeader()` and `adoFetch()`'s direct-`fetch` transport entirely
   and instead **spawns the bundled CLI directly** (`child_process.spawn('node', [adoCliPath, method,
   '--structured'])`, JSON body written to the child's stdin and the stream closed, result read from
   stdout, branch on exit code) — the same contract the skills use, expressed in JS rather than shell.
   The scanner's `-preview` API-version retry quirk for on-prem orgs and its default `api-version:
   "7.2"` become parameters in the JSON body, not header/URL construction of its own. Note that
   `fetchBuildFailureLogs()` calls `getAuthHeader()` directly, outside `adoFetch()`, so it must be
   rewritten too — removing `adoFetch()` alone leaves a live PAT read behind. `scan.mjs`,
   `classify.mjs`, and `state.mjs` are unchanged — they consume `ado-api.mjs`'s return values, not its
   transport.

   This is the one place in the plugin where CLI invocation is expressed as code rather than as a
   skill instruction, and it is not an adapter: it is local to `ado-api.mjs`, exports nothing
   invocation-related, and no other file imports it for that purpose.
10. **Examples and MCP-only artifacts are dropped**, per Non-goals: `examples/`, `launch-ado-mcp.sh`,
    `setup-ado-mcp` skill and command.
11. **Version starts fresh at 1.0.0.** The source plugin's `3.1.5` lineage is not continued —
    `azure-devops/.claude-plugin/plugin.json` starts at `"version": "1.0.0"`, matching the "marketplace
    v1.0.0 release" decision and the same pattern this repo already used for `sandbox` (which started
    at `1.0.0` rather than continuing `claude_plugins`' numbering).
12. **Agent frontmatter is carried forward as-is, but its cross-references are renamed.** Verified
    2026-08-11: all three source agents share this frontmatter shape —
    `name`, `description`, `user-invocable: true`, `disable-model-invocation: false`,
    `modelintelligence` (`5` for the babysit worker, `1` for the other two), `effort`
    (`high` / `xhigh`), and `skills: [ado-mentions]`. Two consequences the migration must handle:
    - **`skills: - ado-mentions` must become `skills: - azure-devops-mentions`** in all three agents.
      Rule 1 renames the skill directory; if the agents' frontmatter reference is not renamed with it,
      all three agents silently lose their mention-conventions skill at load time. This is the one
      cross-reference the `ado:`/`development:` namespace grep in V4 will **not** catch, because
      `ado-mentions` carries no namespace colon.
    - **`name` must match the filename**, which for the routing agent means `name: ado-assistant` in
      `agents/ado-assistant.md` per rule 1's exception — not `azure-devops-devops-assistant`.
    - **Do not add a `model` field.** These agents use `modelintelligence` + `effort`, not `model:`.
      An earlier draft of this spec required agents to carry "matching name/model fields," which every
      migrated agent would violate. `modelintelligence`/`effort`/`user-invocable`/
      `disable-model-invocation` are carried through unchanged. Note this differs from the frontmatter
      example in this repo's root `CLAUDE.md` (which documents `model:`/`tools:`/`permissionMode:`);
      the source agents' shape is preserved rather than rewritten, since rewriting model-selection
      semantics is not part of a transport migration.


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
  migration. It is treated as a vendored third-party dependency: pinned by hash in `provenance.json`,
  exercised through its own introspection subcommands, and trusted at its documented interface rather
  than audited internally.
- **Mutation policy is instruction-level, not enforced.** The three tiers in
  [Mutation policy](#mutation-policy) are rules in `CLAUDE.md`, not a code path that can refuse a call.
  A sufficiently determined or confused model could invoke `manageWorkItemComment action:"delete"`
  despite the prohibition. This is the direct cost of the no-adapter architecture, and it is accepted
  knowingly: the source plugin has exactly the same property today, the alternative was a whole
  runtime layer to build and maintain, and the highest-risk case (append-only comment violation) is
  bounded and visible in ADO's own history. Stated here so that a future maintainer reading
  "hard-blocked" in an old draft does not assume a control exists that does not.
- **CA trust is unverified against a live sandbox.** The transport is statically established
  (`typed-rest-client` + `tunnel` over `https.request`; zero undici), and `NODE_EXTRA_CA_CERTS` is the
  correct mechanism for it, but no network-reaching check is part of
  [release verification](#release-verification) — every check there is offline by design. The first
  real ADO call in a real sandbox is therefore where CA trust is proven. If it fails, the symptom is a
  TLS validation error surfacing as `E_TRANSPORT`, and the fallback is a `requestOptions.ca` /
  explicit-agent configuration; this spec is updated before release rather than patched around at
  runtime.

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
  purpose), the CLI exit-code / error-name table ([Error handling](#error-handling)), the
  [canonical invocation form](#canonical-invocation-form) including the
  `${CLAUDE_PLUGIN_ROOT}`-is-a-placeholder-not-a-shell-variable caveat (carried forward from
  `sandbox-auth/README.md`, and now load-bearing because every skill writes that path by hand), and an
  explicit "No PAT, no MCP server, no adapter — sandbox-auth only" statement up top.
- **`azure-devops/CLAUDE.md`**: per [Compatibility rule 3](#compatibility-and-migration-rules) — the
  single home for the invocation form, the session warm-up, and the mutation policy.
- No changes to `CLAUDE.md`, `.gitattributes`, or `.gitignore` at the repo root are required by this
  plugin — it introduces no new file types those don't already cover (`.mjs`/`.md`/`.json` are already
  handled).

## Release verification

These checks replace what an earlier draft of this spec called a test strategy. **This plugin ships no
test suite** — no `tests/` directory, no runner, no test files (see [Non-goals](#non-goals)). What it
ships is workflow markdown plus a vendored bundle, and what follows is the set of validation and review
steps appropriate to that. Every check below is **offline and credential-free**, so all of them can run
in ordinary CI or by hand.

- **V1 — Plugin validates.** `claude plugin validate ./azure-devops` passes: the manifest parses, and
  every skill/agent/command frontmatter block is well-formed with `name` matching the containing
  directory (skills) or filename basename (agents, including `ado-assistant`). Agents are **not**
  expected to carry a `model` field — see [Compatibility rule 12](#compatibility-and-migration-rules).
- **V2 — Bundled CLI is intact and introspectable.** Against the exact `ado-cli.js` being shipped:
  `node scripts/ado-cli.js` (bare usage), `node scripts/ado-cli.js list --json`,
  `node scripts/ado-cli.js docs --out <tmp>`, and `node scripts/ado-cli.js help <method>` for a
  sample of methods all exit `0` with `AZURE_DEVOPS_ORG_URL`/`AZURE_DEVOPS_PROJECT` unset and no
  network — verified 2026-08-11 that they do. `provenance.json`'s recorded size and hash match the
  shipped file. `references/method-catalog.md` is byte-identical to what regenerating it from that
  same bundle produces right now, so the catalog cannot silently drift from the CLI.
- **V3 — Method references and Tier 1 both resolve.** Every bare method name appearing in any migrated
  skill/agent/command/script matches an entry in a freshly generated `list --json`. `searchWorkItems`
  (→ `listWorkItems`, [rule 8](#compatibility-and-migration-rules)) and `getPullRequestById`
  (→ `getPullRequest`) are corrected, along with any other mismatch this finds. The Tier 1 list in
  `CLAUDE.md` equals that referenced set (see [Mutation policy](#mutation-policy)).
- **V4 — Self-containment holds.** Grep under `azure-devops/` returns zero hits for the `ado:`,
  `development:`, `gh:`, `code-reviewer:`, and `debugging:` namespaces — confirming the
  inline-not-delegate migration ([rule 6](#compatibility-and-migration-rules)) removed every
  external-plugin dependency, not just the obvious ones. The colon-free `ado-`-prefixed-component grep
  ([rule 12](#compatibility-and-migration-rules)) returns no hits outside its allowed names: the agent
  `ado-assistant` (rule 1's recorded exception), the filenames `ado-cli.js`, `ado-api.mjs`,
  `ado-mention-conventions.md`, and `ado-state-transitions.md`. In particular every agent's `skills:`
  list must name `azure-devops-mentions`, not `ado-mentions`.
- **V5 — Privacy scan: no PAT surface, no environment disclosure.** Two greps over the whole plugin
  tree. First, `AZURE_DEVOPS_PAT`, `AZURE_DEVOPS_PERSONAL_ACCESS_TOKEN`, `AZURE_DEVOPS_BEARER_TOKEN`,
  and `--pat`: the only permitted hits are the negative documentation of this rule itself (the
  README/`CLAUDE.md` explaining that no PAT path exists) — zero hits in any instruction or script that
  would supply a credential, and in particular `ado-api.mjs` must contain no `getAuthHeader`.
  Second, no file may instruct an `env` / `printenv` / `echo "$HTTP_PROXY"` / `echo "$HTTPS_PROXY"`
  style environment dump, per [Security and privacy](#security-and-privacy).
- **V6 — Workflow instruction review.** A human read of every migrated skill, agent, and command,
  checking four things that no automated check can: (a) every former MCP tool call became a correct
  [canonical invocation](#canonical-invocation-form) — `--structured` present, JSON on stdin, no
  `--input`, quoted heredoc, quoted `${CLAUDE_PLUGIN_ROOT}`; (b) the
  [mutation policy](#mutation-policy) is restated at each step that could trip it, and no file invokes
  a Tier 2 method or a Tier 3 method without first asking; (c) the inline-not-delegate rewrites in
  `azure-devops-work-on` / `azure-devops-draft-work-item` preserve every phase, gate, and outcome
  contract of the originals; (d) error handling branches on exit code and stdout, never on stderr
  being non-empty. This is the check that carries the most weight in a plugin whose behavior *is* its
  instructions, and it is deliberately listed last so it is done against the finished tree.
- **V7 — Marketplace/docs consistency.** `marketplace.json` parses as valid JSON, the new entry's
  `source` (`./azure-devops`) resolves to a real directory, and `README.md`'s new section and updated
  Extensibility reference are present.
- **V8 — Source repo untouched.** `claude_plugins` has zero uncommitted changes as a result of this
  work; `ado/scripts/ado-cli.js` and everything else in the source `ado/` plugin remain exactly as they
  were before this spec was written.

## Recorded decisions

Three questions this spec previously left open are now answered. They are recorded here and restated
in `azure-devops/CLAUDE.md` where they affect runtime behavior.

1. **Who resolves PR review threads? — Split by mode.** The autonomous `azure-devops-babysit-pr-worker`
   **may** resolve a thread, but only after it has applied **and verified** the requested change. The
   interactive `azure-devops-pr-tender` **never** resolves threads. `review-thread-state-machine.md`
   Rule 1 is rewritten to match; see [Compatibility rule 7](#compatibility-and-migration-rules) for the
   exact wording and the three files it reconciles.
2. **Tests? — None.** No test suite, no runner, no `package.json`, no `tests/`. Confidence comes from
   [release verification](#release-verification) above, which is validation and review, not testing.
   The wording matters: these checks are not "the test suite" under another name, and describing them
   that way would overstate what they establish.
3. **Routing agent name — `ado-assistant`.** Not `azure-devops-devops-assistant` (stutters) and not
   `azure-devops-assistant`. This is an intentional, one-off exception to
   [rule 1](#compatibility-and-migration-rules)'s mechanical prefixing, and the only bare `ado-`
   component name in the plugin; V4's grep carries it as an explicit allowed name.

## Non-goals (recap)

See [Non-goals](#non-goals) above for the complete list — restated here for scan-ability: no MCP
server, no PAT/credential UI, no re-implementation of `development`'s generic design/implement/blind-
spot engines (inline-not-delegate instead), no closing of the `ado-cli.js` provenance gap, no migration
of MCP-only artifacts (`launch-ado-mcp.sh`, `examples/`, `setup-ado-mcp`), and no modification of
`claude_plugins` whatsoever.

