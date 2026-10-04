# Groups

Objective: replace the supervisor-link experience with named groups that any
account can create. The creator administers membership and can read member
records; a group setting optionally grants the same read access to all members.

Acceptance criteria:
- Create and join multiple groups through hashed, reusable invitation links.
- Explicit invitation preview/acceptance explains current record visibility.
- Private groups restrict member records to the admin; shared groups allow
  current members to read one another's records. Record writes stay owner-only.
- Leave/remove, invitation rotation/revocation, and visibility changes take effect
  on every read; retry receipts do not restore removed membership.
- Convert existing supervisor relationships into private groups, preserving
  members and invitation links (user confirmed).
- Support SQLite and PostgreSQL migrations/import, Arabic frontend flows,
  meaningful access-control tests, and updated `docs/repo-map.md`.

Reference: repo map feature/storage sections. Starting commit: `150525c`.
Branch: `codex/groups`. Relevant modules: `backend/main.py`,
`backend/database.py`, `backend/import_sqlite.py`, `backend/migrations/`,
`frontend/app.js`, `frontend/styles.css`, backend and Node integration tests.

Established findings: existing links grant individual supervisor access;
`can_supervise` currently gates management/read routes. PostgreSQL migrations
are versioned SQL files; SQLite schema upgrades run in `Database.__init__`.
Invitation receipts use a per-user request key. Public access must be derived
from current group membership and visibility, without residual legacy grants.

Implemented decisions:
- `groups`, `group_members`, `group_invites`, and
  `group_invite_acceptances` are authoritative. The admin is a member, and cannot
  leave or remove themselves. There is no ownership transfer.
- `app_migrations(groups_v1)` guards transactional SQLite conversion;
  PostgreSQL migration `002_groups.sql` converts existing rows once. Legacy
  tables remain inert; legacy supervisor HTTP routes return 410. Existing
  `#invite` links use the new group preview/acceptance flow.
- Group mutation locks serialize settings, membership and invitation changes.
  Acceptance additionally locks the PostgreSQL user row to serialize request keys
  across groups. Replay after leaving/removal returns 409 without restoring access.
- Member-read authorization checks both memberships and visibility in one SQL
  snapshot, then repeats after schedule/statistics work. Daily reports recheck
  current grants, include admin records, and return `total_members`/row `member`.
- SQLite imports convert old snapshots in memory. Complete migrated group
  snapshots remain authoritative even when stale legacy relationships disagree;
  any partial group schema is rejected. No application database was cut over.
- Frontend group/member/session request epochs reject stale responses. Group
  changes and permission loss clear cached diaries, achievements and reports.
  Invitations explain current visibility and the admin's ability to change it.

Validation (October 4, final implementation):
- Backend: 102 tests, 101 passed and one expected SQLite-only fixture skip in the
  PostgreSQL subclass. Ran with the configured local PostgreSQL server using
  disposable random schemas and a temporary bootstrap SQLite file; application
  data was not migrated or modified. Includes populated PostgreSQL upgrade,
  sequence reset, restart without membership resurrection, old/new importer,
  join/revoke locking, receipt replay and visibility revocation during schedule work.
- Node: 13 tests passed, covering real API/frontend handlers through a minimal
  DOM adapter. Final focused group/report rerun passed 3 tests after adding a
  shared-member cached-report restriction check; existing diary/domain tests had
  passed 10/10. Tests cover invitation/auth races, private/shared controls,
  read-only diaries, stale member/group/session reads and daily report semantics.
- `npm run check` and `git diff --check` passed. No browser/visual verification
  or production deployment was performed.
- Tests used `.venv/bin/python -m unittest discover -s backend/tests -v` with
  `PRAYER_TEST_DATABASE_URL` pointing to local PostgreSQL and ordinary project
  database environment selection disabled. Node used `PYTHON="$PWD/.venv/bin/python"`
  and `node --test tests/*.test.mjs`; a bundled Node executable was used when the
  default environment lacked the Python executable needed by the test server.

Remaining limits: browser rendering is unverified; shared-setting changes cannot
recall records already delivered to a member. No ownership transfer, notification system or production rollout is included.

Current presentation follows the supplied RTL reference: a white selected-group
panel with navy name/users icon, blue inline edit, green admin-only sharing switch,
split daily-report/invite-copy control, avatars, admin badges and record buttons.
The heading places New Group beside Refresh at the top left. Creation uses an
on-demand modal; removal/leave uses compact icons. The three lower panels are gone.

Durable invitation decisions:
- Creating a group inserts its canonical invitation in the same transaction.
- Admin-only `POST /groups/{id}/invite-link` retrieves or ensures that invitation
  under the group lock. Copy works after refresh, login and group switching.
- Nullable `group_invites.share_token` stores the recoverable capability; lookup
  still uses its hash. Member, preview and record projections exclude the token.
- SQLite `groups_v2` and PostgreSQL migration 003 allow one active canonical
  invitation alongside one legacy hash-only invitation. Existing links remain
  valid; explicit rotation revokes all active links. SQLite replay preserves this
  schema on subsequent startup.
- Import accepts complete older snapshots without the column, preserves newer
  tokens, validates their hashes and rejects inconsistent migration markers.

Final validation: 102 backend tests (101 passed, one expected skip), 13 Node tests
passed, syntax checks and git diff check passed. Added checks cover atomic creation,
concurrent link retrieval, privacy, old-schema upgrade/restart, import compatibility
and exact copied URL after refresh/group switching/re-login. Bounded final security
and architecture review found no actionable findings. Browser rendering remains
unverified. The local public preview uses the existing PostgreSQL database; no
production deployment was performed.

Group deletion: the admin header delete icon opens a confirmation explaining that
personal prayer records remain intact. `DELETE /groups/{id}` checks admin access
under the group lock, deletes acceptance receipts before invitations and then
memberships/group in one transaction. Users and days are untouched. Deleted
links cannot join and group-based record access ends; migration markers prevent
legacy resurrection. The frontend clears selected member/report/link state and
rejects old request responses. Invite previews return 404 when deletion removes
the group between lookup and projection.

Deletion validation: 108 backend tests (107 passed, one expected skip), all 13 Node tests, syntax and diff checks passed. Tests cover admin denial, transactional rollback, invitation receipt cleanup/invalidation, retained personal records, no legacy resurrection, preview deletion race, confirmation/cancel/retry, successful state reset and stale list responses. Final bounded review finding was corrected. Public local preview refreshed; no deployment or browser verification.
