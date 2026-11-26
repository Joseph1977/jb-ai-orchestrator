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


def review(version: int, points: str, coverage: str, verdict: str = "CHANGES_REQUESTED",
           extra: str = "") -> str:
    return (f"# Review — demo\nVersion: {version}\nModel: other-model-1\nVerdict: {verdict}\n\n"
            f"## Points\n{points}\n## Coverage\n{coverage}\n{extra}")


def fresh_memory() -> memory_build.Memory:
    memory = memory_build.Memory("demo")
    memory.coverage = {item: "unverified" for item in memory_build.brief_items(BRIEF)}
    return memory


def audited_memory() -> memory_build.Memory:
    memory = fresh_memory()
    memory_build.apply_review(memory, BRIEF, PLAN_V1, REVIEW_V1, "audit", [], "P001")
    return memory


class UpdateTests(unittest.TestCase):
    def failures(self, memory: memory_build.Memory, plan: str, text: str, scope: str) -> str:
        with self.assertRaises(memory_build.FormatError) as caught:
            memory_build.apply_review(memory, BRIEF, plan, text, scope, [], None)
        return "\n".join(caught.exception.failures)

    def test_audit_records_points_coverage_and_audit_done(self) -> None:
        memory = fresh_memory()
        result = memory_build.apply_review(memory, BRIEF, PLAN_V1, REVIEW_V1, "audit", [], "P001")
        self.assertEqual((result["open"], result["new"], result["next_id"]), ("0/1/0", 1, "P002"))
        text = memory.render()
        self.assertIn("Audit: done v1", text)
        self.assertIn("- P001 [major] (D2, C-1) new v1: Deadline is unbounded", text)
        self.assertIn("- open D2: P001", text)
        self.assertIn("- v1 audit:", text)
        self.assertEqual(memory_build.memory_sections(text)[1], [])

    def test_audit_requires_full_coverage_and_no_late(self) -> None:
        partial = review(1, "1. P001 [major] (D2) x → y\n", "- ok: C-1\n- D2: P001\n")
        self.assertIn("audit coverage misses", self.failures(fresh_memory(), PLAN_V1, partial, "audit"))
        late = REVIEW_V1.replace("[major] (D2", "[major] [late] (D2")
        self.assertIn("tagged [late] in an audit", self.failures(fresh_memory(), PLAN_V1, late, "audit"))
        observed = REVIEW_V1 + "\n## Observations\n- (D1) x\n"
        self.assertIn("writes no ## Observations",
                      self.failures(fresh_memory(), PLAN_V1, observed, "audit"))

    def test_converge_routes_new_points_on_unlisted_anchors(self) -> None:
        unlisted = review(2, "1. P002 [major] (S1) x → y\n", "- ok: D2\n")
        self.assertIn("names no anchor listed",
                      self.failures(audited_memory(), plan_v2(), unlisted, "converge"))
        minor_late = review(2, "1. P002 [minor] [late] (S1) x → y Missed: z\n", "- ok: D2\n")
        self.assertIn("below blocker",
                      self.failures(audited_memory(), plan_v2(), minor_late, "converge"))
        no_reason = review(2, "1. P002 [blocker] [late] (S1) x → y\n", "- ok: D2\n")
        self.assertIn("no 'Missed: <reason>'",
                      self.failures(audited_memory(), plan_v2(), no_reason, "converge"))
        wrong_coverage = review(2, "", "- ok: D2, S1\n", "NO_FURTHER_COMMENTS")
        self.assertIn("exactly the ## Changes anchors",
                      self.failures(audited_memory(), plan_v2(), wrong_coverage, "converge"))

    def test_converge_closes_points_settles_and_keeps_observations(self) -> None:
        memory = audited_memory()
        text = review(2, "1. P002 [blocker] [late] (S1) gap → fix Missed: hidden\n", "- ok: D2\n",
                      extra="\n## Observations\n- (D1) wording could be tighter\n")
        result = memory_build.apply_review(memory, BRIEF, plan_v2(), text, "converge", [], "P002")
        self.assertEqual((result["closed"], result["late"], result["observations"]), (1, 1, 1))
        rendered = memory.render()
        self.assertIn("Audit: done v1", rendered)
        self.assertIn("- P001 [major] (D2, C-1) closed v2 fixed:", rendered)
        self.assertIn("- D2 — P001 (v2): bounded deadline.", rendered)
        self.assertIn("- P002 [blocker] [late] (S1) late v2: gap", rendered)
        self.assertIn("- v2 (D1): wording could be tighter", rendered)
        self.assertIn("- verified v2: C-1, D2", rendered)
        self.assertIn("- open S1: P002", rendered)
        self.assertEqual(memory_build.Memory.parse(rendered).closed_ids(), {"P001"})

    def test_closed_id_cannot_be_reused(self) -> None:
        memory = audited_memory()
        memory_build.apply_review(memory, BRIEF, plan_v2(),
                                  review(2, "", "- ok: D2\n", "NO_FURTHER_COMMENTS"),
                                  "converge", [], None)
        reused = review(3, "1. P001 [major] (D2) again → fix\n", "- D2: P001\n")
        self.assertIn("reuses a closed", self.failures(memory, plan_v2(), reused, "converge"))


