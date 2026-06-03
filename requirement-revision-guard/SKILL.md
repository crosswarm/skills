---
name: requirement-revision-guard
description: Install and operate a reusable requirement revision governance mechanism in software projects. Use when a user wants every AI Agent session to record new requirements, requirement changes, requirement document creation, or requirement document edits under docs/spec/revision; when asked to enforce requirement traceability; when modifying PRD/spec/design requirement documents and a per-change revision log is required; or when adding scripts/AGENTS.md rules that block requirement document changes without a corresponding revision record.
---

# Requirement Revision Guard

## Core Rule

Treat any new requirement, requirement change, requirement clarification, acceptance-criteria change, constraint change, or requirement document edit as a traceable event. Before finishing, ensure the project has a new `docs/spec/revision/YYYYMMDD-HHMMSS-title.md` record that explains:

- `变更内容`
- `变更原因`
- `已完成任务`
- `未完成后续计划`

If no follow-up work remains, write `暂无` with a reason. Never leave placeholder text such as `待补充`, `请补充`, `TODO`, or `TBD` in a formal revision record.

## Install Workflow

Use this workflow when the user asks to add the mechanism to a project, make it reusable, or enforce requirement change logs:

1. Inspect the target project for `AGENTS.md`, `package.json`, `docs/spec`, and existing requirement/design docs.
2. Resolve the bundled installer path relative to this skill folder, then run it from the target project root:

```bash
node "$CODEX_HOME/skills/requirement-revision-guard/scripts/install_revision_guard.js" --project .
```

3. If the project uses nonstandard requirement paths, pass them explicitly:

```bash
node "$CODEX_HOME/skills/requirement-revision-guard/scripts/install_revision_guard.js" --project . --requirement-dirs docs/product,docs/requirements,docs/design --exclude-dirs docs/design/assets
```

4. Read the generated/updated files and adapt wording only when the project has stronger local conventions.
5. Run `npm run revision:check` when `package.json` exists. Otherwise run `node scripts/revision-guard.js --check`.
6. Report the installed files and verification result.

The installer is idempotent. It updates the guarded AGENTS block, creates `docs/spec/revision/README.md`, creates `_TEMPLATE.md`, writes `scripts/revision-guard.js`, and adds npm scripts when a `package.json` exists.

## Ongoing Use Workflow

Use this workflow whenever this skill is active and the current task introduces or changes requirements:

1. Create a revision record before or during the change:

```bash
npm run revision:new -- --title "变更标题" --reason "变更原因"
```

2. Update the affected requirement/spec/design docs or implementation.
3. Fill the revision record with concrete content, reason, completed tasks, follow-up plan, and verification.
4. Run `npm run revision:check` before final response.
5. Mention the revision file path and check result in the final response.

For staged/commit workflows, also run:

```bash
npm run revision:check:staged
```

## Guard Behavior

The generated guard script checks the working tree by default and the index with `--staged`. It treats changed Markdown files under configured requirement directories as requiring a newly added revision record, excluding `docs/spec/revision` and configured asset directories.

The script fails if:

- Requirement docs changed but no new formal revision record was added.
- A formal revision record was deleted.
- A revision filename does not match `YYYYMMDD-HHMMSS-title.md`.
- Required sections are missing or empty.
- Placeholder text remains in a formal revision record.

## Adaptation Notes

- Keep project-specific rules in the target project's `AGENTS.md`; keep this skill generic.
- Prefer adding the generated check to CI or a Git hook when the user asks for stronger enforcement.
- For repositories without npm, keep `scripts/revision-guard.js` and invoke it directly with Node.
- When the user asks to distribute the skill itself, provide the local skill folder path and tell recipients to copy it into their own `$CODEX_HOME/skills/` or `~/.codex/skills/`.
