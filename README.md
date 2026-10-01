# Google Calendar MCP Server

An MCP server that gives a coding agent read/write access to the signed-in
user's Google Calendar over stdio, with a real OAuth 2.0 authorization-code
flow (PKCE), cached tokens, and silent refresh.

Written for Week 2 of Modern Software Dev
([assignment](https://github.com/mihail911/modern-software-dev-assignments/blob/master/week2/assignment.md));
`writeup.md` has the design rationale and two captured MCP transcripts.

## Tools

| Tool | Kind | Produces | Consumes |
| --- | --- | --- | --- |
| `list_calendars` | read | `calendar_id`, `access_role` | — |
| `search_events` | read | `event_id` + `calendar_id` | `calendar_id` |
| `get_event` | read | full event detail | `calendar_id`, `event_id` |
| `find_free_slots` | read | bookable `start` / `suggested_end` | `calendar_id`s |
| `create_event` | **write** | a new `event_id` | `calendar_id`, slot times |
| `update_event` | **write** | the updated event | `calendar_id`, `event_id` |
| `check_auth` | read | credential state | — |

They compose: `list_calendars` → `search_events` → `get_event` →
`update_event`, and `find_free_slots` → `create_event`.

Both write tools default to `dry_run=true`: the first call previews, a second
call with `dry_run=false` commits.

## Setup

### 1. Create a Google OAuth client

1. In the [Google Cloud console](https://console.cloud.google.com/), create (or
   pick) a project and enable the **Google Calendar API**.
2. Configure the OAuth consent screen as **External** → **Testing**, and add
   your own Google account under **Test users**.
3. Create an **OAuth client ID** of type **Desktop app**. Note the client ID
   and client secret.

Scopes requested (and nothing more):

| Scope | Why |
| --- | --- |
| `calendar.readonly` | `list_calendars`, `search_events`, `get_event`, `find_free_slots` |
| `calendar.events` | `create_event`, `update_event` |

The broader `.../auth/calendar` scope is deliberately *not* requested: it would
also permit deleting whole calendars and rewriting sharing ACLs, which no tool
here does.

### 2. Install

```bash
uv venv && uv pip install -e ".[dev]"
```

### 3. Provide credentials (environment only — never committed)

```bash
cp .env.example .env     # then edit
export GOOGLE_OAUTH_CLIENT_ID='...apps.googleusercontent.com'
export GOOGLE_OAUTH_CLIENT_SECRET='...'
```

### 4. Authorize once

```bash
uv run gcal-mcp-login            # opens a browser, caches the token
uv run gcal-mcp-login --status    # shows expiry and granted scopes
```

The token is cached at `~/.config/gcal-mcp/token.json` (mode `0600`), override
with `GCAL_MCP_TOKEN_PATH`. It is outside the repo and gitignored either way.

### 5. Run the server

```bash
uv run gcal-mcp
```

That single command is the documented entry point; `python -m gcal_mcp` is
equivalent.

## Register with Claude Code

```bash
cp .mcp.json.example .mcp.json   # .mcp.json is gitignored
# fill in your client id/secret, then:
claude mcp list
```

Or without a config file:

```bash
claude mcp add google-calendar \
  --env GOOGLE_OAUTH_CLIENT_ID=... \
  --env GOOGLE_OAUTH_CLIENT_SECRET=... \
  -- uv --directory /absolute/path/to/Google_MCP_Server run gcal-mcp
```

## Tests

```bash
uv run pytest -q
```

The suite drives the server **through the MCP protocol** over a real stdio
subprocess (`tests/test_protocol.py`), against a stub Google Calendar API and a
stub token endpoint, so no network access or real account is needed. See
`writeup.md` for the end-to-end transcript and the demonstrated failure path.

## Environment variables

| Variable | Required | Purpose |
| --- | --- | --- |
| `GOOGLE_OAUTH_CLIENT_ID` | yes | OAuth client id |
| `GOOGLE_OAUTH_CLIENT_SECRET` | yes | OAuth client secret |
| `GCAL_MCP_TOKEN_PATH` | no | Token cache location |
| `GCAL_MCP_API_BASE` | no | Calendar API base URL (tests point this at a stub) |
| `GCAL_MCP_TOKEN_URI` | no | Token endpoint (tests point this at a stub) |
| `GCAL_MCP_AUTH_URI` | no | Authorization endpoint |
