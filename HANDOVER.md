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
| Driver | `/r/ID?d=SECRET` | Advance/stop cues on one rundown, nothing else |
| Viewer | `/r/ID` | Watch, nothing else |

Authority is checked server-side with `secrets.compare_digest` on every write.
The WebSocket carries no authority at all — it only ever receives pings, and
all edits go over HTTP where the keys get checked. Don't "improve" this by
accepting edits over the socket.

### The driver tier, added 2026-07-26

Driving and editing were one permission; they're now two, because the person
calling the show often isn't the person who built it. The split is enforced
by *endpoint*, not by request-body filtering:

- `PUT /api/rundowns/{id}` — the existing full-document save. Still requires
  edit or owner authority. Unchanged.
- `PUT /api/rundowns/{id}/live` — accepts edit, owner, *or* drive authority,
  and can only ever write `data["live"]`. It reads the stored document,
  replaces that one field, and writes it back. **A drive key cannot smuggle a
  content change through this endpoint even by sending one** — extra keys in
  the body are simply never read. That property is the whole point of the
  separate endpoint; if you ever refactor these two together, you lose it.

Other things that follow from the split, and shouldn't be undone:

- `strip_for_viewer()` keys off `can_edit_row`, not `can_drive_row`. A driver
  is *not* an editor, so **a driver does not receive the Notes column** — same
  as a plain viewer. Tested; see below.
- `driveKey` is only returned by `GET /api/rundowns/{id}` to someone who can
  edit. A driver can't read their own key back out and re-share it upward.
- Cue changes now broadcast as `{"type":"live"}` carrying only the cue
  pointer, instead of riding along in the full `"update"` payload. The
  operator's own browser now *applies* these (it used to ignore broadcasts and
  trust local state) — necessary because a driver on another device can now
  move the cue, and the operator has to see it happen.
- `drive_key` is nullable and backfilled lazily by
  `get_or_create_drive_key()`. Rundowns that existed before this feature get a
  key the first time anyone asks for one. There's no migration step, matching
  the rest of the project — `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` in
  `SCHEMA` handles the existing-deployment case, since `CREATE TABLE IF NOT
  EXISTS` is a no-op there.
- Restoring from archive mints a fresh drive key alongside the fresh edit key
  — that's the only revocation path there is, same as before.

**Last write wins still applies.** Two drivers on the same link, or a driver
and an operator both hitting Go, will fight. Single caller by design.

---

## Decisions, and what was rejected

This is the section that matters. Each of these was argued and settled.

### Actual-duration history, added 2026-08-08

"Show the time used for each past session" — scoped down to per-segment,
after-the-fact, not a live stopwatch overlay. A cue's actual runtime is
logged the moment it stops being live: taken over by the next cue, stopped
outright, or deleted while live. All three paths funnel through one
function, `recordHistory()`, called *before* `doc.live` is cleared or
reassigned — never after, since `recordHistory()` reads `doc.live.startedAt`
to compute the elapsed time.

Why this piggybacks on the drive-tier `/live` endpoint instead of getting
its own: a driver, not just an editor, ends cues — so whatever logs the
actual duration has to be reachable with drive authority. `PUT
/api/rundowns/{id}/live` already had that authority check and already
existed specifically to accept narrow, whitelisted fields; `history` became
a second whitelisted field alongside `live`, not a new endpoint. Re-verified
the narrow-endpoint property still holds with `history` added: a
drive-authority request carrying a content-hijack payload (title, notes,
shareNotes, startEpoch all altered) still only ever changes `live` and
`history` server-side — every hijacked field came back unchanged. If you
ever merge `/live` and the full-document `PUT` together, this guarantee is
what you'd be giving up.

Things that matter if this gets touched again:

- **The badge shows that segment's own raw time-on-air, e.g. "21:05" against
  a 20:00 plan — not a delta, and not a running total.** Went through two
  other shapes the same day before landing here, worth knowing if it comes
  up again: first shipped as `+MM:SS`/`−MM:SS` (the delta); changed to
  `cumulativeShift()`, a running sum of every prior cue's delta, on the
  theory that "are we on schedule right now" mattered more than any one
  segment's own number; reverted within the hour when the actual ask turned
  out to be simpler — just the clock time that segment took, full stop, no
  arithmetic against other segments. `cumulativeShift()` was deleted rather
  than left dead. `actualBadge()` still colors the tag the way Drift already
  is (over its own plan → `--live` red, under → `--ok` green) so an
  over/under segment is visible without reading the number, and the
  planned-vs-actual delta is still available on hover (`title=`) — it's
  just not the headline figure anymore. This is also, conveniently, exactly
  what the CSV's `Actual` column already stores (see below); the two were
  briefly out of sync during the cumulative detour and are back in step now.
