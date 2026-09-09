# Provenance — `skills/review-setup`

- **Source:** `C:\tmp\skills\review-setup\` (SKILL.md, `scripts/`, `tests/`), supplied by the
  repository owner; no license header in source.
- **Imported:** 2026-09-09 into `review-workspace/skills/review-setup/` (plugin version 0.1.0).
- **Excluded:** `scripts/__pycache__/`, `tests/__pycache__/` (machine artifacts).

## Source file hashes (SHA-256, as received)

| File | SHA-256 |
|------|---------|
| `SKILL.md` | `4b4831b36c8704a3998f6044122961d6ff1659dbfab8d60ef8129db39690e9c0` |
| `scripts/review_common.py` | `262be17e537df87dddea2d0ed97d690d0ad146a70ebf1c77f282af63796097b6` |
| `scripts/review_context.py` | `f2b36d326bae3a293cfe25c38fd46af83077bd90c74a5acb3603f8f89b774108` |
| `scripts/review_reset.py` | `b0c0b5a5c61b0c89932651d9cf788b98ce9d475ea92dbf31b067a8ca187f142b` |
| `scripts/review_setup.py` | `792c1a2f3e56112d75bcb03a247fffc94bdb036e9739684e1797dc307d893993` |
| `tests/test_fetch_compatibility.py` | `401dd52e172d475ef63dcbbe6376a5588840b9563ecac0056af035e2b88b55fb` |
| `tests/test_fetch_recovery.py` | `662508b53f30227be8270a460d25b5ad1f22eba808d5ca40554de530ea921055` |
| `tests/test_lifecycle.py` | `20dce8f55482662a380f44c8911ad29e840033e0a096f040521b82579826a378` |
| `tests/test_review_context.py` | `9d8e98ae05a8159f33721570dba8bbc748eeef87b3599a665423a1172ba1e721` |
| `tests/test_review_safety.py` | `22a13035439df738926a9810ce22933344e342503417430d9a91a356cc3aed55` |
| `tests/test_skill_docs.py` | `c58f298a5724ba5c73aa23182a4a1e48c6d7f031d7387d90880004b5079de49e` |

## Baseline test run (pristine source, before any adaptation)

Copied to a disposable WSL directory (`/tmp/review-setup-src`, Ubuntu 24.04, Python 3.12.3,
git 2.43.0, pytest 9.1.1 in a throwaway venv because the WSL image has no `pip`):

```
$ python -m pytest -q tests
146 passed, 43 subtests passed in 68.27s
```

No source test failed. No source script was patched to make tests pass.

## Adaptations made in the imported copy (packaging only)

Files whose imported hash differs from the source hash above:

| File | Imported SHA-256 | Change |
|------|------------------|--------|
| `SKILL.md` | `05a72ebc97cea023d4cbab9817674dd45f8f16824c8bf68b2e1bd9a1ea6f1d9b` | (a) `/workspace/.claude/skills/review-setup/scripts/…` → `<plugin-root>/skills/review-setup/scripts/… --root <review-repo-root>`; `--root` documented as required from a plugin install; "Invoke by absolute path" rewritten. (b) Literal repo list (Nova, NovaClient, Astra, WeveNova, MODISService) replaced with "the repositories configured in the review repo's `.gitmodules`". No other prose changed. |
| `scripts/review_common.py` | `74e5ab8eeaab82f73b260853ae81d593da944fcbde9e2ae068b95174a9557b0a` | `discover_root()`: when `--root` is absent and the script is not at `<root>/.claude/skills/review-setup/scripts/`, raise `ReviewError("--root is required: …")` instead of inferring `parents[4]`. In-repo inference and explicit `--root` behave exactly as before. |
| `tests/test_skill_docs.py` | `dd4ee18f222dfc6e741b8573b5ef0d833fc9126eb7f4e22a6dddcd61dce6da28` | `test_capacity_is_desired_not_claimed_ready`: no longer asserts the literal repo list; asserts the `.gitmodules` phrase and that `Astra`/`WeveNova`/`MODISService` are absent. Consequence of adaptation (b). |
| `tests/test_plugin_root.py` | `89b82fa922a1ab3380278881cba718898b8f20d8c59391864c2c996a7a4e46b3` | New: covers the `--root is required` refusal, explicit-root validation, and the SKILL.md path contract. |

All other files are byte-identical to the source.

## Test run of the imported copy

WSL, same toolchain, copied to `/tmp/rw`:

```
$ python -m pytest -q skills/review-setup/tests
149 passed, 43 subtests passed in 47.18s
```

`review-setup` tests are POSIX-only (`fcntl`, `setsid`); they are not run on Windows.

## 2026-09-08 — daemon-facing CLI contract (Slice 2)

Behavioral changes to the supplied `review-setup` source, beyond the packaging-only
adaptations recorded above. Every existing behaviour is preserved: no exit code changed,
no workspace layout changed, no locking or atomic-write semantics changed, and human
(non-`--json`) output is byte-identical.

| File | Change |
|------|--------|
| `scripts/review_envelope.py` | **New.** The result envelope, the `--json` / `--run-id` flags and the run-id grammar, shared by both CLIs. |
| `scripts/review_provider.py` | **New.** The provider seam. `AdoProvider` delegates to the existing `parse_ado_url`, `canonical_ado_metadata_url`, `AdoClient`, `review_context.resolve_pr` and `review_setup.select_module` verbatim — no logic moved. `GitHubProvider` is a stub whose every method raises. |
| `scripts/review_common.py` | `ReviewError` gained optional keyword-only `effect_type` / `effect_detail`, so a specific refusal can be reported as a named envelope effect without matching on message text. Default behaviour is unchanged and both are ignored outside `--json`. |
| `scripts/review_setup.py` | Added `--json`, `--run-id`, `--provider {ado,github}`. `main()`'s mode dispatch moved into `_dispatch()` so every path — including an uncaught exception — still prints one envelope. `setup_run()` gained a `provider=` parameter (default `AdoProvider`) and calls the provider for client construction, `resolve_pr` and `select_module` instead of calling them directly. |
| `scripts/review_reset.py` | Added `--json` and `--run-id`; the existing summary is emitted as a `reset_complete` effect. |
| `scripts/review_repo.py` | Added `--run-id` (echoed when supplied, self-generated when absent) and `--skill-bundle <plugin>=<relpath>` on bootstrap/update, written to the manifest as `skillBundle`. `check` reports `skill_bundle_missing` and blocks when the key resolves to no `bundle.json`. The `--json` envelope is now a single line rather than indented; its keys are unchanged. |
| `bundle.json` | `review-setup` `entryPoints` gained `warm` and were reordered to `setup, status, warm, reset`. Version stays `0.1.0`. |
| `SKILL.md` (both skills), `README.md` | Document the envelope, the exit-code -> status mapping, `--run-id`, `--provider` and `--skill-bundle`. |
| `tests/test_envelope.py` (review-setup), `tests/test_skill_bundle.py` (review-repo) | **New.** Cover the envelope per mode and per exit code, the run-id grammar and echo, the GitHub stub creating nothing, and the manifest selection key. |

No pre-existing test was modified. `--provider github` is a deliberate visible refusal, not
an adapter; the GitHub implementation is future work.
