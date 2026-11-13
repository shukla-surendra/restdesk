"""Mask secrets before a call is written to history.

History is meant for "what did I send and what came back", not as a credential store, so by
default secret-looking values are replaced with ``[redacted]``:

- request headers / query params / form + JSON body fields with sensitive names
  (``Authorization``, ``Cookie``, ``X-Api-Key``, ``password``, ``access_token`` ...),
- the same fields in the response (``Set-Cookie``, a login response's ``access_token`` ...),
- literal secrets in the saved request snapshot (auth token, password, API key).

Values that are only ``{{variable}}`` references are kept, because they don't contain the
secret itself and are needed to replay the request from history.
"""
from __future__ import annotations

import json
import re
import urllib.parse
from typing import Any

MASK = "[redacted]"
_SUBSTRINGS = ("password", "passwd", "secret", "token", "apikey", "authorization", "cookie",
               "session", "signature", "credential", "privatekey", "bearer")
_EXACT = {"auth", "xauth", "key", "pin", "otp", "sig", "jwt", "pwd"}
_TEMPLATE_ONLY = re.compile(r"^\s*(\{\{[^{}]+\}\}\s*)+$")


def is_sensitive(name: Any) -> bool:
    norm = re.sub(r"[^a-z]", "", str(name or "").lower())
    return bool(norm) and (norm in _EXACT or any(s in norm for s in _SUBSTRINGS))


def _is_template(value: Any) -> bool:
    return isinstance(value, str) and bool(_TEMPLATE_ONLY.match(value))


def _mask_json_value(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: (MASK if is_sensitive(k) and v not in (None, "") and not isinstance(v, (dict, list))
                    else _mask_json_value(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_mask_json_value(v) for v in obj]
    return obj


def mask_json_text(text: Any) -> Any:
    """Mask sensitive keys in a JSON document given as text; other text is returned as-is."""
    if not isinstance(text, str) or not text.lstrip()[:1] in ("{", "["):
        return text
    try:
        data = json.loads(text)
    except ValueError:
        return text
    masked = _mask_json_value(data)
    return text if masked == data else json.dumps(masked, ensure_ascii=False, indent=2)


def mask_form_text(text: Any) -> Any:
    if not isinstance(text, str) or "=" not in text:
        return text
    pairs = urllib.parse.parse_qsl(text, keep_blank_values=True)
    if not any(is_sensitive(k) for k, _ in pairs):
        return text
    return urllib.parse.urlencode([(k, MASK if is_sensitive(k) else v) for k, v in pairs])


def mask_url(url: Any) -> Any:
    if not isinstance(url, str) or "?" not in url:
        return url
    parts = urllib.parse.urlsplit(url)
    pairs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    if not any(is_sensitive(k) for k, _ in pairs):
        return url
    query = urllib.parse.urlencode([(k, MASK if is_sensitive(k) else v) for k, v in pairs])
    return urllib.parse.urlunsplit(parts._replace(query=query))


def _mask_header_pairs(headers: Any) -> Any:
    if not isinstance(headers, list):
        return headers
    out = []
    for pair in headers:
        if isinstance(pair, (list, tuple)) and len(pair) >= 2 and is_sensitive(pair[0]):
            out.append([pair[0], MASK])
        else:
            out.append(pair)
    return out


def _content_type(headers: Any) -> str:
    for pair in headers or []:
        if isinstance(pair, (list, tuple)) and len(pair) >= 2 and str(pair[0]).lower() == "content-type":
            return str(pair[1]).lower()
    return ""


def _mask_body(body: Any, ctype: str) -> Any:
    if "x-www-form-urlencoded" in ctype:
        return mask_form_text(body)
    return mask_json_text(body)


def _mask_kv_rows(rows: Any) -> Any:
    """Snapshot tables: [{key, value, enabled}] - keep {{templates}}, mask literal secrets."""
    if not isinstance(rows, list):
        return rows
    return [{**r, "value": MASK} if isinstance(r, dict) and is_sensitive(r.get("key"))
            and r.get("value") and not _is_template(r.get("value")) else r for r in rows]


def redact_snapshot(snap: dict) -> dict:
    snap = dict(snap or {})
    snap["headers"] = _mask_kv_rows(snap.get("headers"))
    snap["params"] = _mask_kv_rows(snap.get("params"))
    auth = snap.get("auth")
    if isinstance(auth, dict):
        auth = dict(auth)
        for field in ("token", "password", "value"):
            if auth.get(field) and not _is_template(auth[field]):
                auth[field] = MASK
        snap["auth"] = auth
    body = snap.get("body")
    if isinstance(body, dict):
        body = dict(body)
        body["form"] = _mask_kv_rows(body.get("form"))
        if body.get("mode") == "json":
            body["text"] = mask_json_text(body.get("text"))
        snap["body"] = body
    return snap


def redact_result(result: dict) -> dict:
    result = dict(result or {})
    final = result.get("final")
    if isinstance(final, dict):
        final = dict(final)
        final["url"] = mask_url(final.get("url"))
        final["body"] = _mask_body(final.get("body"), _content_type(final.get("headers")))
        final["headers"] = _mask_header_pairs(final.get("headers"))
        result["final"] = final
    resp = result.get("response")
    if isinstance(resp, dict):
        resp = dict(resp)
        if resp.get("bodyEncoding") != "base64":
            resp["body"] = _mask_body(resp.get("body"), _content_type(resp.get("headers")))
        resp["headers"] = _mask_header_pairs(resp.get("headers"))
        result["response"] = resp
    return result