- **Only the most recent run of a cue counts.** `latestActual()` filters
  `doc.history` to one `itemId` and reads the last entry. Jumping back to a
  cue via its row's ▶ and running it again pushes a *new* history entry
  rather than overwriting the old one — full history is kept for the CSV
  archive, but the on-screen badge always reflects the latest attempt, not
  the first.
- **CSV export gained a 7th column, `Actual`, appended at the end — not
  inserted.** Both `to_csv()` in `app/main.py` and the `btnCsv` handler in
  `show.html` do this identically and deliberately: the paste-import parser
  reads Start/Length/Type/Segment/Who/Notes by fixed position (0-5), so an
  Actual column inserted earlier would silently misalign every field on
  re-import of an exported file. `Actual` is also always ignored on import,
  same as `Start` — it's derived, not authored.
- **The Python and JS duration formatters must stay identical.** `fmt_dur()`
  (`app/main.py`) and `fmtDur()` (`show.html`) both zero-pad minutes
  (`04:40`, not `4:40`) and both switch to `H:MM:SS` past an hour. A CSV
  archived by the server and a badge rendered in the browser need to read
  the same way, or "the time we exported" stops matching "the time we saw
  live."

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
- **Live Google Sheets connection (OAuth)** — considered when Sheets import
  was requested, rejected the same day. An OAuth app, per-operator stored
  tokens, and a hard dependency on Google's API staying reachable is a lot of
  permanent surface area for a feature used a handful of times per event.
  Paste-import (see below) gets the same job done with zero credentials and
  zero new dependencies — the entire feature is string parsing plus a diff.

---

### Sheets import (paste + diff), added 2026-07-26

"Import" and "detect changes and update" turned out to be the same feature,
not two: paste is diffed against `doc.items` using a classic LCS match keyed
on `(type, title)`, and importing into an empty rundown is just the case
where everything comes back as "added." There's a preview step
(`diffAgainstDoc()` → `renderImportPreview()`) before anything touches the
live document — added/changed/removed, with per-field before→after on
changed rows.

Things that matter if this gets touched again:

- **Matching is positional-aware, not a naive title lookup.** The real KUK
  Jakarta rundown has three cues literally named "Break." A hashmap keyed on
  title would collide; LCS aligns repeated equal elements in the order they
  appear in both sequences, so the 1st "Break" in the paste matches the 1st
  "Break" already in the rundown, not whichever one a lookup happened to
  find. Tested directly — see `mergeDraftIntoItem`/`diffAgainstDoc` tests
  before this shipped.
- **Existing item ids are preserved on a match**, only the changed fields get
  overwritten. This is why an in-progress live cue survives a content
  update — `doc.live.itemId` still points at something real after import,
  instead of the whole document getting swapped for freshly-generated ids.
- **Day breaks are structurally excluded from the diff**, not just
  unsupported in the parser. `splitDayBreaks()`/`reinsertDayBreaks()` pull
  them out before comparison and stitch them back at the same relative
  position (anchored to the id of whichever item preceded them) afterward.
  Without this, every existing day break would show up as "removed" on
  every single import, since the paste format has no date field to express
  one. If you ever add a "Type: day" column to the export/import format,
  this whole carve-out can go away — until then, don't let day items
  anywhere near the diff.
- **Start is parsed but discarded.** It's a derived field everywhere else in
  the app (`plannedStarts()` computes it from `startEpoch` + cumulative
  durations); accepting it from a paste and treating it as authoritative
  would silently reintroduce the exact per-item-wall-clock model the
  timezone fix removed. Length is what's real; Start is just re-derived
  after import the same as after any manual edit.

---

### Multi-day events, added 2026-07-25

