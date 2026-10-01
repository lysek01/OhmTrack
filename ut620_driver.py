#!/usr/bin/env python3
"""
UT620C+ driver — open the meter over USB HID and decode live resistance readings.

Cross-platform; dependency-free on Windows (native HID backend). On Linux/macOS
install the open-source `hidapi` package. The meter's USB HID command set and reply
frame layout are documented inline; `decode_frame()` turns a reply into a Reading.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

# --- UT620C+ USB HID protocol ---------------------------------------------------
VID = 0x1A86
PID = 0xE429

# Command frames: [report_id=0x00] 05 AB CD 05 00 <CC>
# cmd 0x05 returns the live-value 'R' frame; 0x08/0x0A return a 'T' status frame;
# 0x07 returns an 'S' stored-memory frame.
CMD_LIVE = bytes([0x00, 0x05, 0xAB, 0xCD, 0x05, 0x00, 0x05])   # -> 'R' realtime value
CMD_CONFIG = bytes([0x00, 0x05, 0xAB, 0xCD, 0x05, 0x00, 0x08])  # -> 'T' frame
CMD_CONFIG_ALT = bytes([0x00, 0x05, 0xAB, 0xCD, 0x05, 0x00, 0x0A])
CMD_READBACK = bytes([0x00, 0x05, 0xAB, 0xCD, 0x05, 0x00, 0x07])  # -> 'S' stored memory
# backward-compatible aliases
CMD_LIVE_ALT = CMD_CONFIG
CMD_POLL = CMD_LIVE

MAGIC0, MAGIC1 = 0xAB, 0xCD          # buf[1], buf[2]
TYPE_REALTIME = 0x52                 # buf[5] == 'R'
OVERLOAD_THRESHOLD = 1100.0          # value >= this after scaling -> "OL"
SIGN_OFFSET = 65536.0                # added when overflow/sign nibble == 1

# Autonomous decode of a live-value reply — rules documented below, verified
# against the meter. No calibration needed.
#
# range nibble (WORD_B>>8 & 0xF) -> decade divisor for the mantissa:
RANGE_DIVISOR = {0x8: 10000.0, 0x4: 1000.0, 0x2: 100.0}
RANGE_DIVISOR_DEFAULT = 10.0     # any other nibble -> /10
#
# dec nibble (WORD_B & 0xF) -> display unit. Verified on the meter:
#   dec=4 -> kΩ ;  dec=2 -> Ω ;  dec=1 -> mΩ ;  dec=0 -> µΩ
UNIT_BY_DEC = {0x0: ("µΩ", 1e-6), 0x1: ("mΩ", 1e-3), 0x2: ("Ω", 1.0), 0x4: ("kΩ", 1e3)}
UNIT_BY_DEC_DEFAULT = ("Ω", 1.0)
#
# ohms = (WORD_A[+65536 if sign] / divisor[rng]) * unit_mult[dec]
# (The prefix nibble only drives redundant ×N/÷N display scaling that cancels, so
#  it is decoded for inspection but not used in the value.)


def format_ohms(ohms: float):
    """Human unit for an ohms value -> (display_number, unit_label)."""
    a = abs(ohms)
    if a < 1e-3:
        return ohms * 1e6, "µΩ"
    if a < 1.0:
        return ohms * 1e3, "mΩ"
    if a < 1e3:
        return ohms, "Ω"
    return ohms / 1e3, "kΩ"


@dataclass
class Reading:
    ok: bool
    overload: bool = False
    value: float = 0.0            # number as shown on the meter's display
    unit: str = ""                # µΩ / mΩ / Ω / kΩ
    ohms: float = float("nan")    # value converted to ohms (used for temperature)
    range_nibble: int = 0
    prefix_nibble: int = 0
    decimals_nibble: int = 0
    sign_nibble: int = 0
    word_a: int = 0
    word_b: int = 0
    raw: bytes = b""
    note: str = ""

    @property
    def raw_hex(self) -> str:
        return " ".join(f"{b:02x}" for b in self.raw)


def decode_frame(buf: bytes) -> Reading:
    """Decode a raw HID response into a Reading. See module docstring for the map."""
    if len(buf) < 10:
        return Reading(ok=False, raw=buf, note=f"short frame ({len(buf)}B)")
    if buf[1] != MAGIC0 or buf[2] != MAGIC1:
        return Reading(ok=False, raw=buf, note="bad magic (not AB CD)")
    if buf[5] != TYPE_REALTIME:
        return Reading(ok=False, raw=buf, note=f"not a realtime 'R' frame (type=0x{buf[5]:02x})")

    word_a = (buf[7] << 8) | buf[6]     # raw measurement counts
    word_b = (buf[9] << 8) | buf[8]     # packed status word
    range_nibble = (word_b >> 8) & 0xF
    sign_nibble = (word_b >> 12) & 0xF
    prefix_nibble = (buf[8] >> 4) & 0xF
    decimals_nibble = word_b & 0xF

    adj = word_a + (int(SIGN_OFFSET) if sign_nibble == 1 else 0)
    mantissa = adj / RANGE_DIVISOR.get(range_nibble, RANGE_DIVISOR_DEFAULT)

    common = dict(range_nibble=range_nibble, prefix_nibble=prefix_nibble,
                  decimals_nibble=decimals_nibble, sign_nibble=sign_nibble,
                  word_a=word_a, word_b=word_b, raw=buf)

    if mantissa >= OVERLOAD_THRESHOLD:
        return Reading(ok=True, overload=True, note="OL", **common)

    # unit comes from the dec nibble; ohms is fully determined by the frame.
    unit, unit_mult = UNIT_BY_DEC.get(decimals_nibble, UNIT_BY_DEC_DEFAULT)
    ohms = mantissa * unit_mult
    note = "" if decimals_nibble in UNIT_BY_DEC else f"unknown dec nibble {decimals_nibble:x}"
    return Reading(ok=True, value=mantissa, unit=unit, ohms=ohms, note=note, **common)


# --- Meter status ('T' frame from command 0x0A) --------------------------------
# byte[7] = 0 -> auto range, else manual; byte[8] = range index (see RANGE_LABELS)
TYPE_STATUS = 0x54                    # buf[5] == 'T'
RANGE_LABELS = {1: "100 kΩ", 2: "10 kΩ", 3: "1 kΩ", 4: "100 Ω", 5: "10 Ω",
                6: "1 Ω", 7: "100 mΩ", 8: "10 mΩ", 9: "1 mΩ", 10: "100 µΩ"}

# Measuring (test) current per range index, in amperes. From the UT620C+ datasheet
# (meters.uni-trend.com/product/ut620cplus-micro-ohmmeter): 4-wire, Voc <= 4.2 V.
TEST_CURRENT_BY_RANGE = {
    1: 3e-6,     # 100 kΩ -> 3 µA
    2: 10e-6,    # 10 kΩ  -> 10 µA
    3: 100e-6,   # 1 kΩ   -> 100 µA
    4: 1e-3,     # 100 Ω  -> 1 mA
    5: 10e-3,    # 10 Ω   -> 10 mA
    6: 100e-3,   # 1 Ω    -> 100 mA
    7: 1.0,      # 100 mΩ -> 1 A
    8: 1.0,      # 10 mΩ  -> 1 A
    9: 1.0,      # 1 mΩ   -> 1 A
    10: 1.0,     # 100 µΩ -> 1 A
}


# Datasheet full-scale bands: (upper_ohms, test_current_A, label). In auto-range
# the meter uses the current of the band the reading falls in (shown by the display
# resolution), so deriving both the band and the current from the measured value
# matches the datasheet table exactly.
BANDS = [
    (0.100, 1.0,     "0.001 mΩ – 100 mΩ"),
    (1.0, 100e-3,    "100.1 mΩ – 1000 mΩ"),
    (10.0, 10e-3,    "1.001 Ω – 10 Ω"),
    (100.0, 1e-3,    "10.01 Ω – 100 Ω"),
    (1000.0, 100e-6, "100.1 Ω – 1000 Ω"),
    (10000.0, 10e-6, "1.001 kΩ – 10 kΩ"),
    (300000.0, 3e-6, "10.01 kΩ – 300 kΩ"),
]


def _band_for(ohms):
    a = abs(ohms)
    for hi, amps, label in BANDS:
        if a <= hi:
            return amps, label
    return 3e-6, "10.01 kΩ – 300 kΩ"


def current_for_ohms(ohms):
    """Measuring current (amperes) for a measured resistance, per datasheet bands."""
    if ohms is None or ohms != ohms:
        return None
    return _band_for(ohms)[0]


def band_label_for_ohms(ohms):
    """Full range-band label (e.g. '100.1 Ω – 1000 Ω') for a measured resistance."""
    if ohms is None or ohms != ohms:
        return "—"
    return _band_for(ohms)[1]


def format_amps(a):
    if a is None:
        return "—"
    if a >= 1:
        return f"{a:g} A"
    if a >= 1e-3:
        return f"{a * 1e3:g} mA"
    if a >= 1e-6:
        return f"{a * 1e6:g} µA"
    return f"{a * 1e9:g} nA"


def format_current(range_index):
    return format_amps(TEST_CURRENT_BY_RANGE.get(range_index))


@dataclass
class Status:
    ok: bool
    auto: bool = False
    range_index: int = 0
    range_label: str = ""
    raw: bytes = b""


def decode_status(buf: bytes) -> Status:
    if len(buf) < 9 or buf[1] != MAGIC0 or buf[2] != MAGIC1 or buf[5] != TYPE_STATUS or buf[6] != 0x0A:
        return Status(ok=False, raw=buf)
    auto = buf[7] != 0          # byte7: nonzero = auto-range, 0 = manual (verified on device)
    idx = buf[8]
    return Status(ok=True, auto=auto, range_index=idx,
                  range_label=RANGE_LABELS.get(idx, "—"), raw=buf)


# --- HID backends -------------------------------------------------------------
# Cross-platform, no vendor files. Each backend exposes open()/write()/read()/close().
#   1) hidapi  — the open-source `hidapi` pip package (Windows/Linux/macOS).
#   2) winhid  — native Windows API via ctypes (hid_win.py), zero dependencies.
# No proprietary libraries are required.

class _HidapiBackend:
    name = "hidapi"

    def __init__(self, vid, pid):
        import hid  # pip package "hidapi"  ->  pip install hidapi
        self._hid = hid
        self.vid, self.pid, self.dev = vid, pid, None

    def open(self) -> bool:
        dev = self._hid.device()
        dev.open(self.vid, self.pid)      # raises OSError if the device isn't present
        dev.set_nonblocking(0)
        self.dev = dev
        return True

    def write(self, data: bytes) -> int:
        return self.dev.write(bytes(data))

    def read(self, timeout_ms: int) -> bytes:
        return bytes(self.dev.read(64, timeout_ms))

    def close(self):
        if self.dev:
            self.dev.close()
            self.dev = None


class _WinHidBackend:
    name = "winhid"

    def __init__(self, vid, pid):
        from hid_win import WinHID     # native Windows ctypes, std-lib only
        self.dev = WinHID(vid, pid)

    def open(self) -> bool:
        return self.dev.open()

    def write(self, data: bytes) -> int:
        return self.dev.write(bytes(data))

    def read(self, timeout_ms: int) -> bytes:
        return self.dev.read(timeout_ms)

    def close(self):
        self.dev.close()


def _backend_factories():
    """Order to try: native Windows first (zero deps on Windows), then hidapi."""
    if sys.platform == "win32":
        return [_WinHidBackend, _HidapiBackend]
    return [_HidapiBackend]


class UT620:
    """Cross-platform reader for the UT620C+ over USB HID (no vendor files)."""

    def __init__(self, vid: int = VID, pid: int = PID):
        self.vid, self.pid = vid, pid
        self._be = None
        self.open_errors: list[str] = []

    def open(self) -> bool:
        self.open_errors = []
        for factory in _backend_factories():
            try:
                be = factory(self.vid, self.pid)
                if be.open():
                    self._be = be
                    return True
                self.open_errors.append(f"{factory.name}: device not found")
            except Exception as e:  # ImportError, OSError, etc.
                self.open_errors.append(f"{factory.name}: {e}")
        return False

    def close(self):
        if self._be:
            try:
                self._be.close()
            finally:
                self._be = None

    @property
    def is_open(self) -> bool:
        return self._be is not None

    def _read(self, timeout_ms: int = 300) -> bytes:
        return self._be.read(timeout_ms) if self._be else b""

    def poll(self, command: bytes = CMD_LIVE, timeout_ms: int = 500) -> Reading:
        """Send a live-value request and decode the first valid reply."""
        if not self._be:
            return Reading(ok=False, note="device not open")
        self._be.write(command)
        last = Reading(ok=False, note="no valid reply")
        for _ in range(8):
            raw = self._read(timeout_ms)
            if not raw:
                continue
            r = decode_frame(raw)
            if r.ok:
                return r
            last = r
        return last

    def poll_status(self, timeout_ms: int = 400) -> Status:
        """Query the meter's current range + auto/manual mode (command 0x0A)."""
        if not self._be:
            return Status(ok=False)
        self._be.write(CMD_CONFIG_ALT)
        for _ in range(6):
            raw = self._read(timeout_ms)
            if not raw:
                continue
            s = decode_status(raw)
            if s.ok:
                return s
        return Status(ok=False)

    def __enter__(self):
        if not self.open():
            hint = "; ".join(self.open_errors) or "is the meter on and any other program using it closed?"
            raise RuntimeError(f"could not open {self.vid:04x}:{self.pid:04x} ({hint})")
        return self

    def __exit__(self, *exc):
        self.close()


if __name__ == "__main__":
    import sys, time
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    with UT620() as dev:
        print(f"Opened {VID:04x}:{PID:04x}. Polling live values (Ctrl+C to stop)...")
        while True:
            r = dev.poll()
            if not r.ok:
                print("  ...", r.note, "| raw:", r.raw_hex)
            elif r.overload:
                print("  OL (overload)   raw:", r.raw_hex)
            else:
                print(f"  {r.value:.4f} {r.unit}  = {r.ohms:.6g} Ω   "
                      f"[range={r.range_nibble:x} prefix={r.prefix_nibble:x} dec={r.decimals_nibble:x}]  {r.raw_hex}")
            time.sleep(0.3)
