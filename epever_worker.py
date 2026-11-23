# /// script
# requires-python = ">=3.14"
# dependencies = ["pyserial>=3.5", "minimalmodbus>=2.1"]
# ///
"""Poll the EPEver controller into epever.db, with no browser needed.

Reads FC 03/04 only; the one write is coil 0x0000 (charging on/off, FC 05), and only when the
page asks through epever_server.py (table commands, each one ≤ 10 s old).
Writes: regs (latest register blocks, for the page), raw (every poll), minutes, snapshots.
Exits 0 when the cable is unplugged, 1 after repeated read failures (systemd restarts it).
Run: uv run epever_worker.py [/dev/serial/by-id/...]
"""

import fcntl
import json
import signal
import sys
import termios
import time
from glob import glob
from pathlib import Path

import minimalmodbus
import serial
import serial.rs485

import epever_server as srv

POLL_S = 1 / 3
GAP_S = 90                  # longer between samples ⇒ logging gap (as in the page)
CLOCK_EVERY_S = 10
CFG_EVERY_S = 600
COMMAND_MAX_AGE_MS = 10_000
FAIL_LIMIT = 15
STATE_EVERY_S = 5           # capacity state and last reading → worker_state
FULL_V, FULL_TAIL_A = 13.9, 2   # boost + V ≥ boost setting − 0.1 (or FULL_V) + net < tail ⇒ full
REST_MIN, REST_MAX_A = 15, 0.6  # minutes with |net|, load ≤ REST_MAX_A and no charging ⇒ a resting reading
LAST_KEYS = ("t", "genTotal", "consTotal", "batV", "netA")
PORT_GLOB = "/dev/serial/by-id/usb-Exar_Corp._XR21B1411_*-if00-port0"

# name → (fc, start, count); the same blocks the page reads.
POLLED = {"rt": (4, 0x3100, 18), "st": (4, 0x3200, 3), "stats": (4, 0x3302, 27)}
CLOCK = {"clock": (3, 0x9013, 3)}
CFG = {"cfg": (3, 0x9000, 15), "lowTemp": (3, 0x9010, 2), "tempLimits": (3, 0x9017, 4), "ratedLevel": (3, 0x9067, 1),
       "durations": (3, 0x906B, 4), "mgmt": (3, 0x9070, 1), "liProt": (3, 0x9107, 1)}

u32 = lambda r, i: r[i] | r[i + 1] << 16
s32 = lambda r, i: u32(r, i) - (1 << 32) if r[i + 1] & 0x8000 else u32(r, i)


class Controller:
    """Modbus link that can only read (FC 03/04) and set coil 0."""

    def __init__(self, port: str):
        self.inst = minimalmodbus.Instrument(port, 1)
        self.inst.serial.baudrate = 115200
        self.inst.serial.timeout = 0.5
        self.inst.clear_buffers_before_each_transaction = True
        # XR21B1411 cable: chip RS485 auto-direction, RTS active after send.
        self.inst.serial.rs485_mode = serial.rs485.RS485Settings(rts_level_for_tx=False, rts_level_for_rx=True)
        # Exclusive tty: other non-root opens get EBUSY instead of resetting our baud (e.g. ddt4all's port scan).
        fcntl.ioctl(self.inst.serial.fileno(), termios.TIOCEXCL)

    def read(self, fc: int, start: int, n: int) -> list[int]:
        assert fc in (3, 4)
        return self.inst.read_registers(start, n, functioncode=fc)

    def set_charging(self, on: bool) -> None:
        self.inst.write_bit(0, int(on), functioncode=5)

    def close(self) -> None:
        self.inst.serial.close()


def decode(b: dict) -> dict:
    """Polled blocks → the fields the log needs (page's decode, abridged)."""
    rt, st, stats = b["rt"], b["st"], b["stats"]
    return {"pvV": rt[0] / 100, "batV": rt[4] / 100, "chgA": rt[5] / 100, "pvW": u32(rt, 2) / 100,
            "loadV": rt[12] / 100, "loadA": rt[13] / 100, "loadW": u32(rt, 14) / 100,
            "battStatus": st[0], "chgStatus": st[1], "dischgStatus": st[2], "stage": st[1] >> 2 & 3, "loadOn": st[2] & 1,
            "consTotal": u32(stats, 8) / 100, "genTotal": u32(stats, 16) / 100, "netA": s32(stats, 25) / 100}


