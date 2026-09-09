"""Run identity and the explicit skill-bundle selection key in the review-repo manifest.

The daemon selects a skill bundle from the manifest key alone; a bundle directory that is
merely present must never select. These tests pin the write, the validation grammar, and
the `check` refusal when the key names a path with no bundle.
"""
from __future__ import annotations

import io
import json
from pathlib import Path
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import review_repo  # noqa: E402

from contextlib import redirect_stdout  # noqa: E402

BUNDLE_PATH = ".agents/skills/review-workspace"
RUN_ID = "crd-run1-42-0"


def run(argv: list[str]) -> tuple[int, dict]:
    out = io.StringIO()
    with redirect_stdout(out):
        code = review_repo.main([*argv, "--json"])
    return code, json.loads(out.getvalue())


def manifest_of(root: Path) -> dict:
    return json.loads((root / review_repo.MANIFEST_NAME).read_text(encoding="utf-8"))


def place_bundle(root: Path, relative: str = BUNDLE_PATH) -> None:
    directory = root / relative
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "bundle.json").write_text('{"plugin": "review-workspace"}\n', encoding="utf-8")


def effect_types(envelope: dict) -> list[str]:
    return [effect["type"] for effect in envelope["effects"]]


# -- run identity ------------------------------------------------------------

def test_supplied_run_id_is_echoed_verbatim(tmp_path: Path):
    code, env = run(["bootstrap", "--root", str(tmp_path), "--run-id", RUN_ID])
    assert code == 0
    assert env["runId"] == RUN_ID


def test_run_id_is_self_generated_when_absent(tmp_path: Path):
    code, env = run(["bootstrap", "--root", str(tmp_path)])
    assert code == 0
    assert env["runId"] != RUN_ID
    assert review_repo.RUN_ID_PATTERN.fullmatch(env["runId"])


@pytest.mark.parametrize("value", ["", "-leading", "has space", "has/slash", "x" * 129])
def test_malformed_run_id_is_an_argparse_error(tmp_path: Path, value: str):
    with pytest.raises(SystemExit) as caught:
        run(["bootstrap", "--root", str(tmp_path), "--run-id", value])
    assert caught.value.code == 2


# -- skill bundle: write -----------------------------------------------------

def test_bootstrap_writes_the_skill_bundle_key(tmp_path: Path):
    code, env = run([
        "bootstrap", "--root", str(tmp_path), "--skill-bundle", f"review-workspace={BUNDLE_PATH}",
    ])
    assert code == 0
    assert manifest_of(tmp_path)["skillBundle"] == {
        "plugin": "review-workspace", "path": BUNDLE_PATH,
    }
    assert "skill_bundle" in effect_types(env)


def test_update_sets_and_preserves_the_skill_bundle_key(tmp_path: Path):
    run(["bootstrap", "--root", str(tmp_path)])
    assert "skillBundle" not in manifest_of(tmp_path)
    code, _ = run([
        "update", "--root", str(tmp_path), "--skill-bundle", f"review-workspace={BUNDLE_PATH}",
    ])
    assert code == 0
    assert manifest_of(tmp_path)["skillBundle"]["path"] == BUNDLE_PATH
    # A later update that does not name a bundle must not silently drop the selection.
    (tmp_path / "PRs" / ".gitkeep").unlink()
    code, _ = run(["update", "--root", str(tmp_path)])
    assert code == 0
    assert manifest_of(tmp_path)["skillBundle"]["path"] == BUNDLE_PATH


def test_a_present_bundle_directory_alone_never_selects(tmp_path: Path):
    place_bundle(tmp_path)
    code, _ = run(["bootstrap", "--root", str(tmp_path)])
    assert code == 0
    assert "skillBundle" not in manifest_of(tmp_path)


# -- skill bundle: validation ------------------------------------------------

@pytest.mark.parametrize("spec", [
    "review-workspace",                              # no '='
    "=.agents/skills/x",                             # empty plugin
    "review-workspace=",                             # empty path
    "review-workspace=/agents/skills/x",             # absolute
    "review-workspace=.agents/skills/../x",          # traversal
    "review-workspace=.agents\\skills\\x",           # backslash
    "review-workspace=skills/review-workspace",      # wrong first segment
    "review-workspace=.agents/plugins/x",            # wrong second segment
    "review-workspace=.agents/skills",               # names no bundle directory
    "review-workspace=.agents/skills//x",            # empty segment
    "..=.agents/skills/x",                           # unsafe plugin name
])
def test_malformed_skill_bundle_is_refused(tmp_path: Path, spec: str):
    code, env = run(["bootstrap", "--root", str(tmp_path), "--skill-bundle", spec])
    assert code == review_repo.EXIT_ERROR
    assert env["status"] == "failed"
    assert not (tmp_path / review_repo.MANIFEST_NAME).exists()


def test_claude_skills_prefix_is_accepted(tmp_path: Path):
    code, _ = run([
        "bootstrap", "--root", str(tmp_path),
        "--skill-bundle", "review-workspace=.claude/skills/review-workspace",
    ])
    assert code == 0
    assert manifest_of(tmp_path)["skillBundle"]["path"] == ".claude/skills/review-workspace"


def test_skill_bundle_is_rejected_outside_bootstrap_and_update(tmp_path: Path):
    run(["bootstrap", "--root", str(tmp_path)])
    code, env = run([
        "check", "--root", str(tmp_path), "--skill-bundle", f"review-workspace={BUNDLE_PATH}",
    ])
    assert code == review_repo.EXIT_ERROR
    assert env["status"] == "failed"


# -- skill bundle: check -----------------------------------------------------

def test_check_blocks_when_the_named_bundle_is_missing(tmp_path: Path):
    run(["bootstrap", "--root", str(tmp_path), "--skill-bundle", f"review-workspace={BUNDLE_PATH}"])
    code, env = run(["check", "--root", str(tmp_path)])
    assert code == review_repo.EXIT_CONFLICT
    assert env["status"] == "blocked"
    missing = next(e for e in env["effects"] if e["type"] == "skill_bundle_missing")
    assert missing["path"] == BUNDLE_PATH


def test_check_is_clean_when_the_named_bundle_exists(tmp_path: Path):
    run(["bootstrap", "--root", str(tmp_path), "--skill-bundle", f"review-workspace={BUNDLE_PATH}"])
    place_bundle(tmp_path)
    code, env = run(["check", "--root", str(tmp_path)])
    assert code == 0
    assert env["status"] == "no_op"
    assert "skill_bundle_missing" not in effect_types(env)


def test_check_without_a_skill_bundle_key_is_unaffected(tmp_path: Path):
    run(["bootstrap", "--root", str(tmp_path)])
    place_bundle(tmp_path)
    code, env = run(["check", "--root", str(tmp_path)])
    assert code == 0
    assert "skill_bundle_missing" not in effect_types(env)


def test_update_records_a_selection_before_the_bundle_is_committed(tmp_path: Path):
    """A key may be recorded ahead of the bundle landing; only `check` reports the gap."""
    run(["bootstrap", "--root", str(tmp_path)])
    code, _ = run([
        "update", "--root", str(tmp_path), "--skill-bundle", f"review-workspace={BUNDLE_PATH}",
    ])
    assert code == 0
    assert manifest_of(tmp_path)["skillBundle"]["path"] == BUNDLE_PATH
    code, env = run(["check", "--root", str(tmp_path)])
    assert (code, env["status"]) == (review_repo.EXIT_CONFLICT, "blocked")
    place_bundle(tmp_path)
    assert run(["check", "--root", str(tmp_path)])[0] == 0