class MemoryFormatTests(unittest.TestCase):
    def test_blindness_and_line_length_fail(self) -> None:
        text = audited_memory().render()
        leaked = text.replace("## Round log\n", "## Round log\n- note: two rounds remaining\n")
        self.assertTrue(any("reveals progress" in f for f in memory_build.memory_sections(leaked)[1]))
        long_line = text.replace("## Observations\n", "## Observations\n- " + "x" * 600 + "\n")
        self.assertTrue(any("600" in f for f in memory_build.memory_sections(long_line)[1]))
        reordered = text.replace("## Observations\n", "").replace("## Round log\n",
                                                                  "## Round log\n## Observations\n")
        self.assertTrue(memory_build.memory_sections(reordered)[1])

    def test_settled_text_may_name_limits(self) -> None:
        memory = audited_memory()
        memory.settled["D2"] = (["P001 (v2)"], "the iteration limit stays 4")
        self.assertEqual(memory_build.memory_sections(memory.render())[1], [])


class CompactTests(unittest.TestCase):
    def big_memory(self) -> str:
        memory = audited_memory()
        memory.closed = [f"- P{n:03d} [major] (D{n % 3 + 1}) closed v{n // 10 + 1} fixed: issue {n}"
                         for n in range(2, 160)]
        memory.log += [f"- v{n} converge: responses 1 (x); CHANGES_REQUESTED open 0/1/0; new 1, "
                       f"late 0, observations 0, maintained 0, closed 1" for n in range(2, 60)]
        return memory.render()

    def test_compact_groups_closed_points_and_keeps_sections(self) -> None:
        text = self.big_memory()
        self.assertTrue(memory_build.at_or_over(text, 200, 16 * 1024))
        result = memory_build.compact_text(text)
        self.assertFalse(memory_build.at_or_over(result, 200, 16 * 1024))
        before, _ = memory_build.memory_sections(text)
        after, failures = memory_build.memory_sections(result)
        self.assertEqual(failures, [])
        for name in memory_build.KEPT_SECTIONS:
            self.assertEqual(before[name], after[name], name)
        self.assertEqual(memory_build.Memory.parse(result).closed_ids(),
                         memory_build.Memory.parse(text).closed_ids())
        self.assertEqual(len([l for l in after["Round log"] if memory_build.ROUND_LINE.match(l)]), 3)
        self.assertTrue(any(memory_build.SUMMARY_LINE.match(l) for l in after["Round log"]))
        self.assertEqual(memory_build.compact_text(result).count("summary"), 1)


