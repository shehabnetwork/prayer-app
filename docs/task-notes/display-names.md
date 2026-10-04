# Separate display names

Objective: retain the existing alias as the login username and add an independent,
editable display name for the account avatar, greetings and group identities.

Acceptance: registration collects both fields; login continues using alias;
account settings edit display name; all social/name presentation uses display
name; existing accounts preserve their former visible name through backfill;
SQLite, PostgreSQL and SQLite import support the field. Validate API and frontend
flows without browser tools. See ../repo-map.md for application architecture.

Existing group work is present in the working tree and must be preserved.

Implementation: `alias` remains the stable unique username/API login field.
`display_name` is a separate nonunique trimmed 1–80 character label; control
characters are rejected. New UI registration requires both names; older API
clients inherit their alias when display_name is omitted. Account settings edit
only the display name. User/member/report/invitation responses carry the label,
and frontend identity rendering escapes it. Migration 004 and SQLite startup
backfill existing accounts; snapshot import preserves names or defaults to alias.

Validation: combined SQLite and real PostgreSQL backend suite: 115 tests,
one intentional SQLite-only fixture skip in PostgreSQL parity. Node suite:
14 tests passed, including registration, edit, login separation, group views,
invitation, diary/report and escaping. JavaScript syntax and diff whitespace
checks passed. Browser rendering and deployment were not performed.

Preview runtime verification (October 4): the Cloudflare preview backend on
port 8010 was still serving the pre-display-name AccountPatch schema. Restarted
that backend with its existing PostgreSQL, secure-cookie and trusted-origin
configuration, keeping the tunnel running. The exact `{"display_name":"محمد شهاب"}`
payload returned successfully and persisted through `/auth/me`; the temporary
verification account was removed. Source validation already accepted the name;
this failure was caused by stale running code.
