"""MCP server: lets a coding agent (Claude Code, Cursor, VS Code, Codex, ...) edit RestDesk.

Run:  restdesk mcp [--data PATH]

It speaks the Model Context Protocol over stdio (newline-delimited JSON-RPC 2.0) and works
directly on the same SQLite database as the UI, so the RestDesk server doesn't need to be
running. Every change goes through :meth:`Store.save_workspace` with the revision it loaded,
so deletions land in the trash and a concurrent save from the UI is never overwritten (the
change is re-applied on top of it instead). An open RestDesk window picks the change up within
a couple of seconds.

The agent doesn't send requests: it sets up collections, requests and environments, and the
person clicks Send or runs the collection. It can read call history to see what happened.

Only the Python standard library is used.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .redact import MASK, is_sensitive
from .storage import Conflict, SchemaTooNew, Store

SUPPORTED_PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
BODY_MODES = ("none", "json", "text", "xml", "urlencoded", "multipart")
AUTH_TYPES = ("none", "bearer", "basic", "apikey")
MAX_BODY_CHARS = 20_000  # response bodies returned to the agent are clipped to this

INSTRUCTIONS = """\
RestDesk is a local REST client (like Postman/Insomnia). These tools edit the user's saved
collections, requests and environments in place; the RestDesk window updates on its own.
You don't send requests. The user clicks Send or runs the collection. Use list_history and
get_history_entry to see calls they made.

Conventions that make requests work well in RestDesk:
- Put base URLs and secrets in an environment (save_environment) and reference them as
  {{baseUrl}}, {{apiKey}}. Don't hard-code tokens in requests.
- Chain a login flow with response references instead of copying tokens:
  a request named "Login" that returns {"access_token": ...} lets later requests use
  auth {"type": "bearer", "token": "{{@Login.body.access_token}}"}. Also .header.Name,
  .status, nested paths (.body.data.items[0].id), quoted names (@"Get user".body.id) and
  fallbacks ({{@Login.body.token ?? token}}).
- Built-ins: {{$uuid}} {{$timestamp}} {{$isoTimestamp}} {{$randomInt}}.
- Scripts are plain JS. scripts.pre runs before sending: env.set(k, v), req.setHeader(k, v).
  scripts.post runs after: res.status, res.json(), res.headers,
  test('name', () => expect(x).toBe(y)), env.set(...).
- Order requests in a collection the way they should run (login first): the collection
  runner goes top to bottom.
Call workspace_overview first to find existing collections and avoid duplicates."""


# --------------------------------------------------------------------------- helpers
class ToolError(Exception):
    """A problem the agent should see and can fix (bad id, invalid field...)."""


def _rows(value: Any, field: str) -> list:
    """Accept {k: v}, [[k, v]], or [{key, value, enabled}] and return the UI's row shape."""
    if value is None:
        return []
    if isinstance(value, dict):
        return [{"key": str(k), "value": "" if v is None else str(v), "enabled": True}
                for k, v in value.items()]
    if not isinstance(value, list):
        raise ToolError(f"{field} must be an object or a list")
    out = []
    for row in value:
        if isinstance(row, dict) and "key" in row:
            out.append({"key": str(row["key"]), "value": "" if row.get("value") is None else str(row["value"]),
                        "enabled": row.get("enabled", True) is not False})
        elif isinstance(row, (list, tuple)) and row:
            out.append({"key": str(row[0]), "value": str(row[1]) if len(row) > 1 else "", "enabled": True})
        else:
            raise ToolError(f"{field}: each row needs a key, got {row!r}")
    return out


def blank_request() -> dict:
    """Same defaults as newRequest() in the UI."""
    return {
        "id": str(uuid.uuid4()), "name": "New Request", "method": "GET", "url": "",
        "params": [], "headers": [],
        "body": {"mode": "none", "text": "", "form": []},
        "auth": {"type": "none"},
        "scripts": {"pre": "", "post": ""},
        "settings": {"timeoutMs": 30000, "followRedirects": True, "verifyTls": True},
    }


