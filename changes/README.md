# Change log

One file per change, written **after** the work ships. This is a record of what
shipped, not a plan.

## Format

File name: `YYYY-MM-DD-<short-slug>.md`, dated by the day the work merged. One
file per change keeps entries from conflicting on merge.

Each entry carries:

- **Date** — the merge date.
- **Branch** — the branch the work shipped on.
- **What changed** — the shipped behaviour, not the diff.
- **Why** — the reason the work was done.
- **Migration / breaking** — or `None.` Any Alembic revision goes here.

## Naming phases

Never write a bare "Phase N" — always name the series. Two run here:

| Series | Covers |
|---|---|
| Harness Phase | coding tools, subagents, todos, streaming |
| Binding Phase | the io contract, output binding, refresh/close, output routing, Git input |

Unqualified "Phase N" inside `docs/ORCHESTRATOR.md` always means the **Binding**
series.
