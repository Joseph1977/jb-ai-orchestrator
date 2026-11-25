# Plan — Plan-review skill: honest rounds and a real iteration limit
Version: 1
Model: claude-opus-5-5-medium

## Plan

### What / where / why
- What: make every round complete (In-1, In-2), hide the budget from agents (In-3), gate the limit between rounds (In-4), record honest outcomes (In-5), build prompts mechanically (In-6), fix retry/transition commits (In-7), align tests/docs (In-8).
- Where: `.cursor/skills/plan-review/` only (Out-1); repo guard, digest and recovery markers unchanged (Out-2); model-family rules unchanged (Out-3).
- Why: the late-point `Caused by` rule, "Iteration N of L" plus final-iteration wording, earlier-points-only re-review and free-text prompt additions let runs converge because the budget ran out, not because the plan was sound.
- Size constraint found: `SKILL.md` is 29,901 of the 30,000 bytes enforced by `test_context_byte_budgets`, so the prompt templates move to a new reference first (step 1) to create room.

### Limit-gate flow (answers brief question 2)
1. `review vN` committed: `State: reviewed`, `Iteration: N of L`, open points/trend/next ID recorded.
2. Decide by verdict: `NO_FURTHER_COMMENTS` → Consensus (any N ≤ L, including N = 1).
3. Limit check (only here, never in a prompt or inside a round): open points remain (`CHANGES_REQUESTED` or `APPROVE_WITH_MINORS`) and N = L → `limit-reached vN`.
4. `APPROVE_WITH_MINORS` and N < L → Minors close: `plan-start`/`minors` vN+1 with `Iteration: N+1 of L` (no limit raise).
5. `CHANGES_REQUESTED` and N < L → `plan-start vN+1` with `Iteration: N+1 of L`.
6. `limit-reached`: State `limit-reached`, Iteration and Open points copied unchanged from `review vN`, no consensus; only an explicit user K continues via `feedback vN` setting `Iteration: N of N+K`.

### Step 1 — Move prompt templates out of `SKILL.md` (In-6, enables In-1..In-3)
- New `references/prompts.md` holding `<!-- BEGIN PLANNER_PROMPT -->`, `REVIEWER_PROMPT`, and `MINORS_LINE` blocks; no other content beyond one heading per block.
- `SKILL.md` §Reviewer prompt and §Planner prompt are replaced by one `### Prompts` section: render only with `prompt_build.py`, pass stdout verbatim as the Task prompt, never edit, prefix, append or paste.
- `SKILL.md` §Rules first bullet: "Prompts are exactly `prompt_build.py` output; the orchestrator adds no text, including in a last or retried round."

### Step 2 — Planner template text (In-1, In-3)
- Read line becomes: "Read each task file once: brief.md, plan.md and review.md if present; then referenced repo paths." (`status.md` removed — it carries Iteration/limit.)
- Add scalar lines: `Header lines: Version: <VERSION> / Model: <MODEL>`, `Plan length cap: <PLAN_CAP> lines`, `Retired point IDs: <RETIRED_IDS>`.
- Replace "Resolve every point now." with: "Each version is a complete, self-contained plan of the whole brief scope, not a delta. Answer every open point in full in this version; never defer, split across versions, or mark work for later."
- Keep Evidence, retired-ID, decision vocabulary and the one-line return text unchanged; return line uses `<VERSION>`.

### Step 3 — Reviewer template text (In-2, In-3)
- Delete "Iteration N of L." from line 1, "Raise every issue in v1.", the whole `Caused by` sentence, and "Report final-iteration blockers/majors."
- Read line becomes: "Read each task file once: brief.md, plan.md, prior review.md if present." (`status.md` removed).
- Add scalar lines: header lines, `Review length cap: <REVIEW_CAP> lines`, `Next unused point ID: <NEXT_POINT_ID>`, `Retired point IDs: <RETIRED_IDS>`, `Pushed-back IDs to assess: <PUSHED_BACK_IDS>`.
- Insert: "Every review covers the whole current plan against brief.md and the repo, not only earlier points. Report every issue you find at its true severity, whether new, previously missed, or caused by a change; severity follows impact alone."
- Keep stable-ID, monotonic allocation, never-reuse/retired, pushback-settlement, minor definition, and verdict rules verbatim.

