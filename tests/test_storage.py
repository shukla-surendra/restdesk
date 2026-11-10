import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from restdesk.server import make_handler
from restdesk.storage import Store


def workspace():
    return {
        "version": 1, "activeEnvId": "e1", "lastRequestId": "r2",
        "environments": [{"id": "e1", "name": "Dev", "vars": [{"key": "base", "value": "http://x", "enabled": True}]}],
        "collections": [
            {"id": "c1", "name": "Users", "collapsed": False, "requests": [
                {"id": "r1", "name": "List", "method": "GET", "url": "{{base}}/users"},
                {"id": "r2", "name": "Create", "method": "POST", "url": "{{base}}/users"},
            ]},
            {"id": "c2", "name": "Misc", "collapsed": True, "requests": []},
        ],
    }


def result(status=200, body="{}", tests=()):
    return {"final": {"method": "GET", "url": "http://x/users"},
            "response": {"ok": True, "status": status, "body": body, "timeMs": 12.5, "size": len(body), "headers": []},
            "tests": list(tests), "logs": [], "error": None}


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "rd.db")
    yield s
    s.close()


def test_new_db_is_empty(store):
    assert store.is_empty()
    assert store.load_workspace() == {}


def test_workspace_roundtrip_and_survives_reopen(tmp_path):
    s = Store(tmp_path / "rd.db")
    s.save_workspace(workspace())
    s.close()
    s = Store(tmp_path / "rd.db")
    ws = s.load_workspace()
    assert [c["name"] for c in ws["collections"]] == ["Users", "Misc"]
    assert [r["id"] for r in ws["collections"][0]["requests"]] == ["r1", "r2"]
    assert ws["collections"][1]["collapsed"] is True
    assert ws["activeEnvId"] == "e1" and ws["lastRequestId"] == "r2"
    assert ws["environments"][0]["vars"][0]["key"] == "base"


def test_save_reorders_moves_and_deletes(store):
    store.save_workspace(workspace())
    ws = workspace()
    moved = ws["collections"][0]["requests"].pop(0)
    ws["collections"][1]["requests"].append(moved)
    ws["collections"].reverse()
    ws["environments"] = []
    store.save_workspace(ws)
    out = store.load_workspace()
    assert [c["id"] for c in out["collections"]] == ["c2", "c1"]
    assert [r["id"] for r in out["collections"][0]["requests"]] == ["r1"]
    assert out["environments"] == []
    ws["collections"] = [ws["collections"][1]]  # drop c2 with r1 inside
    store.save_workspace(ws)
    out = store.load_workspace()
    assert [c["id"] for c in out["collections"]] == ["c1"]
    assert [r["id"] for r in out["collections"][0]["requests"]] == ["r2"]


def test_save_rejects_garbage(store):
    with pytest.raises(ValueError):
        store.save_workspace({"collections": "nope"})


def test_history_add_list_get_search_delete(store):
    snap = workspace()["collections"][0]["requests"][0]
    a = store.add_history({"ts": 1000, "snapshot": snap, "result": result(200, tests=[{"name": "t", "pass": True}])})
    store.add_history({"ts": 2000, "snapshot": snap,
                       "result": {"final": {"method": "GET", "url": "http://x/fail"}, "response": {"ok": False}, "tests": []}})
    page = store.list_history()
    assert page["total"] == 2
    assert [h["status"] for h in page["items"]] == ["ERR", "200"]
    assert page["items"][1]["testsPass"] == 1
    assert store.list_history(q="fail")["total"] == 1
    assert store.list_history(request_id="r1")["total"] == 2
    full = store.get_history(a["id"])
    assert full["snapshot"]["name"] == "List" and full["result"]["response"]["body"] == "{}"
    assert store.delete_history(a["id"]) == 1
    assert store.delete_history() == 1
    assert store.list_history()["total"] == 0


def test_history_limit_and_body_clipping(tmp_path):
    s = Store(tmp_path / "rd.db", history_limit=3, max_stored_body=10)
    for i in range(5):
        s.add_history({"ts": i, "snapshot": {}, "result": result(body="x" * 50)})
    items = s.list_history()["items"]
    assert [h["ts"] for h in items] == [4, 3, 2]
    resp = s.get_history(items[0]["id"])["result"]["response"]
    assert resp["body"] == "x" * 10 and resp["storedTruncated"] is True


def test_legacy_json_import(tmp_path, store):
    legacy = workspace()
    legacy["history"] = [{"id": "h1", "ts": 5, "method": "GET", "url": "http://x", "status": 404,
                          "snapshot": {"id": "r1", "name": "List"}}]
    path = tmp_path / "workspace.json"
    path.write_text(json.dumps(legacy))
    assert store.import_legacy_json(path)
    assert len(store.load_workspace()["collections"]) == 2
    assert store.list_history()["items"][0]["status"] == "404"


def test_http_api(store):
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(store))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def call(method, path, body=None, header=True):
        req = urllib.request.Request(base + path, method=method,
                                     data=None if body is None else json.dumps(body).encode())
        if header:
            req.add_header("X-RestDesk", "1")
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, None

    try:
        assert call("GET", "/api/workspace", header=False)[0] == 403
        assert call("PUT", "/api/workspace", workspace()) == (200, {"saved": True})
        assert call("GET", "/api/workspace")[1]["collections"][0]["name"] == "Users"
        code, row = call("POST", "/api/history", {"snapshot": {"id": "r1"}, "result": result()})
        assert code == 200 and row["status"] == "200"
        assert call("GET", "/api/history?q=users")[1]["total"] == 1
        assert call("GET", f"/api/history/{row['id']}")[1]["result"]["response"]["status"] == 200
        assert len(call("GET", "/api/export")[1]["callHistory"]) == 1
        assert "callHistory" not in call("GET", "/api/export?history=0")[1]
        assert call("DELETE", "/api/history")[1] == {"deleted": 1}
        assert call("GET", "/api/history/nope")[0] == 404
    finally:
        server.shutdown()
        server.server_close()
