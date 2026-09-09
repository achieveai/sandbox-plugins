#!/usr/bin/env python3
"""Prepare a pinned, isolated NOVA review and collect deterministic evidence."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
import uuid

from review_common import (AdoClient, ReviewError, ReviewState, SourcePool, atomic_json, bounded_fetch,
                           cleanup_worktree, inspect_cleanup_tree, current_ref,
                           discover_root, ensure_repository, file_hash, git_status, parse_ado_url,
                           preflight, read_submodules, repo_slug, run_git, run_git_read, safe_path, utc_now,
                           validate_sha, worktree_rows, write_new_json, write_new_text)


def _has_commit(source: Path, sha: str) -> bool:
    return run_git(source, ["cat-file", "-e", f"{validate_sha(sha)}^{{commit}}"], check=False).returncode == 0


def _snapshot_shas(snapshot: dict) -> list[str]:
    return list(
        dict.fromkeys(
            validate_sha(snapshot[key])
            for key in ("head_sha", "target_sha", "base_sha")
            if snapshot.get(key)
        )
    )


def _fetch_failure(label: str, result) -> str:
    # Report only recognized diagnostic categories, never remote-supplied text:
    # stderr may contain credentials, headers, or signed URLs.
    text = result.stderr.lower()
    categories = (
        ("authentication", ("authentication failed", "could not read username", "401", "403")),
        ("remote-ref-unavailable", ("couldn't find remote ref", "not our ref", "unadvertised object")),
        ("repository-access", ("repository not found", "does not appear to be a git repository", "tf401019")),
        ("network", ("could not resolve host", "failed to connect", "connection reset", "timed out")),
        ("tls", ("certificate", "ssl", "tls")),
        ("shallow-history", ("shallow", "unshallow", "deepen")),
        ("local-lock", ("unable to create", "file exists", "cannot lock ref")),
    )
    category = next((name for name, tokens in categories if any(token in text for token in tokens)), "unclassified")
    return f"{label}: exit {result.returncode}, {category}"


def _fetch_exact_objects(source: Path, snapshot: dict, *, depth: int, max_depth: int = 1000000) -> None:
    if not isinstance(depth, int) or isinstance(depth, bool) or depth < 1:
        raise ReviewError("Fetch depth must be a positive integer.")
    shas = _snapshot_shas(snapshot)
    missing = [sha for sha in shas if not _has_commit(source, sha)]
    if not missing:
        return
    result = bounded_fetch(source, "origin", missing, depth=depth, max_depth=max_depth)
    failures = []
    if result.returncode:
        failures.append(_fetch_failure("exact-objects", result))
        refs = [snapshot["source_ref"], snapshot["target_ref"], f"refs/pull/{snapshot['pr_number']}/merge"]
        for label, ref in zip(("source-ref", "target-ref", "merge-ref"), refs):
            if not ref.startswith("refs/") or run_git(source, ["check-ref-format", ref], check=False).returncode:
                raise ReviewError("Provider returned an invalid source ref.")
            fallback = bounded_fetch(source, "origin", [ref], depth=depth, max_depth=max_depth)
            if fallback.returncode:
                failures.append(_fetch_failure(label, fallback))
    remaining = [
        f"{role}={validate_sha(snapshot[role + '_sha'])}"
        for role in ("head", "target", "base")
        if snapshot.get(role + "_sha") and not _has_commit(source, snapshot[role + "_sha"])
    ]
    if remaining:
        detail = "; ".join(failures) or "fetch returned success but required objects remain missing"
        raise ReviewError(
            "Exact PR snapshot objects could not be fetched; no moving branch tip was substituted. "
            f"Missing: {', '.join(remaining)}. Fetch diagnostics: {detail}."
        )


def _deepen_history(source: Path, snapshot: dict, *, depth: int, max_depth: int = 1000000) -> None:
    if not isinstance(depth, int) or isinstance(depth, bool) or depth < 1:
        raise ReviewError("History deepen depth must be a positive integer.")
    if run_git(source, ["rev-parse", "--is-shallow-repository"]).stdout.strip() != "true":
        raise ReviewError("History deepening was requested for a complete repository.")
    result = bounded_fetch(source, "origin", _snapshot_shas(snapshot), depth=depth, max_depth=max_depth, deepen=True)
    if result.returncode:
        refs = [snapshot["source_ref"], snapshot["target_ref"]]
        for ref in refs:
            if not ref.startswith("refs/") or run_git(source, ["check-ref-format", ref], check=False).returncode:
                raise ReviewError("Provider returned an invalid source ref.")
            fallback = bounded_fetch(source, "origin", [ref], depth=depth, max_depth=max_depth)
            if fallback.returncode:
                raise ReviewError("Bounded history deepening failed.")


# Compatibility alias for callers that fetch missing immutable endpoints.
def _fetch(source: Path, snapshot: dict, *, depth: int) -> None:
    _fetch_exact_objects(source, snapshot, depth=depth)


def _shallow_boundaries(source: Path) -> tuple[str, ...]:
    path = run_git(source, ["rev-parse", "--git-path", "shallow"]).stdout.strip()
    shallow = Path(path)
    if not shallow.is_absolute():
        shallow = source / shallow
    if not shallow.exists():
        return ()
    return tuple(sorted(line for line in shallow.read_text(encoding="ascii").splitlines() if line))


def resolve_base(source: Path, snapshot: dict, *, max_depth: int = 1000000) -> str:
    ensure_repository(source)
    head, target = validate_sha(snapshot["head_sha"]), validate_sha(snapshot["target_sha"])
    expected = validate_sha(snapshot["base_sha"]) if snapshot.get("base_sha") else None
    depth = 100
    while True:
        result = run_git(source, ["merge-base", "--all", head, target], check=False)
        if result.returncode == 0:
            bases = result.stdout.split()
            if expected:
                if expected not in bases:
                    shallow = run_git(source, ["rev-parse", "--is-shallow-repository"]).stdout.strip() == "true"
                    if not shallow:
                        raise ReviewError("Provider common ancestor does not match the pinned local comparison base.")
                else:
                    return expected
            elif len(bases) == 1:
                return validate_sha(bases[0])
            else:
                raise ReviewError("Multiple merge bases exist and no authoritative iteration base is available.")
        elif result.returncode != 1:
            raise ReviewError(f"Merge-base failed (exit {result.returncode}); this is not evidence of disconnected history.")
        if run_git(source, ["rev-parse", "--is-shallow-repository"]).stdout.strip() != "true":
            raise ReviewError("Pinned source and target have no common ancestor.")
        if depth > max_depth:
            raise ReviewError("History depth budget exhausted before the exact comparison base was established.")
        before = _shallow_boundaries(source)
        _deepen_history(source, snapshot, depth=depth, max_depth=max_depth)
        after = _shallow_boundaries(source)
        if before == after and after:
            raise ReviewError("Bounded history deepening made no progress.")
        depth *= 10


def _exact_snapshot_shas(snapshot: dict, *, require_base: bool = True) -> dict[str, str]:
    values = {
        role: validate_sha(snapshot[f"{role}_sha"])
        for role in ("head", "target")
    }
    if snapshot.get("base_sha"):
        values["base"] = validate_sha(snapshot["base_sha"])
    elif require_base:
        raise ReviewError("The exact comparison base has not been resolved yet.")
    return values


def _intended_retention_refs(manifest: dict) -> dict[str, str]:
    prefix = (
        f"refs/review-setup/retain/{manifest['repo_key']}/"
        f"{manifest['run_id']}"
    )
    return {
        role: f"{prefix}/{role}"
        for role in ("head", "base", "target", "baseline")
    }


def _retention_shas(manifest: dict) -> dict[str, str]:
    values = _exact_snapshot_shas(manifest["snapshot"], require_base=False)
    values["base"] = validate_sha(manifest["base_sha"])
    values["baseline"] = validate_sha(manifest["baseline_sha"])
    return values


def _verify_retention(source: Path, manifest: dict) -> None:
    intended = _intended_retention_refs(manifest)
    if manifest.get("retention_refs") != intended:
        raise ReviewError("Retention intent is not durably recorded.")
    for role, ref in intended.items():
        expected = _retention_shas(manifest)[role]
        actual = run_git_read(source, ["rev-parse", "--verify", ref], check=False)
        if actual.returncode or validate_sha(actual.stdout.strip()) != expected:
            raise ReviewError("A required retention ref does not resolve to its recorded commit.")
        if not _has_commit(source, expected):
            raise ReviewError("A required retained object is not a commit in the owned store.")


def _retain_snapshot(state: ReviewState, source: Path, manifest: dict) -> dict[str, str]:
    refs = _intended_retention_refs(manifest)
    manifest["retention_refs"] = refs
    manifest["phase"] = "retention-intent"
    manifest["status"] = "partial"
    state.save(manifest)
    pool = SourcePool(state, manifest["submodule"])
    pool.retain(manifest["run_id"], _retention_shas(manifest))
    _verify_retention(source, manifest)
    manifest["phase"] = "retention-verified"
    state.save(manifest)
    return refs


def validate_pool_slot(state: ReviewState, manifest: dict, *, require_pinned: bool,
                       require_retention: bool) -> tuple[Path, Path]:
    """Prove one pooled slot's complete ownership and content invariants."""
    if manifest.get("schema_version") != 2 or manifest.get("identity") != state.identity:
        raise ReviewError("Expected an owned schema-2 pool run.")
    module = manifest["submodule"]
    outer = safe_path(state.root, manifest["slot_path"])
    nested = safe_path(outer, module["path"])
    ensure_repository(outer, read_only=True)
    registrations = state.registered(read_only=True)
    if outer.resolve() not in registrations:
        raise ReviewError("Pool outer worktree is not registered in the owned root.")
    common = run_git_read(
        outer, ["rev-parse", "--path-format=absolute", "--git-common-dir"]
    ).stdout.strip()
    if Path(common).resolve() != state.common:
        raise ReviewError("Pool outer worktree has an unexpected common directory.")
    if current_ref(outer, read_only=True) != manifest["branch"]:
        raise ReviewError("Pool outer worktree is not on its recorded review branch.")
    ensure_repository(nested, read_only=True)
    pool = SourcePool(state, module)
    source_common = run_git_read(
        nested, ["rev-parse", "--path-format=absolute", "--git-common-dir"]
    ).stdout.strip()
    if Path(source_common).resolve() != pool.store.resolve():
        raise ReviewError("Pooled source has an unexpected common directory.")
    source_rows = worktree_rows(pool.store, read_only=True)
    if nested.resolve() not in {row["worktree"] for row in source_rows}:
        raise ReviewError("Pooled source is not registered in its owned store.")
    if current_ref(nested, read_only=True) is not None:
        raise ReviewError("Pooled source must remain detached.")
    if git_status(nested, read_only=True):
        raise ReviewError("Pooled source is not clean.")
    expected_head = (
        validate_sha(manifest["snapshot"]["head_sha"])
        if require_pinned
        else validate_sha(manifest["baseline_sha"])
    )
    actual_head = validate_sha(run_git_read(nested, ["rev-parse", "HEAD"]).stdout.strip())
    if actual_head != expected_head:
        raise ReviewError("Pooled source HEAD does not match its expected commit.")
    outer_status = git_status(outer, read_only=True)
    allowed_artifacts = manifest.get("artifacts", {})
    for code, path in outer_status:
        if path == module["path"] and code[0] == " ":
            continue
        expected_hash = allowed_artifacts.get(path)
        artifact = safe_path(outer, path)
        if (
            code == "??"
            and expected_hash
            and artifact.is_file()
            and file_hash(artifact) == expected_hash
        ):
            continue
        raise ReviewError("Pool outer worktree contains changes beyond its expected gitlink and generated artifacts.")
    if require_retention:
        _verify_retention(nested, manifest)
    return outer, nested


