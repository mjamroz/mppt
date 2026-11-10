# /// script
# requires-python = ">=3.14"
# dependencies = ["pyserial>=3.5"]
# ///
"""Read-only Modbus sweep of an EPEver controller: device ID (2B/0E) + single-address FC 01/02/03/04.

Valid addresses and non-"illegal address" anomalies → JSONL (resumable); ranges summary at the end.
Run: uv run epever_scan.py --fc 4 --start 0x3000 --end 0x30FF [--out scan.jsonl] [--port /dev/ttyUSB0]
"""

import argparse
import json
import time
from itertools import groupby
from pathlib import Path

import serial
import serial.rs485

SLAVE = 1
EXC_ILLEGAL_ADDR = 2


def crc16(b: bytes) -> bytes:
    c = 0xFFFF
    for x in b:
        c ^= x
        for _ in range(8):
            c = (c >> 1) ^ 0xA001 if c & 1 else c >> 1
    return c.to_bytes(2, "little")


class Bus:
    def __init__(self, port: str):
        self.s = serial.Serial(port, 115200, timeout=0.05)
        # EPEver cable (XR21B1411): chip RS485 auto-direction, RTS active after send.
        self.s.rs485_mode = serial.rs485.RS485Settings(rts_level_for_tx=False, rts_level_for_rx=True)

    def ask(self, pdu: bytes, timeout: float = 0.3) -> bytes | int | None:
        """PDU → reply PDU | exception code | None (timeout/CRC)."""
        adu = bytes([SLAVE]) + pdu
        self.s.reset_input_buffer()
        self.s.write(adu + crc16(adu))
        buf = bytearray()
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            buf += self.s.read(max(1, self.s.in_waiting))
            while buf and buf[0] != SLAVE:  # line-turnaround garbage (0x00)
                del buf[0]
            if len(buf) < 3:
                continue
            fc = buf[1]
            if fc & 0x80:
                n = 5
            elif fc in (1, 2, 3, 4):
                n = 3 + buf[2] + 2
            else:  # variable length (2B): accept first CRC-valid prefix
                if len(buf) > 4 and crc16(bytes(buf[:-2])) == bytes(buf[-2:]):
                    return bytes(buf[1:-2])
                continue
            if len(buf) >= n:
                frame = bytes(buf[:n])
                if crc16(frame[:-2]) != frame[-2:]:
                    return None
                return frame[2] if fc & 0x80 else frame[1:-2]
        return None


def device_id(bus: Bus) -> dict[int, str]:
    """2B/0E basic+regular objects → {object id: text}."""
    out, obj = {}, 0
    for _ in range(8):
        r = bus.ask(bytes([0x2B, 0x0E, 0x02, obj]), timeout=0.5)
        if not isinstance(r, bytes) or len(r) < 7:
            out[-1] = f"no/invalid reply: {r!r}"
            return out
        more, nxt, count, i = r[4], r[5], r[6], 7
        for _ in range(count):
            oid, ln = r[i], r[i + 1]
            out[oid] = r[i + 2 : i + 2 + ln].decode("latin-1")
            i += 2 + ln
        if more != 0xFF:
            return out
        obj = nxt
    return out


def read_one(bus: Bus, fc: int, addr: int) -> dict | None:
    """One address; None if illegal address."""
    for _ in range(2):
        r = bus.ask(bytes([fc, addr >> 8, addr & 0xFF, 0, 1]))
        if r is not None:
            break
    match r:
        case bytes() if fc in (3, 4):
            return {"v": int.from_bytes(r[2:4])}
        case bytes():
            return {"v": r[2] & 1}
        case int() if r == EXC_ILLEGAL_ADDR:
            return None
        case int():
            return {"x": r}
        case _:
            return {"t": 1}


def done_upto(out: Path) -> dict[int, int]:
    """fc → last completed address (resume)."""
    done = {}
    if out.exists():
        for line in out.read_text().splitlines():
            d = json.loads(line)
            if "done" in d:
                done[d.get("fc")] = max(done.get(d.get("fc"), -1), d.get("done"))
    return done


def summary(out: Path) -> None:
    rows = [json.loads(l) for l in out.read_text().splitlines()]
    hits = sorted({(d.get("fc"), d.get("a")): d for d in rows if "a" in d}.items())  # resume may repeat
    for fc, grp in groupby(hits, key=lambda h: h[0][0]):
        grp = list(grp)
        print(f"\nFC{fc:02X}: {len(grp)} responsive addresses")
        runs = []
        for (_, a), d in grp:
            if runs and a == runs[-1][1] + 1 and ("v" in d) == runs[-1][2]:
                runs[-1][1] = a
            else:
                runs.append([a, a, "v" in d, d])
        for lo, hi, ok, d in runs:
            kind = "values" if ok else ("exception " + str(d.get("x")) if "x" in d else "timeout")
            print(f"  0x{lo:04X}–0x{hi:04X} ({hi - lo + 1:>4}) {kind}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--port", default="/dev/ttyUSB0")
    p.add_argument("--fc", default="4", help="comma list of 1,2,3,4")
    p.add_argument("--start", type=lambda s: int(s, 0), default=0)
    p.add_argument("--end", type=lambda s: int(s, 0), default=0xFFFF)
    p.add_argument("--out", type=Path, default=Path("scan.jsonl"))
    p.add_argument("--id", action="store_true", help="read device identification and exit")
    p.add_argument("--summary", action="store_true", help="summarize --out and exit")
    a = p.parse_args()
    if not set(map(int, a.fc.split(","))) <= {1, 2, 3, 4}:
        p.error("--fc: read function codes 1–4 only")

    if a.summary:
        return summary(a.out)
    bus = Bus(a.port)
    if a.id:
        for oid, text in device_id(bus).items():
            print(f"object {oid}: {text}")
        return

    done = done_upto(a.out)
    with a.out.open("a") as f:
        for fc in map(int, a.fc.split(",")):
            t0, first = time.monotonic(), max(a.start, done.get(fc, -1) + 1)
            for addr in range(first, a.end + 1):
                if (d := read_one(bus, fc, addr)) is not None:
                    f.write(json.dumps({"fc": fc, "a": addr} | d) + "\n")
                    print(f"FC{fc:02X} 0x{addr:04X} {d}", flush=True)
                if addr & 0xFF == 0xFF or addr == a.end:
                    f.write(json.dumps({"fc": fc, "done": addr}) + "\n")
                    f.flush()
                    rate = (addr - first + 1) / (time.monotonic() - t0)
                    print(f"-- FC{fc:02X} done to 0x{addr:04X}, {rate:.0f} addr/s", flush=True)
    summary(a.out)


if __name__ == "__main__":
    main()
