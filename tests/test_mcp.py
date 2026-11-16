import json
import subprocess
import sys

import pytest

from restdesk.mcp import Server, Tools
from restdesk.storage import Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "rd.db")
    yield s
    s.close()


def call(server, name, args=None, mid=1):
    reply = server.handle({"jsonrpc": "2.0", "id": mid, "method": "tools/call",
                           "params": {"name": name, "arguments": args or {}}})
    return reply["result"]


def ok(server, name, args=None):
    res = call(server, name, args)
    assert not res.get("isError"), res
    return res["structuredContent"]


def test_initialize_and_list(store):
    srv = Server(store)
    init = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2025-06-18", "capabilities": {}}})["result"]
    assert init["protocolVersion"] == "2025-06-18"
    assert "tools" in init["capabilities"] and "{{@Login" in init["instructions"]
    assert srv.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    names = {t["name"] for t in srv.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]}
    assert {"workspace_overview", "save_request", "save_environment", "list_history"} <= names
    unknown = srv.handle({"jsonrpc": "2.0", "id": 3, "method": "nope"})
    assert unknown["error"]["code"] == -32601


def test_build_login_flow_on_empty_database(store):
    srv = Server(store)
    env = ok(srv, "save_environment", {"environment": "Local",
                                       "vars": {"baseUrl": "http://localhost:3000", "password": "hunter2"}})
    assert env["created"] and env["active"]
    login = ok(srv, "save_request", {"collection": "Auth flow", "request": {
        "name": "Login", "method": "post", "url": "{{baseUrl}}/login",
        "body": {"mode": "json", "text": {"user": "me", "password": "{{password}}"}},
        "scripts": {"post": "test('ok', () => expect(res.status).toBe(200));"}}})
    assert login["created"] and login["collection"]["name"] == "Auth flow"
    ok(srv, "save_request", {"collection": "Auth flow", "request": {
        "name": "Me", "url": "{{baseUrl}}/me", "headers": {"Accept": "application/json"},
        "auth": {"type": "bearer", "token": "{{@Login.body.access_token}}"}}})

    ws = store.load_workspace()
    (col,) = ws["collections"]
    assert [r["name"] for r in col["requests"]] == ["Login", "Me"]
    login_req, me = col["requests"]
    assert login_req["method"] == "POST"
    assert json.loads(login_req["body"]["text"]) == {"user": "me", "password": "{{password}}"}
    # defaults the UI expects are filled in
    assert me["settings"]["timeoutMs"] == 30000 and me["scripts"] == {"pre": "", "post": ""}
    assert me["headers"] == [{"key": "Accept", "value": "application/json", "enabled": True}]
    assert ws["activeEnvId"] == ws["environments"][0]["id"]

    overview = ok(srv, "workspace_overview")
    assert overview["environments"][0]["vars"] == {"baseUrl": "http://localhost:3000", "password": "[redacted]"}
    assert ok(srv, "get_environment", {"environment": "local", "revealSecrets": True})["vars"][1]["value"] == "hunter2"


def test_update_merges_and_moves(store):
    srv = Server(store)
    rid = ok(srv, "save_request", {"collection": "A", "request": {
        "name": "Create", "method": "POST", "url": "/x",
        "body": {"mode": "json", "text": "{}"}, "auth": {"type": "basic", "username": "u", "password": "p"}}})["request"]["id"]
    ok(srv, "save_request", {"request": {"id": rid, "url": "/y", "auth": {"password": "q"}, "body": {"text": "[]"}}})
    req = ok(srv, "get_request", {"request": "create"})["request"]
    assert req["url"] == "/y" and req["method"] == "POST"
    assert req["auth"] == {"type": "basic", "username": "u", "password": "q"}
    assert req["body"]["mode"] == "json" and req["body"]["text"] == "[]"
    # changing auth type replaces the auth object instead of mixing fields
    ok(srv, "save_request", {"request": {"id": rid, "auth": {"type": "bearer", "token": "t"}}})
    assert ok(srv, "get_request", {"request": rid})["request"]["auth"] == {"type": "bearer", "token": "t"}

    ok(srv, "save_request", {"collection": "B", "request": {"id": rid}, "position": 0})
    cols = {c["name"]: [r["id"] for r in c["requests"]] for c in store.load_workspace()["collections"]}
    assert cols == {"A": [], "B": [rid]}


