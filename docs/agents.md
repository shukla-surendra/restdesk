# Coding agents (MCP)

RestDesk includes an [MCP](https://modelcontextprotocol.io) server, so a coding agent such as
Claude Code, Cursor, VS Code Copilot or Codex can set up your API work for you. Ask it something
like *"add a RestDesk collection for the endpoints in `routes/users.ts`, with a login flow"*. It
creates the collection, requests, environment variables and test scripts, and they appear in your
open RestDesk window within about two seconds.

```bash
restdesk mcp                 # MCP server on stdio, using the same database as the app
restdesk mcp --data ./x.db   # or a specific database
```

It's the same `restdesk` command and has no extra dependencies.

## Set it up

Pick the line for your agent. If you haven't installed RestDesk as a command
(`uv tool install git+https://github.com/shukla-surendra/restdesk`), replace `restdesk mcp` with
`uvx --from git+https://github.com/shukla-surendra/restdesk restdesk mcp`.

**Claude Code**

```bash
claude mcp add restdesk --scope user -- restdesk mcp
```

**Codex CLI**

```bash
codex mcp add restdesk -- restdesk mcp
```

**Cursor** (`~/.cursor/mcp.json`) and **Claude Desktop** (`claude_desktop_config.json`)

```json
{ "mcpServers": { "restdesk": { "command": "restdesk", "args": ["mcp"] } } }
```

**VS Code** (`.vscode/mcp.json`)

```json
{ "servers": { "restdesk": { "type": "stdio", "command": "restdesk", "args": ["mcp"] } } }
```

**Desktop app only, no Python.** The bundled server binary understands `mcp` too. Use it as the command:

| OS | Command |
|---|---|
| macOS | `/Applications/RestDesk.app/Contents/Resources/server/restdesk-server mcp` |
| Windows | `%LOCALAPPDATA%\Programs\RestDesk\resources\server\restdesk-server.exe mcp` |
| Linux (.deb) | `/opt/RestDesk/resources/server/restdesk-server mcp` |

### One database per project

To keep a project's requests apart, point both the app and the agent at a project database:

```bash
restdesk --data ./restdesk.db                       # the app
claude mcp add restdesk -- restdesk mcp --data ./restdesk.db   # the agent (project scope)
```

`*.db` is in RestDesk's own `.gitignore`. Add it to your project's `.gitignore` too, because the
database holds your environment secrets.

## What the agent can do

| Tool | Does |
|---|---|
| `workspace_overview` | list collections, requests and environments (secret-looking values masked) |
| `get_request` | read one request in full |
| `get_environment` | read an environment's variables (masked unless `revealSecrets: true`) |
| `save_request` | create a request (and its collection), or update or move one by id. Partial updates merge |
| `delete_request`, `delete_collection` | delete, into RestDesk's **Trash** (30 days, restorable) |
| `save_collection` | create or rename a collection |
| `save_environment` | create an environment, set or remove variables, make it active |
| `list_history`, `get_history_entry` | read calls *you* sent (secrets already redacted), for debugging |

The server also tells the agent RestDesk's conventions: keep base URLs and secrets in environment
variables, and chain a login with `{{@Login.body.access_token}}` instead of copying tokens. See
[Using one response in another request](user-guide.md#using-one-response-in-another-request).

**The agent doesn't send requests.** You stay in control of what hits your APIs: click Send or
run the collection. The agent can then read the result from history and fix what failed.

## How it stays safe with the app open

- The MCP server writes the database directly, so the RestDesk app doesn't have to be running.
- Every change is saved against the revision it loaded. If you saved in the meantime, the change
  is re-applied on top of your version, so neither side's work is lost.
- An open RestDesk window checks for outside changes every two seconds and on focus. It reloads
  and shows *Workspace updated outside this window*. If you were typing at that moment, you get
  the usual conflict prompt instead.
- Deletes go to the Trash, as they do in the UI.
- The agent sees masked values for secret-looking variables (`token`, `password`, `apiKey`...).
  If it writes back a masked value, that value is ignored rather than overwriting your secret.

Anything the agent can read reaches the model provider it runs on. That includes request bodies,
non-secret variables and (with `revealSecrets` or `get_history_entry`) response data. Keep
production credentials in an environment you don't share with agents, or use a separate database.
