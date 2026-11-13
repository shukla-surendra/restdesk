"""Browser tests: drive the real UI in Chromium against a local target API.

Skipped automatically when Playwright or a browser isn't available. In CI:
    uv run playwright install --with-deps chromium && uv run pytest tests/e2e
Locally, an installed Google Chrome is used if Playwright's own Chromium is missing.
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pw = pytest.importorskip("playwright.sync_api")
expect = pw.expect

from restdesk.server import make_handler  # noqa: E402
from restdesk.storage import Store  # noqa: E402

pytestmark = pytest.mark.e2e


# ---------------------------------------------------------------- local target API
class Target(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/item"):
            return self._json(200, {"id": 1, "title": "hello"})
        return self._json(404, {})

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path == "/login":
            return self._json(200, {"access_token": "tok-SECRET-123", "user": "bob"})
        return self._json(404, {})


def serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def req(rid, name, url, method="GET", pre="", post="", **settings):
    return {"id": rid, "name": name, "method": method, "url": url, "params": [], "headers": [],
            "body": {"mode": "none", "text": "", "form": []}, "auth": {"type": "none"},
            "scripts": {"pre": pre, "post": post},
            "settings": {"timeoutMs": 5000, "followRedirects": True, "verifyTls": True, **settings}}


@pytest.fixture(scope="module")
def browser():
    with pw.sync_playwright() as p:
        b = None
        for kwargs in ({}, {"channel": "chrome"}):
            try:
                b = p.chromium.launch(**kwargs)
                break
            except Exception:
                continue
        if b is None:
            pytest.skip("no Chromium/Chrome available for Playwright")
        yield b
        b.close()


@pytest.fixture
def app(tmp_path):
    target, turl = serve(Target)
    store = Store(tmp_path / "rd.db")
    store.save_workspace({"activeEnvId": "e1", "environments": [
        {"id": "e1", "name": "Dev", "vars": [{"key": "base", "value": turl, "enabled": True}]}],
        "collections": [{"id": "c1", "name": "Suite", "requests": [
            req("r1", "Get item", "{{base}}/item",
                post="test('ok', () => expect(res.status).toBe(200));\n"
                     "test('title', () => expect(res.json().title).toBe('hello'));"),
            req("r2", "Login", "{{base}}/login", method="POST",
                post="env.set('token', res.json().access_token);"),
            req("r3", "Sneaky", "{{base}}/item",
                post="test('no fetch', () => expect(typeof fetch).toBe('undefined'));\n"
                     "test('no XHR', () => expect(typeof XMLHttpRequest).toBe('undefined'));\n"
                     "test('no DOM', () => expect(typeof document).toBe('undefined'));\n"
                     "test('no importScripts', () => expect(typeof importScripts).toBe('undefined'));"),
            req("r4", "Spin", "{{base}}/item", post="while (true) {}", scriptTimeoutMs=1500),
        ]}]})
    server, url = serve(make_handler(store))
    yield store, url + "/"
    server.shutdown()
    target.shutdown()
    store.close()


def open_page(browser, url):
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(url)
    page.wait_for_selector(".req-row")
    page.errors = errors
    return page


def send(page, name):
    page.locator(".req-row", has_text=name).click()
    page.click("#btnSend")
    expect(page.locator("#btnSend")).to_have_text("Send", timeout=15000)


# ---------------------------------------------------------------- tests
def test_send_runs_tests_and_records_history(browser, app):
    store, url = app
    page = open_page(browser, url)
    send(page, "Get item")
    assert page.locator("#statusLine .pill").inner_text().startswith("200")
    assert page.locator("#testCount").inner_text() == "2/2"
    page.wait_for_selector(".hist-row")
    assert store.list_history()["total"] == 1
    assert page.errors == []


def test_history_redacts_secrets_and_replays(browser, app):
    store, url = app
    page = open_page(browser, url)
    send(page, "Login")
    page.wait_for_selector(".hist-row")
    entry = store.get_history(store.list_history()["items"][0]["id"])
    assert "tok-SECRET-123" not in json.dumps(entry)
    # the script still saw the real token, so request chaining keeps working
    expect(page.locator("#saveState")).to_have_text("saved")
    env_vars = {v["key"]: v["value"] for v in store.load_workspace()["environments"][0]["vars"]}
    assert env_vars["token"] == "tok-SECRET-123"
    page.locator(".hist-row").first.click()
    page.wait_for_selector("#statusLine .note")
    assert "secrets redacted" in page.locator("#statusLine").inner_text()


def test_scripts_are_sandboxed(browser, app):
    _, url = app
    page = open_page(browser, url)
    send(page, "Sneaky")
    assert page.locator("#testCount").inner_text() == "4/4"


def test_runaway_script_times_out_and_ui_stays_responsive(browser, app):
    _, url = app
    page = open_page(browser, url)
    started = time.time()
    send(page, "Spin")
    assert time.time() - started < 10
    page.click('[data-tab="rtests"]')
    assert "timed out" in page.locator("#resTests").inner_text()
    page.fill("#reqName", "Spin renamed")  # main thread is free
    assert page.locator(".req-row.active").inner_text().endswith("Spin renamed")


def test_delete_undo_and_trash_restore(browser, app):
    store, url = app
    page = open_page(browser, url)
    row = page.locator(".req-row", has_text="Login")
    row.hover()
    row.locator('[data-a="del"]').click()
    assert page.locator(".req-row", has_text="Login").count() == 0
    page.locator("#toast .toast-action").click()
    assert page.locator(".req-row", has_text="Login").count() == 1

    row = page.locator(".req-row", has_text="Login")
    row.hover()
    row.locator('[data-a="del"]').click()
    expect(page.locator("#saveState")).to_have_text("saved")
    page.click("#btnTrash")
    page.locator("#trashTable button", has_text="Restore").click()
    page.wait_for_selector(".req-row:has-text('Login')")
    expect(page.locator("#saveState")).to_have_text("saved")
    names = [r["name"] for r in store.load_workspace()["collections"][0]["requests"]]
    assert "Login" in names and store.list_trash() == []


def test_other_tab_saves_are_picked_up(browser, app):
    _, url = app
    ctx = browser.new_context()
    a, b = ctx.new_page(), ctx.new_page()
    for p in (a, b):
        p.goto(url)
        p.wait_for_selector(".req-row")
    a.locator(".req-row", has_text="Get item").click()
    a.fill("#reqName", "Renamed in A")
    b.wait_for_selector(".req-row:has-text('Renamed in A')", timeout=5000)
    ctx.close()


def test_stale_tab_gets_conflict_prompt_instead_of_overwriting(browser, app):
    store, url = app
    page = open_page(browser, url)
    # someone else saves behind this tab's back (no broadcast reaches it)
    ws = store.load_workspace()
    ws["collections"][0]["requests"].append(req("r9", "Added elsewhere", "http://x"))
    store.save_workspace(ws, base_revision=ws.pop("revision"))
    messages = []
    page.on("dialog", lambda d: (messages.append(d.message), d.accept()))
    page.locator(".req-row", has_text="Get item").click()
    page.fill("#reqName", "Edited in stale tab")
    page.wait_for_selector(".req-row:has-text('Added elsewhere')", timeout=5000)
    assert messages and "changed in another" in messages[0]
    names = [r["name"] for r in store.load_workspace()["collections"][0]["requests"]]
    assert "Added elsewhere" in names


def test_edit_survives_closing_the_tab_right_away(browser, app):
    store, url = app
    page = open_page(browser, url)
    page.locator(".req-row", has_text="Get item").click()
    page.fill("#reqName", "Saved on close")
    page.close(run_before_unload=True)
    for _ in range(50):
        if any(r["name"] == "Saved on close" for r in store.load_workspace()["collections"][0]["requests"]):
            break
        time.sleep(0.1)
    else:
        pytest.fail("edit was lost on close")
