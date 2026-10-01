# Week 2 Writeup — Google Calendar MCP Server

**API:** Google Calendar v3 (OAuth 2.0 authorization-code + PKCE)
**Transport:** stdio · **Framework:** FastMCP · **Run with:** `uv run gcal-mcp`

The assignment asks for the code to live in `week2/`. It lives in this
standalone repository instead, at the root — so where the rubric says
`week2/writeup.md` and `week2/.mcp.json.example`, read `./writeup.md` and
`./.mcp.json.example`.

I picked Google Calendar because it is the API I actually touch every day, which
made it obvious which tools were worth having and which were busywork. A
calendar also forces the interesting parts of this assignment to be real: the
write tool sends mail to other humans, so a brake is not decorative; and the
natural question an agent gets asked ("find me time and book it") only works if
the tools genuinely compose.

---

## Part I — The server

Seven tools, five readers and two writers:

| Tool | Kind | Returns the ids/values that feed… |
| --- | --- | --- |
| `list_calendars` | read | `calendar_id` → every other tool; `access_role` → the write tools |
| `search_events` | read | `event_id` + `calendar_id` → `get_event`, `update_event` |
| `get_event` | read | full detail for the chosen event |
| `find_free_slots` | read | `start` / `suggested_end` → `create_event` verbatim |
| `create_event` | **write** | a new `event_id` → `get_event`, `update_event` |
| `update_event` | **write** | the updated event |
| `check_auth` | read | credential state, for recovering from auth failures |

The composition graph is the design, not a side effect:

```
list_calendars ──calendar_id──┬─> search_events ──event_id──> get_event
                              │                   └─────────> update_event
                              └─> find_free_slots ──start──> create_event ──event_id──┘
```

Two chains are the ones an agent actually walks: *find something and change it*
(`list_calendars → search_events → get_event → update_event`) and *find time and
book it* (`list_calendars → find_free_slots → create_event`). `find_free_slots`
exists specifically so the second chain needs no date arithmetic from the model:
it emits timestamps in exactly the format `create_event` consumes, in the same
UTC offset it was asked in.

**Running it:** `uv venv && uv pip install -e ".[dev]"`, then `uv run gcal-mcp`.
Full setup including the Google Cloud console steps is in `README.md`.

---

## Part II — Agent ergonomics

### Constraints in the schema, not the prose

Every bounded argument is bounded by the JSON Schema the agent receives, so
there is no way for the docstring and the validator to drift apart:

```python
order_by: Annotated[Literal["startTime", "updated"], Field(...)] = "startTime"
max_results: Annotated[int, Field(ge=1, le=100)] = 20
duration_minutes: Annotated[int, Field(ge=5, le=1440)] = 30
send_updates: Annotated[Literal["none", "all", "externalOnly"], Field(...)] = "none"
calendar_ids: Annotated[list[str] | None, Field(max_length=10)] = None
```

`tests/test_errors.py::test_out_of_range_argument_is_caught_by_the_schema`
confirms the payoff: `max_results=5000` is rejected by the protocol layer with
the offending argument named, *before* a line of my code runs. The agent is told
its argument was wrong rather than quietly receiving a different page size.

`tests/test_protocol.py::test_constraints_live_in_the_schema` asserts these
constraints survive all the way out to the wire, so a refactor that moved a
constraint back into prose would fail the suite.

One constraint deliberately is *not* in the schema: timestamp format. JSON
Schema's `format: date-time` is advisory and silently unenforced by most
validators, so `search_events` and `find_free_slots` parse the value themselves
and return `invalid_argument` naming the exact argument — a timestamp with no
UTC offset is rejected rather than guessed at (see B-series note below).

### Shaped output

No raw Google JSON passes through. Google's `event` resource carries ~40 fields;
`shape_event_summary` keeps 11 and `shape_event_detail` keeps 19. Concretely:

- `etag`, `iCalUID`, `sequence`, `kind`, `created`, `reminders`, `eventType`,
  `htmlLink`-adjacent cruft: dropped. A test asserts `etag` and `iCalUID` never
  reach the agent.
- `start`/`end` are flattened from `{"dateTime": ...} | {"date": ...}` to a
  single string, with the distinction moved to a separate `all_day` boolean.
  Branching on *which key is present* is exactly the kind of thing a model gets
  wrong one call in twenty.