### Step 4 — Prompt builder `scripts/prompt_build.py` (In-6)
- CLI: `prompt_build.py planner|reviewer|minors --task <abs> --version N --model <slug> --plan-cap n --review-cap n [--next-id P###] [--retired IDs|none] [--pushed-back IDs|none]`.
- Allowed scalars exactly: TASK_FOLDER (absolute, canonical direct child of `workspace/outcomes`, name regex from `SKILL.md` §Task folder), VERSION (int ≥ 1), MODEL (`^[a-z0-9][a-z0-9.-]*$`), PLAN_CAP/REVIEW_CAP (1–1000), NEXT_POINT_ID (`^P[0-9]{3,}$`), RETIRED_IDS/PUSHED_BACK_IDS (`none` or comma list of IDs), PREV_VERSION (= VERSION−1, minors only), plus `POLICY_PROBE_PY` code from the `SKILL.md` marker.
- Refuses: unknown options (argparse), any value with whitespace/newline/backtick outside its regex, a template placeholder not in the allowed set, any `<...>` left after rendering; exit 2 with the reason.
- `minors` = planner prompt + exactly the `MINORS_LINE` (rendered "Review v<PREV_VERSION> is APPROVE_WITH_MINORS: apply each minor or push back with Evidence; change nothing else.").
- Prints the prompt to stdout and its sha256 to stderr; reads only `SKILL.md` and `references/prompts.md` beside itself; imports no local skill module; runs as `python3 -I -B` after the digest check like every skill script.

### Step 5 — `references/operations.md` (In-1, In-2, In-5, In-7)
- §status.md: add `Outcome: none | consensus at iteration N of L, open points 0/0/0 | consensus after minors close at iteration N of L, open points 0/0/<pushed-back minors> | limit-reached at iteration L of L, open points b/m/n | stopped at iteration N of L (<reason>)` after `State`.
- New §Status before each commit table (fields set before `git add`): brief v1 planning/1 of L/Outcome none; plan-start vN planner-running/N of L/`Next action: planner writes plan vN — attempt k`; plan vN reviewing/N of L; review-start vN reviewer-running/N of L/attempt k; review vN reviewed/N of L + counts; minors vN+1 awaiting-user/N+1 of L/consensus Outcome; consensus vN awaiting-user/N of L/consensus Outcome; limit-reached vN limit-reached/L of L/limit Outcome; feedback vN planning/N of L′/Outcome none; stopped vN stopped/Stop reason/stopped Outcome; approved vN approved/Outcome unchanged.
- New retry rule: a retried launch commits the exact subject `plan-review(<slug>): plan-start vN` (or `review-start vN`) again with no suffix, after incrementing `attempt k` in `Next action`, so the commit has a real diff.
- §Commit transaction: `pre` gains required `--subject "plan-review(<slug>): <step> v<N>"`; `git commit` uses that same string.
- Limit may change only in `brief v1` or `feedback`; `brief.md` never records the iteration count or limit (agents read it).
- §Format checks: delete the `Caused by` bullet; add "A Response reason or `## Plan` containing `defer`, `next round`, `next version`, `follow-up`, `TBD` or `TODO`, or a `## Plan` that points to an earlier version instead of restating it, fails format."

### Step 6 — `commit_check.py` (In-4, In-5, In-7, C-2)
- Refactor the per-commit body of `history()` into `_validate_step(context, step, version, status_text, files)`; `history()` loops over commits, `pre` replays history then validates the staged tree for `--subject` before any commit.
- `pre`: reject a subject not matching `SUBJECT`, a slug mismatch, or staged `status.md` with the wrong State/Iteration/Outcome for the step; `MANIFEST_VERSION` 3 stores the subject; `post` fails if the commit subject differs.
- `SUBJECT` mismatch message keeps "is not a plan-review step" and adds "; retries reuse the exact subject plan-review(<slug>): <step> v<N>" when a valid subject has a suffix.
- Current format (brief v1 status has `Outcome:`; `pre` rejects a new `brief v1` without it) adds: `consensus` requires latest review `NO_FURTHER_COMMENTS`; `limit-reached` requires latest review verdict CR/AWM, review Iteration N = its limit, Iteration and Open points equal to the review commit, Open points ≠ 0/0/0.
- Current format also adds: `minors` and `plan-start`/`plan` after `reviewed` require review N < its limit; any limit change outside `feedback` fails; `consensus`/`minors`/`limit-reached`/`stopped` require the matching `Outcome` with the commit's Iteration and Open points.
- Legacy format (no `Outcome:` in the brief v1 status): existing rules only, no new failure; result adds `"format": "legacy"` and note `legacy status format: honest-limit checks not applied`.

