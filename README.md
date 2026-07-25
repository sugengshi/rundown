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
| **Viewer** | `/r/ID` | Watch. Nothing else |

**The studio link is the whole account.** There is no recovery. Bookmark it,
and treat it like a password, because it is one.

The edit link exists so you can hand a single show to a co-facilitator without
handing over your whole studio.

## What viewers can and can't see

Viewers get the running order, the live cue, and the countdown. They do **not**
get the Notes column — the server strips it before sending, so it isn't hiding
in the page source. Flip **Show notes to viewers** on the rundown page if you
want them shared. Everything else in a rundown is readable by anyone with the
link. That's what a public viewer link means.

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

- **Drift assumes one timezone.** Cue start times are wall-clock strings in the
  operator's timezone. A viewer elsewhere sees a wrong drift figure; the cue
  name and countdown are still right. Fix is storing the show start as a real
  timestamp — say the word.
- **Shows crossing midnight** produce nonsense start times, same root cause.
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
