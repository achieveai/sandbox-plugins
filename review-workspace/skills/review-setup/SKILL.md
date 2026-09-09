---
name: review-setup
description: >
  Prepare an isolated Git worktree, pinned source snapshot, work-item hierarchy, and
  blame-based file provenance for a NOVA PR review, then reset/archive it afterward.
  Collect-only: writes local artifacts and a local branch, never comments, pushes, or
  invokes a review agent. Use before code-reviewer:pr-context / pr-review to prepare and
  hand off Pre-fetched Context, or to clean up a review slot when done.
user-invocable: true
disable-model-invocation: false
allowed-tools: Read, Bash
---

# Review Setup — NOVA PR Prep & Reset

Deterministic prep/reset for repeated NOVA PR reviews: isolated worktree, pinned source
snapshot, work-item tree, and per-file blame provenance. Each explicit pooled setup
cleans uncommitted content and collects a new numbered round; commit rounds you want
to preserve as working-tree evidence.

## Check the sandbox first

```bash
python3 <plugin-root>/skills/review-setup/scripts/review_setup.py --preflight
```
Prints installed Python/Git/curl versions — no Git or network changes. Run it before
anything else; do not assume these tools are present.

## Set up a review

**Destructive on every explicit pooled PR setup:** discard staged/unstaged tracked
edits, untracked files and ignored files in the selected outer slot and managed source
checkout. This includes manual notes, local config and build output, including on a
same-PR rerun. Commit work you want preserved before running setup. Existing committed
review branches, committed evidence and retained source objects are preserved.

An explicit `--slot N` may switch that owned slot to another PR. Automatic allocation
never takes another occupied review. Root workspace, other slots, shared Git stores and
Git administrative files are protected. Unexpected nested repositories, mismatched
ownership, active Git operations and unfinished archive/reset recovery cause refusal
before cleanup; setup does not silently cancel archive recovery.

Warm-only commands, status and dry-run remain non-destructive. Legacy numeric slots
retain their existing lifecycle; full cleanup applies only to pooled slots.

**Allow substantial time: review setup can take as much as 400 seconds (6 minutes
40 seconds), especially when initializing or fetching source repositories.** This is
not a guaranteed upper bound. Use an execution timeout longer than 400 seconds where
supported. A tool/client timeout does not prove the underlying setup or Git processes
have stopped: check process and slot recovery state before retrying. Do not launch a
duplicate setup while the original run is still active.

### Warm-pool capacity and readiness

The repository-scoped pool is designed for six slots (`0`–`5`) per source repository
configured in the review repo's `.gitmodules`. Canonical names combine the configured
submodule spelling and slot number, such as `Nova-0` or `NovaClient-1` for submodules
named `Nova` and `NovaClient`. With five configured repositories this is a
desired capacity of 30 slots, not a
claim that all 30 are currently created, warm, or ready. Check reported state before
using a slot; never infer readiness from the configured capacity.

Each source repository has one script-owned bare common directory under
`review-setup/source-stores/{repo-key}.git`. Its six nested source worktrees have
independent working files, indexes, and detached HEADs while sharing Git objects. A
warm source HEAD may intentionally differ from the outer NOVA store's recorded
gitlink; that exact delta is expected only when the managed state validates it.
Warm-only commands refuse unrelated dirty, staged, untracked or ignored state.
Explicit PR setup cleans authorized working content only after ownership validation;
foreign paths and unsafe symlink traversal remain refusal conditions.

Legacy schema-1 numeric slots remain separate and unchanged. A legacy numeric slot
does not implicitly map to a repository-scoped slot such as `Nova-0`. Use only an
explicit legacy selector `--legacy-slot N` with reset; never guess or rename a
legacy slot.

Use the setup script's repository-scoped modes:

```bash
python3 <plugin-root>/skills/review-setup/scripts/review_setup.py --status --root <review-repo-root>
python3 <plugin-root>/skills/review-setup/scripts/review_setup.py --warm --repo Nova --slot 1 --dry-run --root <review-repo-root>
python3 <plugin-root>/skills/review-setup/scripts/review_setup.py --warm --repo Nova --slot 1 --root <review-repo-root>
python3 <plugin-root>/skills/review-setup/scripts/review_setup.py --warm-all --dry-run --root <review-repo-root>
python3 <plugin-root>/skills/review-setup/scripts/review_setup.py --warm-all --root <review-repo-root>
python3 <plugin-root>/skills/review-setup/scripts/review_setup.py <PR-number> --root <review-repo-root>
python3 <plugin-root>/skills/review-setup/scripts/review_setup.py <PR-number> --slot 2 --root <review-repo-root>
python3 <plugin-root>/skills/review-setup/scripts/review_setup.py <PR-number> --iteration 3 --slot 2 --org <org> --root <review-repo-root>
```

