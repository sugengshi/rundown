"""
Rundown — a shared run-of-show tool.

One operator edits, anyone with the viewer link watches live.

Identity model, deliberately account-free:
  * an OPERATOR KEY owns every rundown you make. It lives in one bookmarkable
    URL. Lose that bookmark and you lose the list, so the studio page nags.
  * an EDIT KEY unlocks a single rundown, for handing one show to one person.
  * a viewer link carries neither and is read-only.

Nothing expires on a timer. Archiving is manual and reversible. Permanent
deletion is manual and is not.
"""

import io
import os
import csv
import json
import zipfile
import secrets
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - py<3.9 fallback, shouldn't happen on Railway
    ZoneInfo = None

import asyncpg
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

log = logging.getLogger("rundown")
logging.basicConfig(level=logging.INFO)

STATIC = Path(__file__).resolve().parent.parent / "static"

SCHEMA = """
CREATE TABLE IF NOT EXISTS rundowns (
    id          TEXT PRIMARY KEY,
    owner_key   TEXT        NOT NULL,
    edit_key    TEXT        NOT NULL,
    name        TEXT        NOT NULL DEFAULT 'Untitled show',
    data        JSONB       NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS rundowns_owner_idx ON rundowns (owner_key);

CREATE TABLE IF NOT EXISTS archives (
    id           TEXT PRIMARY KEY,
    owner_key    TEXT        NOT NULL,
    name         TEXT        NOT NULL,
    data         JSONB       NOT NULL,
    csv          TEXT        NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL,
    archived_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS archives_owner_idx ON archives (owner_key);
"""

pool: asyncpg.Pool | None = None


# --------------------------------------------------------------------------
# live connections
# --------------------------------------------------------------------------
class Hub:
    """Tracks who is watching which rundown."""

    def __init__(self) -> None:
        self.rooms: dict[str, set[WebSocket]] = {}

    async def join(self, show_id: str, ws: WebSocket) -> None:
        await ws.accept()
        self.rooms.setdefault(show_id, set()).add(ws)

    def leave(self, show_id: str, ws: WebSocket) -> None:
        room = self.rooms.get(show_id)
        if not room:
            return
        room.discard(ws)
        if not room:
            self.rooms.pop(show_id, None)

    def count(self, show_id: str) -> int:
        return len(self.rooms.get(show_id, ()))

    async def broadcast(self, show_id: str, payload: dict) -> None:
        msg = json.dumps(payload, default=str)
        dead = []
        for ws in list(self.rooms.get(show_id, ())):
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.leave(show_id, ws)


hub = Hub()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError(
            "DATABASE_URL is not set. Add a Postgres service in Railway and "
            "link it to this service."
        )
    dsn = dsn.replace("postgres://", "postgresql://", 1)
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=8)
    async with pool.acquire() as con:
        await con.execute(SCHEMA)
    log.info("rundown up")
    try:
        yield
    finally:
        await pool.close()


app = FastAPI(title="Rundown", lifespan=lifespan)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def new_id() -> str:
    return secrets.token_urlsafe(6)


def new_key() -> str:
    return secrets.token_urlsafe(24)


def match(a: str, b: str) -> bool:
    return bool(a) and bool(b) and secrets.compare_digest(a, b)


def as_dict(raw: Any) -> dict:
    return json.loads(raw) if isinstance(raw, str) else (raw or {})


def parse_clock(s: str) -> int:
    parts = [int(p or 0) for p in str(s or "0").split(":")[:3]]
    while len(parts) < 3:
        parts.append(0)
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def fmt_clock(sec: int) -> str:
    sec = int(sec) % 86400
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}"


def fmt_dur(sec: int) -> str:
    sec = int(sec)
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def fmt_clock_epoch(epoch_ms: float, tz_name: str | None) -> str:
    """Format an absolute instant as HH:MM in the show's declared timezone.
    Falls back to UTC if the zone name is missing or unrecognised, rather
    than raising — an archive CSV should never fail to export."""
    tz = timezone.utc
    if tz_name and ZoneInfo is not None:
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = timezone.utc
    return datetime.fromtimestamp(epoch_ms / 1000, tz).strftime("%H:%M")