- `calendar_id` is echoed into every event result, so each search hit is
  self-sufficient as the input to the next call — the agent never has to
  remember which calendar it was searching.
- Unbounded fields are capped: descriptions truncate at 600 chars with an
  explicit `… [truncated]` marker, and attendee lists past 25 collapse to
  `attendee_count` plus an `attendees_omitted` note. A 300-person all-hands
  cannot blow the context window.

### Errors are data

A single decorator, `as_data`, wraps every tool and is the one place a raised
error becomes a payload, so no individual tool has to remember:

```json
{"error": {"code": "rate_limited", "message": "...", "retryable": true,
           "retry_after_seconds": 42, "remedy": "Wait for retry_after_seconds,
           then retry the same call.", "calendar_id": "primary"}}
```

`code` is a closed 9-value vocabulary (`ErrorCode`) so the agent can branch on
it, and `retryable` is **derived** from the code rather than set by hand, so the
two can never contradict each other. The ids involved are echoed back, so an
agent that fired three calls knows which one failed.

The mapping from HTTP to that vocabulary is where the judgement lives. Two cases
worth calling out:

- Google reports **quota exhaustion as `403`**, not `429`. A naive mapper calls
  that `permission_denied` and the agent gives up on work it could have
  completed 40 seconds later. So the body's `reason` outranks the status:
  `rateLimitExceeded` / `userRateLimitExceeded` / `quotaExceeded` become
  `rate_limited` with `retryable: true` even on a 403.
- A `404` carries `remedy: "Do not retry with the same ids. Re-resolve them with
  list_calendars or search_events."` — the retry loop an agent would otherwise
  enter is closed off and replaced with the call that actually fixes it.

Both are covered in `tests/test_units.py`. Note also that failures come back
with `is_error: false` — they are *results*, so the agent's turn continues.

### Docstrings as the contract

Every id-shaped argument says where it comes from, in the argument description
*and* in the schema: `event_id` is "The `event_id` from a `search_events`
result"; `calendar_id` is "a `calendar_id` from `list_calendars`, or `'primary'`
for the user's own main calendar". Each docstring's `Returns:` block names the
actual keys, so the agent knows what it is about to parse.

`FastMCP(instructions=...)` carries the workflow that spans tools — the
numbered flow, the timestamp convention, and one rule that no single tool can
express: *if you get `auth_required`, stop and ask the user to run the login
command; do not retry.* Without it an agent will cheerfully retry an auth
failure five times.

The docstrings also record what the tools *don't* do, because that is where an
agent over-promises on a user's behalf: `find_free_slots` says plainly that it
ignores working hours and other attendees' time zones.

### A brake on the writes

Both mutating tools take `dry_run: bool = True` — the default is the safe one,
so a write happens only when the agent explicitly asks for it twice:

- `create_event(dry_run=True)` returns `would_create` with a
  `notifies_attendees` flag, plus a `next_step` string naming the exact follow-up
  call. Nothing is sent.
- `update_event(dry_run=True)` fetches the event first and returns `current`
  beside `changes`, so an overwrite can be reviewed before the old value is
  gone.

`send_updates` defaults to `"none"`, so a misunderstood instruction stays
private instead of mailing fifty people, and its description tells the agent to
ask before using `"all"`. Annotations back this up: `readOnlyHint: true` on all
five readers, `idempotentHint: false` on `create_event` (calling it twice makes
two events, and the docstring says so and tells the agent to check with
`search_events` before retrying), `destructiveHint: true` on `update_event`.

---

## Part III — OAuth

### The flow

`gcal-mcp-login` runs a full authorization-code exchange: PKCE (S256), an
ephemeral loopback redirect on `127.0.0.1`, and `state` verified on return. The
token lands in `~/.config/gcal-mcp/token.json`, written atomically at mode
`0600` (asserted by a test).

`access_type=offline` with `prompt=consent` is deliberate: without the latter,
Google omits the refresh token on re-authorization, and refresh silently stops
working weeks later.

### Caching and silent refresh

`CredentialProvider` holds the token in memory and on disk, and refreshes it
**120 seconds before** the recorded expiry — not at expiry, so a token cannot
die in the gap between the freshness check and the API call. Refresh is guarded
by an `asyncio.Lock`, so ten concurrent tool calls that all see a stale token
perform one refresh, not ten. Google omits `refresh_token` from refresh
responses, so the existing grant is carried forward; a test asserts it survives.

