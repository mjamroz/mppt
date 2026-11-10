# /// script
# requires-python = ">=3.14"
# dependencies = ["pyserial>=3.5", "minimalmodbus>=2.1"]
# ///
"""Read-only probe of an EPEver Tracer over Modbus RTU (FC 0x03/0x04 only).

Prints raw + decoded values to compare against the controller display.
Run: uv run epever_probe.py [/dev/ttyUSB0]
"""

import sys
from datetime import datetime

import minimalmodbus
import serial
import serial.rs485

HOLD, INPUT = 3, 4

# (fc, start, count); a failing block is retried register by register.
BLOCKS = [
    (INPUT, 0x3100, 8),
    (INPUT, 0x310C, 6),
    (INPUT, 0x311A, 1),
    (INPUT, 0x3200, 3),
    (INPUT, 0x3302, 18),
    (INPUT, 0x331B, 2),
    (HOLD, 0x9013, 3),
    (HOLD, 0x9000, 15),
]

# (label, fc, addr, kind, divisor, unit); u32/s32 = low word first.
POINTS = [
    ("PV voltage", INPUT, 0x3100, "u16", 100, "V"),
    ("PV current", INPUT, 0x3101, "u16", 100, "A"),
    ("PV power", INPUT, 0x3102, "u32", 100, "W"),
    ("Battery voltage", INPUT, 0x3104, "u16", 100, "V"),
    ("Charge current", INPUT, 0x3105, "u16", 100, "A"),
    ("Charge power", INPUT, 0x3106, "u32", 100, "W"),
    ("Load voltage", INPUT, 0x310C, "u16", 100, "V"),
    ("Load current", INPUT, 0x310D, "u16", 100, "A"),
    ("Load power", INPUT, 0x310E, "u32", 100, "W"),
    ("Battery temp", INPUT, 0x3110, "s16", 100, "°C"),
    ("Device temp", INPUT, 0x3111, "s16", 100, "°C"),
    ("SoC (doc: x1)", INPUT, 0x311A, "u16", 1, "%"),
    ("Max batt V today", INPUT, 0x3302, "u16", 100, "V"),
    ("Min batt V today", INPUT, 0x3303, "u16", 100, "V"),
    ("Consumed today", INPUT, 0x3304, "u32", 100, "kWh"),
    ("Consumed month", INPUT, 0x3306, "u32", 100, "kWh"),
    ("Consumed year", INPUT, 0x3308, "u32", 100, "kWh"),
    ("Consumed total", INPUT, 0x330A, "u32", 100, "kWh"),
    ("Generated today", INPUT, 0x330C, "u32", 100, "kWh"),
    ("Generated month", INPUT, 0x330E, "u32", 100, "kWh"),
    ("Generated year", INPUT, 0x3310, "u32", 100, "kWh"),
    ("Generated total", INPUT, 0x3312, "u32", 100, "kWh"),
    ("Net battery current", INPUT, 0x331B, "s32", 100, "A"),
    ("Battery type", HOLD, 0x9000, "u16", 1, ""),
    ("Battery capacity", HOLD, 0x9001, "u16", 1, "Ah"),
    ("High volt disconnect", HOLD, 0x9003, "u16", 100, "V"),
    ("Charging limit V", HOLD, 0x9004, "u16", 100, "V"),
    ("Equalize V", HOLD, 0x9006, "u16", 100, "V"),
    ("Boost V", HOLD, 0x9007, "u16", 100, "V"),
    ("Float V", HOLD, 0x9008, "u16", 100, "V"),
    ("Boost reconnect V", HOLD, 0x9009, "u16", 100, "V"),
    ("Low volt reconnect", HOLD, 0x900A, "u16", 100, "V"),
    ("Under volt warning", HOLD, 0x900C, "u16", 100, "V"),
    ("Low volt disconnect", HOLD, 0x900D, "u16", 100, "V"),
    ("Discharge limit V", HOLD, 0x900E, "u16", 100, "V"),
]

