from __future__ import annotations

import ast
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import memory_build

SCRIPT = Path(memory_build.__file__)
TASK = "workspace/outcomes/task-demo-run_20261101-120000Z-abcd"

BRIEF = """# Brief — demo
Model: example

## Goal
Plan the thing.
1. Which path?
2. Which test?
Done means: a reviewed plan.
## Constraints
- C-1 Bounded.
- C-2 Isolated.
## Scope
### In
- In-1 Engine.
### Out
- Out-1 Rollout.
## Relevant files
- src/app.py
## User feedback
None
"""

PLAN_V1 = """# Plan — demo
Version: 1
Model: example-model-1

## Plan
### Decisions
- D1 (In-1) Engine wrapper.
  Continues here.
- D2 (C-1) Deadline.
### S1 — Extraction
Entry: branch cut. Exit: tests green.
"""

REVIEW_V1 = """# Review — demo
Version: 1
Model: other-model-1
Verdict: CHANGES_REQUESTED

## Points
1. P001 [major] (D2, C-1) Deadline is unbounded → Bound it.

## Coverage
- ok: C-2, In-1, Out-1, Q1, Q2, Done, D1, S1
- D2: P001
- C-1: P001
"""


def plan_v2(change: str = "- D2 changed (P001): bounded deadline.\n", d2: str = "Bounded deadline.") -> str:
    return (
        "# Plan — demo\nVersion: 2\nModel: example-model-1\n\n"
        "## Response to review v1\n| Point ID | Decision | Reason / change made |\n|---|---|---|\n"
        "| P001 | accepted | Bounded. |\n\n"
        f"## Changes\n{change}\n"
        "## Plan\n### Decisions\n- D1 (In-1) Engine wrapper.\n  Continues here.\n"
        f"- D2 (C-1) {d2}\n### S1 — Extraction\nEntry: branch cut. Exit: tests green.\n"
    )


class ParserTests(unittest.TestCase):
    def test_brief_items_follow_brief_order(self) -> None:
        self.assertEqual(memory_build.brief_items(BRIEF),
                         ["Q1", "Q2", "Done", "C-1", "C-2", "In-1", "Out-1"])

    def test_plan_anchors_cover_heading_and_continuation_lines(self) -> None:
        anchors, failures = memory_build.plan_anchors(PLAN_V1)
        self.assertEqual(failures, [])
        self.assertEqual(sorted(anchors), ["D1", "D2", "S1"])
        self.assertIn("Continues here.", anchors["D1"])
        self.assertIn("Entry: branch cut.", anchors["S1"])


class PlanCheckTests(unittest.TestCase):
    def check(self, plan: str, previous: str | None = PLAN_V1, review: str | None = REVIEW_V1):
        return memory_build.check_plan(plan, previous, review)

    def failures(self, plan: str, previous: str | None = PLAN_V1,
                 review: str | None = REVIEW_V1) -> str:
        with self.assertRaises(memory_build.FormatError) as caught:
            self.check(plan, previous, review)
        return "\n".join(caught.exception.failures)

    def test_first_plan_passes_without_changes(self) -> None:
        self.assertEqual(self.check(PLAN_V1, None, None), {"anchors": 3, "changes": 0})

    def test_first_plan_rejects_changes_section(self) -> None:
        plan = PLAN_V1.replace("## Plan", "## Changes\n- D1 added (consistency): x\n\n## Plan")
        self.assertIn("only written when a prior review", self.failures(plan, None, None))

    def test_exact_changes_pass(self) -> None:
        self.assertEqual(self.check(plan_v2()), {"anchors": 3, "changes": 1})

    def test_missing_changes_section_fails(self) -> None:
        plan = plan_v2().replace("## Changes\n- D2 changed (P001): bounded deadline.\n\n", "")
        self.assertIn("## Changes is required", self.failures(plan))

    def test_unlisted_changed_anchor_fails(self) -> None:
        plan = plan_v2(change="- D1 changed (consistency): nothing really.\n")
        failures = self.failures(plan)
        self.assertIn("D2 was changed but ## Changes does not list it", failures)
        self.assertIn("## Changes lists D1, whose text did not change", failures)
        self.assertIn("accepted P001 is cited by no ## Changes line", failures)

    def test_wrong_kind_and_unlisted_removal_fail(self) -> None:
        plan = plan_v2(change="- D2 added (P001): x\n").replace(
            "### S1 — Extraction\nEntry: branch cut. Exit: tests green.\n", "")
        failures = self.failures(plan)
        self.assertIn("## Changes marks D2 added, but it was changed", failures)
        self.assertIn("S1 was removed but ## Changes does not list it", failures)

    def test_duplicate_p_and_unanchored_lines_fail(self) -> None:
        plan = PLAN_V1 + "### Notes\n- D1 again\n- P1 forbidden\nloose line\n"
        failures = self.failures(plan, None, None)
        self.assertIn("anchor D1 is defined twice", failures)
        self.assertIn("outside any anchor", failures)

    def test_extra_section_and_bad_citation_fail(self) -> None:
        plan = plan_v2(change="- D2 changed (P009): x\n") + "\n## Files\n- a\n"
        failures = self.failures(plan)
        self.assertIn("section '## Files' is not allowed", failures)
        self.assertIn("cites P009, not a prior review point", failures)

    def test_long_change_line_fails(self) -> None:
        plan = plan_v2(change="- D2 changed (P001): " + "x" * 300 + "\n")
        self.assertIn("exceeds 300 characters", self.failures(plan))

    def test_legacy_previous_plan_skips_exact_check(self) -> None:
        legacy = PLAN_V1.replace("### Decisions\n", "### Decisions\nloose legacy line\n")
        self.assertEqual(self.check(plan_v2(), previous=legacy)["changes"], 1)


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.repo = Path(self.directory.name).resolve()
        self.env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Memory Test")
        self.git("config", "user.email", "memory@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        (self.repo / TASK).mkdir(parents=True)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def git(self, *args: str) -> str:
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout

    def write(self, name: str, text: str) -> None:
        (self.repo / TASK / name).write_text(text, encoding="utf-8")

    def commit(self, *names: str) -> None:
        self.git("add", "--", *(f"{TASK}/{name}" for name in names))
        self.git("commit", "-qm", "step")

    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["python3", "-I", "-B", str(SCRIPT), *args, "--repo", str(self.repo),
                               "--task", TASK], check=False, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env)

    def test_plan_check_compares_with_committed_plan(self) -> None:
        self.write("plan.md", PLAN_V1)
        self.write("review.md", REVIEW_V1)
        self.commit("plan.md", "review.md")
        self.write("plan.md", plan_v2())
        result = self.run_cli("plan-check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"anchors": 3, "changes": 1})
        self.write("plan.md", plan_v2(change="- D1 changed (P001): x\n"))
        failed = self.run_cli("plan-check")
        self.assertEqual(failed.returncode, 1)
        self.assertIn("D2 was changed", failed.stderr)

    def test_bad_task_path_exits_2(self) -> None:
        result = subprocess.run(["python3", "-I", "-B", str(SCRIPT), "plan-check", "--repo",
                                 str(self.repo), "--task", "workspace/outcomes/not-a-task"],
                                check=False, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=self.env)
        self.assertEqual(result.returncode, 2)


class StaticTests(unittest.TestCase):
    def test_no_local_imports(self) -> None:
        tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
        local = {path.stem for path in SCRIPT.parent.glob("*.py")}
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [(node.module or "").split(".")[0]]
            self.assertFalse(set(names) & local, names)


if __name__ == "__main__":
    unittest.main()