def apply_request_fields(req: dict, fields: dict) -> dict:
    """Merge agent-supplied fields into a request. Top-level fields replace; body, auth,
    scripts and settings are merged key by key so a partial update keeps the rest."""
    for key, value in fields.items():
        if key in ("id",):
            continue
        if key == "method":
            method = str(value).upper()
            if method not in METHODS:
                raise ToolError(f"method must be one of {', '.join(METHODS)}")
            req["method"] = method
        elif key in ("params", "headers"):
            req[key] = _rows(value, key)
        elif key == "body":
            if isinstance(value, str):  # shorthand: a JSON string body
                value = {"mode": "json", "text": value}
            if not isinstance(value, dict):
                raise ToolError("body must be an object like {mode, text, form}")
            body = {**req.get("body", {}), **value}
            if isinstance(body.get("text"), (dict, list)):
                body["text"] = json.dumps(body["text"], indent=2, ensure_ascii=False)
            if body.get("mode") not in BODY_MODES:
                raise ToolError(f"body.mode must be one of {', '.join(BODY_MODES)}")
            body["form"] = _rows(body.get("form"), "body.form")
            req["body"] = body
        elif key == "auth":
            if not isinstance(value, dict):
                raise ToolError("auth must be an object")
            auth = value if value.get("type") and value.get("type") != req.get("auth", {}).get("type") \
                else {**req.get("auth", {}), **value}
            if auth.get("type", "none") not in AUTH_TYPES:
                raise ToolError(f"auth.type must be one of {', '.join(AUTH_TYPES)}")
            req["auth"] = auth
        elif key in ("scripts", "settings"):
            if not isinstance(value, dict):
                raise ToolError(f"{key} must be an object")
            req[key] = {**req.get(key, {}), **value}
        elif key in ("name", "url"):
            req[key] = str(value)
        else:
            req[key] = value  # keep unknown fields; the UI ignores what it doesn't use
    return req


def _find_collection(ws: dict, ref: str | None) -> dict | None:
    if not ref:
        return None
    for col in ws["collections"]:
        if col.get("id") == ref:
            return col
    lc = str(ref).strip().lower()
    return next((c for c in ws["collections"] if str(c.get("name", "")).strip().lower() == lc), None)


def _find_request(ws: dict, ref: str, collection: str | None = None) -> tuple[dict, dict] | None:
    cols = [_find_collection(ws, collection)] if collection else ws["collections"]
    for col in cols:
        for req in (col or {}).get("requests") or []:
            if req.get("id") == ref:
                return col, req
    lc = str(ref).strip().lower()
    for col in cols:
        for req in (col or {}).get("requests") or []:
            if str(req.get("name", "")).strip().lower() == lc:
                return col, req
    return None


def _find_env(ws: dict, ref: str) -> dict | None:
    lc = str(ref).strip().lower()
    return next((e for e in ws["environments"]
                 if e.get("id") == ref or str(e.get("name", "")).strip().lower() == lc), None)


def _summary(req: dict) -> dict:
    return {"id": req.get("id"), "name": req.get("name"), "method": req.get("method"), "url": req.get("url")}


def _masked(key: str, value: Any, reveal: bool) -> Any:
    if reveal or not value or not is_sensitive(key) or "{{" in str(value):
        return value
    return MASK


