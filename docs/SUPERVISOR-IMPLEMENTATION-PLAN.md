# Child Supervision: Implementation Plan and Technical Review

Original plan: October 2, 2026. English translation and technical clarification: October 3, 2026.

**Status:** Implemented in the local working tree; not deployed. The October 2 implementation record reports 34 passing Python tests, 12 passing JavaScript tests, and successful syntax checks. These results were not rerun for this documentation update. The inspected base commit is `fb1c96a5b85eb38ae5c11cbf384ecdb0cde0f515`; the feature includes uncommitted changes, so that commit alone does not reproduce it.

This document preserves the original requirements and design decisions and adds the implemented contract, concurrency semantics, and review questions. Source inspection, historical test results, and recommendations are distinguished below. See [repository map](repo-map.md) and [implementation notes](task-notes/supervisor-links.md).

## 1. Objective and scope

A supervisor registers, generates one reusable supervision link, and manually sends it to all children they want to follow—for example, a parent sharing it with siblings or a teacher sharing it with a class. Each child independently signs in or registers and explicitly confirms adding the supervisor. Acceptance does not consume the link.

The relationship is many-to-many: a child can have multiple supervisors, and a supervisor can follow multiple children. Access is read-only and covers prayer days, full history, and achievements, including records before acceptance. Supervisors cannot change a child's prayers, city, password, or account details.

Children can remove individual supervisors; supervisors can end individual child relationships. Revoking or rotating an invitation prevents new acceptances without removing established relationships.

The scope is this local FastAPI/SQLite project. The separate `prayer-cloud` version referenced in README, deployment, automatic messaging, identity/age verification, supervisor editing, and visibility limited to records after acceptance are excluded.

## 2. Baseline and design decisions

Before implementation, `backend/main.py` contained the SQLite schema, city migration, authentication, sessions, and prayer routes. The tables were `users`, `sessions`, and `days`, with reads and writes scoped to the authenticated account. `frontend/app.js` contained registration, navigation, and account state, with `sessionEpoch` and `dataEpoch` guarding late responses. Existing API and JavaScript integration tests supported logical verification without browser use.

The original plan marked the following as proposed choices rather than previously approved requirements. They now describe implemented behavior:

1. Consent covers past and future records and explains how to remove supervision.
2. Each supervisor has at most one active reusable link, with no automatic expiry. Rotation invalidates previous copies and preserves existing relationships.
3. Registration offers child/supervisor choices, but persistence uses an additive `can_supervise` capability. Supervisors retain their own diaries; existing accounts can enable supervision from their account page.
4. Possessing a valid link and explicitly accepting while authenticated is sufficient. Account types do not establish age, identity, or parental authority.
5. Users send links themselves; no messaging integration or new dependency was required.

At initial exploration, `AGENTS.md` was absent; it was read when available before implementation, and repository-map/task-note requirements were followed. Historical Token Guard execution failures and fallback execution are recorded in the implementation notes, without claiming the fallback output was measured.

## 3. File ownership and dependencies

| Role | Owned files and responsibility |
| --- | --- |
| Coordinator | This plan, API agreement, integration order, conflict resolution, final verification |
| Explorer, read-only | Relevant backend/frontend/test entry points and shared findings |
| Backend implementer | `backend/main.py`: migrations, capability, invitations, relationships, authorized child reads |
| Frontend implementer | `frontend/app.js`, `frontend/styles.css`: registration, consent, management, read-only child views |
| Tester | `backend/tests/test_supervisors.py`, `tests/supervisors_integration.test.mjs`; necessary compatibility changes to existing tests |
| Reviewer, read-only | Authorization, data exposure, migrations, concurrent acceptance, and delayed responses |
| Documentation worker | `README.md`, `backend/README.md`: user flow and final API behavior |

Reference files: `backend/schedule.py` supplies city/timezone behavior; `frontend/domain.js` supplies reward calculations; `frontend/index.html` is the loading entry point; `package.json` and `backend/requirements.txt` define execution requirements. Reward rules and dependencies were not intended to change. `tests/ui_smoke.py` remains optional and requires an explicit browser request. `docs/PUBLIC-PREVIEW.md` becomes relevant only if deployment enters scope.

Workers share a working tree and preserve each other's changes. The original sequence was contract agreement → backend/frontend implementation in separate files → API/frontend integration tests → fixes and independent review → documentation. Deployment was excluded.

## 4. Persistence and migration

`Database.__init__` applies an additive, repeatable migration. Existing IDs, password hashes, sessions, cities, and prayer records must remain unchanged. No relationships are created automatically.

