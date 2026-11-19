# Grant Radar — User Guide

Grant Radar is a funding-opportunity tracker for a research group: a table of calls you curate by hand, plus a radar that watches the funders' sites on its own and files what it finds as **proposals**. Nothing a machine writes reaches the table until a person has read the field-by-field diff and clicked Approve. It replaces a shared list that nobody has time to keep true — deadlines pass, links die, the call that would have fit is found three weeks late.

---

## 1. Getting in

`grantradar.borant.eu/` is a public showcase and never asks who is reading it, so it shows no counts and no grants. The table lives at `/app`, behind the login.

There is **no self-registration** — the app has no sign-up form at all, because a funding table for one institute has no reason to accept strangers. Ask Spit for access: either an account is created for you in the admin panel, or your existing Borant ID gets you in with the single login you already use for the other borant tools. One click on *Open the table* covers both cases.

Two **roles**, and the difference is what each can spend:

| | reader | admin |
|---|---|---|
| Read the table, filter it, export it | yes | yes |
| Read the proposals queue and the sources | yes | yes |
| Add, edit or delete a grant | no | yes |
| Approve or reject a proposal | no | yes |
| Add sources, run a scan, check links | no | yes |
| Manage users and API keys | no | yes |

Everyone starts as **reader**, deliberately: the two buttons that call the language model — *Scan now* and a grant's *check* — spend on the server's API key with no per-user ceiling, so a new account cannot reach them until someone decides it should.

## 2. The grants table

One **grant** is one row, and one row is one funding scheme — not one call. When the 2027 edition of a recurring scheme opens, the radar updates the deadline on the existing row rather than adding a second one; with a few dozen schemes a two-level model would be overhead, and the history of past editions lives in the approved proposals instead.

What a row holds: **name** (the only required field), **funder**, **scope** (free text — the topic, the eligibility that matters), **max amount** and **duration in months** (both free text, because funders write them in incompatible ways), **link**, **notes**, **grant start**, and **type** — a free-text label with suggestions already in use (Project, Postdoc, PI, Network, PhD, Other) rather than a fixed vocabulary, since a scheme that fits none of them should not be forced into Other.

Deadlines are two fields on purpose. **Deadline (text)** is what the funder actually says — "Rolling", "1 Oct 2026", "two calls per year". **Deadline (date)** is the next known deadline in ISO form, and it is the one the tool computes on: the table sorts by it with undated rows last, past dates show in red, and it is what the deadline filter reads. **Recurrence** carries the pattern in words ("annual, usually October") so an expired date is still informative.

**Status** is `open`, `closed` or `archived`. You set it by hand, with one exception: a row whose **Recurring** field says `no` — a one-off or final call — is closed automatically a week after its deadline, and the closure is recorded in the approved proposals with the method `auto_close`. A row marked `yes`, or not marked at all, is never closed for you: a recurring scheme past its deadline is waiting for its next date. The status filter adds a computed fifth view: **open** means status open *and* the deadline is in the future or absent, **expired** means status open but the deadline has passed — a row nobody has triaged yet. That is where the radar's homework shows up.

Filters combine — full-text search (name, scope, notes, funder), funder, type, source, status — and every change re-queries the table without reloading the page. The filter state is in the URL, so a filtered view is a bookmark you can share. **Export to Excel** exports the current filter, not the whole table.

Click any row to open its **detail card**: every field, plus the record's provenance — which source watches it, whether it arrived by hand or through the queue, when the link was last fetched, when the language model last verified it. Clicks on the link and on the buttons are excluded from the trigger, so the modal does not fight the row.

A 📡 next to a name means the row **entered through the proposal queue** rather than being typed into the form. It does not say what filed the proposal — the proposals page says that.

## 3. Filing a grant by hand

*+ New grant* (admin) is the same form as edit. Fill what you know and leave the rest empty; the radar can propose the gaps later. Two fields are worth the extra minute: **link**, because without it the nightly monitor and the per-grant check have nothing to fetch and the row is invisible to both, and **deadline (date)**, because a row without one sorts to the bottom and never appears in an upcoming-deadlines answer.