def day_epoch_ms(item: dict, tz_name: str | None, fallback_ms: float) -> float:
    """A 'day' item resets the running clock to a specific date + time
    instead of continuing from the previous item's duration — same mechanic
    as startEpoch, applied mid-rundown so one rundown can span several days.
    Mirrors dayEpoch() in static/show.html; if the date is missing or
    malformed, hold the clock where it was rather than jumping to 1970."""
    date_s = item.get("date") or ""
    try:
        y, m, d = (int(p) for p in date_s.split("-"))
    except Exception:
        return fallback_ms
    sec = parse_clock(item.get("time") or "09:00")
    tz = timezone.utc
    if tz_name and ZoneInfo is not None:
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = timezone.utc
    try:
        dt = datetime(y, m, d, sec // 3600, (sec % 3600) // 60, sec % 60, tzinfo=tz)
    except Exception:
        return fallback_ms
    return dt.timestamp() * 1000


def to_csv(name: str, data: dict) -> str:
    """A rundown as flat CSV. This is the archive format — plain text on
    purpose, so it outlives this application.

    Rundowns created after the timezone fix carry `startEpoch` (an absolute
    instant, ms since epoch) and `tz` (an IANA zone name) — the show's start
    time as a real point in time, immune to the reader's own clock and to
    midnight rollover. Older rundowns saved before that fix only have `start`
    as a bare "HH:MM" wall-clock string with no zone attached; those still
    render the old (occasionally wrong) way rather than being silently
    reinterpreted."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([f"Rundown: {name}"])
    tz_name = data.get("tz")
    start_label = data.get("start", "")
    if tz_name:
        start_label = f"{start_label} ({tz_name})"
    w.writerow([f"Show starts: {start_label}"])
    w.writerow([])
    w.writerow(["Start", "Length", "Type", "Segment", "Who", "Notes"])

    start_epoch = data.get("startEpoch")
    items = data.get("items", [])

    if isinstance(start_epoch, (int, float)):
        t_ms = float(start_epoch)
        for item in items:
            item_type = item.get("type", "")
            if item_type == "day":
                t_ms = day_epoch_ms(item, tz_name, t_ms)
            is_cue = item_type == "cue"
            w.writerow([
                fmt_clock_epoch(t_ms, tz_name),
                fmt_dur(item.get("dur", 0)) if is_cue else "",
                item_type,
                item.get("title", ""),
                item.get("who", ""),
                item.get("notes", ""),
            ])
            if is_cue:
                t_ms += int(item.get("dur") or 0) * 1000
    else:
        # Legacy path for rundowns archived before the timezone fix.
        t = parse_clock(data.get("start", "00:00"))
        for item in items:
            is_cue = item.get("type") == "cue"
            w.writerow([
                fmt_clock(t),
                fmt_dur(item.get("dur", 0)) if is_cue else "",
                item.get("type", ""),
                item.get("title", ""),
                item.get("who", ""),
                item.get("notes", ""),
            ])
            if is_cue:
                t += int(item.get("dur") or 0)
    return buf.getvalue()


def strip_for_viewer(data: dict) -> dict:
    """Viewers get the schedule, not the production notes — unless the
    operator has turned notes sharing on."""
    if data.get("shareNotes"):
        return data
    clean = dict(data)
    clean["items"] = [{k: v for k, v in item.items() if k != "notes"}
                      for item in data.get("items", [])]
    return clean


async def fetch_row(show_id: str) -> asyncpg.Record:
    async with pool.acquire() as con:
        row = await con.fetchrow("SELECT * FROM rundowns WHERE id = $1", show_id)
        if row is None:
            arc = await con.fetchval("SELECT 1 FROM archives WHERE id = $1", show_id)
    if row is None:
        if arc:
            raise HTTPException(410, "This rundown has been archived. The operator can restore it.")
        raise HTTPException(404, "This rundown does not exist.")
    return row


def can_edit_row(row: asyncpg.Record, k: str, o: str) -> bool:
    return match(k, row["edit_key"]) or match(o, row["owner_key"])


# --------------------------------------------------------------------------
# operator / studio
# --------------------------------------------------------------------------
@app.get("/api/health")
async def health():
    async with pool.acquire() as con:
        live = await con.fetchval("SELECT count(*) FROM rundowns")
        arc = await con.fetchval("SELECT count(*) FROM archives")
    return {"ok": True, "rundowns": live, "archived": arc}


@app.post("/api/operators")
async def create_operator():
    """No signup, no email. A key is a key."""
    return {"operatorKey": new_key()}


@app.get("/api/studio")
async def studio(o: str = Query(...)):
    async with pool.acquire() as con:
        live = await con.fetch(
            """SELECT id, edit_key, name, updated_at, created_at
                 FROM rundowns WHERE owner_key = $1 ORDER BY updated_at DESC""", o)
        arc = await con.fetch(
            """SELECT id, name, created_at, archived_at
                 FROM archives WHERE owner_key = $1 ORDER BY archived_at DESC""", o)
    return {"rundowns": [dict(r) for r in live], "archives": [dict(r) for r in arc]}


@app.get("/api/export")
async def export_all(o: str = Query(...)):
    """Every rundown you own, live and archived, as CSVs in one zip."""
    async with pool.acquire() as con:
        live = await con.fetch("SELECT id, name, data FROM rundowns WHERE owner_key = $1", o)
        arc = await con.fetch("SELECT id, name, csv FROM archives WHERE owner_key = $1", o)

    if not live and not arc:
        raise HTTPException(404, "Nothing to export yet.")

    def safe(s: str) -> str:
        return "".join(c for c in s if c.isalnum() or c in " -_").strip()[:60] or "untitled"

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for r in live:
            z.writestr(f"active/{safe(r['name'])}-{r['id']}.csv",
                       to_csv(r["name"], as_dict(r["data"])))
        for r in arc:
            z.writestr(f"archived/{safe(r['name'])}-{r['id']}.csv", r["csv"])
    buf.seek(0)
    return StreamingResponse(
        buf, media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="rundowns.zip"'})


# --------------------------------------------------------------------------
# rundowns
# --------------------------------------------------------------------------
@app.post("/api/rundowns")
async def create_rundown(body: dict, o: str = Query(...)):
    show_id, key = new_id(), new_key()
    name = (body.get("name") or "Untitled show").strip()[:120] or "Untitled show"
    data = body.get("data") or {"start": "10:00", "live": None, "shareNotes": False, "items": []}
    async with pool.acquire() as con:
        await con.execute(
            """INSERT INTO rundowns (id, owner_key, edit_key, name, data)
               VALUES ($1, $2, $3, $4, $5::jsonb)""",
            show_id, o, key, name, json.dumps(data))
    return {"id": show_id, "editKey": key, "name": name}


@app.get("/api/rundowns/{show_id}")
async def get_rundown(show_id: str, k: str = Query(default=""), o: str = Query(default="")):
    row = await fetch_row(show_id)
    editable = can_edit_row(row, k, o)
    data = as_dict(row["data"])
    return {
        "id": row["id"],
        "name": row["name"],
        "data": data if editable else strip_for_viewer(data),
        "canEdit": editable,
        "editKey": row["edit_key"] if editable else None,
        "updatedAt": row["updated_at"].isoformat(),
        "viewers": hub.count(show_id),
    }


@app.put("/api/rundowns/{show_id}")
async def save_rundown(show_id: str, body: dict,
                       k: str = Query(default=""), o: str = Query(default="")):
    row = await fetch_row(show_id)
    if not can_edit_row(row, k, o):
        raise HTTPException(403, "You have the viewer link, not the edit link.")

    data = body.get("data")
    if not isinstance(data, dict):
        raise HTTPException(400, "Expected a data object.")
    name = (body.get("name") or row["name"]).strip()[:120] or row["name"]

    async with pool.acquire() as con:
        updated = await con.fetchrow(
            """UPDATE rundowns SET data = $2::jsonb, name = $3, updated_at = now()
                WHERE id = $1 RETURNING updated_at""",
            show_id, json.dumps(data), name)

    await hub.broadcast(show_id, {"type": "update", "name": name,
                                  "data": strip_for_viewer(data)})
    return {"ok": True, "updatedAt": updated["updated_at"].isoformat(),
            "viewers": hub.count(show_id)}


@app.post("/api/rundowns/{show_id}/archive")
async def archive_rundown(show_id: str, k: str = Query(default=""), o: str = Query(default="")):
    """Take it out of circulation. The viewer link stops working; the content
    is kept as CSV plus the original JSON, and can be restored."""
    row = await fetch_row(show_id)
    if not can_edit_row(row, k, o):
        raise HTTPException(403, "Only the operator can archive this rundown.")
    data = as_dict(row["data"])
    async with pool.acquire() as con:
        async with con.transaction():
            await con.execute(
                """INSERT INTO archives (id, owner_key, name, data, csv, created_at)
                   VALUES ($1, $2, $3, $4::jsonb, $5, $6)
                   ON CONFLICT (id) DO UPDATE
                     SET data = EXCLUDED.data, csv = EXCLUDED.csv""",
                row["id"], row["owner_key"], row["name"], json.dumps(data),
                to_csv(row["name"], data), row["created_at"])
            await con.execute("DELETE FROM rundowns WHERE id = $1", show_id)
    await hub.broadcast(show_id, {"type": "archived"})
    return {"ok": True}


@app.post("/api/archives/{show_id}/restore")
async def restore_rundown(show_id: str, o: str = Query(...)):
    async with pool.acquire() as con:
        arc = await con.fetchrow(
            "SELECT * FROM archives WHERE id = $1 AND owner_key = $2", show_id, o)
        if arc is None:
            raise HTTPException(404, "No archived rundown with that id.")
        async with con.transaction():
            await con.execute(
                """INSERT INTO rundowns (id, owner_key, edit_key, name, data, created_at)
                   VALUES ($1, $2, $3, $4, $5::jsonb, $6)""",
                arc["id"], arc["owner_key"], new_key(), arc["name"],
                json.dumps(as_dict(arc["data"])), arc["created_at"])
            await con.execute("DELETE FROM archives WHERE id = $1", show_id)
    return {"ok": True, "id": show_id}


@app.get("/api/archives/{show_id}/csv")
async def archive_csv(show_id: str, o: str = Query(...)):
    async with pool.acquire() as con:
        arc = await con.fetchrow(
            "SELECT name, csv FROM archives WHERE id = $1 AND owner_key = $2", show_id, o)
    if arc is None:
        raise HTTPException(404, "No archived rundown with that id.")
    fn = "".join(c for c in arc["name"] if c.isalnum() or c in " -_").strip() or "rundown"
    return StreamingResponse(
        io.BytesIO(arc["csv"].encode("utf-8")), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{fn}.csv"'})


@app.delete("/api/archives/{show_id}")
async def delete_archive(show_id: str, o: str = Query(...)):
    """The only irreversible operation in this application."""
    async with pool.acquire() as con:
        result = await con.execute(
            "DELETE FROM archives WHERE id = $1 AND owner_key = $2", show_id, o)
    if result.endswith(" 0"):
        raise HTTPException(404, "No archived rundown with that id.")
    return {"ok": True}


# --------------------------------------------------------------------------
# live socket
# --------------------------------------------------------------------------
@app.websocket("/ws/{show_id}")
async def ws_endpoint(ws: WebSocket, show_id: str):
    await hub.join(show_id, ws)
    try:
        await hub.broadcast(show_id, {"type": "viewers", "viewers": hub.count(show_id)})
        while True:
            # Clients only ever ping. Edits arrive over HTTP, where the keys
            # get checked, so nothing sent here can change stored data.
            await ws.receive_text()
            await ws.send_text('{"type":"pong"}')
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        hub.leave(show_id, ws)
        await hub.broadcast(show_id, {"type": "viewers", "viewers": hub.count(show_id)})


# --------------------------------------------------------------------------
# pages
# --------------------------------------------------------------------------
@app.get("/r/{show_id}")
async def show_page(show_id: str):
    return FileResponse(STATIC / "show.html")


@app.get("/studio")
async def studio_page():
    return FileResponse(STATIC / "studio.html")


@app.exception_handler(HTTPException)
async def http_error(request, exc: HTTPException):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
