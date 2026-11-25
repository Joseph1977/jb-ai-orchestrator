# Limit reached

On Limit reached, read this file before asking the user or writing the limit commit.

Trigger: review vN has open points (any verdict: `CHANGES_REQUESTED` or `APPROVE_WITH_MINORS`) and N = limit → Limit reached. Never start plan vN+1 or raise the limit without the user's explicit K. Do not commit `plan-start vN+1`. Leave the recorded limit unchanged until that K is explicit. There is no consensus at the limit with open points.

`NO_FURTHER_COMMENTS` goes to Consensus at any N ≤ limit. Feedback at consensus keeps its implicit one-round rule and is not this flow.

## Estimate

Estimate the iterations still needed for consensus from:

- all open blocker, major, and minor points and the total-open trend;
- point IDs the reviewer maintained for two consecutive iterations. Those will not converge by iterating. Flag them as needing a user decision.

Give the estimate as a number or a small range, with a one-line reason.

## Commit and question

1. Set `status.md` `State: limit-reached` and the limit `Outcome`, keep `Iteration` and `Open points` exactly as committed in `review vN`, and record the estimate. Commit `limit-reached vN`. Say whether the commit is local-only. Do not push without an explicit ask.
2. Tell the user the iterations used, the open points by severity, the trend, the bouncing points, and the estimate. Then ask in plain chat text (no interactive question UI):

   ```
   Question: Plan review for <title> hit the <limit>-iteration limit with <x> blocker/<y> major/<z> minor points open; continue?
   1. Approve <estimate> more iterations — matches the estimate to reach consensus.
   2. Approve a different number of iterations — reply with the number.
   3. Decide named bouncing point IDs yourself and approve <k> more iterations — permanently retires those IDs.
   4. Stop — the plan stays at Version <N>, not approved for implementation.
   Recommendation: <option> — <why>
   ```

## K, retire, and stop

On approval of K more iterations: set `Iteration: N of N+K` and `Outcome: none` in `status.md`, `State: planning`, and commit `feedback vN`. A K-only grant commits `status.md` alone; `brief.md` changes in that commit only to record dated user feedback or user-decided point IDs (each needs an explicit point ID, which is appended to `Retired point IDs`), never the limit. Then continue from loop step 2 with the version after the latest committed plan. Retired IDs cannot be maintained, reused, or removed from that list.

K must be an explicit number from the user. Do not reuse a recommendation as approval. Do not raise the limit from silence.

On stop: set `State: stopped`, `Stop reason: user`, the stopped `Outcome`, commit `stopped vN`, say whether it is local-only, and end. Resuming a user stop requires explicit approval to reopen and a new iteration limit (`references/resume.md`).