class Minute:
    """One minute's accumulator → a `minutes` row (page's bucketRecord)."""

    def __init__(self, m: int):
        self.m, self.n, self.v, self.i = m, 0, 0.0, 0.0
        self.vlo = self.ilo = (float("inf"), 0)
        self.vhi = self.ihi = (float("-inf"), 0)
        self.ain = self.aout = self.ws = self.wl = 0.0
        self.lvd, self.st = 0, None

    def add(self, s: dict, prev: dict | None, lvr: float | None) -> None:
        self.n += 1
        self.v += s["batV"]; self.i += s["netA"]
        self.vlo = min(self.vlo, (s["batV"], s["t"])); self.vhi = max(self.vhi, (s["batV"], s["t"]))
        self.ilo = min(self.ilo, (s["netA"], s["t"])); self.ihi = max(self.ihi, (s["netA"], s["t"]))
        if prev and 0 < (dt := (s["t"] - prev["t"]) / 1000) <= GAP_S:
            h = dt / 3600
            ia = (prev["netA"] + s["netA"]) / 2
            if ia > 0: self.ain += ia * h
            else: self.aout -= ia * h
            self.ws += (prev["pvW"] + s["pvW"]) / 2 * h
            self.wl += (prev["loadW"] + s["loadW"]) / 2 * h
        if s["battStatus"] & 0xF == 3 or (s["loadOn"] == 0 and lvr is not None and s["batV"] < lvr):
            self.lvd = 1
        self.st = s["stage"]

    def row(self) -> list:
        sec = lambda t: round(t / 1000 - self.m * 60, 1)
        return [self.m, round(self.v / self.n, 2), round(self.vlo[0], 2), round(self.vhi[0], 2), round(self.i / self.n, 2),
                round(self.ain, 4), round(self.aout, 4), round(self.ws, 2), round(self.wl, 2), self.lvd, self.st, self.n,
                round(self.ilo[0], 2), round(self.ihi[0], 2), sec(self.vlo[1]), sec(self.vhi[1]), sec(self.ilo[1]), sec(self.ihi[1])]


def track(cap: dict, s: dict, prev: dict | None, boost_v: float | None) -> None:
    """The page's capacity tracking (ingest's Ah since full and ahNet, trackRest) on one reading.
    prev may predate a gap; then the lifetime counters bridge it."""
    if cap.get("deficitAh") is None:
        cap |= {"deficitAh": 0, "approx": True}
    for k in ("ahNet", "ahEpoch", "ahNetGapMin", "gapMin"):
        cap.setdefault(k, 0)
    dt = (s["t"] - prev["t"]) / 1000 if prev else None
    if dt is not None and 0 < dt <= GAP_S:
        ah = (prev["netA"] + s["netA"]) / 2 * dt / 3600
        cap["deficitAh"] = max(0, cap["deficitAh"] - ah)
        cap["ahNet"] += ah
    elif dt is not None and dt > GAP_S:
        d_gen, d_cons = s["genTotal"] - prev["genTotal"], s["consTotal"] - prev["consTotal"]
        if d_gen >= 0 and d_cons >= 0:
            ah = (d_gen - d_cons) * 1000 / ((prev["batV"] + s["batV"]) / 2 or 12.8)
            cap["deficitAh"] = max(0, cap["deficitAh"] - ah)
            cap["ahNet"] += ah
            cap["ahNetGapMin"] += round(dt / 60)
        else:
            cap["ahEpoch"] += 1
        cap |= {"approx": True, "gapMin": cap["gapMin"] + round(dt / 60), "restSince": None}
    full_v = boost_v - 0.1 if boost_v else FULL_V
    if s["stage"] == 1 or (s["stage"] == 2 and s["batV"] >= full_v and s["netA"] < FULL_TAIL_A):
        cap |= {"deficitAh": 0, "lastFullAt": s["t"], "approx": False, "gapMin": 0}
    if not (abs(s["netA"]) <= REST_MAX_A and s["chgA"] < 0.05 and s["loadA"] <= REST_MAX_A):
        cap["restSince"] = None
        return
    since = cap["restSince"] = cap.get("restSince") or s["t"]
    if s["t"] - since < REST_MIN * 60000:
        return
    pts = cap.setdefault("restPoints", [])
    pt = {"t": s["t"], "restSince": since, "v": s["batV"], "ahNet": cap["ahNet"], "epoch": cap["ahEpoch"], "gapMin": cap["ahNetGapMin"],
          "ahOut": None if cap["approx"] else cap["deficitAh"], "fullAt": cap.get("lastFullAt")}
    if pts and pts[-1]["restSince"] == since:
        pts[-1] = pt
    else:
        pts.append(pt)
    del pts[:-60]


def get_state(db, k: str):
    return json.loads((db.execute("SELECT v FROM worker_state WHERE k = ?", (k,)).fetchone() or ["null"])[0])


def save_state(db, cap: dict, s: dict) -> None:
    db.executemany("INSERT OR REPLACE INTO worker_state VALUES (?, ?)",
                   [("cap", json.dumps(cap)), ("last", json.dumps({k: s[k] for k in LAST_KEYS}))])


def open_db():
    db = srv.connect()
    db.execute("PRAGMA journal_mode=WAL")
    return db


