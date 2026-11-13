"""0.3 hardening: migrations, save conflicts, trash, history redaction, binary bodies, headers."""
import json
import sqlite3
import threading
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from restdesk import storage
from restdesk.redact import MASK, is_sensitive, mask_url, redact_result, redact_snapshot
from restdesk.server import PAGE_CSP, SANDBOX_CSP, make_handler
from restdesk.storage import Conflict, SchemaTooNew, Store


def ws(*reqs, cols=("c1",)):
    return {"collections": [{"id": c, "name": c.upper(), "requests": [dict(r) for r in reqs] if i == 0 else []}
                            for i, c in enumerate(cols)], "environments": []}


R1 = {"id": "r1", "name": "One", "method": "GET", "url": "http://x/1"}
R2 = {"id": "r2", "name": "Two", "method": "POST", "url": "http://x/2"}


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "rd.db")
    yield s
    s.close()


# ---------------------------------------------------------------- migrations
def test_v1_database_is_upgraded_in_place(tmp_path):
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.executescript(storage.SCHEMA)
    db.execute("INSERT INTO meta VALUES ('schemaVersion', '1'), ('initialized', 'true')")
    db.execute("INSERT INTO collections VALUES ('c1', 0, 'Old', 0, '{}', 0)")
    db.commit()
    db.close()
    s = Store(path)
    assert s.schema_version == storage.SCHEMA_VERSION
    assert s.load_workspace()["collections"][0]["name"] == "Old"
    assert s.list_trash() == []
    assert s.revision() == 0


def test_newer_schema_is_refused(tmp_path):
    path = tmp_path / "new.db"
    Store(path).close()
    db = sqlite3.connect(path)
    db.execute("UPDATE meta SET value='999' WHERE key='schemaVersion'")
    db.commit()
    db.close()
    with pytest.raises(SchemaTooNew):
        Store(path)


# ---------------------------------------------------------------- conflicts
def test_stale_save_is_refused(store):
    assert store.save_workspace(ws(R1), base_revision=0) == 1
    assert store.save_workspace(ws(R1, R2), base_revision=1) == 2
    with pytest.raises(Conflict) as exc:  # a tab still on revision 1 would drop r2
        store.save_workspace(ws(R1), base_revision=1)
    assert exc.value.revision == 2
    assert [r["id"] for r in store.load_workspace()["collections"][0]["requests"]] == ["r1", "r2"]
    assert store.save_workspace(ws(R1)) == 3  # no base revision = explicit overwrite


# ---------------------------------------------------------------- trash
def test_deleted_request_goes_to_trash_and_restores(store):
    store.save_workspace(ws(R1, R2))
    store.save_workspace(ws(R1))
    items = store.list_trash()
    assert [(i["id"], i["kind"], i["collectionName"]) for i in items] == [("r2", "request", "C1")]
    item = store.take_from_trash("r2")
    assert item["data"]["url"] == "http://x/2" and store.list_trash() == []
    assert store.take_from_trash("r2") is None


def test_deleted_collection_is_trashed_whole_and_moves_are_not_deletes(store):
    store.save_workspace(ws(R1, R2, cols=("c1", "c2")))
    moved = {"collections": [{"id": "c2", "name": "C2", "requests": [R2]}], "environments": []}
    store.save_workspace(moved)  # c1 deleted, r2 moved to c2 first
    items = store.list_trash()
    assert len(items) == 1 and items[0]["kind"] == "collection" and items[0]["requestCount"] == 1
    assert store.take_from_trash("c1")["data"]["requests"][0]["id"] == "r1"


def test_undo_after_autosave_clears_trash_entry(store):
    store.save_workspace(ws(R1, R2))
    store.save_workspace(ws(R1))
    store.save_workspace(ws(R1, R2))
    assert store.list_trash() == []


def test_trash_expires(store, monkeypatch):
    store.save_workspace(ws(R1, R2))
    store.save_workspace(ws(R1))
    monkeypatch.setattr(storage, "_now", lambda: 10**12)
    store.save_workspace(ws(R1))
    assert store.list_trash() == []


# ---------------------------------------------------------------- redaction
@pytest.mark.parametrize("name", ["Authorization", "Cookie", "X-Api-Key", "api_key", "password",
                                  "access_token", "client_secret", "Set-Cookie", "key"])
def test_sensitive_names(name):
    assert is_sensitive(name)


@pytest.mark.parametrize("name", ["Content-Type", "author", "user", "id", "Accept", "title"])
def test_non_sensitive_names(name):
    assert not is_sensitive(name)


