# M13 rubric — audit recall on the `opencode-engine-replan` replay

Read only while validating the skill (M13 in `validation.md`). This rubric is frozen: change it only
with a dated note below explaining why, and never to fit a run's result.

## Input

- Task: `workspace/outcomes/task-opencode-engine-replan_20261125-104930Z-14ad`.
- Frozen plan v1: `plan.md` at commit `b2f18e92059064076dfd31753200164985fb33d4`
  (`plan-review(opencode-engine-replan): plan v1`), sha256
  `0e5582c41c87c658059da7659b5abf99f293957e5fef787f405ca2ffb75d6f11`.
- Historical points: `review.md` P001–P025 across that task's review commits v1–v16.

## Applicability

A historical point is in the rubric only if its defect already exists in the frozen plan v1. A
point whose defect first appears in a later plan version, or which depends on one reading of
unchanged text, is excluded: no v1 audit can be expected to raise it.

### In the rubric (16)

| ID | First raised | Defect present in plan v1 |
|---|---|---|
| P001 | v1 | D9 reports `subagents`/`context_compaction` unsupported, but D10 gates neither, and D12 silently drops `task_local`. |
| P002 | v2 | D12 lists and dispatches local tools through `LocalToolProvider.execute` without its `enabled` check, so `LOCAL_TOOLS_ENABLED=false` still exposes and runs them. |
| P003 | v2 | D12 returns `LocalToolProvider.execute` results straight to the sidecar: no media-marker translation and no multimodal capability or gate. |
| P004 | v2 | D12 excludes `ask_user_local` with no gate or caller-visible contract, so switching engines silently changes workflows. |
| P005 | v3 | D10 checks hooks once before the run (plus one re-check); the bridge bypasses the hub's hook wrapping, so a hook added mid-run is ignored. |
| P006 | v3 | D5 records engine identity only on `execution_runs`/`llm_states`; legacy runs have neither, so they get no identity (In-2). |
| P007 | v4 | D12 exposes resolved MCP tools but defines dispatch only for local calls: no MCP dispatch or request/user-token context binding. |
| P008 | v4 | D18 checks `OPENCODE_BASE_URL` only for userinfo/query, so any host is accepted despite D11's one loopback sidecar per process. |
| P009 | v4 | D21's fallback uses a secret "set at sidecar start", but no slice or file owns sidecar launch or that secret. |
| P012 | v6 | D16/Risks rely on a pinned version checked on health, but the test matrix has no version-mismatch test. |
| P013 | v7 | D11 shares one loopback sidecar per process while D12 exposes all local tools including `execute_local`, with no sidecar authentication or shell gate, so one run can reach another. |
| P015 | v7 | D15 keeps stale-claim reclaim unchanged; a dead process's sidecar work is never confirmed stopped before its claim is reclaimed. |
| P017 | v9 | D10 has no subagents/compaction/multimodal gates while D9 reports them false (same root cause as P001; one audit point may satisfy both). |
| P021 | v12 | D10's hook gate runs "when the path is known" before provisioning; hooks found after `ensure_input_workspace` fail in-segment after rows exist. |
| P023 | v14 | D4's `resolve_fresh_engine()` reads the environment at every fresh call site, including each orchestrator execute; no session-level engine is persisted (C-5). |
| P025 | v16 | D9 marks `workspace_free` false and D10 always rejects legacy `executeRequest` and workspace-free AG-UI, leaving In-3's headless set incomplete. |

### Excluded (9)

| ID | Reason |
|---|---|
| P010 | Introduced in plan v3: D12 starts keeping `ask_user_local` listed (v1/v2 exclude it). |
| P011 | Ambiguous: D20 is unchanged from v1 to v5; the defect exists only if D20 is read as narrowing `heartbeat_run` itself. |
| P014 | Introduced in plan v2: D10's `SUBAGENT_ENABLED`/`CONTEXT_COMPACTION_ENABLED` flag gates. |
| P016 | Introduced in plan v8: per-call hook re-read in D12 and `bind_hooks_suppressed()` (D26). |
| P018 | Introduced in plan v5: `execute_mcp_tool(original_name, args)` dispatch in D12. |
| P019 | Introduced in plan v9: the D27 byte-for-byte relay. |
| P020 | Introduced in plan v11: D27 parse-and-allowlist with unchanged forwarding. |
| P022 | Introduced in plan v9: the bounded idle poll and `opencode_idle_unconfirmed`. |
| P024 | Introduced in plan v15: `resolve_fresh_engine()` moved into the execution-record creators. |

## Scoring

- Score one `review.md` of the replay: the audit review v1, or the merged review when the run uses
  `--second-audit-model`. Never score the union of separate runs.
- A rubric item is matched when an audit point raises the same underlying defect: the same root
  cause in the same plan place. A broader point that clearly contains it counts; a nearby topic,
  or a point arguing the opposite direction, does not.
- Standing rulings from earlier scoring:
  - P001 and P017 are both matched by a point naming the subagents/compaction gates missing from
    D10 while D9 reports them false.
  - P003 needs handling of multimodal markers; only asking to strip a leaking `absolutePath`
    field does not count.
  - P005 needs the bridge to bypass hook wrapping; a point saying the bridge still fires hooks
    argues the opposite and does not count.
  - P006 needs legacy `Execution`-only runs to lack identity; plumbing identity through claim
    call sites does not count.
  - P009 needs the fallback secret to have no owner or setting; its strength, or tools/list
    caching, does not count.
  - P015 needs reclaim to wait for confirmed stop of the dead process's sidecar work;
    startup-sweep robustness does not count.
  - P021 needs hooks that first become visible after provisioning; ordering of the hook probe
    against containment checks does not count.
  - P025 needs the headless set to be incomplete; asking for more gating of workspace-free runs
    argues the opposite and does not count.
- Pass: at least 10 of 16 (60%).

## Recorded runs

| Date | Skill commit | Audit | Matched |
|---|---|---|---|
| 2026-11-26 | `79ce2bf` | single, gpt-5.6-sol-medium | 2/16 |
| 2026-11-26 | `79ce2bf` | single, grok-4.7-high | 3/16 |
| 2026-11-26 | `79ce2bf` | single, gemini-4-carbon-medium | 5/16 |
| 2026-11-26 | experiment | anchors split across three gemini-4-carbon-medium reviewers | 6/16 |
| 2026-11-26 | experiment | single gemini-4-carbon-medium, lens list | 7/16 |
| 2026-11-26 | `2a6ff91` | single, gpt-5.6-sol-medium | 5/16 |
| 2026-11-26 | `2a6ff91` | single, gemini-4-carbon-medium | 6/16 |
