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

### Back button, added 2026-08-08

"If Go gets pressed twice, [I] want a button to go back to the previous
segment" — asked live, mid-event. A way to jump to any earlier row already
existed (that row's own ▶ button, via `goToIndex()` — no direction
restriction there ever existed), but scrolling back to find the right row
under time pressure is exactly the kind of thing a dedicated, always-visible
control avoids.

`prevCue()`/`goPrev()` are the exact mirror of the existing
`nextCue()`/`goNext()`, deliberately not a generalized "undo" — a
distinction worth keeping straight if this gets extended:

- **`goPrev()` only acts while a cue is live.** No live cue means no
  "current position" to step back from — unlike `goNext()`, which has a
  special case for starting the show from standby, `goPrev()` just does
  nothing in that state. There's no reasonable "go back" from "nothing is
  happening."
- **At the first cue, it's a no-op**, not a wraparound to the last cue or
  a way to leave the show. Mirrors `nextCue()` returning `-1` off the end
  of the list — the two functions are structurally identical, just walking
  opposite directions over the same `type==="cue"` filter.
- **It's an ordinary `goToIndex()` call under the hood** — same
  `recordHistory()` logging, same narrow `/live` save, same broadcast to
  every connected client. "Going back" writes a fresh history entry for
  whatever cue was live when Back was pressed, same as any other
  cue-to-cue transition. It does **not** delete or rewind that entry —
  if you actually want to erase a stray transition, that's what the reset
  button on the actual-time tag is for (see below), a distinct and
  already-shipped feature, not this one.
- **Available to drivers, not just editors** — gated identically to
  `goNext()`/`stopShow()` (`!canEdit && !canDrive` → return), since
  stepping back doesn't touch the running order, only which cue is live,
  which is squarely within what a driver link is already allowed to do.
- **`Backspace` is the keyboard mirror of `Space`**, with the same
  `e.preventDefault()` treatment `Space` already gets and for the same
  reason: Backspace's browser default is "navigate back a page," which
  would otherwise fire constantly on a driver's tablet, since focus is
  rarely inside a text field between cues.

### Drift == (Est. finish − original finish), confirmed not changed, 2026-08-08

