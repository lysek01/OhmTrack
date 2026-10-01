#!/usr/bin/env python3
"""Console live monitor for the UT620C+ (cross-platform, no vendor files).

Polls the meter and prints the decoded value + range/mode. Handy for a quick
check without the GUI.  Usage:  python ut620_live.py [seconds]
Any other program using the meter must be closed first (it holds the USB port).
"""
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from ut620_driver import UT620

DURATION = float(sys.argv[1]) if len(sys.argv) > 1 else 15.0


def main():
    dev = UT620()
    if not dev.open():
        print("Could not open the meter:", "; ".join(dev.open_errors))
        return
    print(f"Connected via '{dev._be.name}' backend. Polling for {DURATION:.0f}s...\n")
    t0 = time.time()
    last = None
    while time.time() - t0 < DURATION:
        r = dev.poll()
        s = dev.poll_status()
        if not r.ok:
            line = f"...{r.note}"
        elif r.overload:
            line = "OL (over-range)"
        else:
            line = f"{r.value:g} {r.unit}  =  {r.ohms:g} Ω"
        status = f"   [{s.range_label} · {'Auto' if s.auto else 'Manual'}]" if s.ok else ""
        key = (r.word_a, r.word_b, getattr(s, "range_index", 0))
        if key != last:
            print(f"  {line}{status}")
            last = key
        time.sleep(0.15)
    dev.close()
    print("\ndone")


if __name__ == "__main__":
    main()