# --------------------------------------------------------------------------- tools
class Tools:
    def __init__(self, store: Store) -> None:
        self.store = store

    def _load(self) -> tuple[dict, int]:
        ws = self.store.load_workspace()
        rev = int(ws.pop("revision", 0))
        if "collections" not in ws:  # never saved: the UI hasn't been opened yet
            ws = {"version": 1, "activeEnvId": None, "lastRequestId": None,
                  "environments": [], "collections": []}
        return ws, rev

    def _mutate(self, change: Callable[[dict], Any]) -> Any:
        """Load, change, save with the loaded revision. If the UI saved in between, retry
        on top of its version so neither side loses work."""
        for _ in range(5):
            ws, rev = self._load()
            out = change(ws)
            try:
                self.store.save_workspace(ws, base_revision=rev)
                return out
            except Conflict:
                continue
        raise ToolError("the workspace kept changing while saving; try again")

    # ---- read
    def workspace_overview(self, args: dict) -> dict:
        ws, rev = self._load()
        return {
            "database": str(self.store.path),
            "revision": rev,
            "activeEnvironment": next((e["name"] for e in ws["environments"]
                                       if e["id"] == ws.get("activeEnvId")), None),
            "environments": [{"id": e["id"], "name": e["name"],
                              "vars": {v.get("key"): _masked(v.get("key"), v.get("value"), False)
                                       for v in e.get("vars") or [] if v.get("enabled", True) is not False}}
                             for e in ws["environments"]],
            "collections": [{"id": c["id"], "name": c["name"],
                             "requests": [_summary(r) for r in c.get("requests") or []]}
                            for c in ws["collections"]],
        }

    def get_request(self, args: dict) -> dict:
        ws, _ = self._load()
        found = _find_request(ws, args["request"], args.get("collection"))
        if not found:
            raise ToolError(f"no request {args['request']!r}; call workspace_overview to list them")
        col, req = found
        return {"collection": {"id": col["id"], "name": col["name"]}, "request": req}

    def get_environment(self, args: dict) -> dict:
        ws, _ = self._load()
        env = _find_env(ws, args["environment"])
        if not env:
            raise ToolError(f"no environment {args['environment']!r}")
        reveal = bool(args.get("revealSecrets"))
        return {"id": env["id"], "name": env["name"], "active": env["id"] == ws.get("activeEnvId"),
                "vars": [{**v, "value": _masked(v.get("key"), v.get("value"), reveal)}
                         for v in env.get("vars") or []]}

    # ---- write
    def save_request(self, args: dict) -> dict:
        fields = args.get("request") or {}
        if not isinstance(fields, dict):
            raise ToolError("request must be an object")

        def change(ws: dict) -> dict:
            target = _find_collection(ws, args.get("collection"))
            if args.get("collection") and not target:
                target = {"id": str(uuid.uuid4()), "name": str(args["collection"]),
                          "collapsed": False, "requests": []}
                ws["collections"].append(target)
            existing = _find_request(ws, fields["id"]) if fields.get("id") else None
            if fields.get("id") and not existing:
                raise ToolError(f"no request with id {fields['id']!r} (omit id to create one)")
            if existing:
                col, req = existing
                apply_request_fields(req, fields)
                if target and target is not col:  # move it
                    col["requests"].remove(req)
                    target["requests"].append(req)
                    col = target
                created = False
            else:
                if not target:
                    raise ToolError("give a collection (name or id) for a new request")
                req = apply_request_fields(blank_request(), fields)
                target["requests"].append(req)
                col, created = target, True
            pos = args.get("position")
            if isinstance(pos, int):
                col["requests"].remove(req)
                col["requests"].insert(max(0, min(pos, len(col["requests"]))), req)
            return {"created": created, "collection": {"id": col["id"], "name": col["name"]},
                    "request": _summary(req)}

        return self._mutate(change)

    def delete_request(self, args: dict) -> dict:
        def change(ws: dict) -> dict:
            found = _find_request(ws, args["request"], args.get("collection"))
            if not found:
                raise ToolError(f"no request {args['request']!r}")
            col, req = found
            col["requests"].remove(req)
            return {"deleted": _summary(req), "note": "moved to RestDesk's Trash (restorable for 30 days)"}

        return self._mutate(change)

    def save_collection(self, args: dict) -> dict:
        def change(ws: dict) -> dict:
            col = _find_collection(ws, args.get("id") or args["name"])
            if col is None:
                col = {"id": str(uuid.uuid4()), "name": args["name"], "collapsed": False, "requests": []}
                ws["collections"].append(col)
                created = True
            else:
                created = False
                if args.get("id") and args.get("name"):
                    col["name"] = args["name"]
            if args.get("description") is not None:
                col["description"] = str(args["description"])
            return {"created": created, "collection": {"id": col["id"], "name": col["name"]}}

        return self._mutate(change)

    def delete_collection(self, args: dict) -> dict:
        def change(ws: dict) -> dict:
            col = _find_collection(ws, args["collection"])
            if not col:
                raise ToolError(f"no collection {args['collection']!r}")
            ws["collections"].remove(col)
            return {"deleted": {"id": col["id"], "name": col["name"],
                                "requests": len(col.get("requests") or [])},
                    "note": "moved to RestDesk's Trash (restorable for 30 days)"}

        return self._mutate(change)

    def save_environment(self, args: dict) -> dict:
        variables = args.get("vars") or {}
        if not isinstance(variables, dict):
            raise ToolError("vars must be an object {name: value}; use null to remove a variable")

        def change(ws: dict) -> dict:
            env = _find_env(ws, args["environment"])
            created = env is None
            if created:
                env = {"id": str(uuid.uuid4()), "name": args["environment"], "vars": []}
                ws["environments"].append(env)
            skipped = []
            for key, value in variables.items():
                rows = env["vars"]
                i = next((n for n, v in enumerate(rows) if v.get("key") == key), None)
                if value is None:
                    if i is not None:
                        rows.pop(i)
                elif str(value) == MASK:
                    skipped.append(key)  # a masked value read back; never overwrite the real one
                elif i is not None:
                    rows[i] = {**rows[i], "value": str(value), "enabled": True}
                else:
                    rows.append({"key": key, "value": str(value), "enabled": True})
            if args.get("activate") or (created and not ws.get("activeEnvId")):
                ws["activeEnvId"] = env["id"]
            out = {"created": created, "environment": {"id": env["id"], "name": env["name"]},
                   "active": ws.get("activeEnvId") == env["id"],
                   "vars": sorted(v.get("key") for v in env["vars"])}
            if skipped:
                out["skipped"] = {"keys": skipped, "reason": f"value was {MASK!r}; the real value was kept"}
            return out

        return self._mutate(change)

    # ---- history
    def list_history(self, args: dict) -> dict:
        request_id = None
        if args.get("request"):
            ws, _ = self._load()
            found = _find_request(ws, args["request"])
            request_id = found[1]["id"] if found else args["request"]
        return self.store.list_history(limit=int(args.get("limit") or 20), q=args.get("q") or "",
                                       request_id=request_id)

    def get_history_entry(self, args: dict) -> dict:
        entry = self.store.get_history(args["id"])
        if not entry:
            raise ToolError(f"no history entry {args['id']!r}")
        resp = (entry.get("result") or {}).get("response") or {}
        body = resp.get("body")
        if isinstance(body, str) and len(body) > MAX_BODY_CHARS:
            resp["body"] = body[:MAX_BODY_CHARS]
            resp["clippedForAgent"] = f"first {MAX_BODY_CHARS} of {len(body)} characters"
        return entry


