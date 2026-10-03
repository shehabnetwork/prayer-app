# خريطة المشروع

خريطة مختصرة للتنقّل، وليست بديلًا عن المصدر الحالي. أُعدّت في 2 أكتوبر 2026 بقراءات موجّهة لملفات الدخول والإعداد والاختبارات.

- الالتزام المرجعي: `fb1c96a5b85eb38ae5c11cbf384ecdb0cde0f515`.
- الحالة المقروءة: نسخة العمل عند هذا الالتزام؛ كان `AGENTS.md` و`docs/SUPERVISOR-IMPLEMENTATION-PLAN.md` غير متتبّعين. لم تُعدّل ملفات التطبيق.
- النطاق: تطبيق عربي RTL لتتبّع الفرائض والسنن، والحسابات وسجل الأيام والإنجازات، مع مواقيت حسب المدينة وروابط إشراف مشتركة ومتعددة المشرفين.

## المكونات ونقاط الدخول

| المسار | المسؤولية ونقاط التنقّل |
| --- | --- |
| `frontend/index.html` | صفحة الدخول للواجهة؛ تحمل `app.js`. |
| `frontend/app.js` | حالة الواجهة، العرض والتفاعل، وطلبات API. |
| `frontend/domain.js` | حسابات وقواعد المجال في الواجهة. |
| `frontend/styles.css`، `frontend/assets/` | التنسيق والأصول المرئية. |
| `backend/main.py` | تطبيق FastAPI؛ `create_app` يبني التطبيق، و`Database` المستورد من `backend/database.py` يدير التخزين عبر SQLite أو PostgreSQL. مسارات `/api/v1` تشمل المصادقة والحساب والمدن والأيام والفرائض والسنن والتاريخ والإحصاءات. يركّب `frontend/` على `/` عبر `StaticFiles`. نقطة تشغيل Uvicorn هي `backend.main:app`. |
| `backend/schedule.py` | `CITIES` و`ScheduleService`: مواقيت AlAdhan، التخزين المؤقت، وتحديد الصلوات التي حان وقتها. |
| `backend/requirements.txt` | اعتمادات Python المثبّتة الإصدارات، ومنها FastAPI وUvicorn وhttpx وPydantic. |
| `package.json` | مشروع JavaScript بوحدات ES؛ يعرّف أوامر الاختبار وفحص الصياغة. |
| `backend/tests/test_api.py`، `backend/tests/test_prayer_times.py` | اختبارات API والمواقيت. |
| `tests/domain.test.mjs`، `tests/frontend_integration.test.mjs` | اختبارات قواعد الواجهة وتكاملها مع الخادم. |
| `tests/ui_smoke.py`، `tests/requirements-ui.txt` | اختبار واجهة اختياري يعتمد على Playwright؛ لم يُشغّل في إعداد هذه الخريطة. |

مسار البيانات الأساسي: الواجهة ← API في FastAPI ← SQLite. خدمة المواقيت تتصل بـ AlAdhan. قاعدة البيانات الافتراضية `backend/data/prayer.sqlite3`؛ إعدادات الخادم تشمل `PRAYER_DB_PATH` و`PRAYER_COOKIE_SECURE` و`PRAYER_TRUSTED_ORIGIN`.

## الإشراف (تحديث نسخة العمل بعد الالتزام المرجعي)

- backend/main.py: Registration وInviteToken وInviteAcceptance، ترحيل users.can_supervise وجداول supervisor_children وsupervisor_invites وsupervisor_invite_acceptances؛ مسارات /supervisor و/supervisor-invites و/account/supervisors. الدعوة متعددة الاستخدام مع رابط نشط واحد؛ معاملات SQLite للقبول والتدوير، وقراءة السجل فقط بعد التحقق من العلاقة.
- frontend/app.js وfrontend/styles.css: التسجيل كمشرف وتفعيل القدرة، قبول fragment الدعوة، إدارة الرابط والمشرفين والأطفال وعرض دفتر الطفل للقراءة فقط. تفاصيل التنفيذ والتحقق تسجل في docs/task-notes/supervisor-links.md.
- backend/tests/test_supervisors.py وtests/supervisors_integration.test.mjs: تغطية العلاقات والدعوات والترحيل والصلاحيات والقبول المتزامن، وتكامل الواجهة مع API وحماية الردود المتأخرة بين الحسابات والأطفال.

## التشغيل والتحقق

المتطلبات **بحسب README**: Python 3.12+، وNode 24+ لاختبارات الواجهة. الأوامر التالية موجودة في التوثيق أو `package.json` وتُنفّذ من جذر المشروع، داخل بيئة Python المناسبة. لم يُنفّذ تثبيت الاعتمادات أو تشغيل خادم دائم في هذه المهمة؛ نتائج الاختبارات الحالية أدناه:

