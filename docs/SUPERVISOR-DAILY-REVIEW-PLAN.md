# Supervisor Daily Student Review — Implementation Plan

Date: October 3, 2026. **Status: implemented in the local working tree on October 3, 2026; not deployed.**

This plan extends the existing read-only supervision feature. It is written in English for technical review; the proposed application screen and its labels are Arabic RTL. References: [supervision contract](SUPERVISOR-IMPLEMENTATION-PLAN.md), [repository map](repo-map.md), and [existing implementation notes](task-notes/supervisor-links.md).

## 1. Goal and scope

A teacher opens a single daily report to review what every currently linked student recorded yesterday, then moves backward or forward through dates. Each student occupies one row. Each prayer has one column containing independent fard and Sunnah icon slots; a combined totals column supplements the detailed records.

Use the existing visual language, colors, medal icons, and prayer states. The report is read-only. No student edits, rank badges, class/group administration, messaging, exports, or new reward rules are included. Existing individual-child diary views remain available.

The proposed defaults below are implementation recommendations, not additional confirmed requirements.

## 2. Arabic screen and column contract

Entry point: add **«متابعة الطلاب»** for accounts with `can_supervise`, alongside existing supervision management. Screen title: **«سجل الطلاب اليومي»**. Show the selected date prominently and a small legend explaining medals and states.

Columns now appear as **الطالب، الفجر، الظهر، العصر، المغرب، العشاء، الإجمالي**. Each prayer cell preserves its individual records in fixed-width icon slots, in RTL order before → fard → after:

| Prayer | Slots |
| --- | --- |
| الفجر | Before Sunnah, fard |
| الظهر | Before group 1, before group 2, fard, after Sunnah |
| العصر | Fard |
| المغرب | Fard, after Sunnah |
| العشاء | Fard, after Sunnah, Witr |
| الإجمالي | Fard completion/gold and Sunnah completion/silver together |

Unrecorded Sunnah uses a gray ×; unrecorded fard uses a red ×. Completed fard uses gold with 1 at home or 27 at the mosque; completed Sunnah uses silver. Reserve identical icon width and an independent reward-number area so crosses and medals align vertically across all students. Compact subheadings explain each slot's before/after/group/Witr meaning; accessible labels retain full recorded states. Not-yet-due and unavailable schedule states remain distinct from unrecorded states. The table remains read-only and must fit the viewport without horizontal scrolling. Use a fixed table layout, proportional column widths, responsive icon-slot sizes, and wrapping student aliases while retaining all prayer details.

### Totals: counts versus rewards

For a past day, **إجمالي الفرائض** shows completed prayers out of five, plus gold earned—for example **٣ / ٥ · 🥇 ٥٥** for two mosque prayers and one home prayer. **إجمالي السنن** shows completed controls out of seven, plus silver earned—for example **٤ / ٧ · 🥈 ٤**.

Seven sunnah controls means Fajr-before, two Dhuhr-before groups, Dhuhr-after, Maghrib-after, Isha-after, and Witr. This is not seven rakahs. If rakah totals are added later, they must be separately labelled and exclude a fabricated Witr count.

For today, denominators follow existing child-city eligibility: only elapsed fard times and their associated sunnah controls count. Medal targets remain distinct from completion counts: a past day's maximum is 135 gold and 7 silver. Reuse backend snapshot statistics and existing domain rules rather than create a second scoring system. Rows are ordered by achievement as specified below; no class-wide totals or rank badges are proposed.

## 3. Date navigation and timezone semantics

- Default to **yesterday** on first entry. Use the supervisor's selected-city timezone; fall back to the browser's local timezone when no city is selected, with the reference timezone visible.
- Controls: **«اليوم السابق»**, date picker, **«اليوم التالي»**, **«أمس»**, and **«اليوم»**. Reuse the existing RTL arrow convention and explicit accessible labels.
- Keep the selected date while opening a student's diary and returning to the report. Logout/account switching clears report data and resets the default context.
- Proposed maximum selectable date is today in the supervisor's reference timezone. Disable forward navigation at that boundary. Do not invent a lower history cutoff in this feature.
- Dates are strict Gregorian ISO `YYYY-MM-DD` for storage/API. Format the visible date in Arabic with an explicit Gregorian calendar so the picker and display agree.
- Every row refers to the same selected calendar-date key, not a shared UTC time interval. Eligibility and schedule interpretation use each student's city/timezone. A selected date can be past for one student and still current/future for another; show that accurately.
- Switching dates must immediately remove or visibly replace the previous report with a loading state. A delayed response for an earlier date cannot populate the current table.