def prepare_source(state: ReviewState, manifest: dict, *, max_depth: int = 1000000) -> tuple[Path, str]:
    if manifest.get("schema_version") == 2:
        slot = safe_path(state.root, manifest["slot_path"])
        ensure_repository(slot)
        if current_ref(slot) != manifest["branch"]:
            raise ReviewError("Pool slot is not on its recorded review branch.")
        module = manifest["submodule"]
        source = safe_path(slot, module["path"])
        ensure_repository(source)
        pool = SourcePool(state, module)
        common = run_git(
            source, ["rev-parse", "--path-format=absolute", "--git-common-dir"]
        ).stdout.strip()
        if Path(common).resolve() != pool.store.resolve():
            raise ReviewError("Pooled source is bound to an unexpected common directory.")
        if git_status(source):
            raise ReviewError("Source worktree contains changes; refusing checkout.")
        manifest["phase"] = "fetch-intent"
        manifest["status"] = "partial"
        state.save(manifest)
        _fetch_exact_objects(source, manifest["snapshot"], depth=100, max_depth=max_depth)
        base = resolve_base(source, manifest["snapshot"], max_depth=max_depth)
        manifest["base_sha"] = base
        manifest["required_shas"]["base"] = base
        manifest["phase"] = "retention-intent"
        manifest["retention_refs"] = _intended_retention_refs(manifest)
        state.save(manifest)
        _retain_snapshot(state, source, manifest)
        manifest["phase"] = "pin-intent"
        state.save(manifest)
        run_git(source, ["checkout", "--detach", validate_sha(manifest["snapshot"]["head_sha"])])
        manifest["source_prepared"] = True
        manifest["source_baseline"] = {
            "head": manifest["baseline_sha"],
            "ref": manifest.get("baseline_ref"),
        }
        manifest["phase"] = "prepared"
        manifest["status"] = "prepared"
        state.save(manifest)
        validate_pool_slot(state, manifest, require_pinned=True, require_retention=True)
        return source, base

    slot = state.check_slot(manifest)
    module = manifest["submodule"]
    actual_modules = read_submodules(slot)
    matches = [value for value in actual_modules if value["path"] == module["path"]]
    if len(matches) != 1 or parse_ado_url(matches[0]["url"]) != parse_ado_url(module["url"]):
        raise ReviewError("Slot submodule configuration does not match the resolved PR repository.")
    source = safe_path(slot, module["path"])
    if not (source / ".git").exists():
        if source.exists() and any(source.iterdir()):
            raise ReviewError("Uninitialized legacy source contains files; refusing to overwrite.")
        manifest["status"] = "initializing"
        state.save(manifest)
        run_git(
            slot,
            [
                "-c",
                "protocol.file.allow=never",
                "submodule",
                "update",
                "--init",
                "--checkout",
                "--",
                module["path"],
            ],
        )
    ensure_repository(source)
    if git_status(source):
        raise ReviewError("Source worktree contains changes; refusing checkout.")
    origin = run_git(source, ["remote", "get-url", "origin"]).stdout.strip()
    if parse_ado_url(origin) != parse_ado_url(module["url"]):
        raise ReviewError("Existing source origin does not match the configured repository.")
    manifest["status"] = "fetching"
    state.save(manifest)
    _fetch(source, manifest["snapshot"], depth=100)
    base = resolve_base(source, manifest["snapshot"], max_depth=max_depth)
    manifest["base_sha"] = base
    manifest["status"] = "pinning"
    state.save(manifest)
    run_git(source, ["checkout", "--detach", validate_sha(manifest["snapshot"]["head_sha"])])
    manifest["source_prepared"] = True
    manifest["status"] = "prepared"
    state.save(manifest)
    return source, base