def test_redact_result_masks_headers_query_bodies_and_response():
    out = redact_result({
        "final": {"url": "https://x/a?page=2&api_key=abc", "body": "user=bob&password=hunter2",
                  "headers": [["Authorization", "Bearer abc"], ["Content-Type", "application/x-www-form-urlencoded"]]},
        "response": {"ok": True, "status": 200, "headers": [["Set-Cookie", "sid=1"], ["Content-Type", "application/json"]],
                     "body": json.dumps({"access_token": "tok", "user": {"name": "bob", "password": "p"}})},
    })
    assert out["final"]["url"] == f"https://x/a?page=2&api_key={urllib.parse.quote(MASK)}"
    assert "hunter2" not in out["final"]["body"] and "user=bob" in out["final"]["body"]
    assert out["final"]["headers"][0] == ["Authorization", MASK]
    assert out["response"]["headers"][0] == ["Set-Cookie", MASK]
    body = json.loads(out["response"]["body"])
    assert body == {"access_token": MASK, "user": {"name": "bob", "password": MASK}}


def test_redact_snapshot_keeps_templates():
    snap = redact_snapshot({
        "headers": [{"key": "X-Api-Key", "value": "{{apiKey}}"}, {"key": "X-Token", "value": "raw"}],
        "auth": {"type": "bearer", "token": "{{token}}"},
        "body": {"mode": "json", "text": '{"password": "p", "name": "n"}', "form": []},
    })
    assert snap["headers"][0]["value"] == "{{apiKey}}" and snap["headers"][1]["value"] == MASK
    assert snap["auth"]["token"] == "{{token}}"
    assert json.loads(snap["body"]["text"]) == {"password": MASK, "name": "n"}
    assert redact_snapshot({"auth": {"type": "basic", "password": "pw"}})["auth"]["password"] == MASK


def test_history_is_redacted_by_default(store):
    row = store.add_history({"snapshot": {"auth": {"type": "bearer", "token": "secret-literal"}},
                             "result": {"final": {"headers": [["Authorization", "Bearer secret-literal"]]},
                                        "response": {"ok": True, "status": 200, "body": "{}", "headers": []}}})
    stored = json.dumps(store.get_history(row["id"]))
    assert "secret-literal" not in stored
    assert store.get_history(row["id"])["result"]["redacted"] is True


def test_redaction_can_be_disabled(tmp_path):
    s = Store(tmp_path / "rd.db", redact_history=False)
    row = s.add_history({"snapshot": {}, "result": {"final": {"headers": [["Authorization", "Bearer keep"]]}}})
    assert "Bearer keep" in json.dumps(s.get_history(row["id"]))


# ---------------------------------------------------------------- binary bodies
def test_large_binary_body_is_dropped_not_corrupted(tmp_path):
    s = Store(tmp_path / "rd.db", max_stored_body=10)
    row = s.add_history({"snapshot": {}, "result": {"response": {
        "ok": True, "status": 200, "bodyEncoding": "base64", "body": "QUFB" * 10, "size": 30}}})
    resp = s.get_history(row["id"])["result"]["response"]
    assert resp["body"] == "" and resp["bodyNotStored"] is True and resp["size"] == 30


# ---------------------------------------------------------------- HTTP layer
def test_http_conflict_trash_and_security_headers(store):
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(store))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def call(method, path, body=None):
        req = urllib.request.Request(base + path, method=method,
                                     data=None if body is None else json.dumps(body).encode())
        req.add_header("X-RestDesk", "1")
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read() or b"null"), r.headers
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null"), e.headers

    try:
        assert call("PUT", "/api/workspace", {**ws(R1, R2), "baseRevision": 0})[1]["revision"] == 1
        code, body, _ = call("PUT", "/api/workspace", {**ws(R1), "baseRevision": 0})
        assert code == 409 and body["revision"] == 1
        assert call("PUT", "/api/workspace", {**ws(R1), "baseRevision": 1})[0] == 200
        assert [i["id"] for i in call("GET", "/api/trash")[1]["items"]] == ["r2"]
        assert call("POST", "/api/trash/r2/restore", {})[1]["data"]["name"] == "Two"
        assert call("POST", "/api/trash/r2/restore", {})[0] == 404
        assert call("DELETE", "/api/trash")[0] == 200

        with urllib.request.urlopen(base + "/") as r:
            assert r.headers["Content-Security-Policy"] == PAGE_CSP
            assert r.headers["X-Frame-Options"] == "DENY"
        with urllib.request.urlopen(base + "/sandbox-worker.js") as r:
            assert r.headers["Content-Security-Policy"] == SANDBOX_CSP
            assert "connect" not in SANDBOX_CSP and "default-src 'none'" in SANDBOX_CSP
    finally:
        server.shutdown()
        server.server_close()


def test_redacted_flag_only_when_something_was_masked(store):
    row = store.add_history({"snapshot": {"url": "http://x"}, "result": {
        "final": {"url": "http://x", "headers": [["Accept", "*/*"]]},
        "response": {"ok": True, "status": 200, "body": '{"a": 1}', "headers": []}}})
    assert "redacted" not in store.get_history(row["id"])["result"]