| Schema element | Purpose and constraints |
| --- | --- |
| `users.can_supervise` | Defaults to false for existing users; enabling it preserves their diary |
| `supervisor_children` | `supervisor_id`, `child_id`, `created_at`; composite key prevents duplicates, foreign keys reference users, self-links are prohibited, `child_id` is indexed |
| `supervisor_invites` | `id`, `supervisor_id`, unique `token_hash`, `created_at`, `revoked_at`; partial unique index enforces one active invite per supervisor |
| `supervisor_invite_acceptances` | `invite_id`, `child_id`, `request_key`, `accepted_at`; unique `(child_id, request_key)` records operations independently of current relationships |

Tokens are securely random and stored only as SHA-256 hashes. Creation/rotation returns the raw token once; metadata listing cannot recover it. A lost link must be replaced. There is no single-child `accepted_by` or `accepted_at` on the invitation itself.

### Acceptance, concurrency, and retry semantics

Acceptance uses a short SQLite `BEGIN IMMEDIATE` transaction:

1. Look up the receipt for the authenticated child and `request_key`.
2. A receipt for the same token returns the original supervisor response without creating a relationship. Reusing the key for another token returns `409`.
3. A new operation verifies an active invite and rejects self-linking.
4. Insert the relationship with `INSERT OR IGNORE`, record the receipt, and commit. The invitation remains active.

A delayed retry cannot restore access after unlinking. Relinking requires a new explicit confirmation and key; the same still-active invitation can be used. A replay's `{ok:true}` acknowledges the original operation, not the existence of a current relationship. Current relationship lists remain authoritative.

Rotation revokes the old invite and creates its replacement atomically. SQLite serializes writers: acceptance committed before revocation can establish a relationship; new acceptance evaluated after revocation fails. An already recorded acceptance can still return its receipt after revocation without restoring access. The partial unique index protects concurrent creation of active invitations.

## 5. Implemented API contract

All paths use `/api/v1`. Login retains alias/password input. Registration accepts optional `account_type: "child" | "supervisor"` (default `"child"`) and returns `201` with the existing `{user, access_token, token_type}` envelope. User representations include `can_supervise` and existing city/timezone data.

| Method and path | Request / successful response |
| --- | --- |
| `GET /auth/me` | Current user including `can_supervise` |
| `PATCH /account` | `{can_supervise:true}` enables supervision; disabling is unsupported; existing city updates remain supported |
| `POST /supervisor/invites` | `201`: `{invite:{id,created_at,revoked_at},token,link}`; existing active invite returns `409` |
| `POST /supervisor/invites/rotate` | Atomic replacement; same `201` response envelope |
| `GET /supervisor/invites` | `{invites:[...]}` with metadata, no raw tokens |
| `DELETE /supervisor/invites/{invite_id}` | Revoke an owned invite; `{ok:true}`; retain relationships |
| `POST /supervisor-invites/preview` | `{token}` → `{supervisor:{id,alias},scope:"prayer_history_read"}`; no child list or records |
| `POST /supervisor-invites/accept` | Authenticated `{token,request_key}` → `{ok:true,supervisor:{id,alias}}` |
| `GET /account/supervisors` | `{supervisors:[{id,alias}]}` for the current account |
| `DELETE /account/supervisors/{supervisor_id}` | Remove one current-account relationship |
| `GET /supervisor/children` | `{children:[user]}` for the current supervisor |
| `DELETE /supervisor/children/{child_id}` | End one supervisor-owned relationship |
| `GET /supervisor/children/{child_id}/days/{date}` | Authorized child's existing day-snapshot shape |
| `GET /supervisor/children/{child_id}/history` | Required `from`/`to`; inclusive range at most 366 days |
| `GET /supervisor/children/{child_id}/statistics` | Optional `from`/`to` supplied together, with existing date-range rules |

Token inputs accept 32–128 URL-safe characters; operation keys accept 16–128. Reuse a key for retries of one operation; generate a new key for a new confirmation. Preview/new acceptance of invalid, revoked, or replaced invitations returns `404`; self-linking and conflicting key reuse return `409`. Existing errors include `401` for invalid sessions, `403` for unsafe origins, `422` for invalid inputs, and `429` for rate limits. Errors use Arabic `detail` messages without disclosing unrelated children's data.

`link` is null without a configured trusted origin. With `PRAYER_TRUSTED_ORIGIN`, it uses an absolute `/#invite=TOKEN` URL. The frontend falls back to `location.origin` when the server returns null; deployed-origin configuration therefore needs explicit review.

## 6. Authorization and security

`supervised_child` checks the capability and current database relationship before and after reading child data. IDs alone do not authorize access. Regular mutation routes remain scoped to the authenticated account; no child-write API is added for supervisors.

The before/after check denies reads when unlinking is observed during the operation. It cannot retract already delivered data or prevent an in-flight response whose final authorization check preceded unlinking. Removal protects subsequent reads, not copies already seen by a supervisor.

