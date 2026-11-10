# /// script
# requires-python = ">=3.14"
# dependencies = []
# ///
"""Serve this folder on 127.0.0.1 and keep the monitor's log in epever.db (SQLite).

POST /api/sync  {"minutes": [record, ...], "snaps": [snapshot, ...]} → {"minutes": n, "snaps": n}
POST /api/raw   [[t_ms, *RAW_COLS[1:]], ...] → {"raw": n}; every poll, kept forever.
GET  /api/raw?since=t_ms → the same rows, oldest first.
GET  /api/stats?by=day|week|month → per-period totals and extremes, newest first.
GET  /api/status → row counts and time range.
Inserts are keyed by time, so resending is harmless.
Run: uv run epever_server.py [port]
"""

import json
import sqlite3
import sys
from datetime import datetime
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

HERE = Path(__file__).parent
DB = HERE / "epever.db"
MAX_BODY = 16 << 20
RAW_COLS = ("t", "bat_v", "net_a", "load_v", "load_a", "pv_v", "chg_a")
# t_* = seconds into the minute when that extreme happened
MINUTE_COLS = ("m", "v_avg", "v_min", "v_max", "i_avg", "ah_in", "ah_out", "wh_solar", "wh_load", "lvd", "stage", "n",
               "i_min", "i_max", "t_vmin", "t_vmax", "t_imin", "t_imax")
MINUTE_COLS_V1 = 12
SNAP_KEYS = {"t": "t", "genTotal": "gen_total", "consTotal": "cons_total", "batV": "bat_v",
             "offFrom": "off_from", "genWh": "gen_wh", "consWh": "cons_wh", "vFrom": "v_from"}

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS minutes ({", ".join(c + (" INTEGER PRIMARY KEY" if c == "m" else "") for c in MINUTE_COLS)});
CREATE TABLE IF NOT EXISTS snapshots ({", ".join(c + (" INTEGER PRIMARY KEY" if c == "t" else "") for c in SNAP_KEYS.values())});
CREATE TABLE IF NOT EXISTS raw ({", ".join(c + (" INTEGER PRIMARY KEY" if c == "t" else "") for c in RAW_COLS)});
CREATE VIEW IF NOT EXISTS days AS
  SELECT date(m * 60, 'unixepoch', 'localtime') AS day,
         round(sum(ah_in), 2) AS ah_in, round(sum(ah_out), 2) AS ah_out,
         round(sum(wh_solar)) AS wh_solar, round(sum(wh_load)) AS wh_load,
         min(v_min) AS v_min, max(v_max) AS v_max, sum(lvd) AS lvd_min, count(*) AS logged_min
  FROM minutes GROUP BY day;