STAGES = ("not charging", "float", "boost", "equalize")
BATT_V = {0: "normal", 1: "overvolt", 2: "undervolt", 3: "low-volt disconnect", 4: "fault"}


def read_all(inst: minimalmodbus.Instrument) -> dict[tuple[int, int], int]:
    """Blocks → {(fc, addr): raw u16}; logs failures."""
    regs = {}
    for fc, start, n in BLOCKS:
        try:
            vals = inst.read_registers(start, n, functioncode=fc)
            regs |= {(fc, start + i): v for i, v in enumerate(vals)}
        except (minimalmodbus.ModbusException, serial.SerialException, OSError) as e:
            print(f"! block fc{fc} 0x{start:04X}+{n} failed: {e!r}; trying singles")
            for a in range(start, start + n):
                try:
                    regs[fc, a] = inst.read_register(a, functioncode=fc)
                except (minimalmodbus.ModbusException, serial.SerialException, OSError) as e2:
                    print(f"  ! 0x{a:04X}: {e2!r}")
    return regs


def decode(regs: dict, fc: int, addr: int, kind: str) -> int | None:
    """u16/s16/u32/s32 from raw regs; 32-bit = lo | hi<<16."""
    lo = regs.get((fc, addr))
    if lo is None:
        return None
    if kind in ("u16", "s16"):
        return lo - 0x10000 if kind == "s16" and lo & 0x8000 else lo
    hi = regs.get((fc, addr + 1))
    if hi is None:
        return None
    v = lo | hi << 16
    return v - 0x1_0000_0000 if kind == "s32" and v & 0x8000_0000 else v


def main() -> None:
    port = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyUSB0"
    inst = minimalmodbus.Instrument(port, 1)
    inst.serial.baudrate = 115200
    inst.serial.timeout = 0.5
    inst.clear_buffers_before_each_transaction = True
    # EPEver cable (XR21B1411, xr_serial): needs chip RS485 auto-direction, RTS active after send.
    inst.serial.rs485_mode = serial.rs485.RS485Settings(rts_level_for_tx=False, rts_level_for_rx=True)

    regs = read_all(inst)
    inst.serial.close()

    print(f"\n{'register':<22}{'addr':<8}{'raw':>12}  value")
    for label, fc, addr, kind, div, unit in POINTS:
        raw = decode(regs, fc, addr, kind)
        shown = "—" if raw is None else f"{raw / div:g} {unit}"
        print(f"{label:<22}0x{addr:04X}  {raw if raw is not None else '—':>12}  {shown}")

    if (bs := regs.get((INPUT, 0x3200))) is not None:
        print(f"\nBattery status 0x{bs:04X}: voltage {BATT_V.get(bs & 0xF, bs & 0xF)}, temp code {bs >> 4 & 0xF}")
    if (cs := regs.get((INPUT, 0x3201))) is not None:
        print(f"Charging status 0x{cs:04X}: {STAGES[cs >> 2 & 3]}, running={cs & 1}, fault={cs >> 1 & 1}, input code {cs >> 14}")
    if (ds := regs.get((INPUT, 0x3202))) is not None:
        print(f"Discharging status 0x{ds:04X}: load {'on' if ds & 1 else 'off'}, fault={ds >> 1 & 1}")

    clock = [regs.get((HOLD, a)) for a in (0x9013, 0x9014, 0x9015)]
    if None not in clock:
        s, h, mo = clock
        try:
            dev = datetime(2000 + (mo >> 8), mo & 0xFF, h >> 8, h & 0xFF, s >> 8, s & 0xFF)
            drift = (dev - datetime.now()).total_seconds()
            print(f"\nController clock {dev:%Y-%m-%d %H:%M:%S}, drift vs PC {drift:+.0f} s")
        except ValueError:
            print(f"\nController clock raw {[f'0x{c:04X}' for c in clock]} (invalid date)")


if __name__ == "__main__":
    main()
