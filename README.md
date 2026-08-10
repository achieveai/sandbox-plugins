# Sandbox Plugins Marketplace

A private Claude Code plugin marketplace, owned by `achieveai`, hosting plugins for working
inside sandboxed agent sessions.

## Installation

Add this marketplace to Claude Code:

```
/plugin marketplace add achieveai/sandbox-plugins
```

Then install a plugin from it:

```
/plugin install sandbox@sandbox-plugins-marketplace
/plugin install sandbox-auth@sandbox-plugins-marketplace
```

## Available Plugins

### sandbox (v1.0.0)

Workspace/runtime concerns for a sandboxed agent session: bootstrapping a workspace's memory and
scripts directories, maintaining task handoff state across a session, and script-based repeatable
workflows for the sandboxed agent itself. `sandbox` is about *operating inside* the sandbox.

### sandbox-auth (v2.1.1)

Egress-authentication concerns: the wire contract for authenticating outbound network calls from
inside a sandbox that sits behind a MITM egress-proxy + gateway auth-webhook. Covers the HTTP 511
`auth_pending` handshake, 403 deny handling, backoff polling, human-in-the-loop device-code relay,
transparent server-side token injection (the agent never holds tokens), warm-then-run, and a
portable Python probe engine, plus thin per-service skills (GitHub, Azure DevOps, Microsoft Graph,
generic connect) that reference the shared egress-auth mechanics with service-specific probe
URLs/scopes.

## Extensibility

**Sandbox is the expected runtime context for these plugins, not a naming taxonomy.** This
marketplace is expected to grow with plugins named for what they do — for example `email`,
`collaboration`, `azure-devops` — that happen to run inside a sandboxed agent session. Running in
a sandbox is an execution-environment fact, not an identity: a future plugin is never renamed or
prefixed `sandbox-` merely because it is expected to execute in one. The `sandbox` name is reserved
for the workspace/runtime plugin and `sandbox-auth` for the egress-authentication plugin; new,
domain-specific functionality belongs in its own, separately named plugin rather than being folded
into either of these.

## Repository Structure

```
sandbox-plugins/
├── .claude-plugin/
│   └── marketplace.json   # Marketplace catalog
├── sandbox/                # Workspace/runtime plugin
├── sandbox-auth/           # Egress-authentication plugin
├── scratchpad/             # Research and development notes
└── README.md                # This file
```

## Ownership

This is a private repository under the `achieveai` GitHub organization. See the marketplace
`owner` field in `.claude-plugin/marketplace.json` for the maintainer of record.
