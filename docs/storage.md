# Storage and history

All data lives in one **SQLite** database, written through Python's standard `sqlite3` module.
There's no server, account or sync.

## Where the database is

| OS | Default path |
|---|---|
| macOS | `~/Library/Application Support/restdesk/restdesk.db` |
| Linux | `$XDG_DATA_HOME/restdesk/restdesk.db` (usually `~/.local/share/restdesk/restdesk.db`) |
| Windows | `%APPDATA%\restdesk\restdesk.db` |

Override it with `--data PATH` or `RESTDESK_DATA=PATH`. To keep separate workspaces, run one
database per project:

```bash
restdesk --data ./api-work.db
```

The path is also shown at startup and in the tooltip on `saved` in the sidebar.

## What is stored

| Table | Contents |
|---|---|
| `collections` | name, order, collapsed state |
| `requests` | each saved request as JSON (method, URL, params, headers, body, auth, scripts, settings), plus its collection and position |
| `environments` | name, order, variables |
| `meta` | active environment, last open request, schema version |
| `trash` | deleted collections and requests, kept for 30 days (up to 500 items) |
| `history` | one row per call: time, source (`send` / `runner` / `import`), method, final URL, status, timing, size, test counts, the request snapshot, and the full result (resolved request, response status/headers/body, tests, console logs) |

Saves run as transactions in WAL mode, so a crash can't leave the database half-written. Autosave
only rewrites rows that changed.

## Several tabs or windows

Each save carries the **revision** the tab last loaded. If another tab saved in the meantime, the
server refuses the save (`409`) and the tab asks you to either load the latest version or overwrite
it, so a stale tab never silently deletes newer requests. Tabs also announce their saves to each
other, so a tab you aren't editing in reloads on its own.

Pending edits are saved when a tab is hidden or closed. If a very large workspace can't finish
saving on close, the browser asks you to confirm leaving.

## Trash

Deleting a request or collection shows an **Undo** button for a few seconds. After that the item is
still in **Trash** (sidebar footer) for 30 days. *Restore* puts it back in its collection, or in a
new one if the collection is gone. *Empty trash* deletes everything in it permanently.

## Schema upgrades

The database has a schema version. When a newer RestDesk opens an older file, it upgrades the
schema in place inside a transaction. If a database was written by a *newer* RestDesk than the
one you're running, it refuses to start rather than risk damaging the file. Upgrade RestDesk instead.

## History limits

| Setting | Default | Change with |
|---|---|---|
| Entries kept | 2000 newest | `--history-limit N` or `RESTDESK_HISTORY_LIMIT` (`0` = unlimited) |
| Stored body per entry | 2 MB | Longer text bodies are clipped. Binary bodies over the limit aren't stored (shown live up to 50 MB) |
| Secret redaction | on | `--keep-secrets-in-history` or `RESTDESK_KEEP_SECRETS=1` |

### Secret redaction

Before a call is stored, values that look like credentials are replaced with `[redacted]`:

- request and response **headers** such as `Authorization`, `Cookie`, `Set-Cookie`, `X-Api-Key`;
- **query parameters**, **form fields** and **JSON keys** whose names contain `password`, `secret`,
  `token`, `apikey`, `session`, `cookie`, `signature`, `credential` (and names like `key`, `pin`, `otp`),
  in the request *and* the response (a login response's `access_token` is masked too);
- literal secrets in the saved request (auth token, password, API key, secret header values).

Values that are only `{{variable}}` references are kept, so a request opened from history still
works with your environment. Redaction only affects **history**. Your environments, requests and
scripts still see the real values. Replayed entries show *secrets redacted* in the status line.

Name matching is deliberately broad. A field like `next_page_token` is masked too. Turn redaction
off with `--keep-secrets-in-history` if you need exact copies and the database is protected.

## Backups, export and import

The database is a single file. Copy it while RestDesk is stopped, or export to JSON:

```bash
restdesk --export backup.json        # collections, environments and history, then exits
```

The sidebar **Export** button does the same and asks whether to include history. **Import** loads
such a file back, including its history.

You can also query the database directly:

```bash
sqlite3 ~/Library/Application\ Support/restdesk/restdesk.db \
  "select datetime(ts/1000,'unixepoch'), method, status, url from history order by ts desc limit 20"
```

## Upgrading from 0.1 (JSON workspace)

RestDesk 0.1 kept everything in `data/workspace.json`. On first start with an empty database,
RestDesk 0.2 imports `./data/workspace.json` from the current directory, or the file passed to
`--import-json`, along with its history. The JSON file isn't changed or deleted.

## Privacy

History redacts recognisable secrets (see above), but the database still holds everything you type,
including the real values in your **environments** and requests, and response data under names it
doesn't recognise. Treat it like a credentials file. `*.db` is in `.gitignore`. Leave history
out of exports you share.
