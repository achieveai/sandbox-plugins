#!/usr/bin/env python3
"""Merge one managed local review branch into canonical local main without pushing."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import uuid

from review_common import (
    ReviewError,
    ReviewOwnedAllowlist,
    ReviewState,
    SourcePool,
    assert_no_git_operation,
    bounded_fetch,
    check_prepared_slot_conflict,
    checkpoint_pool_run,
    clear_outer_transaction,
    create_or_update_parent_remote_ref,
    current_ref,
    discover_root,
    ensure_repository,
    fetch_session,
    git_status,
    load_outer_transaction,
    lock_session,
    outer_merge_owner,
    outer_recovery_binding,
    outer_writer_scope,
    parse_ado_url,
    preflight,
    read_parent_remote_ref,
    read_submodules,
    recover_outer_writer,
    recover_pool_checkpoint,
    require_commit_identity,
    run_git,
    run_git_read,
    safe_path,
    save_outer_transaction,
    slot_writer_scope,
    utc_now,
    validate_parent_origin_remote,
    validate_parent_ref,
    validate_sha,
    worktree_rows,
    write_new_json,
)

CONFLICT_EXIT = 4
TERMINAL_STATUSES = ("archived", "discarded", "superseded", "merged")
REVIEW_EXTRA_PREFIXES = ("KnowledgeBase/", "DeveloperLearnings/")
SCRIPT_PATH = Path(__file__).resolve()
ACTIVE_PHASES = (
    "ready",
    "merge-baseline-intent",
    "merge-baseline-retained",
    "merge-source-intent",
    "merge-source-pinned",
    "merge-checkpoint-intent",
    "merge-checkpointed",
    "merge-aborted",
)


def _module_and_pool(state: ReviewState, repo: str) -> tuple[dict, SourcePool]:
    matches = [
        module
        for module in read_submodules(state.root)
        if Path(module["path"]).name == repo
    ]
    if len(matches) != 1:
        raise ReviewError("Repository is not a unique canonical configured source name.")
    return matches[0], SourcePool(state, matches[0])


def _validate_manifest_identity(
    state: ReviewState, pool: SourcePool, manifest: dict | None, slot: int
) -> dict:
    """Validate schema/identity fields only, independent of lifecycle phase.

    Factored out of `_validate_manifest` so new action validators (publish/
    reconcile/complete) can reuse identity enforcement without inheriting or
    weakening the existing local-merge phase machine below.
    """
    if manifest is None:
        raise ReviewError("No managed pooled review exists for that repository slot.")
    if (
        manifest.get("schema_version") != 2
        or manifest.get("identity") != state.identity
        or manifest.get("repo_key") != pool.repo_key
        or manifest.get("repo_name") != pool.repo_name
        or manifest.get("slot_number") != slot
        or manifest.get("slot_id") != pool.slot_name(slot)
        or manifest.get("slot_path") != pool.slot_relative_path(slot)
        or manifest.get("submodule") != pool.module
    ):
        raise ReviewError("Pooled review selector does not match its manifest identity.")
    return manifest


def _validate_manifest(
    state: ReviewState, pool: SourcePool, manifest: dict | None, slot: int
) -> dict:
    manifest = _validate_manifest_identity(state, pool, manifest, slot)
    if manifest.get("reset_mode") or manifest.get("reset_phase"):
        raise ReviewError("Archive/reset is already in progress for this review.")
    if manifest.get("status") in ("archived", "discarded", "superseded"):
        raise ReviewError("Selected pooled review is no longer mergeable.")
    if manifest.get("status") != "merged" and (
        manifest.get("status") != "ready" or manifest.get("phase") != "ready"
    ):
        merge_owned = manifest.get("phase") in (
            "merge-baseline-intent",
            "merge-baseline-retained",
            "merge-source-intent",
            "merge-source-pinned",
            "merge-checkpoint-intent",
            "merge-checkpointed",
            "merge-aborted",
        ) and manifest.get("status") in ("partial", "checkpointing", "checkpointed")
        if not merge_owned:
            raise ReviewError("Selected pooled review is not a completed valid setup.")
    return manifest


def _validate_new_flow_manifest(
    state: ReviewState, pool: SourcePool, manifest: dict | None, slot: int
) -> dict:
    """Identity plus lifecycle guard shared by publish and complete.

    Deliberately independent of the legacy `_validate_manifest`'s
    merge-owned phase set: publish/complete do not participate in the old
    local-only merge phase machine at all. Refuses a slot that a legacy
    local-merge transaction currently owns, so the two flows never race on
    the same slot.
    """
    manifest = _validate_manifest_identity(state, pool, manifest, slot)
    if manifest.get("reset_mode") or manifest.get("reset_phase"):
        raise ReviewError("Archive/reset is already in progress for this review.")
    if manifest.get("status") in TERMINAL_STATUSES:
        raise ReviewError("Selected pooled review is no longer publishable or completable.")
    transaction = load_outer_transaction(state)
    if (
        transaction is not None
        and transaction.get("repo_key") == pool.repo_key
        and transaction.get("slot_number") == slot
    ):
        raise ReviewError(
            "A legacy local-merge transaction is active for this slot; resolve or abort it "
            "before using --publish or --complete."
        )
    return manifest


def _review_allowlist(manifest: dict) -> ReviewOwnedAllowlist:
    return ReviewOwnedAllowlist(
        pr_dir=manifest["pr_dir"],
        gitlink_path=manifest["submodule"]["path"],
        extra_prefixes=REVIEW_EXTRA_PREFIXES,
    )


def _outer_and_source(
    state: ReviewState, pool: SourcePool, manifest: dict, *, attached: bool
) -> tuple[Path, Path]:
    outer = safe_path(state.root, manifest["slot_path"])
    source = safe_path(outer, manifest["submodule"]["path"])
    ensure_repository(outer, read_only=True)
    if outer.resolve() not in state.registered(read_only=True):
        raise ReviewError("Selected outer slot is not an owned registered worktree.")
    common = Path(
        run_git_read(
            outer, ["rev-parse", "--path-format=absolute", "--git-common-dir"]
        ).stdout.strip()
    ).resolve()
    if common != state.common:
        raise ReviewError("Selected outer slot belongs to a different repository.")
    expected_ref = manifest["branch"] if attached else None
    if current_ref(outer, read_only=True) != expected_ref:
        raise ReviewError("Selected outer slot moved from its recorded branch state.")
    ensure_repository(source, read_only=True)
    source_common = Path(
        run_git_read(
            source, ["rev-parse", "--path-format=absolute", "--git-common-dir"]
        ).stdout.strip()
    ).resolve()
    if source_common != pool.store.resolve():
        raise ReviewError("Selected nested source is not owned by its source pool.")
    if source.resolve() not in {
        row["worktree"] for row in worktree_rows(pool.store, read_only=True)
    }:
        raise ReviewError("Selected nested source is not a registered pool worktree.")
    if current_ref(source, read_only=True) is not None:
        raise ReviewError("Selected nested source must remain detached.")
    if git_status(source, read_only=True):
        raise ReviewError("Selected nested source is dirty.")
    if run_git_read(
        source,
        ["ls-files", "--others", "--ignored", "--exclude-standard", "-z"],
    ).stdout:
        raise ReviewError("Selected nested source contains ignored local data.")
    return outer, source


def _validate_branch(root: Path, value: str) -> str:
    if not value or value == "." or value.startswith("refs/"):
        raise ReviewError("Configured submodule tracking branch is unsupported.")
    if run_git_read(root, ["check-ref-format", "--branch", value], check=False).returncode:
        raise ReviewError("Configured submodule tracking branch is malformed.")
    return value


def _configured_branch(root: Path, module: dict) -> str | None:
    key = f"submodule.{module['name']}.branch"
    local = run_git_read(root, ["config", "--get", key], check=False)
    if local.returncode == 0:
        return _validate_branch(root, local.stdout.strip())
    config = root / ".gitmodules"
    if config.is_symlink() or not config.is_file():
        raise ReviewError("Canonical .gitmodules is unavailable or unsafe.")
    committed = run_git_read(
        root, ["config", "--file", str(config), "--get", key], check=False
    )
    if committed.returncode == 0:
        return _validate_branch(root, committed.stdout.strip())
    return None


def _remote_default_branch(root: Path, pool: SourcePool) -> str:
    # This is a read-only symbolic-HEAD query. The selected tip itself is fetched
    # separately through the bounded acquisition store before it is trusted.
    result = run_git_read(
        pool.store,
        ["ls-remote", "--symref", "--", pool.module["url"], "HEAD"],
        check=False,
    )
    if result.returncode:
        raise ReviewError("Remote symbolic HEAD lookup failed; no baseline was selected.")
    branches = []
    for line in result.stdout.splitlines():
        match = re.fullmatch(r"ref: refs/heads/([^\t]+)\tHEAD", line)
        if match:
            branches.append(match.group(1))
    if len(branches) != 1:
        raise ReviewError("Remote symbolic HEAD did not identify exactly one branch.")
    return _validate_branch(root, branches[0])


def resolve_tracking_baseline(
    state: ReviewState,
    pool: SourcePool,
    manifest: dict,
    *,
    max_depth: int,
) -> tuple[str, str]:
    """Resolve and freshly fetch the configured/default tracking branch once."""
    recorded_branch = manifest.get("merge_baseline_branch")
    recorded_sha = manifest.get("merge_baseline_sha")
    if recorded_branch or recorded_sha:
        if not recorded_branch or not recorded_sha:
            raise ReviewError("Recorded merge baseline is incomplete.")
        return _validate_branch(state.root, recorded_branch), validate_sha(recorded_sha)
    pool._ensure_store()
    branch = _configured_branch(state.root, pool.module) or _remote_default_branch(
        state.root, pool
    )
    ref = f"refs/heads/{branch}"
    result = bounded_fetch(
        pool.store, pool.module["url"], [ref], depth=1000, max_depth=max_depth
    )
    if result.returncode:
        raise ReviewError("Fresh tracking-baseline fetch failed.")
    tips = getattr(result, "requested_tips", ())
    if len(tips) != 1:
        raise ReviewError("Tracking-baseline fetch returned no exact result identity.")
    return branch, validate_sha(tips[0])


def fresh_tracking_tip(
    state: ReviewState,
    pool: SourcePool,
    manifest: dict,
    *,
    max_depth: int,
) -> tuple[str, str]:
    """Freshly fetch the configured/default tracking branch on every call.

    Unlike `resolve_tracking_baseline`, this never memoizes against a
    recorded `merge_baseline_branch`/`merge_baseline_sha` pair: Phase 2A and
    Phase 2B each need the exact freshest nested tracked tip at the moment of
    that attempt, not the tip recorded once at the start of the legacy local
    merge. `resolve_tracking_baseline` itself is left completely unchanged
    for the legacy local-only merge path.
    """
    pool._ensure_store()
    branch = _configured_branch(state.root, pool.module) or _remote_default_branch(
        state.root, pool
    )
    ref = f"refs/heads/{branch}"
    result = bounded_fetch(
        pool.store, pool.module["url"], [ref], depth=1000, max_depth=max_depth
    )
    if result.returncode:
        raise ReviewError("Fresh tracking-tip fetch failed.")
    tips = getattr(result, "requested_tips", ())
    if len(tips) != 1:
        raise ReviewError("Tracking-tip fetch returned no exact result identity.")
    return branch, validate_sha(tips[0])


def _fetch_parent_main_tip(
    state: ReviewState, pool: SourcePool, slot: int, outer: Path
) -> str:
    """Freshly fetch parent `origin/main` into a private, slot-keyed ref.

    All slot worktrees share one repository's refs, so writing the shared
    `refs/remotes/origin/main` tracking ref from a concurrently-reconciling
    slot would race with another slot doing the same fetch. Instead this
    fetches into a private temporary ref scoped to this repo key and slot,
    mirroring the existing `review_git_prepare.py` `prepare()` idiom for
    nested-source fetches, then reads that private ref back locally.
    """
    validate_parent_origin_remote(state.root)
    ref = validate_parent_ref(state.root, "refs/heads/main")
    tmp_ref = f"refs/review-setup/reconcile/{pool.repo_key}-{slot}"
    run_git(outer, ["fetch", "--no-tags", "--", "origin", f"+{ref}:{tmp_ref}"])
    return validate_sha(
        run_git_read(outer, ["rev-parse", f"{tmp_ref}^{{commit}}"]).stdout.strip()
    )


def _retained_for_merge(manifest: dict, baseline: str) -> dict[str, str]:
    snapshot = manifest.get("snapshot", {})
    retained = manifest.get("retained_commits", {})
    result = {
        "head": retained.get("head") or snapshot.get("head_sha"),
        "base": retained.get("base") or manifest.get("base_sha") or snapshot.get("base_sha"),
        "target": retained.get("target") or snapshot.get("target_sha"),
        "baseline": retained.get("baseline") or manifest.get("baseline_sha"),
        "merge-baseline": baseline,
    }
    if any(not value for value in result.values()):
        raise ReviewError("Review manifest lacks commits required for private retention.")
    return {key: validate_sha(value) for key, value in result.items()}


def _validate_root_preparation(state: ReviewState) -> str:
    """Reject an unsuitable canonical root before preparation can write or fetch."""
    ensure_repository(state.root, read_only=True)
    if current_ref(state.root, read_only=True) != "main":
        raise ReviewError("Canonical review root must have local main checked out.")
    assert_no_git_operation(state.root)
    if _root_tracked_status(
        state.root, allow_source_worktrees=_configured_gitlinks(state.root)
    ):
        raise ReviewError("Canonical review root has tracked or staged changes.")
    return validate_sha(run_git_read(state.root, ["rev-parse", "HEAD"]).stdout.strip())


def _validated_review_start_tip(
    state: ReviewState, manifest: dict, outer: Path
) -> str:
    tip = validate_sha(run_git_read(outer, ["rev-parse", "HEAD"]).stdout.strip())
    start = validate_sha(manifest["branch_start"])
    if run_git_read(
        outer, ["merge-base", "--is-ancestor", start, tip], check=False
    ).returncode:
        raise ReviewError("Review branch tip is not a descendant of its recorded start.")
    allowed_gitlink = manifest["submodule"]["path"]
    allowed_prefix = manifest["pr_dir"] + "/"
    if any(
        path != allowed_gitlink and not path.startswith(allowed_prefix)
        for path in _tree_paths(state.root, start, tip)
    ):
        raise ReviewError(
            "Review branch contains committed changes outside its PR directory and selected gitlink."
        )
    return tip


def _prepare_review(
    state: ReviewState,
    pool: SourcePool,
    manifest: dict,
    *,
    max_depth: int,
    transaction: dict | None = None,
) -> tuple[dict, Path, Path, dict]:
    if manifest.get("status") == "merged":
        return (
            manifest,
            safe_path(state.root, manifest["slot_path"]),
            safe_path(
                state.root, f"{manifest['slot_path']}/{manifest['submodule']['path']}"
            ),
            transaction or {},
        )
    if manifest.get("phase") not in ACTIVE_PHASES:
        raise ReviewError("Selected setup run has not reached a valid merge-ready phase.")
    outer, source = _outer_and_source(state, pool, manifest, attached=True)
    if manifest.get("complete") is not True or manifest.get("stale") is True:
        raise ReviewError("Selected setup is incomplete or stale; refresh it before merging.")
    if manifest.get("status") == "ready":
        retained = manifest.get("retention_refs") or {}
        required_retention = {"head", "base", "target", "baseline"}
        if not required_retention.issubset(retained):
            raise ReviewError("Selected setup lacks its required retained source snapshot.")
        expected_by_kind = {
            "head": manifest.get("snapshot", {}).get("head_sha"),
            "base": manifest.get("base_sha")
            or manifest.get("snapshot", {}).get("base_sha"),
            "target": manifest.get("snapshot", {}).get("target_sha"),
            "baseline": manifest.get("baseline_sha"),
        }
        for kind in required_retention:
            expected = (manifest.get("retained_commits") or {}).get(
                kind
            ) or (manifest.get("required_shas") or {}).get(kind)
            expected = expected or expected_by_kind[kind]
            if not expected:
                raise ReviewError("Selected setup lacks an exact retained source commit.")
            actual = run_git_read(
                pool.store, ["rev-parse", "--verify", retained[kind]], check=False
            )
            if actual.returncode or validate_sha(actual.stdout.strip()) != validate_sha(expected):
                raise ReviewError("Selected setup retention refs no longer match its snapshot.")
    recorded_review_start = manifest.get("merge_review_start_tip")
    review_start_tip = validate_sha(
        recorded_review_start or _validated_review_start_tip(state, manifest, outer)
    )
    actual_review_tip = validate_sha(
        run_git_read(outer, ["rev-parse", "HEAD"]).stdout.strip()
    )
    if recorded_review_start and actual_review_tip not in (
        review_start_tip,
        manifest.get("checkpoint_sha"),
    ):
        raise ReviewError("Review branch moved after its validated merge-only tip was recorded.")
    manifest["merge_review_start_tip"] = review_start_tip
    merge_phase = manifest.get("phase")
    if merge_phase == "ready":
        branch, baseline = resolve_tracking_baseline(
            state, pool, manifest, max_depth=max_depth
        )
        manifest["merge_baseline_branch"] = branch
        manifest["merge_baseline_sha"] = baseline
        manifest["phase"] = "merge-baseline-intent"
        manifest["status"] = "partial"
        state.save_pool_run(manifest)
        merge_phase = manifest["phase"]
    else:
        branch = _validate_branch(state.root, manifest["merge_baseline_branch"])
        baseline = validate_sha(manifest["merge_baseline_sha"])
    if merge_phase == "merge-baseline-intent":
        retained = pool.retain(
            manifest["run_id"], _retained_for_merge(manifest, baseline)
        )
        manifest["retained_commits"] = retained
        manifest.setdefault("retention_refs", {})["merge-baseline"] = pool.retention_ref(
            manifest["run_id"], "merge-baseline"
        )
        manifest["phase"] = "merge-baseline-retained"
        state.save_pool_run(manifest)
        merge_phase = manifest["phase"]
    if merge_phase == "merge-baseline-retained":
        manifest["phase"] = "merge-source-intent"
        state.save_pool_run(manifest)
        merge_phase = manifest["phase"]
    if merge_phase == "merge-source-intent":
        with pool.store_lock(
            phase="merge-source-pin"
        ), pool.shared_writer(phase="merge-source-pin"):
            actual_source = validate_sha(
                run_git_read(source, ["rev-parse", "HEAD"]).stdout.strip()
            )
            if actual_source != baseline:
                run_git(source, ["checkout", "--detach", baseline])
            if (
                git_status(source)
                or current_ref(source) is not None
                or validate_sha(
                    run_git_read(source, ["rev-parse", "HEAD"]).stdout.strip()
                )
                != baseline
            ):
                raise ReviewError("Nested source did not reach the pinned merge baseline.")
        manifest["phase"] = "merge-source-pinned"
        state.save_pool_run(manifest)
        merge_phase = manifest["phase"]
    if merge_phase in (
        "merge-source-pinned",
        "merge-checkpoint-intent",
        "merge-checkpointed",
        "merge-aborted",
    ):
        if validate_sha(run_git_read(source, ["rev-parse", "HEAD"]).stdout.strip()) != baseline:
            raise ReviewError("Selected source moved from the pinned merge baseline.")
    else:
        raise ReviewError("Merge preparation journal is in an unsupported phase.")
    trailer = f"Review-Merge-Run: {manifest['run_id']}"
    # Checkpoint and root reservation are one canonical-root critical section.
    # This closes the gap where another merger could move main after validation
    # but before this review made its durable reservation.
    with state.outer_lock(phase="merge-checkpoint-and-reserve"):
        if manifest.get("status") == "checkpointing":
            recover_pool_checkpoint(
                state,
                manifest,
                outer,
                trailer=trailer,
                expected_parent=review_start_tip,
            )
        files = state.validate_pool_run_changes(
            manifest,
            archive=True,
            expected_source_sha=validate_sha(manifest["merge_baseline_sha"]),
            expected_tip=(
                validate_sha(manifest["checkpoint_sha"])
                if manifest.get("checkpoint_sha")
                else review_start_tip
            ),
        )
        if manifest.get("phase") not in ("merge-checkpointed", "merge-aborted"):
            require_commit_identity(outer, operation="Merge checkpoint")
            manifest["phase"] = "merge-checkpoint-intent"
            state.save_pool_run(manifest)
            tip = checkpoint_pool_run(
                state,
                manifest,
                files,
                slot=outer,
                message=f"Checkpoint review {manifest['key']} ({manifest['run_id']})",
                trailer=trailer,
            )
            manifest["review_tip"] = tip
            manifest["phase"] = "merge-checkpointed"
            manifest["status"] = "checkpointed"
            state.save_pool_run(manifest)
        tip = validate_sha(manifest.get("review_tip") or manifest.get("checkpoint_sha"))
        if validate_sha(run_git_read(outer, ["rev-parse", "HEAD"]).stdout.strip()) != tip:
            raise ReviewError("Review branch moved after its immutable checkpoint.")
        if git_status(outer):
            raise ReviewError("Review slot changed after checkpoint.")
        if transaction is None:
            incoming = _incoming_paths(state, manifest, manifest["review_tip"])
            root_start = _validate_root_initial(
                state,
                manifest,
                incoming,
                validate_sha(manifest["merge_baseline_sha"]),
            )
            if load_outer_transaction(state) is not None:
                raise ReviewError(
                    "Another merge reserved the canonical root while preparing."
                )
            transaction = _transaction_for(state, manifest, incoming, root_start)
            save_outer_transaction(state, transaction)
    return manifest, outer, source, transaction


def _tree_paths(repo: Path, old: str, new: str) -> set[str]:
    return set(
        filter(
            None,
            run_git_read(
                repo, ["diff", "--no-renames", "--name-only", "-z", old, new, "--"]
            ).stdout.split("\0"),
        )
    )


def _incoming_paths(state: ReviewState, manifest: dict, tip: str) -> set[str]:
    start = validate_sha(manifest["branch_start"])
    paths = _tree_paths(state.root, start, tip)
    allowed = {manifest["submodule"]["path"]}
    prefix = manifest["pr_dir"] + "/"
    if any(path not in allowed and not path.startswith(prefix) for path in paths):
        raise ReviewError("Checkpointed branch contains changes outside its PR directory and selected gitlink.")
    # The selected gitlink is always in checkpoint scope, but it may already equal
    # the tracking baseline and therefore need no tree delta in this review.
    return paths


def _git_path(repo: Path, name: str) -> Path:
    value = run_git_read(repo, ["rev-parse", "--git-path", name]).stdout.strip()
    path = Path(value)
    return path if path.is_absolute() else repo / path


def _merge_head(root: Path) -> str | None:
    path = _git_path(root, "MERGE_HEAD")
    if not path.exists():
        return None
    values = path.read_text(encoding="ascii").split()
    if len(values) != 1:
        raise ReviewError("Canonical root has an unexpected multi-head merge.")
    return validate_sha(values[0])


def _root_tracked_status(
    root: Path, *, allow_source_worktrees: set[str] | None = None
) -> list[tuple[str, str]]:
    raw = run_git_read(
        root, ["status", "--porcelain=v1", "-z", "--untracked-files=no"]
    ).stdout
    rows = []
    records = raw.split("\0")
    index = 0
    while index < len(records) and records[index]:
        row = records[index]
        code, relative = row[:2], row[3:]
        # Submodule worktree dirtiness is validated directly (including origin,
        # detached HEAD, ignored files, and exact commit). The outer index remains
        # authoritative; ignore only its worktree-side summary for that one path.
        if not (
            relative in (allow_source_worktrees or set()) and code[0] == " "
        ):
            rows.append((code, relative))
        index += 1
        if "R" in code or "C" in code:
            if index < len(records) and records[index]:
                rows.append((code, records[index]))
                index += 1
    return rows


def _canonical_source_initialized(root: Path, module_path: str) -> bool:
    return (safe_path(root, module_path) / ".git").exists()


def _configured_gitlinks(root: Path) -> set[str]:
    links = set()
    for module in read_submodules(root, read_only=True):
        line = run_git_read(root, ["ls-files", "--stage", "--", module["path"]]).stdout
        if line.startswith("160000 "):
            links.add(module["path"])
    return links


def _transaction_root_status(state: ReviewState, transaction: dict) -> list[tuple[str, str]]:
    return _root_tracked_status(
        state.root, allow_source_worktrees=_configured_gitlinks(state.root)
    )


def _validate_untracked_collisions(root: Path, incoming: set[str]) -> None:
    outputs = (
        run_git_read(root, ["ls-files", "--others", "--exclude-standard", "-z"]).stdout,
        run_git_read(
            root,
            ["ls-files", "--others", "--ignored", "--exclude-standard", "-z"],
        ).stdout,
    )
    local = set(filter(None, "".join(outputs).split("\0")))
    for relative in incoming:
        candidate = safe_path(root, relative)
        cursor = candidate.parent
        while cursor != root:
            if cursor.is_symlink() or (cursor.exists() and not cursor.is_dir()):
                raise ReviewError("Incoming merge path has an unsafe local ancestor.")
            cursor = cursor.parent
        if any(
            path == relative
            or path.startswith(relative.rstrip("/") + "/")
            or relative.startswith(path.rstrip("/") + "/")
            for path in local
        ):
            raise ReviewError("An unrelated untracked or ignored path collides with incoming review content.")


def _validate_canonical_submodule(
    state: ReviewState, manifest: dict, baseline: str
) -> None:
    path = safe_path(state.root, manifest["submodule"]["path"])
    if not (path / ".git").exists():
        if path.exists() and (path.is_symlink() or not path.is_dir() or any(path.iterdir())):
            raise ReviewError("Uninitialized canonical submodule path contains unmanaged content.")
        return
    ensure_repository(path, read_only=True)
    origin = run_git_read(path, ["config", "--get", "remote.origin.url"], check=False)
    if origin.returncode or parse_ado_url(origin.stdout.strip()) != parse_ado_url(
        manifest["submodule"]["url"]
    ):
        raise ReviewError("Initialized canonical submodule has a mismatched origin.")
    if git_status(path, read_only=True) or run_git_read(
        path, ["ls-files", "--others", "--ignored", "--exclude-standard", "-z"]
    ).stdout:
        raise ReviewError("Initialized canonical submodule is dirty.")
    if current_ref(path, read_only=True) is not None:
        raise ReviewError("Initialized canonical submodule must be detached.")
    if validate_sha(run_git_read(path, ["rev-parse", "HEAD"]).stdout.strip()) != baseline:
        raise ReviewError("Initialized canonical submodule is not at the pinned merge baseline.")


def _validate_root_initial(
    state: ReviewState, manifest: dict, incoming: set[str], baseline: str
) -> str:
    ensure_repository(state.root, read_only=True)
    if current_ref(state.root, read_only=True) != "main":
        raise ReviewError("Canonical review root must have local main checked out.")
    assert_no_git_operation(state.root)
    initialized = _canonical_source_initialized(
        state.root, manifest["submodule"]["path"]
    )
    if _root_tracked_status(
        state.root, allow_source_worktrees=_configured_gitlinks(state.root)
    ):
        raise ReviewError("Canonical review root has tracked or staged changes.")
    _validate_untracked_collisions(state.root, incoming)
    _validate_canonical_submodule(state, manifest, baseline)
    return validate_sha(run_git_read(state.root, ["rev-parse", "HEAD"]).stdout.strip())


def _transaction_for(
    state: ReviewState,
    manifest: dict,
    incoming: set[str],
    root_start: str,
) -> dict:
    tip = validate_sha(manifest["review_tip"])
    return {
        "id": uuid.uuid4().hex,
        "run_id": manifest["run_id"],
        "repo_key": manifest["repo_key"],
        "repo_name": manifest["repo_name"],
        "slot_number": manifest["slot_number"],
        "slot_id": manifest["slot_id"],
        "branch": manifest["branch"],
        "review_tip": tip,
        "root_start": validate_sha(root_start),
        "main_branch": "main",
        "baseline_branch": manifest["merge_baseline_branch"],
        "baseline_sha": validate_sha(manifest["merge_baseline_sha"]),
        "module_path": manifest["submodule"]["path"],
        "pr_dir": manifest["pr_dir"],
        "allowed_paths": sorted(incoming | {manifest["submodule"]["path"]}),
        "canonical_source_initialized": _canonical_source_initialized(
            state.root, manifest["submodule"]["path"]
        ),
        "expected_merge_head": tip,
        "phase": "reserved",
        "created_at": utc_now(),
    }


def _validate_transaction(
    state: ReviewState,
    pool: SourcePool,
    manifest: dict,
    transaction: dict,
) -> None:
    expected = {
        "workspace_identity": state.identity,
        "run_id": manifest["run_id"],
        "repo_key": pool.repo_key,
        "repo_name": pool.repo_name,
        "slot_number": manifest["slot_number"],
        "slot_id": manifest["slot_id"],
        "branch": manifest["branch"],
        "review_tip": validate_sha(manifest["review_tip"]),
        "baseline_branch": manifest["merge_baseline_branch"],
        "baseline_sha": validate_sha(manifest["merge_baseline_sha"]),
        "module_path": manifest["submodule"]["path"],
        "main_branch": "main",
    }
    if any(transaction.get(key) != value for key, value in expected.items()):
        raise ReviewError("Active outer-merge transaction belongs to different review state.")


def _index_gitlink(root: Path, relative: str) -> str:
    line = run_git_read(root, ["ls-files", "--stage", "--", relative]).stdout.strip()
    match = re.fullmatch(r"160000 ([0-9a-f]{40}) [0-3]\t.+", line)
    if not match:
        raise ReviewError("Selected source path is not exactly one staged gitlink.")
    return validate_sha(match.group(1))


def _changed_from_start(state: ReviewState, transaction: dict, result: str) -> set[str]:
    return _tree_paths(state.root, transaction["root_start"], result)


def _verify_result_commit(state: ReviewState, transaction: dict, result: str) -> str:
    result = validate_sha(result)
    row = run_git_read(
        state.root, ["rev-list", "--parents", "-n", "1", result]
    ).stdout.split()
    if row != [result, transaction["root_start"], transaction["review_tip"]]:
        raise ReviewError("Result is not the exact two-parent merge recorded by this transaction.")
    if not _changed_from_start(state, transaction, result).issubset(
        set(transaction["allowed_paths"])
    ):
        raise ReviewError("Result merge changed paths outside the recorded incoming scope.")
    tree_link = validate_sha(
        run_git_read(
            state.root,
            ["rev-parse", f"{result}:{transaction['module_path']}"],
        ).stdout.strip()
    )
    if tree_link != transaction["baseline_sha"]:
        raise ReviewError("Result merge does not contain the pinned baseline gitlink.")
    if run_git_read(
        state.root,
        ["merge-base", "--is-ancestor", transaction["review_tip"], result],
        check=False,
    ).returncode:
        raise ReviewError("Result merge does not contain the checkpointed review tip.")
    return result


def _conflict_paths(root: Path) -> list[str]:
    return sorted(
        set(
            filter(
                None,
                run_git_read(
                    root, ["diff", "--name-only", "--diff-filter=U", "-z"]
                ).stdout.split("\0"),
            )
        )
    )


def _handoff(state: ReviewState, transaction: dict, conflicts: list[str]) -> None:
    print("Merge conflicts require manual resolution.", file=sys.stderr)
    print(f"Root: {state.root}", file=sys.stderr)
    print(f"Pinned gitlink: {transaction['module_path']}={transaction['baseline_sha']}", file=sys.stderr)
    print("Unresolved paths:", file=sys.stderr)
    for path in conflicts:
        print(f"  {path}", file=sys.stderr)
    print("Resolve files, stage each intended path with git add, ensure the selected gitlink uses the pinned SHA, then rerun the same merge-review command.", file=sys.stderr)


def _commit_staged_merge(state: ReviewState, transaction: dict) -> str:
    root = state.root
    if _merge_head(root) != transaction["review_tip"]:
        raise ReviewError("MERGE_HEAD moved from the exact checkpointed review tip.")
    conflicts = _conflict_paths(root)
    if conflicts:
        transaction["phase"] = "conflicted"
        transaction["conflict_paths"] = conflicts
        save_outer_transaction(state, transaction)
        _handoff(state, transaction, conflicts)
        raise ReviewError("Merge conflicts remain unresolved.", CONFLICT_EXIT)
    unstaged = set(
        filter(None, run_git_read(root, ["diff", "--name-only", "-z"]).stdout.split("\0"))
    )
    uninitialized_gitlinks = {
        relative
        for relative in _configured_gitlinks(root)
        if not _canonical_source_initialized(root, relative)
    }
    unstaged.difference_update(uninitialized_gitlinks)
    if unstaged:
        raise ReviewError("Resolved merge contains unstaged tracked changes; stage resolutions explicitly.")
    staged = set(
        filter(
            None,
            run_git_read(root, ["diff", "--cached", "--name-only", "-z"]).stdout.split("\0"),
        )
    )
    if not staged or not staged.issubset(set(transaction["allowed_paths"])):
        raise ReviewError("Staged merge paths are empty or outside the recorded incoming scope.")
    if _index_gitlink(root, transaction["module_path"]) != transaction["baseline_sha"]:
        raise ReviewError(
            "Selected gitlink must be staged at the pinned tracking baseline before retry."
        )
    require_commit_identity(root, operation="Merge")
    transaction["phase"] = "commit-intent"
    save_outer_transaction(state, transaction)
    with outer_writer_scope(state, transaction["id"], phase="merge-commit"):
        run_git(
            root,
            [
                "-c",
                "submodule.recurse=false",
                "commit",
                "-m",
                f"Merge local review {transaction['branch']}",
                "-m",
                f"Review-Merge-Run: {transaction['run_id']}",
            ],
        )
    result = validate_sha(run_git_read(root, ["rev-parse", "HEAD"]).stdout.strip())
    _verify_result_commit(state, transaction, result)
    transaction["result_sha"] = result
    transaction["phase"] = "committed"
    save_outer_transaction(state, transaction)
    return result


def _merge_or_resume(state: ReviewState, transaction: dict) -> str:
    root = state.root
    if current_ref(root, read_only=True) != transaction["main_branch"]:
        raise ReviewError("Canonical main branch moved or was switched during merge.")
    head = validate_sha(run_git_read(root, ["rev-parse", "HEAD"]).stdout.strip())
    merge_head = _merge_head(root)
    if head != transaction["root_start"]:
        if merge_head is not None:
            raise ReviewError("Canonical HEAD moved while MERGE_HEAD remains active.")
        return _verify_result_commit(state, transaction, head)
    if merge_head is None:
        if _transaction_root_status(state, transaction):
            raise ReviewError("Canonical root changed after transaction reservation.")
        transaction["phase"] = "merge-intent"
        save_outer_transaction(state, transaction)
        with outer_writer_scope(state, transaction["id"], phase="merge-start"):
            result = run_git(
                root,
                [
                    "-c",
                    "submodule.recurse=false",
                    "merge",
                    "--no-ff",
                    "--no-commit",
                    transaction["review_tip"],
                ],
                check=False,
            )
        merge_head = _merge_head(root)
        if merge_head != transaction["review_tip"]:
            raise ReviewError("Merge did not establish the recorded exact MERGE_HEAD.")
        if result.returncode not in (0, 1):
            raise ReviewError(f"Git merge failed (exit {result.returncode}); transaction remains reserved.")
        conflicts = _conflict_paths(root)
        if conflicts:
            transaction["phase"] = "conflicted"
            transaction["conflict_paths"] = conflicts
            save_outer_transaction(state, transaction)
            _handoff(state, transaction, conflicts)
            raise ReviewError("Merge conflicts require manual resolution.", CONFLICT_EXIT)
        # Git can apply a different descendant submodule tip automatically. Always
        # override it with the exact freshly fetched baseline selected for this run.
        with outer_writer_scope(state, transaction["id"], phase="normalize-gitlink"):
            run_git(
                root,
                [
                    "update-index",
                    "--add",
                    "--cacheinfo",
                    "160000",
                    transaction["baseline_sha"],
                    transaction["module_path"],
                ],
            )
    elif merge_head != transaction["review_tip"]:
        raise ReviewError("A foreign merge replaced this transaction's MERGE_HEAD.")
    return _commit_staged_merge(state, transaction)


def _detach_slot(
    state: ReviewState,
    pool: SourcePool,
    manifest: dict,
    transaction: dict,
    result: str,
) -> tuple[Path, Path]:
    outer = safe_path(state.root, manifest["slot_path"])
    source = safe_path(outer, manifest["submodule"]["path"])
    active = current_ref(outer, read_only=True)
    head = validate_sha(run_git_read(outer, ["rev-parse", "HEAD"]).stdout.strip())
    if active == manifest["branch"]:
        if head != transaction["review_tip"]:
            raise ReviewError("Review slot branch moved before final detachment.")
        transaction["phase"] = "slot-detach-intent"
        save_outer_transaction(state, transaction)
        with outer_writer_scope(state, transaction["id"], phase="slot-detach"):
            run_git(
                outer,
                ["-c", "submodule.recurse=false", "checkout", "--detach", result],
            )
    elif active is not None or head != result:
        raise ReviewError("Review slot changed during merge finalization.")
    if current_ref(source, read_only=True) is not None or git_status(source, read_only=True):
        raise ReviewError("Nested source changed during merge finalization.")
    if validate_sha(run_git_read(source, ["rev-parse", "HEAD"]).stdout.strip()) != transaction[
        "baseline_sha"
    ]:
        raise ReviewError("Nested source moved from the pinned merge baseline.")
    if git_status(outer, read_only=True):
        raise ReviewError("Detached review slot is not clean at the merge result.")
    if transaction.get("phase") not in (
        "branch-delete-intent",
        "branch-deleted",
        "terminal-intent",
        "terminal-recorded",
    ):
        transaction["phase"] = "slot-detached"
        save_outer_transaction(state, transaction)
    return outer, source


def _delete_review_ref(
    state: ReviewState, manifest: dict, transaction: dict, result: str
) -> None:
    _verify_result_commit(state, transaction, result)
    branch_ref = f"refs/heads/{manifest['branch']}"
    current = run_git_read(state.root, ["rev-parse", "--verify", branch_ref], check=False)
    checked = {
        row.get("branch") for row in worktree_rows(state.root, read_only=True)
    }
    if manifest["branch"] in checked:
        raise ReviewError("Review branch remains checked out; refusing deletion.")
    if current.returncode == 0:
        if validate_sha(current.stdout.strip()) != transaction["review_tip"]:
            raise ReviewError("Review branch moved; refusing to delete a different tip.")
        transaction["phase"] = "branch-delete-intent"
        save_outer_transaction(state, transaction)
        with outer_writer_scope(state, transaction["id"], phase="delete-review-ref"):
            run_git(
                state.root,
                ["update-ref", "-d", branch_ref, transaction["review_tip"]],
            )
    elif transaction.get("phase") not in (
        "branch-delete-intent",
        "branch-deleted",
        "terminal-intent",
        "terminal-recorded",
    ):
        raise ReviewError("Review branch disappeared before recorded deletion intent.")
    transaction["phase"] = "branch-deleted"
    save_outer_transaction(state, transaction)


def _history_path(state: ReviewState, run_id: str) -> Path:
    return safe_path(state.directory, f"history/{run_id}.json")


def _finalize(
    state: ReviewState,
    pool: SourcePool,
    manifest: dict,
    transaction: dict,
    result: str,
) -> dict:
    result = _verify_result_commit(state, transaction, result)
    _detach_slot(state, pool, manifest, transaction, result)
    _delete_review_ref(state, manifest, transaction, result)
    manifest["status"] = "merged"
    manifest["phase"] = "merged"
    manifest["merge_phase"] = "merged"
    manifest["merged_at"] = manifest.get("merged_at") or utc_now()
    manifest["merge_result_sha"] = result
    manifest["review_tip"] = transaction["review_tip"]
    manifest["merge_transaction_id"] = transaction["id"]
    transaction["phase"] = "terminal-intent"
    save_outer_transaction(state, transaction)
    state.save_pool_run(manifest)
    history = _history_path(state, manifest["run_id"])
    if history.exists():
        existing = json.loads(history.read_text(encoding="utf-8"))
        if (
            existing.get("run_id") != manifest["run_id"]
            or existing.get("merge_result_sha") != result
        ):
            raise ReviewError("Existing terminal history does not match this merge.")
    else:
        write_new_json(history, manifest)
    transaction["phase"] = "terminal-recorded"
    transaction["result_sha"] = result
    save_outer_transaction(state, transaction)
    clear_outer_transaction(state, transaction["id"])
    return manifest


def _verify_terminal(state: ReviewState, pool: SourcePool, manifest: dict) -> dict:
    if manifest.get("status") != "merged":
        raise ReviewError("Selected review is not terminal merged state.")
    result = validate_sha(manifest["merge_result_sha"])
    tip = validate_sha(manifest["review_tip"])
    if current_ref(state.root, read_only=True) != "main":
        raise ReviewError("Canonical root is no longer on main.")
    main = validate_sha(run_git_read(state.root, ["rev-parse", "HEAD"]).stdout.strip())
    if run_git_read(
        state.root, ["merge-base", "--is-ancestor", result, main], check=False
    ).returncode:
        raise ReviewError("Recorded merge result is no longer contained by local main.")
    if run_git_read(
        state.root, ["merge-base", "--is-ancestor", tip, main], check=False
    ).returncode:
        raise ReviewError("Checkpointed review tip is no longer contained by local main.")
    branch = run_git_read(
        state.root, ["show-ref", "--verify", "--quiet", f"refs/heads/{manifest['branch']}"], check=False
    )
    if branch.returncode == 0:
        raise ReviewError("Recorded merged review branch unexpectedly exists again.")
    outer, source = _outer_and_source(state, pool, manifest, attached=False)
    if validate_sha(run_git_read(outer, ["rev-parse", "HEAD"]).stdout.strip()) != result:
        raise ReviewError("Merged slot moved from its result commit.")
    if validate_sha(run_git_read(source, ["rev-parse", "HEAD"]).stdout.strip()) != validate_sha(
        manifest["merge_baseline_sha"]
    ):
        raise ReviewError("Merged slot source moved from its pinned baseline.")
    return manifest


def merge_review(
    root: Path,
    repo: str,
    slot: int,
    *,
    dry_run: bool = False,
    max_depth: int = 1000000,
    history_timeout: float = 300,
    lock_timeout: float = 1200,
) -> dict:
    state = ReviewState(root, read_only=dry_run)
    module, pool = _module_and_pool(state, repo)
    slot = SourcePool._validate_slots([slot])[0]
    # Fail-closed compatibility guard (rulings R2, R18): before this slot is
    # ever touched -- dry-run inspection included -- refuse if Task 3's
    # new-format prepared-slot manifest disagrees with the legacy manifest
    # about which PR currently occupies it. No other merge behavior changes.
    check_prepared_slot_conflict(state, pool, slot)
    if dry_run:
        manifest = _validate_manifest(state, pool, state.load_pool_run(pool.repo_key, slot), slot)
        transaction = load_outer_transaction(state)
        return {
            "dry_run": True,
            "repo": pool.repo_name,
            "slot": pool.slot_name(slot),
            "run_id": manifest["run_id"],
            "status": manifest["status"],
            "phase": manifest.get("phase"),
            "branch": manifest["branch"],
            "active_transaction": transaction,
        }
    with lock_session(lock_timeout), state.lock(shared=True, phase="merge-review"):
        with state.slot_lock(pool.repo_key, slot, phase="merge-review"):
            transaction = load_outer_transaction(state)
            manifest = _validate_manifest(
                state, pool, state.load_pool_run(pool.repo_key, slot), slot
            )
            if manifest.get("status") == "merged" and transaction is None:
                return _verify_terminal(state, pool, manifest)
            if transaction is None:
                _validate_root_preparation(state)
                with slot_writer_scope(state, pool.repo_key, slot, phase="merge-review"):
                    with fetch_session(history_timeout, log=True):
                        manifest, outer, source, transaction = _prepare_review(
                            state,
                            pool,
                            manifest,
                            max_depth=max_depth,
                            transaction=None,
                        )
            else:
                _validate_transaction(state, pool, manifest, transaction)
            with outer_merge_owner(transaction["id"]):
                with state.outer_lock(phase="merge-review"):
                    _validate_transaction(state, pool, manifest, transaction)
                    result = _merge_or_resume(state, transaction)
                    transaction["result_sha"] = result
                    if transaction.get("phase") in (
                        "reserved",
                        "merge-intent",
                        "conflicted",
                        "commit-intent",
                    ):
                        transaction["phase"] = "committed"
                        save_outer_transaction(state, transaction)
                    return _finalize(state, pool, manifest, transaction, result)


def abort_merge(
    root: Path,
    repo: str,
    slot: int,
    *,
    confirm: str | None,
    lock_timeout: float = 1200,
) -> dict:
    state = ReviewState(root)
    _, pool = _module_and_pool(state, repo)
    slot = SourcePool._validate_slots([slot])[0]
    with lock_session(lock_timeout), state.lock(shared=True, phase="merge-abort"):
        with state.slot_lock(pool.repo_key, slot, phase="merge-abort"):
            manifest = _validate_manifest(
                state, pool, state.load_pool_run(pool.repo_key, slot), slot
            )
            if confirm != manifest["key"]:
                raise ReviewError(f"Abort requires --confirm {manifest['key']}.")
            transaction = load_outer_transaction(state)
            if transaction is None:
                raise ReviewError("No active merge transaction exists to abort.")
            _validate_transaction(state, pool, manifest, transaction)
            with outer_merge_owner(transaction["id"]):
                with state.outer_lock(phase="merge-abort"):
                    if current_ref(state.root, read_only=True) != transaction["main_branch"]:
                        raise ReviewError("Canonical branch moved; refusing abort.")
                    head = validate_sha(
                        run_git_read(state.root, ["rev-parse", "HEAD"]).stdout.strip()
                    )
                    if head != transaction["root_start"]:
                        raise ReviewError("Merge is already committed or canonical HEAD moved; abort refused.")
                    merge_head = _merge_head(state.root)
                    if merge_head is not None:
                        if merge_head != transaction["review_tip"]:
                            raise ReviewError("Foreign MERGE_HEAD prevents abort.")
                        with outer_writer_scope(
                            state, transaction["id"], phase="merge-abort"
                        ):
                            run_git(
                                state.root,
                                ["-c", "submodule.recurse=false", "merge", "--abort"],
                            )
                    elif _transaction_root_status(state, transaction):
                        raise ReviewError("Canonical root changed without MERGE_HEAD; abort refused.")
                    if (
                        validate_sha(
                            run_git_read(state.root, ["rev-parse", "HEAD"]).stdout.strip()
                        )
                        != transaction["root_start"]
                        or _transaction_root_status(state, transaction)
                        or _merge_head(state.root) is not None
                    ):
                        raise ReviewError("Git merge abort did not restore the recorded root state.")
                    manifest["status"] = "checkpointed"
                    manifest["phase"] = "merge-aborted"
                    manifest["merge_phase"] = "aborted"
                    manifest["merge_aborted_at"] = utc_now()
                    state.save_pool_run(manifest)
                    clear_outer_transaction(state, transaction["id"])
                    return manifest


def inspect_recovery(
    root: Path, repo: str, slot: int, *, lock_timeout: float = 1200
) -> dict:
    state = ReviewState(root)
    _, pool = _module_and_pool(state, repo)
    slot = SourcePool._validate_slots([slot])[0]
    with lock_session(lock_timeout), state.lock(shared=True, phase="merge-recovery"):
        with state.slot_lock(pool.repo_key, slot, phase="merge-recovery"):
            manifest = _validate_manifest(
                state, pool, state.load_pool_run(pool.repo_key, slot), slot
            )
            transaction = load_outer_transaction(state)
            if transaction is None:
                return {"pending": False, "run_id": manifest["run_id"]}
            _validate_transaction(state, pool, manifest, transaction)
            with outer_merge_owner(transaction["id"]):
                with state.outer_lock(phase="merge-recovery"):
                    return recover_outer_writer(state, inspect=True)


def apply_recovery(
    root: Path,
    repo: str,
    slot: int,
    assertion: dict,
    *,
    lock_timeout: float = 1200,
) -> dict:
    state = ReviewState(root)
    _, pool = _module_and_pool(state, repo)
    slot = SourcePool._validate_slots([slot])[0]
    with lock_session(lock_timeout), state.lock(shared=True, phase="merge-recovery"):
        with state.slot_lock(pool.repo_key, slot, phase="merge-recovery"):
            manifest = _validate_manifest(
                state, pool, state.load_pool_run(pool.repo_key, slot), slot
            )
            transaction = load_outer_transaction(state)
            if transaction is None:
                raise ReviewError("No active merge transaction exists.")
            _validate_transaction(state, pool, manifest, transaction)
            with outer_merge_owner(transaction["id"]):
                with state.outer_lock(phase="merge-recovery"):
                    return recover_outer_writer(state, assertion=assertion)


def publish_review(
    root: Path,
    repo: str,
    slot: int,
    *,
    confirm: str | None,
    lock_timeout: float = 1200,
) -> dict:
    """Commit only allowed review-owned content and push it to parent origin.

    Performs no reconciliation, no root lock, and no parent-`main` movement.
    Runs entirely under the target slot lock plus `slot_writer_scope`.
    """
    state = ReviewState(root)
    _, pool = _module_and_pool(state, repo)
    slot = SourcePool._validate_slots([slot])[0]
    with lock_session(lock_timeout), state.lock(shared=True, phase="merge-review-publish"):
        with state.slot_lock(pool.repo_key, slot, phase="merge-review-publish"):
            check_prepared_slot_conflict(state, pool, slot)
            manifest = _validate_new_flow_manifest(
                state, pool, state.load_pool_run(pool.repo_key, slot), slot
            )
            if confirm != manifest["key"]:
                raise ReviewError(f"Publish requires --confirm {manifest['key']}.")
            outer, source = _outer_and_source(state, pool, manifest, attached=True)
            assert_no_git_operation(outer)
            allowlist = _review_allowlist(manifest)
            with slot_writer_scope(state, pool.repo_key, slot, phase="publish"):
                source_sha = validate_sha(
                    run_git_read(source, ["rev-parse", "HEAD"]).stdout.strip()
                )
                files = state.validate_pool_run_changes(
                    manifest,
                    archive=True,
                    expected_source_sha=source_sha,
                    extra_prefixes=allowlist.extra_prefixes,
                )
                require_commit_identity(outer, operation="Publish checkpoint")
                manifest["publish_phase"] = "publish-checkpoint-intent"
                state.save_pool_run(manifest)
                tip = checkpoint_pool_run(
                    state,
                    manifest,
                    files,
                    slot=outer,
                    message=f"Publish review {manifest['key']} ({manifest['run_id']})",
                    trailer=f"Review-Publish-Run: {manifest['run_id']}",
                    extra_prefixes=allowlist.extra_prefixes,
                )
                manifest["publish_phase"] = "publish-checkpointed"
                state.save_pool_run(manifest)
                ref = validate_parent_ref(state.root, f"refs/heads/{manifest['branch']}")
                expected_old = manifest.get("published_tip")
                if expected_old is not None:
                    expected_old = validate_sha(expected_old)
                current_remote = read_parent_remote_ref(state.root, ref)
                if current_remote == tip:
                    # Exact after-state: a prior attempt already pushed this exact
                    # checkpoint tip but the process stopped before the local
                    # after-state was journaled. Resume without re-pushing.
                    published = tip
                elif current_remote == expected_old:
                    manifest["publish_phase"] = "publish-push-intent"
                    state.save_pool_run(manifest)
                    published = create_or_update_parent_remote_ref(
                        outer, ref, tip, expected_old=expected_old, writer_scope="slot"
                    )
                else:
                    raise ReviewError(
                        "Parent review ref is neither at its expected prior published tip "
                        "nor at this publish's exact result; refusing to publish."
                    )
                manifest["published_tip"] = published
                manifest["publish_phase"] = "published"
                manifest["published_at"] = utc_now()
                state.save_pool_run(manifest)
            return {
                "repo": pool.repo_name,
                "slot": pool.slot_name(slot),
                "run_id": manifest["run_id"],
                "branch": manifest["branch"],
                "published_tip": manifest["published_tip"],
            }


def _reconcile_handoff(
    outer: Path, pool: SourcePool, manifest: dict, conflicts: list[str]
) -> None:
    print("Reconciliation conflicts require manual resolution.", file=sys.stderr)
    print(f"Slot: {outer}", file=sys.stderr)
    print("Unresolved paths:", file=sys.stderr)
    for path in conflicts:
        print(f"  {path}", file=sys.stderr)
    print("Allowed review-owned surfaces (edit and `git add` only within these):", file=sys.stderr)
    print(f"  {manifest['pr_dir']}/", file=sys.stderr)
    for prefix in REVIEW_EXTRA_PREFIXES:
        print(f"  {prefix}", file=sys.stderr)
    print(
        f"  {manifest['submodule']['path']} (selected gitlink; already resolved "
        "automatically -- do not edit)",
        file=sys.stderr,
    )
    print(
        "Resolve each listed path in this slot only, then `git add <resolved-path>` for "
        "each one. Do not choose or edit the selected gitlink, commit, push, fetch, switch "
        "branches, reset, or touch canonical root.",
        file=sys.stderr,
    )
    print("Continuation command:", file=sys.stderr)
    print(
        f"  python3 {SCRIPT_PATH} --repo {pool.repo_name} --slot {manifest['slot_number']} "
        f"--complete --confirm {manifest['key']}",
        file=sys.stderr,
    )


def _blob_at(outer: Path, treeish: str | None, path: str) -> str | None:
    """The blob id of `path` in `treeish`, or in the index if `treeish` is None."""
    ref = f":0:{path}" if treeish is None else f"{treeish}:{path}"
    result = run_git_read(outer, ["rev-parse", "--verify", "--quiet", ref], check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def _snapshot_auto_merge_paths(outer: Path) -> dict[str, str]:
    """Blob ids for every path git staged automatically during the merge.

    Called once, immediately after `git merge --no-commit` returns and before
    any conflict resolution. Conflicted paths have no stage-0 index entry at
    that moment, so they are naturally excluded; every other changed path is
    exactly whatever git's own three-way merge produced -- which may equal
    neither parent's blob verbatim (e.g. a clean line-level combination of
    both sides). Recording it now, before any operator or script action can
    alter it, is what later lets a legitimate but non-trivial auto-merge
    result be told apart from a hand-edited or unrelated staged path.
    """
    changed = sorted(
        set(
            filter(
                None,
                run_git_read(outer, ["diff", "--cached", "--name-only", "-z"]).stdout.split("\0"),
            )
        )
    )
    snapshot = {}
    for path in changed:
        blob = _blob_at(outer, None, path)
        if blob is not None:
            snapshot[path] = blob
    return snapshot


def _unexpected_reconciliation_paths(
    outer: Path,
    manifest: dict,
    gitlink_path: str,
    result_tree: str | None,
) -> list[str]:
    """Changed paths outside git's own recorded auto-merge or a resolution.

    `result_tree` is `None` to check the currently staged index against
    `HEAD`/`parent_tip` (pre-commit), or an exact commit sha to check an
    already committed result (post-commit resume). Every changed path must be
    the already-resolved gitlink, a recorded and allowlisted conflict
    resolution, or bit-for-bit what `_snapshot_auto_merge_paths` captured
    immediately after the merge -- anything else was staged or committed
    outside the script's own reconciliation (an unrelated path, or a
    hand-edited/fabricated result) and must be refused.
    """
    resolved = set(manifest.get("reconcile_all_conflict_paths") or [])
    allowlist = _review_allowlist(manifest)
    auto_merged = manifest.get("reconcile_auto_merge_paths") or {}
    if result_tree is None:
        diff_args = ["diff", "--cached", "--name-only", "-z"]
    else:
        parent_tip = validate_sha(manifest["reconcile_parent_tip"])
        diff_args = ["diff", "--name-only", "-z", parent_tip, result_tree]
    changed = sorted(
        set(filter(None, run_git_read(outer, diff_args).stdout.split("\0")))
    )
    unexpected = []
    for path in changed:
        if path == gitlink_path:
            continue
        if path in resolved:
            if not allowlist.allows(path):
                unexpected.append(path)
            continue
        if auto_merged.get(path) != _blob_at(outer, result_tree, path):
            unexpected.append(path)
    return unexpected


def _verify_reconciled_commit(state: ReviewState, manifest: dict, result: str) -> str:
    result = validate_sha(result)
    outer = safe_path(state.root, manifest["slot_path"])
    parent_tip = validate_sha(manifest["reconcile_parent_tip"])
    main_sha = validate_sha(manifest["reconcile_main_sha"])
    row = run_git_read(outer, ["rev-list", "--parents", "-n", "1", result]).stdout.split()
    if row != [result, parent_tip, main_sha]:
        raise ReviewError(
            "Result is not the exact two-parent reconciliation commit recorded by this attempt."
        )
    gitlink_sha = manifest.get("reconcile_gitlink_sha")
    if gitlink_sha:
        tree_link = validate_sha(
            run_git_read(
                outer, ["rev-parse", f"{result}:{manifest['submodule']['path']}"]
            ).stdout.strip()
        )
        if tree_link != validate_sha(gitlink_sha):
            raise ReviewError(
                "Reconciliation result does not contain the automatically resolved gitlink."
            )
    return result


def _commit_reconciliation(
    state: ReviewState,
    pool: SourcePool,
    manifest: dict,
    outer: Path,
    parent_tip: str,
    main_sha: str,
    gitlink_path: str,
) -> str:
    if _merge_head(outer) != main_sha:
        raise ReviewError("MERGE_HEAD moved from the exact fetched reconciliation SHA.")
    if _conflict_paths(outer):
        raise ReviewError("Reconciliation conflicts remain unresolved.")
    unstaged = set(
        filter(None, run_git_read(outer, ["diff", "--name-only", "-z"]).stdout.split("\0"))
    )
    uninitialized_gitlinks = {
        relative
        for relative in _configured_gitlinks(outer)
        if not _canonical_source_initialized(outer, relative)
    }
    unstaged.difference_update(uninitialized_gitlinks)
    if unstaged:
        raise ReviewError(
            "Resolved reconciliation contains unstaged tracked changes; stage resolutions "
            "explicitly."
        )
    gitlink_sha = manifest.get("reconcile_gitlink_sha")
    if gitlink_sha and _index_gitlink(outer, gitlink_path) != validate_sha(gitlink_sha):
        raise ReviewError(
            "Selected gitlink must remain staged at its automatically resolved tracked tip."
        )
    unexpected = _unexpected_reconciliation_paths(outer, manifest, gitlink_path, None)
    if unexpected:
        raise ReviewError(
            "Staged reconciliation content includes paths outside the allowed "
            "review-owned surfaces or the recorded conflict resolutions: "
            + ", ".join(unexpected)
        )
    require_commit_identity(outer, operation="Reconcile")
    manifest["reconcile_phase"] = "reconcile-commit-intent"
    state.save_pool_run(manifest)
    with slot_writer_scope(state, pool.repo_key, manifest["slot_number"], phase="reconcile-commit"):
        run_git(
            outer,
            [
                "-c",
                "submodule.recurse=false",
                "commit",
                "-m",
                f"Reconcile parent main into review {manifest['branch']}",
                "-m",
                f"Review-Reconcile-Run: {manifest['run_id']}",
            ],
        )
    result = validate_sha(run_git_read(outer, ["rev-parse", "HEAD"]).stdout.strip())
    _verify_reconciled_commit(state, manifest, result)
    manifest["reconciled_tip"] = result
    manifest["checkpoint_sha"] = result
    manifest["reconcile_phase"] = "reconciled"
    state.save_pool_run(manifest)
    return result


def _reconcile_or_resume(
    state: ReviewState,
    pool: SourcePool,
    manifest: dict,
    outer: Path,
    *,
    max_depth: int,
) -> str:
    gitlink_path = manifest["submodule"]["path"]
    parent_tip = validate_sha(manifest["reconcile_parent_tip"])
    main_sha = validate_sha(manifest["reconcile_main_sha"])
    head = validate_sha(run_git_read(outer, ["rev-parse", "HEAD"]).stdout.strip())
    merge_head = _merge_head(outer)
    if head != parent_tip:
        if merge_head is not None:
            raise ReviewError("Outer review branch moved while MERGE_HEAD remains active.")
        if manifest.get("reconcile_phase") != "reconcile-commit-intent":
            raise ReviewError(
                "Outer review branch holds a commit the script did not record itself "
                "attempting; refusing to accept a manually created or externally "
                "advanced commit."
            )
        verified = _verify_reconciled_commit(state, manifest, head)
        unexpected = _unexpected_reconciliation_paths(outer, manifest, gitlink_path, verified)
        if unexpected:
            raise ReviewError(
                "Resumed reconciliation commit includes paths outside the allowed "
                "review-owned surfaces or the recorded conflict resolutions: "
                + ", ".join(unexpected)
            )
        manifest["reconciled_tip"] = verified
        manifest["checkpoint_sha"] = verified
        manifest["reconcile_phase"] = "reconciled"
        state.save_pool_run(manifest)
        return verified
    if merge_head is None:
        if git_status(outer):
            raise ReviewError("Outer review branch changed after reconciliation was reserved.")
        manifest["reconcile_phase"] = "reconcile-merge-intent"
        state.save_pool_run(manifest)
        with slot_writer_scope(
            state, pool.repo_key, manifest["slot_number"], phase="reconcile-merge"
        ):
            result = run_git(
                outer,
                ["-c", "submodule.recurse=false", "merge", "--no-ff", "--no-commit", main_sha],
                check=False,
            )
        merge_head = _merge_head(outer)
        if merge_head != main_sha:
            raise ReviewError(
                "Reconciliation merge did not establish the recorded exact MERGE_HEAD."
            )
        if result.returncode not in (0, 1):
            raise ReviewError(
                f"Git merge failed (exit {result.returncode}); reconciliation remains reserved."
            )
        manifest["reconcile_auto_merge_paths"] = _snapshot_auto_merge_paths(outer)
        state.save_pool_run(manifest)
        conflicts = set(_conflict_paths(outer))
        allowlist = _review_allowlist(manifest)
        if gitlink_path in conflicts:
            _, resolved_sha = fresh_tracking_tip(state, pool, manifest, max_depth=max_depth)
            with slot_writer_scope(
                state, pool.repo_key, manifest["slot_number"], phase="reconcile-gitlink"
            ):
                run_git(
                    outer,
                    ["update-index", "--add", "--cacheinfo", "160000", resolved_sha, gitlink_path],
                )
            if _index_gitlink(outer, gitlink_path) != resolved_sha:
                raise ReviewError(
                    "Selected gitlink did not verify at its freshly resolved tracked tip."
                )
            manifest["reconcile_gitlink_sha"] = resolved_sha
            state.save_pool_run(manifest)
            conflicts.discard(gitlink_path)
        manifest["reconcile_all_conflict_paths"] = sorted(conflicts)
        if conflicts:
            for path in conflicts:
                if not allowlist.allows(path):
                    raise ReviewError(
                        "Reconciliation conflict occurred outside the review-owned surfaces."
                    )
            manifest["reconcile_phase"] = "reconcile-conflicted"
            manifest["reconcile_conflict_paths"] = sorted(conflicts)
            state.save_pool_run(manifest)
            _reconcile_handoff(outer, pool, manifest, sorted(conflicts))
            raise ReviewError("Reconciliation conflicts require manual resolution.", CONFLICT_EXIT)
    elif merge_head != main_sha:
        raise ReviewError("A foreign merge replaced this reconciliation's MERGE_HEAD.")
    else:
        recorded = set(manifest.get("reconcile_conflict_paths") or [])
        current = set(_conflict_paths(outer))
        gitlink_sha = manifest.get("reconcile_gitlink_sha")
        if gitlink_sha:
            if gitlink_path in current:
                raise ReviewError(
                    "Selected gitlink conflict reappeared; it must not be edited manually."
                )
            if _index_gitlink(outer, gitlink_path) != validate_sha(gitlink_sha):
                raise ReviewError(
                    "Selected gitlink no longer matches its automatically resolved tracked tip."
                )
        unexpected = current - recorded
        if unexpected:
            raise ReviewError(
                "Unexpected new conflicts appeared; resolve only the originally listed paths."
            )
        remaining = current & recorded
        if remaining:
            manifest["reconcile_conflict_paths"] = sorted(remaining)
            state.save_pool_run(manifest)
            _reconcile_handoff(outer, pool, manifest, sorted(remaining))
            raise ReviewError("Reconciliation conflicts remain unresolved.", CONFLICT_EXIT)
    return _commit_reconciliation(state, pool, manifest, outer, parent_tip, main_sha, gitlink_path)


def _push_reconciled_tip(
    state: ReviewState, pool: SourcePool, manifest: dict, outer: Path, result: str
) -> str:
    ref = validate_parent_ref(state.root, f"refs/heads/{manifest['branch']}")
    expected_old = validate_sha(manifest["published_tip"])
    current_remote = read_parent_remote_ref(state.root, ref)
    if current_remote == result:
        # Exact after-state: a prior attempt already pushed this exact
        # reconciliation result but stopped before journaling it locally.
        pushed = result
    elif current_remote == expected_old:
        manifest["reconcile_phase"] = "reconcile-push-intent"
        state.save_pool_run(manifest)
        with slot_writer_scope(
            state, pool.repo_key, manifest["slot_number"], phase="reconcile-push"
        ):
            pushed = create_or_update_parent_remote_ref(
                outer, ref, result, expected_old=expected_old, writer_scope="slot"
            )
    else:
        raise ReviewError(
            "Parent review ref is neither at its expected prior published tip nor at "
            "this reconciliation's exact result; refusing to push."
        )
    manifest["published_tip"] = pushed
    manifest["reconciled_tip"] = pushed
    manifest["reconcile_phase"] = "reconciled-pushed"
    manifest["reconcile_completed_at"] = utc_now()
    state.save_pool_run(manifest)
    return pushed


def _reconcile_summary(pool: SourcePool, manifest: dict) -> dict:
    return {
        "repo": pool.repo_name,
        "slot": pool.slot_name(manifest["slot_number"]),
        "run_id": manifest["run_id"],
        "branch": manifest["branch"],
        "reconcile_phase": manifest.get("reconcile_phase"),
        "reconcile_main_sha": manifest.get("reconcile_main_sha"),
        "reconciled_tip": manifest.get("reconciled_tip"),
        "published_tip": manifest.get("published_tip"),
        "ready_for_root": manifest.get("reconcile_phase") == "reconciled-pushed",
        "note": (
            "Phase 2A slot reconciliation only. Canonical-root integration (Phase 2B) is "
            "not implemented by this action; parent main and the remote review ref's "
            "eventual deletion are untouched."
        ),
    }


def reconcile_review(
    root: Path,
    repo: str,
    slot: int,
    *,
    confirm: str | None,
    max_depth: int = 1000000,
    lock_timeout: float = 1200,
) -> dict:
    """Resume-safe Phase 2A slot reconciliation, used by `--complete`.

    Fetches exact parent `origin/main`, merges it into the outer review
    branch, auto-resolves only the selected gitlink, hands off any other
    conflicts with exit 4, then commits and pushes the reconciliation. Runs
    entirely under the target slot lock/writer -- never the canonical root
    lock. Does not implement Phase 2B (root integration/deletion).
    """
    state = ReviewState(root)
    _, pool = _module_and_pool(state, repo)
    slot = SourcePool._validate_slots([slot])[0]
    with lock_session(lock_timeout), state.lock(shared=True, phase="merge-review-complete"):
        with state.slot_lock(pool.repo_key, slot, phase="merge-review-complete"):
            check_prepared_slot_conflict(state, pool, slot)
            manifest = _validate_new_flow_manifest(
                state, pool, state.load_pool_run(pool.repo_key, slot), slot
            )
            if confirm != manifest["key"]:
                raise ReviewError(f"Complete requires --confirm {manifest['key']}.")
            if manifest.get("reconcile_phase") == "reconciled-pushed":
                return _reconcile_summary(pool, manifest)
            if not manifest.get("published_tip"):
                raise ReviewError(
                    "Publish the review before completing it; no published tip is recorded."
                )
            outer, _source = _outer_and_source(state, pool, manifest, attached=True)
            if manifest.get("reconcile_phase") is None:
                assert_no_git_operation(outer)
                head = validate_sha(run_git_read(outer, ["rev-parse", "HEAD"]).stdout.strip())
                if head != validate_sha(manifest["published_tip"]):
                    raise ReviewError(
                        "Outer review branch moved from its published tip; publish again "
                        "before completing."
                    )
                if git_status(outer):
                    raise ReviewError(
                        "Outer review branch has unexpected local changes; refusing to "
                        "reconcile."
                    )
                manifest["reconcile_parent_tip"] = head
                manifest["reconcile_phase"] = "reconcile-fetch-intent"
                state.save_pool_run(manifest)
            if manifest["reconcile_phase"] == "reconcile-fetch-intent":
                with slot_writer_scope(state, pool.repo_key, slot, phase="reconcile-fetch"):
                    main_sha = _fetch_parent_main_tip(state, pool, slot, outer)
                manifest["reconcile_main_sha"] = main_sha
                manifest["reconcile_phase"] = "reconcile-merge-intent"
                state.save_pool_run(manifest)
            result = _reconcile_or_resume(state, pool, manifest, outer, max_depth=max_depth)
            if manifest.get("reconcile_phase") != "reconciled-pushed":
                _push_reconciled_tip(state, pool, manifest, outer, result)
            return _reconcile_summary(pool, manifest)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="Canonical source repository name, for example Nova.")
    parser.add_argument("--slot", required=True, type=int, help="Repository-local pool slot number, 0 through 5.")
    parser.add_argument("--root", help="Canonical review root (normally inferred from this script).")
    parser.add_argument("--dry-run", action="store_true", help="Inspect selection and active transaction without mutation or network.")
    parser.add_argument("--max-depth", type=int, default=1000000)
    parser.add_argument("--history-timeout", type=float, default=300)
    parser.add_argument("--lock-timeout", type=float, default=1200)
    parser.add_argument("--abort", action="store_true", help="Abort this exact active merge and preserve its checkpoint.")
    parser.add_argument("--publish", action="store_true", help="Commit only allowed review-owned content and push it to the parent review ref.")
    parser.add_argument("--complete", action="store_true", help="Run or resume Phase 2A slot reconciliation against parent main (does not integrate canonical root).")
    parser.add_argument("--confirm", help="Required review key for --abort, --publish, or --complete.")
    parser.add_argument("--recover-inspect", action="store_true", help="Print immutable evidence for a pending root-writer recovery.")
    parser.add_argument("--recover-assert", help="JSON file containing an exact operator recovery assertion.")
    args = parser.parse_args(argv)
    actions = sum(
        (args.abort, args.publish, args.complete, args.recover_inspect, bool(args.recover_assert))
    )
    if actions > 1 or args.dry_run and actions:
        parser.error(
            "Choose only one of --dry-run, --abort, --publish, --complete, --recover-inspect, "
            "or --recover-assert."
        )
    try:
        preflight()
        root = discover_root(args.root)
        if args.abort:
            print(
                "WARNING: abort abandons staged and worktree conflict resolutions in the canonical root; the review checkpoint and retention refs are preserved.",
                file=sys.stderr,
            )
            result = abort_merge(
                root,
                args.repo,
                args.slot,
                confirm=args.confirm,
                lock_timeout=args.lock_timeout,
            )
        elif args.publish:
            result = publish_review(
                root,
                args.repo,
                args.slot,
                confirm=args.confirm,
                lock_timeout=args.lock_timeout,
            )
        elif args.complete:
            result = reconcile_review(
                root,
                args.repo,
                args.slot,
                confirm=args.confirm,
                max_depth=args.max_depth,
                lock_timeout=args.lock_timeout,
            )
        elif args.recover_inspect:
            result = inspect_recovery(
                root, args.repo, args.slot, lock_timeout=args.lock_timeout
            )
        elif args.recover_assert:
            assertion_path = Path(args.recover_assert)
            if assertion_path.is_symlink() or not assertion_path.is_file():
                raise ReviewError("Recovery assertion must be a regular JSON file.")
            result = apply_recovery(
                root,
                args.repo,
                args.slot,
                json.loads(assertion_path.read_text(encoding="utf-8")),
                lock_timeout=args.lock_timeout,
            )
        else:
            result = merge_review(
                root,
                args.repo,
                args.slot,
                dry_run=args.dry_run,
                max_depth=args.max_depth,
                history_timeout=args.history_timeout,
                lock_timeout=args.lock_timeout,
            )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except ReviewError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return exc.code
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(
            f"ERROR: local merge state is invalid or unavailable ({type(exc).__name__}).",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