def test_validation_errors_are_reported(store):
    srv = Server(store)
    assert call(srv, "save_request", {"collection": "A", "request": {"method": "FETCH"}})["isError"]
    assert call(srv, "save_request", {"request": {"name": "no collection"}})["isError"]
    assert call(srv, "save_request", {"request": {"id": "missing"}})["isError"]
    assert call(srv, "get_request", {})["isError"]  # missing argument
    assert call(srv, "nope")["isError"]
    assert store.load_workspace().get("collections") is None  # nothing was written


def test_delete_goes_to_trash(store):
    srv = Server(store)
    ok(srv, "save_request", {"collection": "A", "request": {"name": "One"}})
    ok(srv, "save_request", {"collection": "B", "request": {"name": "Two"}})
    ok(srv, "delete_request", {"request": "One"})
    ok(srv, "delete_collection", {"collection": "B"})
    trash = {t["name"]: t["kind"] for t in store.list_trash()}
    assert trash == {"One": "request", "B": "collection"}


def test_masked_value_never_overwrites_secret(store):
    srv = Server(store)
    ok(srv, "save_environment", {"environment": "Dev", "vars": {"apiToken": "real"}})
    out = ok(srv, "save_environment", {"environment": "Dev", "vars": {"apiToken": "[redacted]", "x": "1"}})
    assert out["skipped"]["keys"] == ["apiToken"]
    ok(srv, "save_environment", {"environment": "Dev", "vars": {"x": None}})
    vars_ = store.load_workspace()["environments"][0]["vars"]
    assert vars_ == [{"key": "apiToken", "value": "real", "enabled": True}]


def test_retries_on_concurrent_ui_save(store, monkeypatch):
    """If the UI saves between our load and save, the change is applied on top of it."""
    store.save_workspace({"collections": [{"id": "c1", "name": "UI", "requests": []}], "environments": []})
    tools = Tools(store)
    real_load, raced = tools._load, []

    def racing_load():
        ws, rev = real_load()
        if not raced:  # the UI sneaks in a save right after our first load
            raced.append(1)
            store.save_workspace({"collections": [{"id": "c1", "name": "UI renamed", "requests": []}],
                                  "environments": []})
        return ws, rev

    monkeypatch.setattr(tools, "_load", racing_load)
    tools.save_request({"collection": "c1", "request": {"name": "From agent"}})
    (col,) = store.load_workspace()["collections"]
    assert col["name"] == "UI renamed" and [r["name"] for r in col["requests"]] == ["From agent"]


def test_history_tools(store):
    srv = Server(store)
    ok(srv, "save_request", {"collection": "A", "request": {"name": "Ping"}})
    rid = store.load_workspace()["collections"][0]["requests"][0]["id"]
    store.add_history({"snapshot": {"id": rid, "name": "Ping", "method": "GET", "url": "http://x"},
                       "result": {"final": {"method": "GET", "url": "http://x"},
                                  "response": {"ok": True, "status": 401, "body": "x" * 30000, "headers": []},
                                  "tests": [], "logs": []}})
    listed = ok(srv, "list_history", {"request": "Ping"})
    assert listed["total"] == 1 and listed["items"][0]["status"] == "401"
    entry = ok(srv, "get_history_entry", {"id": listed["items"][0]["id"]})
    assert len(entry["result"]["response"]["body"]) == 20000 and "clippedForAgent" in entry["result"]["response"]


def test_stdio_round_trip(tmp_path):
    db = tmp_path / "rd.db"
    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2099-01-01"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "save_request", "arguments": {"collection": "C", "request": {"name": "R"}}}},
    ]
    proc = subprocess.run([sys.executable, "-m", "restdesk", "mcp", "--data", str(db)],
                          input="\n".join(json.dumps(x) for x in lines) + "\nnot json\n",
                          capture_output=True, text=True, timeout=30)
    replies = [json.loads(x) for x in proc.stdout.splitlines()]
    assert [r.get("id") for r in replies] == [1, 2, None]
    assert replies[0]["result"]["protocolVersion"] == "2025-11-25"
    assert replies[1]["result"]["structuredContent"]["created"] is True
    assert replies[2]["error"]["code"] == -32700
    s = Store(db)
    assert s.load_workspace()["collections"][0]["requests"][0]["name"] == "R"
    s.close()