# --------------------------------------------------------------------------- schemas
_ROWS_SCHEMA = {
    "description": "Object {name: value}, or a list of {key, value, enabled}",
    "anyOf": [{"type": "object", "additionalProperties": {"type": "string"}},
              {"type": "array", "items": {"type": "object", "properties": {
                  "key": {"type": "string"}, "value": {"type": "string"},
                  "enabled": {"type": "boolean"}}, "required": ["key"]}}],
}
_REQUEST_SCHEMA = {
    "type": "object",
    "description": "Fields to set. Omit id to create; include id to update (only the fields you "
                   "give change; body/auth/scripts/settings merge key by key).",
    "properties": {
        "id": {"type": "string", "description": "Existing request id, to update it"},
        "name": {"type": "string", "description": "Unique within the collection; used by {{@Name...}} references"},
        "method": {"type": "string", "enum": list(METHODS)},
        "url": {"type": "string", "description": "e.g. {{baseUrl}}/users/{{userId}}"},
        "params": {**_ROWS_SCHEMA, "description": "Query params (added to the URL). " + _ROWS_SCHEMA["description"]},
        "headers": _ROWS_SCHEMA,
        "body": {"type": "object", "properties": {
            "mode": {"type": "string", "enum": list(BODY_MODES)},
            "text": {"description": "Body for json/text/xml. For json, a string or an object (stored pretty-printed)."},
            "form": {**_ROWS_SCHEMA, "description": "Fields for urlencoded/multipart"}}},
        "auth": {"type": "object", "description":
                 "{type:'none'} | {type:'bearer', token, prefix?='Bearer'} | {type:'basic', username, password} "
                 "| {type:'apikey', key, value, in:'header'|'query'}. Values may use {{templates}}.",
                 "properties": {"type": {"type": "string", "enum": list(AUTH_TYPES)}}},
        "scripts": {"type": "object", "properties": {
            "pre": {"type": "string", "description": "JS run before sending"},
            "post": {"type": "string", "description": "JS run after the response (tests, env.set)"}}},
        "settings": {"type": "object", "properties": {
            "timeoutMs": {"type": "integer"}, "followRedirects": {"type": "boolean"},
            "verifyTls": {"type": "boolean"}}},
    },
}