def put_regs(db, blocks: dict, values: dict, t: int) -> None:
    db.executemany("INSERT OR REPLACE INTO regs VALUES (?, ?, ?, ?, ?, ?)",
                   [(k, *blocks[k], t, json.dumps(v)) for k, v in values.items()])


def read_blocks(ctl: Controller, blocks: dict, required: bool) -> dict:
    out = {}
    for k, blk in blocks.items():
        try:
            out[k] = ctl.read(*blk)
        except (minimalmodbus.ModbusException, OSError):
            if required: raise
    return out


def snapshot(db, s: dict) -> None:
    """Counters at (re)start; with what happened since the last logged reading if that was a gap."""
    last = get_state(db, "last")
    off = last and s["t"] - last["t"] > GAP_S * 1000
    db.execute("INSERT OR REPLACE INTO snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
               (s["t"], s["genTotal"], s["consTotal"], s["batV"], last["t"] if off else None,
                round((s["genTotal"] - last["genTotal"]) * 1000) if off else None,
                round((s["consTotal"] - last["consTotal"]) * 1000) if off else None, last["batV"] if off else None))


def run_commands(db, ctl: Controller, now: int) -> None:
    for cid, t, coil in db.execute("SELECT id, t, coil FROM commands WHERE done IS NULL ORDER BY id").fetchall():
        if now - t > COMMAND_MAX_AGE_MS or coil not in (0, 1):
            result = "expired" if coil in (0, 1) else "invalid"
        else:
            try:
                ctl.set_charging(bool(coil)); result = "ok"
            except (minimalmodbus.ModbusException, OSError) as e:
                result = f"failed: {e}"
        db.execute("UPDATE commands SET done = ?, result = ? WHERE id = ?", (now, result, cid))


def main() -> int:
    port = sys.argv[1] if len(sys.argv) > 1 else next(iter(glob(PORT_GLOB)), None)
    if not port or not Path(port).exists():
        print("EPEver cable not found", file=sys.stderr)
        return 0
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # systemctl stop / shutdown: save the minute and state first
    ctl, db = Controller(port), open_db()
    prev, minute, fails, lvr, boost_v = None, None, 0, None, None
    last, cap = get_state(db, "last"), get_state(db, "cap") or {}
    next_clock = next_cfg = next_state = 0.0
    print(f"polling {port}", file=sys.stderr)
    try:
        while True:
            started = time.monotonic()
            now = time.time_ns() // 1_000_000
            try:
                if started >= next_cfg:
                    cfg = read_blocks(ctl, CFG, required=False)
                    put_regs(db, CFG, cfg, now)
                    if "cfg" in cfg:
                        lvr, boost_v = cfg["cfg"][10] / 100, cfg["cfg"][7] / 100
                    next_cfg = started + CFG_EVERY_S
                b = read_blocks(ctl, POLLED, required=True)
                if started >= next_clock:
                    put_regs(db, CLOCK, read_blocks(ctl, CLOCK, required=False), now)
                    next_clock = started + CLOCK_EVERY_S
                run_commands(db, ctl, now)
                fails = 0
            except (minimalmodbus.ModbusException, OSError, serial.SerialException, termios.error) as e:
                if not Path(port).exists():
                    print("cable unplugged", file=sys.stderr)
                    return 0
                fails += 1
                print(f"read failed ({fails}): {e!r}", file=sys.stderr)
                if fails >= FAIL_LIMIT:
                    return 1
                time.sleep(POLL_S)
                continue
            s = decode(b) | {"t": now}
            put_regs(db, POLLED, b, now)
            if prev is None:
                snapshot(db, s)
            db.execute(f"INSERT OR REPLACE INTO raw ({', '.join(srv.RAW_COLS)}) VALUES ({', '.join('?' * len(srv.RAW_COLS))})",
                       (now, s["batV"], s["netA"], s["loadV"], s["loadA"], s["pvV"], s["chgA"],
                        s["battStatus"], s["chgStatus"], s["dischgStatus"]))
            m = now // 60000
            if minute and minute.m != m:
                db.execute(f"INSERT OR REPLACE INTO minutes VALUES ({', '.join('?' * len(srv.MINUTE_COLS))})", minute.row())
            if not minute or minute.m != m:
                minute = Minute(m)
            minute.add(s, prev, lvr)
            track(cap, s, prev or (last if last and last.get("netA") is not None else None), boost_v)
            if started >= next_state:
                save_state(db, cap, s)
                next_state = started + STATE_EVERY_S
            db.commit()
            prev = s
            time.sleep(max(0.0, POLL_S - (time.monotonic() - started)))
    except KeyboardInterrupt:
        return 0
    finally:
        if minute and minute.n:
            db.execute(f"INSERT OR REPLACE INTO minutes VALUES ({', '.join('?' * len(srv.MINUTE_COLS))})", minute.row())
        if prev:
            save_state(db, cap, prev)
        db.commit()
        db.close()
        ctl.close()


if __name__ == "__main__":
    sys.exit(main())