def select_module(client: AdoClient, snapshot: dict, modules: list[dict]) -> dict:
    repository = snapshot["repository"]
    matches = [module for module in modules if module["org_url"].lower() == client.org_url.lower()
               and module["project"].casefold() == repository["project"]["name"].casefold()
               and module["repository"].casefold() == repository["name"].casefold()]
    if len(matches) != 1:
        raise ReviewError("PR repository does not map uniquely to a configured source submodule.")
    if repository.get("url") and "/_git/" in repository["url"]:
        if parse_ado_url(repository["url"]) != parse_ado_url(matches[0]["url"]):
            raise ReviewError("PR repository URL disagrees with the configured submodule.")
    return matches[0]


def select_module_by_name(root: Path, repo: str) -> dict:
    if not isinstance(repo, str):
        raise ReviewError("Repository must be a canonical configured name.")
    matches = [
        module
        for module in read_submodules(root)
        if Path(module["path"]).name == repo
    ]
    if len(matches) != 1:
        raise ReviewError("Repository is not a unique canonical configured source name.")
    return matches[0]


def _pool_status(root: Path, module: dict, slot: int) -> dict:
    state = ReviewState(root, read_only=True)
    pool = SourcePool(state, module)
    name = pool.slot_name(slot)
    outer = safe_path(root, pool.slot_relative_path(slot))
    nested = safe_path(root, f"{pool.slot_relative_path(slot)}/{module['path']}")
    row = {
        "repository": pool.repo_name,
        "slot_number": slot,
        "slot": name,
        "slot_path": pool.slot_relative_path(slot),
        "status": "absent",
    }
    try:
        manifest = state.load_pool_run(pool.repo_key, slot)
    except ReviewError as exc:
        row["status"] = "blocked"
        row["reason"] = str(exc)
        return row
    if outer.is_symlink() or nested.is_symlink():
        row["status"] = "blocked"
        row["reason"] = "symlinked managed path"
        return row
    if not outer.exists():
        if manifest and manifest.get("status") not in ("archived", "discarded", "superseded"):
            row["status"] = "partial"
            row["reason"] = "owned reservation has no outer worktree yet"
        return row
    registered = state.registered(read_only=True)
    if outer.resolve() not in registered:
        row["status"] = "occupied"
        row["reason"] = "foreign outer directory"
        return row
    try:
        ensure_repository(outer, read_only=True)
        outer_status = git_status(outer, read_only=True)
        expected_delta = all(
            path == module["path"] and code[0] == " "
            for code, path in outer_status
        )
        branch = current_ref(outer, read_only=True)
        if manifest and manifest.get("status") not in ("archived", "discarded", "superseded", "ready"):
            if branch not in (None, manifest.get("branch")) or not expected_delta:
                row["status"] = "blocked"
                row["reason"] = "owned partial topology drifted from its manifest"
            else:
                row["status"] = "partial"
                row["reason"] = f"owned operation interrupted during {manifest.get('phase', manifest.get('status'))}"
            return row
        if branch is not None or not expected_delta:
            row["status"] = "occupied"
            row["reason"] = "active or unexpectedly dirty outer worktree"
            return row
        if not (nested / ".git").exists():
            row["status"] = "partial" if manifest else "blocked"
            row["reason"] = "nested source is not initialized"
            return row
        ensure_repository(nested, read_only=True)
        common = run_git_read(
            nested, ["rev-parse", "--path-format=absolute", "--git-common-dir"]
        ).stdout.strip()
        if (
            Path(common).resolve() != pool.store.resolve()
            or git_status(nested, read_only=True)
            or current_ref(nested, read_only=True) is not None
        ):
            row["status"] = "occupied"
            row["reason"] = "foreign, attached, or dirty nested source"
            return row
        row["status"] = "warm"
        row["head_sha"] = validate_sha(
            run_git_read(nested, ["rev-parse", "HEAD"]).stdout.strip()
        )
        row["baseline_sha"] = pool.cached_baseline_sha_read_only()
    except ReviewError as exc:
        row["status"] = "blocked"
        row["reason"] = str(exc)
    return row


