# Kit help — list my rules, skills, agents, and commands

Read-only. Do not edit, create, or commit anything.

Inspect both the active capabilities supplied by Cursor in this session and these filesystem
locations **now**. Do not infer installed items from general product knowledge.

| Group | Sources |
|---|---|
| Rules — project | active workspace rules plus `**/.cursor/rules/**/*.mdc` |
| Rules — user | active Cursor Settings user rules plus `~/.cursor/rules/**/*.mdc` |
| Agent guides | `AGENTS.md` and `**/AGENTS.md` throughout the repo |
| Skills — project | `**/.cursor/skills/*/SKILL.md` |
| Skills — user | `~/.cursor/skills/*/SKILL.md` |
| Skills — Cursor built-in | active built-ins plus `~/.cursor/skills-cursor/*/SKILL.md` |
| Skills — plugins | active plugin skills plus cached `~/.cursor/plugins/**/skills/*/SKILL.md` |
| Commands — project / user | `**/.cursor/commands/**/*.md`, `~/.cursor/commands/**/*.md` |
| Subagents — built-in | available subagent types supplied by Cursor for this session |
| Subagents — custom | `.cursor/agents/*.md`, `~/.cursor/agents/*.md` |
| Hooks | `.cursor/hooks.json`, `~/.cursor/hooks.json` |

Expand `~` to the current user's home directory. De-duplicate an item exposed through both the
active session and a file, but retain its source/precedence. Do not call a source empty merely
because it has no corresponding local file.

For repo-recursive searches, respect ignore files and exclude `node_modules`, generated/build
trees, every `prev-versions/` directory, and `.docs/POC/`. Never read the two forbidden document
areas. For plugin caches:

- identify an item by plugin ID plus skill frontmatter `name`, not by cache path;
- prefer the active-session definition;
- otherwise use plugin manifest/version metadata to select the newest semantic version;
- if no active or newest version can be established, show one qualified item with all candidate
  cache paths marked ambiguous, not several same-name items.

Cursor may not expose inactive/manual rules created in Cursor Settings. If they are not present in
the active session and have no readable file, do not invent them; add
`Unavailable: inactive Cursor Settings rules not exposed to this session`.

Output format:

- One heading per group. Skip a group that is empty, and list the skipped groups in one line at the
  end (`Empty: …`).
- One bullet per item: `name` — what it does / when it applies (max ~15 words), taken from the
  file's frontmatter `description` or first heading/paragraph.
- For rules, add `(always)`, `(globs: …)`, or `(manual)` from frontmatter. `manual` means it has no
  description, file patterns, or always-apply flag and must be attached explicitly.
- For skills and commands, show how to invoke them (e.g. `/plan-review <goal> [--max-iterations N]`).
- Keep built-in Cursor skills to one line each; no detail beyond the description.
- For hooks, use one bullet per configured hook event, not one bullet per `hooks.json` file.
- No other prose before or after the list.

If the user passes an argument (e.g. `/kit-help plan-review`), show only that item with a slightly
longer summary (≤ 10 bullets): qualified name and source, purpose, invocation, options, files it
writes, and safeguards.

Strip exactly one leading slash from the argument, if present. Match the result case-insensitively
against the exact item name first, then against its invocation with its leading slash removed. If
no item matches, output exactly:

`No installed kit item named "<argument>".`

If more than one item has that name, show every exact match, qualified by group and source (for
example `skill:project/plan-review` and `skill:user/plan-review`). Do not silently choose by
precedence. Partial-name suggestions are out of scope.
