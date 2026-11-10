# /// script
# requires-python = ">=3.14"
# dependencies = ["pyserial>=3.5", "minimalmodbus>=2.1"]
# ///
"""Set the EPEver controller clock (holding 0x9013–0x9015, one FC 0x10 write) to the laptop's local time.

Writes only the clock; reads it before and after.
Run: uv run epever_setclock.py [/dev/ttyUSB0]
"""

import sys
from datetime import datetime

import minimalmodbus
import serial.rs485

CLOCK = 0x9013


def decode(r: list[int]) -> datetime:
    """[min|sec, day|hour, year|month] → datetime."""
    return datetime(2000 + (r[2] >> 8), r[2] & 0xFF, r[1] >> 8, r[1] & 0xFF, r[0] >> 8, r[0] & 0xFF)


def encode(t: datetime) -> list[int]:
    return [t.minute << 8 | t.second, t.day << 8 | t.hour, (t.year - 2000) << 8 | t.month]


def main() -> None:
    inst = minimalmodbus.Instrument(sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyUSB0", 1)
    inst.serial.baudrate = 115200
    inst.serial.timeout = 0.5
    inst.clear_buffers_before_each_transaction = True
    # EPEver cable (XR21B1411): chip RS485 auto-direction, RTS active after send.
    inst.serial.rs485_mode = serial.rs485.RS485Settings(rts_level_for_tx=False, rts_level_for_rx=True)

    before = decode(inst.read_registers(CLOCK, 3, functioncode=3))
    now = datetime.now()
    print(f"controller {before:%Y-%m-%d %H:%M:%S}, laptop {now:%Y-%m-%d %H:%M:%S} ({(before - now).total_seconds():+.0f} s)")
    inst.write_registers(CLOCK, encode(datetime.now()))
    after = decode(inst.read_registers(CLOCK, 3, functioncode=3))
    now = datetime.now()
    print(f"set:       {after:%Y-%m-%d %H:%M:%S}, laptop {now:%Y-%m-%d %H:%M:%S} ({(after - now).total_seconds():+.0f} s)")


if __name__ == "__main__":
    main()