Asked to "count the drift from the original finish time with the est finish
time" — checked before touching anything, because a live event was
reporting a nonsensical `+23:38:05` Drift and the request sounded like it
might fix that. It wouldn't have: `drift` in `tick()` and `estFinish −
segmentPlannedEnd()` are the same number, algebraically, given the app's
existing assumption that everything after the live cue still runs to plan.
Verified numerically (not just by hand) with three scenarios run through
the actual functions, including one shaped exactly like the reported bug —
all three matched to the millisecond. See `drift_equiv_check.mjs`-style
reasoning if this needs re-deriving.

The real cause of the 23-hour figure: `doc.startEpoch` only gets set the
first time a rundown is opened, or when someone explicitly edits "Show
starts" (`$("inStart")`'s `change` handler → `setStartEpoch()`). It is never
silently refreshed to "today." A rundown set up (or last touched) on a
different calendar day than the actual live event keeps the old day as its
anchor, and every planned time — Drift, Est. finish, the CSV archive — is
off by however many days have passed. **This is a real, sharp,
undocumented-until-now edge case, not something either of the two above
formulas can fix**, since both read from the same stale `startEpoch`. The
fix in the moment is operational: re-enter "Show starts" on the day of the
event to force the `change` event and re-anchor. Worth a self-check on load
(warn if the anchor's calendar date doesn't match today) if this recurs —
nothing built for it yet.

### Live-adjusted Start times, added 2026-08-08

"Can the start time of a session adjust according to the real ending time
of the previous session" — asked right after actual-duration history
shipped, and it turns out that feature already had everything this one
needs: `doc.history` records exactly when each cue really started and
ended. The gap was that the Start column never read it — it was purely
`plannedStarts()`, the flat original schedule, forever.

Two design forks were confirmed with the operator before building, since
guessing wrong here means every row's timestamp is wrong, not just one
badge:

1. **Completed segments show their REAL start**, not the original plan.
   `doc.history`'s `startedAt` for a cue's most recent run *is* its Start
   value now — it's not being computed or projected, it's a fact already on
   hand.
2. **Segments that haven't run yet keep shifting, live**, not just once a
   cue formally ends. `displayStarts()` re-runs every 250ms via `tick()`
   alongside Drift and Est. finish, so a segment 8 minutes into a 5-minute
   overrun visibly pushes every later Start back in real time, the same way
   the countdown does — not frozen until someone presses Next.

**`plannedStarts()` was deliberately left untouched — `displayStarts()` is
a new, separate function, only used for the on-screen Start column.**
Three other things still need the flat, non-drifting schedule and would
quietly break if it started moving: Drift (`plannedEpoch` in `tick()` is
"what SHOULD have happened," and drift is measured against it — if the
schedule itself absorbed the drift, Drift would read close to zero forever,
which defeats the number); `segmentPlannedEnd()`, the reference "Est.
finish" measures itself against; and CSV export, which archives what was
*planned*, not a live projection frozen at export time — the `Actual`
column (see below) is where the archive's real timing already lives.
If you ever consider merging these two functions, re-read this paragraph
first.

What `displayStarts()` actually does, cue by cue, in list order:

- **The live cue**: Start = `doc.live.startedAt` (fact, already known).
  Anchor for whatever's next = `now() + max(0, plannedDur − elapsed)` —
  identical arithmetic to `leftEl`/`finishEl` in `tick()`, so a cue running
  long projects its neighbor to start "right now," clamped, never negative.
- **A cue with a history entry, not currently live**: Start = that entry's
  real `startedAt`; anchor for next = that entry's real `endedAt`. This is
  true whether the cue is positionally before or after the live cue — a
  driver jumping backward and re-running an earlier cue updates that cue's
  own Start on its next completion, same as anywhere else; nothing special
  is done to "future" cues that happen to already have history from an
  earlier pass.
- **Anything else (never run, not live)**: falls back to the flat
  plan — current anchor, then anchor advances by the cue's own planned
  duration. Exactly `plannedStarts()`'s behavior, for exactly the part of
  the show that hasn't happened yet.
- **A day break**: still an unconditional reset to its own date/time,
  regardless of how much drift accumulated before it — same as
  `plannedStarts()`. Confirmed with a wildly-overrun cue immediately before
  a day break: the break, and everything after it, ignores that drift
  entirely. This wasn't re-litigated; it's the same reasoning multi-day
  events already established.
- Start cells are rendered as a read-only `<span>` in every role (editor,
  driver, viewer) — never an `<input>` — specifically so `tick()` can
  overwrite their `textContent` every 250ms without any risk of clobbering
  something someone's mid-typing elsewhere in the row.

### Reset button on the actual-time tag, added 2026-08-08

Direct fallout from the stale-`startEpoch` bug two sections up, surfaced by
a real rundown: an operator tested the Go/Stop controls before a live event
(a few seconds each on the first two cues), which logged real history
entries exactly as the feature is supposed to — then those test runs stuck
around, permanently overriding both the actual-time tag and the Start
column for those two rows with test timing instead of the plan, with no
way to undo it short of deleting and recreating the rows.

Confirmed re-entering "Show starts" does **not** fix this — `displayStarts()`
prefers real history over the anchor for any cue that has it, by design
(see two sections up), so the stale test data wins regardless of what the
anchor says. This is the same mechanism, just experienced from the other
side.

`actualBadge()` now renders a real `<button>` instead of a `<span>` when
`canEdit` is true, with `data-act="resethist"` wired into the existing rows
click handler (same delegation pattern as `up`/`down`/`dup`/`del` —
`e.target.closest("button[data-act]")`, then branch on `a`). Clicking it
runs `doc.history = doc.history.filter(h => h.itemId !== thisItemsId)` —
**every** entry for that item, not just the latest; "reset" means "make
this look like it never ran," not "undo one attempt." No `confirm()`
dialog, matching `del` immediately above it in the same handler — this
editor has no undo anywhere else, and a stray click here is no more
consequential than a stray click on delete.

Viewers and drivers still get the plain, inert `<span>` — gated in
`actualBadge()` itself by checking `canEdit` before choosing which tag to
return, not just left to CSS, so a driver's HTML never contains a button
there in the first place. CSS strips all button chrome (`button.actual`)
so it's pixel-identical to the read-only version except for a hover
underline — no new column width, no re-litigating the tools-column overflow
work from earlier in the project.

### Editor auto-scroll on load/reconnect only, added 2026-08-08

Viewers and drivers already auto-scrolled to the live row on every
`render()` — that block deliberately excludes the editor, because the
editor's screen re-renders on every keystroke, and re-centering their
scroll position while they're typing a note on an unrelated row would be
worse than not scrolling at all.

Asked to make the screen "always scroll to the active session when the
screen is refreshed" — confirmed with the operator before building, since
"always" read two very different ways: literally every re-render (would
break editing), or specifically when the page comes back (load, or a
socket reconnect). Went with the latter. New function `scrollToLive()`
holds the same `scrollIntoView` logic already used in two other places
(`goToIndex()`'s own local scroll, and the render()-gated viewer/driver
block) — called from exactly two spots, both editor-only: once at the end
of `loadShow()` (covers a hard page load/refresh), and once in
`ws.onopen()` (covers a dropped-and-restored socket; `onopen` fires on
every automatic reconnect, not just the first connect, since `connect()`
re-runs itself via `setTimeout` in `onclose`). It is **not** called from
`render()` — that's the entire point of building a separate function
instead of just widening the existing gate.

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
- **`startEpoch` doesn't refresh itself.** It's set the first time a rundown
  is opened, or whenever someone explicitly edits "Show starts" — never
  silently re-anchored to today. Set up a rundown one day, run it live on a
  later day without re-touching "Show starts," and every planned time —
  Drift, Est. finish, the whole Start column — is off by however many days
  passed, all at once. Symptom looks alarming (a multi-hour Drift out of
  nowhere) but the fix is one field: re-enter "Show starts" on the day of
  the event, even to the same value, to force the `change` handler and
  re-anchor `startEpoch` to today. Nothing catches this automatically yet —
  a load-time check comparing the anchor's calendar date to today's would
  close it, if this keeps coming up.

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
- **live-adjusted Start times (2026-08-08):** `displayStarts()` matches
  `plannedStarts()` exactly when nothing has run and nothing is live (no
  behavior change before a show starts); a completed cue shows its
  real recorded start and the next cue anchors off its real end, not the
  plan; a live cue shows its own real start and projects a later cue from
  `now() + remaining`; an overrunning live cue clamps that projection to
  "now," never negative; a day break still resets the anchor outright even
  immediately after a wildly-overrun cue, ignoring accumulated drift
  entirely, same as before this feature existed. The Sheets import/diff
  suite was re-run again after this change and still passes in full.
- **Drift == Est. finish delta, verified 2026-08-08 (no code changed):**
  ran the exact `tick()` formulas for both numbers through three scenarios
  — mid-cue not yet overrunning, mid-cue already overrunning its own
  length, and a `startEpoch` anchored a day early — and they matched to
  the millisecond in every case. Confirms the two are algebraically
  identical given the app's existing assumption that everything after the
  live cue runs to plan; recorded so nobody re-derives this from scratch
  next time the same question comes up.
- **editor auto-scroll on load/reconnect (2026-08-08):** `scrollToLive()`
  scrolls to the correct row when a cue is live, does nothing when no cue
  is live, and does nothing when `doc.live.itemId` points at a row that no
  longer exists (e.g. deleted while live) rather than throwing.
- **reset button on the actual-time tag (2026-08-08):** `actualBadge()`
  renders a real `<button>` with `data-act="resethist"` for an editor and a
  plain, inert `<span>` with no `data-act` at all for a non-editor — checked
  directly on the returned HTML, not inferred from CSS. The reset filter
  itself (`doc.history.filter(h => h.itemId !== id)`) was verified to clear
  every entry for the targeted item while leaving another item's history
  completely untouched.
  ⚠️ **Not yet tested against the real backend**: this is a plain content
  edit going through the existing full-document `save()`/`PUT
  /api/rundowns/{id}`, the same path `del`/`dup`/every other row edit
  already uses — should need nothing new server-side — but that path itself
  wasn't re-exercised end-to-end this round (the sandbox's Node test
  environment recycled mid-session and the Sheets import/diff suite file
  was lost with it; nothing touched here overlaps that code, but it means
  this feature has unit coverage only, not an integration re-run). Worth a
  real save-and-reload check before trusting it on the next live event.
- **Back button (2026-08-08):** `prevCue()` mirrors `nextCue()` exactly,
  confirmed by running both over the same heading/cue list. `goPrev()`
  does nothing with no cue live (`doc.live` stays `null`, not just "no
  visible change"), does nothing at the very first cue (still on that same
  cue afterward, not wrapped or cleared), and — the case that actually
  matters — correctly steps `doc.live` back to the immediately preceding
  cue while logging a real history entry for the cue it just left, exactly
  as `goNext()`/any row's ▶ already do. Shares the same
  `goToIndex()`/`saveLive()` path as every other transition, so it inherits
  that path's existing coverage rather than needing its own.

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