## 4. Backend design and proposed API

Add **`GET /api/v1/supervisor/daily-review?date=YYYY-MM-DD&limit=50&offset=0`**. This contract is now implemented.

Use a batch endpoint rather than issue one day request per student from the browser. Default page size 50, maximum 100; the UI allows reviewing all students through labelled paging. Sort the entire authorized roster before pagination: completed eligible fard count descending, then completed eligible sunnah controls descending, then normalized alias and user ID for stable ties. Gold medal amounts do not determine order. Rows with unknown eligibility totals sort after known totals. The first row has the highest record. Offset paging is a simple proposed first version; roster edits can shift page boundaries. Refresh resets to the first page. If stable traversal under frequent roster edits is required, settle a cursor contract before implementation rather than promising snapshot consistency. The complete roster belongs to the report even when a student has no saved row for that day.

Proposed envelope:

```json
{
  "date": "2026-10-02",
  "generated_at": "2026-10-03T08:00:00Z",
  "limit": 50,
  "offset": 0,
  "total_students": 1,
  "rows": [
    {
      "student": {"id": 12, "alias": "student_demo", "city_id": "jeddah", "timezone": "Asia/Riyadh"},
      "has_record": true,
      "day": "existing day snapshot object: prayers, sunnah, dhuhr_before_blocks, stats, schedule"
    }
  ]
}
```

The `day` string above is a schema placeholder; the actual response must contain the existing snapshot object, not a string. Preserve raw per-control fields and existing schedule/statistics metadata. Return only identity/context fields needed for the report; never credentials, sessions, invitation tokens, or unrelated account data.

`has_record` comes from actual database-row presence, not from a zero total or guessed version. For absent days, produce the existing blank snapshot without inserting a record and mark the row **«لا يوجد سجل محفوظ لهذا اليوم»**. A saved all-zero day and an absent day are different states.

### Query and authorization strategy

1. Require a current authenticated account with supervisor capability.
2. Query only current relationships owned by that supervisor, and batch-load requested-date records using a relationship join/left join. Pagination and counts are applied to the authorized roster.
3. Reuse `blank_day`, `row_day`, and `day_stats` with each child's context. Follow `statistical_snapshot` for eligibility: past dates need no upstream schedule request, future dates have no eligible prayers, and today requires timings. Do not call `selected_snapshot` once per student: it retrieves schedules even for past dates. Preserve the no-city fallback and capture one request instant for consistent row calculations.
4. Recheck relationships before returning, following the existing before/after authorization pattern. Drop rows unlinked during report construction and reconcile pagination/count metadata with the final authorized roster; never return a removed child's row merely because it was authorized at the initial query.
5. Do not hold a SQLite write lock or long database transaction while fetching external schedules. Group schedule work by distinct city/date and invoke it once per group. Existing caching is not a concurrent single-flight guarantee. An individual unavailable schedule should not fail every row; distinguish provider unavailability from an unexpected server error.

No schema migration or new dependency is expected. The existing relationship key `(supervisor_id, child_id)` and day key `(user_id, date)` support the join. Verify query plans before proposing an additional index.

Reuse existing errors: `401` unauthenticated, `403` missing capability, `422` invalid date/pagination, and existing server failure behavior. An authorized supervisor with no students receives `200` with an empty list. Use `Cache-Control: no-store`. Changing query values cannot expand the roster beyond authorized relationships. Do not add an arbitrary child-ID filter that bypasses roster checks.

The revocation boundary matches the existing feature: rechecks reduce races but cannot retract a response already delivered or one whose last check preceded removal. The report is a read-time view, not a permanent archival snapshot. Concurrent student updates and roster changes can affect later refreshes/pages.

## 5. Frontend structure, accessibility, and loading