TOOLS: list[dict] = [
    {"name": "workspace_overview",
     "description": "List all collections (with their requests: id, name, method, url) and environments "
                    "(variables; secret-looking values masked). Call this first.",
     "inputSchema": {"type": "object", "properties": {}},
     "annotations": {"readOnlyHint": True}},
    {"name": "get_request",
     "description": "Get one saved request in full (params, headers, body, auth, scripts, settings).",
     "inputSchema": {"type": "object", "properties": {
         "request": {"type": "string", "description": "Request id or name"},
         "collection": {"type": "string", "description": "Collection id or name, to disambiguate a name"}},
         "required": ["request"]},
     "annotations": {"readOnlyHint": True}},
    {"name": "get_environment",
     "description": "Get one environment's variables. Secret-looking values are masked unless revealSecrets is true.",
     "inputSchema": {"type": "object", "properties": {
         "environment": {"type": "string", "description": "Environment id or name"},
         "revealSecrets": {"type": "boolean", "default": False}},
         "required": ["environment"]},
     "annotations": {"readOnlyHint": True}},
    {"name": "save_request",
     "description": "Create a request in a collection (created if missing), or update/move an existing one by id.",
     "inputSchema": {"type": "object", "properties": {
         "collection": {"type": "string", "description": "Collection id or name. Required to create; "
                                                         "for an update, moves the request there."},
         "request": _REQUEST_SCHEMA,
         "position": {"type": "integer", "description": "0-based index in the collection (default: keep / append)"}},
         "required": ["request"]},
     "annotations": {"destructiveHint": False, "idempotentHint": False}},
    {"name": "delete_request",
     "description": "Delete a request (it goes to RestDesk's Trash).",
     "inputSchema": {"type": "object", "properties": {
         "request": {"type": "string", "description": "Request id or name"},
         "collection": {"type": "string", "description": "Collection id or name, to disambiguate a name"}},
         "required": ["request"]},
     "annotations": {"destructiveHint": True}},
    {"name": "save_collection",
     "description": "Create a collection by name (returns the existing one if the name is taken), "
                    "or rename one by passing its id and a new name.",
     "inputSchema": {"type": "object", "properties": {
         "name": {"type": "string"}, "id": {"type": "string", "description": "Existing collection id, to rename"},
         "description": {"type": "string"}},
         "required": ["name"]},
     "annotations": {"destructiveHint": False, "idempotentHint": True}},
    {"name": "delete_collection",
     "description": "Delete a collection and its requests (they go to RestDesk's Trash).",
     "inputSchema": {"type": "object", "properties": {
         "collection": {"type": "string", "description": "Collection id or name"}},
         "required": ["collection"]},
     "annotations": {"destructiveHint": True}},
    {"name": "save_environment",
     "description": "Create an environment or set/remove its variables. Variables you don't mention are kept.",
     "inputSchema": {"type": "object", "properties": {
         "environment": {"type": "string", "description": "Environment id or name (created if missing)"},
         "vars": {"type": "object", "description": "{name: value}; value null removes the variable",
                  "additionalProperties": {"type": ["string", "null"]}},
         "activate": {"type": "boolean", "description": "Make it the active environment"}},
         "required": ["environment"]},
     "annotations": {"destructiveHint": False, "idempotentHint": True}},
    {"name": "list_history",
     "description": "Recent calls the user sent, newest first (status, timing, test pass/fail). Secrets are redacted.",
     "inputSchema": {"type": "object", "properties": {
         "q": {"type": "string", "description": "Filter by URL, name, method or status (e.g. '401')"},
         "request": {"type": "string", "description": "Only calls of this request (id or name)"},
         "limit": {"type": "integer", "default": 20, "maximum": 200}}},
     "annotations": {"readOnlyHint": True}},
    {"name": "get_history_entry",
     "description": "One call from history: the request as sent, the response (headers, body) and test results.",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
     "annotations": {"readOnlyHint": True}},
]