Existing Origin protection applies to cookie-authenticated mutations; existing session protections and API `no-store` headers remain in effect. Invitation secrets use POST bodies rather than query parameters. URL fragments avoid sending the token in the initial HTTP request, but remain secrets accessible to recipient JavaScript/tab storage; they must not appear in logs or analytics.

Separate in-process, per-IP budgets permit 60 create/rotate requests and 600 preview/accept requests per 10 minutes. These are not shared across workers or restarts. Public deployment requires review of proxy client-IP handling, shared abuse controls, HTTPS, secure cookies, and trusted-origin settings. A leaked reusable invite can admit further consenting accounts until rotation/revocation; it is not a verified roster.

## 7. Frontend behavior and asynchronous state

1. Offer account type at registration and capability activation for existing accounts.
2. Show linked children and create/copy/revoke/rotate controls. Explain link reuse, replacement, and why a lost token cannot be recovered.
3. Capture `#invite=TOKEN`, remove the fragment from the address bar, and retain the token in tab `sessionStorage` through authentication. Do not use `localStorage` for it.
4. Preview the supervisor alias and full-history permission, with explicit accept/cancel controls. Opening, previewing, or signing in never links automatically.
5. Keep the operation key stable across retries, clear pending invitation state on completion/cancellation, and provide manual copying if Clipboard fails.
6. Show supervisors on the child's account page and allow independent removal. Display a selected child's diary read-only using their city/timezone and existing reward calculations.
7. Hide/disable child mutation controls and guard handlers; backend authorization is authoritative. Keep Arabic RTL loading/error messages.

Context stamps/request epochs guard account and selected-child boundaries. Clear child data on switching, logout, and unlinking. Independent authentication request tracking prevents invitation cancellation from enabling overlapping sign-in requests; startup `/auth/me` cannot replace a newer login. These are documented regression fixes from the implementation review.

## 8. Acceptance and verification

| Area | Required evidence |
| --- | --- |
| Relationships | Several children use one link; a child adds several supervisors; duplicate acceptance creates no duplicate relationship |
| Consent/ownership | Preview/login/GET do not link; reject self-links, unauthenticated acceptance, and foreign invite management |
| Concurrent operations | Independent acceptances succeed; create/rotate maintain one active invite; acceptance versus revocation respects transaction ordering |
| Retry safety | Old retry after unlinking does not restore access; new confirmation/key can relink |
| Revocation | Removing one supervisor preserves others; later reads fail; rotation preserves established relationships |
| Data isolation | Changed child IDs cannot expose unrelated data; supervisors cannot change child records/city; child city drives schedules |
| Migration | Preserve existing sessions/days; repeat initialization safely |
| Frontend races | Account/child switching, cancelled invites, and delayed bootstrap responses cannot show the wrong data |
| Link flow | Pending invite survives authentication; explicit consent, copying, and copy fallback work through the DOM harness |

Evidence files: `backend/tests/test_supervisors.py` and `tests/supervisors_integration.test.mjs`, alongside existing API, schedule, domain, and integration tests. Relevant source and test presence were inspected for this update; current test execution and completeness of coverage were not re-established.

Run from the project root with the required environment:

```sh
.venv/bin/python -m unittest discover -s backend/tests -v
PYTHON="$PWD/.venv/bin/python" npm test
npm run check
git diff --check
```

Historical October 2 results: 34/34 Python and 12/12 JavaScript tests passed; checks of `frontend/app.js`, `frontend/domain.js`, and whitespace passed. Tests used temporary local API servers and mocked schedule providers. This translation did not run tests, a browser, desktop control, screenshots, live AlAdhan checks, or deployment. Logical tests do not establish visual quality.

## 9. Tech lead review and rollout questions

These are review recommendations, not additional implemented guarantees:

- Confirm acceptance of full-history visibility, non-expiring links, and self-service relinking. Start-date restrictions or write permission change the API and authorization model.
- Review origin/proxy configuration, including the implemented current-origin fallback and client-IP trust. Do not assume the server always supplies an absolute trusted link.
- Back up SQLite, test migration on a representative copy, rerun checks against the exact release revision, and verify legacy login/diary behavior before rollout. Historical results are not release evidence for a later revision.
- Define rollback behavior. The migration is additive; no down-migration is described. Verify older code against the retained schema without discarding relationships or receipts.
- Establish operational bounds and retention for invitation metadata, acceptance receipts, relationship counts, and child lists. This document does not establish cleanup jobs, pagination guarantees, or a retention policy.
- Review shared rate enforcement and audit events for creation, rotation, acceptance, and unlinking. Exclude raw tokens and sensitive prayer records from logs.
- Before using real children's data, complete the privacy, parental-consent, safeguarding, and deployment review required by `backend/README.md`. Alias/password accounts do not verify authority or identity.

No application code was changed by this translation. Production readiness and deployment remain separate decisions.