Add separate report state: selected date, rows, pagination, loading/error status, and a monotonically increasing request epoch. Guard responses with account/session, view, selected date, page, and request identity. Invalidate requests on account switching, logout, leaving the report, or relationship management changes. Abort superseded fetches where practical; response guards remain required.

Use a semantic HTML table with column headers, student row headers, a caption naming the date, and labelled cells. Keep the header visible. Fit all seven columns to the viewport without horizontal scrolling or hiding prayer records; shrink icon slots and spacing on narrow screens and wrap student aliases. Reuse current typography, colors, medal treatment, and Arabic number formatting.

States: loading, no linked students, no saved student records, unavailable schedule per row, recoverable request error with **«إعادة المحاولة»**, and refresh with **«تحديث»**. Announce date/result changes accessibly. Escape aliases before HTML rendering. Student names can open the existing read-only diary for the same date.

Do not silently show stale results as current after a failed navigation. No editable medal controls, inline grading, comparison badges, or bulk mutation behavior.

## 6. Files and implementation sequence

| Area | Expected files / changes |
| --- | --- |
| Backend | `backend/main.py`: authorized roster/day batch query and report route, reuse snapshot helpers |
| Frontend | `frontend/app.js`: supervisor entry point, report state, date/paging handlers, read-only cell renderers |
| Styling | `frontend/styles.css`: RTL table, viewport-fitting columns, fixed-width aligned responsive icon slots, compact medals |
| Backend tests | `backend/tests/test_supervisors.py`: report data, authorization, empty days, schedules, pagination, revocation |
| Frontend tests | `tests/supervisors_integration.test.mjs`: date navigation, cell rendering, stale responses, paging, diary return |
| Documentation | `backend/README.md`, `README.md`, affected `docs/repo-map.md` sections |

Before implementation, record confirmed contract decisions in a concise task note. Stabilize backend envelope and totals semantics first; then backend/frontend can proceed with separate ownership. Add tests, resolve failures, review authorization/race handling, and update documentation. Implementation was authorized by the user on October 3, 2026, including achievement sorting.

## 7. Acceptance and meaningful tests

- Yesterday shows every currently linked student, including students with no saved record, across any pages.
- Each requested Arabic column is present in chronological order; no prayer/sunnah detail is replaced with a total.
- Fard 0/1/2 and sunnah completion states are accurate; both Dhuhr-before groups and Witr are independently represented.
- Example fixtures verify 3/5 fard with 55 gold, and 4/7 sunnah with 4 silver; medals do not become prayer/rakah counts.
- Previous/next/date picker/yesterday/today update the date and records; next is disabled at the proposed today boundary.
- Child-city schedules, mixed timezones, no-city accounts, future eligibility, and schedule outages retain existing semantics without fabricated totals.
- Report reads do not create missing day records or change rewards. Student edits followed by refresh are reflected.
- Unauthenticated/non-supervisor requests fail; unrelated students never appear. Removal during report construction prevents the row from being returned; other rows remain accessible.
- Ranking applies to the whole roster before pagination: higher fard completion first, then higher sunnah completion; exact ties use alias/ID. Test that medal bonuses cannot outweigh fard count. Pagination validates limits/offsets and handles roster changes without cross-account leakage.
- Late date/page/account responses cannot overwrite newer results; leaving and returning preserves only the intended date context.
- Aliases are safely escaped; cells expose meaningful accessible labels; existing single-child supervision and diary tests stay passing.

Proposed verification commands: `.venv/bin/python -m unittest discover -s backend/tests -v`, `PYTHON="$PWD/.venv/bin/python" npm test`, `npm run check`, and `git diff --check`. Implementation verification on October 3 passed 40 Python and 13 JavaScript tests. No visual verification was performed. Browser/desktop verification requires an explicit later request.

## 8. Review decisions before implementation

Confirm the proposed yesterday default, supervisor reference timezone, today navigation cap, and page sizes. Confirm that “total sunnah” means completed controls (seven maximum including Witr), with medals shown alongside counts. The existing alias is the student name; real-name collection is not proposed.

Review batch-query and schedule-fetch cost for a representative class size. Before adding caching or limits beyond pagination, measure existing helpers; do not assume one external call per student is acceptable. The report should not change existing permission scope or account relationships.
