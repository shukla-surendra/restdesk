# Development

RestDesk is a [uv](https://docs.astral.sh/uv/) project. The app uses only the Python standard
library and vanilla JS, with no build step.

```bash
git clone https://github.com/shukla-surendra/restdesk && cd restdesk
uv sync                    # creates .venv with the dev tools (pytest)
uv run restdesk            # run from source
uv run pytest              # run the tests
uv build                   # build sdist + wheel into dist/
```

Set `RESTDESK_VERBOSE=1` to log every HTTP request the server handles.

## Layout

```
pyproject.toml               # project metadata, `restdesk` console script, uv_build backend
src/restdesk/server.py       # HTTP server, proxy engine (urllib), CLI
src/restdesk/storage.py      # SQLite persistence: workspace tables + call history
src/restdesk/static/         # UI: index.html, app.js, style.css (served as-is)
tests/                       # pytest: storage + HTTP API
docs/                        # this documentation and screenshots
```

## How a Send works

1. `app.js` runs the pre-request script, fills in `{{variables}}`, and builds the final request.
2. It `POST`s that to `/api/send`. `server.py` makes the call with `urllib`, decodes gzip/deflate,
   and returns status, headers, body (UTF-8 or base64) and timing.
3. The browser runs the test script and renders the response.
4. The call, request snapshot and result are `POST`ed to `/api/history` and stored by `storage.py`.

Collections and environments autosave with a debounced `PUT /api/workspace`. The store upserts
rows and deletes the ones that are gone, all in one transaction.

## Contributing

Issues and pull requests are welcome. Please:

- keep the app dependency-free (stdlib Python, vanilla JS),
- add or update tests in `tests/` for server/storage changes, and run `uv run pytest`,
- keep the server bound to `127.0.0.1` and the `X-RestDesk` header check in place.
