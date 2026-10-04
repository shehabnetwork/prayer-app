## Repository knowledge and task discovery

Use shared discovery notes to avoid repeating repository exploration.

### Repository map
- Start repository discovery with the relevant sections of `docs/repo-map.md`
  before searching source files. Keep the map in English as the shared reference
  for understanding how the current application is implemented.
- If it is missing, create a concise initial map when a task requires
  broad repository exploration. Do not scan the entire repository solely
  to produce exhaustive documentation.
- Include major components, responsibilities, important entry points,
  relevant paths, and verified build/test commands.
- Map each implemented feature to its frontend/backend entry points, key
  symbols, storage or external dependencies, and relevant tests. Explain the
  important data flows and configuration choices concisely.
- Record the commit inspected. Mark unverified information explicitly.
- Read only sections relevant to the current task.
- After implementing any new feature, update the affected sections of
  `docs/repo-map.md` before completing the task. Also update it after fixes,
  refactors, or configuration changes that make its descriptions inaccurate.
  Describe the final code rather than appending a chronological change log.
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
