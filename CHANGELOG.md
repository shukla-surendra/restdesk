# Changelog

## 0.3.0

Hardening release: data safety and security.

### Safety
- **No more lost work across tabs.** Saves carry a revision number. A save from a tab with an
  older copy is refused (`409`) and you choose whether to load the latest version or overwrite.
  Idle tabs reload automatically when another tab saves.
- **Edits are saved when the tab closes.** Pending changes are written when the tab is hidden. If a
  large workspace can't finish saving on close, the browser asks before leaving.
- **Undo and Trash.** Deleting a request or collection shows an *Undo* toast. Deleted items stay in
  **Trash** (sidebar footer) for 30 days and can be restored.
- **Schema migrations.** Older databases are upgraded in place on start. A database from a newer
  RestDesk is refused instead of being damaged.

### Security
- **Scripts run in a sandbox.** Pre-request and test scripts run in a Web Worker with no DOM, no
  network (`fetch`, XHR, WebSocket, `importScripts` blocked by CSP and removed), and a time limit
  (10 s default, set per request in *Settings*). An imported collection can no longer read your
  other data or tokens.
- **History hides secrets.** Auth headers, cookies, API keys, passwords and tokens are replaced with
  `[redacted]` in stored calls (request, response and snapshot). `{{variables}}` are kept so
  history can still be replayed. Use `--keep-secrets-in-history` to turn this off.
- **Content-Security-Policy** on the UI (no inline or remote script), plus `X-Frame-Options` and
  `Referrer-Policy`.

### Fixes
- Large binary responses (images, files) in history are no longer stored as corrupted, clipped
  base64. The body is skipped and the UI says so.
- Opening the app no longer rewrites the workspace on every page load.

### Development
- Browser tests (Playwright) cover sending, history redaction, the sandbox, the script time limit,
  Undo/Trash, cross-tab sync and save conflicts. They run in CI against a local test API.

## 0.2.0
- uv project layout and `restdesk` console script.
- SQLite storage for collections, environments and full call history; JSON export/import.
- Searchable history with stored responses; migration from the 0.1 JSON workspace.

## 0.1.0
- First version: single-file server, JSON workspace.
