from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import preflight


class SkillRootTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.skill = self.root / "installed" / "plan-review"
        (self.skill / "scripts").mkdir(parents=True)
        (self.skill / "SKILL.md").write_text("# skill\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_direct_project_or_user_install_resolves(self) -> None:
        self.assertEqual(
            preflight.resolve_skill_root(str(self.skill / "SKILL.md")),
            self.skill.resolve(),
        )

    def test_symlinked_install_resolves(self) -> None:
        link = self.root / "linked-skill"
        link.symlink_to(self.skill, target_is_directory=True)
        self.assertEqual(
            preflight.resolve_skill_root(str(link / "SKILL.md")),
            self.skill.resolve(),
        )

    def test_skill_must_be_regular_file(self) -> None:
        (self.skill / "SKILL.md").unlink()
        (self.skill / "SKILL.md").mkdir()
        with self.assertRaises(preflight.PreflightError):
            preflight.resolve_skill_root(str(self.skill / "SKILL.md"))

    def test_scripts_must_be_real_directory(self) -> None:
        (self.skill / "scripts").rmdir()
        (self.skill / "scripts").write_text("not a directory\n", encoding="utf-8")
        with self.assertRaises(preflight.PreflightError):
            preflight.resolve_skill_root(str(self.skill / "SKILL.md"))


class IgnoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary_directory.name)
        self.git("init", "-q")
        self.task = "workspace/outcomes/task-demo-run_20261101-120000Z-abcd"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def git(self, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(self.repo), *args],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )

    def ignored(self, *, env: dict[str, str] | None = None) -> list[str]:
        return preflight.ignored_task_files(str(self.repo), self.task, env=env)

    def write_gitignore(self, text: str) -> None:
        (self.repo / ".gitignore").write_text(text, encoding="utf-8")

    def test_unignored_candidate_passes(self) -> None:
        self.assertEqual(self.ignored(), [])

    def test_workspace_is_ignored(self) -> None:
        self.write_gitignore("workspace/\n")
        self.assertEqual(len(self.ignored()), 5)

    def test_outcomes_is_ignored(self) -> None:
        self.write_gitignore("workspace/outcomes/\n")
        self.assertEqual(len(self.ignored()), 5)

    def test_candidate_folder_is_ignored(self) -> None:
        self.write_gitignore(f"{self.task}/\n")
        self.assertEqual(len(self.ignored()), 5)

    def test_markdown_pattern_checks_all_real_paths(self) -> None:
        self.write_gitignore("*.md\n")
        self.assertEqual(len(self.ignored()), 5)

    def test_memory_only_is_detected(self) -> None:
        self.write_gitignore("memory.md\n")
        self.assertEqual(self.ignored(), [f"{self.task}/memory.md"])

    def test_status_only_is_detected(self) -> None:
        self.write_gitignore("status.md\n")
        self.assertEqual(self.ignored(), [f"{self.task}/status.md"])

    def test_info_exclude_is_honored(self) -> None:
        exclude = self.repo / ".git" / "info" / "exclude"
        exclude.write_text("workspace/\n", encoding="utf-8")
        self.assertEqual(len(self.ignored()), 5)

    def test_global_excludes_file_is_honored(self) -> None:
        home = self.repo / "home"
        home.mkdir()
        excludes = home / "global-ignore"
        excludes.write_text("workspace/\n", encoding="utf-8")
        env = os.environ.copy()
        env["HOME"] = str(home)
        env["GIT_CONFIG_GLOBAL"] = str(home / "gitconfig")
        subprocess.run(
            ["git", "config", "--global", "core.excludesFile", str(excludes)],
            check=True,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(len(self.ignored(env=env)), 5)

    def test_excluded_parent_cannot_be_reincluded(self) -> None:
        self.write_gitignore("workspace/\n!workspace/outcomes/**\n")
        self.assertEqual(len(self.ignored()), 5)

    def test_git_error_fails_closed(self) -> None:
        result = subprocess.CompletedProcess(
            args=["git"], returncode=128, stdout=b"", stderr=b"fatal"
        )
        with mock.patch.object(preflight.subprocess, "run", return_value=result):
            with self.assertRaises(preflight.PreflightError):
                self.ignored()


if __name__ == "__main__":
    unittest.main()
