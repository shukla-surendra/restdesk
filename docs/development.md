# Development

RestDesk is a [uv](https://docs.astral.sh/uv/) project. The app uses only the Python standard
library and vanilla JS, with no build step.

```bash
git clone https://github.com/shukla-surendra/restdesk && cd restdesk
uv sync                    # creates .venv with the dev tools (pytest)
uv run restdesk            # run from source
uv run pytest              # run all tests (browser tests skip if no Chromium/Chrome)
uv run pytest -m "not e2e" # unit tests only
uv run playwright install chromium   # once, to run the browser tests without Google Chrome
uv build                   # build sdist + wheel into dist/
```

Set `RESTDESK_VERBOSE=1` to log every HTTP request the server handles.

## Layout

```
pyproject.toml               # project metadata, `restdesk` console script, uv_build backend
src/restdesk/server.py       # HTTP server, proxy engine (urllib), CLI
src/restdesk/storage.py      # SQLite persistence: workspace, trash, history, migrations
src/restdesk/redact.py       # masks secrets before calls are written to history
src/restdesk/static/         # UI: index.html, app.js, style.css (served as-is)
src/restdesk/static/sandbox-worker.js   # runs user scripts, isolated from the page
tests/                       # pytest: storage, redaction, HTTP API
tests/e2e/                   # Playwright browser tests against a local target API
docs/                        # this documentation and screenshots
```

## How a Send works

1. `app.js` runs the pre-request script in a fresh `sandbox-worker.js` worker (served with a CSP
   that blocks network access, terminated on timeout), then fills in `{{variables}}` and builds
   the final request.
2. It `POST`s that to `/api/send`. `server.py` makes the call with `urllib`, decodes gzip/deflate,
   and returns status, headers, body (UTF-8 or base64) and timing.
3. The test script runs in another sandbox worker, and the response is rendered.
4. The call, request snapshot and result are `POST`ed to `/api/history`. `redact.py` masks
   secrets, and `storage.py` stores the entry.

Collections and environments autosave with a debounced `PUT /api/workspace` that carries the
revision the tab loaded. The store refuses stale revisions (`409`), upserts changed rows, moves
removed ones to `trash`, and bumps the revision, all in one transaction. Tabs announce saves on a
`BroadcastChannel` so idle tabs reload.

## Changing the database schema

Add a function to `MIGRATIONS` in `storage.py` with the next version number. Never edit a
released step. Add a test that builds the previous schema and opens it with `Store`
(see `tests/test_hardening.py::test_v1_database_is_upgraded_in_place`).

## Contributing

Issues and pull requests are welcome. Please:

- keep the app dependency-free (stdlib Python, vanilla JS),
- add or update tests in `tests/` for server/storage changes (and `tests/e2e/` for UI behaviour),
  and run `uv run pytest`,
- keep the server bound to `127.0.0.1` and the `X-RestDesk` header check in place.
