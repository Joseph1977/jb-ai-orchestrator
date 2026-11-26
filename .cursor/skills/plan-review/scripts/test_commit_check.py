from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import commit_check

import memory_build

TASK = "workspace/outcomes/task-demo-run_20261101-120000Z-abcd"
STATUS = f"{TASK}/status.md"
BRIEF = f"{TASK}/brief.md"
MEMORY = f"{TASK}/memory.md"


def memory_text(audit: str = "pending", open_ids: tuple[str, ...] = (),
                closed: tuple[str, ...] = (), log: tuple[str, ...] = ()) -> str:
    lines = ["# Memory — demo", "", "## Settled decisions", "## Coverage", f"Audit: {audit}",
             "## Open points", *(f"- {pid} [major] (D1) new v1: issue" for pid in open_ids),
             "## Observations", "## Closed points", *closed, "## User decisions / retired IDs",
             "## Round log", *log]
    return "\n".join(lines) + "\n"


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

    def commit(self, *paths: str, subject: str = "plan-review(demo): step v1") -> None:
        self.git("commit", "-q", "--only", "-m", subject, "--", *paths)

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
        subject = "plan-review(demo-run): brief v1"
        memory = f"{TASK}/memory.md"
        self.write(STATUS, "Iteration: 1 of 4\nState: planning\nOutcome: none\n")
        self.write(BRIEF, "brief v1\n")
        self.write(memory, memory_text())
        self.git("add", "--", STATUS, BRIEF, memory)
        digest = subprocess.run(
            ["python3", str(script), "pre", "--repo", str(self.repo),
             "--output", str(manifest_path), "--subject", subject, STATUS, BRIEF, memory],
            check=True, text=True, stdout=subprocess.PIPE, env=env,
        ).stdout.strip()
        self.commit(STATUS, BRIEF, memory, subject=subject)

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


class HistoryFixture(unittest.TestCase):
    """Shared repository and step helpers for resume-time validation of task commits."""

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

    def step(self, step: str, version: int, files: dict[str, str], run_post: bool = True,
             valid: bool = True, subject: str | None = None):
        """Commit one step through `pre`; `valid=False` builds a deliberately invalid commit."""
        subject = subject or f"plan-review(demo-run): {step} v{version}"
        paths = []
        for name, text in files.items():
            (self.repo / TASK / name).write_text(text, encoding="utf-8")
            paths.append(f"{TASK}/{name}")
        self.git("add", "--", *paths)
        manifest = Path(self.manifest_directory.name) / f"{step}-{version}.json"
        pre = None
        if valid:
            pre = self.cli("pre", "--repo", str(self.repo), "--output", str(manifest),
                           "--subject", subject, *paths)
            self.assertEqual(pre.returncode, 0, pre.stderr)
        self.git("commit", "-q", "--only", "-m", subject, "--", *paths)
        if not run_post or pre is None:
            return None
        return self.cli("post", "--repo", str(self.repo), "--baseline", str(manifest),
                        "--baseline-sha256", pre.stdout.strip(), "--status-file", STATUS)

    def history(self) -> dict:
        return commit_check.history(str(self.repo), TASK)

    def brief(self) -> None:
        # Legacy fixture (status without Outcome): `pre` refuses to start such a run, so commit it directly.
        self.step("brief", 1, {"brief.md": "# Brief\n", "status.md": self.status("planning")},
                  valid=False)

