## Repository knowledge and task discovery

Use shared discovery notes to avoid repeating repository exploration.

### Repository map
- Use `docs/repo-map.md` as a navigation aid.
- If it is missing, create a concise initial map when a task requires
  broad repository exploration. Do not scan the entire repository solely
  to produce exhaustive documentation.
- Include major components, responsibilities, important entry points,
  relevant paths, and verified build/test commands.
- Record the commit inspected. Mark unverified information explicitly.
- Read only sections relevant to the current task.
- Update affected sections when implementation changes make them inaccurate.
- Current source code is authoritative; verify relevant code before editing.

### Task discovery notes
- For substantial, cross-module, or multi-agent tasks, create a concise
  note under `docs/task-notes/` before substantive implementation.
- Skip this note for small, clearly scoped changes.
- Record the objective, acceptance criteria, relevant files and symbols,
  dependencies, established findings, and unresolved questions.
- Refer to the repository map rather than duplicating it.
- Keep notes concise. Do not copy source files, raw logs, or conversation history.
- Update notes only for material discoveries or decisions.

### Agent coordination
- When delegation is authorized, use one Explorer for shared discovery
  where that avoids duplicated searches.
- The Explorer returns findings; a write-capable agent saves the notes.
- Give each worker relevant findings, paths, and acceptance criteria.
- Workers may read source code needed for implementation or independent review.
- Do not repeat established discovery unless it is incomplete, stale,
  or contradicted by current code.
- Do not delegate solely to create or maintain these notes.