def status_run(root: Path) -> list[dict]:
    ensure_repository(root, read_only=True)
    rows = []
    for module in read_submodules(root, read_only=True):
        for slot in range(SourcePool.SLOT_COUNT):
            rows.append(_pool_status(root, module, slot))
    return rows


POOL_DISK_RESERVE_BYTES = 2 * 1024 * 1024 * 1024


def warm_run(root: Path, repo: str, slot: int, *, dry_run: bool = False) -> dict:
    # Resolve repository identity first, before interpreting the repo-local slot.
    module = select_module_by_name(root, repo)
    numbers = SourcePool._validate_slots([slot])
    existing = _pool_status(root, module, numbers[0])
    if dry_run:
        existing["action"] = "reuse" if existing["status"] == "warm" else "would-warm"
        return existing
    if existing["status"] in ("occupied", "blocked", "partial"):
        return existing
    state = ReviewState(root)
    with state.lock():
        pool = SourcePool(state, module, reserve_bytes=POOL_DISK_RESERVE_BYTES)
        result = pool.warm(numbers)[0]
    return {"repository": pool.repo_name, "slot_number": numbers[0], "status": "warm", **result}


def warm_all_run(root: Path, *, dry_run: bool = False) -> list[dict]:
    rows = []
    halted = False
    for module in read_submodules(root):
        repo = Path(module["path"]).name
        for slot in range(SourcePool.SLOT_COUNT):
            if halted and not dry_run:
                row = _pool_status(root, module, slot)
                row["reason"] = "not attempted after an earlier warm failure"
                rows.append(row)
                continue
            try:
                row = warm_run(root, repo, slot, dry_run=dry_run)
            except ReviewError as exc:
                row = _pool_status(root, module, slot)
                row["status"] = "partial" if row["status"] != "occupied" else "occupied"
                row["reason"] = str(exc)
                halted = True
            rows.append(row)
    return rows


def _collect(client, snapshot, source, base, *, max_depth, max_work_items, max_commits):
    from review_context import collect_context
    report = collect_context(client, snapshot, source, base, max_work_items=max_work_items, max_commits=max_commits)
    # Shallow-history provenance is not silently attributed to a graft boundary.
    # Retry collection only after an actual history expansion, preserving the SHAs.
    depth = 100
    while any(item.get("provenance_status") == "partial-shallow-history" for item in report.get("files", [])) and depth <= max_depth:
        if run_git(source, ["rev-parse", "--is-shallow-repository"]).stdout.strip() != "true":
            break
        _deepen_history(source, snapshot, depth=depth, max_depth=max_depth)
        report = collect_context(client, snapshot, source, base, max_work_items=max_work_items, max_commits=max_commits)
        depth *= 10
    return report


def publish_report(state: ReviewState, manifest: dict, report: dict) -> None:
    from review_context import render_markdown
    slot = (
        safe_path(state.root, manifest["slot_path"])
        if manifest.get("schema_version") == 2
        else state.slot_path(manifest)
    )
    directory = safe_path(slot, manifest["pr_dir"])
    if "report_paths" not in manifest:
        existing = set()
        if directory.exists():
            for path in directory.iterdir():
                match = re.match(r"PR_Setup_(\d+)\.(json|md)$", path.name)
                if match:
                    existing.add(int(match[1]))
        number = max([manifest.get("report_sequence", 0), *existing]) + 1
        manifest["report_sequence"] = number
        manifest["report_paths"] = [f"{manifest['pr_dir']}/PR_Setup_{number:02d}.json",
                                    f"{manifest['pr_dir']}/PR_Setup_{number:02d}.md"]
    report.update({"schema_version": 1, "tool_version": "1.0.0", "run_id": manifest["run_id"],
                   "slot": manifest["slot"], "branch": manifest["branch"],
                   "worktree": manifest["slot_path"], "submodule": manifest["submodule"]["path"],
                   "base_sha": manifest["base_sha"], "snapshot": manifest["snapshot"]})
    # Keep a private complete rendering so a crash between JSON and Markdown can
    # resume publication without re-fetching a different external snapshot.
    pending = state.directory / "pending" / f"{manifest['run_id']}{'-' + manifest['report_generation'] if manifest.get('report_generation') else ''}.json"
    if pending.is_symlink() or pending.parent.is_symlink():
        raise ReviewError("Refusing symlink pending report.")
    if not pending.exists():
        write_new_json(pending, report)
    else:
        report = json.loads(pending.read_text(encoding="utf-8"))
    texts = [json.dumps(report, indent=2, sort_keys=True, ensure_ascii=True) + "\n", render_markdown(report)]
    import hashlib
    manifest["planned_artifacts"] = {relative: hashlib.sha256(text.encode("utf-8")).hexdigest()
                                     for relative, text in zip(manifest["report_paths"], texts)}
    manifest["status"] = "publishing"
    state.save(manifest)
    for relative, text in zip(manifest["report_paths"], texts):
        path = safe_path(slot, relative)
        expected = manifest["planned_artifacts"][relative]
        if path.exists():
            if file_hash(path) != expected:
                raise ReviewError("Interrupted report publication found changed output; refusing overwrite.")
        else:
            write_new_text(path, text)
        manifest["artifacts"][relative] = file_hash(path)
        state.save(manifest)
    manifest["complete"] = bool(report.get("complete"))
    manifest["stale"] = bool(report.get("stale"))
    if manifest.get("schema_version") == 2:
        manifest["phase"] = "ready-validation"
        manifest["status"] = "partial"
        state.save(manifest)
        validate_pool_slot(
            state,
            manifest,
            require_pinned=True,
            require_retention=True,
        )
    manifest["phase"] = "ready"
    manifest["status"] = "ready"
    state.save(manifest)