"""


def connect() -> sqlite3.Connection:
    db = sqlite3.connect(DB)
    db.executescript(SCHEMA)
    have = {row[1] for row in db.execute("PRAGMA table_info(minutes)")}
    for col in MINUTE_COLS:
        if col not in have:
            db.execute(f"ALTER TABLE minutes ADD COLUMN {col}")
    return db


def store_raw(rows: list) -> dict:
    """Upsert raw readings (one per poll)."""
    rows = [r for r in rows if isinstance(r, list) and len(r) == len(RAW_COLS)]
    with connect() as db:
        db.executemany(f"INSERT OR REPLACE INTO raw VALUES ({', '.join('?' * len(RAW_COLS))})", rows)
    return {"raw": len(rows)}


def read_raw(since_ms: int) -> list:
    with connect() as db:
        return db.execute("SELECT * FROM raw WHERE t >= ? ORDER BY t", (since_ms,)).fetchall()


def store(body: dict) -> dict:
    """Upsert minute records and snapshots; return counts stored."""
    minutes = [r + [None] * (len(MINUTE_COLS) - len(r)) for r in body.get("minutes") or []
               if isinstance(r, list) and MINUTE_COLS_V1 <= len(r) <= len(MINUTE_COLS)]
    snaps = [s for s in body.get("snaps") or [] if isinstance(s, dict) and s.get("t") is not None]
    with connect() as db:
        db.executemany(f"INSERT OR REPLACE INTO minutes ({', '.join(MINUTE_COLS)}) VALUES ({', '.join('?' * len(MINUTE_COLS))})", minutes)
        db.executemany(f"INSERT OR REPLACE INTO snapshots VALUES ({', '.join('?' * len(SNAP_KEYS))})",
                       [[s.get(k) for k in SNAP_KEYS] for s in snaps])
    return {"minutes": len(minutes), "snaps": len(snaps)}


def status() -> dict:
    with connect() as db:
        n, first, last = db.execute("SELECT count(*), min(m), max(m) FROM minutes").fetchone()
        (ns,) = db.execute("SELECT count(*) FROM snapshots").fetchone()
        (nr,) = db.execute("SELECT count(*) FROM raw").fetchone()
    return {"minutes": n, "firstMinute": first, "lastMinute": last, "snaps": ns, "raw": nr, "db": str(DB)}


def period_of(ts: float, by: str) -> str:
    d = datetime.fromtimestamp(ts)
    match by:
        case "week":
            y, w, _ = d.isocalendar()
            return f"{y}-W{w:02}"
        case "month":
            return f"{d:%Y-%m}"
        case _:
            return f"{d:%Y-%m-%d}"


def stats(by: str) -> list[dict]:
    """Per day / ISO week / month, newest first: logged totals, extremes with their time, energy while unlogged.
    ahSolar, ahLoad = Σ minute Wh ÷ minute mean battery V."""
    out: dict[str, dict] = {}
    with connect() as db:
        rows = db.execute("SELECT m, v_avg, ah_in, ah_out, wh_solar, wh_load, lvd, v_min, v_max, i_avg, i_min, i_max,"
                          " t_vmin, t_vmax, t_imin, t_imax FROM minutes ORDER BY m").fetchall()
        snaps = db.execute("SELECT t, gen_wh, cons_wh FROM snapshots WHERE gen_wh IS NOT NULL").fetchall()
    for m, vavg, ain, aout, ws, wl, lvd, vmin, vmax, iavg, imin, imax, tvmin, tvmax, timin, timax in rows:
        t0 = m * 60
        a = out.setdefault(period_of(t0, by), {"minutes": 0, "ahIn": 0.0, "ahOut": 0.0, "whSolar": 0.0, "whLoad": 0.0, "ahSolar": 0.0, "ahLoad": 0.0,
                                               "lvdMin": 0, "vLow": None, "vHigh": None, "iLow": None, "iHigh": None,
                                               "offWhIn": 0, "offWhOut": 0})
        a["minutes"] += 1
        a["ahIn"] += ain or 0; a["ahOut"] += aout or 0; a["whSolar"] += ws or 0; a["whLoad"] += wl or 0; a["lvdMin"] += lvd or 0
        if vavg:
            a["ahSolar"] += (ws or 0) / vavg; a["ahLoad"] += (wl or 0) / vavg
        for key, v, off, low in (("vLow", vmin, tvmin, True), ("vHigh", vmax, tvmax, False),
                                 ("iLow", iavg if imin is None else imin, timin, True),
                                 ("iHigh", iavg if imax is None else imax, timax, False)):
            if v is not None and (a[key] is None or (v < a[key][0] if low else v > a[key][0])):
                a[key] = [v, t0 + (off or 0)]
    for t, gen, cons in snaps:
        a = out.get(period_of(t / 1000, by))
        if a:
            a["offWhIn"] += gen or 0; a["offWhOut"] += cons or 0
    return [{"period": k, **v} for k, v in sorted(out.items(), reverse=True)]


class Handler(SimpleHTTPRequestHandler):
    def send_json(self, code: int, obj: dict | list) -> None:
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        if url.path == "/api/status":
            self.send_json(200, status())
        elif url.path == "/api/raw":
            since = (parse_qs(url.query).get("since") or ["0"])[0]
            self.send_json(200, read_raw(int(since) if since.isdigit() else 0))
        elif url.path == "/api/stats":
            self.send_json(200, stats((parse_qs(url.query).get("by") or ["day"])[0]))
        else:
            super().do_GET()

    def do_POST(self) -> None:
        handle = {"/api/sync": store, "/api/raw": store_raw}.get(self.path)
        if not handle:
            self.send_json(404, {"error": "not found"})
            return
        size = int(self.headers.get("Content-Length") or 0)
        if not 0 < size <= MAX_BODY:
            self.send_json(413, {"error": "bad size"})
            return
        try:
            self.send_json(200, handle(json.loads(self.rfile.read(size))))
        except (ValueError, TypeError, AttributeError, sqlite3.Error) as e:
            self.send_json(400, {"error": str(e)})

    def log_message(self, *args) -> None:
        pass


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    connect().close()
    ThreadingHTTPServer(("127.0.0.1", port), partial(Handler, directory=str(HERE))).serve_forever()


if __name__ == "__main__":
    main()
