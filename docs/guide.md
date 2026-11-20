# Grant Radar — User Guide

Grant Radar is a funding-opportunity tracker: a table of calls you curate by hand, plus a radar that watches the funders' sites on its own and files what it finds as **proposals**. Nothing a machine finds reaches the table until a person has read the field-by-field diff and clicked Approve. It replaces a shared list that nobody has time to keep true — deadlines pass, links die, the call that would have fit is found three weeks late.

It is organised in **workspaces**. A research group has one, and so do you: each holds its own table, its own sources and its own description of what it is looking for.

---

## 1. Getting in

`grantradar.borant.eu/` is a public showcase and never asks who is reading it, so it shows no counts and no grants. The tables live behind the login: `/app` takes you to the workspace you were last in.

There is **no self-registration** — the app has no sign-up form at all. Ask Spit for access: your Borant ID gets you in with the single login you already use for the other borant tools. One click on *Open the table* does it.

The first time you sign in you land in your **personal workspace**, which is empty. To see a group's table, an admin of that workspace has to add you to it; you appear in their list as soon as you have signed in once.

## 2. Workspaces

Everything lives in a workspace: its grants, its sources, its proposal queue and its settings. **Nothing is shared between two** — the same call tracked by two workspaces is two rows, each checked and refreshed on its own. You move between the ones you belong to with the selector in the header, and every address carries the workspace's name: `/w/ibme/app`.

There are two kinds. A **personal** workspace is created for you the first time you sign in; it is yours alone and takes no other members. A **shared** workspace is created by a platform administrator, who names its first admin; from then on its admins manage it.

Three **roles**, held per workspace:

| | read | write | admin |
|---|---|---|---|
| Read the table, filter it, export it | yes | yes | yes |
| Read the proposals queue, the sources, the settings | yes | yes | yes |
| Add, edit or delete a grant; run a check on one | no | yes | yes |
| Approve or reject a proposal | no | yes | yes |
| Add or edit sources, run a scan, check links, import sources | no | no | yes |
| Change the research interests, the members, who pays | no | no | yes |

Not being a member is not a fourth role: a workspace you do not belong to answers "not found", exactly like one that does not exist. In your personal workspace you are the admin.

A grant can be **copied** into another workspace you can write to, from its detail card. The copy is independent from then on: editing one does not touch the other.

There is also a **platform** role, which is not a workspace role. A platform administrator creates shared workspaces and manages accounts, and sees the list of all workspaces — but is not thereby a member of any, and cannot open one that is not theirs.

### Research interests and fit

Each workspace describes itself in its **settings**: who the group is, where it is based, what it works on, what it is looking for and what it is not. The scan reads that text and nothing else to decide what is relevant. It is worth writing carefully, and worth saying what you *cannot* apply to — a constraint written there ("no positions that require relocating") does more than any number of rejected proposals.

Against that text every call gets a **fit**: `high`, `medium` or `low`. Fit is not the same as a proposal's **confidence**. Confidence says how well the facts were verified; fit says how much the call concerns you. A well-documented call that barely touches your work is high confidence and low fit; a fellowship made for you whose next call is not yet announced is low confidence and high fit. The table shows fit as a chip and can be filtered by it, and you can set or correct it by hand in the grant's form.

If you want the scale to mean something specific — say, `high` only for calls you could lead — write that in the settings too: the scan follows it.

### Who pays for the scans

Scans and checks call a language model, and the bill goes to a person. Each workspace names one of its admins as the one who pays, and the scan runs on **that person's own Anthropic API key**. There is no key belonging to the server.

You save your key on your own page — your name in the header. It is stored encrypted, shown afterwards only as its last four characters, never sent to a browser or to an assistant, and sent to nobody but Anthropic. The same page lists which workspaces you are paying for.

A workspace whose paying admin has no key is not scanned, and its pages say so, naming who has to act. The same banner appears when a key is rejected or the account behind it runs out of credit. In your personal workspace the payer is you.

Each workspace also has a switch for the **monthly scan**. Switch it off where you want to scan only when you ask for it — for instance on a workspace that mirrors another one's sources, so the same pages are not paid for twice.

## 3. The grants table

One **grant** is one row. For a recurring scheme the row is the scheme, not one call: when the next edition opens, the radar updates the deadline on the existing row rather than adding a second one, and the history of past editions lives in the approved proposals. A one-off call — a single Horizon topic, a national programme with one deadline — is a row of its own that ends when the call does.