def _report_high_water(manifest: dict, outer: Path) -> int:
    numbers = [manifest.get("report_sequence", 0)]
    names = list(manifest.get("artifacts", {})) + list(manifest.get("report_paths", []))
    directory = safe_path(outer, manifest["pr_dir"])
    if directory.is_dir():
        names += [p.name for p in directory.iterdir()]
    for name in names:
        match = re.search(r"PR_Setup_(\d+)\.(?:json|md)$", name)
        if match:
            numbers.append(int(match[1]))
    return max(numbers)


def _cleanup_setup_slot(state: ReviewState, snapshot: dict, module: dict, requested: int | None) -> None:
    """Only explicit setup owns this destructive path; warm/status never call it."""
    pool = SourcePool(state, module)
    key = f"{repo_slug(snapshot['repository']['name'])}-{int(snapshot['pr_number'])}"
    runs = state.all_pool_runs()
    terminal = ("archived", "discarded", "superseded")
    existing = next((r for r in runs if r.get("key") == key and r.get("status") not in terminal), None)
    if existing and requested is not None and existing["slot_number"] != requested:
        raise ReviewError("PR already owns another slot; refusing cleanup of the selected slot.")
    number = existing["slot_number"] if existing else requested
    if number is None:
        occupied = {r["slot_number"] for r in runs if r.get("repo_key") == pool.repo_key
                    and r.get("status") not in terminal}
        number = next((n for n in range(SourcePool.SLOT_COUNT) if n not in occupied), None)
        if number is None:
            return  # Allocation will report exhaustion without touching another review.
    SourcePool._validate_slots([number])
    outer = safe_path(state.root, pool.slot_relative_path(number))
    source = safe_path(outer, module["path"])
    old = state.load_pool_run(pool.repo_key, number)
    if old and (old.get("reset_mode") or old.get("reset_phase")) and old.get("status") not in terminal:
        raise ReviewError("Archive/reset recovery is in progress; setup will not clear it.")
    if not outer.exists():
        return
    if not (source / ".git").exists() and old and old.get("phase") in (
        "reservation-intent", "warm-intent", "branch-intent"
    ):
        return  # Existing reservation recovery owns incomplete topology; nothing is cleaned.
    # Validate BOTH checkouts before deleting anything, independently of dirtiness.
    safe_path(state.common, pool.store.relative_to(state.common).as_posix())
    if (outer / ".git").is_symlink() or (source / ".git").is_symlink():
        raise ReviewError("Symlink Git administration is not an owned cleanup target.")
    ensure_repository(outer, read_only=True)
    if outer.resolve() not in state.registered(read_only=True):
        raise ReviewError("Selected cleanup slot is not an owned registered worktree.")
    if Path(run_git_read(outer, ["rev-parse", "--path-format=absolute", "--git-common-dir"]).stdout.strip()).resolve() != state.common:
        raise ReviewError("Selected cleanup slot belongs to another root.")
    ensure_repository(source, read_only=True)
    if Path(run_git_read(source, ["rev-parse", "--path-format=absolute", "--git-common-dir"]).stdout.strip()).resolve() != pool.store.resolve():
        raise ReviewError("Selected cleanup source belongs to another store.")
    if source.resolve() not in {r["worktree"] for r in worktree_rows(pool.store, read_only=True)}:
        raise ReviewError("Selected cleanup source is not registered.")
    if current_ref(source, read_only=True) is not None:
        raise ReviewError("Selected cleanup source must remain detached.")
    origin = run_git_read(pool.store, ["config", "--get", "remote.origin.url"]).stdout.strip()
    if parse_ado_url(origin) != parse_ado_url(module["url"]):
        raise ReviewError("Selected cleanup source origin changed.")
    journal_path = safe_path(state.directory, f"cleanup/{pool.repo_key}-{number}.json")
    journal = json.loads(journal_path.read_text()) if journal_path.exists() else None
    active = journal and journal.get("phase") != "done"
    if active:
        if journal.get("identity") != state.identity or journal.get("target") != snapshot["snapshot_fingerprint"] or journal.get("key") != key:
            raise ReviewError("Another setup cleanup is unfinished; resume that exact PR snapshot first.")
        if (old or {}).get("run_id") != journal.get("old_run_id"):
            raise ReviewError("Cleanup reservation changed outside this operation.")
    outer_head = validate_sha(run_git_read(outer, ["rev-parse", "HEAD"]).stdout.strip())
    source_head = validate_sha(run_git_read(source, ["rev-parse", "HEAD"]).stdout.strip())
    branch = current_ref(outer, read_only=True)
    if not active and old and old.get("phase") in (
        "reservation-intent", "warm-intent", "branch-intent"
    ) and branch is None:
        return  # Resume pre-branch allocation using its existing non-destructive checks.
    gitlink = run_git_read(outer, ["ls-tree", "HEAD", "--", module["path"]]).stdout
    if not gitlink.startswith("160000 commit "):
        raise ReviewError("Committed source path is not a managed gitlink.")
    if active:
        expected_heads = {journal["outer_head"]}
        expected_sources = {journal["source_head"]}
        expected_branches = {journal["branch"]}
        if journal["phase"] == "switching":
            expected_heads.add(journal["root_head"])
            expected_sources.add(journal["baseline"])
            expected_branches.add(None)
        if outer_head not in expected_heads or source_head not in expected_sources or branch not in expected_branches:
            raise ReviewError("Cleanup checkout changed outside its journal.")
    elif old and old.get("status") not in terminal:
        if old["slot_path"] != pool.slot_relative_path(number) or old["submodule"] != module or branch != old["branch"]:
            raise ReviewError("Selected cleanup slot does not match its recorded review.")
        if run_git_read(outer, ["merge-base", "--is-ancestor", old["branch_start"], outer_head], check=False).returncode:
            raise ReviewError("Selected review branch history changed unexpectedly.")
        if source_head not in {old.get("baseline_sha"), old["snapshot"]["head_sha"]}:
            raise ReviewError("Selected source HEAD changed outside its review.")
    elif branch is not None:
        raise ReviewError("Unreserved cleanup slot is on an unowned branch.")
    inventory = {"outer": inspect_cleanup_tree(outer, protected=source), "source": inspect_cleanup_tree(source)}
    # Committed .gitmodules must still match; working edits are intentionally discarded.
    configured = run_git_read(outer, ["show", "HEAD:.gitmodules"]).stdout
    root_configured = run_git_read(state.root, ["show", "HEAD:.gitmodules"]).stdout
    if configured != root_configured:
        raise ReviewError("Committed slot submodule configuration differs from the root.")
    switching = old and old.get("status") not in terminal and (
        old["key"] != key or old["snapshot"]["snapshot_fingerprint"] != snapshot["snapshot_fingerprint"]
    )
    if not active:
        baseline = pool.cached_baseline_sha()
        if baseline is None:
            raise ReviewError("Cleanup requires a verified cached baseline.")
        if (not old or old.get("status") in terminal) and source_head != baseline:
            raise ReviewError("Unreserved source is not at its verified warm baseline.")
        # Refuse branch collisions and incompatible committed layouts before deletion.
        target_branch = f"review/{key}"
        target_ref = f"refs/heads/{target_branch}"
        if run_git_read(state.root, ["show-ref", "--verify", "--quiet", target_ref], check=False).returncode == 0:
            target_config = run_git_read(state.root, ["show", f"{target_ref}:.gitmodules"], check=False)
            target_link = run_git_read(state.root, ["ls-tree", target_ref, "--", module["path"]]).stdout
            if target_config.returncode or target_config.stdout != root_configured or not target_link.startswith("160000 commit "):
                raise ReviewError("Requested review branch has an incompatible committed source layout.")
        if any(r.get("branch") == target_branch and r["worktree"] != outer.resolve()
               for r in worktree_rows(state.root, read_only=True)):
            raise ReviewError("Requested review branch is already checked out in another slot.")
        journal = {"identity": state.identity, "key": key, "target": snapshot["snapshot_fingerprint"],
                   "old_run_id": (old or {}).get("run_id"), "branch": branch,
                   "outer_head": outer_head, "source_head": source_head,
                   "root_head": validate_sha(run_git_read(state.root, ["rev-parse", "HEAD"]).stdout.strip()),
                   "baseline": baseline, "switching": bool(switching), "inventory": inventory,
                   "phase": "cleaning", "id": uuid.uuid4().hex,
                   "report_sequence": _report_high_water(old, outer) if old else 0}
        if old:
            history = safe_path(state.directory, f"setup-history/{journal['id']}.json")
            write_new_json(history, {**old, "preserved_branch_tip": outer_head,
                                    "report_sequence": journal["report_sequence"]})
        atomic_json(journal_path, journal)
    if journal["phase"] == "cleaning":
        cleanup_worktree(source, journal["source_head"])
        cleanup_worktree(outer, journal["outer_head"], protected=module["path"])
        if git_status(source) or any(path != module["path"] or code[0] != " " for code, path in git_status(outer)):
            raise ReviewError("Slot cleanup did not produce the expected clean checkouts.")
        journal["phase"] = "switching" if journal["switching"] else "cleaned"
        atomic_json(journal_path, journal)
    if journal["phase"] == "switching":
        # Existing committed branches and their retained objects remain untouched.
        run_git(outer, ["-c", "submodule.recurse=false", "checkout", "--detach", journal["root_head"]])
        run_git(source, ["checkout", "--detach", journal["baseline"]])
        old["status"] = "superseded"
        old["phase"] = "superseded"
        state.save(old)
    elif old and old.get("status") not in terminal:
        old["report_sequence"] = journal["report_sequence"]
        old["report_generation"] = journal["id"]
        old["branch_start"] = journal["outer_head"]
        for field in ("report_paths", "planned_artifacts", "complete", "stale"):
            old.pop(field, None)
        old["artifacts"] = {}
        old["status"] = "prepared" if old.get("source_prepared") else "reserved"
        old["phase"] = old["status"]
        state.save(old)
    journal["phase"] = "done"
    atomic_json(journal_path, journal)


