# Rundown

A run-of-show tool. One operator drives, anyone with the viewer link watches the
clock move in real time. FastAPI + Postgres + WebSocket, deployed on Railway.

## Deploy

1. Push this folder to a GitHub repo.
2. Railway → **New Project → Deploy from GitHub repo** → pick it.
3. In the same project: **New → Database → Add PostgreSQL**.
4. Open your web service → **Variables** → **Add Reference** → pick the Postgres
   service's `DATABASE_URL`. The app refuses to start without it, on purpose.
5. **Settings → Networking → Generate Domain**.

Tables are created on first boot. There is no migration step and nothing to seed.

Health check: `GET /api/health` → `{"ok": true, "rundowns": n, "archived": n}`

Local:

```bash
pip install -r requirements.txt
export DATABASE_URL="postgres://postgres:pw@localhost:5432/rundown"
uvicorn app.main:app --reload
```

## How the access model works

There are no accounts and no passwords. Three kinds of link:

| Link | Looks like | Can do |
|---|---|---|
| **Studio** | `/studio?o=SECRET` | Everything you own: create, edit, archive, restore, delete, export |
| **Edit** | `/r/ID?k=SECRET` | Edit and drive one rundown |
| **Driver** | `/r/ID?d=SECRET` | Go / skip / stop cues on one rundown. Cannot change the running order |
| **Viewer** | `/r/ID` | Watch. Nothing else |

**The studio link is the whole account.** There is no recovery. Bookmark it,
and treat it like a password, because it is one.

The edit link exists so you can hand a single show to a co-facilitator without
handing over your whole studio.

The **driver link** is for the person calling the show when that isn't you — an
AV operator, a co-host, someone at the back of the room with a laptop. They get
the Go/Stop buttons, the ▶ on each row, and the `Space`/`Esc` keys; they don't
get the running order, the segment names, the durations, or the Notes column.
Copy it from the studio list ("Copy driver link") or from the rundown page.

**Treat the driver link as a password too.** It's weaker than the edit link, but
whoever holds it can advance or stop cues on a live show. If it leaks mid-event
there's no revoke button — archive and restore the rundown, which mints fresh
edit *and* driver keys.

## What viewers can and can't see

Viewers get the running order, the live cue, and the countdown. They do **not**
get the Notes column — the server strips it before sending, so it isn't hiding
in the page source. Flip **Show notes to viewers** on the rundown page if you
want them shared. Everything else in a rundown is readable by anyone with the
link. That's what a public viewer link means.

## Importing from Google Sheets

There's no live connection to Google Sheets on purpose — that would mean an
OAuth app, stored tokens, and a dependency on Google's API staying up, for a
feature used a handful of times a year. Instead: select your rows in Sheets,
copy, and click **Import from Sheets** on the rundown page to paste them in.

Column order matches **Export CSV** exactly — Start, Length, Type, Segment,
Who, Notes — with or without a header row. **Start is ignored on import**; it's
always recalculated from the show's start time and each segment's length, the
same as it would be if you typed the durations in by hand. Editing the Start
column in your sheet does nothing, by design.

This isn't just a one-shot create. Paste into a rundown that already has
content and **Preview changes** shows you exactly what importing would do —
added rows, removed rows, and which fields changed on existing ones — before
anything is touched. Matching is by segment type and title, so reordering
rows in the sheet, or having several segments named "Break," doesn't confuse
it into deleting and re-adding things that didn't actually change.

**Day breaks can't be imported this way** — the paste format has no date
field to represent one. Any day breaks already in the rundown are left
exactly where they are; the import only ever touches segments and headings.

## Multi-day events

One rundown can cover a whole multi-day event instead of needing a separate
rundown per day. Click **Add day** to insert a day break: give it a label,
a date, and a start time. Everything after that point in the list starts
counting from that day's start instead of continuing from the previous
segment's duration — so day 2 doesn't accidentally start 30 hours into
day 1's clock.

The **Show left** figure only counts the current day's remaining segments,
not the whole event, so it stays useful once there's more than one day in
the list. Everything else — drift, the live cue, notes-stripping for
viewers — works the same across a day break as within a day.

Day breaks assume the whole event is in one timezone, same as the rest of
the show; enter them from the same device/timezone you set "Show starts"
from.

## Retention

Nothing expires. Nothing is deleted on a schedule.

- **Archive** — manual, reversible. Removes it from the active list, stops the
  viewer link, keeps both a CSV and the original JSON.
- **Delete** — only available on already-archived rundowns, asks you to type the
  show name, and is permanent. It is the one irreversible action in the app.
- **Download everything** — a zip of every rundown you own, active and archived,
  as CSVs. Run it before anything you'd regret losing.

## Known limits

Read this part.

- ~~Drift assumes one timezone~~ **Fixed.** `doc.start` (the "HH:MM" field) is
  now anchored to `startEpoch` — a real instant, set from the operator's own
  clock the moment they set or change the start time — plus `tz`, the IANA
  zone name, carried along so every viewer's browser renders that instant as
  the same wall-clock time the operator sees, no matter where the viewer's
  device thinks it is. Rundowns saved before this fix have no `startEpoch`;
  the first time an operator opens one, the client fills it in from their own
  clock and re-saves. Until then it falls back to the old (occasionally
  wrong) behaviour. The Python-side CSV export mirrors this: new-style
  archives use `startEpoch`/`tz`, pre-fix archives fall back to the legacy
  wall-clock arithmetic.
- ~~Shows crossing midnight produce nonsense start times~~ **Fixed as a
  side effect** — cumulative durations are now added to a real timestamp
  instead of wrapped mod 24h, so a show that runs past midnight just keeps
  counting forward correctly.
- **A disconnected viewer keeps counting the wrong segment.** Countdowns run
  locally from a synced start timestamp, which is why they stay smooth on bad
  wifi. The page shows a warning banner when the socket drops, but the numbers
  keep moving. This is a deliberate trade: a stuttering clock is worse for a
  workshop audience than a slightly stale one. For broadcast, invert it.
- **Device clocks matter.** A viewer whose laptop clock is five minutes off sees
  a five-minute error.
- **Last write wins.** Two people on the same edit link will overwrite each
  other. Intended for one operator.
- **Railway is a single container.** If it restarts mid-show, viewers reconnect
  automatically and the WebSocket rooms rebuild — but there's a gap. Nobody's
  uptime is free.

## Layout

```
app/main.py       API, WebSocket hub, archiving, export
static/index.html landing — mints an operator key
static/studio.html your rundowns, archives, export
static/show.html  the rundown itself, operator and viewer in one file
static/style.css  shared tokens
```