`--status` reads all configured repo/slot states. `--warm --repo NAME --slot INT`
warms one canonical repository slot. `--warm-all` processes repositories in configured
order and slots `0`–`5` sequentially; it does not start 30 concurrent downloads.
`--dry-run` is valid only with a warm mode and plans without mutation. In PR setup,
`--slot INT` is interpreted within the PR's resolved repository; omit it to
repo-locally auto-allocate/reuse. The main checkout is never switched.

Other flags: `--org` (URL or short org name; only needed when more than one ADO org is
configured), `--iteration INT` (a specific PR iteration), `--max-depth` (default
`1000000`), `--max-work-items` (default `2000`), `--max-commits` (default `500`),
`--root` (canonical review repo root; **required when installed from a plugin** — see
"Invoke by absolute path" below).

Pool rows use `absent` (not created), `warm` (validated reusable checkout), `occupied`
(active, foreign, or unexpectedly dirty state that is preserved), `partial`
(an owned interrupted operation), and `blocked` (managed topology cannot be safely
validated or completed). Warm commands return exit
code `2` if any requested row is occupied, partial, or blocked; status itself is read-only and
returns `0`. Never treat absent, occupied, or blocked as ready.

This detaches the source submodule at the exact PR head SHA (the base SHA is fetched and
resolved as the comparison base, not checked out), collects the work-item hierarchy and
per-file blame provenance, and writes `PRs/{repo-slug}-{pr}/PR_Setup_NN.json` + `.md`. A
repeat pooled setup re-resolves the PR, cleans uncommitted slot content and recollects
context. It writes the next numbered handoff rather than returning a stale ready
artifact; committed prior evidence is never overwritten. Deleted untracked handoff
numbers are not reused. Full previous run records remain in `review-setup/setup-history/`.
Use the same setup command to refresh incomplete provenance with a larger `--max-depth`;
the default 1M is a fetch budget ceiling, not an eager download of one million commits.
Deepening for provenance is best effort: when a deepen would exceed `--max-depth` (or the
bounded compatibility fetch budget on servers without relative-depth fetch, such as Azure
DevOps), setup stops deepening, keeps the affected files at `partial-shallow-history`, and
exits `2`. Only the comparison base is required history; it still refuses at the budget.

Exit codes: `0` complete, `2` partial context collected, `3` the PR snapshot changed
mid-collection (report marked stale — explicit pooled setup cleans and recollects a
new round; use `--iteration` deliberately to pin a historical snapshot), `1`
fatal/refused — inspect the reported recovery state before retrying; partial mutations
(worktree, submodule init, manifest) may already be on disk and journaled.

## Reset a slot

```bash
python3 <plugin-root>/skills/review-setup/scripts/review_reset.py --slot Nova-2 --dry-run --root <review-repo-root>
python3 <plugin-root>/skills/review-setup/scripts/review_reset.py --slot Nova-2 --root <review-repo-root>
python3 <plugin-root>/skills/review-setup/scripts/review_reset.py --slot Nova-2 --mode discard --confirm <repo-slug>-<pr-number> --root <review-repo-root>
```

`--mode` defaults to `archive`. `--dry-run` previews branch/file disposition with no
mutation. Discard requires `--confirm {repo-slug}-{pr-number}` to match exactly.

- **Archive (default):** commits only this PR's own artifact directory plus the pinned
  submodule pointer (gitlink) — nothing else in the tree — then renames the branch to
  `archive/review/{repo-slug}-{pr}/{run-id}`. Never pushes.
- **Discard:** removes only files this run wrote (hash-verified) and deletes the exact
  local `review/...` branch. Any unrecognized or user-modified file blocks discard —
  archive instead.

Exit codes: `0` success (including `--dry-run`), `1` failed/refused — recovery state is
retained and a retry resumes from where it stopped; no blind cleanup is performed even
after an interrupted archive with partial progress.

For a legacy numeric slot, use `review_reset.py --legacy-slot 2 --dry-run`.
Pooled reset keeps the nested source initialized and returns it to a verified baseline.
Archive requires an already-configured Git identity; the scripts never configure one.

## Scope

