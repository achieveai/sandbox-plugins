"""Offline context tests: fake provider responses and real temporary Git history."""
from __future__ import annotations

import copy
import importlib
import importlib.util
import json
import os
from pathlib import Path
from unittest.mock import patch
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
SHA_A, SHA_B, SHA_C = "a" * 40, "b" * 40, "c" * 40


class FakeAdo:
    org_url = "https://dev.azure.com/Example"

    def __init__(self, pr=None, iterations=None, linked=None, items=None, matches=None):
        self.pr = pr or {
            "pullRequestId": 42,
            "repository": {"id": "repo-id", "name": "Nova", "remoteUrl": "https://dev.azure.com/Example/Project/_git/Nova",
                           "project": {"id": "project-id", "name": "Project"}},
            "sourceRefName": "refs/heads/topic", "targetRefName": "refs/heads/dev",
            "title": "Example PR", "description": "Current description", "status": "active",
            "createdBy": {"displayName": "Developer"},
        }
        self.iterations = iterations if iterations is not None else [
            {"id": 1, "sourceRefCommit": {"commitId": SHA_A}, "targetRefCommit": {"commitId": SHA_B},
             "commonRefCommit": {"commitId": SHA_C}},
        ]
        self.linked = linked or []
        self.items = items or {}
        self.matches = matches or {}
        self.calls = []

    def request(self, method, path, body=None):
        self.calls.append((method, path, copy.deepcopy(body)))
        endpoint = path.split("?", 1)[0]
        if endpoint.endswith("/iterations"):
            return {"value": copy.deepcopy(self.iterations)}
        if endpoint.endswith("/workitems"):
            return {"value": [{"id": str(i)} for i in self.linked]}
        if endpoint.endswith("/workitemsbatch"):
            return {"value": [copy.deepcopy(self.items[i]) for i in body["ids"] if i in self.items]}
        if endpoint.endswith("/pullrequestquery"):
            return {"results": [{sha: copy.deepcopy(self.matches.get(sha, [])) for sha in query["items"]}
                                for query in body["queries"]]}
        if "/pullrequests/" in endpoint.lower():
            number = int(endpoint.rsplit("/", 1)[1])
            if number == 42:
                return copy.deepcopy(self.pr)
            return {"pullRequestId": number, "title": f"Historical {number}", "description": f"Description {number}",
                    "status": "completed", "repository": copy.deepcopy(self.pr["repository"])}
        raise AssertionError(f"Unexpected request {method} {path}")

    def get_pages(self, path):
        return self.request("GET", path)["value"]


def item(number, kind="Task", parents=(), children=(), related=()):
    relations = []
    for rel, ids in (("Hierarchy-Reverse", parents), ("Hierarchy-Forward", children), ("Related", related)):
        relations.extend({"rel": "System.LinkTypes." + rel,
                          "url": f"https://dev.azure.com/Example/_apis/wit/workItems/{i}"} for i in ids)
    return {"id": number, "rev": 2, "fields": {"System.Title": f"Item {number}", "System.WorkItemType": kind,
            "System.State": "Active", "System.TeamProject": "Different Project" if number % 2 else "Project",
            "System.Description": "<p>Business context</p>",
            "Microsoft.VSTS.Common.AcceptanceCriteria": "<p>Expected outcome</p>"}, "relations": relations}


def fixture_temp_root():
    job_directory = os.environ.get("CLAUDE_JOB_DIR")
    if job_directory:
        candidate = Path(job_directory) / "tmp"
        if candidate.is_dir():
            return candidate
    return Path(tempfile.gettempdir())