Manual edits leave no history — only the record's created and updated timestamps. If you want a change to be on record with a reason, that is what the proposal queue is for.

*Delete grant* is permanent and takes the grant's proposals with it, so the audit trail for that scheme disappears too. Prefer `archived` for a scheme that is over: it keeps the row and takes it out of the nightly link checks.

## 4. Sources

A **source** is a place the radar is told to look: a name, a funding page URL, and a line of **hints** in free text describing what matters and what to ignore. The hints go into the scanner's prompt verbatim, so this is the one knob that tunes what comes back — "calls relevant for bioethics / digital health / AI ethics; ignore artist residencies" does more work than a longer source list.

Sources can be switched off without being deleted, which is how you park one that produces noise while keeping what it already found.

Two things about what makes a source useful, both learned in production. The scanner works by **web search**, so a rich directory behind a paywall or a login is not a source no matter how much it holds — only public, indexed call pages are reachable. And a hint that is narrower than the funder is a hint that excludes by construction: a Horizon Europe source scoped to one cluster silently hides every relevant topic in the others.

The **scan log** at the bottom of the page is the honest record: when each sweep ran, against which source, and what came out of it. A source that says "never" under *Last scan* has never been swept, whatever else the page shows.

## 5. The radar

Three discovery processes, all inside the app, none of them able to write to the table. The one thing that does change a row without you is not discovery but a rule: the nightly auto-close of one-off calls, described under Status above.

**Link monitor** — nightly, no language model, no cost. It fetches the link of every open grant (closed and archived rows are skipped, as are rows without one), stores a hash of the readable text, and files a `flag` proposal when a link stops answering or when the page's text changed since the last check. One failed fetch is not a dead link: a link is marked dead, and flagged, after three failed nights in a row, and it is flagged once per outage — acknowledging the flag does not bring it back the next night. Cosmetic changes are stripped before hashing, so a flag means the content moved. The first check of a new row only records the hash — you get a flag from the second night onwards.

**Semantic scan** — monthly (first of the month, 04:00 Zurich), plus *Scan now* on the sources page, for all enabled sources or one. For each source, the model searches the live web, compares what it finds against the digest of the grants that source already watches, and files `new` and `update` proposals with a rationale, the page it read, and a confidence of high, medium or low. It is instructed to be conservative and to mark uncertainty as low confidence, because a wrong proposal costs review time. It is also scoped: the prompt describes an institute working on bioethics, digital health, AI ethics, public health and empirical ethics in Zurich, and calls outside that are meant to be skipped.

**Per-grant check** — the *check* button on a row and *Check now* in the detail card. The model fetches that grant's page (searching for the current official page if the link died or moved), compares every stored field, and either stamps the row as verified — visible as *LLM check* in the detail card — or files an `update` proposal with only the fields that should change. An empty diff never becomes a proposal, and a second check while an update is already pending does not file a duplicate. It takes a minute or two and runs in the background; you can leave the page.

## 6. The proposals queue

The queue is the boundary between what the machine may do and what only a person may do. Every proposal carries four chips — its **kind**, its **confidence**, the **method** that produced it (`llm_scan` for the monthly sweep, `llm_check` for a per-grant verification, `link_monitor` for a flag, `ono_mcp` for something an assistant filed), and its rationale with the source URL. For an update, the diff is computed against the row as it stands right now and shows current against proposed, field by field.

What Approve does depends on the kind:

- **new** — inserts the grant, marks its origin as arrived-through-the-queue, and attaches the source the proposal belonged to.
- **update** — writes only the fields in the payload onto the existing row, and lets the row inherit the proposal's source if it had none.
- **flag** — writes nothing at all. Approving a flag acknowledges it; the decision it points at (edit the link, close the scheme, ignore it) is a separate action of yours.

Reject writes nothing, ever. A decided proposal is never rewritten and stays readable under the *approved* and *rejected* filters — that is the tool's trail, and the only place where the reason for a change is recorded. One thing does remove it: deleting the grant deletes its proposals too (§3), so `archived` is what keeps the trail of a scheme that is over.

One consequence to know: rejecting is not a blacklist. Duplicate suppression looks only at what is still pending, so a call you rejected as out of scope can be proposed again by a later sweep. If you never want to see it again, the durable fix is a hint on the source that excludes it.