class HistoryTests(HistoryFixture):
    """Legacy status format (no Outcome line): the pre-existing rules still apply."""

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
                                "status.md": self.status("reviewed")}, run_post=False, valid=False)

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
                  run_post=False, valid=False)

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
                  run_post=False, valid=False)

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
                              "status.md": self.status("reviewing")}, run_post=False, valid=False)

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
                              "status.md": self.status("reviewing", 5)}, run_post=False, valid=False)

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
                  run_post=False, valid=False)

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
                  run_post=False, valid=False)

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
                                "status.md": self.status("awaiting-user")}, run_post=False, valid=False)

        result = self.history()

        self.assertFalse(result["ok"])
        self.assertTrue(any("Verdict APPROVE_WITH_MINORS" in item for item in result["failures"]))

    def test_minors_rejected_after_changes_requested(self) -> None:
        self.through_review("CHANGES_REQUESTED")
        self.step("minors", 2, {"plan.md": "# Plan\nVersion: 2\n",
                                "status.md": self.status("awaiting-user", 2)}, run_post=False, valid=False)

        result = self.history()

        self.assertFalse(result["ok"])
        self.assertTrue(any("got v1 CHANGES_REQUESTED" in item for item in result["failures"]))

    def test_minors_rejected_with_wrong_version(self) -> None:
        self.through_review("APPROVE_WITH_MINORS")
        self.step("minors", 3, {"plan.md": "# Plan\nVersion: 3\n",
                                "status.md": self.status("awaiting-user", 3)}, run_post=False, valid=False)

        result = self.history()

        self.assertFalse(result["ok"])
        self.assertTrue(any("minors v3 must be v2 after review" in item
                            for item in result["failures"]))

    def test_consensus_rejected_after_approve_with_minors(self) -> None:
        self.through_review("APPROVE_WITH_MINORS")
        self.step("consensus", 1, {"status.md": self.status("awaiting-user")}, run_post=False, valid=False)

        result = self.history()

        self.assertFalse(result["ok"])
        self.assertTrue(any("consensus is rejected after Verdict APPROVE_WITH_MINORS" in item
                            for item in result["failures"]))

    def test_limit_reached_rejected_after_approve_with_minors(self) -> None:
        self.through_review("APPROVE_WITH_MINORS")
        self.step("limit-reached", 1, {"status.md": self.status("limit-reached")},
                  run_post=False, valid=False)

        result = self.history()

        self.assertFalse(result["ok"])
        self.assertTrue(any("limit-reached is rejected after Verdict APPROVE_WITH_MINORS" in item
                            for item in result["failures"]))

    def test_plan_rejected_after_approve_with_minors(self) -> None:
        self.through_review("APPROVE_WITH_MINORS")
        self.step("plan", 2, {"plan.md": "# Plan\nVersion: 2\n",
                              "status.md": self.status("reviewing", 2)}, run_post=False, valid=False)

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
                              "status.md": self.status("reviewing", 2)}, run_post=False, valid=False)

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
                  run_post=False, valid=False)

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
                              "status.md": self.status("reviewing", 4)}, run_post=False, valid=False)

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


