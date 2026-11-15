"""Disposable, real-Git fixture for manually exercising `review_git_prepare.prepare()`.

Builds, once per instance, a throwaway topology that mirrors the real NOVA
review-setup layout closely enough for `prepare()` to operate on it
unmodified:

- a bare "remote" simulating the ADO-hosted repository, seeded with a base
  commit and a movable ``refs/pull/<pr>/merge`` ref (the PR merge ref);
- a bare per-repository "source store" (matching ``SourcePool.store``'s
  shape), fetched from the remote;
- a real, non-bare "canonical root" checkout (matching ``ReviewState.root``'s
  shape) with a ``.gitmodules`` gitlink entry for the repository;
- a linked outer worktree at ``.worktrees/{Repo}-{slot}`` (``root_head``,
  detached);
- a linked nested worktree at ``repos/<Repo>`` inside it, backed by the
  source store (``SourcePool.slot_relative_path(slot)``'s layout).

This module never mocks Git: every operation is a real ``git`` subprocess
call (``subprocess.run(..., check=True)``), matching the disposable target
``prepare()`` itself will operate on. It is a reusable fixture-builder, not a
``unittest`` module -- Task 3 exercises it via a short, throwaway driver
script (see Step 5 of the task brief).

The fixture's own ``.gitmodules`` URL is a cosmetic, ADO-shaped string (only
ever parsed as metadata, e.g. by ``review_common.parse_ado_url`` /
``read_submodules``); the real, locally fetchable Git location a caller
should hand to ``prepare()`` as ``ResolvedPullRequest.remote_url`` is
``DisposableFixture.remote_url`` (a plain filesystem path to the bare
remote) -- the two are intentionally decoupled, exactly as a real ADO
remote's Git URL and its REST-facing organization URL are two different
things.
"""
from __future__ import annotations

import shutil
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path


class FixtureError(RuntimeError):
    """A fixture-construction or fixture-helper step failed."""


def _git(
    cwd: Path,
    *args: str,
    check: bool = True,
    env: dict | None = None,
) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        env=env,
    )
    if check and result.returncode:
        raise FixtureError(
            f"git {' '.join(args)} (cwd={cwd}) failed (exit {result.returncode}): "
            f"{result.stderr.strip()}"
        )
    return result


def _configure_identity(repo: Path) -> None:
    _git(repo, "config", "user.email", "fixture@example.invalid")
    _git(repo, "config", "user.name", "Disposable Fixture")
    _git(repo, "config", "commit.gpgsign", "false")


_OPERATION_MARKERS = (
    "MERGE_HEAD",
    "CHERRY_PICK_HEAD",
    "REVERT_HEAD",
    "rebase-merge",
    "rebase-apply",
    "index.lock",
)


