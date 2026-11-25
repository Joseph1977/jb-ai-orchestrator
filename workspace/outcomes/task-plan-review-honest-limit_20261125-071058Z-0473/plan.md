# Plan — Plan-review skill: honest rounds and a real iteration limit
Version: 3
Model: claude-opus-5-5-medium

## Response to review

| Point | Decision | Reason |
|---|---|---|
| P003 | accepted | M4 now checks only configured-budget disclosure (an `Iteration:` line, this run's `N of L`, the active maximum, final/last-round wording); limits as subject matter are allowed. The static blindness test scans template text, not filled task-folder names (Tests In-3; M4). |
| P004 | accepted | The "consensus after minors close" outcome is removed. Minors close now produces `plan vN+1`, which is reviewed like any other plan: consensus only on `NO_FURTHER_COMMENTS` (0/0/0); open minors at N+1 = L end `limit-reached` (gate steps 4–6; Steps 5–8). |

## Plan

### What / where / why
- What: make every round complete (In-1, In-2), hide the budget from agents (In-3), gate the limit between rounds (In-4), record honest outcomes (In-5), build prompts mechanically (In-6), fix retry/transition commits (In-7), align tests/docs (In-8).
- Where: `.cursor/skills/plan-review/` only (Out-1); repo guard, digest and recovery markers unchanged (Out-2); model-family rules unchanged (Out-3).
- Why: the late-point `Caused by` rule, "Iteration N of L" plus final-iteration wording, earlier-points-only re-review, free-text prompt additions and an unreviewed Minors close let runs reach consensus because the budget ran out, not because the plan was sound.
- Size constraint: `SKILL.md` is 29,901 of the 30,000 bytes enforced by `test_context_byte_budgets`, so templates move to a new reference first (Step 1).

### Limit-gate flow (brief question 2)
1. `review vN` committed: `State: reviewed`, `Iteration: N of L`, open points/trend/next ID recorded.
2. Decide by verdict: `NO_FURTHER_COMMENTS` → Consensus (any N ≤ L, including N = 1); Outcome open points are always 0/0/0.
3. Limit check (only here, never in a prompt or inside a round): open points remain (`CHANGES_REQUESTED` or `APPROVE_WITH_MINORS`) and N = L → `limit-reached vN`.
4. `APPROVE_WITH_MINORS` and N < L → Minors close: `plan-start vN+1` with `Iteration: N+1 of L`, planner in `minors` mode, `plan vN+1`, then `review vN+1` and back to step 1.
5. `CHANGES_REQUESTED` and N < L → `plan-start vN+1` with `Iteration: N+1 of L`, then plan and review as usual.
6. `limit-reached`: State `limit-reached`, Iteration and Open points copied unchanged from `review vN`, no consensus; only an explicit user K continues via `feedback vN` setting `Iteration: N of N+K`.

### Step 1 — Move prompt templates out of `SKILL.md` (In-6, enables In-1..In-3)
- New `references/prompts.md` with `<!-- BEGIN PLANNER_PROMPT -->`/`END` blocks for `PLANNER_PROMPT`, `REVIEWER_PROMPT`, `MINORS_LINE`; one heading per block, nothing else.
- Fillable scalars use `{{NAME}}` only; every literal metavariable (`<metadata|frontmatter>`, `<path>`, `<ordinal>`, `<issue>`, `<fix>`, `<ref>`, `<reason>`, `<accepted>`, `<repo path[#section]>`, `<standard § name>`) stays verbatim agent-facing text.
- `SKILL.md` §Reviewer prompt and §Planner prompt become one `### Prompts` section: render only with `prompt_build.py`, pass stdout verbatim as the Task prompt, never edit, prefix, append or paste.
- `SKILL.md` §Rules first bullet: "Prompts are exactly `prompt_build.py` output; the orchestrator adds no text, including in a last or retried round."

### Step 2 — Planner template text (In-1, In-3)
- Read line: "Read each task file once: brief.md, plan.md and review.md if present; then referenced repo paths." (`status.md` removed — it carries Iteration/limit).
- Scalar lines: `Task folder: {{TASK_FOLDER}}`, `Model header: {{MODEL}}`, `Plan length cap: {{PLAN_CAP}} lines`, `Retired point IDs: {{RETIRED_IDS}}`.
- Write line: "Write ONLY plan.md: title, `Version:` = 1 + the larger `Version:` header of plan.md and review.md (1 when neither exists), Model; …" (rest unchanged).
- Replace "Resolve every point now." with: "Each version is a complete, self-contained plan of the whole brief scope, not a delta. Answer every open point in full in this version; never defer, split across versions, or mark work for later."
- Return line: "plan v<the Version you wrote> written — <accepted>/<partly>/<pushed back>"; Evidence, retired-ID and decision vocabulary unchanged.

### Step 3 — Reviewer template text (In-2, In-3)
- Delete "Iteration N of L.", "Raise every issue in v1.", the whole `Caused by` sentence, and "Report final-iteration blockers/majors."
- Read line: "Read each task file once: brief.md, plan.md, prior review.md if present." (`status.md` and "vN" removed).
- Scalar lines: task folder, `Model header: {{MODEL}}`, `Review length cap: {{REVIEW_CAP}} lines`, `Next unused point ID: {{NEXT_POINT_ID}}`, `Retired point IDs: {{RETIRED_IDS}}`, `Pushed-back IDs to assess: {{PUSHED_BACK_IDS}}`.
- Write line: "Write ONLY review.md: title, `Version:` equal to plan.md `Version:`, Model, Verdict; …" (rest unchanged).
- Insert: "Every review covers the whole current plan against brief.md and the repo, not only earlier points. Report every issue you find at its true severity, whether new, previously missed, or caused by a change; severity follows impact alone."
- Keep stable-ID, monotonic allocation, never-reuse/retired, pushback-settlement, minor definition and verdict rules verbatim; pushed-back minors from a Minors close are assessed by these same rules.

### Step 4 — Prompt builder `scripts/prompt_build.py` (In-6, In-3)
- CLI: `prompt_build.py planner|reviewer|minors --task <abs> --model <slug> --plan-cap n --review-cap n [--next-id P###] [--retired IDs|none] [--pushed-back IDs|none]`; no version, iteration or limit option exists.
- Allowed scalars exactly: TASK_FOLDER (absolute, canonical direct child of `workspace/outcomes`, name regex from `SKILL.md` §Task folder), MODEL (`^[a-z0-9][a-z0-9.-]*$`), PLAN_CAP/REVIEW_CAP (1–1000), NEXT_POINT_ID (`^P[0-9]{3,}$`), RETIRED_IDS/PUSHED_BACK_IDS (`none` or comma list of IDs), POLICY_PROBE_PY (read from the `SKILL.md` marker, not the CLI).
- Before substitution, every `{{`/`}}` in the template must belong to a token `\{\{([A-Z_]+)\}\}` whose name is in the allowed set and supplied; substitution is one pass, so values are never rescanned.
- Refuses with exit 2 and reason: unknown option (argparse), a CLI value with whitespace/newline/backtick or outside its regex, a stray brace or unknown/unsupplied token; `<...>` text is never inspected.
- `minors` = planner prompt + exactly the `MINORS_LINE`: "The current review.md is APPROVE_WITH_MINORS: apply each minor or push back with Evidence; change nothing else."
- Prints the prompt to stdout and its sha256 to stderr; reads only `SKILL.md` and `references/prompts.md` beside itself; no local skill imports; runs as `python3 -I -B` after the digest check like every skill script.
- Version derivation is safe: recovery.md §Format failure restores plan.md before a retry, resume.md §Version sequencing launches no planner over an unreviewed plan, and operations.md §Format checks plus `commit_check.py` (`Version does not match`) reject a wrong Version.

### Step 5 — `references/operations.md` (In-1, In-2, In-5, In-7)
- §status.md: add `Outcome: none | consensus at iteration N of L, open points 0/0/0 | limit-reached at iteration L of L, open points b/m/n | stopped at iteration N of L (<reason>)` after `State`; no consensus variant with nonzero counts exists.
- New §Status before each commit table (set before `git add`): brief v1 planning/1 of L/Outcome none; plan-start vN planner-running/N of L/`Next action: planner writes plan vN — attempt k`; plan vN reviewing/N of L; review-start vN reviewer-running/N of L/attempt k; review vN reviewed/N of L + counts; consensus vN awaiting-user/N of L/consensus Outcome; limit-reached vN limit-reached/L of L/limit Outcome; feedback vN planning/N of L′/Outcome none; stopped vN stopped/Stop reason/stopped Outcome; approved vN approved/Outcome unchanged.
- Minors close uses the `plan-start`/`plan` rows above; the `minors` subject and its Allowed-files entry are marked legacy-only.
- Retry rule: a retried launch commits the exact subject `plan-review(<slug>): plan-start vN` (or `review-start vN`) again with no suffix, after incrementing `attempt k` in `Next action`, so the commit has a real diff.
- §Commit transaction: `pre` gains required `--subject "plan-review(<slug>): <step> v<N>"`; `git commit` uses that same string.
- Limit may change only in `brief v1` or `feedback`; `brief.md` never records the iteration count, an `Iteration:` line or this run's limit (agents read it).
- §Format checks: delete the `Caused by` bullet; keep exact `Version: N`; add "A Response reason or `## Plan` containing `defer`, `next round`, `next version`, `follow-up`, `TBD` or `TODO`, or a `## Plan` that points to an earlier version instead of restating it, fails format."

### Step 6 — `commit_check.py` (In-4, In-5, In-7, C-2)
- Refactor the per-commit body of `history()` into `_validate_step(context, step, version, status_text, files)`; `history()` loops over commits; `pre` replays history, then validates the staged tree for `--subject` before any commit.
- `pre`: reject a subject not matching `SUBJECT`, a slug mismatch, or staged `status.md` with the wrong State/Iteration/Outcome for the step; `MANIFEST_VERSION` 3 stores the subject; `post` fails if the commit subject differs.
- `SUBJECT` mismatch message keeps "is not a plan-review step" and adds "; retries reuse the exact subject plan-review(<slug>): <step> v<N>" when a valid subject has a suffix.
- Current format (brief v1 status has `Outcome:`; `pre` rejects a new `brief v1` without it): `consensus` requires latest review `NO_FURTHER_COMMENTS` and Outcome open points 0/0/0; `limit-reached` requires latest review CR/AWM, review Iteration N = its limit, Iteration and Open points equal to the review commit, Open points ≠ 0/0/0.
- Current format also: `minors` is rejected ("Minors close commits plan vN+1 and is reviewed"); `plan-start`/`plan` vN+1 after an AWM review are allowed (lifting the "rejected until minors commits" rule at line ~506) and, like after CR, require review N < its limit; any limit change outside `feedback` fails; terminal commits require the matching `Outcome` with their Iteration and Open points.
- Legacy format (no `Outcome:` in the brief v1 status): existing rules only, including `minors` → `awaiting-user`; no new failure; result adds `"format": "legacy"` and note `legacy status format: honest-limit checks not applied`.

### Step 7 — `SKILL.md` loop text (In-4, In-5, C-1)
- §Loop step 4 Decide replaced by the six-line gate flow.
- §Minors close rewritten: "Only when N < limit: commit `plan-start vN+1` with `Iteration: N+1 of L`, render `prompt_build.py minors`, validate and commit `plan vN+1` at `reviewing`, then run Review. Consensus follows only a `NO_FURTHER_COMMENTS` review." (deletes `max(limit,N+1)`, `awaiting-user` and the `minors` commit).
- §Consensus: "After no comments, use the current version" (drops "or Minors close step 2"); step 1 records the consensus `Outcome`; step 2 lists pushback IDs the reviewer accepted; add "After `approved`, the session ends; implementing changes to this skill runs outside the loop."
- §Loop steps 2–3: "exact retry" points to the operations retry rule; §Commits references `pre --subject`.

### Step 8 — Other references (In-4, In-7, In-8)
- `limit.md`: trigger "review vN with open points (any verdict) and N = limit"; delete "APPROVE_WITH_MINORS is not Limit reached … including when N = limit"; commit sets State/Outcome, keeps Iteration and Open points; K sets `Iteration: N of N+K` in `feedback vN`; brief records retired decisions only.
- `resume.md` step 3: describe `pre --subject`, current vs legacy format and the new checks; mark the `minors` paragraph (line 29) legacy-only and add the current AWM → `plan-start vN+1` rule.
- `resume.md` step 6 `reviewed`: "`APPROVE_WITH_MINORS` goes to Minors close when N < limit, else Limit reached"; `awaiting-user` drops the pushed-back-minor go list; step 7 replaces the `max(limit, N+1)` sentence with "Minors close runs only when N < limit."
- `recovery.md` §Format failure: remove the `Caused by` example, add deferral/partial-plan and wrong-derived-Version examples; retry launches follow the operations retry rule.
- `validation.md` §Loop and format: replace the final-iteration, uncaused-late and "Minors close … handles pushed-back minors" bullets with M1–M9; §Honest claims: verbatim-prompt delivery is manual-only.
- `integrity.md`: no change (searched; it only keeps guard data out of prompts).

### Step 9 — Changes record (AGENTS.md §Keep the docs current)
- `changes/<date>-plan-review-honest-limit.md`: branch/PR, what/why, breaking note (`pre --subject` required, manifest v3, prompts only via `prompt_build.py`, Minors close is reviewed and `minors` is legacy-only; no in-flight transaction may span the upgrade), C-2 behaviour; no Alembic, no migration note; `readme.md`/`docs/ORCHESTRATOR.md` untouched.

### Execution and C-1/C-3
- Implement only after this run is `approved` and its session has ended; no plan-review run may be running in the implementing chat, so the digest change trips no checkpoint.
- Branch `fix/plan-review-honest-limit` from `origin/on-going-dev`; if its `.cursor/skills/plan-review/` differs from this branch, stop and ask; PR targets `on-going-dev` only; no attribution anywhere.
- Order: Step 1 → 4 → 2/3 → 6 → 5/7/8 → tests → 9; then the unit suite from `SKILL.md` §Validation, then a fresh `/plan-review` for M1–M9.

### C-2 answer
- Both old folders stay readable: no file format they use is removed, and their status files lack `Outcome:`, so history runs in legacy mode with no new failures.
- `fe98` ended consensus at 4 of 4 with review v4 `NO_FURTHER_COMMENTS`; `ca88` is at `reviewer-running` v4 with no terminal commit — neither hits a new rule even in current mode.
- Pre-existing corruption stays flagged with existing messages: "`<sha>: subject 'plan-review(<slug>): plan-start v1 retry' is not a plan-review step; retries reuse the exact subject …`", "`status.md Iteration does not match v2`", "`plan must set State reviewing, got planner-running`".

## Files
- Changed: `.cursor/skills/plan-review/SKILL.md`, `references/{operations,limit,resume,recovery,validation}.md`, `scripts/commit_check.py`, `scripts/test_commit_check.py`, `scripts/test_skill_contract.py`.
- New: `references/prompts.md`, `scripts/prompt_build.py`, `scripts/test_prompt_build.py`, `changes/<date>-plan-review-honest-limit.md`.
- Searched, unchanged: `references/integrity.md`, `.cursor/commands/kit-help.md`, `repo_guard.py`, `preflight.py`.

## Tests (must exist and pass)
- In-1: `test_planner_template_requires_complete_replan`; `test_operations_has_deferral_format_rule`.
- In-2: `test_reviewer_template_reviews_whole_plan_at_true_severity`; `test_caused_by_rule_removed` (SKILL, prompts, operations, recovery, validation); stable-ID/pushback strings still asserted; drop the "cited point ID must differ" contract.
- In-3: `test_templates_are_iteration_blind` (raw `prompts.md` blocks contain no `(?i)\biteration\b|\bv\d|version:? *\d|\bfinal\b|last round|remaining|budget|\blimit\b`); `test_rendered_prompts_disclose_no_budget` (all three modes contain no `Iteration:` and no `\b\d+ of \d+\b`); `test_prompts_do_not_read_status_md`.
- In-3 continued: `test_builder_has_no_version_iteration_or_limit_input` (argparse rejects `--version`, `--iteration`, `--limit`); `test_templates_define_version_derivation` (planner and reviewer derivation text present).
- In-4: `test_limit_reached_requires_review_at_limit`; `test_limit_reached_after_minors_verdict_at_limit_passes`; `test_minors_subject_rejected_current_format`; `test_plan_start_after_awm_below_limit_passes`; `test_plan_start_after_review_at_limit_rejected_without_feedback`; `test_feedback_with_k_continues_past_limit_passes`.
- In-4 continued: `test_consensus_requires_no_further_comments`; `test_consensus_after_awm_without_new_review_rejected`; `test_limit_change_outside_feedback_rejected`; `test_limit_reached_preserves_open_points`; `test_skill_decide_contains_gate_sequence`; `test_skill_minors_close_is_reviewed`.
- In-5: `test_outcome_required_on_terminal_commits`; `test_outcome_matches_iteration_and_open_points`; `test_consensus_outcome_nonzero_counts_rejected`; `test_early_consensus_at_iteration_one_of_six_passes`; `test_limit_reached_outcome_four_of_four`.
- In-6: `test_shipped_templates_render` (each mode exits 0 on real `prompts.md`; output keeps `<metadata|frontmatter>`, `<path>`, `<ordinal>`, `<issue>` and contains no `{{`).
- In-6 continued (`test_prompt_build.py`): unknown option, newline/backtick injection, bad path/ID/cap, unknown or unsupplied token, stray brace all exit 2; value containing `{{MODEL}}`-like text is not rescanned; minors adds exactly one line; deterministic sha; no local imports; allowed-scalar set equals template tokens.
- In-6 continued: `test_context_byte_budgets` renders via builder (keeps 30,000/7,500 caps); `test_skill_has_no_inline_prompt_templates`; `test_rules_require_builder_output`.
- In-7: `test_retry_subject_suffix_rejected_with_hint`; `test_plan_start_retry_after_planner_running_passes`; `test_pre_rejects_wrong_state_before_commit`; `test_pre_rejects_iteration_mismatch`; `test_pre_rejects_new_brief_without_outcome`; `test_post_rejects_subject_mismatch`; `test_manifest_v2_rejected`.
- In-8/C-2: `test_legacy_status_format_uses_existing_rules` (fixtures without `Outcome:` keep passing, incl. `test_minors_after_reviewed_passes`, `test_minors_at_limit_passes`, `test_approved_after_minors_passes`, `test_plan_rejected_after_approve_with_minors`, `test_limit_reached_rejected_after_approve_with_minors` as legacy cases); existing `Version does not match` tests kept; helper `step()` passes `--subject`.
- Command: `PYTHONDONTWRITEBYTECODE=1 python3 -I -B -m unittest discover -s "$SKILL_ROOT/scripts"`.

## Manual checks (`references/validation.md`)
- M1 seeded missed major: plan v2 keeps a major defect review v1 missed; review v2 reports it as major with no prefix.
- M2 whole-plan review: review v2 raises a seeded issue in a section untouched by v1 points.
- M3 prompt integrity: each Task prompt equals `prompt_build.py` output (sha256 match); no added text in any round.
- M4 blindness: rendered prompts disclose no budget; brief.md has no `Iteration:` line and does not state this run's limit or "N of L"; plan/review may discuss limits as subject matter; agents never open `status.md`; derived Versions pass format checks in v1, v2 and a Minors close.
- M5 limit: `--max-iterations 1` with a seeded major ends `limit-reached v1`, open points unchanged, no `plan-start v2`, plain-text question asked.
- M6 early consensus: a trivial brief with limit 6 reaches consensus before 6; `Outcome` reads "consensus at iteration N of 6, open points 0/0/0".
- M7 legacy: `history` on both old folders reports `format: legacy` and only pre-existing failure messages.
- M8 retry: a guard-failed planner retry commits `plan-start vN` again with attempt 2 and passes `pre`/`history`.
- M9 Minors close: a seeded minor at N < L yields `plan vN+1` then `review vN+1`; a pushed-back minor is assessed there; consensus only at 0/0/0, otherwise gate continues.

## Risks
- `SKILL.md` byte cap: Step 1 lands first; contract test prints actual bytes on failure.
- Agents can still read a prior `Version:` header in task files; that reveals progress, never the limit or a final round, which is what drives budget-aware behaviour (In-3 targets prompts).
- A wrong derived Version is a format failure (one retry, then `stopped`), never silent corruption.
- Deferral keywords can false-positive; restricted to Response reasons and `## Plan`, fixed by the self-planner or one format retry.
- Verbatim prompt delivery and semantic severity honesty cannot be proven statically; covered by M1–M3 and stated in §Honest claims.
- Late majors are allowed and Minors close costs a review round, so some runs end `limit-reached` instead of false consensus — intended (In-4, In-5).
- Legacy mode could hide new runs; `pre` rejects a new `brief v1` without `Outcome:`.

## Validation
- Unit suite green; byte budgets reported under caps; M1–M9 recorded in the PR description; `history` on both old folders matches the C-2 answer.
