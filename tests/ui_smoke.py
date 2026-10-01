"""Real Chromium end-to-end smoke checks using a synthetic account and temporary SQLite.
Optional setup: pip install -r tests/requirements-ui.txt; playwright install chromium
Run from repository root: python tests/ui_smoke.py
"""
import json
import os
from pathlib import Path
import shutil
import socket
import secrets
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "qa"
OUT.mkdir(parents=True, exist_ok=True)
checks = []
TEST_PASSWORD = secrets.token_urlsafe(24)

def check(name, condition):
    assert condition, name
    checks.append(name)
    print("PASS", name, flush=True)

with tempfile.TemporaryDirectory(prefix="prayer-ui-") as temporary:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1",0))
        port=sock.getsockname()[1]
    base=f"http://127.0.0.1:{port}"
    env={**os.environ,"PRAYER_DB_PATH":str(Path(temporary)/"qa.sqlite3")}
    server=subprocess.Popen([sys.executable,"-m","uvicorn","backend.main:app","--host","127.0.0.1","--port",str(port)],cwd=ROOT,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
    try:
        for attempt in range(60):
            try:
                with urlopen(base+"/api/v1/health",timeout=1) as response:
                    if response.status==200: break
            except Exception: time.sleep(.1)
        else: raise RuntimeError("Local test server did not start")
        with sync_playwright() as p:
            executable=shutil.which("chromium") or shutil.which("google-chrome")
            browser=p.chromium.launch(headless=True,executable_path=executable,args=["--no-sandbox"] if executable else [])
            context=browser.new_context(viewport={"width":1280,"height":1050},device_scale_factor=1)
            page=context.new_page()
            errors=[]
            page.on("pageerror",lambda error:errors.append(str(error)))
            page.goto(base)
            page.get_by_role("button",name="حساب جديد",exact=True).click()
            page.get_by_label("الاسم المستعار",exact=True).fill("بطل_تجريبي")
            page.get_by_label("كلمة المرور",exact=True).fill(TEST_PASSWORD)
            page.get_by_role("button",name="ابدأ رحلتك ✦",exact=True).click()
            page.locator(".prayer-board").wait_for()
            page.locator(".character-button:enabled").first.wait_for()
            check("alias registration opens authenticated daily tracker",page.locator(".prayer-board").is_visible())
            check("five obligatory rows and exactly one character control per prayer",page.locator(".prayer-row").count()==5 and page.locator(".character-button").count()==5)
            check("Witr lives separately after obligatory board",page.locator(".witr-card").count()==1 and page.locator(".prayer-row [data-action=witr]").count()==0)
            check("Arabic RTL document",page.locator("html").get_attribute("dir")=="rtl" and page.locator("html").get_attribute("lang")=="ar")
            def click(selector):
                page.locator(selector).click()
                page.wait_for_function("!document.querySelector('.character-button').disabled")
            fard='[data-action="prayer"][data-key="fajr"]'
            for expected in ["صلّيت في البيت","صلّيت في المسجد","لم أصلّ بعد"]:
                click(fard)
                check("Fajr character cycles to "+expected,expected in page.locator(fard).get_attribute("aria-label"))
            click('[data-action="witr"]')
            click('[data-action="sunnah"][data-key="fajr_before"]')
            check("sunnah and Witr never increase five-prayer daily percent",page.locator(".percentage").inner_text()=="٠٪")
            second='[data-action="sunnah"][data-key="dhuhr_before"][data-block="1"]'
            first='[data-action="sunnah"][data-key="dhuhr_before"][data-block="0"]'
            click(second)
            check("second Dhuhr two-rakah block can be selected independently",page.locator(second).get_attribute("aria-pressed")=="true" and page.locator(first).get_attribute("aria-pressed")=="false")
            click(first)
            check("two Dhuhr blocks give four before-rakahs",page.locator(".block-total").inner_text()=="٤ من ٤ ركعات")
            click(second)
            check("one Dhuhr block gives two before-rakahs",page.locator(".block-total").inner_text()=="٢ من ٤ ركعات")
            click(fard)
            click('[data-action="prayer"][data-key="dhuhr"]')
            click('[data-action="prayer"][data-key="maghrib"]')
            click('[data-action="prayer"][data-key="maghrib"]')
            check("three completed obligatory prayers show 60 percent",page.locator(".percentage").inner_text()=="٦٠٪" and "٣ من ٥" in page.locator(".completed-text").inner_text())
            check("home and mosque counters are separate", "٢" in page.locator(".places").inner_text() and "١" in page.locator(".places").inner_text())
            check("all SVG and font assets load",page.evaluate("Array.from(document.images).every(i=>i.complete&&i.naturalWidth>0)"))
            today=page.locator("#day-date").input_value()
            page.screenshot(path=str(OUT/"desktop.png"),full_page=True)
            page.reload()
            page.locator(".prayer-board").wait_for()
            check("cookie session and saved prayers survive reload",page.locator(".percentage").inner_text()=="٦٠٪")
            page.get_by_role("button",name="اليوم السابق",exact=True).click()
            page.wait_for_function("document.querySelector('.prayer-board')!==null")
            check("date navigation opens an isolated daily snapshot",page.locator(".percentage").inner_text()=="٠٪" and page.locator("#day-date").input_value()!=today)
            page.get_by_role("button",name="اليوم",exact=True).click()
            page.wait_for_function("document.querySelector('.prayer-board')!==null")
            check("returning to today restores persisted daily state",page.locator(".percentage").inner_text()=="٦٠٪")
            page.locator('[data-view="achievements"]').first.click()
            page.locator(".badges-grid").wait_for()
            check("achievements and recorded history load",page.locator(".achievement-badge").count()==4 and page.locator(".history-row").count()==1)
            page.screenshot(path=str(OUT/"achievements.png"),full_page=True)
            page.locator(".history-row").first.click()
            page.locator(".prayer-board").wait_for()
            page.set_viewport_size({"width":768,"height":1024})
            check("tablet has no horizontal page overflow",page.evaluate("document.documentElement.scrollWidth<=window.innerWidth"))
            page.screenshot(path=str(OUT/"tablet.png"),full_page=True)
            page.set_viewport_size({"width":390,"height":844})
            check("mobile responsive layout has no horizontal page overflow",page.evaluate("document.documentElement.scrollWidth<=window.innerWidth"))
            check("main prayer touch controls stay at least 44px",page.locator(".character-button").first.bounding_box()["height"]>=44)
            page.screenshot(path=str(OUT/"mobile.png"),full_page=True)
            page.set_viewport_size({"width":1280,"height":1050})
            page.locator('[data-view="account"]').click()
            page.get_by_role("button",name="تسجيل الخروج",exact=True).click()
            page.locator("#auth-form").wait_for()
            check("logout returns to sign-in screen",page.locator("#auth-form").is_visible())
            page.locator("#alias").fill("بطل_تجريبي")
            page.locator("#password").fill(TEST_PASSWORD)
            page.locator('#auth-form [type="submit"]').click()
            page.locator(".prayer-board").wait_for()
            check("login restores the same per-account persisted progress",page.locator(".percentage").inner_text()=="٦٠٪")
            check("no JavaScript runtime errors",not errors)
            browser.close()
    finally:
        server.terminate()
        try: server.wait(timeout=5)
        except subprocess.TimeoutExpired: server.kill();server.wait()
report={"checks_passed":len(checks),"checks":checks,"screenshots":["desktop.png","tablet.png","mobile.png","achievements.png"],"data":"Synthetic alias, temporary test database removed automatically"}
(OUT/"ui-results.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
print(json.dumps(report,ensure_ascii=False,indent=2))