Originally the recommendation was one rundown per day — the data model only
had a single `startEpoch`, so a multi-day event meant either re-entering
cumulative offsets across days (unusable) or juggling several separate
rundowns and edit links. Built properly instead: a new `"day"` item type
(alongside existing `"cue"` and `"heading"`) that resets the running clock to
an explicit date + time rather than continuing from the previous item's
duration.

What changed, if you touch this again:

- `static/show.html`: `dayEpoch()` computes the reset point; `plannedStarts()`
  applies it inline; `currentDaySegment()` scopes the "Show left" clock (both
  the standby total and the live countdown) to the segment between the
  nearest passed day-break and the next one, so day 3's cues don't get added
  into day 1's remaining-time figure. The day-break row itself has its own
  date/time inputs (`f-day-date`, `f-day-time`) instead of a duration field.
- `app/main.py`: `day_epoch_ms()` is the Python mirror of `dayEpoch()`, used
  by `to_csv()` so archived/exported CSVs reset correctly at each day break
  too. Kept deliberately tolerant of malformed dates — falls back to holding
  the previous clock position rather than raising, since a CSV export should
  never hard-fail on bad data.
- Day breaks inherit the single-timezone assumption the rest of the show
  already makes (see "Drift assumes one timezone" below) — a date/time
  entered from a different device/timezone than the show's `tz` will be
  wrong in the same way changing "Show starts" from a different device
  would be. Not a new limitation, just the existing one applied per day.
- Reordering (`up`/`down`/`dup`) treats a day break like any other row —
  moving cues across a day boundary is just moving them in the array, no
  special-casing. This is intentional: which day a cue "belongs to" is
  purely positional.

---

## Known limits

- ~~**Drift assumes one timezone.**~~ **Fixed 2026-07-25.** Show start is now
  `startEpoch` (absolute ms since epoch, set from the operator's own clock at
  the moment they set/change "Show starts") plus `tz` (IANA zone name), not a
  bare "HH:MM" re-interpreted per-viewer. Every viewer's browser formats that
  same instant via `Intl.DateTimeFormat({timeZone: doc.tz})`, so cue times and
  drift now read the same for the operator in Singapore and a viewer anywhere
  else. Old rundowns (pre-fix) have no `startEpoch`; the client backfills it
  from the operator's clock the first time they open the show and re-saves —
  until then those specific rundowns still use the old per-viewer math. The
  Python CSV export (`to_csv` in `app/main.py`) has the matching two paths.
  If you touch this again: `plannedStarts()` and `fmtClock()` in
  `static/show.html`, and `to_csv`/`fmt_clock_epoch` in `app/main.py`, need to
  move together.
- ~~**Shows crossing midnight** produce nonsense start times.~~ **Fixed as a
  side effect** of the above — cumulative durations add to a real epoch
  instead of wrapping mod 86400, so there's no rollover discontinuity to get
  wrong.
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
- **driver tier (2026-07-26):** a drive key cannot edit content; a drive key
  posting a content-hijack payload to `/live` has every content field
  discarded while its cue change still applies; a drive key receives no Notes
  and no `driveKey`; viewer and wrong-key both 403 on `/live`; an edit key
  passed in the `d=` slot does not authorise (and vice versa); empty/null keys
  never authorise. Run these against the real DB before trusting a deploy —
  they were verified against the authorisation layer directly, since no
  Postgres was available in the environment where the tier was built.
- edit key and operator key both authorise writes
- archive → viewer link returns 410 → restore → re-archive → permanent delete
- export zip returns a valid archive
- WebSocket connects and broadcasts viewer counts
- **actual-duration history (2026-08-08):** a drive-authority hijack payload
  to `/live` carrying `history` alongside altered title/notes/shareNotes/
  startEpoch leaves everything but `live` and `history` unchanged;
  malformed `history` (not a list) 400s; wrong/viewer key still 403s on the
  now-larger endpoint surface. On the frontend: `recordHistory()` +
  `latestActual()` + `actualBadge()` tested via a Node stub-DOM harness
  extracting the real functions out of `show.html` — the badge's visible
  text is the raw actual time (not the delta, which only appears on hover),
  over/under coloring is right in both directions, a never-run cue renders
  no badge, an earlier segment's overrun does NOT bleed into a later
  segment's own badge, and `fmtDur` stays consistent. The existing Sheets
  import/diff test suite was re-run after this change and still passes in
  full — nothing in the render/CSV path regressed it.

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