## 7. What Grant Radar is not

It is a radar, not a database of calls. It knows what someone put in the table and what a sweep of the configured sources happened to surface — so **a call missing from the table is not evidence that no such call exists**. Three reasons in the code, and each one is a real gap:

- The radar only looks where you point it. A funder with no source is never searched.
- A sweep is a sample. Each source gets a bounded number of web searches and a bounded number of turns per scan; when the budget runs out the sweep ends, and an empty result and an exhausted search look identical from the outside.
- The monthly cadence is a month. A call that opens and closes inside four weeks can be missed entirely; the *Scan now* button exists for when you have a reason to look today.

It also does not track your applications. There is no notion of who is applying, no status pipeline, no team — that was deliberately postponed, and for now the deadline you are working towards lives in your calendar, not here. And there is no email alerting: nothing will tell you a deadline is close unless you look, or ask.

## 8. Working from a chat (MCP)

If you work with an AI assistant, Grant Radar plugs into it, and this is the surface where the tool earns its keep — "what closes in the next two months that a postdoc could apply to" is a question you ask in a sentence and answer badly with filters.

**Getting a key.** Ask an admin to generate one in the admin panel; it is shown once. Paste the endpoint `https://grantradar.borant.eu/mcp` and the key into your client's MCP settings as an `X-API-Key` header. Clients that cannot set headers can use `https://grantradar.borant.eu/mcp/k/<your-key>/` instead — the key sits in the URL there, where access logs may keep it, so take one key per client and revoke rather than share.

**What the assistant can do.** Search the tracked grants with the same filters as the table (including the deadline-aware `open` and `expired`), read one grant in full, list the deadlines falling inside a window of days you choose, read the pending queue, list the configured sources, and file a proposal — a new grant or an update to one — which lands in the same queue as everything else, tagged `ono_mcp`, waiting for the same human click.

**What it cannot do.** Approve or reject anything: approval exists only as a button in the web UI, on every surface, by design. It also cannot edit the table directly, add or change a source, or start a scan.

**What it reaches.** A key is access, not an identity — it is not tied to your account, and it opens everything the table holds to whichever model that client talks to. Treat handing one out as the same decision as sending the table itself.

For scripts rather than chats, `GET /ono/grants` with the same key returns a compact JSON dump of the table.

## 9. Data protection

The grants table holds public information about funding schemes — no participants, no patients, no applicants. The only personal data in the database is your **account row**: username, email address, a password hash, and the Borant ID subject if you arrive through the single login. Deleting an account in Borant ID does not delete it here; an admin deactivates it in the admin panel.

Three things do leave the machine, and it is worth knowing which:

- The **monthly scan** sends the source's name, URL and hints, plus a digest of the grants that source watches, to Anthropic's API. The digest does not include the Notes field.
- The **per-grant check** sends the whole stored record of that one grant, Notes included, to the same API. So Notes are not a private scratchpad — keep names, contacts and anything confidential out of them, and put the internal judgement somewhere else.
- The **link monitor** only fetches the call's own page, identifying itself as Grant Radar. Nothing else leaves.

## 10. Good practices

- **Triage the `expired` view, not the inbox.** It is the shortest list in the tool and the only one that means a decision is overdue: either the scheme has a next edition and the date needs updating, or it is over and belongs in `closed`. Setting **Recurring** on a row is what lets the second kind close by itself next time.
- **Spend your effort on hints, not on sources.** A precise line about what the group can actually apply to improves every future sweep; a fifteenth source mostly adds proposals to reject.
- **Check before you write a proposal, not after you have planned around a row.** A deadline in this table was true when someone last looked. If a call is about to shape a month of your work, hit *check* and read the page yourself.
- **Approve the diff, not the rationale.** The rationale is the model's argument and it is usually good; the diff is what will be true tomorrow. Read the fields.
- **Reject with a hint.** A rejection alone will come back next month. Correcting the source's hints is what makes a judgement stick.
- **A radar tells you where to look.** The decision whether a call is worth a proposal is not in the table and was never meant to be.
