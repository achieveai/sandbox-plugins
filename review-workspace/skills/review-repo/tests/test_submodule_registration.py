"""Registration must key on the submodule PATH, not on the .gitmodules section name.

A real review repository can already carry `[submodule "repos/X"] path = repos/X` -- the
section name is arbitrary and need not equal the short repo name. Keying on the name alone
appends a second section for the same path, which is ambiguous for git and for
review_common.read_submodules, and double-counts the repo in the manifest.
"""
from __future__ import annotations

import io
import json
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import review_repo  # noqa: E402

from contextlib import redirect_stdout  # noqa: E402

URL = "https://dev.azure.com/org/project/_git/MCQdbDEV"
OTHER_URL = "https://dev.azure.com/org/project/_git/Other"


def run(argv: list[str]) -> tuple[int, dict]:
    out = io.StringIO()
    with redirect_stdout(out):
        code = review_repo.main([*argv, "--json"])
    return code, json.loads(out.getvalue())


def manifest_of(root: Path) -> dict:
    return json.loads((root / review_repo.MANIFEST_NAME).read_text(encoding="utf-8"))


def effect_types(envelope: dict) -> list[str]:
    return [effect["type"] for effect in envelope["effects"]]


def seed_gitmodules(root: Path, section: str, path: str, url: str) -> bytes:
    """Write a .gitmodules whose section name is deliberately not the short repo name."""
    for key, value in (("path", path), ("url", url)):
        subprocess.run(
            ["git", "config", "-f", str(root / ".gitmodules"), f"submodule.{section}.{key}", value],
            check=True, capture_output=True,
        )
    return (root / ".gitmodules").read_bytes()


def section_count(root: Path, path: str) -> int:
    listing = subprocess.run(
        ["git", "config", "-f", str(root / ".gitmodules"), "--get-regexp", r"^submodule\..*\.path$"],
        capture_output=True, text=True, check=False,
    )
    return sum(1 for line in listing.stdout.splitlines() if line.split(" ", 1)[1] == path)


# -- the defect --------------------------------------------------------------

def test_existing_section_under_another_name_is_not_duplicated(tmp_path: Path):
    before = seed_gitmodules(tmp_path, "repos/MCQdbDEV", "repos/MCQdbDEV", URL)
    code, env = run(["bootstrap", "--root", str(tmp_path), "--repo", f"MCQdbDEV={URL}"])
    assert code == 0
    assert (tmp_path / ".gitmodules").read_bytes() == before
    assert section_count(tmp_path, "repos/MCQdbDEV") == 1
    assert manifest_of(tmp_path)["registeredSubmodules"] == ["repos/MCQdbDEV"]
    assert "submodule_already_registered" in effect_types(env)
    assert "submodule_registered" not in effect_types(env)


def test_url_mismatch_on_the_same_path_is_refused(tmp_path: Path):
    before = seed_gitmodules(tmp_path, "repos/MCQdbDEV", "repos/MCQdbDEV", URL)
    code, env = run(["bootstrap", "--root", str(tmp_path), "--repo", f"MCQdbDEV={OTHER_URL}"])
    assert code == review_repo.EXIT_CONFLICT
    assert env["status"] == "blocked"
    assert (tmp_path / ".gitmodules").read_bytes() == before
    assert not (tmp_path / review_repo.MANIFEST_NAME).exists()


def test_plan_reports_the_existing_registration_rather_than_a_new_one(tmp_path: Path):
    before = seed_gitmodules(tmp_path, "repos/MCQdbDEV", "repos/MCQdbDEV", URL)
    code, env = run(["plan", "--root", str(tmp_path), "--repo", f"MCQdbDEV={URL}"])
    assert code == 0
    assert "submodule_already_registered" in effect_types(env)
    assert "submodule_register" not in effect_types(env)
    assert (tmp_path / ".gitmodules").read_bytes() == before


def test_plan_refuses_a_url_mismatch_on_the_same_path(tmp_path: Path):
    seed_gitmodules(tmp_path, "repos/MCQdbDEV", "repos/MCQdbDEV", URL)
    code, env = run(["plan", "--root", str(tmp_path), "--repo", f"MCQdbDEV={OTHER_URL}"])
    assert code == review_repo.EXIT_CONFLICT
    assert env["status"] == "blocked"


# -- behaviour that must survive the fix -------------------------------------

def test_a_matching_section_named_for_the_repo_is_still_a_no_op(tmp_path: Path):
    before = seed_gitmodules(tmp_path, "MCQdbDEV", "repos/MCQdbDEV", URL)
    code, env = run(["bootstrap", "--root", str(tmp_path), "--repo", f"MCQdbDEV={URL}"])
    assert code == 0
    assert (tmp_path / ".gitmodules").read_bytes() == before
    assert manifest_of(tmp_path)["registeredSubmodules"] == ["MCQdbDEV"]


def test_a_section_named_for_the_repo_at_a_different_path_is_refused(tmp_path: Path):
    before = seed_gitmodules(tmp_path, "MCQdbDEV", "vendor/MCQdbDEV", URL)
    code, env = run(["bootstrap", "--root", str(tmp_path), "--repo", f"MCQdbDEV={URL}"])
    assert code == review_repo.EXIT_CONFLICT
    assert env["status"] == "blocked"
    assert (tmp_path / ".gitmodules").read_bytes() == before


def test_an_unrelated_repo_is_still_registered_normally(tmp_path: Path):
    seed_gitmodules(tmp_path, "repos/MCQdbDEV", "repos/MCQdbDEV", URL)
    code, env = run(["bootstrap", "--root", str(tmp_path), "--repo", f"Other={OTHER_URL}"])
    assert code == 0
    assert "submodule_registered" in effect_types(env)
    assert section_count(tmp_path, "repos/Other") == 1
    assert manifest_of(tmp_path)["registeredSubmodules"] == ["Other", "repos/MCQdbDEV"]


def test_a_second_repo_pointing_at_the_same_path_is_impossible(tmp_path: Path):
    """repos/<name> is derived from the name, so two names can never share a path."""
    run(["bootstrap", "--root", str(tmp_path), "--repo", f"MCQdbDEV={URL}"])
    assert section_count(tmp_path, "repos/MCQdbDEV") == 1
    code, _ = run(["bootstrap", "--root", str(tmp_path), "--repo", f"MCQdbDEV={URL}"])
    assert code == 0
    assert section_count(tmp_path, "repos/MCQdbDEV") == 1
