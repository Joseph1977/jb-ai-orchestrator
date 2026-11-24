from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import repo_guard


class RepoGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.external_directory = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary_directory.name)
        self.git("init", "-q")
        self.git("config", "user.name", "Repo Guard Test")
        self.git("config", "user.email", "repo-guard@example.invalid")
        (self.repo / ".gitignore").write_text(
            ".env\nlogs/\n.venv/\n", encoding="utf-8"
        )
        (self.repo / "tracked.txt").write_text("tracked\n", encoding="utf-8")
        (self.repo / "plan.md").write_text("plan v1\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-qm", "initial")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()
        self.external_directory.cleanup()

    def git(self, *args: str) -> None:
        subprocess.run(
            ["git", "-C", str(self.repo), *args],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def snapshot(self) -> dict:
        return repo_guard.capture(str(self.repo), "plan.md")

    def assert_changed(self, before: dict) -> dict:
        result = repo_guard.compare(before, self.snapshot())
        self.assertFalse(result["same"], json.dumps(result, indent=2))
        return result

    def test_unchanged_dirty_repo_matches_and_allowed_file_is_ignored(self) -> None:
        (self.repo / "tracked.txt").write_text("pre-existing dirty\n", encoding="utf-8")
        before = self.snapshot()

        self.assertTrue(repo_guard.compare(before, self.snapshot())["same"])
        (self.repo / "plan.md").write_text("plan v2\n", encoding="utf-8")
        self.assertTrue(repo_guard.compare(before, self.snapshot())["same"])

    def test_ignored_files_and_directories_are_detected(self) -> None:
        (self.repo / ".env").write_text("TOKEN=before\n", encoding="utf-8")
        (self.repo / "logs").mkdir()
        (self.repo / "logs" / "service.log").write_text("before\n", encoding="utf-8")
        (self.repo / ".venv").mkdir()
        (self.repo / ".venv" / "marker").write_text("before\n", encoding="utf-8")
        before = self.snapshot()

        (self.repo / ".env").write_text("TOKEN=after\n", encoding="utf-8")
        (self.repo / "logs" / "service.log").write_text("after\n", encoding="utf-8")
        (self.repo / ".venv" / "marker").write_text("after\n", encoding="utf-8")

        result = self.assert_changed(before)
        self.assertEqual(
            result["changed"], [".env", ".venv/marker", "logs/service.log"]
        )

    def test_preexisting_ignored_dirt_is_allowed_when_unchanged(self) -> None:
        (self.repo / ".env").write_text("pre-existing\n", encoding="utf-8")
        before = self.snapshot()

        self.assertTrue(repo_guard.compare(before, self.snapshot())["same"])

    def test_symlink_target_change_is_detected(self) -> None:
        os.symlink("first", self.repo / "link")
        before = self.snapshot()
        (self.repo / "link").unlink()
        os.symlink("second", self.repo / "link")

        self.assertEqual(self.assert_changed(before)["changed"], ["link"])

    def test_owned_file_replaced_by_symlink_does_not_hide_target_change(self) -> None:
        before = self.snapshot()
        (self.repo / "plan.md").unlink()
        os.symlink("tracked.txt", self.repo / "plan.md")
        (self.repo / "plan.md").write_text("changed through link\n", encoding="utf-8")

        self.assertEqual(self.assert_changed(before)["changed"], ["tracked.txt"])

    def test_assume_unchanged_content_change_is_detected(self) -> None:
        self.git("update-index", "--assume-unchanged", "tracked.txt")
        before = self.snapshot()
        (self.repo / "tracked.txt").write_text("changed\n", encoding="utf-8")

        self.assertEqual(self.assert_changed(before)["changed"], ["tracked.txt"])

    def test_skip_worktree_content_change_is_detected(self) -> None:
        self.git("update-index", "--skip-worktree", "tracked.txt")
        before = self.snapshot()
        (self.repo / "tracked.txt").write_text("changed\n", encoding="utf-8")

        self.assertEqual(self.assert_changed(before)["changed"], ["tracked.txt"])

    def test_head_branch_and_index_changes_are_detected(self) -> None:
        before = self.snapshot()
        self.git("checkout", "-qb", "other")
        (self.repo / "tracked.txt").write_text("staged\n", encoding="utf-8")
        self.git("add", "tracked.txt")
        self.git("commit", "-qm", "other")

        result = self.assert_changed(before)
        self.assertEqual(
            set(result["git"]),
            {"branch", "control", "head", "index_sha256", "refs_sha256"},
        )

    def test_index_flag_change_is_detected(self) -> None:
        before = self.snapshot()
        self.git("update-index", "--assume-unchanged", "tracked.txt")

        result = self.assert_changed(before)
        self.assertEqual(set(result["git"]), {"control", "flags_sha256"})

    def test_repository_config_change_is_detected(self) -> None:
        before = self.snapshot()
        self.git("config", "plan-review.changed", "true")

        result = self.assert_changed(before)
        self.assertEqual(set(result["git"]), {"control"})

    def test_repository_hook_change_is_detected(self) -> None:
        hook = self.repo / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        before = self.snapshot()
        hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")

        result = self.assert_changed(before)
        self.assertEqual(set(result["git"]), {"control"})

    def test_arbitrary_git_directory_change_is_detected(self) -> None:
        before = self.snapshot()
        marker = self.repo / ".git" / "unexpected-state"
        marker.write_text("changed\n", encoding="utf-8")

        result = self.assert_changed(before)
        self.assertEqual(set(result["git"]), {"control"})
        self.assertIn(str(marker.resolve()), result["git"]["control"]["added"])

    def test_external_hooks_path_change_is_detected(self) -> None:
        hooks = Path(self.external_directory.name)
        hook = hooks / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.git("config", "core.hooksPath", str(hooks))
        before = self.snapshot()
        hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")

        result = self.assert_changed(before)
        self.assertEqual(set(result["git"]), {"control"})

    def test_linked_worktree_git_pointer_is_snapshotted(self) -> None:
        linked = Path(self.external_directory.name) / "linked"
        self.git("worktree", "add", "-q", "-b", "linked", str(linked))

        manifest = repo_guard.capture(str(linked), "plan.md")

        self.assertIn(
            str(linked.resolve() / ".git"),
            manifest["git"]["control"]["entries"],
        )

    def test_manifest_integrity_is_enforced(self) -> None:
        manifest = self.snapshot()
        path = self.repo / "baseline.json"
        digest = repo_guard.write_manifest(manifest, str(path))
        self.assertEqual(repo_guard.load_manifest(str(path), digest), manifest)

        path.write_text("{}\n", encoding="utf-8")
        with self.assertRaises(repo_guard.GuardError):
            repo_guard.load_manifest(str(path), digest)

    def test_cli_capture_and_compare_report_ignored_change(self) -> None:
        script = Path(repo_guard.__file__)
        with tempfile.NamedTemporaryFile() as baseline:
            captured = subprocess.run(
                [
                    "python3",
                    str(script),
                    "capture",
                    "--repo",
                    str(self.repo),
                    "--allow",
                    "plan.md",
                    "--output",
                    baseline.name,
                ],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            digest = captured.stdout.strip()
            (self.repo / ".env").write_text("changed\n", encoding="utf-8")
            compared = subprocess.run(
                [
                    "python3",
                    str(script),
                    "compare",
                    "--repo",
                    str(self.repo),
                    "--allow",
                    "plan.md",
                    "--baseline",
                    baseline.name,
                    "--baseline-sha256",
                    digest,
                ],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

        self.assertEqual(compared.returncode, 1, compared.stderr)
        self.assertEqual(json.loads(compared.stdout)["added"], [".env"])

    @staticmethod
    def recovery_marker() -> str:
        digest = "a" * 64
        return (
            "Recovery: skill-integrity-failed "
            f"phase=skill-restored baseline={digest} current={digest} failure=none "
            "commit=none pre-head=unknown repo-check=pending prior=none "
            f"guard=/tmp/guard guard-sha={digest} "
            "commit-manifest=none commit-manifest-sha=none\n"
        )

    def delayed_snapshot(self) -> tuple[dict, dict]:
        status = self.repo / "status.md"
        status.write_text("State: reviewer-running\n", encoding="utf-8")
        before = self.snapshot()
        status.write_text(
            "State: reviewer-running\n" + self.recovery_marker(),
            encoding="utf-8",
        )
        after = repo_guard.capture(
            str(self.repo), "plan.md", recovery_status_value="status.md"
        )
        return before, after

    def test_delayed_compare_normalizes_only_recovery_marker(self) -> None:
        before, after = self.delayed_snapshot()

        result = repo_guard.compare(before, after, semantic_git=True)

        self.assertTrue(result["same"], json.dumps(result, indent=2))

    def test_delayed_compare_preserves_original_missing_newline(self) -> None:
        status = self.repo / "status.md"
        status.write_text("State: reviewer-running", encoding="utf-8")
        before = self.snapshot()
        status.write_text(
            "State: reviewer-running\n" + self.recovery_marker(),
            encoding="utf-8",
        )
        after = repo_guard.capture(
            str(self.repo), "plan.md", recovery_status_value="status.md"
        )

        result = repo_guard.compare(before, after, semantic_git=True)

        self.assertTrue(result["same"], json.dumps(result, indent=2))

    def test_delayed_compare_rejects_extra_status_change(self) -> None:
        before, _after = self.delayed_snapshot()
        status = self.repo / "status.md"
        status.write_text(
            "State: changed\n" + self.recovery_marker(),
            encoding="utf-8",
        )
        after = repo_guard.capture(
            str(self.repo), "plan.md", recovery_status_value="status.md"
        )

        result = repo_guard.compare(before, after, semantic_git=True)

        self.assertFalse(result["same"])
        self.assertEqual(result["changed"], ["status.md"])

    def test_delayed_capture_rejects_malformed_or_duplicate_marker(self) -> None:
        status = self.repo / "status.md"
        for text in (
            "Recovery: skill-integrity-failed malformed\n",
            self.recovery_marker() + self.recovery_marker(),
            self.recovery_marker().replace(
                "failure=none", "failure=planted-marker"
            ),
        ):
            status.write_text(text, encoding="utf-8")
            with self.assertRaises(repo_guard.GuardError):
                repo_guard.capture(
                    str(self.repo), "plan.md", recovery_status_value="status.md"
                )

    def test_delayed_compare_ignores_raw_git_bytes(self) -> None:
        before, _after = self.delayed_snapshot()
        (self.repo / ".git" / "incidental-state").write_text(
            "changed\n", encoding="utf-8"
        )
        after = repo_guard.capture(
            str(self.repo), "plan.md", recovery_status_value="status.md"
        )

        result = repo_guard.compare(before, after, semantic_git=True)

        self.assertTrue(result["same"], json.dumps(result, indent=2))

    def test_delayed_compare_detects_semantic_ref_change(self) -> None:
        before, _after = self.delayed_snapshot()
        self.git("branch", "unexpected")
        after = repo_guard.capture(
            str(self.repo), "plan.md", recovery_status_value="status.md"
        )

        result = repo_guard.compare(before, after, semantic_git=True)

        self.assertFalse(result["same"])
        self.assertIn("refs_sha256", result["git"])


if __name__ == "__main__":
    unittest.main()
