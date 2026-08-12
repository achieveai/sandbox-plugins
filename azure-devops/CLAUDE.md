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

Before the first Azure DevOps CLI call in a session, run `sandbox-auth:azure-devops` (pass the target org if known). If a CLI call fails with an auth error—`E_AUTH` in this plugin's vocabulary; the CLI never prints that token, so recognize an HTTP 401/403, `unauthorized`, `authentication failed`, or a proxy `403 denied` inside the structured error body (see `references/error-codes.md`)—run `sandbox-auth:azure-devops` once more, then retry the call once. **Never ask the user to supply a PAT, token, or credential file** — this plugin has no PAT interface anywhere.

## Proxy and CA preconditions (session-level, not per-call)

| Variable | Requirement |
|---|---|
| `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY` | Uppercase only — no lowercase fallback. Read natively by the CLI's HTTP client. |
| `NODE_EXTRA_CA_CERTS` | Must be set **before** `node` starts, from whichever CA bundle var is already present (e.g. a Python/curl CA var). Node ignores `REQUESTS_CA_BUNDLE` / `SSL_CERT_FILE` / `CURL_CA_BUNDLE`. |

## Mutation & Privacy Policy — canonical text (enforced by instruction, not code)

> **This file is reference documentation, not an enforcement point.** `claude plugin validate` (v2.1.228)
> reports that a plugin-root `CLAUDE.md` is *not* loaded as project context, so nothing here is guaranteed
> to be in context when a skill or agent runs. The block below is the **canonical** text; the enforcing
> copies are restated **byte-identical** inside all nine executing components that can mutate Azure DevOps
> (`work-on`, `publish-pr`, `babysit-pr`, `work-items`, `work-my-backlog`, `draft-work-item`, `assistant`,
> `pr-tender`, `babysit-pr-worker`). Ten copies total; edit one, edit all ten, and re-verify identity.
> Tier vocabulary used by `references/error-codes.md`: Tier 1 = bullet 1 (ordinary, proceed),
> Tier 2 = bullet 3 (comment update/delete, forbidden), Tier 3 = bullet 4 (exceptional, confirm first).

<mutation_privacy_policy>
- **Ordinary mutations — just do them.** Invoking this workflow, or an explicit user request, authorizes the creates, updates, comments, commits, and pushes it performs by name. No extra gate is added on top. This NEVER bypasses an existing gate: confirmation prompts, mandatory previews, hard checkpoints, and `approvalSource` verifications stay exactly as written, and where a local rule is stricter, the local rule wins.
- **Read-only discovery is not a mutation.** `ado-cli.js list --json`, `--help`, and any list/get/query method may be called freely at any time — no confirmation, even for methods this plugin does not otherwise name.
- **Comments are append-only.** NEVER invoke `manageWorkItemComment` with `action: "update"` or `"delete"`, and never edit or delete a comment already posted on a work item or PR. To correct or supersede something, post a NEW comment. This limits comments only — a work item's own fields and state are still updated normally when the workflow or the user calls for it.
- **Confirm the exceptional first** — name the action and the resource, and proceed only on an affirmative: destructive or irreversible methods (`mergePullRequest`, `runPipeline`, `deletePackageVersion`, `rotateSecrets`, `manageSecurityPolicies`, overwrite-style `createOrUpdateWikiPage`), anything outside this workflow's stated scope, or a broad/bulk write across many items at once. Required only when the user did not ask for that action and no hard gate in this workflow already authorized it — once a checkpoint or `approvalSource` gate has passed, autonomous loops continue without further prompting.
- **No PAT surface.** Never ask the user for a PAT, token, or credential file, and never pass `--pat`. Auth is `sandbox-auth:azure-devops` only. The CLI's stderr auth banner is an invariant: auth type `none`, PAT not set. If it ever reports a PAT or any other auth type, STOP — treat it as a credential leak and report it instead of continuing.
- **Never dump the environment.** No `env`, `printenv`, or `echo "$HTTP_PROXY"`. Proxy variables carry a per-sandbox credential — test presence (`[ -n "$HTTPS_PROXY" ]`), never print the value.
- **Never paste raw payloads.** Summarize CLI output, request bodies, and build/test logs; never copy them wholesale into chat, ADO comments, or state files. Redact any `token`/`pat`/`password`/`secret`/`authorization` field before quoting it.
- **Thread resolution** follows this plugin's existing review-thread rules — nothing in this block changes them.
</mutation_privacy_policy>

## Review Thread Resolution

Threads are closed by the reviewer, or by the autonomous babysit worker once it has applied and verified the requested change. Interactive/assistive flows never resolve threads on the developer's behalf. See `references/review-thread-state-machine.md`.
