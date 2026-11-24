from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import commit_check

TASK = "workspace/outcomes/task-demo-run_20261101-120000Z-abcd"
STATUS = f"{TASK}/status.md"
BRIEF = f"{TASK}/brief.md"


class CommitCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.manifest_directory = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary_directory.name).resolve()
        self.git("init", "-q", "-b", "on-going-dev")
        self.git("config", "user.name", "Commit Check Test")
        self.git("config", "user.email", "commit-check@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "core.hooksPath", str(self.repo / ".git" / "hooks"))
        (self.repo / TASK).mkdir(parents=True)
        self.write(STATUS, "State: planning\n")
        self.write(BRIEF, "brief\n")
        self.write("other.txt", "other\n")
        self.write("staged.txt", "staged v1\n")
        self.git("add", ".")
        self.git("commit", "-qm", "initial")
        self.git("checkout", "-qb", "feature/demo")
        self.write("staged.txt", "staged v2\n")
        self.git("add", "staged.txt")
        self.write("other.txt", "dirty unrelated\n")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()
        self.manifest_directory.cleanup()

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.repo), *args],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).stdout

    def write(self, path: str, text: str) -> None:
        (self.repo / path).write_text(text, encoding="utf-8")

    def hook(self, body: str) -> None:
        hook = self.repo / ".git" / "hooks" / "pre-commit"
        hook.write_text(f"#!/bin/sh\nset -e\n{body}\n", encoding="utf-8")
        hook.chmod(0o755)

    def commit(self, *paths: str) -> None:
        self.git("commit", "-q", "--only", "-m", "plan-review(demo): step v1", "--", *paths)

    def run_pre(self, *paths: str) -> dict:
        self.git("add", "--", *paths)
        return commit_check.pre(str(self.repo), list(paths))

    def test_clean_path_limited_commit_passes_and_preserves_unrelated_work(self) -> None:
        self.write(STATUS, "State: reviewing\n")
        manifest = self.run_pre(STATUS, BRIEF)
        self.assertEqual(manifest["expected_changed"], [STATUS])

        self.commit(STATUS, BRIEF)
        result = commit_check.post(manifest)

        self.assertTrue(result["ok"], json.dumps(result, indent=2))
        self.assertEqual(self.git("diff", "--cached", "--name-only"), "staged.txt\n")
        self.assertEqual((self.repo / "other.txt").read_text(), "dirty unrelated\n")

    def test_worktree_change_after_staging_is_rejected_before_commit(self) -> None:
        self.write(STATUS, "State: reviewing\n")
        self.git("add", "--", STATUS)
        self.write(STATUS, "State: changed after inspection\n")

        with self.assertRaisesRegex(commit_check.CheckError, "worktree mode/content differs"):
            commit_check.pre(str(self.repo), [STATUS])

    def test_worktree_mode_change_after_staging_is_rejected_before_commit(self) -> None:
        self.write(STATUS, "State: reviewing\n")
        self.git("add", "--", STATUS)
        (self.repo / STATUS).chmod(0o755)

        with self.assertRaisesRegex(commit_check.CheckError, "worktree mode/content differs"):
            commit_check.pre(str(self.repo), [STATUS])

    def test_gitattributes_filters_and_line_endings_are_respected(self) -> None:
        self.write(".gitattributes", "*.md text eol=crlf filter=upper\n")
        self.git("config", "filter.upper.clean", "tr a-z A-Z")
        self.git("config", "filter.upper.smudge", "cat")
        self.git("add", ".gitattributes")
        self.git("commit", "-qm", "attributes", "--", ".gitattributes")
        (self.repo / STATUS).write_bytes(b"state: reviewing\r\n")

        manifest = self.run_pre(STATUS)
        staged = self.git("cat-file", "-p", manifest["files"][STATUS]["entry"][1])
        self.assertEqual(staged, "STATE: REVIEWING\n")
        self.commit(STATUS)
        result = commit_check.post(manifest)

        self.assertTrue(result["ok"], json.dumps(result, indent=2))

    def test_unstaged_allowed_path_is_rejected(self) -> None:
        with self.assertRaisesRegex(commit_check.CheckError, "no allowed path differs"):
            commit_check.pre(str(self.repo), [STATUS])

    def test_hook_that_rewrites_and_stages_allowed_file_is_detected(self) -> None:
        self.write(STATUS, "State: reviewing\n")
        manifest = self.run_pre(STATUS)
        self.hook(f'echo hooked >> "{STATUS}"\ngit add "{STATUS}"')

        self.commit(STATUS)
        result = commit_check.post(manifest)

        self.assertFalse(result["ok"])
        self.assertEqual(result["pre_head"], manifest["pre_head"])
        self.assertNotEqual(result["commit"], manifest["pre_head"])
        self.assertTrue(any("committed entry" in item for item in result["failures"]))

    def test_hook_that_stages_executable_mode_is_detected(self) -> None:
        self.write(STATUS, "State: reviewing\n")
        manifest = self.run_pre(STATUS)
        self.hook(f'chmod +x "{STATUS}"\ngit update-index --chmod=+x "{STATUS}"')

        self.commit(STATUS)
        result = commit_check.post(manifest)

        self.assertFalse(result["ok"])
        self.assertIn("100755", self.git("ls-tree", "HEAD", "--", STATUS))
        self.assertTrue(any("committed entry" in item for item in result["failures"]))

    def test_hook_that_makes_allowed_file_executable_without_staging_is_detected(self) -> None:
        self.write(STATUS, "State: reviewing\n")
        manifest = self.run_pre(STATUS)
        self.hook(f'chmod +x "{STATUS}"')

        self.commit(STATUS)
        result = commit_check.post(manifest)

        self.assertFalse(result["ok"])
        self.assertTrue(any("worktree entry" in item for item in result["failures"]))

    def test_hook_that_rewrites_allowed_file_without_staging_is_detected(self) -> None:
        self.write(STATUS, "State: reviewing\n")
        manifest = self.run_pre(STATUS)
        self.hook(f'echo hooked >> "{STATUS}"')

        self.commit(STATUS)
        result = commit_check.post(manifest)

        self.assertFalse(result["ok"])
        self.assertEqual(
            len([item for item in result["failures"] if "worktree entry" in item]), 1
        )
        self.assertFalse(any("committed entry" in item for item in result["failures"]))

    def test_hook_that_stages_unrelated_path_is_detected(self) -> None:
        self.write(STATUS, "State: reviewing\n")
        manifest = self.run_pre(STATUS)
        self.hook("git add other.txt")

        self.commit(STATUS)
        result = commit_check.post(manifest)

        self.assertFalse(result["ok"])
        self.assertTrue(any("committed paths" in item for item in result["failures"]))

    def test_unrelated_index_change_is_detected(self) -> None:
        self.write(STATUS, "State: reviewing\n")
        manifest = self.run_pre(STATUS)

        self.commit(STATUS)
        self.git("reset", "-q", "--", "staged.txt")
        result = commit_check.post(manifest)

        self.assertFalse(result["ok"])
        self.assertTrue(any("unrelated index" in item for item in result["failures"]))

    def test_missing_commit_is_reported(self) -> None:
        self.write(STATUS, "State: reviewing\n")
        manifest = self.run_pre(STATUS)
        self.hook("exit 1")

        with self.assertRaises(subprocess.CalledProcessError):
            self.commit(STATUS)
        result = commit_check.post(manifest)

        self.assertFalse(result["ok"])
        self.assertEqual(result["commit"], manifest["pre_head"])

    def test_cli_round_trip_and_manifest_integrity(self) -> None:
        script = Path(commit_check.__file__)
        manifest_path = Path(self.manifest_directory.name) / "commit-check.json"
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        self.write(STATUS, "State: reviewing\n")
        self.git("add", "--", STATUS)
        digest = subprocess.run(
            ["python3", str(script), "pre", "--repo", str(self.repo),
             "--output", str(manifest_path), STATUS],
            check=True, text=True, stdout=subprocess.PIPE, env=env,
        ).stdout.strip()
        self.commit(STATUS)

        def run_post(sha: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                ["python3", str(script), "post", "--repo", str(self.repo),
                 "--baseline", str(manifest_path), "--baseline-sha256", sha,
                 "--status-file", STATUS],
                check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=env,
            )

        passed = run_post(digest)
        self.assertEqual(passed.returncode, 0, passed.stderr)
        self.assertTrue(json.loads(passed.stdout)["ok"])
        self.assertNotIn("Recovery:", (self.repo / STATUS).read_text())

        failed = run_post("0" * 64)
        self.assertEqual(failed.returncode, 2)
        self.assertIn("Recovery: commit-check-failed", (self.repo / STATUS).read_text())


