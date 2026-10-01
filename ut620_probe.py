#!/usr/bin/env python3
"""
UT620C+ raw protocol probe (cross-platform, no vendor files).

Sends each known command and prints the raw HID reply, so you can inspect the
protocol yourself. Uses the same backend as the app (native Windows HID, or the
open-source `hidapi` pip package elsewhere).

Usage:  python ut620_probe.py
Any other program using the meter must be closed first (it holds the USB port).
"""
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from ut620_driver import UT620

COMMANDS = {
    "0x05 (live value 'R')": bytes([0x00, 0x05, 0xAB, 0xCD, 0x05, 0x00, 0x05]),
    "0x0A (status 'T')":     bytes([0x00, 0x05, 0xAB, 0xCD, 0x05, 0x00, 0x0A]),
    "0x08 (config 'T')":     bytes([0x00, 0x05, 0xAB, 0xCD, 0x05, 0x00, 0x08]),
    "0x07 (readback 'S')":   bytes([0x00, 0x05, 0xAB, 0xCD, 0x05, 0x00, 0x07]),
}


def hx(b):
    return " ".join(f"{x:02x}" for x in b)


def main():
    dev = UT620()
    if not dev.open():
        print("Could not open the meter:", "; ".join(dev.open_errors))
        return
    be = dev._be
    print(f"Connected via '{be.name}' backend.\n")
    for name, frame in COMMANDS.items():
        be.write(frame)
        replies = []
        for _ in range(4):
            raw = be.read(400)
            if raw:
                replies.append(raw)
        print(f"-> {name}")
        for raw in replies:
            print(f"     {hx(raw[:12])}")
        if not replies:
            print("     (no reply)")
        time.sleep(0.15)
    dev.close()


if __name__ == "__main__":
    main()
