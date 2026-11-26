from __future__ import annotations

import os
import re
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


SKILL = Path(__file__).resolve().parent.parent / "SKILL.md"
RESUME = SKILL.parent / "references" / "resume.md"
OPERATIONS = SKILL.parent / "references" / "operations.md"
LIMIT = SKILL.parent / "references" / "limit.md"
RECOVERY = SKILL.parent / "references" / "recovery.md"
VALIDATION = SKILL.parent / "references" / "validation.md"
PROMPTS = SKILL.parent / "references" / "prompts.md"
BUILDER = SKILL.parent / "scripts" / "prompt_build.py"
TASK_NAME = "task-demo-run_20261101-120000Z-abcd"
BUDGET_WORDS = re.compile(
    r"(?i)\biteration\b|\bv\d|version:? *\d|\bfinal\b|last round|remaining|budget|\blimit\b"
)


def block(name: str) -> str:
    text = PROMPTS.read_text(encoding="utf-8")
    start, end = f"<!-- BEGIN {name} -->", f"<!-- END {name} -->"
    return text.split(start, 1)[1].split(end, 1)[0].strip()


REFERENCE_TASK_BYTES = 100


def render(mode: str, reference_path: bool = False) -> str:
    """Render a prompt with the shipped builder, exactly as the orchestrator does.

    `reference_path` swaps the temporary task path for a fixed 100-byte path so byte budgets
    do not depend on where the tests run.
    """
    with tempfile.TemporaryDirectory() as directory:
        task = Path(directory).resolve() / "workspace" / "outcomes" / TASK_NAME
        task.mkdir(parents=True)
        result = subprocess.run(
            ["python3", "-I", "-B", str(BUILDER), mode, "--task", str(task),
             "--model", "example-model-1", "--plan-cap", "150", "--review-cap", "50",
             "--next-id", "P007", "--retired", "none", "--pushed-back", "none"],
            check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
    if result.returncode != 0:
        raise AssertionError(f"prompt_build {mode} exited {result.returncode}: {result.stderr!r}")
    text = result.stdout.decode("utf-8")
    if reference_path:
        suffix = f"/workspace/outcomes/{TASK_NAME}"
        reference = "/" + "r" * (REFERENCE_TASK_BYTES - len(suffix) - 1) + suffix
        text = text.replace(str(task), reference)
    return text


def extract(marker: str) -> str:
    text = SKILL.read_text(encoding="utf-8")
    start = f"<!-- BEGIN {marker} -->"
    end = f"<!-- END {marker} -->"
    body = text.split(start, 1)[1].split(end, 1)[0].strip()
    if body.startswith("```python") and body.endswith("```"):
        body = body[len("```python") : -len("```")].strip()
    return body


def namespace(marker: str) -> dict:
    result = {"__name__": f"test_{marker.lower()}"}
    exec(compile(extract(marker), str(SKILL), "exec"), result)
    return result


class DigestSnippetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        (self.root / "references").mkdir()
        (self.root / "scripts").mkdir()
        (self.root / "SKILL.md").write_text("skill\n", encoding="utf-8")
        self.digest = namespace("SKILL_TREE_DIGEST_PY")["skill_digest"]

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_deterministic_and_content_sensitive(self) -> None:
        before = self.digest(self.root)
        self.assertEqual(before, self.digest(self.root))
        (self.root / "references" / "resume.md").write_text("resume\n", encoding="utf-8")
        self.assertNotEqual(before, self.digest(self.root))

    def test_mode_is_hashed(self) -> None:
        path = self.root / "scripts" / "helper.py"
        path.write_text("pass\n", encoding="utf-8")
        before = self.digest(self.root)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        self.assertNotEqual(before, self.digest(self.root))

    def test_child_symlink_and_special_file_are_rejected(self) -> None:
        (self.root / "link").symlink_to(self.root / "SKILL.md")
        with self.assertRaises(RuntimeError):
            self.digest(self.root)

    def test_benign_artifacts_are_excluded(self) -> None:
        before = self.digest(self.root)
        for name in (".DS_Store", ".file.swp", ".#lock", "#auto#", "backup~"):
            (self.root / name).write_text("ignored\n", encoding="utf-8")
        cache = self.root / "__pycache__"
        cache.mkdir()
        (cache / "x.pyc").write_bytes(b"bytes")
        self.assertEqual(before, self.digest(self.root))
        (self.root / "backup.tmp").write_text("included\n", encoding="utf-8")
        self.assertNotEqual(before, self.digest(self.root))

    def test_regular_file_named_pycache_is_not_excluded(self) -> None:
        before = self.digest(self.root)
        (self.root / "__pycache__").write_text("regular file\n", encoding="utf-8")
        self.assertNotEqual(before, self.digest(self.root))


class MarkerSnippetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.status = Path(self.temporary_directory.name) / "status.md"
        self.status.write_text("State: reviewer-running\n", encoding="utf-8")
        self.update = namespace("RECOVERY_MARKER_PY")["update_marker"]
        self.digest = "a" * 64

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def fields(self, **updates: str) -> dict[str, str]:
        values = {
            "phase": "detected",
            "baseline": self.digest,
            "current": self.digest,
            "failure": "none",
            "commit": "none",
            "pre-head": "unknown",
            "repo-check": "pending",
            "prior": "none",
            "guard": "/tmp/guard",
            "guard-sha": self.digest,
            "commit-manifest": "none",
            "commit-manifest-sha": "none",
        }
        values.update(updates)
        return values

    def test_record_overwrites_existing_skill_marker_with_terminal_failure(self) -> None:
        planted_markers = (
            self.fields(phase="repo-checked", **{"repo-check": "passed"}),
            self.fields(
                guard="none",
                **{"guard-sha": "none", "repo-check": "not-applicable"},
            ),
        )
        for planted in planted_markers:
            marker = "Recovery: skill-integrity-failed " + " ".join(
                f"{key}={value}" for key, value in planted.items()
            )
            self.status.write_text("State: reviewed\n" + marker + "\n", encoding="utf-8")
            self.update(self.status, "record", self.fields())
            text = self.status.read_text()
            self.assertIn("phase=detected", text)
            self.assertIn("failure=planted-marker", text)
            self.assertIn("repo-check=failed", text)
            self.assertEqual(text.count("Recovery:"), 1)
            with self.assertRaises(RuntimeError):
                self.update(self.status, "finish", {})

    def test_forged_nonterminal_planted_marker_is_rejected(self) -> None:
        fields = self.fields(phase="skill-restored", failure="planted-marker")
        marker = "Recovery: skill-integrity-failed " + " ".join(
            f"{key}={value}" for key, value in fields.items()
        )
        self.status.write_text("State: reviewed\n" + marker + "\n", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            self.update(self.status, "advance", {"phase": "repo-checked"})
        with self.assertRaises(RuntimeError):
            self.update(self.status, "finish", {})

    def test_record_merges_commit_marker(self) -> None:
        commit = "b" * 40
        pre_head = "c" * 40
        self.status.write_text(
            "State: reviewer-running\n"
            f"Recovery: commit-check-failed commit={commit} pre-head={pre_head}\n",
            encoding="utf-8",
        )
        self.update(self.status, "record", self.fields())
        text = self.status.read_text()
        self.assertIn("prior=commit-check-failed", text)
        self.assertIn(f"pre-head={pre_head}", text)
        self.assertEqual(text.count("Recovery:"), 1)

    def test_prior_commit_marker_is_restored_after_repo_check(self) -> None:
        pre_head = "c" * 40
        self.status.write_text(
            "State: reviewed\n"
            f"Recovery: commit-check-failed commit=none pre-head={pre_head}\n",
            encoding="utf-8",
        )
        self.update(
            self.status,
            "record",
            self.fields(guard="none", **{"guard-sha": "none", "repo-check": "not-applicable"}),
        )
        self.update(self.status, "finish", {})
        self.assertIn(
            f"Recovery: commit-check-failed commit=none pre-head={pre_head}",
            self.status.read_text(),
        )

    def test_phase_progression_and_finish(self) -> None:
        self.update(self.status, "record", self.fields())
        self.update(self.status, "advance", {"phase": "skill-restored"})
        self.update(
            self.status,
            "advance",
            {"phase": "repo-checked", "repo-check": "passed"},
        )
        self.update(self.status, "finish", {})
        self.assertNotIn("Recovery:", self.status.read_text())

    def test_unverified_commit_is_terminal(self) -> None:
        self.update(self.status, "record", self.fields(commit="b" * 40))
        self.update(self.status, "advance", {"phase": "skill-restored"})
        self.update(
            self.status,
            "advance",
            {"phase": "repo-checked", "repo-check": "passed"},
        )
        with self.assertRaises(RuntimeError):
            self.update(self.status, "finish", {})

    def test_failed_repository_check_is_terminal(self) -> None:
        self.update(self.status, "record", self.fields())
        self.update(
            self.status,
            "advance",
            {"phase": "skill-restored", "repo-check": "failed"},
        )
        with self.assertRaises(RuntimeError):
            self.update(self.status, "advance", {"phase": "repo-checked"})

    def test_phase_operations_are_idempotent_after_crash(self) -> None:
        self.update(self.status, "record", self.fields())
        self.update(self.status, "advance", {"phase": "skill-restored"})
        self.update(self.status, "advance", {"phase": "skill-restored"})
        self.update(
            self.status,
            "advance",
            {"phase": "repo-checked", "repo-check": "passed"},
        )
        self.update(
            self.status,
            "advance",
            {"phase": "repo-checked", "repo-check": "passed"},
        )
        self.assertIn("phase=repo-checked", self.status.read_text())

    def test_invalid_marker_values_are_rejected(self) -> None:
        with self.assertRaises(RuntimeError):
            self.update(
                self.status,
                "record",
                self.fields(phase="repo-checked", **{"repo-check": "pending"}),
            )

    def test_impossible_manifest_repository_combinations_are_rejected(self) -> None:
        invalid = (
            self.fields(guard="none", **{"guard-sha": "none"}),
            self.fields(**{"repo-check": "not-applicable"}),
            self.fields(
                phase="repo-checked",
                **{"repo-check": "passed", "guard": "none", "guard-sha": "none"},
            ),
            self.fields(
                phase="repo-checked",
                **{
                    "repo-check": "not-applicable",
                    "guard": "none",
                    "guard-sha": "none",
                },
            ),
        )
        for fields in invalid:
            self.status.write_text("State: reviewer-running\n", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                self.update(self.status, "record", fields)

    def test_planted_unguarded_checked_marker_cannot_finish(self) -> None:
        fields = self.fields(
            phase="repo-checked",
            **{
                "repo-check": "not-applicable",
                "guard": "none",
                "guard-sha": "none",
            },
        )
        marker = "Recovery: skill-integrity-failed " + " ".join(
            f"{key}={value}" for key, value in fields.items()
        )
        self.status.write_text("State: reviewed\n" + marker + "\n", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            self.update(self.status, "finish", {})

    def test_running_state_requires_guard_evidence(self) -> None:
        fields = self.fields(
            guard="none", **{"guard-sha": "none", "repo-check": "not-applicable"}
        )
        with self.assertRaises(RuntimeError):
            self.update(self.status, "record", fields)

    def test_running_state_accepts_only_terminal_precapture_marker(self) -> None:
        fields = self.fields(
            guard="none",
            **{
                "guard-sha": "none",
                "failure": "pre-capture",
                "repo-check": "failed",
            },
        )
        self.update(self.status, "record", fields)
        self.assertIn("failure=pre-capture", self.status.read_text())
        with self.assertRaises(RuntimeError):
            self.update(self.status, "advance", {"phase": "skill-restored"})

    def test_multiple_markers_fail(self) -> None:
        self.status.write_text(
            "Recovery: commit-check-failed commit=none pre-head=x\n"
            "Recovery: commit-check-failed commit=none pre-head=y\n",
            encoding="utf-8",
        )
        with self.assertRaises(RuntimeError):
            self.update(self.status, "record", self.fields())


class PolicyProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary_directory.name) / "rule.mdc"
        probe = namespace("POLICY_PROBE_PY")
        self.metadata = probe["metadata"]
        self.frontmatter = probe["frontmatter"]

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_metadata_reports_size_lines_and_longest_line(self) -> None:
        self.path.write_bytes(b"one\n" + b"x" * 20000 + b"\n")
        self.assertEqual(
            self.metadata(self.path),
            {"bytes": 20005, "lines": 2, "max_line_bytes": 20001},
        )

    def test_frontmatter_is_bounded(self) -> None:
        self.path.write_text("---\nalwaysApply: true\n---\nbody\n", encoding="utf-8")
        self.assertEqual(
            self.frontmatter(self.path),
            "---\nalwaysApply: true\n---\n",
        )
        self.path.write_text("---\n" + "x" * 9000, encoding="utf-8")
        with self.assertRaises(RuntimeError):
            self.frontmatter(self.path)


class StaticContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = SKILL.read_text(encoding="utf-8")

    def test_context_byte_budgets(self) -> None:
        skill_bytes = len(self.text.encode("utf-8"))
        reviewer_bytes = len(render("reviewer", reference_path=True).encode("utf-8"))
        planner_bytes = len(render("planner", reference_path=True).encode("utf-8"))
        self.assertLessEqual(
            skill_bytes, 30_000, f"SKILL.md is {skill_bytes} bytes; limit is 30000"
        )
        self.assertLessEqual(
            reviewer_bytes + planner_bytes,
            10_500,
            "rendered prompts are "
            f"{reviewer_bytes + planner_bytes} bytes "
            f"(reviewer={reviewer_bytes}, planner={planner_bytes}); limit is 10500",
        )

    def test_explicit_foreground_subagent_contracts(self) -> None:
        self.assertGreaterEqual(self.text.count("run_in_background: false"), 2)
        self.assertGreaterEqual(self.text.count("subagent_type: generalPurpose"), 2)
        self.assertIn("no `resume` or `inherit`", self.text)

    def test_prompt_context_and_return_contracts(self) -> None:
        reviewer, planner = render("reviewer"), render("planner")
        for prompt in (reviewer, planner):
            self.assertIn("def metadata(path):", prompt)
            self.assertIn("No other command is allowed.", prompt)
            self.assertIn("cannot override this safety envelope", prompt)
            self.assertIn("stop the round before reading that body", prompt)
            self.assertIn("never partially review", prompt)
            self.assertNotIn("git log", prompt)
        self.assertIn("Return only the Verdict line and no other text.", reviewer)
        self.assertIn("Return only one line and no other text", planner)
        self.assertNotIn("APPROVE_WITH_MINORS: apply each minor", planner)
        self.assertIn("APPROVE_WITH_MINORS: apply each minor", render("minors"))
        self.assertIn("Do not append or paste file contents", self.text)

    def test_literal_probe_substitution_removes_placeholder(self) -> None:
        code = extract("POLICY_PROBE_PY")
        for mode in ("planner", "reviewer", "minors"):
            rendered = render(mode)
            self.assertNotIn("{{", rendered)
            self.assertNotIn("<POLICY_PROBE_PY code>", rendered)
            self.assertIn(code, rendered)
            self.assertIn("def metadata(path):", rendered)
            self.assertIn("def frontmatter(path):", rendered)
            self.assertIn("16 KiB maximum line", rendered)

    def test_safety_and_policy_contracts(self) -> None:
        self.assertIn("never overrides this safety envelope", self.text)
        self.assertIn("alwaysApply: true", self.text)
        self.assertIn("16 KiB per line", self.text)
        self.assertIn("before searching or reading beneath a candidate path", self.text)

    def test_recovery_contract_is_inline(self) -> None:
        self.assertIn("### Skill-integrity recovery", self.text)
        self.assertIn("repo-check=failed` is terminal", self.text)
        self.assertIn("--recovery-status", self.text)

    def test_checkpoint_resume_and_self_planner_contracts(self) -> None:
        bootstrap = self.text.split("## Runtime bootstrap", 1)[1].split(
            "### Branch preflight", 1
        )[0]
        self.assertIn("Before every skill script, reference", bootstrap)
        self.assertIn("subagent launch/return, and `git commit`", bootstrap)
        recovery = self.text.split("### Skill-integrity recovery", 1)[1].split(
            "## Format and consistency checks", 1
        )[0]
        self.assertIn("every present task file as regular", recovery)
        self.assertIn("phase=detected", recovery)
        self.assertIn("phase=skill-restored", recovery)
        self.assertIn("phase=repo-checked", recovery)
        model_checks = self.text.split("### Model checks", 1)[1].split(
            "### Branch preflight", 1
        )[0]
        self.assertIn("only when the user explicitly selects `--planner-model self`", model_checks)
        self.assertIn("Use a new planner subagent for every version", model_checks)
        resume = RESUME.read_text(encoding="utf-8")
        self.assertIn("`repo-check=failed` is terminal", resume)
        self.assertIn("reaches this reference or an ordinary recovery choice", resume)

    def test_digest_gated_operations_reference_retains_moved_contracts(self) -> None:
        operations = OPERATIONS.read_text(encoding="utf-8")
        normalized_skill = " ".join(self.text.split())
        normalized_operations = " ".join(operations.split())
        gate = (
            "Operations gate: digest-check/read `operations.md` once per chat before new "
            "task-folder creation (resume: before first write). At each commit/output "
            "validation digest-check; reread only after context loss."
        )
        self.assertEqual(normalized_skill.count(gate), 1)
        self.assertEqual(
            normalized_operations.count(
                "Digest-check/read once per chat before new task-folder creation (resume: before "
                "first write). At commits/output validation recheck digest; reread only after "
                "context loss."
            ),
            1,
        )
        for contract in (
            "# Brief — <title>",
            "# Plan — <title>",
            "# Review — <title>",
            "# Status — <title>",
            "## Commit transaction",
            "## Format checks",
            "plan-start",
            "review-start",
            "Next point ID",
        ):
            self.assertIn(contract, operations)

    def test_operations_retains_task_path_and_review_correctness_invariants(self) -> None:
        operations = " ".join(OPERATIONS.read_text(encoding="utf-8").split())
        for contract in (
            "Candidate must not exist as a file, directory, or dangling symlink.",
            "On collision, rename and rerun ignore preflight for that exact candidate.",
            "After plain `mkdir`, `lstat` a real directory and require its canonical path to be a "
            "direct child of canonical `workspace/outcomes`.",
            "`closed by user`; only retired IDs use the last, and every retired ID in that review "
            "must use it.",
            "Accept lines match `^P[0-9]{3,}: accept rebuttal$`",
            "maintain lines match `^P[0-9]{3,}: maintain, because .+: .+$`",
        ):
            self.assertEqual(operations.count(contract), 1, contract)


class HonestLimitContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.skill = SKILL.read_text(encoding="utf-8")
        cls.operations = " ".join(OPERATIONS.read_text(encoding="utf-8").split())
        cls.planner = " ".join(block("PLANNER_PROMPT").split())
        cls.reviewer = " ".join(block("REVIEWER_PROMPT").split())

    def test_planner_template_requires_complete_replan(self) -> None:
        for sentence in (
            "Each version is a complete, self-contained plan of the whole brief scope, not a delta.",
            "Answer every open point in full in this version; never defer, split across "
            "versions, or mark work for later.",
        ):
            self.assertIn(sentence, self.planner)
        self.assertNotIn("Resolve every point now.", self.planner)

    def test_operations_has_deferral_format_rule(self) -> None:
        self.assertIn(
            "A Response reason or `## Plan` containing `defer`, `next round`, `next version`, "
            "`follow-up`, `TBD` or `TODO`, or a `## Plan` that points to an earlier version "
            "instead of restating it, fails format.",
            self.operations,
        )

    def test_reviewer_template_reviews_whole_plan_at_true_severity(self) -> None:
        for sentence in (
            "Before writing the Verdict, build a checklist of every brief Constraint, In item, "
            "Out item, numbered question and Done criterion, and every plan anchor; check each "
            "against the plan and the repo.",
            "Do not stop after the first issue: report every blocker, major and minor you find "
            "in this one review",
            "severity follows impact alone",
            "`## Coverage` lists every checklist item exactly once",
        ):
            self.assertIn(sentence, self.reviewer)
        for kept in ("allocate monotonically from the supplied ID", "never reuse, renumber, or use "
                     "retired IDs", "Settle pushback from cited evidence", "maintain only with "
                     "counterevidence", "Minor means no effect on safety, feasibility, scope, cost, "
                     "or validation"):
            self.assertIn(kept, self.reviewer)

    def test_caused_by_rule_removed(self) -> None:
        for path in (SKILL, PROMPTS, OPERATIONS, RECOVERY, VALIDATION):
            text = path.read_text(encoding="utf-8")
            for banned in ("Caused by", "Raise every issue in v1", "final-iteration",
                           "Iteration N of L", "otherwise new points are minor"):
                self.assertNotIn(banned, text, f"{path.name}: {banned}")

    def test_templates_are_iteration_blind(self) -> None:
        for name in ("PLANNER_PROMPT", "REVIEWER_PROMPT", "MINORS_LINE"):
            found = BUDGET_WORDS.findall(block(name))
            self.assertEqual(found, [], f"{name} reveals progress or budget: {found}")

    def test_rendered_prompts_disclose_no_budget(self) -> None:
        for mode in ("planner", "reviewer", "minors"):
            rendered = render(mode)
            self.assertNotIn("Iteration:", rendered, mode)
            self.assertIsNone(re.search(r"\b\d+ of \d+\b", rendered), mode)

    def test_prompts_do_not_read_status_md(self) -> None:
        for name in ("PLANNER_PROMPT", "REVIEWER_PROMPT", "MINORS_LINE"):
            self.assertNotIn("status.md", block(name), name)

    def test_templates_define_version_derivation(self) -> None:
        self.assertIn(
            "`Version:` = 1 + the larger `Version:` header of plan.md and review.md "
            "(1 when neither exists)",
            self.planner,
        )
        self.assertIn("`Version:` equal to plan.md `Version:`", self.reviewer)
        self.assertIn('"plan v<the Version you wrote> written', self.planner)

    def test_limit_and_operations_state_brief_optional_for_feedback(self) -> None:
        limit = " ".join(LIMIT.read_text(encoding="utf-8").split())
        self.assertIn("A K-only grant commits `status.md` alone", limit)
        self.assertIn("`Iteration: N of N+K`", limit)
        self.assertIn(
            "`feedback`: `status.md` required; `brief.md` optional, changed only for dated user "
            "feedback or retired-point decisions (a K-only grant changes `status.md` alone)",
            self.operations,
        )
        self.assertIn("`brief.md` never records the iteration count", self.operations)
        self.assertNotIn("APPROVE_WITH_MINORS is not Limit reached", limit)

    def test_skill_decide_contains_gate_sequence(self) -> None:
        decide = self.skill.split("4. **Decide**", 1)[1].split("### Minors close", 1)[0]
        decide = " ".join(decide.split())
        order = (
            "`NO_FURTHER_COMMENTS` at any N ≤ limit → Consensus",
            "Open points remain (`CHANGES_REQUESTED` or `APPROVE_WITH_MINORS`) and N = limit → ",
            "commit `limit-reached vN`",
            "`APPROVE_WITH_MINORS` and N < limit → Minors close",
            "`CHANGES_REQUESTED` and N < limit → commit `plan-start vN+1` with `Iteration: N+1 of L`",
        )
        positions = [decide.index(item) for item in order]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("the limit is checked here, never in a prompt or inside a round", decide)

    def test_skill_minors_close_is_reviewed(self) -> None:
        minors = " ".join(self.skill.split("### Minors close", 1)[1].split("## Repository guard", 1)[0].split())
        self.assertIn("Only when N < limit: commit `plan-start vN+1` with `Iteration: N+1 of L`", minors)
        self.assertIn("validate and commit `plan vN+1` at `reviewing`, then run Review", minors)
        self.assertIn("Consensus follows only a `NO_FURTHER_COMMENTS` review.", minors)
        for gone in ("max(limit", "`minors`", "awaiting-user"):
            self.assertNotIn(gone, minors)
        consensus = self.skill.split("## Consensus", 1)[1].split("## Triggers", 1)[0]
        self.assertNotIn("Minors close step 2", consensus)
        self.assertIn("the session ends", consensus)

    def test_skill_has_no_inline_prompt_templates(self) -> None:
        for gone in ("### Reviewer prompt", "### Planner prompt",
                     "You are the independent senior plan reviewer",
                     "You are the engineering planner", "<POLICY_PROBE_PY code>"):
            self.assertNotIn(gone, self.skill)
        self.assertIn("### Prompts", self.skill)
        self.assertIn("scripts/prompt_build.py", self.skill)
        self.assertIn("references/prompts.md", self.skill)

    def test_rules_require_builder_output(self) -> None:
        rules = " ".join(self.skill.split("## Rules", 1)[1].split("## Validation", 1)[0].split())
        self.assertIn(
            "Prompts are exactly `prompt_build.py` output; the orchestrator adds no text, "
            "including in a last or retried round.",
            rules,
        )
        prompts = " ".join(self.skill.split("### Prompts", 1)[1].split("## Consensus", 1)[0].split())
        self.assertIn("Pass stdout verbatim as the Task prompt; never edit, prefix, append, or paste.", prompts)

    def test_retry_and_subject_rules_are_documented(self) -> None:
        self.assertIn("a retried launch commits the exact subject", self.operations)
        self.assertIn("--subject", self.operations)
        self.assertIn("pre --subject", self.skill)


if __name__ == "__main__":
    unittest.main()