def _finish_pool_reserve(state: ReviewState, manifest: dict) -> dict:
    pool = SourcePool(
        state,
        manifest["submodule"],
        reserve_bytes=POOL_DISK_RESERVE_BYTES,
    )
    status = _pool_status(state.root, manifest["submodule"], manifest["slot_number"])
    if status["status"] in ("occupied", "blocked"):
        raise ReviewError(
            f"Requested pool slot {manifest['slot_id']} is {status['status']}: "
            f"{status.get('reason', 'unsafe state')}."
        )
    outer = safe_path(state.root, manifest["slot_path"])
    if outer.is_dir() and current_ref(outer, read_only=True) == manifest["branch"]:
        # A prior attempt may already have attached the owned branch before
        # saving reserved, or the old retry path may have rewritten fetch-intent.
        # Validate that exact completed reservation instead of warming it again.
        checked_outer, source = validate_pool_slot(
            state, manifest, require_pinned=False, require_retention=False
        )
        if manifest["slot_path"] != pool.slot_relative_path(manifest["slot_number"]):
            raise ReviewError("Pool reservation path does not match its slot identity.")
        if run_git_read(checked_outer, ["rev-parse", "HEAD"]).stdout.strip() != manifest["branch_start"]:
            raise ReviewError("Review ref changed during pooled reservation.")
        if run_git_read(source, ["ls-files", "--others", "--ignored", "--exclude-standard", "-z"]).stdout:
            raise ReviewError("Source checkout has ignored local data; refusing reservation recovery.")
        manifest["phase"] = "reserved"
        manifest["status"] = "reserved"
        state.save(manifest)
        return manifest
    manifest["phase"] = "warm-intent"
    manifest["status"] = "partial"
    state.save(manifest)
    row = pool.warm([manifest["slot_number"]])[0]
    for field in ("root_head", "baseline_sha", "slot_path"):
        expected = manifest.get(field)
        actual = row[field]
        if expected is not None and expected != actual:
            raise ReviewError(f"Pool reservation {field} changed during resume.")
        manifest[field] = actual
    outer = safe_path(state.root, manifest["slot_path"])
    manifest["phase"] = "branch-intent"
    state.save(manifest)
    branch_ref = f"refs/heads/{manifest['branch']}"
    exists = run_git(
        state.root, ["show-ref", "--verify", "--quiet", branch_ref], check=False
    ).returncode == 0
    if exists:
        actual = validate_sha(run_git(state.root, ["rev-parse", branch_ref]).stdout.strip())
        if actual != manifest["branch_start"]:
            raise ReviewError("Review ref changed during pooled reservation.")
        if current_ref(outer) != manifest["branch"]:
            run_git(outer, ["checkout", manifest["branch"]])
    else:
        if current_ref(outer) is not None:
            raise ReviewError("Interrupted pooled reservation is attached to an unknown branch.")
        run_git(outer, ["checkout", "-b", manifest["branch"], manifest["branch_start"]])
    manifest["phase"] = "reserved"
    manifest["status"] = "reserved"
    state.save(manifest)
    return manifest


