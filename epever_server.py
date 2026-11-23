# /// script
# requires-python = ">=3.14"
# dependencies = []
# ///
"""Serve this folder on 127.0.0.1 and keep the monitor's log in epever.db (SQLite).

POST /api/sync  {"minutes": [record, ...], "snaps": [snapshot, ...]} → {"minutes": n, "snaps": n}
POST /api/raw   [[t_ms, *RAW_COLS[1:]], ...] → {"raw": n}; every poll, kept forever (status words 0x3200–0x3202 optional).
GET  /api/raw?since=t_ms → the same rows, oldest first.
GET  /api/pv?since=t_ms → [[epoch minute, mean panel V], ...], oldest first.
GET  /api/ranges → {"batV"|"netA"|"chgA"|"loadA": [lowest, highest]} of all readings, outer 0.1 % ignored.
GET  /api/snaps → counter snapshots (page key names), oldest first.
GET  /api/minutes?since=t_ms → minute records (MINUTE_COLS order), oldest first.
GET  /api/regs → epever_worker.py's latest blocks and capacity state: {"regs": [{key, fc, addr, n, t, vals}, ...], "cap": {...}}.
POST /api/charging {"on": bool} → queued for epever_worker.py (coil 0); waits ≤ 3 s → {"result": "ok" | ...}.
GET  /api/stats?by=day|week|month → per-period totals and extremes, newest first.
GET  /api/status → row counts and time range.
Inserts are keyed by time, so resending is harmless.
Run: uv run epever_server.py [port]
"""

import json
import sqlite3
import sys
import time
from datetime import datetime
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

HERE = Path(__file__).parent
DB = HERE / "epever.db"
MAX_BODY = 16 << 20
RAW_COLS = ("t", "bat_v", "net_a", "load_v", "load_a", "pv_v", "chg_a", "batt_status", "chg_status", "load_status")
RAW_COLS_V1 = 7
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
-- written by epever_worker.py: latest register blocks, charging-switch requests, its last reading
CREATE TABLE IF NOT EXISTS regs (key TEXT PRIMARY KEY, fc, addr, n, t, vals);
CREATE TABLE IF NOT EXISTS commands (id INTEGER PRIMARY KEY, t, coil, done, result);
CREATE TABLE IF NOT EXISTS worker_state (k TEXT PRIMARY KEY, v);
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
    for table, cols in (("minutes", MINUTE_COLS), ("raw", RAW_COLS)):
        have = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        for col in cols:
            if col not in have:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {col}")
    return db


def store_raw(rows: list) -> dict:
    """Upsert raw readings (one per poll)."""
    rows = [r + [None] * (len(RAW_COLS) - len(r)) for r in rows if isinstance(r, list) and RAW_COLS_V1 <= len(r) <= len(RAW_COLS)]
    with connect() as db:
        db.executemany(f"INSERT OR REPLACE INTO raw ({', '.join(RAW_COLS)}) VALUES ({', '.join('?' * len(RAW_COLS))})", rows)
    return {"raw": len(rows)}


def read_raw(since_ms: int) -> list:
    with connect() as db:
        return db.execute(f"SELECT {', '.join(RAW_COLS)} FROM raw WHERE t >= ? ORDER BY t", (since_ms,)).fetchall()


def read_pv(since_ms: int) -> list:
    """Panel V per minute: [[t // 60000, mean pv_v], ...] for t ≥ since_ms."""
    with connect() as db:
        return db.execute("SELECT t / 60000, round(avg(pv_v), 2) FROM raw WHERE t >= ? AND pv_v IS NOT NULL"
                          " GROUP BY t / 60000 ORDER BY 1", (since_ms,)).fetchall()


def read_minutes(since_ms: int) -> list:
    with connect() as db:
        return db.execute(f"SELECT {', '.join(MINUTE_COLS)} FROM minutes WHERE m >= ? ORDER BY m", (since_ms // 60000,)).fetchall()


RANGE_COLS = {"batV": "bat_v", "netA": "net_a", "chgA": "chg_a", "loadA": "load_a"}
RANGE_TAIL = 0.001   # ignored at each end: seconds-long spikes such as a disconnected battery (1.3 V, 16.9 V)
RANGE_TTL_S = 300
_ranges: tuple[float, dict] = (0.0, {})


def ranges() -> dict:
    """Lowest and highest raw reading per column, past the outer RANGE_TAIL; cached RANGE_TTL_S."""
    global _ranges
    if time.time() - _ranges[0] < RANGE_TTL_S:
        return _ranges[1]
    out = {}
    with connect() as db:
        for k, c in RANGE_COLS.items():
            if not (n := db.execute(f"SELECT count({c}) FROM raw").fetchone()[0]):
                continue
            ends = [db.execute(f"SELECT {c} FROM raw WHERE {c} IS NOT NULL ORDER BY {c} {d} LIMIT 1 OFFSET ?",
                               (int(n * RANGE_TAIL),)).fetchone()[0] for d in ("ASC", "DESC")]
            out[k] = ends
    _ranges = (time.time(), out)
    return out


def read_snaps() -> list:
    with connect() as db:
        return [dict(zip(SNAP_KEYS, r)) for r in db.execute(f"SELECT {', '.join(SNAP_KEYS.values())} FROM snapshots ORDER BY t")]


def read_regs() -> dict:
    with connect() as db:
        cap = db.execute("SELECT v FROM worker_state WHERE k = 'cap'").fetchone()
        return {"regs": [{"key": k, "fc": fc, "addr": a, "n": n, "t": t, "vals": json.loads(v)}
                         for k, fc, a, n, t, v in db.execute("SELECT key, fc, addr, n, t, vals FROM regs")],
                "cap": json.loads(cap[0]) if cap else None}


def set_charging(body: dict) -> dict:
    """Queue coil 0 for the worker; the result once it has run, or "pending"."""
    if not isinstance(body.get("on"), bool):
        return {"result": "invalid"}
    with connect() as db:
        cid = db.execute("INSERT INTO commands (t, coil) VALUES (?, ?)", (time.time_ns() // 1_000_000, int(body["on"]))).lastrowid
    for _ in range(30):
        time.sleep(0.1)
        with connect() as db:
            if (r := db.execute("SELECT result FROM commands WHERE id = ? AND done IS NOT NULL", (cid,)).fetchone()):
                return {"result": r[0]}
    return {"result": "pending"}


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
        elif url.path == "/api/minutes":
            since = (parse_qs(url.query).get("since") or ["0"])[0]
            self.send_json(200, read_minutes(int(since) if since.isdigit() else 0))
        elif url.path == "/api/ranges":
            self.send_json(200, ranges())
        elif url.path == "/api/snaps":
            self.send_json(200, read_snaps())
        elif url.path == "/api/regs":
            self.send_json(200, read_regs())
        elif url.path == "/api/pv":
            since = (parse_qs(url.query).get("since") or ["0"])[0]
            self.send_json(200, read_pv(int(since) if since.isdigit() else 0))
        elif url.path == "/api/stats":
            self.send_json(200, stats((parse_qs(url.query).get("by") or ["day"])[0]))
        else:
            super().do_GET()

    def do_POST(self) -> None:
        handle = {"/api/sync": store, "/api/raw": store_raw, "/api/charging": set_charging}.get(self.path)
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