There is a second, independent path: any request that comes back `401` triggers
one forced refresh and one retry. The proactive skew handles the predictable
case; the 401 retry handles the token being invalidated early. Transcript A
below runs with a deliberately **expired** cached token, and the agent never
sees it — step 1 just works.

### Minimal scopes

| Scope | Used by | Why not more |
| --- | --- | --- |
| `calendar.readonly` | `list_calendars`, `search_events`, `get_event`, `find_free_slots` | Read-only; cannot create or modify anything |
| `calendar.events` | `create_event`, `update_event` | Events only — cannot delete a calendar or rewrite its sharing ACL |

The broad `.../auth/calendar` scope is **not** requested. It would grant
calendar deletion and ACL editing, and no tool here does either, so asking for
it would be asking the user to trust code that has no use for the trust. Nothing
in the server deletes an event, which is why there is no `delete_event` tool and
no scope that implies one.

### No secrets in the repo

Client id and secret are read from the environment, from the first commit.
`.gitignore` covers `.env`, `.mcp.json`, `token.json`, `*.token.json` and
`client_secret*.json`; the committed files are `.env.example` and
`.mcp.json.example` with placeholder values. The token cache lives outside the
repo tree by default.

### The token dying mid-session

This is the case the split between `login_cli.py` and `CredentialProvider`
exists for. A stdio server has no terminal and no user attention, so it must
never block a tool call on a browser. When the grant is revoked, the refresh
fails with `invalid_grant` and the tool returns:

```json
{"error": {"code": "auth_expired", "retryable": false,
  "message": "Google refused to refresh the cached token (invalid_grant); access was revoked or the grant expired.",
  "remedy": "Ask the user to run `uv run gcal-mcp-login` in a terminal, then retry. This server will not open a browser during a tool call."}}
```

`retryable: false` stops the retry loop; the `remedy` hands the agent something
to say to the user. `check_auth(verify=true)` then lets the agent confirm the
re-login worked before resuming real work, without spending a Calendar call.
See B4 below for the whole recovery arc.

---

## Part IV — Integration and demonstration

### Registration

`.mcp.json.example` is committed; copy to `.mcp.json` (gitignored) and fill in
the client credentials, or:

```bash
claude mcp add google-calendar \
  --env GOOGLE_OAUTH_CLIENT_ID=... --env GOOGLE_OAUTH_CLIENT_SECRET=... \
  -- uv --directory /absolute/path/to/Google_MCP_Server run gcal-mcp
```

### Protocol-level test

`tests/test_protocol.py` and `tests/test_errors.py` never import a tool
function. They launch the server as a **real subprocess** (`python -m gcal_mcp`)
and speak MCP over stdio through a FastMCP client, asserting on `list_tools`
output (names, annotations, schemas) and on `tools/call` results. Only Google is
stubbed (`tests/fake_google.py`), so the suite is hermetic — no network, no real
account — while the code under test is unmodified. `tests/test_units.py` covers
the interval arithmetic and HTTP-status mapping underneath.

```
$ uv run pytest -q
..................................                                       [100%]
34 passed in 22.98s
```

### Transcripts

Both transcripts below were captured by `scripts/capture_transcript.py`, which
drives the unmodified server over stdio via the MCP protocol; every argument and
result shown is traffic that really happened. Google is the test stub, so the
capture is reproducible on any machine:

```bash
uv run python scripts/capture_transcript.py
```

---

### Transcript A — chained read + write

> **Prompt:** *“Find my design review on Oct 1 and tell me who's on it, then book me a 45-minute prep block before it on my own calendar.”*

The cached access token is seeded **already expired**, so step 1 also exercises the silent refresh: the agent never sees it.

**`list_calendars`** — arguments:
```json
{
  "max_results": 25
}
```
result:
```json
{
  "calendars": [
    {
      "calendar_id": "primary",
      "summary": "Albert Li",
      "time_zone": "America/Los_Angeles",
      "primary": true,
      "access_role": "owner"
    },
    {
      "calendar_id": "team@group.calendar.google.com",
      "summary": "Team Calendar",
      "time_zone": "America/Los_Angeles",
      "primary": false,
      "access_role": "reader"
    }
  ],
  "count": 2
}
```