### Step 7 — `SKILL.md` loop text (In-4, In-5, C-1)
- §Loop step 4 Decide replaced by the six-line gate flow above; Minors close sets `Iteration: N+1 of L` and runs only when N < L (delete `max(limit,N+1)`).
- §Consensus step 1 records the consensus `Outcome`; add "After `approved`, the session ends; implementing changes to this skill runs outside the loop."
- §Loop steps 2–3: "exact retry" points to the operations retry rule; §Commits references `pre --subject`.

### Step 8 — Other references (In-4, In-7, In-8)
- `limit.md`: trigger becomes "review vN with open points and N = limit"; delete "APPROVE_WITH_MINORS is not Limit reached … including when N = limit"; commit sets State/Outcome, keeps Iteration and Open points; K sets `Iteration: N of N+K` in `feedback vN`; brief records retired decisions only.
- `resume.md` step 3: describe `pre --subject`, current vs legacy format and the new checks; step 7: replace the `max(limit, N+1)` sentence with "Minors close runs only when N < limit."
- `recovery.md` §Format failure: remove the `Caused by` example, add deferral/partial-plan example; retry launches follow the operations retry rule.
- `validation.md` §Loop and format: replace the final-iteration and uncaused-late bullets with M1–M8 below; §Honest claims: verbatim-prompt delivery is manual-only.
- `integrity.md`: no change (searched; it only keeps guard data out of prompts).

### Step 9 — Changes record (AGENTS.md §Keep the docs current)
- `changes/<date>-plan-review-honest-limit.md`: branch/PR, what/why, breaking note (`pre --subject` required, manifest v3; no in-flight transaction may span the upgrade), C-2 behaviour; no Alembic, so no migration note; `readme.md`/`docs/ORCHESTRATOR.md` untouched (not caller API or harness).

### Execution and C-1/C-3
- Implement only after this run is `approved` and its session has ended; no plan-review run may be in a running state in the implementing chat, so the digest change trips no checkpoint.
- Branch `fix/plan-review-honest-limit` from `origin/on-going-dev`; if its `.cursor/skills/plan-review/` differs from this branch, stop and ask; PR targets `on-going-dev` only; no attribution anywhere.
- Order: step 1 → 4 → 2/3 → 6 → 5/7/8 → tests → 9; then run the unit suite from `SKILL.md` §Validation, then a fresh `/plan-review` for M1–M8.

### C-2 answer
- Both old folders stay readable: no file format they use is removed, and their status files lack `Outcome:`, so history runs in legacy mode with no new failures.
- `fe98` ended consensus at 4 of 4 with review v4 `NO_FURTHER_COMMENTS`; `ca88` is at `reviewer-running` v4 with no terminal commit — neither hits a new rule even in current mode.
- Pre-existing corruption stays flagged with existing messages (history is immutable): "`<sha>: subject 'plan-review(<slug>): plan-start v1 retry' is not a plan-review step; retries reuse the exact subject …`", "`status.md Iteration does not match v2`", "`plan must set State reviewing, got planner-running`".

## Files
- Changed: `.cursor/skills/plan-review/SKILL.md`, `references/{operations,limit,resume,recovery,validation}.md`, `scripts/commit_check.py`, `scripts/test_commit_check.py`, `scripts/test_skill_contract.py`.
- New: `references/prompts.md`, `scripts/prompt_build.py`, `scripts/test_prompt_build.py`, `changes/<date>-plan-review-honest-limit.md`.
- Searched, unchanged: `references/integrity.md`, `.cursor/commands/kit-help.md` (invocation unchanged), `repo_guard.py`, `preflight.py`.

