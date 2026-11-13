# Local HTTP API

The UI talks to the RestDesk process over a small JSON API. You can script against it too, for
example to read history from another tool.

Every `/api/*` call must:

- go to `127.0.0.1`, `localhost` or `[::1]` (other `Host` values get `403`, which blocks DNS rebinding), and
- send the header `X-RestDesk: 1` (other web pages can't set it without a CORS preflight, which RestDesk never approves).

| Method & path | Purpose |
|---|---|
| `GET /api/workspace` | collections, environments, active env, `revision` (only `{revision}` before the first save) |
| `PUT /api/workspace` | replace collections/environments. Include `baseRevision` (the revision you loaded). Returns `{revision}`, or `409 {error: "conflict", revision}` if someone saved since. Omit `baseRevision` to overwrite unconditionally. Removed collections/requests go to the trash |
| `POST /api/send` | perform an HTTP call: `{method, url, headers: [[k, v]], body \| bodyBase64, timeoutMs, followRedirects, verifyTls}` |
| `GET /api/history?q=&limit=&offset=&requestId=` | list entries, newest first: `{total, items: [...]}` |
| `GET /api/history/{id}` | one entry with `snapshot` and full `result` |
| `POST /api/history` | add an entry: `{snapshot, result, source?, ts?, id?}` |
| `DELETE /api/history/{id}` | delete one entry |
| `DELETE /api/history` | delete all history |
| `GET /api/export?history=0\|1` | full JSON export |
| `GET /api/trash` | deleted collections/requests: `{items: [...]}` |
| `POST /api/trash/{id}/restore` | remove an item from the trash and return it (`{kind, data, collectionId, collectionName}`). The caller adds it back to the workspace |
| `DELETE /api/trash` | empty the trash |
| `GET /api/info` | version, database path, history limit, redaction on/off, schema version |

Example:

```bash
curl -s -H 'X-RestDesk: 1' 'http://127.0.0.1:8787/api/history?q=404&limit=5'
```