**`search_events`** — arguments:
```json
{
  "calendar_id": "primary",
  "query": "design review",
  "time_min": "2026-10-01T00:00:00-07:00",
  "time_max": "2026-10-02T00:00:00-07:00"
}
```
result:
```json
{
  "events": [
    {
      "event_id": "evt_review_002",
      "calendar_id": "primary",
      "summary": "Design review",
      "start": "2026-10-01T10:00:00-07:00",
      "end": "2026-10-01T12:00:00-07:00",
      "all_day": false,
      "location": "Room 4",
      "status": "confirmed",
      "organizer": "dana@example.com",
      "attendee_count": 1,
      "recurring": false
    }
  ],
  "count": 1,
  "truncated": false
}
```

**`get_event`** — arguments:
```json
{
  "calendar_id": "primary",
  "event_id": "evt_review_002"
}
```
result:
```json
{
  "event": {
    "event_id": "evt_review_002",
    "calendar_id": "primary",
    "summary": "Design review",
    "start": "2026-10-01T10:00:00-07:00",
    "end": "2026-10-01T12:00:00-07:00",
    "all_day": false,
    "location": "Room 4",
    "status": "confirmed",
    "organizer": "dana@example.com",
    "attendee_count": 1,
    "recurring": false,
    "attendees": [
      {
        "email": "albert@example.com",
        "response_status": "accepted",
        "optional": false
      }
    ],
    "description": "",
    "time_zone": "",
    "conference_url": "",
    "recurrence": [],
    "recurring_event_id": "",
    "html_link": "https://calendar.google.com/event?eid=evt_review_002",
    "updated": "",
    "organizer_is_self": false
  }
}
```

**`find_free_slots`** — arguments:
```json
{
  "time_min": "2026-10-01T08:00:00-07:00",
  "time_max": "2026-10-01T10:00:00-07:00",
  "duration_minutes": 45,
  "calendar_ids": [
    "primary"
  ]
}
```
result:
```json
{
  "slots": [
    {
      "start": "2026-10-01T08:00:00-07:00",
      "end": "2026-10-01T09:00:00-07:00",
      "gap_minutes": 60,
      "suggested_end": "2026-10-01T08:45:00-07:00"
    },
    {
      "start": "2026-10-01T09:15:00-07:00",
      "end": "2026-10-01T10:00:00-07:00",
      "gap_minutes": 45,
      "suggested_end": "2026-10-01T10:00:00-07:00"
    }
  ],
  "count": 2,
  "window": {
    "time_min": "2026-10-01T08:00:00-07:00",
    "time_max": "2026-10-01T10:00:00-07:00",
    "duration_minutes": 45,
    "calendars_checked": [
      "primary"
    ]
  },
  "warnings": []
}
```

**`create_event`** — arguments:
```json
{
  "calendar_id": "primary",
  "summary": "Prep: design review",
  "start": "2026-10-01T08:00:00-07:00",
  "end": "2026-10-01T08:45:00-07:00",
  "description": "Read the deck before the review."
}
```
result:
```json
{
  "dry_run": true,
  "would_create": {
    "calendar_id": "primary",
    "summary": "Prep: design review",
    "start": "2026-10-01T08:00:00-07:00",
    "end": "2026-10-01T08:45:00-07:00",
    "location": "",
    "description": "Read the deck before the review.",
    "attendee_emails": [],
    "notifies_attendees": false
  },
  "next_step": "Nothing was written. Repeat this call with identical arguments and dry_run=false to create the event."
}
```

**`create_event`** — arguments:
```json
{
  "calendar_id": "primary",
  "summary": "Prep: design review",
  "start": "2026-10-01T08:00:00-07:00",
  "end": "2026-10-01T08:45:00-07:00",
  "description": "Read the deck before the review.",
  "dry_run": false
}
```
result:
```json
{
  "created": true,
  "event": {
    "event_id": "evt_created_001",
    "calendar_id": "primary",
    "summary": "Prep: design review",
    "start": "2026-10-01T08:00:00-07:00",
    "end": "2026-10-01T08:45:00-07:00",
    "all_day": false,
    "location": "",
    "status": "confirmed",
    "organizer": "albert@example.com",
    "attendee_count": 0,
    "recurring": false,
    "attendees": [],
    "description": "Read the deck before the review.",
    "time_zone": "",
    "conference_url": "",
    "recurrence": [],
    "recurring_event_id": "",
    "html_link": "https://calendar.google.com/event?eid=evt_created_001",
    "updated": "",
    "organizer_is_self": true
  }
}
```