@dataclass
class DisposableFixture:
    """One throwaway {remote, store, canonical root, outer+nested worktree}
    topology, rooted under ``base``. The caller owns ``base``'s lifetime
    (e.g. a ``tempfile.TemporaryDirectory()``) and is responsible for
    removing it; this class never deletes ``base`` itself."""

    base: Path
    repo_name: str = "Nova"
    org: str = "faketest-org"
    project: str = "FakeProject"
    pr: int = 101
    slot: int = 0

    remote: Path = field(init=False)
    store: Path = field(init=False)
    root: Path = field(init=False)
    outer: Path = field(init=False)
    nested: Path = field(init=False)
    merge_ref: str = field(init=False)
    root_head: str = field(init=False, default="")

    def __post_init__(self) -> None:
        self.base = Path(self.base)
        self.remote = self.base / "remote" / f"{self.repo_name}.git"
        self.store = self.base / "source-stores" / f"{self.repo_name.lower()}.git"
        self.root = self.base / "nova-reviews"
        self.outer = self.root / ".worktrees" / f"{self.repo_name}-{self.slot}"
        self.nested = self.outer / "repos" / self.repo_name
        self.merge_ref = f"refs/pull/{self.pr}/merge"
        self._store_baseline = ""

    @property
    def remote_url(self) -> str:
        """The real, locally fetchable Git location for
        ``ResolvedPullRequest.remote_url`` -- see the module docstring for
        why this is deliberately distinct from ``gitmodules_url``."""
        return str(self.remote)

    @property
    def gitmodules_url(self) -> str:
        return f"https://dev.azure.com/{self.org}/{self.project}/_git/{self.repo_name}"

    # -- construction --------------------------------------------------

    def build(self, *, seed_nested: bool = True) -> "DisposableFixture":
        self._build_remote()
        self._build_store()
        self._build_root()
        self._build_outer_worktree()
        if seed_nested:
            self._build_nested_worktree(self._store_baseline)
        return self

    def _build_remote(self) -> None:
        self.remote.parent.mkdir(parents=True, exist_ok=True)
        _git(self.remote.parent, "init", "--bare", "-q", str(self.remote))
        seed = self.base / "_seed"
        _git(self.base, "clone", "-q", str(self.remote), str(seed))
        _configure_identity(seed)
        (seed / "README.md").write_text("disposable fixture seed\n")
        _git(seed, "add", "README.md")
        _git(seed, "commit", "-q", "-m", "seed commit")
        _git(seed, "branch", "-M", "main")
        _git(seed, "push", "-q", "origin", "main")
        self.advance_pr(message="initial PR content")
        shutil.rmtree(seed)

    def _build_store(self) -> None:
        self.store.parent.mkdir(parents=True, exist_ok=True)
        _git(self.store.parent, "init", "--bare", "-q", str(self.store))
        _git(self.store, "remote", "add", "origin", str(self.remote))
        _git(self.store, "fetch", "-q", "--no-tags", "origin", "+refs/heads/main:refs/heads/main")
        self._store_baseline = _git(self.store, "rev-parse", "refs/heads/main").stdout.strip()

    def _build_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        _git(self.root, "init", "-q")
        _configure_identity(self.root)
        _git(
            self.root, "config", "--file", ".gitmodules",
            f"submodule.{self.repo_name}.path", f"repos/{self.repo_name}",
        )
        _git(
            self.root, "config", "--file", ".gitmodules",
            f"submodule.{self.repo_name}.url", self.gitmodules_url,
        )
        # A real gitlink (mode 160000) so checking out this commit creates the
        # empty placeholder directory an uninitialized submodule leaves
        # behind -- exactly what `_build_nested_worktree` turns into a linked
        # worktree of the bare store, mirroring the real topology.
        _git(
            self.root, "update-index", "--add", "--cacheinfo",
            f"160000,{self._store_baseline},repos/{self.repo_name}",
        )
        _git(self.root, "add", ".gitmodules")
        _git(self.root, "commit", "-q", "-m", "canonical root baseline")
        self.root_head = _git(self.root, "rev-parse", "HEAD").stdout.strip()

    def _build_outer_worktree(self) -> None:
        self.outer.parent.mkdir(parents=True, exist_ok=True)
        _git(self.root, "worktree", "add", "--detach", "-q", str(self.outer), self.root_head)

    def _build_nested_worktree(self, source_sha: str) -> None:
        _git(self.store, "worktree", "add", "--detach", "-q", str(self.nested), source_sha)

    # -- PR ref manipulation ---------------------------------------------

    def advance_pr(self, *, message: str = "advance") -> str:
        """Advance ``refs/pull/<pr>/merge`` on the remote to a brand-new
        commit (a later PR iteration / a same-PR rerun target). Returns the
        new commit SHA. Independent of any local clone or worktree state."""
        work = self.base / f"_advance-{uuid.uuid4().hex}"
        _git(self.base, "clone", "-q", str(self.remote), str(work))
        _configure_identity(work)
        try:
            if _git(work, "rev-parse", "--verify", "refs/remotes/origin/main", check=False).returncode == 0:
                _git(work, "checkout", "-q", "main")
            marker = work / f"pr-{self.pr}-{uuid.uuid4().hex}.txt"
            marker.write_text(message + "\n")
            _git(work, "add", marker.name)
            _git(work, "commit", "-q", "-m", message)
            sha = _git(work, "rev-parse", "HEAD").stdout.strip()
            _git(work, "push", "-q", "-f", "origin", f"HEAD:{self.merge_ref}")
        finally:
            shutil.rmtree(work)
        return sha

    def unreachable_merge_ref(self) -> None:
        """Point ``refs/pull/<pr>/merge`` at an object the remote never
        receives, so fetching it fails -- for the fetch-failure scenario."""
        phantom = "f" * 40  # syntactically valid, never a real object here
        _git(self.remote, "update-ref", self.merge_ref, phantom)

    # -- dirtying helpers -------------------------------------------------

    def dirty(
        self,
        repo: Path,
        *,
        staged: bool = True,
        unstaged: bool = True,
        untracked: bool = True,
        ignored: bool = True,
        stale_marker: bool = True,
    ) -> None:
        """Dirty ``repo`` (an outer or nested worktree) across every axis
        `prepare()` must discard: staged/unstaged/untracked/ignored content,
        plus a stale Git operation marker."""
        if staged:
            path = repo / "staged-dirt.txt"
            path.write_text("staged\n")
            _git(repo, "add", "staged-dirt.txt")
        if unstaged:
            candidates = [
                p for p in repo.iterdir()
                if p.is_file() and not p.name.startswith(".") and p.name != "staged-dirt.txt"
            ]
            target = candidates[0] if candidates else repo / "unstaged-seed.txt"
            if not candidates:
                target.write_text("seed\n")
                _git(repo, "add", target.name)
                _git(repo, "commit", "-q", "-m", "seed unstaged target")
            with target.open("a", encoding="utf-8") as stream:
                stream.write("unstaged dirt\n")
        if untracked:
            (repo / "untracked-dirt.txt").write_text("untracked\n")
        if ignored:
            (repo / ".gitignore").write_text("ignored-dirt.txt\n")
            (repo / "ignored-dirt.txt").write_text("ignored\n")
        if stale_marker:
            self._write_operation_marker(repo)

    def _write_operation_marker(self, repo: Path) -> None:
        resolved = _git(repo, "rev-parse", "--git-path", "MERGE_HEAD").stdout.strip()
        path = Path(resolved)
        if not path.is_absolute():
            path = repo / path
        path.write_text(("f" * 40) + "\n")

    # -- read-only observation helpers -------------------------------------

    def head(self, repo: Path) -> str:
        return _git(repo, "rev-parse", "HEAD").stdout.strip()

    def is_detached(self, repo: Path) -> bool:
        return _git(repo, "symbolic-ref", "-q", "HEAD", check=False).returncode != 0

    def status(self, repo: Path) -> str:
        return _git(repo, "status", "--porcelain", "--ignored=matching").stdout

    def has_operation_marker(self, repo: Path) -> bool:
        for name in _OPERATION_MARKERS:
            resolved = _git(repo, "rev-parse", "--git-path", name).stdout.strip()
            path = Path(resolved)
            if not path.is_absolute():
                path = repo / path
            if path.exists():
                return True
        return False
