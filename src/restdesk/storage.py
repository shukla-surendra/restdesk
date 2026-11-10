"""SQLite persistence for RestDesk.

Everything the UI creates lives in one SQLite file:

- ``collections`` / ``requests``  - your saved API list, in order
- ``environments``                - variable sets
- ``meta``                        - small key/value settings (active env, last open request)
- ``history``                     - every call you send, with the request snapshot, the
                                    resolved request, the response and test results

Only the standard library ``sqlite3`` module is used. Writes are serialised with a lock and
run inside transactions, so a crash never leaves a half-written workspace.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS collections (
    id         TEXT PRIMARY KEY,
    position   INTEGER NOT NULL,
    name       TEXT NOT NULL,
    collapsed  INTEGER NOT NULL DEFAULT 0,
    extra      TEXT NOT NULL DEFAULT '{}',
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS requests (
    id            TEXT PRIMARY KEY,
    collection_id TEXT NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
    position      INTEGER NOT NULL,
    name          TEXT NOT NULL,
    method        TEXT NOT NULL,
    url           TEXT NOT NULL,
    data          TEXT NOT NULL,
    updated_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS requests_by_collection ON requests(collection_id, position);
CREATE TABLE IF NOT EXISTS environments (
    id         TEXT PRIMARY KEY,
    position   INTEGER NOT NULL,
    name       TEXT NOT NULL,
    vars       TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS history (
    id          TEXT PRIMARY KEY,
    ts          INTEGER NOT NULL,
    source      TEXT NOT NULL DEFAULT 'send',
    request_id  TEXT,
    name        TEXT,
    method      TEXT NOT NULL,
    url         TEXT NOT NULL,
    status      TEXT NOT NULL,
    time_ms     REAL,
    size        INTEGER,
    tests_pass  INTEGER NOT NULL DEFAULT 0,
    tests_fail  INTEGER NOT NULL DEFAULT 0,
    snapshot    TEXT NOT NULL,
    result      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS history_by_ts ON history(ts DESC);
CREATE INDEX IF NOT EXISTS history_by_request ON history(request_id, ts DESC);
"""

# Columns returned by history listings (the heavy snapshot/result blobs are fetched on demand).
_HISTORY_SUMMARY = ("id, ts, source, request_id, name, method, url, status, time_ms, size, "
                    "tests_pass, tests_fail")


def _now() -> float:
    return time.time()


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


