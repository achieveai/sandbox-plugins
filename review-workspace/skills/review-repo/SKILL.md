---
name: review-repo
description: >
  Bootstrap, check, or update the owned skeleton of a code-review repository
  (KnowledgeBase/, PRs/, DeveloperLearnings/, repos/, .agents/skills/) and register
  source repositories as lazily-materialized submodules in .gitmodules. Use when creating
  a new review repo, when the daemon reports "upgrade needed", or before running
  review-setup against a repo whose layout is unknown. Never deletes, never overwrites a
  locally edited owned file, never clones a submodule.
user-invocable: true
allowed-tools: Read, Bash
---

# Review Repo — Bootstrap, Check, Update

One stdlib-only script, safe on Windows and POSIX (no `fcntl`, no hard links):

```bash
python3 <plugin-root>/skills/review-repo/scripts/review_repo.py plan      --root <review-repo-root> [--repo <name>=<git-url>]...
python3 <plugin-root>/skills/review-repo/scripts/review_repo.py bootstrap --root <review-repo-root> [--repo <name>=<git-url>]... [--dry-run]
python3 <plugin-root>/skills/review-repo/scripts/review_repo.py check     --root <review-repo-root>
python3 <plugin-root>/skills/review-repo/scripts/review_repo.py update    --root <review-repo-root>
```

Add `--json` to get the result envelope (`schemaVersion`, `status`, `runId`, `operation`,
`artifacts`, `effects`) defined in the plugin's `bundle.json`. `--root` is always required.

## Owned layout

```
review_repo/
  .agents/skills/<repo-specific-skill>/...   # optional trusted overrides
  KnowledgeBase/<component>/...
  PRs/<project_repo>-<PR-Number>/...
  DeveloperLearnings/<dev-alias>/...
  repos/<project_repo>/                       # registered submodule; lazy materialization
  review-workspace.manifest.json              # {schemaVersion, pluginVersion, createdAt, ownedPaths, fileHashes}
  .review-workspace/rollback-<timestamp>.json # written by update before it changes anything
```

The skill owns only the five directories, their `.gitkeep` files, and the manifest.
Everything else (PR artifacts, knowledge base content, `.gitmodules`, `.agents` overrides)
is preserved and ignored by `check`.

## Operations

- **plan** (or `bootstrap --dry-run`): print what bootstrap would create or register.
  No mutation. Exit `0`.
- **bootstrap**: create missing owned entries, register each `--repo` in `.gitmodules`
  at `repos/<name>` via `git config -f .gitmodules` **without cloning** (no
  `git submodule update`), and write the manifest. Existing content and existing
  `.gitmodules` entries are untouched. Re-running on a bootstrapped root is a `no_op`.
  Exit `0`; `3` if a `--repo` name is already registered with a different path or URL.
- **check**: compare the manifest's owned files and hashes with disk.
  Missing owned entries are reported as `upgrade_needed`; modified owned files as
  `conflict` ("local edit — refusing to overwrite"); unknown files are ignored.
  Exit `0` clean, `2` upgrade needed, `3` conflicts.
- **update**: apply only the missing owned entries. Refuses with exit `3` when `check`
  reports conflicts and changes nothing. Before writing, journals the previous manifest
  and per-file hashes to `.review-workspace/rollback-<timestamp>.json` (`status:
  pending`), then marks it `completed`. A leftover `pending` journal means an interrupted
  update: `check` reports `interrupted_update` (exit `2`) and the next `update` recovers
  from the journaled manifest.

Exit `1` (`status: failed`) for bad arguments, a missing `--root`, or a `git` failure.

## Notes

- The script never runs `git init`, `git add`, or `git commit`; the review repo is
  expected to already be a Git checkout (the daemon clones it). Commit the skeleton and
  `.gitmodules` yourself if you want them tracked.
- `review-setup` requires `.gitmodules` entries whose path is `repos/<name>` and whose
  URL is an Azure DevOps HTTPS URL; `review-repo` registers whatever `--repo` you pass
  and does not validate the provider.
