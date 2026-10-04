# Repository map refresh — October 4, 2026

Objective: replace the mixed-language historical map with an English reference
to current feature implementations and require ongoing maintenance in AGENTS.md.

Inspected commit: `729097aab4c3d31e64435967fdc32a6c512cbf3d`; initial working tree clean.
Navigation: `docs/repo-map.md`. Relevant sources: backend/main.py,
backend/database.py, backend/import_sqlite.py, frontend/app.js,
frontend/domain.js, package.json, and existing feature task notes.

Acceptance: feature-to-source/test mappings, current PostgreSQL/SQLite data flow,
configuration and executable commands, explicit verification limits, English
throughout the map, and mandatory updates after new features.

Discovery is read-only and shared by one Explorer; documentation edits are owned
by the coordinating agent. No application behavior or database data is changed.

Completed: English feature index, architecture/data flow, storage/configuration,
run/test commands, historical validation limits, and focused reference links.
AGENTS.md now requires map-first discovery and updates after every new feature.
Source symbols and package scripts were checked; `git diff --check` passed.
Application tests were not rerun for this documentation-only change.
