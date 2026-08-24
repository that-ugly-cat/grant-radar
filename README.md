# Grant Radar

Funding-opportunity tracker with built-in LLM discovery. Replaces a shared
SharePoint list: a grants table you edit by hand, plus a radar that finds new
calls and updates on its own — and puts everything in a review queue instead of
touching your data.

## How it works

- **Grants table** — FastAPI + SQLite + HTMX. Filters, deadline sort, manual edit.
- **Discovery, two engines, both in-app:**
  - *Link monitor* (nightly, no LLM): fetches every grant link, flags dead links
    and changed pages.
  - *Semantic scan* (monthly, or "Scan now"): one Claude call per source
    (`claude-opus-5` + server-side web search). The model compares what it finds
    against the current table and proposes `new` / `update` entries.
- **Proposals queue** — nothing written by machines reaches the grants table
  without a human clicking Approve on a field-by-field diff. Decided proposals
  are never deleted: they are the audit log of the radar.
- **Sources** — the radar only looks where you point it: each source has a URL
  and free-text hints for the scanner.
- **API for agents** — MCP (streamable HTTP) at `/mcp` with tools
  `search_grants`, `get_grant`, `upcoming_deadlines`, `list_proposals`,
  `propose_grant`, `propose_update`. Auth via `X-API-Key` header **or**
  capability URL `/mcp/k/{key}` for clients that can't send headers.
  REST: `GET /ono/grants` (compact dump) + minimal JSON CRUD under `/api`.
  Proposal approval is deliberately not exposed to the API.

## Run

```bash
cp .env.example .env   # set ANTHROPIC_API_KEY, JWT_SECRET, ADMIN_PASSWORD
docker compose up --build -d
```

App on `127.0.0.1:8015`, DB persisted in `./data/`. First start creates the
admin user from `ADMIN_USERNAME` / `ADMIN_PASSWORD`.

### Seed from CSV

```bash
docker exec grantradar python scripts/seed_from_csv.py /data/grants.csv
```

(copy the CSV into `./data/` first). Expected columns: `Grant name, Funder,
Scope/topic, Max amount, Duration (months), Deadline, Link, Notes, Grant start`.

### Dev, no Docker

```bash
pip install -r requirements.txt
GR_SCHEDULER=0 uvicorn app.main:app --reload --port 8015
```

## Deploy (borant)

Clone in `/opt/apps/grantradar`, create `.env`, `docker compose up --build -d`,
then Caddy: `grantradar.borant.eu → reverse_proxy localhost:8015`.

Update: `git pull && docker compose up --build -d` (the DB lives in `./data/`,
untouched by pulls; schema init is idempotent).

## Authentication: two modes

The app authenticates on its own by default and never needs an identity
provider. `AUTH_MODE=gateway` is the other mode, for a deploy that sits behind
an SSO gate speaking the `X-Borant-*` header contract.

```
AUTH_MODE=local     (default)   email + password against the users table
AUTH_MODE=gateway               the upstream gate vouches via X-Borant-Sub
```

`local` is the default deliberately. An app that believes an identity header
with no gate in front of it lets anyone be anyone, so the gateway path stays
dead code until someone turns it on. In `gateway` the app additionally checks
that the request came from `BORANT_TRUSTED_PROXY` — under Docker that is a
bridge gateway and **not** `127.0.0.1`, and it is read off the app's log after
a real request rather than deduced from the network layout.

Three things that do not change in `gateway`:

- **The machine surfaces keep their own keys.** `/mcp`, `/mcp/k/{key}`,
  `/api/*` and `/ono/grants` authenticate with the revocable `api_keys` table,
  because a model client has no browser and no cookie. They belong outside any
  gate.
- **Local passwords stay populated.** That is what makes flipping back to
  `local` a working way home. A user who only ever arrived through the gate is
  given a random local password nobody knows; an admin can set a real one from
  `/admin`.
- **A role that spends is never provisioned quietly.** `POST /scan-now` and
  `POST /grants/{id}/check` draw on the server's Anthropic key with no
  per-user ceiling, and `admin` is the role that reaches them. The gate's
  `X-Borant-Hint` may carry `admin` and it is honoured — but only at profile
  creation, only from the trusted proxy, and only with a warning naming the
  address and the subject. An unrecognised hint is treated as a typo and falls
  back to `reader` rather than inventing a role.

Linking existing accounts to gate subjects is a one-off manual step, run before
the mode is flipped:

```
docker exec grantradar python scripts/map_borant.py --report
docker exec grantradar python scripts/map_borant.py --map you@example.org=01ABC...
```

Rollback is two independent moves: `AUTH_MODE=local` plus
`docker compose up -d` restores the app as it was, and dropping the gate's
block from the reverse proxy removes the redirect to its login.