What a row holds: **name** (the only required field), **funder**, **scope** (free text — the topic, the eligibility that matters), **max amount** and **duration in months** (both free text, because funders write them in incompatible ways), **link**, **notes**, **grant start**, **fit**, and **type** — a free-text label with suggestions already in use (Project, Postdoc, PI, Network, PhD, Other) rather than a fixed vocabulary, since a scheme that fits none of them should not be forced into Other.

Deadlines are two fields on purpose. **Deadline (text)** is what the funder actually says — "Rolling", "1 Oct 2026", "two calls per year". **Deadline (date)** is the next known deadline in ISO form, and it is the one the tool computes on: the table sorts by it with undated rows last, past dates show in red, and it is what the deadline filter reads. For a two-step call, the date is the first step you cannot skip. **Recurrence** carries the pattern in words ("annual, usually October") so an expired date is still informative.

**Recurring** is a separate yes/no, and it decides what happens after the deadline. A row marked `no` — a one-off or final call — is **closed automatically** a week after its deadline, and the closure is recorded among the approved proposals with the method `auto_close`. A row marked `yes`, or not marked at all, is never closed for you: a recurring scheme past its deadline is waiting for its next date.

**Status** is `open`, `closed` or `archived`. Apart from that automatic closure, you set it by hand. The status filter adds a computed view: **open** means status open *and* the deadline is in the future or absent, **expired** means status open but the deadline has passed — a row waiting either for its next date or for someone to close it. Closed and archived rows are shown dimmed.

Filters combine — full-text search (name, scope, notes, funder), funder, type, source, fit, status — and every change re-queries the table without reloading the page. The filter state is in the URL, so a filtered view is a bookmark you can share with someone in the same workspace. **Export to Excel** exports the current filter, not the whole table.

In the table, notes, scope and recurrence are cut to two or three lines. Click any row to open its **detail card**: every field in full, plus the record's provenance — which source watches it, whether it arrived by hand or through the queue, when the link was last fetched, when the language model last verified it — and the button to copy it elsewhere. On a phone each row is a card, and scope and amount are left to the detail card.

A 📡 next to a name means the row **entered through the proposal queue** rather than being typed into the form. It does not say what filed the proposal — the proposals page says that.

## 4. Filing a grant by hand

*+ New grant* (write) is the same form as edit. Fill what you know and leave the rest empty; the radar can propose the gaps later. Three fields are worth the extra minute: **link**, because without it the nightly monitor and the per-grant check have nothing to fetch; **deadline (date)**, because a row without one sorts to the bottom and never appears in an upcoming-deadlines answer; and **recurring**, because it is what lets a finished call close by itself.

Manual edits leave no history — only the record's created and updated timestamps. If you want a change to be on record with a reason, that is what the proposal queue is for.

*Delete grant* is permanent and takes the grant's proposals with it, so the audit trail for that scheme disappears too. Prefer `closed` or `archived` for a scheme that is over: either keeps the row and takes it out of the nightly link checks.

## 5. Sources

A **source** is a place the radar is told to look: a name, a funding page URL, and **hints** in free text describing what matters and what to ignore. The hints go into the scanner's prompt verbatim. Sources belong to a workspace, and only its admins change them.

The division of labour between the two texts matters. The workspace's **research interests** say who you are and what counts as relevant, once, for every source. A source's **hints** say where on that funder's site to look and what is particular to it — which schemes exist, how its deadlines work, who may lead. Two things learned the hard way:

- **A hint that says "report X" gets X reported**, relevant or not. Name the schemes as places to look, and leave the judgement to the research interests. If a scheme is of no use to you, say it is out of scope.
- **A hint narrower than the funder excludes by construction**: a Horizon Europe source scoped to one cluster silently hides every relevant topic in the others.

The scanner works by **web search**, so a rich directory behind a paywall or a login is not a source no matter how much it holds — only public, indexed call pages are reachable. For the same reason the URL does not have to be the exact page: the funder's site is enough for the scan to find its way.

Sources can be switched off without being deleted, which is how you park one that produces noise, or that no longer concerns you, while keeping the grants it already found attached to it.

