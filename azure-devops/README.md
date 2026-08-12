# azure-devops

CLI-native Azure DevOps workflows — work items, PR publish/babysit/tend, review-thread response,
and backlog triage. Every operation runs through a bundled `ado-cli.js`, spawned as a child
process. **No MCP server. No PAT interface. No adapter/wrapper library. Sandbox-auth only.**

## Quick start

```
/plugin install azure-devops@sandbox-plugins-marketplace
```

Then, once per session, before the first Azure DevOps call:

```
/sandbox-auth:azure-devops
```

## Skills

7 skills. `work-on`, `publish-pr`, `babysit-pr`, `draft-work-item`, and `work-my-backlog` are also
reachable as slash commands (below). `work-items` and `mentions` are internal helpers other
skills/agents load by name.

| Skill | Purpose |
|---|---|
| `work-on` | Autonomous two-phase workflow driven by an Azure DevOps work item: analyze, plan, post the plan, wait for HITL approval, then implement, verify, and publish a PR. |
| `publish-pr` | Publish local changes as a PR — creates or links a work item, pushes the branch, composes the description, optionally tends reviewer feedback and build failures. |
| `babysit-pr` | Autonomous PR monitoring loop — fixes build breaks, test failures, coverage gaps, and review comments unattended. |
| `draft-work-item` | Turns a rough requirement into a structured work item — classifies intent, drafts at the appropriate depth, runs a duplicate check and mandatory preview, then creates it. |
| `work-my-backlog` | Scans a sprint backlog assigned to the caller, classifies each item's state, and dispatches `work-on` / `babysit-pr-worker` as needed. |
| `work-items` | Work item CRUD and sprint queries (create, update, link, sprint management). Internal — loaded by name. |
| `mentions` | Azure DevOps `@`-mention and URL conventions. Internal — loaded by name. |

Alongside these: 3 agents (`assistant`, `pr-tender`, `babysit-pr-worker`) and 5 commands
(`/work-on`, `/publish-pr`, `/babysit-pr`, `/draft-work-item`, `/work-my-backlog`).

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
4. `${CLAUDE_PLUGIN_ROOT}` is a Claude Code placeholder, not a shell variable — it is substituted before the shell runs. Keep it double-quoted. Never `export` it or derive it via `dirname "$0"`.
5. Branch only on exit code and stdout. Non-empty stderr is not a failure — it always carries an auth banner and Node's DEP0169 deprecation warning, even on success.

## Authentication

No PAT, ever. Run `sandbox-auth:azure-devops` once per session before the first call (pass the
target org if known). If a call fails with `E_AUTH`, run it once more and retry. `AZURE_DEVOPS_ORG_URL`
and `AZURE_DEVOPS_PROJECT` are still required as plain configuration — not credentials.

## Error Vocabulary

Exit codes:

| Exit | Meaning |
|---|---|
| 0 | Success |
| 1 | Generic / tool error |
| 2 | Invalid input, Zod validation failure |
| 3 | Unknown method |
| 4 | Missing required env vars |

Named errors (shared vocabulary surfaced by the CLI's structured output — not a runtime type):

| Name | Meaning | What the skill does |
|---|---|---|
| `E_AUTH` | Not authenticated / auth rejected | Run `sandbox-auth:azure-devops` once, retry once, then report. |
| `E_NOT_FOUND` | Target resource does not exist | Report the specific ID. Do not retry. |
| `E_VALIDATION` | Input failed schema validation | Fix the body. `node ado-cli.js help <method>` prints the schema. |
| `E_UNKNOWN_METHOD` | Method not recognized by the bundled CLI | Stop — the skill references a method that does not exist. |
| `E_CONFIG` | Required environment/config missing | Report which of `AZURE_DEVOPS_ORG_URL` / `AZURE_DEVOPS_PROJECT` is unset. Do not retry. |
| `E_RATE_LIMIT` | Throttled by ADO | Wait, retry once. |
| `E_BLOCKED` | Mutation policy forbids the call | Do not invoke. Explain; for Tier 3, ask first. |
| `E_UPSTREAM` | ADO returned a well-formed error with no more specific code | Surface ADO's own `message` verbatim. |
| `E_TRANSPORT` | Could not reach ADO at all | Check proxy/CA preconditions once, then report. Never quote the proxy URL. |

Full detail: `references/error-codes.md`.

## Mutation Policy

Three tiers, enforced by instruction (no runtime adapter exists to enforce it in code):

1. **Ordinary** — methods any ported file already references, plus anything the user explicitly asks for. Proceed normally. Read-only discovery (`list --json`, `--help`, list/get/query methods) is never a mutation and never needs confirmation. Ordinary mutation never bypasses a workflow's own confirmation prompt, preview, hard checkpoint, or `approvalSource` gate — the stricter local rule wins.
2. **Forbidden** — `manageWorkItemComment` update/delete, and editing or deleting any already-posted work item or PR comment. Never invoke; post a new comment instead. Work item fields and state are still updatable.
3. **Confirm first** — destructive methods (e.g. `mergePullRequest`), anything out of scope, or a broad/bulk write. Ask before invoking, unless the user requested it or a hard gate in the workflow already authorized it.

See `CLAUDE.md` for the canonical `<mutation_privacy_policy>` block, restated byte-identical in all nine mutating skills and agents.

## Limitations

- **No independent provenance** — `scripts/ado-cli.js` is vendored from an untracked build artifact in
  the source repo, not a published release. `references/provenance.json` records byte size, content
  hash, and copy date so drift is detectable, but there is no upstream version pin beyond that.
- **Narrow auth surface** — only no-flag (`sandbox-auth`-injected) auth is reachable. Entra/az-cli/
  interactive/on-prem login modes the CLI's flags imply are not usable from this plugin.
- **The bundle is unreviewable** — 87,719 lines, treated as a vendored third-party dependency: pinned by
  hash, exercised through its own introspection (`list`, `help`, `docs`), trusted at its documented interface.
- **Mutation policy is not code-enforced** — a determined or confused model could invoke a forbidden
  method despite the instruction. There is no runtime block; ADO's own history is the backstop.
- **CA trust is unverified offline** — every release check runs credential-free with no network; the
  first real call in a real sandbox is where `NODE_EXTRA_CA_CERTS` correctness is actually proven.

## License

MIT