class FixtureEnvironmentTests(unittest.TestCase):
    def test_without_job_directory_uses_existing_tmpdir(self):
        with tempfile.TemporaryDirectory(dir=fixture_temp_root()) as directory:
            with patch.dict(os.environ, {"TMPDIR": directory}, clear=True), patch.object(tempfile, "tempdir", None):
                self.assertEqual(fixture_temp_root(), Path(directory))
                with tempfile.TemporaryDirectory(dir=fixture_temp_root()) as child:
                    self.assertEqual(Path(child).parent, Path(directory))

    def test_missing_job_directory_falls_back_to_tmpdir(self):
        with tempfile.TemporaryDirectory(dir=fixture_temp_root()) as directory:
            with patch.dict(os.environ, {"CLAUDE_JOB_DIR": str(Path(directory) / "absent"), "TMPDIR": directory}, clear=True), patch.object(tempfile, "tempdir", None):
                self.assertEqual(fixture_temp_root(), Path(directory))


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec("review_context"), "review_context module must exist")
        self.context = importlib.import_module("review_context")

    def test_resolve_uses_org_lookup_and_highest_iteration(self):
        client = FakeAdo(iterations=[
            {"id": 3, "sourceRefCommit": {"commitId": SHA_B}, "targetRefCommit": {"commitId": SHA_C}, "commonRefCommit": {"commitId": SHA_A}},
            {"id": 1, "sourceRefCommit": {"commitId": SHA_A}, "targetRefCommit": {"commitId": SHA_B}},
        ])
        result = self.context.resolve_pr(client, 42)
        self.assertEqual(result["iteration_id"], 3)
        self.assertEqual(result["head_sha"], SHA_B)
        self.assertEqual(result["base_sha"], SHA_A)
        self.assertEqual(client.calls[0][1], "_apis/git/pullrequests/42?api-version=7.1")
        self.assertEqual(result["repository"]["name"], "Nova")
        self.assertEqual(result["source_ref"], "refs/heads/topic")

    def test_provider_org_username_is_removed_before_snapshot_persistence(self):
        client = FakeAdo()
        client.pr["repository"]["remoteUrl"] = "https://Example@dev.azure.com/Example/Project/_git/Nova"
        result = self.context.resolve_pr(client, 42)
        self.assertEqual(result["repository"]["url"], "https://dev.azure.com/example/Project/_git/Nova")
        self.assertNotIn("@", result["repository"]["url"])

    def test_provider_other_username_or_password_is_rejected(self):
        for url in ("https://somebody@dev.azure.com/Example/Project/_git/Nova",
                    "https://Example:password@dev.azure.com/Example/Project/_git/Nova",
                    "https://Example:@dev.azure.com/Example/Project/_git/Nova"):
            with self.subTest(url=url.split("@")[-1]):
                client = FakeAdo()
                client.pr["repository"]["remoteUrl"] = url
                with self.assertRaises(Exception):
                    self.context.resolve_pr(client, 42)

    def test_historical_iteration_does_not_use_current_branch_tip(self):
        client = FakeAdo()
        client.pr["lastMergeSourceCommit"] = {"commitId": "d" * 40}
        result = self.context.resolve_pr(client, 42, iteration=1)
        self.assertEqual(result["head_sha"], SHA_A)
        self.assertEqual(result["target_sha"], SHA_B)
        self.assertEqual(result["base_sha"], SHA_C)

    def test_missing_iteration_and_invalid_sha_fail(self):
        with self.assertRaises(Exception):
            self.context.resolve_pr(FakeAdo(), 42, iteration=99)
        client = FakeAdo(iterations=[{"id": 1, "sourceRefCommit": {"commitId": "--upload-pack=bad"},
                                     "targetRefCommit": {"commitId": SHA_B}}])
        with self.assertRaises(Exception):
            self.context.resolve_pr(client, 42)

    def test_work_item_tree_includes_all_siblings_and_cross_project_children(self):
        children = list(range(10, 35))
        items = {1: item(1, "Feature", parents=[99], children=children), 99: item(99, "Epic", children=[1])}
        items.update({i: item(i, "User Story", parents=[1], children=[100] if i == 10 else [], related=[999]) for i in children})
        items[100] = item(100, parents=[10])
        client = FakeAdo(linked=[100], items=items)
        tree = self.context.collect_work_items(client, self.context.resolve_pr(client, 42))
        self.assertTrue(tree["complete"])
        self.assertEqual(tree["roots"], [1])
        self.assertEqual({node["id"] for node in tree["nodes"]}, {1, 100, *children})
        self.assertFalse(any(999 in (body or {}).get("ids", []) for _, _, body in client.calls))
        self.assertFalse(any(99 in (body or {}).get("ids", []) for _, _, body in client.calls))
        batches = [call for call in client.calls if call[1].startswith("_apis/wit/workitemsbatch")]
        self.assertTrue(batches)
        self.assertTrue(all(len(call[2]["ids"]) <= 200 for call in batches))
        self.assertTrue(any(edge["kind"] == "related" for edge in tree["edges"]))

    def test_missing_parent_produces_partial_highest_available_root(self):
        client = FakeAdo(linked=[3], items={3: item(3, parents=[2]), 2: item(2, "User Story", parents=[1], children=[3])})
        tree = self.context.collect_work_items(client, self.context.resolve_pr(client, 42))
        self.assertFalse(tree["complete"])
        self.assertEqual(tree["roots"], [2])
        self.assertIn(1, tree["missing_ids"])

    def test_parent_cycle_terminates_and_marks_partial(self):
        client = FakeAdo(linked=[1], items={1: item(1, parents=[2], children=[2]), 2: item(2, parents=[1], children=[1])})
        tree = self.context.collect_work_items(client, self.context.resolve_pr(client, 42))
        self.assertFalse(tree["complete"])
        self.assertTrue(any("cycle" in warning.lower() for warning in tree["warnings"]))
        self.assertLess(len(client.calls), 10)

    def test_work_item_limit_is_explicit_not_silent(self):
        client = FakeAdo(linked=[1], items={1: item(1, "Feature", children=[2, 3]), 2: item(2), 3: item(3)})
        tree = self.context.collect_work_items(client, self.context.resolve_pr(client, 42), max_work_items=2)
        self.assertFalse(tree["complete"])
        self.assertLessEqual(len(tree["nodes"]), 2)
        self.assertTrue(any("limit" in warning.lower() for warning in tree["warnings"]))

    def test_multiple_roots_and_no_linked_items(self):
        client = FakeAdo(linked=[1, 2], items={1: item(1), 2: item(2, "Feature")})
        tree = self.context.collect_work_items(client, self.context.resolve_pr(client, 42))
        self.assertEqual(tree["roots"], [1, 2])
        empty = self.context.collect_work_items(FakeAdo(), self.context.resolve_pr(FakeAdo(), 42))
        self.assertTrue(empty["complete"])
        self.assertEqual(empty["nodes"], [])

    def test_diff_protocol_uses_only_lf_for_line_boundaries(self):
        patch = "@@ -1,3 +1,3 @@\n line one\n-old\n+new\v+not_a_git_diff_line\n line three\n"
        base, added = self.context._parse_diff_ranges(patch)
        self.assertEqual(base, [[1, 3]])
        self.assertEqual(added, [[2, 2]])

    def test_child_cycle_below_feature_is_explicit(self):
        client = FakeAdo(linked=[1], items={1: item(1, "Feature", children=[2]), 2: item(2, children=[3]), 3: item(3, children=[2])})
        tree = self.context.collect_work_items(client, self.context.resolve_pr(client, 42))
        self.assertFalse(tree["complete"])
        self.assertTrue(any("child hierarchy cycle" in warning for warning in tree["warnings"]))

    def test_linked_item_and_batch_failures_are_partial_not_empty_success(self):
        from review_common import ReviewError
        client = FakeAdo()
        snapshot = self.context.resolve_pr(client, 42)
        client.get_pages = lambda path: (_ for _ in ()).throw(ReviewError("credential=must-not-appear"))
        result = self.context.collect_work_items(client, snapshot)
        self.assertFalse(result["complete"])
        self.assertNotIn("must-not-appear", json.dumps(result))
        client = FakeAdo(linked=[1])
        request = client.request
        def fail_batch(method, path, body=None):
            if "/workitemsbatch" in path:
                raise ReviewError("credential=must-not-appear")
            return request(method, path, body)
        client.request = fail_batch
        result = self.context.collect_work_items(client, snapshot)
        self.assertFalse(result["complete"])
        self.assertEqual(result["missing_ids"], [1])
        self.assertNotIn("must-not-appear", json.dumps(result))

    def test_work_item_batches_are_chunked_at_200(self):
        children = list(range(2, 403))
        client = FakeAdo(linked=[1], items={1: item(1, "Feature", children=children), **{i: item(i, parents=[1]) for i in children}})
        result = self.context.collect_work_items(client, self.context.resolve_pr(client, 42))
        self.assertTrue(result["complete"])
        self.assertEqual(len(result["nodes"]), 402)
        batches = [body["ids"] for _, path, body in client.calls if "/workitemsbatch" in path]
        self.assertEqual([len(ids) for ids in batches], [1, 200, 200, 1])

    def test_commit_query_errors_do_not_echo_provider_secrets(self):
        from review_common import ReviewError
        client = FakeAdo()
        snapshot = self.context.resolve_pr(client, 42)
        client.request = lambda *args, **kwargs: (_ for _ in ()).throw(ReviewError("token=must-not-appear"))
        result = self.context.resolve_origin_prs(client, snapshot, [SHA_A])
        self.assertFalse(result["complete"])
        self.assertEqual(result["commits"][SHA_A]["status"], "query-failed")
        self.assertNotIn("must-not-appear", json.dumps(result))

    def test_commit_queries_keep_multiple_matches_and_fetch_full_descriptions(self):
        client = FakeAdo(matches={SHA_A: [{"pullRequestId": 10}, {"pullRequestId": 11}]})
        result = self.context.resolve_origin_prs(client, self.context.resolve_pr(client, 42), [SHA_A, SHA_A, SHA_B])
        self.assertEqual(result["commits"][SHA_A]["pr_ids"], [10, 11])
        self.assertEqual(result["commits"][SHA_B]["status"], "no-associated-pr")
        self.assertEqual(result["pull_requests"]["10"]["description"], "Description 10")
        query = next(body for _, path, body in client.calls if "/pullrequestquery" in path)
        self.assertEqual({q["type"] for q in query["queries"]}, {"commit", "lastMergeCommit"})
        self.assertEqual(len(query["queries"][0]["items"]), 2)

    def test_commit_query_budget_marks_unqueried_commits(self):
        client = FakeAdo()
        result = self.context.resolve_origin_prs(client, self.context.resolve_pr(client, 42), [SHA_A, SHA_B], max_commits=1)
        self.assertFalse(result["complete"])
        self.assertEqual(result["commits"][SHA_B]["status"], "not-queried-limit")

    def test_markdown_never_executes_or_renders_raw_html(self):
        report = {"snapshot": self.context.resolve_pr(FakeAdo(), 42), "work_items": {"roots": [1], "nodes": [item(1)], "edges": []},
                  "files": [], "provenance": {}, "warnings": ["Missing something"], "complete": False}
        report["snapshot"]["title"] = "<script>evil</script>\n# fake heading"
        markdown = self.context.render_markdown(report)
        self.assertNotIn("<script>", markdown)
        self.assertIn("Missing something", markdown)
        self.assertIn("partial", markdown.lower())


