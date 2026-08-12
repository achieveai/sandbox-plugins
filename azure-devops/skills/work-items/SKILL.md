---
name: work-items
description: >
  Internal helper. Load only when explicitly named by another skill or agent.
user-invocable: true
disable-model-invocation: false
---

# Work Items

You are an Azure DevOps work item management assistant. Help the user create, update, query, and organize work items.

## Mutation & Privacy Policy

<mutation_privacy_policy>
- **Ordinary mutations — just do them.** Invoking this workflow authorizes the creates, updates, comments, commits, and pushes it performs by name. No extra gate is added; existing confirmation and preview prompts stay exactly as they are.
- **Comments are append-only.** NEVER invoke `manageWorkItemComment` with `action: "update"` or `"delete"`, and never edit or delete an existing work item or PR comment. To correct something, post a NEW comment.
- **Confirm anything exceptional first** — name the action and the resource, and proceed only on an affirmative: destructive or irreversible methods (`mergePullRequest`, `runPipeline`, `deletePackageVersion`, `rotateSecrets`, `manageSecurityPolicies`, overwrite-style `createOrUpdateWikiPage`), any bulk create/update/delete, anything outside this workflow's stated scope, or any CLI method this plugin does not reference by name.
- **No PAT surface.** Never ask the user for a PAT, token, or credential file, and never pass `--pat`. Auth is `sandbox-auth:azure-devops` only.
- **Never dump the environment.** No `env`, `printenv`, or `echo "$HTTP_PROXY"`. Proxy variables carry a per-sandbox credential — test presence (`[ -n "$HTTPS_PROXY" ]`), never print the value.
- **Never paste raw payloads.** Summarize CLI output, request bodies, and build/test logs; never copy them wholesale into chat, ADO comments, or state files. Redact any `token`/`pat`/`password`/`secret`/`authorization` field before quoting it.
- **Thread resolution** follows this plugin's existing review-thread rules — nothing in this block changes them.
</mutation_privacy_policy>

## Workflow

Branch based on what the user wants:

### Create

1. Ask for type (Bug/Task/User Story), title, description if not provided.
2. Detect current sprint via `getCurrentSprint`:

```bash
node "${CLAUDE_PLUGIN_ROOT}/scripts/ado-cli.js" getCurrentSprint --structured <<'ADOJSON'
{}
ADOJSON
```

3. Create with `createWorkItem`:

```bash
node "${CLAUDE_PLUGIN_ROOT}/scripts/ado-cli.js" createWorkItem --structured <<'ADOJSON'
{ "workItemType": "<Bug|Task|User Story>", "title": "<title>", "description": "<description>" }
ADOJSON
```

4. Report: "Created #ID: title"

### Query

1. Parse user intent into WIQL or text search.
2. Run `listWorkItems`:

```bash
node "${CLAUDE_PLUGIN_ROOT}/scripts/ado-cli.js" listWorkItems --structured <<'ADOJSON'
{ "query": "<WIQL query matching the described filters>" }
ADOJSON
```

3. Present results as a table: | ID | Type | Title | State | Assigned To |

### Update

1. Fetch current state with `getWorkItemById`:

```bash
node "${CLAUDE_PLUGIN_ROOT}/scripts/ado-cli.js" getWorkItemById --structured <<'ADOJSON'
{ "id": <work_item_id> }
ADOJSON
```

2. Show current values, confirm changes with user.
3. Apply with `updateWorkItem` for field changes:

```bash
node "${CLAUDE_PLUGIN_ROOT}/scripts/ado-cli.js" updateWorkItem --structured <<'ADOJSON'
{ "id": <work_item_id>, "fields": { "<field>": "<new value>" } }
ADOJSON
```

Use `updateWorkItemState` instead when only the workflow state is changing — it takes a different body shape (`state`, not `fields`):

```bash
node "${CLAUDE_PLUGIN_ROOT}/scripts/ado-cli.js" updateWorkItemState --structured <<'ADOJSON'
{ "id": <work_item_id>, "state": "<new state>" }
ADOJSON
```

### Link

1. Identify source and target (work item, PR, commit).
2. Create link with `createLink`. `repository` is required whenever `targetId` is a `PR#`, `BRANCH#`, or `COMMIT#` reference (omit it only when linking to another plain work item):

```bash
node "${CLAUDE_PLUGIN_ROOT}/scripts/ado-cli.js" createLink --structured <<'ADOJSON'
{ "sourceId": <work_item_id>, "targetId": "PR#<pr_id>", "linkType": "<link type>", "repository": "<repository name>" }
ADOJSON
```

For a work-item-to-work-item link, drop `repository` and use a plain ID (or `WI#<id>`) as `targetId`:

```bash
node "${CLAUDE_PLUGIN_ROOT}/scripts/ado-cli.js" createLink --structured <<'ADOJSON'
{ "sourceId": <work_item_id>, "targetId": "WI#<target_work_item_id>", "linkType": "<link type>" }
ADOJSON
```

3. Report the link created.

### Sprint Management

1. Use `getSprints` or `getCurrentSprint` to find target sprint:

```bash
node "${CLAUDE_PLUGIN_ROOT}/scripts/ado-cli.js" getSprints --structured <<'ADOJSON'
{}
ADOJSON
```

(use `getCurrentSprint` in place of `getSprints` when only the active sprint is needed.)

2. Use `getSprintWorkItems` to view sprint contents:

```bash
node "${CLAUDE_PLUGIN_ROOT}/scripts/ado-cli.js" getSprintWorkItems --structured <<'ADOJSON'
{ "sprintId": "<sprint id>" }
ADOJSON
```

3. Move items by updating their Iteration Path with `updateWorkItem`:

```bash
node "${CLAUDE_PLUGIN_ROOT}/scripts/ado-cli.js" updateWorkItem --structured <<'ADOJSON'
{ "id": <work_item_id>, "fields": { "System.IterationPath": "<target iteration path>" } }
ADOJSON
```

## Usage Examples

- "Create a bug for the login page crash"
- "Show me all active tasks assigned to me"
- "Move item 1234 to the current sprint"
- "Link work item 5678 to PR #42"

## ADO Reference Conventions

Use the `azure-devops:mentions` skill before composing work item descriptions,
comments, or link references. It loads the full mention syntax (`#ID` for work
items, `!ID` for PRs, `@alias` for users, bot comment prefix, etc.).

## Guidelines

- Always confirm before making changes to work items
- Use the bundled `ado-cli.js` (listWorkItems, createWorkItem, updateWorkItem, etc.) for all operations
- When creating items, ask for required fields if not provided: title, type, and description
- Format query results as readable tables
