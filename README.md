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
