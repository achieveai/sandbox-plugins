#!/usr/bin/env python3
"""Bootstrap, check and update the owned skeleton of a review repository.

Stdlib only, Python >= 3.10, POSIX and Windows safe (no fcntl, no os.link).

Owned skeleton (created when missing, never deleted, never overwritten):

    KnowledgeBase/.gitkeep
    PRs/.gitkeep
    DeveloperLearnings/.gitkeep
    repos/.gitkeep
    .agents/skills/.gitkeep
    review-workspace.manifest.json

Submodules passed as ``--repo <name>=<git-url>`` are *registered* in
``.gitmodules`` at ``repos/<name>`` and never cloned or materialized.

Operations and exit codes:

    plan / bootstrap --dry-run   0 report only, no mutation
    bootstrap                    0 created or no-op, 3 conflicting submodule url, 1 error
    check                        0 clean, 2 upgrade needed, 3 local edit conflicts, 1 error
    update                       0 applied or no-op, 3 refused on conflicts, 1 error

``--json`` prints the result envelope described in ``bundle.json``.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tempfile
import uuid

MANIFEST_NAME = "review-workspace.manifest.json"
STATE_DIR = ".review-workspace"
MANIFEST_SCHEMA_VERSION = 1
ENVELOPE_SCHEMA_VERSION = 1
STATUSES = ("completed", "no_op", "retryable", "blocked", "failed")
OWNED_DIRS = ("KnowledgeBase", "PRs", "DeveloperLearnings", "repos", ".agents/skills")
GITKEEP = ".gitkeep"
REPO_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

EXIT_OK, EXIT_ERROR, EXIT_UPGRADE, EXIT_CONFLICT = 0, 1, 2, 3


class RepoError(Exception):
    def __init__(self, message: str, code: int = EXIT_ERROR, status: str = "failed"):
        super().__init__(message)
        self.code = code
        self.status = status


# -- helpers -----------------------------------------------------------------

def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def plugin_version() -> str:
    """Version from the plugin manifest two levels above skills/; 'unknown' if absent."""
    manifest = Path(__file__).resolve().parents[3] / ".claude-plugin" / "plugin.json"
    try:
        return str(json.loads(manifest.read_text(encoding="utf-8")).get("version", "unknown"))
    except (OSError, ValueError):
        return "unknown"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def owned_entries() -> list[str]:
    """Owned paths in creation order; directories end with '/'."""
    entries: list[str] = []
    for directory in OWNED_DIRS:
        entries.append(directory + "/")
        entries.append(f"{directory}/{GITKEEP}")
    return entries


def owned_files() -> list[str]:
    return [entry for entry in owned_entries() if not entry.endswith("/")]


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_json(path: Path, value: dict) -> None:
    atomic_write_text(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RepoError(f"Cannot read {path.name}: {type(exc).__name__}") from None
    if not isinstance(value, dict):
        raise RepoError(f"{path.name} is not a JSON object.")
    return value


def parse_repo(spec: str) -> tuple[str, str]:
    name, sep, url = spec.partition("=")
    if not sep or not name or not url:
        raise RepoError(f"--repo must be <name>=<git-url>, got {spec!r}.")
    if not REPO_NAME.fullmatch(name) or name in (".", "..") or name.endswith("."):
        raise RepoError(f"Unsafe repository name {name!r}; use letters, digits, '.', '_' or '-'.")
    if any(ch.isspace() or ord(ch) < 32 for ch in url):
        raise RepoError(f"Repository URL for {name!r} contains whitespace or control characters.")
    return name, url


def git_config(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    command = ["git", "config", "-f", str(root / ".gitmodules"), *args]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError as exc:
        raise RepoError(f"git is required to manage .gitmodules ({type(exc).__name__}).") from None
    if check and result.returncode:
        raise RepoError(f"git config on .gitmodules failed (exit {result.returncode}).")
    return result


def gitmodules_lookup(root: Path, name: str) -> dict[str, str] | None:
    if not (root / ".gitmodules").is_file():
        return None
    path = git_config(root, "--get", f"submodule.{name}.path", check=False)
    url = git_config(root, "--get", f"submodule.{name}.url", check=False)
    if path.returncode and url.returncode:
        return None
    return {"path": path.stdout.strip(), "url": url.stdout.strip()}


# -- result envelope ---------------------------------------------------------

class Result:
    def __init__(self, operation: str):
        self.operation = operation
        self.run_id = utc_now().replace("-", "").replace(":", "") + "-" + uuid.uuid4().hex[:8]
        self.status = "completed"
        self.code = EXIT_OK
        self.artifacts: list[dict] = []
        self.effects: list[dict] = []
        self.messages: list[str] = []

    def effect(self, kind: str, path: str, detail: str = "") -> None:
        self.effects.append({"type": kind, "path": path, "detail": detail})

    def artifact(self, path: str, kind: str) -> None:
        self.artifacts.append({"path": path, "kind": kind})

    def envelope(self) -> dict:
        assert self.status in STATUSES
        return {
            "schemaVersion": ENVELOPE_SCHEMA_VERSION,
            "status": self.status,
            "runId": self.run_id,
            "operation": self.operation,
            "artifacts": self.artifacts,
            "effects": self.effects,
        }

    def text(self) -> str:
        lines = [f"{self.operation}: {self.status} (exit {self.code})"]
        for effect in self.effects:
            detail = f" -- {effect['detail']}" if effect["detail"] else ""
            lines.append(f"  {effect['type']:<28} {effect['path']}{detail}")
        for artifact in self.artifacts:
            lines.append(f"  artifact {artifact['kind']:<10} {artifact['path']}")
        if not self.effects and not self.artifacts:
            lines.append("  clean; nothing to do")
        lines.extend(f"  {message}" for message in self.messages)
        return "\n".join(lines)


# -- state inspection --------------------------------------------------------

def resolve_root(value: str) -> Path:
    root = Path(value).expanduser().absolute()
    if not root.is_dir():
        raise RepoError(f"--root does not exist or is not a directory: {root}")
    return root


def pending_rollbacks(root: Path) -> list[Path]:
    state = root / STATE_DIR
    if not state.is_dir():
        return []
    pending = []
    for path in sorted(state.glob("rollback-*.json")):
        try:
            if read_json(path).get("status") == "pending":
                pending.append(path)
        except RepoError:
            continue
    return pending


def load_manifest(root: Path) -> dict | None:
    path = root / MANIFEST_NAME
    if not path.is_file():
        return None
    manifest = read_json(path)
    if manifest.get("schemaVersion") != MANIFEST_SCHEMA_VERSION:
        raise RepoError(f"{MANIFEST_NAME} has unsupported schemaVersion {manifest.get('schemaVersion')!r}.")
    return manifest


def effective_manifest(root: Path) -> tuple[dict | None, Path | None]:
    """The manifest on disk, or the one journaled by an interrupted update."""
    pending = pending_rollbacks(root)
    if pending:
        journal = read_json(pending[-1])
        previous = journal.get("previousManifest")
        if isinstance(previous, dict) and previous.get("schemaVersion") == MANIFEST_SCHEMA_VERSION:
            return previous, pending[-1]
        return load_manifest(root), pending[-1]
    return load_manifest(root), None


def build_manifest(root: Path, created_at: str, submodules: list[str]) -> dict:
    hashes = {}
    for relative in owned_files():
        path = root / PurePosixPath(relative)
        hashes[relative] = sha256_file(path) if path.is_file() else None
    return {
        "schemaVersion": MANIFEST_SCHEMA_VERSION,
        "pluginVersion": plugin_version(),
        "createdAt": created_at,
        "updatedAt": utc_now(),
        "ownedPaths": owned_entries(),
        "fileHashes": {k: v for k, v in hashes.items() if v is not None},
        "registeredSubmodules": sorted(set(submodules)),
    }


def diff_against(root: Path, manifest: dict | None, result: Result) -> tuple[list[str], list[str]]:
    """Return (missing owned entries, conflicting owned files) for the current disk state."""
    missing: list[str] = []
    conflicts: list[str] = []
    known_hashes = (manifest or {}).get("fileHashes", {})
    if manifest is None:
        missing.append(MANIFEST_NAME)
    for entry in owned_entries():
        path = root / PurePosixPath(entry.rstrip("/"))
        if entry.endswith("/"):
            if not path.is_dir():
                missing.append(entry)
            continue
        if not path.is_file():
            missing.append(entry)
            continue
        expected = known_hashes.get(entry)
        if expected is not None and sha256_file(path) != expected:
            conflicts.append(entry)
    for entry in missing:
        result.effect("upgrade_needed", entry, "missing owned entry" if entry != MANIFEST_NAME else "run bootstrap")
    for entry in conflicts:
        result.effect("conflict", entry, "local edit -- refusing to overwrite")
    return missing, conflicts


# -- operations --------------------------------------------------------------

def apply_skeleton(root: Path, missing: list[str], result: Result) -> None:
    for entry in missing:
        if entry == MANIFEST_NAME:
            continue
        path = root / PurePosixPath(entry.rstrip("/"))
        if entry.endswith("/"):
            path.mkdir(parents=True, exist_ok=True)
            result.effect("created_dir", entry)
            result.artifact(entry, "dir")
        else:
            atomic_write_text(path, "")
            result.effect("created_file", entry)
            result.artifact(entry, "file")


def register_submodules(root: Path, repos: list[tuple[str, str]], result: Result, *, dry_run: bool) -> bool:
    """Register missing entries; return True if anything changed. Conflicts raise."""
    changed = False
    for name, url in repos:
        relative = f"repos/{name}"
        existing = gitmodules_lookup(root, name)
        if existing is not None:
            if existing["path"] == relative and existing["url"] == url:
                continue
            raise RepoError(
                f"submodule {name!r} is already registered at {existing['path']!r} with url {existing['url']!r}; "
                "existing .gitmodules entries are never rewritten.",
                EXIT_CONFLICT, "blocked",
            )
        if dry_run:
            result.effect("submodule_register", relative, f"{name} -> {url}")
            changed = True
            continue
        git_config(root, f"submodule.{name}.path", relative)
        git_config(root, f"submodule.{name}.url", url)
        result.effect("submodule_registered", relative, f"{name} -> {url}; not materialized")
        result.artifact(".gitmodules", "file")
        changed = True
    return changed


def registered_names(root: Path) -> list[str]:
    if not (root / ".gitmodules").is_file():
        return []
    listing = git_config(root, "--get-regexp", r"^submodule\..*\.path$", check=False)
    names = []
    for line in listing.stdout.splitlines():
        key = line.split(" ", 1)[0]
        names.append(key[len("submodule."):-len(".path")])
    return names


def op_plan(root: Path, repos: list[tuple[str, str]], result: Result) -> None:
    manifest, journal = effective_manifest(root)
    if journal is not None:
        result.effect("interrupted_update", journal.relative_to(root).as_posix(), "run update to recover")
    probe = Result("probe")
    missing, conflicts = diff_against(root, manifest, probe)
    for entry in missing:
        if entry == MANIFEST_NAME:
            result.effect("write_manifest", MANIFEST_NAME)
        elif entry.endswith("/"):
            result.effect("create_dir", entry)
        else:
            result.effect("create_file", entry)
    for entry in conflicts:
        result.effect("conflict", entry, "local edit -- bootstrap leaves it alone")
    register_submodules(root, repos, result, dry_run=True)
    if not result.effects:
        result.status = "no_op"


def op_bootstrap(root: Path, repos: list[tuple[str, str]], result: Result) -> None:
    manifest, journal = effective_manifest(root)
    probe = Result("probe")
    missing, _conflicts = diff_against(root, manifest, probe)
    # Bootstrap never overwrites: conflicting owned files are left as they are.
    changed = register_submodules(root, repos, result, dry_run=False)
    apply_skeleton(root, missing, result)
    names = sorted(set(registered_names(root)) | {name for name, _ in repos})
    created_at = (manifest or {}).get("createdAt") or utc_now()
    new_manifest = build_manifest(root, created_at, names)
    if manifest is None or missing or changed or manifest.get("registeredSubmodules") != names \
            or manifest.get("pluginVersion") != new_manifest["pluginVersion"]:
        write_json(root / MANIFEST_NAME, new_manifest)
        result.artifact(MANIFEST_NAME, "manifest")
        if manifest is None:
            result.effect("wrote_manifest", MANIFEST_NAME)
    if journal is not None:
        complete_rollback(journal)
        result.effect("recovered_interrupted_update", journal.relative_to(root).as_posix())
    if not result.effects:
        result.status = "no_op"


def op_check(root: Path, result: Result) -> None:
    manifest, journal = effective_manifest(root)
    if journal is not None:
        result.effect("interrupted_update", journal.relative_to(root).as_posix(), "run update to recover")
    missing, conflicts = diff_against(root, manifest, result)
    if conflicts:
        result.status, result.code = "blocked", EXIT_CONFLICT
    elif missing or journal is not None:
        result.status, result.code = "completed", EXIT_UPGRADE
    else:
        result.status, result.code = "no_op", EXIT_OK
        result.messages.append("clean")


def write_rollback(root: Path, manifest: dict | None) -> Path:
    state = root / STATE_DIR
    state.mkdir(parents=True, exist_ok=True)
    stamp = utc_now().replace("-", "").replace(":", "")
    path = state / f"rollback-{stamp}-{uuid.uuid4().hex[:6]}.json"
    previous = {}
    for relative in owned_files():
        file_path = root / PurePosixPath(relative)
        previous[relative] = sha256_file(file_path) if file_path.is_file() else None
    write_json(path, {
        "schemaVersion": 1,
        "status": "pending",
        "createdAt": utc_now(),
        "pluginVersion": plugin_version(),
        "previousManifest": manifest,
        "previousFileHashes": previous,
    })
    return path


def complete_rollback(path: Path) -> None:
    journal = read_json(path)
    journal["status"] = "completed"
    journal["completedAt"] = utc_now()
    write_json(path, journal)


def op_update(root: Path, result: Result) -> None:
    manifest, journal = effective_manifest(root)
    probe = Result("probe")
    missing, conflicts = diff_against(root, manifest, probe)
    if conflicts:
        result.effects.extend(probe.effects)
        result.status, result.code = "blocked", EXIT_CONFLICT
        result.messages.append("refusing to update: resolve local edits to owned files first")
        return
    if manifest is None and journal is None:
        result.effects.extend(probe.effects)
        result.status, result.code = "completed", EXIT_UPGRADE
        result.messages.append("no manifest: run bootstrap first")
        return
    if not missing and journal is None:
        result.status = "no_op"
        return
    rollback = journal if journal is not None else write_rollback(root, manifest)
    result.artifact(rollback.relative_to(root).as_posix(), "rollback")
    apply_skeleton(root, missing, result)
    names = registered_names(root)
    created_at = (manifest or {}).get("createdAt") or utc_now()
    write_json(root / MANIFEST_NAME, build_manifest(root, created_at, names))
    result.artifact(MANIFEST_NAME, "manifest")
    complete_rollback(rollback)
    if journal is not None:
        result.effect("recovered_interrupted_update", journal.relative_to(root).as_posix())
    result.status = "completed"


# -- CLI ---------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bootstrap, check, or update a review repository skeleton.")
    parser.add_argument("operation", choices=("plan", "bootstrap", "check", "update"))
    parser.add_argument("--root", required=True, help="Review repository root directory.")
    parser.add_argument("--repo", action="append", default=[], metavar="NAME=URL",
                        help="Register a source submodule at repos/NAME (bootstrap/plan only; repeatable).")
    parser.add_argument("--dry-run", action="store_true", help="With bootstrap: report only, no mutation.")
    parser.add_argument("--json", action="store_true", help="Emit the result envelope as JSON.")
    args = parser.parse_args(argv)

    operation = "plan" if args.operation == "plan" or (args.operation == "bootstrap" and args.dry_run) else args.operation
    result = Result(operation)
    try:
        if args.dry_run and args.operation not in ("plan", "bootstrap"):
            raise RepoError("--dry-run is valid only with plan or bootstrap.")
        if args.repo and args.operation not in ("plan", "bootstrap"):
            raise RepoError("--repo is valid only with plan or bootstrap.")
        repos = [parse_repo(spec) for spec in args.repo]
        if len({name for name, _ in repos}) != len(repos):
            raise RepoError("Duplicate --repo names.")
        root = resolve_root(args.root)
        if operation == "plan":
            op_plan(root, repos, result)
        elif operation == "bootstrap":
            op_bootstrap(root, repos, result)
        elif operation == "check":
            op_check(root, result)
        else:
            op_update(root, result)
    except RepoError as exc:
        result.status, result.code = exc.status, exc.code
        result.messages.append(str(exc))
        result.effect("error", "", str(exc))
        if not args.json:
            print(f"review-repo {operation} stopped: {exc}", file=sys.stderr)
    print(json.dumps(result.envelope(), indent=2) if args.json else result.text())
    return result.code


if __name__ == "__main__":
    sys.exit(main())
