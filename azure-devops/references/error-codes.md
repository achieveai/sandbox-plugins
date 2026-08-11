# Error Codes

This is the shared error vocabulary surfaced by `ado-cli.js`'s structured output — not a distinct runtime type. Referenced by CLAUDE.md's retry-on-E_AUTH rule and by babysit-pr-worker's blocker classification.

## Exit Codes

| Exit | Meaning |
|---|---|
| 0 | Success |
| 1 | Generic / tool error |
| 2 | Invalid input, Zod validation failure |
| 3 | Unknown method |
| 4 | Missing required env vars |

## Error Vocabulary

| Name | Meaning | How the skill recognizes it | What the skill does |
|---|---|---|---|
| `E_AUTH` | Not authenticated / auth rejected | HTTP 401/403 from ADO in the result body, or a proxy `403 denied` | Run `sandbox-auth:azure-devops` once, retry once, then report |
| `E_NOT_FOUND` | Target resource does not exist | HTTP 404, or an ADO `typeKey` naming a missing work item/PR | Report the specific ID. Do not retry. |
| `E_VALIDATION` | Input failed schema validation | Exit code 2 | Fix the body. `node ado-cli.js help <method>` prints the schema. |
| `E_UNKNOWN_METHOD` | Method not recognized by the bundled CLI | Exit code 3 | Stop — the skill references a method that does not exist. A V3 finding. |
| `E_CONFIG` | Required environment/config missing | Exit code 4 | Report which of `AZURE_DEVOPS_ORG_URL` / `AZURE_DEVOPS_PROJECT` is unset. Do not retry. |
| `E_RATE_LIMIT` | Throttled by ADO | HTTP 429 with `Retry-After` | Wait, retry once. The CLI's `withAuthRetry` already backs off before this surfaces. |
| `E_BLOCKED` | Mutation policy forbids the call | Tier 2, or Tier 3 without confirmation — recognized *before* invoking | Do not invoke. Explain; for Tier 3, ask. |
| `E_UPSTREAM` | ADO returned a well-formed error with no more specific code | Any other 4xx/5xx with a parsed ADO error body | Surface ADO's own `message` verbatim |
| `E_TRANSPORT` | Could not reach ADO at all | Exit code 1 with no parseable ADO error body, or `node` failing to start | Check proxy/CA preconditions once, then report. **Never quote the proxy URL.** |