# --------------------------------------------------------------------------- protocol
class Server:
    def __init__(self, store: Store) -> None:
        self.tools = Tools(store)

    def handle(self, msg: Any) -> dict | None:
        """Handle one JSON-RPC message; returns the response, or None for notifications."""
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return self._error(None, -32600, "invalid request")
        mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if "id" not in msg:  # notification (initialized, cancelled, ...)
            return None
        if method == "initialize":
            asked = params.get("protocolVersion")
            return self._result(mid, {
                "protocolVersion": asked if asked in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0],
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "restdesk", "title": "RestDesk", "version": __version__},
                "instructions": INSTRUCTIONS,
            })
        if method == "ping":
            return self._result(mid, {})
        if method == "tools/list":
            return self._result(mid, {"tools": TOOLS})
        if method == "tools/call":
            return self._result(mid, self._call(params.get("name"), params.get("arguments") or {}))
        return self._error(mid, -32601, f"method not found: {method}")

    def _call(self, name: Any, args: dict) -> dict:
        if not any(t["name"] == name for t in TOOLS):
            return {"isError": True, "content": [{"type": "text", "text": f"unknown tool {name!r}"}]}
        try:
            out = getattr(self.tools, name)(args)
        except ToolError as exc:
            return {"isError": True, "content": [{"type": "text", "text": str(exc)}]}
        except KeyError as exc:
            return {"isError": True, "content": [{"type": "text", "text": f"missing argument {exc}"}]}
        except Exception as exc:  # keep the server alive, tell the agent what broke
            return {"isError": True, "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}]}
        return {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False, indent=1)}],
                "structuredContent": out}

    @staticmethod
    def _result(mid: Any, result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    @staticmethod
    def _error(mid: Any, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}

    def serve(self, stdin=None, stdout=None) -> None:
        stdin = stdin or sys.stdin
        stdout = stdout or sys.stdout
        for line in stdin:
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                reply = self._error(None, -32700, "parse error")
            else:
                if isinstance(msg, list):  # batch (older protocol versions)
                    reply = [r for r in (self.handle(m) for m in msg) if r is not None] or None
                else:
                    reply = self.handle(msg)
            if reply is not None:
                stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
                stdout.flush()


def main(argv: list[str] | None = None) -> int:
    from .server import default_data_path  # late import: server imports nothing from here

    ap = argparse.ArgumentParser(prog="restdesk mcp",
                                 description="Run RestDesk's MCP server on stdio (for coding agents)")
    ap.add_argument("--data", default=os.environ.get("RESTDESK_DATA", str(default_data_path())),
                    help="SQLite database file (default: the same one the RestDesk app uses)")
    args = ap.parse_args(argv)
    try:
        store = Store(Path(args.data).expanduser().resolve())
    except SchemaTooNew as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"RestDesk MCP server {__version__} on {store.path}", file=sys.stderr)  # stdout is the protocol
    try:
        Server(store).serve()
    except KeyboardInterrupt:
        pass
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