**Moving sources between workspaces.** The sources page exports the workspace's sources as a JSON file — name, URL, hints, on or off — and imports such a file. Sources are matched by name, so importing the same file twice updates and adds nothing. It is the way to start a new workspace from an existing list. The file also carries the exporting workspace's research interests, for reference; importing never applies them.

The **scan log** at the bottom of the page is the honest record: when each sweep ran, against which source, and what came out of it — how many proposals, or the error. A source that says "never" under *Last scan* has never been swept, whatever else the page shows.

## 6. The radar

Three discovery processes, all inside the app, none of them able to write to the table. The one thing that does change a row without you is not discovery but a rule: the nightly auto-close of one-off calls, described in §3.

**Link monitor** — nightly, no language model, no cost. It fetches the link of every open grant in every workspace (closed and archived rows are skipped, as are rows without a link), stores a hash of the readable text, and files a `flag` proposal when a link stops answering or when the page's text changed since the last check. One failed fetch is not a dead link: a link is marked dead, and flagged, after three failed nights in a row, and it is flagged once per outage — acknowledging the flag does not bring it back the next night. Cosmetic changes are stripped before hashing. The first check of a new row only records the hash.

**Semantic scan** — monthly (first of the month, 04:00 Zurich) for the workspaces that take part, plus *Scan now* on the sources page, for all enabled sources or one. For each source, the model reads the workspace's research interests, searches the live web, compares what it finds against the grants that source already watches, and files `new` and `update` proposals with a rationale, the page it read, a confidence and a fit. It proposes a status change when a scheme has ended, and fills in *recurring* where it can establish it. It is instructed to be conservative, and not to propose a new call it would rate a low fit unless the source's hints ask for it.

Only one scan runs at a time in the whole app. If one is running, a second *Scan now* does nothing, and the sources page says a scan is in progress.

**Per-grant check** — the *check* button on a row and *Check now* in the detail card. The model fetches that grant's page (searching for the current official page if the link died or moved), compares every stored field, and either stamps the row as verified — visible as *LLM check* in the detail card — or files an `update` proposal with only the fields that should change. An empty diff never becomes a proposal, and a second check while an update is already pending does not file a duplicate. It takes a minute or two and runs in the background; you can leave the page.

Both the scan and the check run on the key of the workspace's paying admin (§2). A scan that fails — no key, a rejected key, no credit left — shows as a banner on every page of that workspace until a scan of that source succeeds.

## 7. The proposals queue

The queue is the boundary between what the machine may do and what only a person may do. Every proposal carries its **kind**, its **confidence**, the **method** that produced it (`llm_scan` for a sweep, `llm_check` for a per-grant verification, `link_monitor` for a flag, `ono_mcp` for something an assistant filed, `auto_close` for the nightly rule), and its rationale with the source URL. For an update, the diff is computed against the row as it stands right now and shows current against proposed, field by field. For a new grant there is nothing to compare with, and the proposal lists the fields it would create.

What Approve does depends on the kind:

- **new** — inserts the grant in this workspace, marks its origin as arrived-through-the-queue, and attaches the source the proposal belonged to.
- **update** — writes only the fields in the payload onto the existing row, and lets the row inherit the proposal's source if it had none. An update can carry a status, so approving one can close a grant.
- **flag** — writes nothing at all. Approving a flag acknowledges it; the decision it points at (edit the link, close the scheme, ignore it) is a separate action of yours.

Reject writes nothing, ever. A decided proposal is never rewritten and stays readable under the *approved* and *rejected* filters — that is the tool's trail, and the only place where the reason for a change is recorded. One thing does remove it: deleting the grant deletes its proposals too (§4).

Two consequences to know. **Rejecting is not a blacklist**: duplicate suppression looks only at what is still pending, so a call you rejected as out of scope can be proposed again by a later sweep. If you never want to see it again, the durable fix is in the source's hints or in the research interests. And **a pending update blocks further updates to the same grant**: while one waits, a scan that finds a newer date for that row drops it. Clear the queue before a scan you care about.

## 8. What Grant Radar is not

It is a radar, not a database of calls. It knows what someone put in the table and what a sweep of the configured sources happened to surface — so **a call missing from the table is not evidence that no such call exists**. Three reasons in the code, and each one is a real gap:

- The radar only looks where you point it. A funder with no source is never searched.
- A sweep is a sample. Each source gets a bounded number of web searches and a bounded number of turns per scan; when the budget runs out the sweep ends, and an empty result and an exhausted search look identical from the outside.
- The monthly cadence is a month. A call that opens and closes inside four weeks can be missed entirely; the *Scan now* button exists for when you have a reason to look today.

