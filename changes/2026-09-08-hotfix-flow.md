# Document the production-fix flow

**Date:** 2026-09-08
**Branch / PR:** `docs/hotfix-flow` → `on-going-dev`

## What changed

`CONTRIBUTING.md` gains a *Fixing production* section and `AGENTS.md` states the
same rule in its contributing list: an urgent fix to what is released branches
from `origin/master` as `hotfix/<something>`, reaches `master` by pull request,
and is then applied a second time on a branch cut from the current
`origin/on-going-dev`. Both documents now also say plainly that neither branch
is ever pushed to directly.

`.gitignore` lists certificate and private-key extensions.

## Why

The normal flow was already documented, but the release route was not. Without
the second pull request, `on-going-dev` stays behind and the next
`on-going-dev` → `master` promotion reverts the fix — a silent regression that
is hard to attribute afterwards. Saying so next to the rule is what makes the
second step happen while the fix is still fresh.

The key patterns are prophylactic. No such file is tracked, and none was; a
public repository simply should not depend on nobody ever running a command
that drops a `.pem` in the tree.

## Migration or breaking notes

None. Documentation and ignore rules only.