def _reserve_pool_run(
    state: ReviewState, snapshot: dict, module: dict, slot: int | None
) -> dict:
    pool = SourcePool(state, module, reserve_bytes=POOL_DISK_RESERVE_BYTES)
    requested = None if slot is None else SourcePool._validate_slots([slot])[0]
    key = f"{repo_slug(snapshot['repository']['name'])}-{int(snapshot['pr_number'])}"
    branch = f"review/{key}"
    if run_git(state.root, ["check-ref-format", "--branch", branch], check=False).returncode:
        raise ReviewError("Invalid review branch name.")
    runs = state.all_pool_runs()
    for old in runs:
        if old.get("key") != key or old.get("status") in ("archived", "discarded", "superseded"):
            continue
        if requested is not None and old["slot_number"] != requested:
            raise ReviewError(f"PR already owns slot {old['slot_id']}; reset it before choosing another slot.")
        if old["snapshot"].get("snapshot_fingerprint") != snapshot.get("snapshot_fingerprint"):
            raise ReviewError("PR snapshot changed; archive/reset the prior run before preparing a new snapshot.", 3)
        if old.get("reset_mode") or old.get("reset_phase"):
            raise ReviewError(
                f"Slot {old['slot_id']} has {old.get('reset_mode', 'reset')} recovery in progress "
                f"at phase {old.get('reset_phase', 'unknown')}; setup will not restart or clear it."
            )
        if old.get("status") in ("partial", "reserving", "reserved") and old.get("phase") in (
            "reservation-intent", "warm-intent", "branch-intent"
        ):
            return _finish_pool_reserve(state, old)
        if old.get("phase") == "fetch-intent":
            outer, _ = validate_pool_slot(state, old, require_pinned=False, require_retention=False)
            if run_git_read(outer, ["rev-parse", "HEAD"]).stdout.strip() != old["branch_start"]:
                raise ReviewError("Review ref changed after failed PR fetch.")
        return old
    occupied = {
        run["slot_number"]
        for run in runs
        if run.get("repo_key") == pool.repo_key
        and run.get("status") not in ("archived", "discarded", "superseded")
    }
    number = requested
    if number is None:
        number = next((candidate for candidate in range(SourcePool.SLOT_COUNT) if candidate not in occupied), None)
        if number is None:
            raise ReviewError(f"No free {pool.repo_name} review slot in range 0-5.")
    if number in occupied:
        raise ReviewError("Requested repository slot is reserved for another review.")
    status = _pool_status(state.root, module, number)
    if status["status"] in ("occupied", "blocked"):
        raise ReviewError(f"Requested pool slot {status['slot']} is {status['status']}: {status.get('reason', 'unsafe state')}.")
    root_head = validate_sha(run_git(state.root, ["rev-parse", "HEAD"]).stdout.strip())
    branch_result = run_git(
        state.root, ["rev-parse", "--verify", f"refs/heads/{branch}"], check=False
    )
    branch_start = (
        validate_sha(branch_result.stdout.strip())
        if branch_result.returncode == 0
        else root_head
    )
    sequence = 0
    history_dir = safe_path(state.directory, "setup-history")
    if history_dir.exists():
        for path in history_dir.glob("*.json"):
            if path.is_symlink():
                raise ReviewError("Refusing symlink setup history.")
            record = json.loads(path.read_text())
            if record.get("identity") == state.identity and record.get("key") == key:
                sequence = max(sequence, record.get("report_sequence", 0))
    manifest = {
        "report_sequence": sequence,
        "schema_version": 2,
        "identity": state.identity,
        "repo_key": pool.repo_key,
        "repo_name": pool.repo_name,
        "slot_number": number,
        "slot": pool.slot_name(number),
        "slot_id": pool.slot_name(number),
        "slot_path": pool.slot_relative_path(number),
        "key": key,
        "branch": branch,
        "branch_existed": branch_result.returncode == 0,
        "branch_start": branch_start,
        "root_head": root_head,
        "baseline_sha": None,
        "baseline_ref": f"refs/review-setup/baselines/{pool.repo_key}",
        "submodule": module,
        "snapshot": snapshot,
        "pr_dir": f"PRs/{key}",
        "status": "partial",
        "phase": "reservation-intent",
        "run_id": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8],
        "created_at": utc_now(),
        "artifacts": {},
        "baseline_artifacts": {},
        "source_prepared": False,
        "source_baseline": None,
        "retention_refs": {},
        "required_shas": _exact_snapshot_shas(snapshot, require_base=False),
    }
    state.save(manifest)
    return _finish_pool_reserve(state, manifest)


