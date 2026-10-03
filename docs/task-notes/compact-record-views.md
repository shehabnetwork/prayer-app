# Compact achievement history and student day views — October 3, 2026

Objective: reuse the approved daily-report compact layout in achievement history and the selected student read-only day. See docs/repo-map.md and docs/task-notes/daily-review.md for baseline components.

Scope: frontend/app.js rendering helpers/achievements/daily, frontend/styles.css proportional table, frontend/index.html asset version, affected JavaScript integration tests. Retain achievement summaries/badges and period selection; history date buttons still open records. Student day keeps navigation, summaries, and return banner. Own editable diary stays functional and unchanged.

Acceptance: five prayer columns plus combined totals; chronological independent Sunnah/fard slots with gray/red crosses and medals aligned; both Dhuhr-before groups and Witr visible; no horizontal scrolling. Reuse common renderer; preserve schedule/unknown semantics, data guards and read-only permissions. No backend changes, browser or desktop tools.

Status: implemented. `compactPrayerTable` shares headers, fixed slots, totals and markers across daily roster, achievements history, and selected student day. The user's editable daily board remains separate.

Verification: four focused integration tests passed, including own/child history-date navigation, read-only compact student records, schedule waiting/unavailable states, and existing diary entry behavior. npm run check and git diff --check passed. Main source review confirmed explicit unknown eligibility is retained and full day schedule objects use existing eligibility helpers. No browser/visual verification; Node commands used ordinary execution after Token Guard could not execute the NVM binary. Asset version: 20261003-compact-history-1.