Review artifacts live only under this PR's own `PRs/{repo-slug}-{pr}/` directory.
Setup/reset also touch the worktree's submodule gitlink pointer (pinned or restored,
never left dangling) and its own local `review/...` / `archive/...` branch and
`.worktrees/` checkout. Internal bookkeeping — locks, manifests, slot registry, and run
history — lives under the repository's Git common directory (`review-setup/`), not
under `PRs/`. These scripts keep existing local exclusions in `.git/info/exclude` and
add `/.worktrees/` there if it isn't already present; they never modify `.gitignore`.
None of this touches the installed code-reviewer plugin, the daemon's own
`review-slot-*` clones, other PR rounds, or any remote/server state.

Shared-store objects needed by a retained review must be protected by owned Git refs;
a gitlink or a SHA recorded only in JSON does not preserve reachability through GC.
Periodic maintenance runs foreground after reset,
at most once per 24 hours for each owned idle source common directory, and conditional
on object-store filesystem headroom. It must retain normal two-week pruning grace and
must not use aggressive GC, immediate pruning, force, reflog expiry, or worktree
metadata pruning. A skipped or failed maintenance attempt does not make a successful
review reset fail.

Warm reset attempts a bounded refresh of a
verified default/main/dev baseline. A handled refresh failure may use only a verified
cached baseline and must report a warning; with no valid cache, the slot remains
recoverably blocked. The warm/status interface emits `absent`, `warm`, `occupied`,
`blocked`, or `partial`. Do not confuse a pool `partial` state with setup exit code `2`,
which means partial review context was collected. Never count partial, occupied,
blocked, or absent as ready.

Warmup checkout capacity is separate from maintenance headroom. Before creating more
working trees, the installed implementation must reserve capacity on both the
source-object and working-tree filesystems when those are distinct, account for the
remaining checkout footprint, and stop safely before disk exhaustion. Warm/setup currently enforce a 2 GiB minimum free-space reserve; maintenance requires
5 GiB plus its measured temporary object footprint. These minimums are not a
30-checkout capacity guarantee; rollout must separately measure remaining checkout
footprint. Desired capacity is not a disk-size promise.

## Invoke by absolute path

Run scripts as `python3 <plugin-root>/skills/review-setup/scripts/<script>.py --root <review-repo-root>`,
where `<plugin-root>` is the installed `review-workspace` plugin directory (for example
the marketplace plugin cache) and `<review-repo-root>` is the review repository that
holds `.gitmodules` and `PRs/`. A path relative to a slot's own checkout will not
resolve; always use absolute paths.

`--root` is **required** when the skill is installed from a plugin: the scripts only
infer the root when they live at `<review-repo-root>/.claude/skills/review-setup/scripts/`
(four directories below the root). Installed anywhere else, a missing `--root` is
refused with an explicit error rather than silently inferring the wrong directory.

## Machine-readable output

Both CLIs accept `--json`, which prints exactly one result-envelope object on stdout as a
single line and nothing else, on every path including failures:

```json
{"schemaVersion":1,"status":"completed","runId":"...","operation":"setup","artifacts":[],"effects":[]}
```

`--json` requires `--run-id <id>`, the caller's own run identity, echoed verbatim as
`runId`. Its grammar is `[A-Za-z0-9][A-Za-z0-9._-]{0,127}`. It is distinct from the
script's internal manifest run id, which is reported as a `script_run_id` effect.

Exit codes map onto envelope statuses faithfully — nothing is reinterpreted:

| Exit | Meaning | `status` | Effect |
|------|---------|----------|--------|
| `0` | complete | `completed` | `setup_complete` |
| `2` | partial context | `completed` | `partial_context` — the required scope was **not** satisfied |
| `3` | snapshot moved; reset first | `blocked` | `stale_snapshot` |
| `1` | refused | `failed` | `error`, or `unsupported_provider` |
| `1` | another run holds the lock | `retryable` | `lock_contention` |

`--status`, `--preflight` and dry-run warms report `no_op` with one `slot_status` effect
per row. Argument errors are the one exception: they exit `2` from `argparse` before an
operation exists to report on.

Paths in `artifacts` and in effect detail fields are relative to `--root`, with POSIX
separators.

## Providers

`--provider {ado,github}` selects the hosting provider for a PR setup (default `ado`);
it is not accepted in the other modes. Only Azure DevOps is implemented. `--provider
github` refuses immediately — before any filesystem or Git work — with exit `1`,
`status: failed` and an `unsupported_provider` effect. The GitHub adapter is future work.

## Handing results to code-reviewer

Read the produced `PR_Setup_NN.md` and pass it **verbatim** as a `Pre-fetched Context:`
block to `code-reviewer:pr-context` or `code-reviewer:pr-review`. This skill never
invokes a review agent and never posts a comment itself — that is always a separate,
explicit step.