def setup_run(root: Path, pr_number: int, *, slot: int | None = None, org: str | None = None,
              iteration: int | None = None, max_depth: int = 1000000, max_work_items: int = 2000,
              max_commits: int = 500, client=None) -> dict:
    from review_context import resolve_pr
    if pr_number < 1 or max_depth < 1 or max_work_items < 1 or max_commits < 1:
        raise ReviewError("PR number and resource budgets must be positive.")
    modules = read_submodules(root)
    organizations = sorted({module["org_url"] for module in modules})
    if client is None:
        if org:
            org_url = org if org.startswith("https://") else f"https://dev.azure.com/{org}"
            client = AdoClient(org_url)
        elif len(organizations) == 1:
            client = AdoClient(organizations[0])
        else:
            raise ReviewError("More than one ADO organization is configured; supply --org.")
    snapshot = resolve_pr(client, pr_number, iteration)
    module = select_module(client, snapshot, modules)
    _exact_snapshot_shas(snapshot, require_base=False)
    state = ReviewState(root)
    with state.lock():
        key = f"{repo_slug(snapshot['repository']['name'])}-{int(snapshot['pr_number'])}"
        legacy = next(
            (
                run
                for run in state.all_runs()
                if run.get("key") == key
                and run.get("status") not in ("archived", "discarded", "superseded")
            ),
            None,
        )
        if legacy is not None:
            if slot is not None and legacy["slot"] != slot:
                raise ReviewError(
                    f"PR already owns legacy slot {legacy['slot']}; use its explicit legacy lifecycle."
                )
            if legacy["snapshot"].get("snapshot_fingerprint") != snapshot.get("snapshot_fingerprint"):
                raise ReviewError("PR snapshot changed; archive/reset the prior run before preparing a new snapshot.", 3)
            manifest = legacy
        else:
            _cleanup_setup_slot(state, snapshot, module, slot)
            manifest = _reserve_pool_run(state, snapshot, module, slot)
        if manifest["status"] == "ready":
            if manifest.get("schema_version") == 2:
                validate_pool_slot(
                    state,
                    manifest,
                    require_pinned=True,
                    require_retention=True,
                )
            return manifest
        if manifest["status"] == "publishing":
            pending = state.directory / "pending" / f"{manifest['run_id']}{'-' + manifest['report_generation'] if manifest.get('report_generation') else ''}.json"
            publish_report(state, manifest, json.loads(pending.read_text(encoding="utf-8")))
            return manifest
        source, base = prepare_source(state, manifest, max_depth=max_depth)
        report = _collect(client, snapshot, source, base, max_depth=max_depth,
                          max_work_items=max_work_items, max_commits=max_commits)
        current = resolve_pr(client, pr_number, iteration)
        stale = current["snapshot_fingerprint"] != snapshot["snapshot_fingerprint"]
        report["stale"] = stale
        if stale:
            report.setdefault("warnings", []).append("PR snapshot changed during collection; this report is pinned but stale.")
            report["complete"] = False
        publish_report(state, manifest, report)
        return manifest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pr_number", type=int, nargs="?")
    parser.add_argument("--slot", type=int)
    parser.add_argument("--repo")
    parser.add_argument("--org")
    parser.add_argument("--iteration", type=int)
    parser.add_argument("--root", help="Canonical review store root (normally inferred from this script).")
    parser.add_argument("--max-depth", type=int, default=1000000)
    parser.add_argument("--max-work-items", type=int, default=2000)
    parser.add_argument("--max-commits", type=int, default=500)
    parser.add_argument("--preflight", action="store_true", help="Check installed runtime tools without Git changes or network calls.")
    parser.add_argument("--status", action="store_true", help="Report all configured repository slots without mutation.")
    parser.add_argument("--warm", action="store_true", help="Warm one canonical repository slot.")
    parser.add_argument("--warm-all", action="store_true", help="Warm all configured repository slots sequentially.")
    parser.add_argument("--dry-run", action="store_true", help="Plan a warm operation without mutation.")
    args = parser.parse_args(argv)
    modes = sum(bool(value) for value in (args.preflight, args.status, args.warm, args.warm_all))
    if modes > 1:
        parser.error("--preflight, --status, --warm, and --warm-all are mutually exclusive")
    supplied = {
        "pr_number": args.pr_number is not None,
        "slot": args.slot is not None,
        "repo": args.repo is not None,
        "org": args.org is not None,
        "iteration": args.iteration is not None,
        "max_depth": args.max_depth != 1000000,
        "max_work_items": args.max_work_items != 2000,
        "max_commits": args.max_commits != 500,
        "dry_run": args.dry_run,
    }
    if args.preflight:
        allowed = set()
        mode_name = "--preflight"
    elif args.status:
        allowed = set()
        mode_name = "--status"
    elif args.warm:
        allowed = {"repo", "slot", "dry_run"}
        mode_name = "--warm"
        if not args.repo or args.slot is None:
            parser.error("--warm requires --repo and --slot")
    elif args.warm_all:
        allowed = {"dry_run"}
        mode_name = "--warm-all"
    else:
        allowed = {
            "pr_number", "slot", "org", "iteration", "max_depth",
            "max_work_items", "max_commits",
        }
        mode_name = "PR setup"
    invalid = sorted(name for name, present in supplied.items() if present and name not in allowed)
    if invalid:
        parser.error(f"{mode_name} does not accept: {', '.join('--' + name.replace('_', '-') for name in invalid)}")
    if args.dry_run and not (args.warm or args.warm_all):
        parser.error("--dry-run is valid only with --warm or --warm-all")
    try:
        if args.preflight:
            print(json.dumps(preflight(), indent=2))
            return 0
        root = discover_root(args.root)
        if args.status:
            print(json.dumps(status_run(root), indent=2))
            return 0
        if args.warm:
            row = warm_run(root, args.repo, args.slot, dry_run=args.dry_run)
            print(json.dumps(row, indent=2))
            return 0 if row["status"] not in ("occupied", "blocked", "partial") else 2
        if args.warm_all:
            rows = warm_all_run(root, dry_run=args.dry_run)
            print(json.dumps(rows, indent=2))
            return 2 if any(row["status"] in ("occupied", "blocked", "partial") for row in rows) else 0
        preflight()
        if args.pr_number is None:
            parser.error("pr_number is required unless another mode is supplied")
        manifest = setup_run(root, args.pr_number, slot=args.slot, org=args.org, iteration=args.iteration,
                             max_depth=args.max_depth, max_work_items=args.max_work_items, max_commits=args.max_commits)
        slot_id = manifest.get("slot_id", f"legacy-{manifest['slot']}")
        print(f"PR: {manifest['key']}\nSlot: {slot_id}\nBranch: {manifest['branch']}")
        print(f"Worktree: {root / manifest['slot_path']}\nSource: {manifest['submodule']['path']}")
        print(f"Head: {manifest['snapshot']['head_sha']}\nBase: {manifest.get('base_sha', 'unavailable')}")
        for relative in manifest.get("report_paths", []):
            print(f"Artifact: {root / manifest['slot_path'] / relative}")
        print(f"Complete: {manifest.get('complete', False)}; stale: {manifest.get('stale', False)}")
        script = Path(__file__).with_name("review_reset.py")
        if manifest.get("schema_version") == 2:
            selector = f"--slot {slot_id}"
        else:
            selector = f"--legacy-slot {manifest['slot']}"
        print(f"Archive: python3 {script} {selector} --mode archive")
        print(f"Discard: python3 {script} {selector} --mode discard --confirm {manifest['key']}")
        return 3 if manifest.get("stale") else 0 if manifest.get("complete") else 2
    except ReviewError as exc:
        print(f"Review setup stopped: {exc}", file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    raise SystemExit(main())