class GitContextTests(unittest.TestCase):
    def setUp(self):
        self.context = importlib.import_module("review_context")
        self.temp = tempfile.TemporaryDirectory(prefix="context-git-", dir=fixture_temp_root())
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git("init", "-q")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.test")

    def git(self, *args):
        result = subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True, text=True)
        return result.stdout.strip()

    def commit(self, message):
        self.git("add", "--all")
        self.git("commit", "-qm", message)
        return self.git("rev-parse", "HEAD")

    def test_changed_region_blame_and_added_lines(self):
        path = self.root / "sample.txt"
        path.write_text("".join(f"old {i}\n" for i in range(1, 21)))
        base = self.commit("base")
        lines = path.read_text().splitlines(True)
        lines[9] = "replacement\n"
        path.write_text("".join(lines))
        head = self.commit("change")
        data = self.context.collect_file_changes(self.root, base, head, current_pr=42)
        record = data["files"][0]
        self.assertTrue(data["complete"])
        self.assertEqual(record["base_ranges"], [[7, 13]])
        self.assertEqual({line["commit"] for line in record["blame"]}, {base})
        self.assertEqual(record["added_ranges"], [[10, 10]])
        self.assertEqual(record["added_lines_pr"], 42)
        self.assertEqual(len(record["blame"]), 7)

    def test_rename_delete_new_binary_and_odd_paths(self):
        old_name = "old name\tquoted\".txt"
        (self.root / old_name).write_text("a\nb\nc\nd\ne\nf\ng\nh\ni\nj\n")
        (self.root / "deleted.txt").write_text("gone\n")
        (self.root / "binary.dat").write_bytes(b"\x00before")
        base = self.commit("base")
        self.git("mv", old_name, "renamed\nfile.txt")
        (self.root / "deleted.txt").unlink()
        (self.root / "new.txt").write_text("brand new\n")
        (self.root / "binary.dat").write_bytes(b"\x00after")
        head = self.commit("change")
        data = self.context.collect_file_changes(self.root, base, head, current_pr=42)
        by_path = {entry["path"]: entry for entry in data["files"]}
        self.assertEqual(by_path["renamed\nfile.txt"]["old_path"], old_name)
        self.assertEqual(by_path["new.txt"]["blame"], [])
        self.assertEqual(by_path["binary.dat"]["provenance_status"], "not-applicable-binary")
        self.assertEqual(by_path["deleted.txt"]["blame"][0]["commit"], base)
        self.assertTrue(data["complete"])

    def test_literal_git_pathspec_characters_do_not_select_other_files(self):
        name = "*.txt"
        (self.root / name).write_text("a\nb\nc\n")
        (self.root / "other.txt").write_text("x\ny\nz\n")
        base = self.commit("base")
        (self.root / name).write_text("a\nchanged\nc\n")
        (self.root / "other.txt").write_text("x\nother changed\nz\n")
        head = self.commit("change")
        data = self.context.collect_file_changes(self.root, base, head, current_pr=42)
        selected = next(entry for entry in data["files"] if entry["path"] == name)
        self.assertTrue(data["complete"])
        self.assertEqual(len(selected["blame"]), 3)
        self.assertEqual({line["original_path"] for line in selected["blame"]}, {name})
        self.assertEqual(selected["added_ranges"], [[2, 2]])

    def test_blame_does_not_execute_textconv(self):
        name = "file.bin"
        (self.root / name).write_text("one\ntwo\nthree\n")
        (self.root / ".gitattributes").write_text("*.bin diff=trap\n")
        base = self.commit("base")
        (self.root / name).write_text("one\nchanged\nthree\n")
        head = self.commit("head")
        self.git("config", "diff.trap.textconv", "false")
        report = self.context.collect_file_changes(self.root, base, head, current_pr=42)
        self.assertTrue(report["complete"])
        self.assertEqual(len(report["files"][0]["blame"]), 3)

    def test_carriage_return_and_unicode_separator_paths_remain_literal(self):
        name = "carriage\rreturn .txt"
        (self.root / name).write_bytes(b"one\ntwo\nthree\n")
        base = self.commit("base")
        (self.root / name).write_bytes(b"one\nmodified\nthree\n")
        head = self.commit("head")
        report = self.context.collect_file_changes(self.root, base, head, current_pr=42)
        self.assertTrue(report["complete"])
        self.assertEqual(report["files"][0]["path"], name)
        self.assertEqual(report["files"][0]["added_ranges"], [[2, 2]])
        self.assertEqual({line["original_path"] for line in report["files"][0]["blame"]}, {name})

    def test_context_collection_links_git_lines_to_provider_prs(self):
        (self.root / "f.txt").write_text("a\nb\nc\n")
        base = self.commit("base")
        (self.root / "f.txt").write_text("a\nchanged\nc\n")
        head = self.commit("head")
        client = FakeAdo(iterations=[{"id": 1, "sourceRefCommit": {"commitId": head},
                                    "targetRefCommit": {"commitId": base}, "commonRefCommit": {"commitId": base}}],
                         matches={base: [{"pullRequestId": 10}]})
        snapshot = self.context.resolve_pr(client, 42)
        report = self.context.collect_context(client, snapshot, self.root, base)
        self.assertTrue(report["complete"])
        self.assertEqual(report["files"][0]["origin_pr_ids"], [10])
        self.assertEqual(report["provenance"]["pull_requests"]["10"]["description"], "Description 10")
        self.assertIn("PR #10", self.context.render_markdown(report))

    def test_git_classified_addition_does_not_invent_rename_history(self):
        (self.root / "old.txt").write_text("".join(f"line {i}\n" for i in range(1, 31)))
        base = self.commit("base")
        self.git("mv", "old.txt", "renamed.txt")
        moved = (self.root / "renamed.txt").read_text().replace("line 15\n", "changed 15\n")
        (self.root / "renamed.txt").write_text(moved)
        (self.root / "old.txt").write_text("replacement file\n")
        head = self.commit("rename and replacement")
        data = self.context.collect_file_changes(self.root, base, head, current_pr=42)
        renamed = next(entry for entry in data["files"] if entry["path"] == "renamed.txt")
        self.assertTrue(data["complete"])
        # Git -M classifies this as modification + addition, not rename: do not invent a pairing.
        self.assertEqual(renamed["status"], "A")
        self.assertEqual(renamed["base_ranges"], [])
        self.assertEqual(renamed["added_ranges"], [[1, 30]])
        self.assertEqual(renamed["blame"], [])

    def test_empty_submodule_directory_does_not_fall_back_to_parent_git(self):
        (self.root / "tracked.txt").write_text("base\n")
        commit = self.commit("base")
        empty = self.root / "repos" / "Empty"
        empty.mkdir(parents=True)
        with self.assertRaisesRegex(Exception, "own Git worktree"):
            self.context.collect_file_changes(empty, commit, commit, current_pr=42)

    def test_shallow_boundary_is_not_reported_as_original_commit(self):
        source = self.root
        (source / "f.txt").write_text("a\nb\nc\n")
        self.commit("old")
        (source / "other.txt").write_text("another\n")
        base = self.commit("base")
        clone = source / "shallow"
        subprocess.run(["git", "clone", "-q", "--depth=1", source.as_uri(), str(clone)], check=True, capture_output=True)
        (clone / "f.txt").write_text("a\nchanged\nc\n")
        subprocess.run(["git", "-C", str(clone), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit", "-qam", "head"], check=True, capture_output=True)
        head = subprocess.run(["git", "-C", str(clone), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
        data = self.context.collect_file_changes(clone, base, head, current_pr=42)
        self.assertFalse(data["complete"])
        self.assertTrue(any(line["shallow_boundary"] for line in data["files"][0]["blame"]))
        self.assertNotIn(base, data["historical_commits"])


if __name__ == "__main__":
    unittest.main()