class SeedTests(unittest.TestCase):
    STATUS = ("# Status — demo\nIteration: 16 of 16\nNext point ID: P026\n"
              "Retired point IDs: P004\nTrend: 5, 4, 3\n")
    LEGACY_REVIEW = ("# Review — demo\nVersion: 16\nModel: other\nVerdict: CHANGES_REQUESTED\n\n"
                     "## Points\n1. P025 [major] D9 is unsafe → gate it\n")

    def test_seed_records_open_closed_and_pending_audit(self) -> None:
        text = memory_build.seed_text(BRIEF, PLAN_V1, self.LEGACY_REVIEW, self.STATUS, "pending")
        parts, failures = memory_build.memory_sections(text)
        self.assertEqual(failures, [])
        self.assertEqual(parts["Coverage"][0], "Audit: pending")
        self.assertEqual(parts["Open points"],
                         ["- P025 [major] (legacy) seeded v16: D9 is unsafe"])
        closed = memory_build.Memory.parse(text).closed_ids()
        self.assertIn("P001", closed)
        self.assertNotIn("P004", closed)
        self.assertNotIn("P025", closed)
        self.assertIn("- seed v16: retired P004", text)
        self.assertIn("- pre-memory trend: 5, 4, 3", text)

    def test_waived_audit(self) -> None:
        text = memory_build.seed_text(BRIEF, PLAN_V1, None, self.STATUS, "waived")
        self.assertIn("Audit: waived at seed v16", text)


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

    def test_init_scope_update_feedback_round_trip(self) -> None:
        self.write("brief.md", BRIEF)
        self.assertEqual(self.run_cli("init").returncode, 0)
        self.assertEqual(self.run_cli("init").returncode, 2)
        self.write("plan.md", PLAN_V1)
        self.write("status.md", "# Status — demo\nIteration: 1 of 4\nNext point ID: P001\n"
                                "Retired point IDs: none\n")
        self.commit("brief.md", "memory.md", "plan.md", "status.md")
        scope = self.run_cli("scope")
        self.assertEqual(json.loads(scope.stdout), {"scope": "audit"})
        self.write("review.md", REVIEW_V1)
        self.assertEqual(self.run_cli("update", "--scope", "converge").returncode, 2)
        updated = self.run_cli("update", "--scope", "audit")
        self.assertEqual(updated.returncode, 0, updated.stderr)
        self.assertEqual(json.loads(updated.stdout)["next_id"], "P002")
        self.commit("review.md", "memory.md")
        self.assertEqual(json.loads(self.run_cli("scope").stdout), {"scope": "converge"})

        self.write("status.md", "# Status — demo\nIteration: 2 of 5\n")
        self.assertEqual(json.loads(self.run_cli("feedback").stdout), {"changed": False})
        self.write("brief.md", BRIEF.replace("None\n", "- 2026-11-26: narrow scope\n"))
        no_audit = self.run_cli("feedback", "--no-audit", "--retired", "P001")
        self.assertEqual(no_audit.returncode, 0, no_audit.stderr)
        memory = (self.repo / TASK / "memory.md").read_text(encoding="utf-8")
        self.assertIn("Audit: done v1", memory)
        self.assertIn("- feedback v2: retired P001", memory)
        self.assertNotIn("- P001 [major]", memory.split("## Open points")[1].split("##")[0])
        self.assertEqual(self.run_cli("feedback").returncode, 0)
        self.assertIn("Audit: pending", (self.repo / TASK / "memory.md").read_text("utf-8"))

    def test_seed_and_compact_refusals(self) -> None:
        self.write("brief.md", BRIEF)
        self.write("status.md", "# Status — demo\nIteration: 3 of 4\nNext point ID: P004\n")
        self.commit("brief.md", "status.md")
        self.assertEqual(self.run_cli("compact").returncode, 2)
        seeded = self.run_cli("seed", "--audit", "pending")
        self.assertEqual(seeded.returncode, 0, seeded.stderr)
        self.commit("memory.md")
        self.assertEqual(self.run_cli("seed", "--audit", "pending").returncode, 2)
        below = self.run_cli("compact")
        self.assertEqual(below.returncode, 2)
        self.assertIn("below the soft limit", below.stderr)

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
