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
manifest, writes `.gitmodules` entries for `--repo <name>=<url>` without cloning, and
records the optional `--skill-bundle <plugin>=<path>` selection key.
`review-setup` then materializes `repos/<name>` per PR inside its own worktree pool.

## How the daemon pins it

`bundle.json` is the contract the daemon reads:

- `plugin` + `version` — the marketplace plugin and version to install/pin.
- `skills[].entryPoints` — script paths (relative to the plugin root) per operation;
  the daemon always appends `--root <review-repo-root>` and `--json`.
- `skills[].requiredCapabilities` — what the sandbox must provide (`posix-flock` means
  `review-setup` cannot run on Windows).
- `skills[].ioSchemaVersion` — the version of the CLI contract the skill honours: the
  flags each entry point accepts and the envelope it emits. It is bumped when a caller
  written against the previous version would break; `bundleSchemaVersion` versions the
  manifest itself, and `version` the plugin's code.
- `resultEnvelope` — the JSON shape every `--json` invocation emits:
  `{schemaVersion, status, runId, operation, artifacts, effects}` with
  `status ∈ {completed, no_op, retryable, blocked, failed}`.

## The result envelope

All three CLIs print exactly one envelope object on stdout under `--json`, as a single
line, with nothing else on stdout, on every path including failures. A caller that takes
the last stdout line and one that parses the whole stream therefore see the same object.

`--run-id <id>` carries the caller's run identity, echoed verbatim as `runId`
(`[A-Za-z0-9][A-Za-z0-9._-]{0,127}`). It is **required** with `--json` on the
`review-setup` CLIs and optional on `review-repo`, which generates its own when absent.

Statuses are mapped from exit codes without reinterpretation. In particular `review-setup`
exit `2` is a `completed` envelope carrying a `partial_context` effect: the run finished
but the required scope was not satisfied, and the caller must decide. Lock contention is
`retryable`, a moved PR snapshot is `blocked`. Argument errors exit `2` from `argparse`
before an operation exists to report on, and print no envelope.

`review-setup` also takes `--provider {ado,github}` on a PR setup. Only Azure DevOps is
implemented; `github` refuses before any filesystem or Git work with an
`unsupported_provider` effect.

`review-repo bootstrap|update` takes `--skill-bundle <plugin>=<path>`, which records
`skillBundle` in `review-workspace.manifest.json`. That key is the **only** thing that
selects a review-repo-supplied skill bundle — a bundle directory that is merely present
never selects. `check` reports a key that resolves to no `bundle.json` as
`skill_bundle_missing` and blocks.

Upgrade flow: install the new plugin version, run `review-repo check` on each review
repo; exit `2` → run `update`; exit `3` → a human resolves the local edit first.

## Tests

```bash
# review-repo (Windows or POSIX)
python -m pytest -q review-workspace/skills/review-repo/tests
# review-setup (POSIX only)
python3 -m pytest -q review-workspace/skills/review-setup/tests
```
