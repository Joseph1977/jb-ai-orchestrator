# AGENTS.md

Instructions for anyone working in this repository, human or agent.

## Where you are working

| You are editing… | Also read… |
|---|---|
| `src/app/services/harness/**` | `docs/ORCHESTRATOR.md` §3 — adapter selection, eager caps, progressive disclosure |
| `src/app/services/context_compaction.py` | `docs/ORCHESTRATOR.md` §8 — offload, summarization, compaction, the `preCompact` hook |
| Lifecycle, resume, close, persistence | `docs/ORCHESTRATOR.md` §10, §12, §13 |
| `src/app/controllers/**`, public API | `readme.md` — the caller-facing contract |
| `alembic/versions/**` | `docs/ORCHESTRATOR.md` §13 before adding a revision |
| `ag-ui-demo/**` | `ag-ui-demo/README.md`. Its `PLAN.md` is historical — do not treat it as current |

`docs/ORCHESTRATOR.md` is the source of truth for harness and lifecycle
behaviour; `readme.md` is the source of truth for the caller-facing API.

## Contributing

- Branch from `master`, and open a pull request against it. Do not commit
  directly to `master`.
- Pull the latest `master` into your branch and resolve conflicts locally
  before opening or updating a pull request.
- Do not merge your own pull request.
- Never commit real credentials. Application settings live in
  `src/.env/{ENV}/.env` and secrets there are placeholders of the form
  `__VARIABLE_NAME__`, substituted at deploy time. `.env` files holding real
  values are git-ignored and must stay that way.

## Keep the docs current

Update `readme.md` when the caller-facing API, setup/run steps, or
configuration changes, and `docs/ORCHESTRATOR.md` when harness or lifecycle
behaviour changes. Routine refactors, tests, and comment-only edits need no
documentation change.

Record completed work in `changes/` — one markdown file per change, named
`YYYY-MM-DD-<short-slug>.md`, so entries never conflict on merge. Write the
entry after the work is done: it records what shipped, not what is planned.
Each entry captures the date, the branch or pull request, what changed, why,
and any migration or breaking note. Alembic revisions always get a migration
note.

## Attribution

Do not add tool, model, vendor, or assistant attribution to any output. This
covers source comments, docs, generated files, commit messages and trailers,
branch names, and pull request titles, descriptions, and comments. If tooling
inserts such text automatically, remove it before pushing.
