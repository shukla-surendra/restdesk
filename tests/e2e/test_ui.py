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
        if self.path.startswith("/echo"):
            return self._json(200, {"headers": {k.lower(): v for k, v in self.headers.items()}})
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
    page.route("**/api/revision", lambda route: route.abort())  # the tab can't notice the change first
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


def test_changes_from_an_agent_show_up_in_an_open_window(browser, app):
    from restdesk.mcp import Tools
    store, url = app
    page = open_page(browser, url)
    Tools(store).save_request({"collection": "Suite", "request": {"name": "Added by agent", "url": "http://x"}})
    page.wait_for_selector(".req-row:has-text('Added by agent')", timeout=6000)
    expect(page.locator("#toast")).to_contain_text("updated outside")
    # the tab is in sync again, so its next edit saves without a conflict prompt
    messages = []
    page.on("dialog", lambda d: (messages.append(d.message), d.dismiss()))
    page.locator(".req-row", has_text="Get item").click()
    page.fill("#reqName", "Edited after agent")
    deadline = time.time() + 5
    while True:
        names = [r["name"] for r in store.load_workspace()["collections"][0]["requests"]]
        if "Edited after agent" in names or time.time() > deadline:
            break
        time.sleep(0.1)
    assert "Edited after agent" in names and "Added by agent" in names and not messages
    assert not page.errors


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


def test_response_survives_switching_requests_and_reload(browser, app):
    _, url = app
    page = open_page(browser, url)
    send(page, "Get item")
    page.locator(".req-row", has_text="Login").click()
    assert "Send a request" in page.locator("#statusLine").inner_text()
    page.locator(".req-row", has_text="Get item").click()
    assert page.locator("#statusLine .pill").inner_text().startswith("200")
    assert page.locator("#testCount").inner_text() == "2/2"
    # after a reload the last call comes back from history
    page.wait_for_selector(".hist-row")
    page.reload()
    page.locator(".req-row", has_text="Get item").click()
    expect(page.locator("#statusLine .pill")).to_have_text("200 OK")
    assert "last call" in page.locator("#statusLine").inner_text()


def tree_names(page):
    return [t.strip() for t in page.locator(".req-row .row-name").all_inner_texts()]


def test_drag_to_reorder_requests_is_saved(browser, app):
    store, url = app
    page = open_page(browser, url)
    page.locator(".req-row", has_text="Spin").drag_to(
        page.locator(".req-row", has_text="Get item"), target_position={"x": 20, "y": 2})
    assert tree_names(page) == ["Spin", "Get item", "Login", "Sneaky"]
    expect(page.locator("#saveState")).to_have_text("saved")
    assert [r["name"] for r in store.load_workspace()["collections"][0]["requests"]] == \
        ["Spin", "Get item", "Login", "Sneaky"]
    # keyboard: Alt+Shift+Down moves the selected request down
    page.locator(".req-row", has_text="Spin").click()
    page.keyboard.press("Alt+Shift+ArrowDown")
    assert tree_names(page) == ["Get item", "Spin", "Login", "Sneaky"]


def test_drag_request_into_another_collection(browser, app):
    store, url = app
    page = open_page(browser, url)
    page.click("#btnNewCol")  # creates "Other" with one new request
    page.fill("#askInput", "Other")
    page.click("#askOk")
    page.locator(".req-row", has_text="Login").drag_to(page.locator(".col-row", has_text="Other"))
    expect(page.locator("#saveState")).to_have_text("saved")
    cols = {c["name"]: [r["name"] for r in c["requests"]] for c in store.load_workspace()["collections"]}
    assert "Login" not in cols["Suite"] and cols["Other"][-1] == "Login"


def add_request(store, request, env_vars=()):
    ws = store.load_workspace()
    ws["collections"][0]["requests"].append(request)
    ws["environments"][0]["vars"] += [{"key": k, "value": v, "enabled": True} for k, v in env_vars]
    store.save_workspace(ws, base_revision=ws.pop("revision"))


