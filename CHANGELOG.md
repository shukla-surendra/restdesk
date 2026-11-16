# Changelog

## Unreleased

### Fixed
- **Switching requests no longer loses the response.** Each request keeps its last response for
  the session, and after a reload its most recent call is restored from history. A response that
  arrives after you switch away is shown on the request that sent it, not the one now open.

### Added
- **Use one response in another request.** `{{@Login.body.access_token}}`, `{{@Login.header.Location}}`
  and `{{@Login.status}}` read another request's last response (this session, else history), in
  URLs, params, headers, bodies, auth and env variables. `??` adds fallbacks in any order:
  `{{@Login.body.token ?? token ?? "default"}}`. Templates that resolve to nothing are reported.
  Hover a header or param value to see what it resolves to.
- **Project export/import.** Export lets you choose collections and environments (all variables
  included) and, optionally, history. Import previews the file, then merges without duplicating
  ids (environments matched by name get the imported variables, with an overwrite option), or
  replaces the project.
- **App icon** for the web UI (favicon, touch icon, sidebar) and the desktop app.
- **Desktop app.** An Electron wrapper in `desktop/` runs the same UI with a bundled, PyInstaller-frozen
  server, so no Python install is needed. A CI workflow builds macOS, Windows and Linux installers.
  See [docs/desktop.md](docs/desktop.md).
- Server: `--port 0` picks a free port, a `RESTDESK_READY <url>` line is printed at startup, and
  `--exit-with-stdin` stops the server when its parent process goes away.
- Naming or renaming a collection uses an in-page dialog instead of the browser's `prompt()`.
- **Drag and drop in the sidebar.** Reorder requests, move them between collections, and reorder
  collections. **Alt+Shift+↑/↓** moves the selected request from the keyboard.

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