class CurrentFormatTests(HistoryFixture):
    """Status files with an Outcome line: honest-limit rules."""

    @staticmethod
    def cstatus(state: str, iteration: int = 1, limit: int = 4, outcome: str = "none",
                open_points: str = "0/0/0", stop_reason: str = "none", attempt: int = 1) -> str:
        return (
            f"# Status — demo\nIteration: {iteration} of {limit}\nState: {state}\n"
            f"Outcome: {outcome}\nOpen points: {open_points}\nStop reason: {stop_reason}\n"
            f"Next action: attempt {attempt}\n"
        )

    def cbrief(self, limit: int = 4, memory: bool = True) -> None:
        self.audit, self.log = "pending", []
        files = {"brief.md": "# Brief\n", "status.md": self.cstatus("planning", 1, limit)}
        if memory:
            files["memory.md"] = memory_text()
        self.step("brief", 1, files, valid=memory)

    def reviewed_memory(self, n: int, closed: tuple[str, ...] = ()) -> str:
        if self.audit == "pending":
            self.audit = f"done v{n}"
        self.log.append(f"- v{n} audit: new 0, late 0, observations 0, maintained 0, closed 0")
        return memory_text(self.audit, closed=closed, log=tuple(self.log))

    def round_(self, n: int, verdict: str, limit: int, open_points: str = "0/0/0",
               memory: bool = True, closed: tuple[str, ...] = ()) -> None:
        self.step("plan-start", n, {"status.md": self.cstatus("planner-running", n, limit)},
                  valid=memory)
        self.step("plan", n, {"plan.md": f"# Plan\nVersion: {n}\n",
                              "status.md": self.cstatus("reviewing", n, limit)}, valid=memory)
        self.step("review-start", n, {"status.md": self.cstatus("reviewer-running", n, limit)},
                  valid=memory)
        files = {
            "review.md": f"# Review\nVersion: {n}\nVerdict: {verdict}\n",
            "status.md": self.cstatus("reviewed", n, limit, open_points=open_points),
        }
        if memory:
            files["memory.md"] = self.reviewed_memory(n, closed)
        self.step("review", n, files, valid=memory)

    def limit_reached(self, n: int, open_points: str, **overrides: str) -> None:
        outcome = f"limit-reached at iteration {n} of {n}, open points {open_points}"
        self.step("limit-reached", n, {"status.md": self.cstatus(
            "limit-reached", n, n, outcome=overrides.get("outcome", outcome),
            open_points=overrides.get("open_points", open_points))},
            **{key: value for key, value in overrides.items() if key == "valid"})

    def consensus(self, n: int, limit: int, **kwargs) -> None:
        outcome = kwargs.pop("outcome", f"consensus at iteration {n} of {limit}, open points 0/0/0")
        self.step("consensus", n, {"status.md": self.cstatus(
            "awaiting-user", n, limit, outcome=outcome)}, **kwargs)

    def assert_pre_rejects(self, step: str, version: int, files: dict[str, str],
                           pattern: str, subject: str | None = None) -> None:
        paths = []
        for name, text in files.items():
            (self.repo / TASK / name).write_text(text, encoding="utf-8")
            paths.append(f"{TASK}/{name}")
        self.git("add", "--", *paths)
        subject = subject or f"plan-review(demo-run): {step} v{version}"
        with self.assertRaisesRegex(commit_check.CheckError, pattern):
            commit_check.pre(str(self.repo), paths, subject)

    def failures(self) -> str:
        result = self.history()
        self.assertEqual(result["format"], "current")
        return "\n".join(result["failures"])

    def test_current_format_full_flow_passes(self) -> None:
        self.cbrief()
        self.round_(1, "CHANGES_REQUESTED", 4, "1/0/0")
        self.round_(2, "NO_FURTHER_COMMENTS", 4)
        self.consensus(2, 4)
        self.step("approved", 2, {"status.md": self.cstatus(
            "approved", 2, 4, outcome="consensus at iteration 2 of 4, open points 0/0/0")})

        result = self.history()

        self.assertTrue(result["ok"], json.dumps(result, indent=2))
        self.assertEqual((result["format"], result["last_state"]), ("current", "approved"))
        self.assertEqual(result["memory"], "native")
        self.assertNotIn("note", result)
        self.assertNotIn("memory_note", result)

    def test_brief_without_memory_rejected(self) -> None:
        self.assert_pre_rejects("brief", 1, {"brief.md": "# Brief\n",
                                             "status.md": self.cstatus("planning")},
                                "brief v1 must create memory.md")

    def test_review_must_update_memory_to_match_points_and_audit(self) -> None:
        self.cbrief()
        self.step("plan-start", 1, {"status.md": self.cstatus("planner-running")})
        self.step("plan", 1, {"plan.md": "# Plan\nVersion: 1\n",
                              "status.md": self.cstatus("reviewing")})
        self.step("review-start", 1, {"status.md": self.cstatus("reviewer-running")})
        review = ("# Review\nVersion: 1\nVerdict: CHANGES_REQUESTED\n\n## Points\n"
                  "1. P001 [major] (D1) issue → fix\n")
        status = self.cstatus("reviewed", open_points="0/1/0")
        self.assert_pre_rejects("review", 1, {"review.md": review, "status.md": status},
                                "review must update memory.md")
        self.git("reset", "-q")
        self.assert_pre_rejects("review", 1, {
            "review.md": review, "status.md": status,
            "memory.md": memory_text(log=("- v1 audit: new 1",))},
            r"Open points \[\] differ from review.md Points \['P001'\].*Audit: done v1")
        self.git("reset", "-q")
        self.step("review", 1, {"review.md": review, "status.md": status,
                                "memory.md": memory_text("done v1", ("P001",))})
        self.assertTrue(self.history()["ok"])

    def test_soft_limit_blocks_launch_until_memory_compact(self) -> None:
        self.cbrief()
        closed = tuple(f"- P{n:03d} [major] (D{n % 3 + 1}) closed v1 fixed: issue {n}"
                       for n in range(1, 230))
        self.round_(1, "CHANGES_REQUESTED", 4, "1/0/0", closed=closed)
        big = (self.repo / MEMORY).read_text(encoding="utf-8")
        self.assert_pre_rejects("plan-start", 2, {
            "status.md": self.cstatus("planner-running", 2, 4)},
            "memory.md is 2[0-9]{2} lines.*commit memory-compact v1 first")
        self.git("reset", "-q", "--", STATUS)
        self.git("checkout", "--", STATUS)
        compacted = memory_build.compact_text(big)
        self.assert_pre_rejects("memory-compact", 1, {
            "memory.md": compacted.replace("## Observations\n", "## Observations\n- v1 (D1): x\n"),
            "status.md": self.cstatus("reviewed", open_points="1/0/0")},
            "memory-compact changed the Observations section")
        self.git("reset", "-q")
        self.step("memory-compact", 1, {"memory.md": compacted,
                                        "status.md": self.cstatus("reviewed", open_points="1/0/0")})
        self.step("plan-start", 2, {"status.md": self.cstatus("planner-running", 2, 4)})
        self.assertTrue(self.history()["ok"], json.dumps(self.history(), indent=2))

    def test_pre_memory_task_must_seed_before_launch(self) -> None:
        self.cbrief(memory=False)
        self.round_(1, "CHANGES_REQUESTED", 4, "0/1/0", memory=False)
        self.assert_pre_rejects("plan-start", 2, {
            "status.md": self.cstatus("planner-running", 2, 4)},
            "run memory_build.py seed and commit memory-seed v1 first")
        self.git("reset", "-q", "--", STATUS)
        self.git("checkout", "--", STATUS)
        seeded = memory_text(log=("- seed v1: memory started, audit pending",))
        self.step("memory-seed", 1, {"memory.md": seeded,
                                     "status.md": self.cstatus("reviewed", open_points="0/1/0")})
        self.assert_pre_rejects("memory-seed", 1, {
            "memory.md": seeded + "- again\n",
            "status.md": self.cstatus("reviewed", open_points="0/1/0")},
            "memory-seed requires that memory.md is absent")
        self.git("reset", "-q")
        self.git("checkout", "--", MEMORY)
        self.step("plan-start", 2, {"status.md": self.cstatus("planner-running", 2, 4)})

        result = self.history()

        self.assertTrue(result["ok"], json.dumps(result, indent=2))
        self.assertEqual(result["memory"], "seeded")
        self.assertEqual(result["memory_note"], "pre-memory commits: memory checks not applied")

    def seeded_after_no_comments(self, limit: int) -> None:
        self.cbrief(limit, memory=False)
        self.round_(1, "NO_FURTHER_COMMENTS", limit, memory=False)
        self.step("memory-seed", 1, {"memory.md": memory_text(log=("- seed v1: audit pending",)),
                                     "status.md": self.cstatus("reviewed", 1, limit)})

    def test_pending_audit_blocks_consensus_from_old_review(self) -> None:
        self.seeded_after_no_comments(4)
        self.assert_pre_rejects("consensus", 1, {"status.md": self.cstatus(
            "awaiting-user", 1, 4, outcome="consensus at iteration 1 of 4, open points 0/0/0")},
            "consensus is rejected: memory.md records Audit: pending")
        self.git("reset", "-q", "--", STATUS)
        self.git("checkout", "--", STATUS)
        self.step("plan-start", 2, {"status.md": self.cstatus("planner-running", 2, 4)})
        self.assertTrue(self.history()["ok"])

    def test_pending_audit_at_limit_goes_to_limit_reached(self) -> None:
        self.seeded_after_no_comments(1)
        self.limit_reached(1, "0/0/0")
        self.assertTrue(self.history()["ok"], json.dumps(self.history(), indent=2))

    def test_feedback_brief_change_requires_pending_audit(self) -> None:
        self.cbrief(1)
        self.round_(1, "CHANGES_REQUESTED", 1, "0/1/0")
        self.limit_reached(1, "0/1/0")
        status = self.cstatus("planning", 1, 2, open_points="0/1/0")
        self.assert_pre_rejects("feedback", 1, {"brief.md": "# Brief\nnew goal\n",
                                                "status.md": status},
                                "changed brief.md outside ## User feedback")
        self.git("reset", "-q")
        decided = "# Brief\n## User feedback\n- 2026-11-26: user decided P001\n"
        self.assert_pre_rejects("feedback", 1, {"brief.md": decided, "status.md": status},
                                "without retiring a point")
        self.git("reset", "-q")
        self.step("feedback", 1, {"brief.md": decided,
                                  "status.md": status + "Retired point IDs: P001\n"})
        self.assertTrue(self.history()["ok"])

    def test_deleting_memory_after_start_rejected(self) -> None:
        self.cbrief()
        (self.repo / STATUS).write_text(self.cstatus("planner-running"), encoding="utf-8")
        self.git("rm", "-q", "--", MEMORY)
        self.git("add", "--", STATUS)
        self.git("commit", "-qm", "plan-review(demo-run): plan-start v1")
        self.assertIn("memory.md is missing after it was started", self.failures())

    def test_limit_reached_requires_review_at_limit(self) -> None:
        self.cbrief()
        self.round_(1, "CHANGES_REQUESTED", 4, "0/1/0")
        self.assert_pre_rejects("limit-reached", 1, {"status.md": self.cstatus(
            "limit-reached", 1, 4, outcome="limit-reached at iteration 1 of 4, open points 0/1/0",
            open_points="0/1/0")}, "requires review v1 at its limit")
        self.git("reset", "-q", "--", STATUS)
        self.git("checkout", "--", STATUS)
        self.limit_reached(1, "0/1/0", valid=False)
        self.assertIn("requires review v1 at its limit", self.failures())

    def test_limit_reached_after_minors_verdict_at_limit_passes(self) -> None:
        self.cbrief(2)
        self.round_(1, "CHANGES_REQUESTED", 2, "0/1/0")
        self.round_(2, "APPROVE_WITH_MINORS", 2, "0/0/1")
        self.limit_reached(2, "0/0/1")

        result = self.history()

        self.assertTrue(result["ok"], json.dumps(result, indent=2))
        self.assertEqual(result["last_state"], "limit-reached")

    def test_minors_subject_rejected_current_format(self) -> None:
        self.cbrief()
        self.round_(1, "APPROVE_WITH_MINORS", 4, "0/0/1")
        files = {"plan.md": "# Plan\nVersion: 2\n",
                 "status.md": self.cstatus("awaiting-user", 2, 4)}
        self.assert_pre_rejects("minors", 2, files, "Minors close commits plan v2 and is reviewed")
        self.git("reset", "-q")
        self.step("minors", 2, files, valid=False)
        self.assertIn("Minors close commits plan v2 and is reviewed", self.failures())

    def test_plan_start_after_awm_below_limit_passes(self) -> None:
        self.cbrief()
        self.round_(1, "APPROVE_WITH_MINORS", 4, "0/0/1")
        self.round_(2, "NO_FURTHER_COMMENTS", 4)
        self.consensus(2, 4)

        result = self.history()

        self.assertTrue(result["ok"], json.dumps(result, indent=2))
        self.assertEqual(result["last_state"], "awaiting-user")

    def test_plan_start_after_review_at_limit_rejected_without_feedback(self) -> None:
        self.cbrief(1)
        self.round_(1, "CHANGES_REQUESTED", 1, "0/1/0")
        self.assert_pre_rejects("plan-start", 2, {
            "status.md": self.cstatus("planner-running", 2, 1)}, "Iteration exceeds its limit")
        self.git("reset", "-q", "--", STATUS)
        self.git("checkout", "--", STATUS)
        self.assert_pre_rejects("plan-start", 2, {
            "status.md": self.cstatus("planner-running", 2, 2)}, "reached the limit 1")
        self.git("reset", "-q", "--", STATUS)
        self.git("checkout", "--", STATUS)
        self.step("plan-start", 2, {"status.md": self.cstatus("planner-running", 2, 2)},
                  valid=False)
        failures = self.failures()
        self.assertIn("reached the limit 1", failures)
        self.assertIn("only feedback may change it", failures)

    def test_feedback_with_k_continues_past_limit_passes(self) -> None:
        self.cbrief(1)
        self.round_(1, "CHANGES_REQUESTED", 1, "0/1/0")
        self.limit_reached(1, "0/1/0")
        self.step("feedback", 1, {"status.md": self.cstatus("planning", 1, 3, open_points="0/1/0")})
        self.round_(2, "NO_FURTHER_COMMENTS", 3)
        self.consensus(2, 3)

        result = self.history()

        self.assertTrue(result["ok"], json.dumps(result, indent=2))
        self.assertEqual(result["last_state"], "awaiting-user")

    def test_feedback_k_only_status_change_passes(self) -> None:
        self.cbrief(1)
        self.round_(1, "CHANGES_REQUESTED", 1, "0/1/0")
        self.limit_reached(1, "0/1/0")
        brief_before = self.git("rev-parse", f"HEAD:{BRIEF}")
        files = {"status.md": self.cstatus("planning", 1, 2, open_points="0/1/0")}
        (self.repo / STATUS).write_text(files["status.md"], encoding="utf-8")
        self.git("add", "--", STATUS)

        manifest = commit_check.pre(str(self.repo), [STATUS, BRIEF],
                                    "plan-review(demo-run): feedback v1")
        self.assertEqual(manifest["expected_changed"], [STATUS])
        self.git("reset", "-q", "--", STATUS)
        self.step("feedback", 1, files)

        changed = self.git("show", "--name-only", "--format=", "HEAD").split()
        self.assertEqual(changed, [STATUS])
        self.assertEqual(self.git("rev-parse", f"HEAD:{BRIEF}"), brief_before)
        result = self.history()
        self.assertTrue(result["ok"], json.dumps(result, indent=2))

    def test_consensus_requires_no_further_comments(self) -> None:
        self.cbrief()
        self.round_(1, "CHANGES_REQUESTED", 4, "1/0/0")
        self.consensus(1, 4, valid=False, run_post=False)

        self.assertIn("Verdict NO_FURTHER_COMMENTS", self.failures())

    def test_consensus_after_awm_without_new_review_rejected(self) -> None:
        self.cbrief()
        self.round_(1, "APPROVE_WITH_MINORS", 4, "0/0/1")
        self.assert_pre_rejects("consensus", 1, {"status.md": self.cstatus(
            "awaiting-user", 1, 4, outcome="consensus at iteration 1 of 4, open points 0/0/0")},
            "Verdict NO_FURTHER_COMMENTS")

    def test_limit_change_outside_feedback_rejected(self) -> None:
        self.cbrief()
        self.round_(1, "CHANGES_REQUESTED", 4, "1/0/0")
        self.assert_pre_rejects("plan-start", 2, {
            "status.md": self.cstatus("planner-running", 2, 5)}, "only feedback may change it")

    def test_limit_reached_preserves_open_points(self) -> None:
        self.cbrief(1)
        self.round_(1, "CHANGES_REQUESTED", 1, "0/1/0")
        self.assert_pre_rejects("limit-reached", 1, {"status.md": self.cstatus(
            "limit-reached", 1, 1, open_points="0/2/0",
            outcome="limit-reached at iteration 1 of 1, open points 0/2/0")},
            "Open points 0/2/0 must equal the review's 0/1/0")
        self.git("reset", "-q", "--", STATUS)
        self.git("checkout", "--", STATUS)
        self.assert_pre_rejects("limit-reached", 1, {"status.md": self.cstatus(
            "limit-reached", 1, 1, open_points="0/0/0",
            outcome="limit-reached at iteration 1 of 1, open points 0/0/0")},
            "nonzero Open points")

    def test_outcome_required_on_terminal_commits(self) -> None:
        self.cbrief(1)
        self.round_(1, "CHANGES_REQUESTED", 1, "0/1/0")
        self.assert_pre_rejects("limit-reached", 1, {"status.md": self.cstatus(
            "limit-reached", 1, 1, open_points="0/1/0")}, "limit-reached must set Outcome")
        self.git("reset", "-q", "--", STATUS)
        self.git("checkout", "--", STATUS)
        self.limit_reached(1, "0/1/0")
        self.step("stopped", 1, {"status.md": self.cstatus("stopped", 1, 1, stop_reason="user")},
                  valid=False)
        self.assertIn("stopped must set Outcome", self.failures())

    def test_stopped_outcome_matches_reason(self) -> None:
        self.cbrief(1)
        self.round_(1, "CHANGES_REQUESTED", 1, "0/1/0")
        self.limit_reached(1, "0/1/0")
        self.step("stopped", 1, {"status.md": self.cstatus(
            "stopped", 1, 1, outcome="stopped at iteration 1 of 1 (user)", stop_reason="user")})

        result = self.history()

        self.assertTrue(result["ok"], json.dumps(result, indent=2))

    def test_approved_must_keep_consensus_outcome(self) -> None:
        self.cbrief()
        self.round_(1, "NO_FURTHER_COMMENTS", 4)
        self.consensus(1, 4)
        self.assert_pre_rejects("approved", 1, {"status.md": self.cstatus("approved", 1, 4)},
                                "approved must keep the consensus Outcome")

    def test_outcome_matches_iteration_and_open_points(self) -> None:
        self.cbrief()
        self.round_(1, "NO_FURTHER_COMMENTS", 4)
        self.assert_pre_rejects("consensus", 1, {"status.md": self.cstatus(
            "awaiting-user", 1, 4,
            outcome="consensus at iteration 2 of 4, open points 0/0/0")}, "consensus must set Outcome")

    def test_consensus_outcome_nonzero_counts_rejected(self) -> None:
        self.cbrief()
        self.round_(1, "NO_FURTHER_COMMENTS", 4)
        self.assert_pre_rejects("consensus", 1, {"status.md": self.cstatus(
            "awaiting-user", 1, 4,
            outcome="consensus at iteration 1 of 4, open points 0/1/0")}, "consensus must set Outcome")

    def test_early_consensus_at_iteration_one_of_six_passes(self) -> None:
        self.cbrief(6)
        self.round_(1, "NO_FURTHER_COMMENTS", 6)
        self.consensus(1, 6)

        result = self.history()

        self.assertTrue(result["ok"], json.dumps(result, indent=2))
        self.assertIn("consensus at iteration 1 of 6, open points 0/0/0",
                      self.git("show", f"HEAD:{STATUS}"))

    def test_limit_reached_outcome_four_of_four(self) -> None:
        self.cbrief(4)
        self.round_(1, "CHANGES_REQUESTED", 4, "1/0/0")
        self.round_(2, "CHANGES_REQUESTED", 4, "0/1/0")
        self.round_(3, "CHANGES_REQUESTED", 4, "0/1/0")
        self.round_(4, "CHANGES_REQUESTED", 4, "0/1/0")
        self.limit_reached(4, "0/1/0")

        result = self.history()

        self.assertTrue(result["ok"], json.dumps(result, indent=2))
        self.assertIn("limit-reached at iteration 4 of 4, open points 0/1/0",
                      self.git("show", f"HEAD:{STATUS}"))

    def test_retry_subject_suffix_rejected_with_hint(self) -> None:
        self.cbrief()
        hint = r"retries reuse the exact subject plan-review\(demo-run\): plan-start v1"
        self.assert_pre_rejects("plan-start", 1, {"status.md": self.cstatus("planner-running")},
                                hint, subject="plan-review(demo-run): plan-start v1 retry")
        self.git("reset", "-q", "--", STATUS)
        self.git("checkout", "--", STATUS)
        self.step("plan-start", 1, {"status.md": self.cstatus("planner-running")}, valid=False,
                  subject="plan-review(demo-run): plan-start v1 retry")
        self.assertRegex("\n".join(self.history()["failures"]), hint)

    def test_plan_start_retry_after_planner_running_passes(self) -> None:
        self.cbrief()
        self.step("plan-start", 1, {"status.md": self.cstatus("planner-running", attempt=1)})
        self.step("plan-start", 1, {"status.md": self.cstatus("planner-running", attempt=2)})
        self.step("plan", 1, {"plan.md": "# Plan\nVersion: 1\n",
                              "status.md": self.cstatus("reviewing")})

        result = self.history()

        self.assertTrue(result["ok"], json.dumps(result, indent=2))

    def test_review_start_retry_after_reviewer_running_passes(self) -> None:
        self.cbrief()
        self.step("plan", 1, {"plan.md": "# Plan\nVersion: 1\n",
                              "status.md": self.cstatus("reviewing")})
        self.step("review-start", 1, {"status.md": self.cstatus("reviewer-running", attempt=1)})
        self.step("review-start", 1, {"status.md": self.cstatus("reviewer-running", attempt=2)})

        result = self.history()

        self.assertTrue(result["ok"], json.dumps(result, indent=2))

    def test_pre_rejects_wrong_state_before_commit(self) -> None:
        self.cbrief()
        self.step("plan-start", 1, {"status.md": self.cstatus("planner-running")})
        self.assert_pre_rejects("plan", 1, {"plan.md": "# Plan\nVersion: 1\n",
                                            "status.md": self.cstatus("planner-running", attempt=2)},
                                "plan must set State reviewing, got planner-running")

    def test_pre_rejects_iteration_mismatch(self) -> None:
        self.cbrief()
        self.round_(1, "CHANGES_REQUESTED", 4, "1/0/0")
        self.assert_pre_rejects("plan-start", 2, {
            "status.md": self.cstatus("planner-running", 1, 4)}, "Iteration does not match v2")

    def test_pre_rejects_new_brief_without_outcome(self) -> None:
        self.assert_pre_rejects("brief", 1, {"brief.md": "# Brief\n",
                                             "status.md": self.status("planning")},
                                "must record an Outcome line")

    def test_pre_requires_subject_on_the_command_line(self) -> None:
        self.cbrief()
        manifest = Path(self.manifest_directory.name) / "no-subject.json"
        (self.repo / STATUS).write_text(self.cstatus("planner-running"), encoding="utf-8")
        self.git("add", "--", STATUS)
        result = self.cli("pre", "--repo", str(self.repo), "--output", str(manifest), STATUS)
        self.assertEqual(result.returncode, 2)
        self.assertIn("--subject", result.stderr)

    def test_post_rejects_subject_mismatch(self) -> None:
        self.cbrief()
        (self.repo / STATUS).write_text(self.cstatus("planner-running"), encoding="utf-8")
        self.git("add", "--", STATUS)
        manifest = commit_check.pre(str(self.repo), [STATUS], "plan-review(demo-run): plan-start v1")
        self.git("commit", "-q", "--only", "-m", "plan-review(demo-run): plan-start v1 retry",
                 "--", STATUS)

        result = commit_check.post(manifest)

        self.assertFalse(result["ok"])
        self.assertTrue(any("commit subject" in item for item in result["failures"]))

    def test_manifest_v2_rejected(self) -> None:
        path = Path(self.manifest_directory.name) / "old.json"
        data = json.dumps({"version": 2}, sort_keys=True, separators=(",", ":")) + "\n"
        path.write_text(data, encoding="utf-8")
        digest = commit_check._sha256_bytes(data.encode("utf-8"))

        with self.assertRaisesRegex(commit_check.CheckError, "unsupported manifest version"):
            commit_check.load_manifest(str(path), digest)

    def test_legacy_status_format_uses_existing_rules(self) -> None:
        self.brief()
        self.step("plan", 1, {"plan.md": "# Plan\nVersion: 1\n",
                              "status.md": self.status("reviewing")})
        self.step("review-start", 1, {"status.md": self.status("reviewer-running")})
        self.step("review", 1, {"review.md": "# Review\nVersion: 1\n",
                                "status.md": self.status("reviewed")})
        self.step("consensus", 1, {"status.md": self.status("awaiting-user")})

        result = self.history()

        self.assertTrue(result["ok"], json.dumps(result, indent=2))
        self.assertEqual(result["format"], "legacy")
        self.assertEqual(result["note"],
                         "legacy status format: honest-limit checks not applied")
        cli = json.loads(self.cli("history", "--repo", str(self.repo), "--task", TASK).stdout)
        self.assertEqual(cli["format"], "legacy")

    def test_legacy_status_format_keeps_minors_and_limit_cases(self) -> None:
        self.through_review("APPROVE_WITH_MINORS", limit=1)
        self.minors(iteration=2, limit=2)

        result = self.history()

        self.assertTrue(result["ok"], json.dumps(result, indent=2))
        self.assertEqual(result["format"], "legacy")


if __name__ == "__main__":
    unittest.main()