def echoed(page):
    return json.loads(page.locator("#resBody").inner_text())["headers"]


def test_response_references_with_fallbacks(browser, app):
    store, url = app
    r = req("r5", "Use token", "{{base}}/echo")
    r["headers"] = [
        {"key": "X-Token", "value": "{{@Login.body.access_token ?? fallbackTok}}", "enabled": True},
        {"key": "X-Reverse", "value": "{{missingVar ?? @Login.body.user}}", "enabled": True},
        {"key": "X-Status", "value": "{{@Login.status}}", "enabled": True},
        {"key": "X-Ctype", "value": '{{@"Login".header.content-type}}', "enabled": True},
        {"key": "X-Default", "value": "{{@Nope.body.x ?? 'dflt'}}", "enabled": True},
    ]
    add_request(store, r, [("fallbackTok", "env-tok")])
    page = open_page(browser, url)

    # Login never sent: env fallback / literal default; unresolvable ones are reported
    send(page, "Use token")
    h = echoed(page)
    assert h["x-token"] == "env-tok" and h["x-default"] == "dflt"
    assert h["x-status"] == "{{@Login.status}}"
    assert "Not resolved" in page.locator("#toast").inner_text()

    # after Login: values come from its response
    send(page, "Login")
    send(page, "Use token")
    h = echoed(page)
    assert h["x-token"] == "tok-SECRET-123"
    assert h["x-reverse"] == "bob" and h["x-status"] == "200" and h["x-ctype"] == "application/json"

    # after a reload, Login's stored response has the token redacted, so the env fallback wins,
    # while non-secret fields still come from history
    expect(page.locator(".hist-row")).to_have_count(3)
    page.reload()
    page.wait_for_selector(".req-row")
    send(page, "Use token")
    h = echoed(page)
    assert h["x-token"] == "env-tok" and h["x-reverse"] == "bob"


def test_export_and_import_project(browser, app, tmp_path):
    store, url = app
    page = open_page(browser, url)
    page.click("#btnExport")
    with page.expect_download() as dl:
        page.click("#btnDoExport")
    path = tmp_path / "project.json"
    dl.value.save_as(path)
    data = json.loads(path.read_text())
    assert data["format"] == "restdesk-project"
    assert [c["name"] for c in data["collections"]] == ["Suite"]
    assert data["environments"][0]["vars"][0]["key"] == "base"
    assert "callHistory" not in data

    # change things, then merge the file back: no duplicates, variables restored, local ones kept
    ws = store.load_workspace()
    ws["environments"][0]["vars"][0]["value"] = "changed"
    ws["environments"][0]["vars"].append({"key": "local", "value": "keep", "enabled": True})
    store.save_workspace(ws, base_revision=ws.pop("revision"))
    page.reload()
    page.wait_for_selector(".req-row")
    page.set_input_files("#fileImport", str(path))
    expect(page.locator("#impSummary")).to_contain_text("Suite")
    page.click("#btnDoImport")
    expect(page.locator("#saveState")).to_have_text("saved")
    ws = store.load_workspace()
    ids = [r["id"] for c in ws["collections"] for r in c["requests"]]
    assert len(ws["collections"]) == 1 and len(ids) == len(set(ids)) == 4
    env_vars = {v["key"]: v["value"] for v in ws["environments"][0]["vars"]}
    assert env_vars["base"] != "changed" and env_vars["local"] == "keep"

    # replace mode swaps the whole project
    page.click("#btnNewCol")
    page.fill("#askInput", "Scratch")
    page.click("#askOk")
    page.set_input_files("#fileImport", str(path))
    page.check("#importDialog input[value=replace]")
    page.click("#btnDoImport")
    expect(page.locator("#saveState")).to_have_text("saved")
    assert [c["name"] for c in store.load_workspace()["collections"]] == ["Suite"]
