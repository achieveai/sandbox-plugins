# review-workspace

Versioned, plugin-packaged tooling for the code-review daemon's **review repositories**.
Instead of daemon code creating and mutating review repos, the daemon pins a version of
this plugin and runs its skills.

## Skills

| Skill | Role |
|-------|------|
| `review-repo` | Bootstrap / check / update the owned review-repo skeleton and register source repos as lazily-materialized submodules. Stdlib Python, Windows + POSIX. |
| `review-setup` | Prepare an isolated per-PR worktree with a pinned source snapshot, work-item hierarchy and blame provenance; reset/archive it afterwards. POSIX only (`fcntl`); requires `git` and `curl`. Imported from the repository owner's skill, see `PROVENANCE.md`. |

## Review repo layout

```
review_repo/
  .agents/skills/<repo-specific-skill>/...   # optional trusted overrides
  KnowledgeBase/<component>/...
  PRs/<project_repo>-<PR-Number>/...
  DeveloperLearnings/<dev-alias>/...
  repos/<project_repo>/                       # registered submodule; lazy materialization
  review-workspace.manifest.json              # owned paths + sha256 of owned files
```

`review-repo bootstrap` creates the five directories (each with a `.gitkeep`) and the
manifest, and writes `.gitmodules` entries for `--repo <name>=<url>` without cloning.
`review-setup` then materializes `repos/<name>` per PR inside its own worktree pool.

## How the daemon pins it

`bundle.json` is the contract the daemon reads:

- `plugin` + `version` — the marketplace plugin and version to install/pin.
- `skills[].entryPoints` — script paths (relative to the plugin root) per operation;
  the daemon always appends `--root <review-repo-root>` (and `--json` for `review-repo`).
- `skills[].requiredCapabilities` — what the sandbox must provide (`posix-flock` means
  `review-setup` cannot run on Windows).
- `resultEnvelope` — the JSON shape `review-repo --json` emits:
  `{schemaVersion, status, runId, operation, artifacts, effects}` with
  `status ∈ {completed, no_op, retryable, blocked, failed}`.

Upgrade flow: install the new plugin version, run `review-repo check` on each review
repo; exit `2` → run `update`; exit `3` → a human resolves the local edit first.

## Tests

```bash
# review-repo (Windows or POSIX)
python -m pytest -q review-workspace/skills/review-repo/tests
# review-setup (POSIX only)
python3 -m pytest -q review-workspace/skills/review-setup/tests
```
