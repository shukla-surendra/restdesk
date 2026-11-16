# Desktop app (Electron)

RestDesk runs two ways from one codebase:

| | Web | Desktop |
|---|---|---|
| Start | `uv run restdesk`, then open the browser tab | Open the RestDesk app |
| Needs | Python 3.9+ | Nothing; the server is bundled |
| UI | `src/restdesk/static/` | the same files, shown in an Electron window |
| Data | `restdesk.db` in your user data folder | the same file, so both see the same collections |

## How it works

```
RestDesk.app (Electron main process, desktop/main.js)
  ├─ starts resources/server/restdesk-server --port 0 --no-browser --exit-with-stdin
  ├─ waits for the "RESTDESK_READY http://127.0.0.1:<port>/" line it prints
  ├─ opens that URL in a sandboxed window (no Node.js access in the page)
  └─ on quit: saves any pending edit, then closes the server's stdin so it stops
```

`restdesk-server` is the normal Python package frozen with PyInstaller, so the desktop app makes
HTTP calls, stores data and redacts history in exactly the same way as the web version.
`--exit-with-stdin` makes the server stop whenever the app exits, even after a crash.

Links that point outside RestDesk open in your default browser. **Help → Open in Browser** opens
the running desktop session in a browser tab.

You can run the web version and the desktop app at the same time. They share one database, and
the save-conflict check (see [storage](storage.md)) stops either one from overwriting the other.

## Develop

Needs Node.js 20+ and uv.

```bash
cd desktop
npm install
npm start            # Electron window running `uv run restdesk` from this checkout
npm run smoke        # headless check: starts, renders the UI, quits (exit code 0 = ok)
```

Environment variables:

- `RESTDESK_DATA=/path/to.db` uses a different database.
- `RESTDESK_SERVER=/path/to/binary` runs a specific server binary.

## Build installers

```bash
cd desktop
npm run dist:dir     # unpacked app in desktop/dist/ (quick local test)
npm run dist         # .dmg/.zip on macOS, NSIS .exe on Windows, AppImage/.deb on Linux
```

`npm run build:server` (run by both commands) freezes the server for the **current** OS and CPU,
so build each platform on that platform. The `desktop` GitHub workflow does this on macOS, Windows
and Linux and uploads the installers as build artifacts.

## Signing

Builds are unsigned by default. macOS shows a Gatekeeper warning for unsigned apps (right-click →
**Open** the first time), and Windows SmartScreen shows a similar one. To sign, set the usual
[electron-builder variables](https://www.electron.build/code-signing):

- macOS: `CSC_LINK`, `CSC_KEY_PASSWORD`, and `APPLE_ID` / `APPLE_APP_SPECIFIC_PASSWORD` /
  `APPLE_TEAM_ID` for notarization.
- Windows: `WIN_CSC_LINK`, `WIN_CSC_KEY_PASSWORD`.

## Icon

The app icon is `desktop/resources/icon.svg`, and the web favicon is `src/restdesk/static/icon.svg`.
After editing either one, run `npm run icons` in `desktop/` to regenerate `resources/icon.png` (1024
px; electron-builder makes the macOS `.icns` and Windows `.ico` from it) and `static/icon-192.png`.

## Size

About 280 MB unpacked on macOS (Electron itself is most of it, plus about 20 MB for the server), and
roughly 100 MB as a compressed download.