Events actually created on the calendar: `['evt_created_001']`

### Transcript B — failure paths

**B1. A hallucinated event id.** Non-retryable, with a way back:

**`get_event`** — arguments:
```json
{
  "calendar_id": "primary",
  "event_id": "evt_design_review_oct1"
}
```
result:
```json
{
  "error": {
    "code": "not_found",
    "message": "Nothing found to get event 'evt_design_review_oct1': Event not found: evt_design_review_oct1.",
    "retryable": false,
    "remedy": "Do not retry with the same ids. Re-resolve them with list_calendars or search_events.",
    "calendar_id": "primary",
    "event_id": "evt_design_review_oct1"
  }
}
```

**B2. A write to a calendar the user can only read:**

**`create_event`** — arguments:
```json
{
  "calendar_id": "team@group.calendar.google.com",
  "summary": "Team offsite",
  "start": "2026-10-01T13:00:00-07:00",
  "end": "2026-10-01T14:00:00-07:00",
  "dry_run": false
}
```
result:
```json
{
  "error": {
    "code": "permission_denied",
    "message": "Not allowed to create an event on calendar 'team@group.calendar.google.com': You need to have writer access to this calendar..",
    "retryable": false,
    "remedy": "The signed-in account may lack write access to this calendar, or the granted scopes are read-only. Check access_role from list_calendars.",
    "calendar_id": "team@group.calendar.google.com"
  }
}
```

**B3. A rate limit.** Retryable, with Google's own delay:

**`search_events`** — arguments:
```json
{
  "calendar_id": "primary"
}
```
result:
```json
{
  "error": {
    "code": "rate_limited",
    "message": "Google rate-limited the request to search events on calendar 'primary': Rate Limit Exceeded.",
    "retryable": true,
    "retry_after_seconds": 42,
    "remedy": "Wait for retry_after_seconds, then retry the same call.",
    "calendar_id": "primary"
  }
}
```

**B4. The OAuth grant revoked mid-session.**

The access token is expired and the user has revoked the app in their Google account settings, so the refresh fails. The server returns an instruction instead of trying to open a browser:

**`list_calendars`** — arguments:
```json
{}
```
result:
```json
{
  "error": {
    "code": "auth_expired",
    "message": "Google refused to refresh the cached token (invalid_grant); access was revoked or the grant expired.",
    "retryable": false,
    "remedy": "Ask the user to run `uv run gcal-mcp-login` in a terminal, then retry. This server will not open a browser during a tool call."
  }
}
```

The agent then confirms the credential state directly, without spending a Calendar call:

**`check_auth`** — arguments:
```json
{
  "verify": true
}
```
result:
```json
{
  "authorized": false,
  "grant_valid": false,
  "token_path": "/Users/albertli/MCP_Server/.transcript-token.json",
  "oauth_client_configured": true,
  "token_state": "stale",
  "scopes_requested": [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events"
  ],
  "scopes_granted": [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events"
  ],
  "detail": "Cached access token is stale; it will be refreshed silently on the next call. Google refused to refresh the cached token (invalid_grant); access was revoked or the grant expired."
}
```

After the user runs `gcal-mcp-login`, the same check turns green and the agent knows it is safe to retry:

**`check_auth`** — arguments:
```json
{
  "verify": true
}
```
result:
```json
{
  "authorized": true,
  "grant_valid": true,
  "token_path": "/Users/albertli/MCP_Server/.transcript-token.json",
  "oauth_client_configured": true,
  "token_state": "fresh",
  "scopes_requested": [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events"
  ],
  "scopes_granted": [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events"
  ],
  "detail": "Cached access token is stale; it will be refreshed silently on the next call. Google accepted the refresh token."
}
```