class HistoryTests(unittest.TestCase):
    """Resume-time validation of task commits from Git history alone."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.manifest_directory = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary_directory.name).resolve()
        self.script = Path(commit_check.__file__)
        self.env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        self.git("init", "-q", "-b", "on-going-dev")
        self.git("config", "user.name", "History Test")
        self.git("config", "user.email", "history@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "core.hooksPath", str(self.repo / ".git" / "hooks"))
        (self.repo / "README.md").write_text("readme\n", encoding="utf-8")
        self.git("add", "README.md")
        self.git("commit", "-qm", "initial")
        self.git("checkout", "-qb", "feature/demo")
        (self.repo / TASK).mkdir(parents=True)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()
        self.manifest_directory.cleanup()

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.repo), *args],
            check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ).stdout

    def cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["python3", str(self.script), *args],
            check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=self.env,
        )

    def hook(self, body: str) -> None:
        hook = self.repo / ".git" / "hooks" / "pre-commit"
        hook.write_text(f"#!/bin/sh\nset -e\n{body}\n", encoding="utf-8")
        hook.chmod(0o755)

    @staticmethod
    def status(state: str, iteration: int = 1, limit: int = 10) -> str:
        return f"# Status — demo\nIteration: {iteration} of {limit}\nState: {state}\n"

    def through_review(self, verdict: str, limit: int = 10) -> None:
        self.brief()
        self.step("plan", 1, {"plan.md": "# Plan\nVersion: 1\n",
                              "status.md": self.status("reviewing", 1, limit)})
        self.step("review-start", 1, {"status.md": self.status("reviewer-running", 1, limit)})
        self.step("review", 1, {
            "review.md": f"# Review\nVersion: 1\nVerdict: {verdict}\n",
            "status.md": self.status("reviewed", 1, limit),
        })

    def minors(self, iteration: int = 2, limit: int = 10) -> None:
        self.step("minors", iteration, {
            "plan.md": f"# Plan\nVersion: {iteration}\n",
            "status.md": self.status("awaiting-user", iteration, limit),
        })

    def step(self, step: str, version: int, files: dict[str, str], run_post: bool = True):
        paths = []
        for name, text in files.items():
            (self.repo / TASK / name).write_text(text, encoding="utf-8")
            paths.append(f"{TASK}/{name}")
        self.git("add", "--", *paths)
        manifest = Path(self.manifest_directory.name) / f"{step}-{version}.json"
        pre = self.cli("pre", "--repo", str(self.repo), "--output", str(manifest), *paths)
        self.assertEqual(pre.returncode, 0, pre.stderr)
        self.git("commit", "-q", "--only", "-m", f"plan-review(demo-run): {step} v{version}",
                 "--", *paths)
        if not run_post:
            return None
        return self.cli("post", "--repo", str(self.repo), "--baseline", str(manifest),
                        "--baseline-sha256", pre.stdout.strip(), "--status-file", STATUS)

    def history(self) -> dict:
        return commit_check.history(str(self.repo), TASK)

    def brief(self) -> None:
        self.step("brief", 1, {"brief.md": "# Brief\n", "status.md": self.status("planning")})

    def test_valid_history_through_consensus_passes(self) -> None:
        self.brief()
        self.step("plan", 1, {"plan.md": "# Plan\nVersion: 1\n",
                              "status.md": self.status("reviewing")})
        self.step("review-start", 1, {"status.md": self.status("reviewer-running")})
        self.step("review", 1, {"review.md": "# Review\nVersion: 1\n",
                                "status.md": self.status("reviewed")})
        self.step("consensus", 1, {"status.md": self.status("awaiting-user")})

        result = self.history()

        self.assertTrue(result["ok"], json.dumps(result, indent=2))
        self.assertEqual((result["commits"], result["last_state"]), (5, "awaiting-user"))
        self.assertIsNone(result["recovery"])
        cli = self.cli("history", "--repo", str(self.repo), "--task", TASK)
        self.assertEqual(cli.returncode, 0, cli.stdout)

    def test_interrupted_before_post_valid_commit_is_verified_from_history(self) -> None:
        self.brief()
        self.step("plan", 1, {"plan.md": "# Plan\nVersion: 1\nrepeated\n",
                              "status.md": self.status("reviewing")}, run_post=False)

        result = self.history()

        self.assertTrue(result["ok"], json.dumps(result, indent=2))
        self.assertEqual(result["last_state"], "reviewing")

    def test_interrupted_before_post_hook_added_path_is_detected(self) -> None:
        self.brief()
        (self.repo / "notes.txt").write_text("unrelated\n", encoding="utf-8")
        self.hook("git add notes.txt")
        self.step("plan", 1, {"plan.md": "# Plan\nVersion: 1\n",
                              "status.md": self.status("reviewing")}, run_post=False)

        result = self.history()

        self.assertFalse(result["ok"])
        self.assertTrue(any("disallowed paths" in item for item in result["failures"]))

    def test_interrupted_before_post_executable_mode_is_detected(self) -> None:
        self.brief()
        self.hook(f'git update-index --chmod=+x "{TASK}/plan.md"')
        self.step("plan", 1, {"plan.md": "# Plan\nVersion: 1\n",
                              "status.md": self.status("reviewing")}, run_post=False)

        result = self.history()

        self.assertFalse(result["ok"])
        self.assertTrue(any("mode 100755" in item for item in result["failures"]))

    def test_interrupted_before_post_invalid_transition_is_detected(self) -> None:
        self.brief()
        self.step("review", 1, {"review.md": "# Review\nVersion: 1\n",
                                "status.md": self.status("reviewed")}, run_post=False)

        result = self.history()

        self.assertFalse(result["ok"])
        failures = "\n".join(result["failures"])
        self.assertIn("not allowed after State planning", failures)
        self.assertIn("no committed plan v1", failures)

    def test_next_self_planned_iteration_may_follow_reviewed(self) -> None:
        self.brief()
        self.step("plan", 1, {"plan.md": "# Plan\nVersion: 1\n",
                              "status.md": self.status("reviewing")})
        self.step("review-start", 1, {"status.md": self.status("reviewer-running")})
        self.step("review", 1, {"review.md": "# Review\nVersion: 1\n",
                                "status.md": self.status("reviewed")})
        self.step("plan", 2, {"plan.md": "# Plan\nVersion: 2\n",
                              "status.md": self.status("reviewing", 2)})

        result = self.history()

        self.assertTrue(result["ok"], json.dumps(result, indent=2))

    def test_next_subagent_iteration_may_follow_reviewed(self) -> None:
        self.brief()
        self.step("plan", 1, {"plan.md": "# Plan\nVersion: 1\n",
                              "status.md": self.status("reviewing")})
        self.step("review-start", 1, {"status.md": self.status("reviewer-running")})
        self.step("review", 1, {"review.md": "# Review\nVersion: 1\n",
                                "status.md": self.status("reviewed")})
        self.step("plan-start", 2, {"status.md": self.status("planner-running", 2)})

        result = self.history()

        self.assertTrue(result["ok"], json.dumps(result, indent=2))

    def test_missing_persistent_brief_is_rejected(self) -> None:
        self.brief()
        self.step("plan", 1, {"plan.md": "# Plan\nVersion: 1\n",
                              "status.md": self.status("reviewing")})
        self.step("review-start", 1, {"status.md": self.status("reviewer-running")})
        self.step("review", 1, {"review.md": "# Review\nVersion: 1\n",
                                "status.md": self.status("reviewed")})
        self.step("consensus", 1, {"status.md": self.status("awaiting-user")})
        (self.repo / STATUS).write_text(self.status("planning"), encoding="utf-8")
        self.git("rm", "-q", "--", BRIEF)
        self.git("add", "--", STATUS)
        self.git("commit", "-q", "--only", "-m", "plan-review(demo-run): feedback v1",
                 "--", BRIEF, STATUS)

        result = self.history()

        self.assertFalse(result["ok"])
        self.assertTrue(any("persistent task files are missing" in item
                            for item in result["failures"]))

    def test_non_plan_step_cannot_jump_versions(self) -> None:
        self.brief()
        self.step("plan", 1, {"plan.md": "# Plan\nVersion: 1\n",
                              "status.md": self.status("reviewing")})
        self.step("review-start", 1, {"status.md": self.status("reviewer-running")})
        self.step("review", 1, {"review.md": "# Review\nVersion: 1\n",
                                "status.md": self.status("reviewed")})
        self.step("consensus", 99, {"status.md": self.status("awaiting-user", 99)},
                  run_post=False)

        result = self.history()

        self.assertFalse(result["ok"])
        self.assertTrue(any("consensus v99 must be v1 after review" in item
                            for item in result["failures"]))

    def test_subject_version_must_match_status_iteration(self) -> None:
        self.brief()
        self.step("plan", 1, {"plan.md": "# Plan\nVersion: 1\n",
                              "status.md": self.status("reviewing")})
        self.step("review-start", 1, {"status.md": self.status("reviewer-running")})
        self.step("review", 1, {"review.md": "# Review\nVersion: 1\n",
                                "status.md": self.status("reviewed")})
        self.step("consensus", 1, {"status.md": self.status("awaiting-user", 2)},
                  run_post=False)

        result = self.history()

        self.assertFalse(result["ok"])
        self.assertTrue(any("Iteration does not match v1" in item
                            for item in result["failures"]))

    def test_next_plan_cannot_repeat_reviewed_version(self) -> None:
        self.brief()
        self.step("plan", 1, {"plan.md": "# Plan\nVersion: 1\n",
                              "status.md": self.status("reviewing")})
        self.step("review-start", 1, {"status.md": self.status("reviewer-running")})
        self.step("review", 1, {"review.md": "# Review\nVersion: 1\n",
                                "status.md": self.status("reviewed")})
        self.step("plan", 1, {"plan.md": "# Plan\nVersion: 1\n",
                              "status.md": self.status("reviewing")}, run_post=False)

        result = self.history()

        self.assertFalse(result["ok"])
        self.assertTrue(any("plan v1 must be v2 after review" in item
                            for item in result["failures"]))

    def test_next_plan_cannot_skip_versions(self) -> None:
        self.brief()
        self.step("plan", 1, {"plan.md": "# Plan\nVersion: 1\n",
                              "status.md": self.status("reviewing")})
        self.step("review-start", 1, {"status.md": self.status("reviewer-running")})
        self.step("review", 1, {"review.md": "# Review\nVersion: 1\n",
                                "status.md": self.status("reviewed")})
        self.step("plan", 5, {"plan.md": "# Plan\nVersion: 5\n",
                              "status.md": self.status("reviewing", 5)}, run_post=False)

        result = self.history()

        self.assertFalse(result["ok"])
        self.assertTrue(any("plan v5 must be v2 after review" in item
                            for item in result["failures"]))

    def test_plan_version_advances_after_review_limit_and_user_stop(self) -> None:
        self.brief()
        self.step("plan", 1, {"plan.md": "# Plan\nVersion: 1\n",
                              "status.md": self.status("reviewing")})
        self.step("review-start", 1, {"status.md": self.status("reviewer-running")})
        self.step("review", 1, {"review.md": "# Review\nVersion: 1\n",
                                "status.md": self.status("reviewed")})
        self.step("limit-reached", 1, {"status.md": self.status("limit-reached")})
        self.step("stopped", 1, {"status.md": self.status("stopped")})
        self.step("plan-start", 1, {"status.md": self.status("planner-running")},
                  run_post=False)

        result = self.history()

        self.assertFalse(result["ok"])
        self.assertTrue(any("plan-start v1 must be v2 after stopped" in item
                            for item in result["failures"]))

    def test_next_plan_version_remains_open_after_user_stop(self) -> None:
        self.brief()
        self.step("plan", 1, {"plan.md": "# Plan\nVersion: 1\n",
                              "status.md": self.status("reviewing")})
        self.step("review-start", 1, {"status.md": self.status("reviewer-running")})
        self.step("review", 1, {"review.md": "# Review\nVersion: 1\n",
                                "status.md": self.status("reviewed")})
        self.step("limit-reached", 1, {"status.md": self.status("limit-reached")})
        self.step("stopped", 1, {"status.md": self.status("stopped")})
        self.step("plan-start", 2, {"status.md": self.status("planner-running", 2)})
        self.step("plan", 2, {"plan.md": "# Plan\nVersion: 2\n",
                              "status.md": self.status("reviewing", 2)})

        result = self.history()

        self.assertTrue(result["ok"], json.dumps(result, indent=2))

    def test_same_version_plan_retry_remains_valid_after_stop(self) -> None:
        self.brief()
        self.step("plan", 1, {"plan.md": "# Plan\nVersion: 1\n",
                              "status.md": self.status("reviewing")})
        self.step("review-start", 1, {"status.md": self.status("reviewer-running")})
        self.step("review", 1, {"review.md": "# Review\nVersion: 1\n",
                                "status.md": self.status("reviewed")})
        self.step("plan-start", 2, {"status.md": self.status("planner-running", 2)})
        self.step("stopped", 2, {"status.md": self.status("stopped", 2)})
        self.step("plan-start", 2, {"status.md": self.status("planner-running", 2)})

        result = self.history()

        self.assertTrue(result["ok"], json.dumps(result, indent=2))

    def test_post_failure_leaves_uncommitted_marker_that_resume_detects(self) -> None:
        self.brief()
        self.hook(f'echo "hook text" >> "{TASK}/plan.md"\ngit add "{TASK}/plan.md"')
        post = self.step("plan", 1, {"plan.md": "# Plan\nVersion: 1\n",
                                     "status.md": self.status("reviewing")})
        head = self.git("rev-parse", "HEAD").strip()

        self.assertEqual(post.returncode, 1, post.stdout + post.stderr)
        marker = f"Recovery: commit-check-failed commit={head} pre-head="
        self.assertIn(marker, (self.repo / STATUS).read_text())
        self.assertNotIn("Recovery:", self.git("show", f"HEAD:{STATUS}"))
        self.assertEqual(self.git("status", "--porcelain", "--", STATUS), f" M {STATUS}\n")

        result = self.history()
        self.assertTrue(result["recovery"].startswith(marker))
        cli = self.cli("history", "--repo", str(self.repo), "--task", TASK)
        self.assertEqual(cli.returncode, 1, cli.stdout)

    def test_committed_recovery_marker_is_rejected(self) -> None:
        self.brief()
        digest = "a" * 40
        self.step("plan", 1, {"plan.md": "# Plan\nVersion: 1\n",
                              "status.md": self.status("reviewing")
                              + f"Recovery: commit-check-failed commit={digest} "
                              + f"pre-head={digest}\n"},
                  run_post=False)

        result = self.history()

        self.assertFalse(result["ok"])
        self.assertTrue(any("recovery marker" in item for item in result["failures"]))

    def test_multiple_worktree_recovery_markers_are_rejected(self) -> None:
        self.brief()
        status = self.repo / STATUS
        status.write_text(
            status.read_text()
            + "Recovery: commit-check-failed commit=x pre-head=y\n"
            + "Recovery: skill-integrity-failed phase=detected\n",
            encoding="utf-8",
        )

        with self.assertRaises(commit_check.CheckError):
            commit_check.recovery_marker(self.repo, TASK)

    def test_malformed_single_recovery_marker_is_rejected(self) -> None:
        self.brief()
        status = self.repo / STATUS
        status.write_text(
            status.read_text() + "Recovery: skill-integrity-failed phase=detected\n",
            encoding="utf-8",
        )

        with self.assertRaises(commit_check.CheckError):
            commit_check.recovery_marker(self.repo, TASK)

    def test_impossible_skill_recovery_state_is_rejected(self) -> None:
        self.brief()
        status = self.repo / STATUS
        digest = "a" * 64
        status.write_text(
            status.read_text()
            + "Recovery: skill-integrity-failed "
            f"phase=detected baseline={digest} current={digest} failure=none "
            "commit=none pre-head=unknown repo-check=pending prior=none "
            "guard=none guard-sha=none commit-manifest=none "
            "commit-manifest-sha=none\n",
            encoding="utf-8",
        )

        with self.assertRaises(commit_check.CheckError):
            commit_check.recovery_marker(self.repo, TASK)

    def test_terminal_skill_markers_are_canonical(self) -> None:
        self.brief()
        status = self.repo / STATUS
        digest = "a" * 64
        for failure in ("pre-capture", "planted-marker"):
            marker = (
                "Recovery: skill-integrity-failed "
                f"phase=detected baseline={digest} current={digest} failure={failure} "
                "commit=none pre-head=unknown repo-check=failed prior=none "
                "guard=none guard-sha=none commit-manifest=none "
                "commit-manifest-sha=none"
            )
            status.write_text(
                self.status("briefed") + marker + "\n", encoding="utf-8"
            )
            self.assertEqual(commit_check.recovery_marker(self.repo, TASK), marker)

    def test_nonterminal_planted_marker_is_rejected(self) -> None:
        self.brief()
        status = self.repo / STATUS
        digest = "a" * 64
        marker = (
            "Recovery: skill-integrity-failed "
            f"phase=skill-restored baseline={digest} current={digest} "
            "failure=planted-marker commit=none pre-head=unknown "
            "repo-check=pending prior=none "
            f"guard=/tmp/guard guard-sha={digest} "
            "commit-manifest=none commit-manifest-sha=none"
        )
        status.write_text(status.read_text() + marker + "\n", encoding="utf-8")
        with self.assertRaises(commit_check.CheckError):
            commit_check.recovery_marker(self.repo, TASK)

    def test_write_recovery_marker_refuses_existing_marker(self) -> None:
        self.brief()
        status = self.repo / STATUS
        status.write_text(
            status.read_text()
            + "Recovery: skill-integrity-failed phase=detected\n",
            encoding="utf-8",
        )

        with self.assertRaises(commit_check.CheckError):
            commit_check.write_recovery_marker(
                self.repo, STATUS, "none", self.git("rev-parse", "HEAD").strip()
            )

    def test_staged_allowed_path_and_unstaged_marker_are_both_visible(self) -> None:
        self.brief()
        plan = self.repo / TASK / "plan.md"
        status = self.repo / STATUS
        plan.write_text("# Plan\nVersion: 1\n", encoding="utf-8")
        status.write_text(self.status("reviewing"), encoding="utf-8")
        self.git("add", str(plan), str(status))
        digest = "a" * 64
        status.write_text(
            status.read_text()
            + "Recovery: skill-integrity-failed "
            f"phase=detected baseline={digest} current={digest} failure=none "
            "commit=none pre-head=unknown repo-check=not-applicable prior=none "
            "guard=none guard-sha=none commit-manifest=none "
            "commit-manifest-sha=none\n",
            encoding="utf-8",
        )

        porcelain = self.git(
            "status", "--porcelain", "--untracked-files=all", "--", TASK
        )
        self.assertIn(f"A  {TASK}/plan.md", porcelain)
        self.assertIn(f"MM {STATUS}", porcelain)
        self.assertIn(
            "Recovery: skill-integrity-failed",
            commit_check.recovery_marker(self.repo, TASK) or "",
        )

    def test_non_task_path_is_rejected(self) -> None:
        with self.assertRaises(commit_check.CheckError):
            commit_check.history(str(self.repo), "workspace/outcomes/not-a-task")

    def test_minors_after_reviewed_passes(self) -> None:
        self.through_review("APPROVE_WITH_MINORS")
        self.minors()

        result = self.history()

        self.assertTrue(result["ok"], json.dumps(result, indent=2))
        self.assertEqual(result["last_state"], "awaiting-user")

    def test_minors_after_plan_start_passes(self) -> None:
        self.through_review("APPROVE_WITH_MINORS")
        self.step("plan-start", 2, {"status.md": self.status("planner-running", 2)})
        self.minors()

        result = self.history()

        self.assertTrue(result["ok"], json.dumps(result, indent=2))

    def test_minors_rejected_with_no_review(self) -> None:
        self.brief()
        self.step("minors", 1, {"plan.md": "# Plan\nVersion: 1\n",
                                "status.md": self.status("awaiting-user")}, run_post=False)

        result = self.history()

        self.assertFalse(result["ok"])
        self.assertTrue(any("Verdict APPROVE_WITH_MINORS" in item for item in result["failures"]))

    def test_minors_rejected_after_changes_requested(self) -> None:
        self.through_review("CHANGES_REQUESTED")
        self.step("minors", 2, {"plan.md": "# Plan\nVersion: 2\n",
                                "status.md": self.status("awaiting-user", 2)}, run_post=False)

        result = self.history()

        self.assertFalse(result["ok"])
        self.assertTrue(any("got v1 CHANGES_REQUESTED" in item for item in result["failures"]))

    def test_minors_rejected_with_wrong_version(self) -> None:
        self.through_review("APPROVE_WITH_MINORS")
        self.step("minors", 3, {"plan.md": "# Plan\nVersion: 3\n",
                                "status.md": self.status("awaiting-user", 3)}, run_post=False)

        result = self.history()

        self.assertFalse(result["ok"])
        self.assertTrue(any("minors v3 must be v2 after review" in item
                            for item in result["failures"]))

    def test_consensus_rejected_after_approve_with_minors(self) -> None:
        self.through_review("APPROVE_WITH_MINORS")
        self.step("consensus", 1, {"status.md": self.status("awaiting-user")}, run_post=False)

        result = self.history()

        self.assertFalse(result["ok"])
        self.assertTrue(any("consensus is rejected after Verdict APPROVE_WITH_MINORS" in item
                            for item in result["failures"]))

    def test_limit_reached_rejected_after_approve_with_minors(self) -> None:
        self.through_review("APPROVE_WITH_MINORS")
        self.step("limit-reached", 1, {"status.md": self.status("limit-reached")},
                  run_post=False)

        result = self.history()

        self.assertFalse(result["ok"])
        self.assertTrue(any("limit-reached is rejected after Verdict APPROVE_WITH_MINORS" in item
                            for item in result["failures"]))

    def test_plan_rejected_after_approve_with_minors(self) -> None:
        self.through_review("APPROVE_WITH_MINORS")
        self.step("plan", 2, {"plan.md": "# Plan\nVersion: 2\n",
                              "status.md": self.status("reviewing", 2)}, run_post=False)

        result = self.history()

        self.assertFalse(result["ok"])
        self.assertTrue(any("plan is rejected after Verdict APPROVE_WITH_MINORS" in item
                            for item in result["failures"]))

    def test_minors_feedback_then_following_plan_passes(self) -> None:
        self.through_review("APPROVE_WITH_MINORS")
        self.minors()
        self.step("feedback", 2, {"brief.md": "# Brief\n\nFeedback.\n",
                                  "status.md": self.status("planning", 2)})
        self.step("plan", 3, {"plan.md": "# Plan\nVersion: 3\n",
                              "status.md": self.status("reviewing", 3)})

        result = self.history()

        self.assertTrue(result["ok"], json.dumps(result, indent=2))

    def test_plan_at_minors_version_rejected_after_feedback(self) -> None:
        self.through_review("APPROVE_WITH_MINORS")
        self.minors()
        self.step("feedback", 2, {"brief.md": "# Brief\n\nFeedback.\n",
                                  "status.md": self.status("planning", 2)})
        self.step("plan", 2, {"plan.md": "# Plan\nVersion: 2\n",
                              "status.md": self.status("reviewing", 2)}, run_post=False)

        result = self.history()

        self.assertFalse(result["ok"])
        self.assertTrue(any("plan v2 must be v3 after feedback" in item
                            for item in result["failures"]))

    def test_minors_at_limit_passes(self) -> None:
        self.through_review("APPROVE_WITH_MINORS", limit=1)
        self.minors(iteration=2, limit=2)

        result = self.history()

        self.assertTrue(result["ok"], json.dumps(result, indent=2))

    def test_feedback_after_stopped_minors_attempt_allows_plan(self) -> None:
        self.through_review("APPROVE_WITH_MINORS")
        self.step("plan-start", 2, {"status.md": self.status("planner-running", 2)})
        self.step("stopped", 2, {"status.md": self.status("stopped", 2)})
        self.step("feedback", 2, {"brief.md": "# Brief\n\nFeedback.\n",
                                  "status.md": self.status("planning", 2)})
        self.step("plan", 2, {"plan.md": "# Plan\nVersion: 2\n",
                              "status.md": self.status("reviewing", 2)})

        result = self.history()

        self.assertTrue(result["ok"], json.dumps(result, indent=2))
        self.assertEqual(result["last_state"], "reviewing")

    def test_feedback_unblocks_plan_but_not_consensus(self) -> None:
        self.through_review("APPROVE_WITH_MINORS")
        self.step("plan-start", 2, {"status.md": self.status("planner-running", 2)})
        self.step("stopped", 2, {"status.md": self.status("stopped", 2)})
        self.step("feedback", 2, {"brief.md": "# Brief\n\nFeedback.\n",
                                  "status.md": self.status("planning", 2)})
        self.step("consensus", 2, {"status.md": self.status("awaiting-user", 2)},
                  run_post=False)

        result = self.history()

        self.assertFalse(result["ok"])
        self.assertTrue(any(
            "consensus is rejected after Verdict APPROVE_WITH_MINORS" in item
            for item in result["failures"]
        ))

    def test_changes_requested_review_clears_feedback_exception(self) -> None:
        self.through_review("APPROVE_WITH_MINORS")
        self.step("plan-start", 2, {"status.md": self.status("planner-running", 2)})
        self.step("stopped", 2, {"status.md": self.status("stopped", 2)})
        self.step("feedback", 2, {"brief.md": "# Brief\n\nFeedback.\n",
                                  "status.md": self.status("planning", 2)})
        self.step("plan", 2, {"plan.md": "# Plan\nVersion: 2\n",
                              "status.md": self.status("reviewing", 2)})
        self.step("review-start", 2, {"status.md": self.status("reviewer-running", 2)})
        self.step("review", 2, {
            "review.md": "# Review\nVersion: 2\nVerdict: CHANGES_REQUESTED\n",
            "status.md": self.status("reviewed", 2),
        })
        self.step("plan", 3, {"plan.md": "# Plan\nVersion: 3\n",
                              "status.md": self.status("reviewing", 3)})
        self.step("review-start", 3, {"status.md": self.status("reviewer-running", 3)})
        self.step("review", 3, {
            "review.md": "# Review\nVersion: 3\nVerdict: APPROVE_WITH_MINORS\n",
            "status.md": self.status("reviewed", 3),
        })
        self.step("plan", 4, {"plan.md": "# Plan\nVersion: 4\n",
                              "status.md": self.status("reviewing", 4)}, run_post=False)

        result = self.history()
        rejections = [
            item for item in result["failures"]
            if "plan is rejected after Verdict APPROVE_WITH_MINORS" in item
        ]

        self.assertEqual(len(rejections), 1, json.dumps(result, indent=2))

    def test_approved_after_minors_passes(self) -> None:
        self.through_review("APPROVE_WITH_MINORS")
        self.minors()
        self.step("approved", 2, {"status.md": self.status("approved", 2)})

        result = self.history()

        self.assertTrue(result["ok"], json.dumps(result, indent=2))
        self.assertEqual(result["last_state"], "approved")


if __name__ == "__main__":
    unittest.main()
