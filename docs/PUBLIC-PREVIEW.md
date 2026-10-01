# تجربة عامة — 1 أكتوبر 2026

الرابط الحالي: https://minolta-saying-advocate-saver.trycloudflare.com

يشغّل الجهاز المتصل نسخة FastAPI + SQLite مباشرة. لا توجد بوابة دخول ChatGPT
على هذا الرابط؛ أنشئ حسابًا تجريبيًا باسم مستعار وكلمة مرور من شاشة التطبيق.
الرابط مؤقت ويبقى متاحًا ما دام الجهاز والخادم والنفق يعملون. الموقع المستضاف
القديم على chatgpt.site لم يُعدّل في هذه العملية.

كل المكتبات داخل `.venv` وأداة النفق داخل `.tools`؛ لم يُثبّت أي برنامج عالميًا.
البيانات محفوظة في `backend/data/prayer.sqlite3`.

للتشغيل مجددًا من جذر المشروع، شغّل النفق أولًا في طرفية:

    .tools/cloudflared tunnel --url http://127.0.0.1:8000 --no-autoupdate --protocol http2

سيظهر رابط HTTPS جديد. استخدمه بدل `PUBLIC_HTTPS_URL` في طرفية ثانية:

    PRAYER_COOKIE_SECURE=true PRAYER_TRUSTED_ORIGIN=PUBLIC_HTTPS_URL .venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 --no-proxy-headers

لا تغيّر عنوان الأصل إلى قيمة عامة أو نجمة؛ يجب أن يطابق رابط النفق الحالي.
أوقف العمليتين بـ Ctrl+C عند انتهاء التجربة.