| الغرض | الأمر |
| --- | --- |
| تثبيت اعتمادات الخادم | `python -m pip install -r backend/requirements.txt` |
| التشغيل المحلي | `python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000` |
| اختبارات Python | `python -m unittest discover -s backend/tests -v` |
| اختبارات JavaScript والتكامل | `PYTHON="$PWD/.venv/bin/python" npm test` |
| فحص صياغة JavaScript | `npm run check` |

`npm test` يشغّل `node --test tests/*.test.mjs`، و`npm run check` يفحص `frontend/app.js` و`frontend/domain.js` بواسطة `node --check`. لم يُحدَّد أمر بناء مستقل في `package.json`؛ الخادم يقدّم ملفات الواجهة مباشرة.

تحقق 2 أكتوبر 2026: `.venv/bin/python -m unittest discover -s backend/tests -v` نجح (34/34)، و`node --test tests/*.test.mjs` باستخدام Python البيئة المحلية نجح (12/12). استُخدم Node المرفق مع البيئة. فحص `node --check` لملفي app.js وdomain.js و`git diff --check` نجحت. اختبارات API المحلية احتاجت السماح بفتح منافذ localhost خارج sandbox. لا اختبار بصري أو اتصال فعلي بمصدر المواقيت.

## مراجع وحدود التحقق

- `AGENTS.md`: قواعد الاستكشاف وصيانة هذه الخريطة؛ راجع المصدر قبل أي تعديل لاحق.
- `README.md`: التشغيل، نموذج الصلاة، ومتطلبات الاختبار. نتائج الاختبارات التاريخية المذكورة فيه لم تُعدّ التحقق منها هنا.
- `backend/README.md`: عقد API، المصادقة، التخزين وإعدادات الخادم.
- `docs/PUBLIC-PREVIEW.md`: مرجع المعاينة العامة؛ تفاصيل النشر وصلاحية الرابط لم تُتحقّق في هذه المهمة.
- يشير README إلى نسخة مستقلة باسم `prayer-cloud` خارج نطاق هذه الخريطة؛ لم تُفحص.
- لم يُختبر العرض البصري أو الاتصال الفعلي بـ AlAdhan أو النشر. شُغّلت خوادم API مؤقتة لاختبارات التكامل؛ راجع ملاحظات المهمة للتحقق والمراجعة الخاصين بالإشراف.

## Daily supervisor review — October 3, 2026

- `backend/main.py`: `GET /api/v1/supervisor/daily-review`; batch roster/day join, current relationship recheck, grouped current-day schedules, completed-fard/Sunnah ordering before pagination.
- `frontend/app.js`, `frontend/styles.css`: Arabic RTL detailed daily table, medals, yesterday default, date/page navigation, guarded responses, and read-only student diary return.
- `backend/tests/test_daily_review.py`, `tests/daily_review_integration.test.mjs`: report authorization, sorting, schedule/timezone boundaries, raw records, date navigation and stale responses.
- Verification: October 3 local run passed 40 Python tests and 13 JavaScript tests; source review and syntax/whitespace checks are tracked in `docs/task-notes/daily-review.md`. No browser/visual verification or deployment.

Daily table compact layout: `frontend/app.js` groups independent controls into five prayer columns plus combined totals; `frontend/styles.css` uses proportional fixed-layout columns and aligned RTL icon slots without horizontal scrolling.

Compact record views: `compactPrayerTable` in `frontend/app.js` is shared by daily student report, achievement history (date rows), and the selected student read-only day. The editable own-day board remains separate. Verification details: `docs/task-notes/compact-record-views.md`.

## PostgreSQL storage — October 4, 2026

- Inspected base: `1a891f586165e2875e199f9093a4c122bbc24ae9`; implementation branch `codex/postgresql-migration`.
- `backend/database.py`: SQLite-compatible storage interface and Psycopg PostgreSQL adapter, schema migrations and ordered owner-row transaction guards. `backend/main.py` keeps API contracts and imports this class.
- `backend/migrations/001_initial.sql`: initial PostgreSQL schema, identity IDs, foreign keys and invitation constraints; `schema_migrations` tracks applied versions.
- `backend/import_sqlite.py`: read-only SQLite snapshot, empty-target atomic transfer, verification, identity reset and dry-run CLI.
- `backend/tests/test_postgres.py`: isolated-schema API parity and concurrency/configuration tests; `backend/tests/test_postgres_import.py`: source validation and real PostgreSQL transfer/authentication tests. Set `PRAYER_TEST_DATABASE_URL` to run PostgreSQL tests.
- `compose.postgres.yml`: optional local PostgreSQL16 service with persistent volume and localhost-only port15432. `PRAYER_DATABASE_URL` enables PostgreSQL; no URL retains existing local SQLite behavior.
- Setup/cutover/rollback commands: `docs/POSTGRESQL-MIGRATION.md`; final verification and review: `docs/task-notes/postgresql-migration.md`.
