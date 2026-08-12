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

## Mutation Policy (3 tiers — enforced by instruction, not code)

> **This file is reference documentation, not an enforcement point.** `claude plugin validate` (v2.1.228)
> reports that a plugin-root `CLAUDE.md` is *not* loaded as project context, so nothing here is guaranteed
> to be in context when a skill or agent runs. The enforcing copy of the rules below — plus the privacy
> rules — is restated verbatim inside every executing component that can mutate Azure DevOps, under a
> `<mutation_privacy_policy>` block.

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
