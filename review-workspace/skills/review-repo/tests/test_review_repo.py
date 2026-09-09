"""Tests for the review-repo bootstrap/check/update skill script (Windows + POSIX)."""
from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
SCRIPT = SCRIPTS / "review_repo.py"
sys.path.insert(0, str(SCRIPTS))

import review_repo  # noqa: E402

OWNED_DIRS = ["KnowledgeBase", "PRs", "DeveloperLearnings", "repos", ".agents/skills"]
EMPTY_SHA = hashlib.sha256(b"").hexdigest()


def run(argv: list[str]) -> tuple[int, dict]:
    """Invoke main() in-process with --json and parse the result envelope."""
    out = io.StringIO()
    with redirect_stdout(out):
        code = review_repo.main([*argv, "--json"])
    return code, json.loads(out.getvalue())


def gitmodules_get(root: Path, key: str) -> str | None:
    result = subprocess.run(
        ["git", "config", "-f", str(root / ".gitmodules"), "--get", key],
        capture_output=True, text=True, check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def load_manifest(root: Path) -> dict:
    return json.loads((root / review_repo.MANIFEST_NAME).read_text(encoding="utf-8"))


# -- bootstrap ---------------------------------------------------------------

def test_bootstrap_empty_root_creates_skeleton_and_manifest(tmp_path: Path):
    code, env = run(["bootstrap", "--root", str(tmp_path)])
    assert code == 0
    assert env["schemaVersion"] == 1
    assert env["status"] == "completed"
    assert env["operation"] == "bootstrap"
    assert env["runId"]
    for rel in OWNED_DIRS:
        assert (tmp_path / rel).is_dir(), rel
        assert (tmp_path / rel / ".gitkeep").is_file(), rel
    manifest = load_manifest(tmp_path)
    assert manifest["schemaVersion"] == 1
    assert manifest["pluginVersion"] == review_repo.plugin_version()
    assert manifest["createdAt"].endswith("Z") or "+" in manifest["createdAt"]
    for rel in OWNED_DIRS:
        assert f"{rel}/" in manifest["ownedPaths"]
        assert f"{rel}/.gitkeep" in manifest["ownedPaths"]
        assert manifest["fileHashes"][f"{rel}/.gitkeep"] == EMPTY_SHA
    # every created path is reported as an artifact; the manifest is one of them
    artifact_paths = {a["path"] for a in env["artifacts"]}
    assert review_repo.MANIFEST_NAME in artifact_paths
    assert "PRs/.gitkeep" in artifact_paths


def test_bootstrap_preserves_existing_content_and_gitmodules_entries(tmp_path: Path):
    (tmp_path / "PRs" / "nova-42").mkdir(parents=True)
    note = tmp_path / "PRs" / "nova-42" / "PR_Setup_01.md"
    note.write_text("keep me", encoding="utf-8")
    (tmp_path / "KnowledgeBase").mkdir()
    kb = tmp_path / "KnowledgeBase" / "notes.md"
    kb.write_text("existing kb", encoding="utf-8")
    subprocess.run(["git", "config", "-f", str(tmp_path / ".gitmodules"),
                    "submodule.Existing.path", "repos/Existing"], check=True)
    subprocess.run(["git", "config", "-f", str(tmp_path / ".gitmodules"),
                    "submodule.Existing.url", "https://dev.azure.com/org/proj/_git/Existing"], check=True)

    code, env = run(["bootstrap", "--root", str(tmp_path), "--repo",
                     "Nova=https://dev.azure.com/org/proj/_git/Nova"])
    assert code == 0
    assert note.read_text(encoding="utf-8") == "keep me"
    assert kb.read_text(encoding="utf-8") == "existing kb"
    assert (tmp_path / "PRs" / ".gitkeep").is_file()
    assert gitmodules_get(tmp_path, "submodule.Existing.path") == "repos/Existing"
    assert gitmodules_get(tmp_path, "submodule.Existing.url") == "https://dev.azure.com/org/proj/_git/Existing"
    assert gitmodules_get(tmp_path, "submodule.Nova.path") == "repos/Nova"
    # unknown files are not owned
    manifest = load_manifest(tmp_path)
    assert "PRs/nova-42/PR_Setup_01.md" not in manifest["fileHashes"]
    assert ".gitmodules" not in manifest["fileHashes"]


def test_bootstrap_rerun_is_a_no_op(tmp_path: Path):
    code1, env1 = run(["bootstrap", "--root", str(tmp_path)])
    created = load_manifest(tmp_path)["createdAt"]
    code2, env2 = run(["bootstrap", "--root", str(tmp_path)])
    assert (code1, code2) == (0, 0)
    assert env1["status"] == "completed"
    assert env2["status"] == "no_op"
    assert env2["effects"] == []
    assert load_manifest(tmp_path)["createdAt"] == created
    code3, env3 = run(["check", "--root", str(tmp_path)])
    assert code3 == 0
    assert env3["status"] == "no_op"


def test_bootstrap_registers_submodule_without_materializing(tmp_path: Path):
    code, env = run(["bootstrap", "--root", str(tmp_path),
                     "--repo", "Nova=https://dev.azure.com/org/proj/_git/Nova",
                     "--repo", "NovaClient=https://dev.azure.com/org/proj/_git/NovaClient"])
    assert code == 0
    assert not (tmp_path / "repos" / "Nova").exists()
    assert not (tmp_path / "repos" / "NovaClient").exists()
    assert gitmodules_get(tmp_path, "submodule.Nova.path") == "repos/Nova"
    assert gitmodules_get(tmp_path, "submodule.Nova.url") == "https://dev.azure.com/org/proj/_git/Nova"
    assert gitmodules_get(tmp_path, "submodule.NovaClient.path") == "repos/NovaClient"
    kinds = [e["type"] for e in env["effects"]]
    assert kinds.count("submodule_registered") == 2
    # registering again is idempotent and leaves a single entry
    code, env = run(["bootstrap", "--root", str(tmp_path),
                     "--repo", "Nova=https://dev.azure.com/org/proj/_git/Nova"])
    assert code == 0
    assert env["status"] == "no_op"
    text = (tmp_path / ".gitmodules").read_text(encoding="utf-8")
    assert text.count('[submodule "Nova"]') == 1


def test_bootstrap_rejects_conflicting_repo_registration(tmp_path: Path):
    run(["bootstrap", "--root", str(tmp_path), "--repo", "Nova=https://dev.azure.com/org/proj/_git/Nova"])
    code, env = run(["bootstrap", "--root", str(tmp_path), "--repo", "Nova=https://dev.azure.com/org/other/_git/Nova"])
    assert code == 3
    assert env["status"] == "blocked"
    assert gitmodules_get(tmp_path, "submodule.Nova.url") == "https://dev.azure.com/org/proj/_git/Nova"


@pytest.mark.parametrize("bad", ["Nova", "=https://x", "../evil=https://x", "a b=https://x", "Nova=https://x y"])
def test_bootstrap_rejects_malformed_repo_arguments(tmp_path: Path, bad: str):
    code, env = run(["bootstrap", "--root", str(tmp_path), "--repo", bad])
    assert code == 1
    assert env["status"] == "failed"
    assert not (tmp_path / "PRs").exists()


# -- plan / dry-run ----------------------------------------------------------

def test_plan_reports_without_mutation(tmp_path: Path):
    code, env = run(["plan", "--root", str(tmp_path), "--repo", "Nova=https://dev.azure.com/org/proj/_git/Nova"])
    assert code == 0
    assert env["operation"] == "plan"
    assert env["status"] == "completed"
    assert list(tmp_path.iterdir()) == []
    planned = {(e["type"], e["path"]) for e in env["effects"]}
    assert ("create_dir", "PRs/") in planned
    assert ("create_file", "PRs/.gitkeep") in planned
    assert ("submodule_register", "repos/Nova") in planned
    assert ("write_manifest", review_repo.MANIFEST_NAME) in planned
    # bootstrap --dry-run is the same thing
    code, env2 = run(["bootstrap", "--dry-run", "--root", str(tmp_path)])
    assert code == 0
    assert list(tmp_path.iterdir()) == []
    assert env2["operation"] == "plan"


def test_plan_on_bootstrapped_root_is_no_op(tmp_path: Path):
    run(["bootstrap", "--root", str(tmp_path)])
    code, env = run(["plan", "--root", str(tmp_path)])
    assert code == 0
    assert env["status"] == "no_op"


# -- check -------------------------------------------------------------------

def test_check_reports_upgrade_needed_for_missing_owned_entries(tmp_path: Path):
    run(["bootstrap", "--root", str(tmp_path)])
    (tmp_path / "PRs" / ".gitkeep").unlink()
    (tmp_path / "DeveloperLearnings" / ".gitkeep").unlink()
    (tmp_path / "DeveloperLearnings").rmdir()
    code, env = run(["check", "--root", str(tmp_path)])
    assert code == 2
    assert env["status"] == "completed"
    missing = {e["path"] for e in env["effects"] if e["type"] == "upgrade_needed"}
    assert missing == {"PRs/.gitkeep", "DeveloperLearnings/", "DeveloperLearnings/.gitkeep"}


def test_check_reports_conflicts_for_locally_edited_owned_files(tmp_path: Path):
    run(["bootstrap", "--root", str(tmp_path)])
    (tmp_path / "PRs" / ".gitkeep").write_text("edited locally\n", encoding="utf-8")
    (tmp_path / "PRs" / "unknown.md").write_text("ignored\n", encoding="utf-8")
    code, env = run(["check", "--root", str(tmp_path)])
    assert code == 3
    assert env["status"] == "blocked"
    conflicts = [e for e in env["effects"] if e["type"] == "conflict"]
    assert [c["path"] for c in conflicts] == ["PRs/.gitkeep"]
    assert "refusing to overwrite" in conflicts[0]["detail"]
    assert all(e["path"] != "PRs/unknown.md" for e in env["effects"])


def test_check_without_manifest_needs_bootstrap(tmp_path: Path):
    code, env = run(["check", "--root", str(tmp_path)])
    assert code == 2
    assert any(e["type"] == "upgrade_needed" and e["path"] == review_repo.MANIFEST_NAME for e in env["effects"])


def test_check_rejects_missing_root(tmp_path: Path):
    code, env = run(["check", "--root", str(tmp_path / "nope")])
    assert code == 1
    assert env["status"] == "failed"


# -- update ------------------------------------------------------------------

def test_update_applies_missing_entries_and_writes_rollback(tmp_path: Path):
    run(["bootstrap", "--root", str(tmp_path)])
    before = load_manifest(tmp_path)
    (tmp_path / "PRs" / ".gitkeep").unlink()
    code, env = run(["update", "--root", str(tmp_path)])
    assert code == 0
    assert env["status"] == "completed"
    assert (tmp_path / "PRs" / ".gitkeep").is_file()
    rollbacks = sorted((tmp_path / review_repo.STATE_DIR).glob("rollback-*.json"))
    assert len(rollbacks) == 1
    rollback = json.loads(rollbacks[0].read_text(encoding="utf-8"))
    assert rollback["status"] == "completed"
    assert rollback["previousManifest"]["fileHashes"] == before["fileHashes"]
    assert rollback["previousFileHashes"]["PRs/.gitkeep"] is None  # was missing before update
    assert rollback["previousFileHashes"]["KnowledgeBase/.gitkeep"] == EMPTY_SHA
    code, env = run(["check", "--root", str(tmp_path)])
    assert code == 0


def test_update_is_no_op_on_clean_root(tmp_path: Path):
    run(["bootstrap", "--root", str(tmp_path)])
    code, env = run(["update", "--root", str(tmp_path)])
    assert code == 0
    assert env["status"] == "no_op"
    assert not list((tmp_path / review_repo.STATE_DIR).glob("rollback-*.json")) if (tmp_path / review_repo.STATE_DIR).exists() else True


def test_update_refuses_on_conflict_without_touching_anything(tmp_path: Path):
    run(["bootstrap", "--root", str(tmp_path)])
    edited = tmp_path / "PRs" / ".gitkeep"
    edited.write_text("edited locally\n", encoding="utf-8")
    (tmp_path / "repos" / ".gitkeep").unlink()  # also an upgrade candidate
    code, env = run(["update", "--root", str(tmp_path)])
    assert code == 3
    assert env["status"] == "blocked"
    assert edited.read_text(encoding="utf-8") == "edited locally\n"
    assert not (tmp_path / "repos" / ".gitkeep").exists()
    assert not (tmp_path / review_repo.STATE_DIR).exists()


def test_update_recovers_from_interrupted_update(tmp_path: Path):
    run(["bootstrap", "--root", str(tmp_path)])
    manifest = load_manifest(tmp_path)
    # Simulate an interrupted update: rollback journal written, entry not applied,
    # manifest possibly not rewritten.
    (tmp_path / "PRs" / ".gitkeep").unlink()
    state = tmp_path / review_repo.STATE_DIR
    state.mkdir()
    pending = state / "rollback-20260101T000000Z.json"
    pending.write_text(json.dumps({
        "schemaVersion": 1, "status": "pending", "createdAt": "2026-01-01T00:00:00Z",
        "pluginVersion": manifest["pluginVersion"], "previousManifest": manifest,
        "previousFileHashes": {p: (EMPTY_SHA if p != "PRs/.gitkeep" else None) for p in manifest["fileHashes"]},
    }), encoding="utf-8")
    (tmp_path / review_repo.MANIFEST_NAME).unlink()  # manifest write never happened

    code, env = run(["check", "--root", str(tmp_path)])
    assert code == 2
    assert any(e["type"] == "interrupted_update" for e in env["effects"])

    code, env = run(["update", "--root", str(tmp_path)])
    assert code == 0
    assert env["status"] == "completed"
    assert any(e["type"] == "recovered_interrupted_update" for e in env["effects"])
    assert (tmp_path / "PRs" / ".gitkeep").is_file()
    assert load_manifest(tmp_path)["fileHashes"] == manifest["fileHashes"]
    assert json.loads(pending.read_text(encoding="utf-8"))["status"] == "completed"
    code, env = run(["check", "--root", str(tmp_path)])
    assert code == 0


# -- envelope / CLI ----------------------------------------------------------

def test_cli_subprocess_emits_envelope_and_exit_code(tmp_path: Path):
    result = subprocess.run([sys.executable, str(SCRIPT), "bootstrap", "--root", str(tmp_path), "--json"],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    env = json.loads(result.stdout)
    assert set(env) == {"schemaVersion", "status", "runId", "operation", "artifacts", "effects"}
    result = subprocess.run([sys.executable, str(SCRIPT), "check", "--root", str(tmp_path)],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0
    assert "clean" in result.stdout.lower()


def test_envelope_statuses_match_bundle_contract():
    bundle = json.loads((SCRIPTS.parents[2] / "bundle.json").read_text(encoding="utf-8"))
    assert set(review_repo.STATUSES) == set(bundle["resultEnvelope"]["statuses"])
    assert bundle["resultEnvelope"]["fields"] == ["status", "runId", "operation", "artifacts", "effects"]
    skill = next(s for s in bundle["skills"] if s["id"] == "review-repo")
    for entry in skill["entryPoints"].values():
        assert (SCRIPTS.parents[2] / entry.split()[0]).is_file()
    assert bundle["version"] == review_repo.plugin_version()
