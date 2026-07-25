# Rundown — handover

**Status:** built, tested against a real Postgres, not yet deployed.
**Stack:** FastAPI + Postgres + WebSocket, targeting Railway.
**Code:** `rundown-railway.zip` — this document lives in its root as `HANDOVER.md`.

Read `README.md` for what the thing does and how to deploy it. Read this for
why it's shaped the way it is, and what not to undo.

---

## What it is

A run-of-show tool, modelled on rundownstudio.app. One operator builds a running
order and drives it live; anyone with a viewer link watches the clock move in
real time. Intended for the operator's own teaching and workshop sessions, not
as a product.

The core mechanic: enter a segment length, every downstream start time
recalculates. Press `Space` to take the next cue live. The active cue counts
down and then counts into negative red. A tally bar down the left edge shows
live / next / done at a glance.

---

## File map

```
app/main.py         API, WebSocket hub, archiving, CSV + zip export
static/index.html   landing — mints an operator key
static/studio.html  your rundowns, archives, export-everything
static/show.html    the rundown itself; operator and viewer are the same file
static/style.css    shared design tokens
requirements.txt    fastapi, uvicorn[standard], asyncpg
Procfile            uvicorn app.main:app --host 0.0.0.0 --port $PORT
railway.json        Nixpacks build, health check on /api/health
```

Two Postgres tables, created on first boot, no migration step: `rundowns` and
`archives`.

---

## The access model

No accounts, no passwords. Three kinds of link:

| Link | Shape | Can do |
|---|---|---|
| Studio | `/studio?o=SECRET` | Everything you own |
| Edit | `/r/ID?k=SECRET` | Edit and drive one rundown |
| Viewer | `/r/ID` | Watch, nothing else |

Authority is checked server-side with `secrets.compare_digest` on every write.
The WebSocket carries no authority at all — it only ever receives pings, and
all edits go over HTTP where the keys get checked. Don't "improve" this by
accepting edits over the socket.

---

## Decisions, and what was rejected

This is the section that matters. Each of these was argued and settled.

### Firebase was evaluated and dropped

Firebase Realtime Database was built first and works. It was replaced by
Railway + Postgres for two reasons that don't go away:

1. Scheduled cleanup needs Cloud Functions, which needs the Blaze billing plan.
   The moment billing is on, Firebase stops being the simple option.
2. With a public viewer link, Firebase clients read the whole node — meaning
   every viewer could read the Notes column, and no rule could prevent it.
   On our own server we simply don't send that field.

Also: Python, Postgres, and Railway were already in use here. Two backends is
worse than one, and the second is always the one nobody remembers how to fix.

The Firebase build has been deleted. Don't resurrect it without re-reading
the two points above.

### Nothing expires on a timer — this is deliberate

An early draft had 30-day expiry with an APScheduler sweep. It was removed on
request, and APScheduler was dropped from the dependencies with it.

The reasoning for keeping it removed: an automated deletion is a bet that
someone is watching. Nobody is. Instead:

- **Archive** — manual, reversible, keeps CSV *and* original JSON.
- **Delete** — only on already-archived rundowns, requires typing the show
  name, permanent. It is the only irreversible action in the application.
- **Download everything** — zip of every rundown you own, active and archived.

An earlier proposal to email a CSV before deletion was rejected: a once-only
email, 30 days after last contact, into an unwatched mailbox, from a free tier
that may have changed. It fails silently and you find out too late. Archiving
makes it unnecessary — there is nothing to rescue.

**Also rejected: deleting the previous event when a new one starts.** That
destroys last month's record at exactly the moment a client asks about it.

### The operator link was added unprompted

"Download everything" needs a definition of *everything*, and the only one
available was browser storage — the exact fragility we were removing. So one
secret URL owns all your shows.

**The cost, stated plainly: lose that bookmark and there is no recovery.** No
email, no reset. This was a considered trade against building real accounts.

### Countdowns run locally, not streamed

The network carries "cue 7 went live at 14:03:22". Each viewer's browser counts
down from there. This keeps the clock smooth on bad wifi.

The failure mode is deliberate and worth knowing: **a disconnected viewer keeps
confidently counting down a segment you've already left.** There's a warning
banner when the socket drops, but the numbers keep moving. Chosen because a
stuttering clock is worse for a workshop audience than a slightly stale one.
For broadcast, invert this.

### Deliberately not built

- **Drag-and-drop reordering** — arrow buttons instead. Reliable beats fluid
  when you're operating live.
- **Colour-coded cues** — costs legibility in a dim room.
- **Teleprompter** — a separate tool; cramming it in makes both worse.
- **Real accounts** — see open threads.

---

## Known limits

- **Drift assumes one timezone.** Show start times are wall-clock strings in the
  operator's timezone. A viewer elsewhere sees a wrong drift figure; cue name
  and countdown stay correct, so it degrades gracefully. Fix: store the start
  as a real timestamp. **This is the most likely thing to bite first**, given
  the operator is in Singapore working with an Indonesian community.
- **Shows crossing midnight** produce nonsense start times. Same root cause,
  same fix.
- **Device clocks matter.** A viewer five minutes off sees a five-minute error.
- **Last write wins.** Two people on the same edit link overwrite each other
  silently. Single operator by design.
- **Single container.** A Railway restart mid-show drops the WebSocket rooms;
  clients reconnect automatically, but there's a gap.

---

## Open threads

**Multiple editors — raised, then cancelled mid-discussion.** Worth knowing what
was established before it stopped:

- A second *independent* account already works — visit `/` and create another
  studio link. No code needed.
- Handing one show to one person already works — that's the per-rundown edit
  link.
- What genuinely doesn't exist: two people safely co-editing one rundown
  (last write wins would clobber them), revoking one person without cutting off
  everyone, and any record of who changed what.
- The recommendation on the table was **not** real accounts. It was a small
  table of named, revocable editor keys — same practical result, none of the
  password-reset and support burden. Roughly 80 lines.

Nothing was built for this. Nothing is half-finished.

---

## Testing

Verified end-to-end against a real Postgres, not just compiled. The cases that
were actually run:

- viewer link cannot write (403), wrong edit key cannot write (403)
- another operator's key cannot delete your archive (404)
- **Notes are stripped server-side from the viewer payload** — confirmed absent,
  not merely hidden in the UI
- edit key and operator key both authorise writes
- archive → viewer link returns 410 → restore → re-archive → permanent delete
- export zip returns a valid archive
- WebSocket connects and broadcasts viewer counts

If you change the permission logic, re-run these. The notes-stripping one is
the one that silently causes real harm if it regresses.

---

## Deploy

Full steps in `README.md`. The step people skip:

After adding the Postgres service, open the **web** service → Variables →
**Add Reference** → pick Postgres's `DATABASE_URL`. The app refuses to boot
without it, on purpose, with an explicit error in the logs.

Confirm with `GET /api/health` → `{"ok": true, "rundowns": 0, "archived": 0}`.

---

## Handling secrets

Studio and edit links are credentials. Don't paste them into shared documents,
chat threads, or issue trackers — including whatever picks this project up next.
If one leaks: archive and restore the affected rundown, which mints a fresh
edit key. There is no equivalent rotation for a leaked studio link; you'd need
to create a new studio and move the shows.