**`list_calendars`** — arguments:
```json
{
  "max_results": 5
}
```
result:
```json
{
  "calendars": [
    {
      "calendar_id": "primary",
      "summary": "Albert Li",
      "time_zone": "America/Los_Angeles",
      "primary": true,
      "access_role": "owner"
    },
    {
      "calendar_id": "team@group.calendar.google.com",
      "summary": "Team Calendar",
      "time_zone": "America/Los_Angeles",
      "primary": false,
      "access_role": "reader"
    }
  ],
  "count": 2
}
```

---

## What the transcripts show

**Transcript A (chaining).** The agent walks four tools' outputs into the next
tool's inputs with no reformatting anywhere:

1. `list_calendars` → picks the entry with `primary: true`, and sees
   `access_role: "owner"` so it knows the write will be allowed.
2. `search_events` → `evt_review_002`, returned *with* its `calendar_id`.
3. `get_event` on that pair → the attendee list the user asked about.
4. `find_free_slots` with `time_max` set to the event's own `start` value from
   step 2 — one tool's output used directly as another's bound.
5. `create_event` with the slot's `start` and `suggested_end` passed through
   verbatim: preview first, then the same call with `dry_run=false`.

The cached access token was seeded **already expired**. The refresh happened
inside step 1 and appears nowhere in the transcript, which is the entire point.

**Transcript B (failures).** Four provoked failures, each returning a different
actionable shape:

| | Provocation | Code | `retryable` | What the agent does next |
| --- | --- | --- | --- | --- |
| B1 | Invented event id | `not_found` | `false` | Re-run `search_events` (the `remedy` says so) |
| B2 | Write to a `reader` calendar | `permission_denied` | `false` | Check `access_role`, pick another calendar |
| B3 | Google rate limit | `rate_limited` | `true` | Wait 42s and retry the identical call |
| B4 | Grant revoked mid-session | `auth_expired` | `false` | Tell the user to run `gcal-mcp-login`, then `check_auth(verify=true)` |

B1 is the failure I care most about, because it is the one an agent causes
itself. `evt_design_review_oct1` is a plausible-looking id a model might
construct from an event's title rather than carry from a search result. The
response names both ids back, says `retryable: false`, and names the tool that
produces real ids — so the cost is one wasted call, not a retry loop and not a
dead turn.

B4 shows the full recovery arc: the error tells the agent what to ask the user
for, `check_auth(verify=true)` reports `grant_valid: false` while access is still
revoked, flips to `grant_valid: true` once the grant is restored, and the
following `list_calendars` succeeds. The server never tried to open a browser.

### Verifying against a real Google account

The transcripts above are hermetic by design, so a grader can reproduce them.
The real-account path is the same code with two env vars unset — after
`gcal-mcp-login`, `GCAL_MCP_API_BASE` and `GCAL_MCP_TOKEN_URI` simply default to
Google's own endpoints. To confirm end to end:

```bash
export GOOGLE_OAUTH_CLIENT_ID=... GOOGLE_OAUTH_CLIENT_SECRET=...
uv run gcal-mcp-login && uv run gcal-mcp-login --status
# then, in Claude Code with the server registered:
#   "What's on my calendar tomorrow, and find me 45 minutes before my first meeting."
```

---

## Known limits

- **No pagination.** `search_events` returns one page and sets
  `truncated: true` rather than following `nextPageToken`. An agent that needs
  more should narrow the window, which is almost always the better move; a
  `page_token` argument would be the honest fix.
- **No delete, and no `delete` scope to match.** Deliberate: the destructive
  operation an agent is most likely to get wrong is the one it cannot perform.
- **`find_free_slots` is busy/free only.** It ignores working hours, lunch, and
  other attendees' time zones. The docstring says so, because an agent that
  assumes otherwise will propose 7am meetings.
- **Recurring series are edited one occurrence at a time.** `update_event` on an
  expanded occurrence changes only that occurrence; editing a whole series would
  need the series id and a separate tool.
- **`check_auth` without `verify` cannot see a revoked grant** — only a round
  trip to Google can. That is why `verify` exists, and why the default answer
  reports `grant_valid: null` instead of claiming confidence it does not have.

## Cleanup after grading

```bash
claude mcp remove google-calendar      # or delete the .mcp.json entry
rm ~/.config/gcal-mcp/token.json       # delete the cached token
# then revoke the app at https://myaccount.google.com/permissions
```