It does not share anything between workspaces either. A call your group tracks is not in your personal workspace unless you copy it there, and a correction made in one is not made in the other.

It also does not track your applications. There is no notion of who is applying, no status pipeline, no team — that was deliberately postponed, and for now the deadline you are working towards lives in your calendar, not here. And there is no email alerting: nothing will tell you a deadline is close unless you look, or ask.

## 9. Working from a chat (MCP)

If you work with an AI assistant, Grant Radar plugs into it, and this is the surface where the tool earns its keep — "what closes in the next two months that I could lead" is a question you ask in a sentence and answer badly with filters.

**Getting a key.** Generate one yourself on your own page — your name in the header; it is shown once. Paste the endpoint `https://grantradar.borant.eu/mcp` and the key into your client's MCP settings as an `X-API-Key` header. Clients that cannot set headers can use `https://grantradar.borant.eu/mcp/k/<your-key>/` instead — the key sits in the URL there, where access logs may keep it, so take one key per client and revoke rather than share.

**What it reaches.** A key speaks for you: it reaches the workspaces you are a member of, with your role in each, and nothing else. Revoking it closes all of them at once. Handing a key to a client is handing it everything you can read.

**What the assistant can do.** List your workspaces, with the research interests of each; search a workspace's grants with the same filters as the table (including the deadline-aware `open` and `expired`, and the fit); read one grant in full; list the deadlines falling inside a window of days you choose; read the queue; list the sources and whether their last scan succeeded; and, where you have the write role, file a proposal — a new grant or an update to one — which lands in that workspace's queue tagged `ono_mcp`, waiting for the same human click. The assistant names the workspace it means on every call; left unnamed, it is your personal one.

**What it cannot do.** Approve or reject anything: approval exists only as a button in the web UI, by design. It also cannot edit the table directly, add or change a source, change the settings, or start a scan. And it never sees an Anthropic key.

For scripts rather than chats, `GET /ono/grants?workspace=<name>` with the same key returns a compact JSON dump of that workspace's table.

## 10. Data protection

The grants table holds public information about funding schemes — no participants, no patients, no applicants. The personal data in the database is your **account row** — username, email address, a password hash, the Borant ID subject — your workspace memberships, and, if you saved one, your **Anthropic API key**, encrypted with a secret that exists only in the server's environment. Deleting an account in Borant ID does not delete it here; a platform administrator deactivates it.

A workspace's **research interests** can be personal: in a personal workspace they describe one person's career, eligibility and plans. They are visible to the members of that workspace, and they are sent to the language model on every scan and check.

Three things leave the machine, and it is worth knowing which:

- The **scan** sends the workspace's research interests, the source's name, URL and hints, and a digest of the grants that source watches, to Anthropic's API, on the paying admin's key. The digest does not include the Notes field.
- The **per-grant check** sends the research interests and the whole stored record of that one grant, Notes included, to the same API. So Notes are not a private scratchpad — keep names, contacts and anything confidential out of them.
- The **link monitor** only fetches the call's own page, identifying itself as Grant Radar. Nothing else leaves.

## 11. Good practices

- **Write the research interests before adding sources.** They decide what every sweep brings back. Say what you can lead, what you can only join, and what is excluded.
- **Triage the `expired` view, not the inbox.** It is the short list that means a decision is overdue: either the scheme has a next edition and the date needs updating, or it is over. Setting **Recurring** on a row is what lets the second kind close by itself next time.
- **Clear the queue before a scan.** A pending update on a grant makes the scan drop a newer one for the same row.
- **Scan one source first.** After changing the interests, the hints or the key, *scan* on a single row costs little and shows whether the result is what you meant.
- **Check before you plan around a row.** A deadline in this table was true when someone last looked. If a call is about to shape a month of your work, hit *check* and read the page yourself.
- **Approve the diff, not the rationale.** The rationale is the model's argument and it is usually good; the diff is what will be true tomorrow. Read the fields.
- **Reject with a hint.** A rejection alone will come back next month. Correcting the source's hints, or the research interests, is what makes a judgement stick.
- **A radar tells you where to look.** The decision whether a call is worth a proposal is not in the table and was never meant to be.
