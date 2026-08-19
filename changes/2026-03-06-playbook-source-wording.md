# Readme: the playbook input is a source, not a folder

- **Date:** 2026-03-06
- **Branch:** `docs/playbook-source-wording`
- **Target:** `on-going-dev`

## What changed

The opening line and the first differentiator bullet both called the playbook
input a "folder", which undersells and misdescribes it: `initiate` accepts a git
URL, an archive URL, a `file://` or UNC folder, or a local path. Both now say
"source", and the bullet names the `folder` request field explicitly so the API
contract is still findable.

The same bullet also listed discovered files by name (`AGENTS.md`, `CLAUDE.md`,
rules, skills, commands, subagent definitions), which both duplicated the layout
list in the opening paragraph and ignored the vocabulary the rest of the docs
use. `docs/ORCHESTRATOR.md` §1 defines a **primitive** and `initiate` returns a
`PrimitiveSummary` catalog, so the bullet now says the service detects the layout,
discovers the primitives inside, and returns that catalog — with the term linked
to its definition.

Harness coverage was also left implicit. Only `cursor` and `claude-code` are
registered in `_ADAPTERS` alongside `generic`; nothing reads a Copilot layout. The
feature bullet now notes that an adapter is two methods over shared primitive
discovery — roughly a hundred lines, as `cursor.py` and `claude.py` show — and
`Limitations and open work` gains a "Harness coverage" entry stating what the
generic fallback does and does not pick up, so the gap is stated rather than
implied.

"Archive / shared-folder URL" was also doing too much work as a summary. Nobody
reaching for cloud storage recognises their bucket or container in that phrase,
and the real constraint was buried: remote input is fetched by
`_download_and_extract`, which rejects anything not ending in `.zip`, `.tar.gz`,
`.tgz` or `.tar`. There is no object-store input provider at all — no bucket or
container is ever listed. The summary row now spells out the accepted forms, and
the source-kind table gains a note saying a blob or S3 URL works only when it
addresses the archive object itself, that `credentials.inputAccessToken` becomes
an `Authorization` header, and that object storage is an output-side feature
(`shared_folder` and `azure_blob`).

The header also gained an authorship line under the stack line, linking to the
author's LinkedIn profile, so the front door names who built it.

## Why

The readme is the front door, and its first two paragraphs implied the service
only takes a local directory, while its input summary implied object storage was
a supported input source in a way the code does not honour.

## Migration or breaking notes

None. Wording only; the `folder` field is unchanged.