## Tests (must exist and pass)
- In-1: `test_planner_template_requires_complete_replan` (contains the complete-plan and never-defer text); `test_operations_has_deferral_format_rule`.
- In-2: `test_reviewer_template_reviews_whole_plan_at_true_severity`; `test_caused_by_rule_removed` (absent from SKILL, prompts, operations, recovery, validation); stable-ID/pushback strings still asserted; drop the "cited point ID must differ" contract.
- In-3: `test_rendered_prompts_are_iteration_blind` (builder output, max-length scalars, no `(?i)\biteration\b|final|last round|remaining|budget`); `test_prompts_do_not_read_status_md`.
- In-4: `test_limit_reached_requires_review_at_limit`; `test_limit_reached_after_minors_verdict_at_limit_passes`; `test_minors_at_limit_rejected_current_format`; `test_plan_start_after_review_at_limit_rejected_without_feedback`; `test_feedback_with_k_continues_past_limit_passes`.
- In-4 continued: `test_consensus_requires_no_further_comments`; `test_limit_change_outside_feedback_rejected`; `test_limit_reached_preserves_open_points`; `test_skill_decide_contains_gate_sequence`.
- In-5: `test_outcome_required_on_terminal_commits`; `test_outcome_matches_iteration_and_open_points`; `test_early_consensus_at_iteration_one_of_six_passes`; `test_limit_reached_outcome_four_of_four`.
- In-6: `test_prompt_build.py` — unknown option, newline/backtick injection, bad path/ID/cap, unknown template placeholder, leftover placeholder all exit 2; minors adds exactly one line; deterministic sha; no local imports; allowed-scalar set equals template placeholders.
- In-6 continued: `test_context_byte_budgets` renders via builder (keeps 30,000/7,500 caps); `test_skill_has_no_inline_prompt_templates`; `test_rules_require_builder_output`.
- In-7: `test_retry_subject_suffix_rejected_with_hint`; `test_plan_start_retry_after_planner_running_passes`; `test_pre_rejects_wrong_state_before_commit`; `test_pre_rejects_iteration_mismatch`; `test_pre_rejects_new_brief_without_outcome`; `test_post_rejects_subject_mismatch`; `test_manifest_v2_rejected`.
- In-8/C-2: `test_legacy_status_format_uses_existing_rules` (existing fixtures without `Outcome:` keep passing, incl. `test_minors_at_limit_passes` and `test_limit_reached_rejected_after_approve_with_minors` as legacy cases); test helper `step()` passes `--subject`.
- Command: `PYTHONDONTWRITEBYTECODE=1 python3 -I -B -m unittest discover -s "$SKILL_ROOT/scripts"`.

## Manual checks (`references/validation.md`)
- M1 seeded missed major: plan v2 keeps a major defect v1 review missed; review v2 reports it as major with no prefix.
- M2 whole-plan review: review v2 raises a seeded issue in a section untouched by v1 points.
- M3 prompt integrity: each Task prompt in the Cursor tool call equals `prompt_build.py` output (sha256 match); no added text in any round.
- M4 blindness: rendered prompts and brief/plan/review files contain no iteration count or limit; agents never open `status.md`.
- M5 limit: `--max-iterations 1` with a seeded major ends `limit-reached v1`, open points unchanged, no `plan-start v2`, plain-text question asked.
- M6 early consensus: a trivial brief with limit 6 reaches consensus before 6; `Outcome` reads "consensus at iteration N of 6, open points 0/0/0".
- M7 legacy: `history` on both old folders reports `format: legacy` and only pre-existing failure messages.
- M8 retry: a guard-failed planner retry commits `plan-start vN` again with attempt 2 and passes `pre`/`history`.

## Risks
- `SKILL.md` byte cap: step 1 must land first; contract test prints actual bytes on failure.
- `Version: N` still exposes the version number (file format needs it); the limit, budget and final-round wording are hidden, which is what drives budget-aware behaviour.
- Deferral keywords can false-positive; restricted to Response reasons and `## Plan`, fixed by the self-planner or one format retry.
- Verbatim prompt delivery and semantic severity honesty cannot be proven statically; covered by M1–M3 and stated in §Honest claims.
- Late majors are now allowed, so some runs will end `limit-reached` instead of false consensus — intended.
- Legacy mode could hide new runs; `pre` rejects a new `brief v1` without `Outcome:`.

## Validation
- Unit suite green; byte budgets reported under caps; M1–M8 recorded in the PR description; `history` on both old folders matches the C-2 answer.