class Store:
    def __init__(self, path: Path, history_limit: int = 2000,
                 max_stored_body: int = 2 * 1024 * 1024) -> None:
        self.path = Path(path)
        self.history_limit = history_limit      # 0 = keep everything
        self.max_stored_body = max_stored_body  # response bodies larger than this are clipped
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(SCHEMA)
        self._set_meta_default("schemaVersion", SCHEMA_VERSION)

    # ------------------------------------------------------------------ helpers
    class _Tx:
        def __init__(self, store: "Store") -> None:
            self.store = store

        def __enter__(self) -> sqlite3.Connection:
            self.store._lock.acquire()
            self.store._db.execute("BEGIN IMMEDIATE")
            return self.store._db

        def __exit__(self, exc_type, exc, tb) -> None:
            try:
                self.store._db.execute("ROLLBACK" if exc_type else "COMMIT")
            finally:
                self.store._lock.release()

    def _tx(self) -> "Store._Tx":
        return Store._Tx(self)

    def _set_meta_default(self, key: str, value: Any) -> None:
        with self._tx() as db:
            db.execute("INSERT OR IGNORE INTO meta(key, value) VALUES (?, ?)", (key, _dumps(value)))

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def is_empty(self) -> bool:
        with self._lock:
            row = self._db.execute(
                "SELECT (SELECT COUNT(*) FROM collections) + (SELECT COUNT(*) FROM environments) "
                "+ (SELECT COUNT(*) FROM history)").fetchone()
        return row[0] == 0

    # ------------------------------------------------------------------ workspace
    def load_workspace(self) -> dict:
        """Return the workspace in the shape the UI uses (without history)."""
        with self._lock:
            db = self._db
            meta = {r["key"]: json.loads(r["value"]) for r in db.execute("SELECT key, value FROM meta")}
            if not meta.get("initialized"):
                return {}
            cols = []
            for c in db.execute("SELECT * FROM collections ORDER BY position"):
                col = {**json.loads(c["extra"] or "{}"), "id": c["id"], "name": c["name"],
                       "collapsed": bool(c["collapsed"]), "requests": []}
                for r in db.execute("SELECT data FROM requests WHERE collection_id=? ORDER BY position",
                                    (c["id"],)):
                    col["requests"].append(json.loads(r["data"]))
                cols.append(col)
            envs = [{"id": e["id"], "name": e["name"], "vars": json.loads(e["vars"])}
                    for e in db.execute("SELECT * FROM environments ORDER BY position")]
        return {
            "version": meta.get("workspaceVersion", 1),
            "activeEnvId": meta.get("activeEnvId"),
            "lastRequestId": meta.get("lastRequestId"),
            "environments": envs,
            "collections": cols,
        }

    def save_workspace(self, ws: dict) -> None:
        """Make the stored collections/requests/environments match ``ws`` exactly.

        Rows are upserted and only changed rows are rewritten; anything not present in
        ``ws`` is deleted. History is never touched here.
        """
        if not isinstance(ws, dict) or not isinstance(ws.get("collections"), list):
            raise ValueError("workspace must be an object with a 'collections' list")
        now = _now()
        with self._tx() as db:
            keep_cols: list[str] = []
            keep_reqs: list[str] = []
            for ci, col in enumerate(ws["collections"]):
                cid = str(col.get("id") or uuid.uuid4())
                keep_cols.append(cid)
                extra = {k: v for k, v in col.items() if k not in ("id", "name", "collapsed", "requests")}
                db.execute(
                    """INSERT INTO collections(id, position, name, collapsed, extra, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT(id) DO UPDATE SET position=excluded.position, name=excluded.name,
                         collapsed=excluded.collapsed, extra=excluded.extra, updated_at=excluded.updated_at
                       WHERE position IS NOT excluded.position OR name IS NOT excluded.name
                         OR collapsed IS NOT excluded.collapsed OR extra IS NOT excluded.extra""",
                    (cid, ci, str(col.get("name") or "Untitled"), int(bool(col.get("collapsed"))),
                     _dumps(extra), now))
                for ri, req in enumerate(col.get("requests") or []):
                    rid = str(req.get("id") or uuid.uuid4())
                    req = {**req, "id": rid}
                    keep_reqs.append(rid)
                    db.execute(
                        """INSERT INTO requests(id, collection_id, position, name, method, url, data, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT(id) DO UPDATE SET collection_id=excluded.collection_id,
                             position=excluded.position, name=excluded.name, method=excluded.method,
                             url=excluded.url, data=excluded.data, updated_at=excluded.updated_at
                           WHERE collection_id IS NOT excluded.collection_id
                             OR position IS NOT excluded.position OR data IS NOT excluded.data""",
                        (rid, cid, ri, str(req.get("name") or ""), str(req.get("method") or "GET"),
                         str(req.get("url") or ""), _dumps(req), now))
            self._delete_missing(db, "requests", keep_reqs)
            self._delete_missing(db, "collections", keep_cols)

            keep_envs: list[str] = []
            for ei, env in enumerate(ws.get("environments") or []):
                eid = str(env.get("id") or uuid.uuid4())
                keep_envs.append(eid)
                db.execute(
                    """INSERT INTO environments(id, position, name, vars, updated_at)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(id) DO UPDATE SET position=excluded.position, name=excluded.name,
                         vars=excluded.vars, updated_at=excluded.updated_at
                       WHERE position IS NOT excluded.position OR name IS NOT excluded.name
                         OR vars IS NOT excluded.vars""",
                    (eid, ei, str(env.get("name") or ""), _dumps(env.get("vars") or []), now))
            self._delete_missing(db, "environments", keep_envs)

            for key, value in (("activeEnvId", ws.get("activeEnvId")),
                               ("lastRequestId", ws.get("lastRequestId")),
                               ("workspaceVersion", ws.get("version", 1)),
                               ("initialized", True)):
                db.execute("INSERT INTO meta(key, value) VALUES (?, ?) "
                           "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, _dumps(value)))

    @staticmethod
    def _delete_missing(db: sqlite3.Connection, table: str, keep: Iterable[str]) -> None:
        keep = list(keep)
        db.execute("CREATE TEMP TABLE IF NOT EXISTS _keep(id TEXT PRIMARY KEY)")
        db.execute("DELETE FROM _keep")
        db.executemany("INSERT OR IGNORE INTO _keep(id) VALUES (?)", [(k,) for k in keep])
        db.execute(f"DELETE FROM {table} WHERE id NOT IN (SELECT id FROM _keep)")  # noqa: S608

    # ------------------------------------------------------------------ history
    def add_history(self, entry: dict) -> dict:
        """Store one sent call. Returns the stored summary row."""
        snapshot = entry.get("snapshot") or {}
        result = dict(entry.get("result") or {})
        response = dict(result.get("response") or {})
        body = response.get("body")
        if isinstance(body, str) and len(body) > self.max_stored_body:
            response["body"] = body[: self.max_stored_body]
            response["storedTruncated"] = True
        if response:
            result["response"] = response
        tests = result.get("tests") or []
        passed = sum(1 for t in tests if t.get("pass"))
        final = result.get("final") or {}
        ok = response.get("ok")
        row = {
            "id": str(entry.get("id") or uuid.uuid4()),
            "ts": int(entry["ts"]) if entry.get("ts") is not None else int(_now() * 1000),
            "source": str(entry.get("source") or "send"),
            "request_id": snapshot.get("id"),
            "name": snapshot.get("name"),
            "method": str(final.get("method") or snapshot.get("method") or "GET"),
            "url": str(final.get("url") or snapshot.get("url") or ""),
            "status": str(response.get("status")) if ok else "ERR",
            "time_ms": response.get("timeMs"),
            "size": response.get("size"),
            "tests_pass": passed,
            "tests_fail": len(tests) - passed,
            "snapshot": _dumps(snapshot),
            "result": _dumps(result),
        }
        with self._tx() as db:
            db.execute(f"INSERT OR REPLACE INTO history({', '.join(row)}) "
                       f"VALUES ({', '.join('?' * len(row))})", tuple(row.values()))
            if self.history_limit > 0:
                db.execute("DELETE FROM history WHERE id IN (SELECT id FROM history "
                           "ORDER BY ts DESC LIMIT -1 OFFSET ?)", (self.history_limit,))
        return self._summary(row)

    @staticmethod
    def _summary(row: Any) -> dict:
        keys = [k.strip() for k in _HISTORY_SUMMARY.split(",")]
        d = {k: row[k] for k in keys}
        return {"id": d["id"], "ts": d["ts"], "source": d["source"], "requestId": d["request_id"],
                "name": d["name"], "method": d["method"], "url": d["url"], "status": d["status"],
                "timeMs": d["time_ms"], "size": d["size"], "testsPass": d["tests_pass"],
                "testsFail": d["tests_fail"]}

    def list_history(self, limit: int = 100, offset: int = 0, q: str = "",
                     request_id: str | None = None) -> dict:
        where, args = [], []
        if q:
            where.append("(url LIKE ? OR name LIKE ? OR method LIKE ? OR status LIKE ?)")
            like = f"%{q}%"
            args += [like, like, like, like]
        if request_id:
            where.append("request_id = ?")
            args.append(request_id)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        with self._lock:
            total = self._db.execute(f"SELECT COUNT(*) FROM history {clause}", args).fetchone()[0]
            rows = self._db.execute(
                f"SELECT {_HISTORY_SUMMARY} FROM history {clause} ORDER BY ts DESC LIMIT ? OFFSET ?",
                [*args, max(1, min(int(limit), 1000)), max(0, int(offset))]).fetchall()
        return {"total": total, "items": [self._summary(r) for r in rows]}

    def get_history(self, hid: str) -> dict | None:
        with self._lock:
            row = self._db.execute(f"SELECT {_HISTORY_SUMMARY}, snapshot, result FROM history WHERE id=?",
                                   (hid,)).fetchone()
        if not row:
            return None
        return {**self._summary(row), "snapshot": json.loads(row["snapshot"]),
                "result": json.loads(row["result"])}

    def delete_history(self, hid: str | None = None) -> int:
        with self._tx() as db:
            cur = (db.execute("DELETE FROM history WHERE id=?", (hid,)) if hid
                   else db.execute("DELETE FROM history"))
            return cur.rowcount

    # ------------------------------------------------------------------ import / export
    def export_all(self, include_history: bool = True) -> dict:
        ws = self.load_workspace() or {"version": 1, "collections": [], "environments": []}
        if include_history:
            with self._lock:
                ids = [r[0] for r in self._db.execute("SELECT id FROM history ORDER BY ts DESC")]
            ws["callHistory"] = [self.get_history(i) for i in ids]
        return ws

    def import_legacy_json(self, path: Path) -> bool:
        """Import a RestDesk 0.1 ``workspace.json`` (including its short history list)."""
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if not isinstance(data, dict) or not isinstance(data.get("collections"), list):
            return False
        old_history = data.pop("history", None) or []
        self.save_workspace(data)
        for h in reversed(old_history):
            if not isinstance(h, dict):
                continue
            status = h.get("status")
            self.add_history({
                "id": h.get("id"), "ts": h.get("ts"), "source": "import",
                "snapshot": h.get("snapshot") or {},
                "result": {"final": {"method": h.get("method"), "url": h.get("url")},
                           "response": ({"ok": True, "status": status} if status != "ERR"
                                        else {"ok": False}),
                           "tests": [], "logs": []},
            })
        return True
