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
| `history` | one row per call: time, source (`send` / `runner` / `import`), method, final URL, status, timing, size, test counts, the request snapshot, and the full result (resolved request, response status/headers/body, tests, console logs) |

Saves run as transactions in WAL mode, so a crash can't leave the database half-written. Autosave
only rewrites rows that changed.

## History limits

| Setting | Default | Change with |
|---|---|---|
| Entries kept | 2000 newest | `--history-limit N` or `RESTDESK_HISTORY_LIMIT` (`0` = unlimited) |
| Stored body per entry | 2 MB | Longer bodies are clipped in history (shown live up to 50 MB) |

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

The database holds everything you type **and every response you receive**, including tokens,
cookies and passwords. Treat it like a credentials file. `*.db` is in `.gitignore`. Leave history
out of exports you share.
