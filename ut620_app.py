#!/usr/bin/env python3
"""
OhmTrack — live app for the UNI-T UT620C+ micro-ohm meter.

Live resistance over USB HID, temperature conversion (editable RTD coefficients,
optional 3-point calibration, generic linear, copper winding), configurable graph,
meter range/mode + measuring current, named sensor profiles, and CSV logging with
selectable columns. Built for long unattended runs (all-night logging).

No vendor files. Windows needs nothing extra; Linux/macOS: pip install hidapi.
Run:  python ut620_app.py   (first close any other program that is using the meter)
"""

from __future__ import annotations

import csv
import json
import math
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
import webbrowser
from collections import deque
from pathlib import Path
from tkinter import ttk, filedialog, messagebox

import rtd
from ut620_driver import (UT620, Reading, Status, CMD_LIVE, format_amps,
                          current_for_ohms, band_label_for_ohms)

APP_NAME = "OhmTrack"
APP_VERSION = "1.0.1"
AUTHOR = "Martin Lysek"
AUTHOR_URL = "https://github.com/lysek01"

# Profiles live next to the program (portable): beside the .exe when frozen,
# otherwise beside this script.
if getattr(sys, "frozen", False):
    _APP_DIR = os.path.dirname(sys.executable)
else:
    _APP_DIR = os.path.dirname(os.path.abspath(__file__))
PROFILE_DIR = Path(_APP_DIR) / "profiles"

STATUS_PERIOD_S = 2.0   # how often to refresh the meter's range/mode/current
PLOT_MAXPTS = 20000

THEMES = {
    "light": dict(bg="#f3f4f6", card="#ffffff", text="#1b1f24", muted="#6b7280",
                  accent="#2563eb", accent2="#ea580c", grid="#e5e7eb", guide="#9ca3af",
                  chart_bg="#ffffff", border="#d7dbe0", entry="#ffffff", link="#2563eb",
                  btn="#e8eaed", btn_text="#1b1f24"),
    "dark":  dict(bg="#0d0f13", card="#141821", text="#e6e9ee", muted="#9aa4b2",
                  accent="#4ea1ff", accent2="#ff8c42", grid="#242a33", guide="#4b5563",
                  chart_bg="#0f1218", border="#2b333d", entry="#1a1f27", link="#6db5ff",
                  btn="#222a35", btn_text="#e6e9ee"),
}

MODELS = ["Pt100", "Pt1000", "NTC (Beta)", "Linear (generic)", "Copper winding rise"]
RTD_MODELS = ("Pt100", "Pt1000")
NTC_MODELS = ("NTC (Beta)",)
CAL_MODELS = RTD_MODELS + NTC_MODELS
INTERVALS = {"0.1 s": 0.1, "0.2 s": 0.2, "0.25 s": 0.25, "0.5 s": 0.5,
             "1 s": 1.0, "2 s": 2.0, "5 s": 5.0, "10 s": 10.0}
WINDOWS = {"30 s": 30, "1 min": 60, "2 min": 120, "5 min": 300, "10 min": 600,
           "30 min": 1800, "1 h": 3600, "All": None}
SOURCES = ["Temperature (°C)", "Resistance (Ω)"]

PARAM_DEFS = {
    "R0":    ("R₀  [Ω]", "100"),
    "A":     ("A  [1/°C]", "3.9083e-3"),
    "B":     ("B  [1/°C²]", "-5.775e-7"),
    "C":     ("C  [1/°C⁴] (T<0)", "-4.183e-12"),
    "T0":    ("T₀  [°C]", "20"),
    "alpha": ("α  [1/°C]", "0.00393"),
    "Beta":  ("β  [K]", "3950"),
    "R1":    ("R₁ cold  [Ω]", "1.0"),
    "T1":    ("T₁ cold  [°C]", "20"),
    "k":     ("k (Cu 235 / Al 225)", "235"),
}
MODEL_PARAMS = {
    "Pt100": ["R0", "A", "B", "C"],
    "Pt1000": ["R0", "A", "B", "C"],
    "NTC (Beta)": ["R0", "T0", "Beta"],
    "Linear (generic)": ["R0", "T0", "alpha"],
    "Copper winding rise": ["R1", "T1", "k"],
}
CAL_ROWS_MAX = 6

CSV_COLUMNS = {
    "timestamp": "timestamp", "t_rel_s": "t_rel_s", "value": "value", "unit": "unit",
    "ohms": "ohms", "temperature_C": "temperature_C", "current_A": "current_A",
    "range": "range", "mode": "mode", "overload": "overload", "raw": "raw",
}
CSV_DEFAULT = ["timestamp", "ohms", "temperature_C"]


def resource_path(rel):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


# --------------------------------------------------------------------------- #
class Acquirer(threading.Thread):
    """Polls the meter; auto-reconnects if the USB link drops (for long runs)."""

    def __init__(self, out_q, interval_provider):
        super().__init__(daemon=True)
        self.out_q = out_q
        self.interval_provider = interval_provider
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        dev, fails = None, 0
        next_t = time.monotonic()
        last_status = 0.0
        while not self._stop.is_set():
            if dev is None:
                try:
                    dev = UT620()
                    if not dev.open():
                        self.out_q.put(("error", "Could not open the meter (1A86:E429).\n"
                                                 "Turn it on, plug in USB, and close any other program using it."))
                        return
                    self.out_q.put(("conn", "Connected"))
                    fails = 0
                    next_t = time.monotonic()
                except Exception as e:
                    self.out_q.put(("error", str(e)))
                    return
            r = dev.poll(CMD_LIVE)
            self.out_q.put(("reading", r))
            fails = 0 if r.ok else fails + 1
            if fails >= 20:
                self.out_q.put(("conn", "Reconnecting…"))
                try:
                    dev.close()
                except Exception:
                    pass
                dev, fails = None, 0
                self._stop.wait(1.0)
                next_t = time.monotonic()
                continue
            # refresh range/mode on a time basis (not every Nth sample) so it stays
            # light at fast sample rates
            now = time.monotonic()
            if now - last_status >= STATUS_PERIOD_S:
                s = dev.poll_status()
                if s.ok:
                    self.out_q.put(("meter", s))
                last_status = now
            # Fixed-rate pacing: keep the AVERAGE period equal to the set interval.
            # Sleeping the whole interval *after* the work would make each cycle take
            # interval + work, so the cadence drifts and 1 s timestamps occasionally
            # skip a second. Here we schedule the next tick instead, so per-cycle work
            # time does not accumulate into drift.
            interval = max(0.05, float(self.interval_provider()))
            next_t += interval
            delay = next_t - time.monotonic()
            if delay > 0:
                if self._stop.wait(delay):
                    break
            elif delay < -interval:      # a real stall (reconnect / long read) -> resync
                next_t = time.monotonic()
        if dev:
            dev.close()
        self.out_q.put(("conn", "Disconnected"))


# --------------------------------------------------------------------------- #
class StripChart(tk.Canvas):
    def __init__(self, master, theme, **kw):
        super().__init__(master, highlightthickness=0, **kw)
        self.theme = theme
        self.data = deque(maxlen=PLOT_MAXPTS)
        self.window_s = 120
        self.ymode = "Auto"
        self.ymin_fixed, self.ymax_fixed = 0.0, 100.0
        self.show_minmax = True
        self.color_key = "accent2"
        self.stats = (float("nan"),) * 4
        self.configure(background=theme["chart_bg"])
        self.bind("<Configure>", lambda e: self.redraw())

    def set_theme(self, theme):
        self.theme = theme
        self.configure(background=theme["chart_bg"])
        self.redraw()

    def add(self, t, y):
        if y is not None and math.isfinite(y):
            self.data.append((t, y))

    def visible(self):
        if not self.data:
            return []
        now = self.data[-1][0]
        if self.window_s is None:
            return list(self.data)
        return [(t, y) for (t, y) in self.data if now - t <= self.window_s] or list(self.data)

    def redraw(self):
        self.delete("all")
        th = self.theme
        pts = self.visible()
        if pts:
            ys = [y for _, y in pts]
            self.stats = (ys[-1], min(ys), max(ys), sum(ys) / len(ys))
        else:
            self.stats = (float("nan"),) * 4
        w, h = self.winfo_width(), self.winfo_height()
        if w < 40 or h < 40 or len(pts) < 2:
            return
        ys = [y for _, y in pts]
        if self.ymode == "Fixed" and self.ymax_fixed > self.ymin_fixed:
            ymin, ymax = self.ymin_fixed, self.ymax_fixed
        else:
            ymin, ymax = min(ys), max(ys)
            if ymax - ymin < 1e-6:
                ymin -= 1; ymax += 1
            pad = (ymax - ymin) * 0.12
            ymin -= pad; ymax += pad
        t0, t1 = pts[0][0], pts[-1][0]
        span = max(t1 - t0, 1e-3)
        ml, mr, mt, mb = 60, 12, 12, 22
        sx = lambda t: ml + (t - t0) / span * (w - ml - mr)
        sy = lambda y: mt + (ymax - y) / (ymax - ymin) * (h - mt - mb)
        for i in range(5):
            yy = ymin + (ymax - ymin) * i / 4
            py = sy(yy)
            self.create_line(ml, py, w - mr, py, fill=th["grid"])
            self.create_text(ml - 6, py, text=f"{yy:.2f}", anchor="e", fill=th["muted"], font=("Segoe UI", 8))
        for i in range(5):
            tt = t0 + span * i / 4
            px = sx(tt)
            self.create_line(px, mt, px, h - mb, fill=th["grid"])
            self.create_text(px, h - mb + 10, text=f"-{t1 - tt:.0f}s", anchor="n", fill=th["muted"], font=("Segoe UI", 8))
        if self.show_minmax:
            for val in (min(ys), max(ys)):
                if ymin <= val <= ymax:
                    self.create_line(ml, sy(val), w - mr, sy(val), fill=th["guide"], dash=(4, 3))
        coords = []
        for (t, y) in pts:
            coords += [sx(t), sy(y)]
        self.create_line(*coords, fill=th[self.color_key], width=2, smooth=True)
        self.create_oval(sx(t1) - 3, sy(pts[-1][1]) - 3, sx(t1) + 3, sy(pts[-1][1]) + 3,
                         fill=th[self.color_key], outline="")


# --------------------------------------------------------------------------- #
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1100x780")
        self.minsize(960, 690)
        self._icon = None
        try:
            self._icon = tk.PhotoImage(file=resource_path("assets/icon.png"))
            self.iconphoto(True, self._icon)
        except Exception:
            pass

        self.q = queue.Queue()
        self.acq = None
        self.t_start = time.time()
        self.csv_writer = self.csv_file = None
        self.csv_columns = list(CSV_DEFAULT)
        self.last = None
        self.meter = None
        self.current_a = None
        self.history = deque(maxlen=PLOT_MAXPTS)
        self.paused = False
        self.interval_s = 0.25
        self.theme_name = "light"
        self.theme = THEMES[self.theme_name]

        self._bg_frames, self._card_frames = [], []
        self._bg_labels, self._card_labels, self._muted_labels = [], [], []
        self._buttons = []

        self._tkstyle = ttk.Style(self)
        try:
            self._tkstyle.theme_use("clam")
        except tk.TclError:
            pass
        self._build_ui()
        self._apply_theme()
        self._refresh_profiles()
        self.after(80, self._drain)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---- themed widget factories ------------------------------------------ #
    def _frame(self, master, card=False, **kw):
        f = tk.Frame(master, **kw)
        (self._card_frames if card else self._bg_frames).append(f)
        return f

    def _label(self, master, card=False, muted=False, **kw):
        l = tk.Label(master, **kw)
        if muted:
            self._muted_labels.append((l, card))
        else:
            (self._card_labels if card else self._bg_labels).append(l)
        return l

    def _button(self, master, **kw):
        b = tk.Button(master, relief="flat", cursor="hand2", borderwidth=0,
                      highlightthickness=1, padx=10, pady=5, **kw)
        self._buttons.append(b)
        return b

    # ---- UI ---------------------------------------------------------------- #
    def _build_ui(self):
        # --- top bar (always visible) ---
        top = self._frame(self)
        top.pack(side="top", fill="x", padx=14, pady=(12, 6))
        self.btn_connect = self._button(top, text="Connect", width=11, command=self.on_connect)
        self.btn_connect.pack(side="left")
        self.conn_var = tk.StringVar(value="Disconnected")
        self._label(top, muted=True, textvariable=self.conn_var, font=("Segoe UI", 10)).pack(side="left", padx=12)
        self._label(top, text="Sampling:").pack(side="left", padx=(10, 4))
        self.interval_var = tk.StringVar(value="0.25 s")
        ib = ttk.Combobox(top, textvariable=self.interval_var, width=7, state="readonly", values=list(INTERVALS))
        ib.pack(side="left")
        ib.bind("<<ComboboxSelected>>", lambda e: self._on_interval())
        self.btn_theme = self._button(top, text="🌙  Dark", command=self.on_toggle_theme)
        self.btn_theme.pack(side="right")
        self.btn_log = self._button(top, text="Start CSV log", command=self.on_log)
        self.btn_log.pack(side="right", padx=(0, 8))
        self.show_debug = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="Raw data", variable=self.show_debug,
                        command=self._toggle_debug).pack(side="right", padx=(0, 8))

        read = self._frame(self)
        read.pack(side="top", fill="x", padx=14, pady=6)
        self.r_card = self._card(read, "RESISTANCE", "accent")
        self.r_card["frame"].pack(side="left", expand=True, fill="both", padx=(0, 6))
        self.t_card = self._card(read, "TEMPERATURE", "accent2")
        self.t_card["frame"].pack(side="left", expand=True, fill="both", padx=(6, 0))

        self.meter_var = tk.StringVar(value="Meter: —")
        self._label(self, muted=True, textvariable=self.meter_var, font=("Segoe UI", 10),
                    anchor="w").pack(side="top", fill="x", padx=16, pady=(0, 4))

        # --- footer + bottom bar, reserved at the bottom BEFORE the middle so they
        #     are never clipped when the window is short ---
        footer = self._frame(self)
        footer.pack(side="bottom", fill="x", padx=16, pady=(0, 10))
        self._label(footer, muted=True, text=f"{APP_NAME}  ·  v{APP_VERSION}", font=("Segoe UI", 8)).pack(side="left")
        self.author_lbl = tk.Label(footer, text=f"Made by {AUTHOR}", font=("Segoe UI", 8, "underline"), cursor="hand2")
        self.author_lbl.pack(side="right")
        self.author_lbl.bind("<Button-1>", lambda e: webbrowser.open(AUTHOR_URL))

        # raw/debug info bar — packed on demand (only while "Raw data" is on)
        self.bottom_frame = self._frame(self)
        self.raw_var = tk.StringVar(value="")
        self.raw_lbl = self._label(self.bottom_frame, muted=True, textvariable=self.raw_var,
                                   font=("Consolas", 9), anchor="w")

        # --- middle (chart + settings) fills whatever is left ---
        mid = self._frame(self)
        mid.pack(side="top", fill="both", expand=True, padx=14, pady=6)
        chart_col = self._frame(mid)
        chart_col.pack(side="left", fill="both", expand=True)
        self._build_graph_controls(chart_col)
        self.chart = StripChart(chart_col, self.theme)
        self.chart.pack(fill="both", expand=True, padx=8, pady=(4, 6))
        self._build_stats(chart_col)

        side = self._frame(mid, card=True, width=330)
        side.pack(side="left", fill="y", padx=(10, 0))
        side.pack_propagate(False)
        side.configure(highlightthickness=1)
        self.side = side
        self._build_settings(side)

    def _build_graph_controls(self, parent):
        bar = self._frame(parent)
        bar.pack(fill="x", padx=8, pady=(6, 0))
        self._label(bar, text="Plot:").pack(side="left")
        self.source_var = tk.StringVar(value=SOURCES[0])
        sb = ttk.Combobox(bar, textvariable=self.source_var, width=16, state="readonly", values=SOURCES)
        sb.pack(side="left", padx=(4, 10))
        sb.bind("<<ComboboxSelected>>", lambda e: self._rebuild_chart())
        self._label(bar, text="Window:").pack(side="left")
        self.window_var = tk.StringVar(value="2 min")
        wb = ttk.Combobox(bar, textvariable=self.window_var, width=6, state="readonly", values=list(WINDOWS))
        wb.pack(side="left", padx=(4, 10))
        wb.bind("<<ComboboxSelected>>", lambda e: self._apply_graph_opts())
        self._label(bar, text="Y:").pack(side="left")
        self.ymode_var = tk.StringVar(value="Auto")
        yb = ttk.Combobox(bar, textvariable=self.ymode_var, width=6, state="readonly", values=["Auto", "Fixed"])
        yb.pack(side="left", padx=(4, 6))
        yb.bind("<<ComboboxSelected>>", lambda e: self._apply_graph_opts())
        self.ymin_var, self.ymax_var = tk.StringVar(value="0"), tk.StringVar(value="100")
        self.ymin_ent = ttk.Entry(bar, textvariable=self.ymin_var, width=6)
        self.ymax_ent = ttk.Entry(bar, textvariable=self.ymax_var, width=6)
        self.ymin_ent.pack(side="left"); self._label(bar, text="–").pack(side="left")
        self.ymax_ent.pack(side="left", padx=(0, 10))
        for e in (self.ymin_ent, self.ymax_ent):
            e.bind("<Return>", lambda ev: self._apply_graph_opts())
            e.bind("<FocusOut>", lambda ev: self._apply_graph_opts())
        self.minmax_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="min/max", variable=self.minmax_var, command=self._apply_graph_opts).pack(side="left", padx=(0, 8))
        self.btn_pause = self._button(bar, text="Pause", width=7, command=self._toggle_pause)
        self.btn_pause.pack(side="right", padx=(4, 0))
        self._button(bar, text="Clear", width=6, command=self._clear_chart).pack(side="right")

    def _build_stats(self, parent):
        row = self._frame(parent)
        row.pack(fill="x", padx=8, pady=(0, 6))
        self.stat_vars = {}
        for key, label in (("cur", "Current"), ("min", "Min"), ("max", "Max"), ("avg", "Avg")):
            cell = self._frame(row, card=True)
            cell.pack(side="left", expand=True, fill="x", padx=3)
            cell.configure(highlightthickness=1)
            self._label(cell, card=True, muted=True, text=label, font=("Segoe UI", 8)).pack(anchor="w", padx=8, pady=(4, 0))
            v = tk.StringVar(value="—")
            self._label(cell, card=True, textvariable=v, font=("Segoe UI", 13, "bold")).pack(anchor="w", padx=8, pady=(0, 5))
            self.stat_vars[key] = v

    def _card(self, parent, title, accent_key):
        f = self._frame(parent, card=True)
        f.configure(highlightthickness=1)
        self._label(f, card=True, muted=True, text=title, font=("Segoe UI", 10)).pack(anchor="w", padx=16, pady=(12, 0))
        val = tk.Label(f, text="—", font=("Segoe UI", 44, "bold"))
        val.pack(anchor="w", padx=16)
        sub = self._label(f, card=True, muted=True, text="", font=("Consolas", 10))
        sub.pack(anchor="w", padx=16, pady=(0, 12))
        return {"frame": f, "val": val, "sub": sub, "accent": accent_key}

    def _build_settings(self, parent):
        self._label(parent, card=True, text="Resistance → temperature",
                    font=("Segoe UI", 11, "bold"), anchor="w").pack(anchor="w", padx=12, pady=(12, 8))
        self.model_var = tk.StringVar(value="Pt100")
        mb = ttk.Combobox(parent, textvariable=self.model_var, state="readonly", values=MODELS)
        mb.pack(fill="x", padx=12)
        mb.bind("<<ComboboxSelected>>", lambda e: self._on_model_change())

        grid = self._frame(parent, card=True)
        grid.columnconfigure(0, weight=1)
        self.grid_frame = grid
        self.param_widgets = {}
        for key, (label, default) in PARAM_DEFS.items():
            lbl = self._label(grid, card=True, text=label, anchor="w")
            var = tk.StringVar(value=default)
            ent = ttk.Entry(grid, textvariable=var, width=13)
            self.param_widgets[key] = (lbl, ent, var)

        # calibration (RTD only)
        self.cal_on = tk.BooleanVar(value=False)
        self.cal_chk = ttk.Checkbutton(parent, text="Apply calibration (from cal sheet)",
                                       variable=self.cal_on, command=self._sync_cal)
        self.cal_frame = self._frame(parent, card=True)
        self._label(self.cal_frame, card=True, text="Cal points  (T [°C] → R [Ω])",
                    font=("Segoe UI", 9), anchor="w").grid(row=0, column=0, columnspan=2, sticky="w", pady=(2, 4))
        self.cal_rows = []
        self.btn_add_cal = self._button(self.cal_frame, text="+ Add point", command=self._add_cal_row)
        self.cal_info = tk.StringVar(value="")
        self.cal_info_lbl = self._label(self.cal_frame, card=True, muted=True, textvariable=self.cal_info,
                                        font=("Consolas", 8), justify="left", anchor="w")
        for _ in range(3):
            self._add_cal_row()

        self.btn_r1 = self._button(parent, text="R₁ := current resistance", command=self._set_r1_current)

        # named profiles
        prof = self._frame(parent, card=True)
        self.prof_frame = prof
        prof.pack(fill="x", padx=12, pady=(6, 10))
        self._label(prof, card=True, text="Profile", font=("Segoe UI", 9), anchor="w").pack(anchor="w")
        self.profile_name = tk.StringVar(value="")
        self.profile_box = ttk.Combobox(prof, textvariable=self.profile_name, values=[])
        self.profile_box.pack(fill="x", pady=(2, 4))
        self.profile_box.bind("<<ComboboxSelected>>", lambda e: self._load_profile())
        prow = self._frame(prof, card=True)
        prow.pack(fill="x")
        self._button(prow, text="Save", command=self._save_profile).pack(side="left", expand=True, fill="x", padx=(0, 3))
        self._button(prow, text="Refresh", command=self._refresh_profiles).pack(side="left", expand=True, fill="x", padx=3)
        self._button(prow, text="Delete", command=self._delete_profile).pack(side="left", expand=True, fill="x", padx=(3, 0))

        self.decoded_var = tk.StringVar(value="")
        self.decoded_lbl = self._label(parent, card=True, muted=True, textvariable=self.decoded_var,
                                       font=("Consolas", 8), justify="left", anchor="w")
        self._on_model_change()

    def _add_cal_row(self, t_val="", r_val=""):
        if len(self.cal_rows) >= CAL_ROWS_MAX:
            return
        i = len(self.cal_rows) + 1
        tv, rv = tk.StringVar(value=str(t_val)), tk.StringVar(value=str(r_val))
        te = ttk.Entry(self.cal_frame, textvariable=tv, width=9)
        re_ = ttk.Entry(self.cal_frame, textvariable=rv, width=9)
        te.grid(row=i, column=0, sticky="w", pady=2)
        re_.grid(row=i, column=1, sticky="w", pady=2, padx=(6, 0))
        self.cal_rows.append((tv, rv, te, re_))
        self.btn_add_cal.grid(row=CAL_ROWS_MAX + 1, column=0, columnspan=2, sticky="we", pady=(4, 2))
        self.cal_info_lbl.grid(row=CAL_ROWS_MAX + 2, column=0, columnspan=2, sticky="w", pady=(4, 2))
        if len(self.cal_rows) >= CAL_ROWS_MAX:
            self.btn_add_cal.grid_remove()

    # ---- theme ------------------------------------------------------------- #
    def on_toggle_theme(self):
        self.theme_name = "dark" if self.theme_name == "light" else "light"
        self.theme = THEMES[self.theme_name]
        self.btn_theme.configure(text=("☀  Light" if self.theme_name == "dark" else "🌙  Dark"))
        self._apply_theme()

    def _apply_theme(self):
        th = self.theme
        self.configure(bg=th["bg"])
        s = self._tkstyle
        s.configure(".", background=th["bg"], foreground=th["text"])
        s.configure("TCombobox", fieldbackground=th["entry"], background=th["entry"], foreground=th["text"])
        s.configure("TEntry", fieldbackground=th["entry"], foreground=th["text"])
        s.configure("TCheckbutton", background=th["bg"], foreground=th["text"])
        s.map("TCombobox", fieldbackground=[("readonly", th["entry"])])
        s.map("TCheckbutton", background=[("active", th["bg"])])
        for f in self._bg_frames:
            f.configure(bg=th["bg"])
        for f in self._card_frames:
            f.configure(bg=th["card"], highlightbackground=th["border"])
        for l in self._bg_labels:
            l.configure(bg=th["bg"], fg=th["text"])
        for l in self._card_labels:
            l.configure(bg=th["card"], fg=th["text"])
        for (l, card) in self._muted_labels:
            l.configure(bg=(th["card"] if card else th["bg"]), fg=th["muted"])
        for b in self._buttons:
            b.configure(bg=th["btn"], fg=th["btn_text"], activebackground=th["grid"],
                        activeforeground=th["text"], highlightbackground=th["border"])
        for card in (self.r_card, self.t_card):
            card["val"].configure(bg=th["card"], fg=th[card["accent"]])
        self.side.configure(highlightbackground=th["border"])
        self.author_lbl.configure(bg=th["bg"], fg=th["link"])
        self.chart.set_theme(th)

    # ---- graph ------------------------------------------------------------- #
    def _on_interval(self):
        self.interval_s = INTERVALS.get(self.interval_var.get(), 0.25)

    def _apply_graph_opts(self):
        self.chart.window_s = WINDOWS.get(self.window_var.get(), 120)
        self.chart.ymode = self.ymode_var.get()
        self.chart.show_minmax = self.minmax_var.get()
        fixed = self.chart.ymode == "Fixed"
        self.ymin_ent.configure(state=("normal" if fixed else "disabled"))
        self.ymax_ent.configure(state=("normal" if fixed else "disabled"))
        try:
            self.chart.ymin_fixed = float(self.ymin_var.get())
            self.chart.ymax_fixed = float(self.ymax_var.get())
        except ValueError:
            pass
        self.chart.redraw()
        self._update_stats()

    def _rebuild_chart(self):
        is_res = self.source_var.get().startswith("Resistance")
        self.chart.color_key = "accent" if is_res else "accent2"
        self.chart.data.clear()
        for (t, temp, ohms) in self.history:
            v = ohms if is_res else temp
            if v is not None and math.isfinite(v):
                self.chart.data.append((t, v))
        self.chart.redraw()
        self._update_stats()

    def _toggle_pause(self):
        self.paused = not self.paused
        self.btn_pause.configure(text=("Resume" if self.paused else "Pause"))

    def _clear_chart(self):
        self.history.clear()
        self.chart.data.clear()
        self.chart.redraw()
        self._update_stats()

    def _update_stats(self):
        cur, mn, mx, avg = self.chart.stats
        is_res = self.source_var.get().startswith("Resistance")
        def fmt(v):
            return "—" if not math.isfinite(v) else (f"{v:.4g} Ω" if is_res else f"{v:.2f} °C")
        for key, val in (("cur", cur), ("min", mn), ("max", mx), ("avg", avg)):
            self.stat_vars[key].set(fmt(val))

    # ---- debug toggle ------------------------------------------------------ #
    def _toggle_debug(self):
        if self.show_debug.get():
            self.bottom_frame.pack(side="bottom", fill="x", padx=14, pady=(0, 2))
            self.raw_lbl.pack(side="left")
            self.decoded_lbl.pack(anchor="w", padx=12, pady=(6, 10))
        else:
            self.raw_lbl.pack_forget()
            self.decoded_lbl.pack_forget()
            self.bottom_frame.pack_forget()
            self.raw_var.set("")

    # ---- model / params ---------------------------------------------------- #
    def _on_model_change(self):
        m = self.model_var.get()
        if m == "Pt100":
            self.param_widgets["R0"][2].set("100"); self._reset_cvd()
        elif m == "Pt1000":
            self.param_widgets["R0"][2].set("1000"); self._reset_cvd()
        elif m == "NTC (Beta)":
            self.param_widgets["R0"][2].set("10000")
            self.param_widgets["T0"][2].set("25")
            self.param_widgets["Beta"][2].set("3950")
        self._layout_params()

    def _reset_cvd(self):
        self.param_widgets["A"][2].set("3.9083e-3")
        self.param_widgets["B"][2].set("-5.775e-7")
        self.param_widgets["C"][2].set("-4.183e-12")

    def _layout_params(self):
        m = self.model_var.get()
        keys = MODEL_PARAMS.get(m, [])
        for lbl, ent, _ in self.param_widgets.values():
            lbl.grid_remove(); ent.grid_remove()
        for i, key in enumerate(keys):
            lbl, ent, _ = self.param_widgets[key]
            lbl.grid(row=i, column=0, sticky="w", pady=3)
            ent.grid(row=i, column=1, sticky="e", pady=3, padx=(8, 0))
        self.grid_frame.pack_forget()
        self.cal_chk.pack_forget()
        self.cal_frame.pack_forget()
        self.btn_r1.pack_forget()
        self.grid_frame.pack(fill="x", padx=12, pady=(10, 4), before=self.prof_frame)
        if m in CAL_MODELS:
            self.cal_chk.pack(anchor="w", padx=12, pady=(2, 2), before=self.prof_frame)
        else:
            self.cal_on.set(False)
        if m == "Copper winding rise":
            self.btn_r1.pack(fill="x", padx=12, pady=(0, 6), before=self.prof_frame)
        self._sync_cal()

    def _sync_cal(self):
        m = self.model_var.get()
        on = self.cal_on.get() and m in CAL_MODELS
        if on:
            self.cal_frame.pack(fill="x", padx=12, pady=(0, 4), before=self.prof_frame)
        else:
            self.cal_frame.pack_forget()
        # when calibrating, this model's coefficients are driven by the fit -> disable them
        for key, (_, ent, _) in self.param_widgets.items():
            if ent.winfo_manager():  # only the ones currently shown
                ent.configure(state=("disabled" if on else "normal"))
        self._update_cal_info()

    def _collect_cal_points(self):
        pts = []
        for (tv, rv, _, _) in self.cal_rows:
            ts, rs = tv.get().strip(), rv.get().strip()
            if ts and rs:
                try:
                    pts.append((float(ts), float(rs)))
                except ValueError:
                    pass
        return pts

    def _update_cal_info(self):
        m = self.model_var.get()
        if not (self.cal_on.get() and m in CAL_MODELS):
            self.cal_info.set("")
            return
        pts = self._collect_cal_points()
        if len(pts) < 2:
            self.cal_info.set("need ≥ 2 points (3 for full fit)")
            return
        if m in RTD_MODELS:
            co = rtd.cvd_coeffs_from_points(pts)
            self.cal_info.set(f"fit: R₀={co[0]:.4g}\nA={co[1]:.5g}  B={co[2]:.4g}" if co else "fit failed")
        else:  # NTC -> Steinhart–Hart
            co = rtd.steinhart_hart_from_points(pts)
            self.cal_info.set(f"Steinhart–Hart:\nA={co[0]:.4g} B={co[1]:.4g}\nC={co[2]:.4g}" if co else "fit failed")

    def _param(self, key, default=0.0):
        try:
            return float(self.param_widgets[key][2].get())
        except (ValueError, KeyError):
            return default

    def _set_r1_current(self):
        r = self.last
        if r and r.ok and not r.overload and math.isfinite(r.ohms):
            self.param_widgets["R1"][2].set(f"{r.ohms:.6g}")

    # ---- profiles ---------------------------------------------------------- #
    def _refresh_profiles(self):
        try:
            PROFILE_DIR.mkdir(parents=True, exist_ok=True)
            names = sorted(p.stem for p in PROFILE_DIR.glob("*.json"))
        except OSError:
            names = []
        self.profile_box.configure(values=names)

    def _profile_path(self, name):
        safe = re.sub(r"[^\w\-. ]+", "_", name).strip() or "profile"
        return PROFILE_DIR / f"{safe}.json"

    def _profile_dict(self, name):
        return {
            "name": name,
            "model": self.model_var.get(),
            "params": {k: v.get() for k, (_, _, v) in self.param_widgets.items()},
            "cal_on": bool(self.cal_on.get()),
            "calibration": [(tv.get(), rv.get()) for (tv, rv, _, _) in self.cal_rows],
        }

    def _save_profile(self):
        name = self.profile_name.get().strip()
        if not name:
            messagebox.showinfo("Profile", "Type a profile name first.")
            return
        try:
            PROFILE_DIR.mkdir(parents=True, exist_ok=True)
            with open(self._profile_path(name), "w", encoding="utf-8") as f:
                json.dump(self._profile_dict(name), f, indent=2, ensure_ascii=False)
        except OSError as e:
            messagebox.showerror("Save profile", str(e))
            return
        self._refresh_profiles()
        self.profile_name.set(name)

    def _load_profile(self):
        name = self.profile_name.get().strip()
        path = self._profile_path(name)
        if not path.exists():
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            messagebox.showerror("Load profile", str(e))
            return
        if data.get("model") in MODELS:
            self.model_var.set(data["model"])
        self._on_model_change()
        for k, val in (data.get("params") or {}).items():
            if k in self.param_widgets:
                self.param_widgets[k][2].set(str(val))
        for (tv, rv, te, re_) in self.cal_rows:
            te.destroy(); re_.destroy()
        self.cal_rows = []
        for pair in (data.get("calibration") or [])[:CAL_ROWS_MAX]:
            t, r = (pair + ["", ""])[:2] if isinstance(pair, list) else (pair[0], pair[1])
            self._add_cal_row(t, r)
        while len(self.cal_rows) < 3:
            self._add_cal_row()
        self.cal_on.set(bool(data.get("cal_on")))
        self._layout_params()

    def _delete_profile(self):
        name = self.profile_name.get().strip()
        path = self._profile_path(name)
        if path.exists() and messagebox.askyesno("Delete profile", f"Delete profile '{name}'?"):
            try:
                path.unlink()
            except OSError as e:
                messagebox.showerror("Delete profile", str(e))
            self.profile_name.set("")
            self._refresh_profiles()

    # ---- acquisition ------------------------------------------------------- #
    def on_connect(self):
        if self.acq and self.acq.is_alive():
            self.acq.stop(); self.acq = None
            self.btn_connect.configure(text="Connect")
            return
        self.acq = Acquirer(self.q, lambda: self.interval_s)
        self.acq.start()
        self.btn_connect.configure(text="Disconnect")

    # ---- CSV --------------------------------------------------------------- #
    def on_log(self):
        if self.csv_writer:
            self._stop_log(); return
        cols = self._choose_csv_columns()
        if cols is None:
            return
        path = filedialog.asksaveasfilename(defaultextension=".csv",
                                            filetypes=[("CSV", "*.csv")], initialfile="ohmtrack_log.csv")
        if not path:
            return
        try:
            self.csv_file = open(path, "w", newline="", encoding="utf-8")
        except OSError as e:
            messagebox.showerror("CSV log", str(e)); return
        self.csv_columns = cols
        self.csv_writer = csv.writer(self.csv_file)
        self.csv_writer.writerow([CSV_COLUMNS[c] for c in cols])
        self.csv_file.flush()
        self._keep_awake(True)
        self.btn_log.configure(text="Stop CSV log")

    def _stop_log(self):
        f = self.csv_file
        self.csv_writer = self.csv_file = None
        if f:
            try:
                f.flush(); f.close()
            except Exception:
                pass
        self._keep_awake(False)
        try:
            self.btn_log.configure(text="Start CSV log")
        except tk.TclError:
            pass

    def _choose_csv_columns(self):
        dlg = tk.Toplevel(self)
        dlg.title("Choose CSV columns")
        dlg.configure(bg=self.theme["bg"])
        dlg.transient(self); dlg.grab_set()
        tk.Label(dlg, text="Export these columns:", bg=self.theme["bg"], fg=self.theme["text"],
                 font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=14, pady=(12, 6))
        vars_ = {}
        box = tk.Frame(dlg, bg=self.theme["bg"]); box.pack(fill="x", padx=14)
        for key, label in CSV_COLUMNS.items():
            v = tk.BooleanVar(value=(key in self.csv_columns))
            ttk.Checkbutton(box, text=label, variable=v).pack(anchor="w")
            vars_[key] = v
        result = {"cols": None}
        btns = tk.Frame(dlg, bg=self.theme["bg"]); btns.pack(fill="x", padx=14, pady=12)
        def ok():
            result["cols"] = [k for k in CSV_COLUMNS if vars_[k].get()]; dlg.destroy()
        tk.Button(btns, text="OK", width=10, command=ok, relief="flat").pack(side="right")
        tk.Button(btns, text="Cancel", width=10, command=dlg.destroy, relief="flat").pack(side="right", padx=(0, 6))
        dlg.update_idletasks()
        dlg.geometry(f"+{self.winfo_rootx()+120}+{self.winfo_rooty()+120}")
        self.wait_window(dlg)
        return None if result["cols"] is None else (result["cols"] or list(CSV_DEFAULT))

    def _keep_awake(self, on):
        if sys.platform != "win32":
            return
        try:
            import ctypes
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | (0x00000001 if on else 0))
        except Exception:
            pass

    def _csv_value(self, col, r, temp, t_rel):
        if col == "timestamp":
            t = time.time()
            return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) + f".{int((t % 1) * 1000):03d}"
        if col == "t_rel_s":
            return f"{t_rel:.3f}"
        if col == "value":
            return "OL" if r.overload else f"{r.value:g}"
        if col == "unit":
            return "" if r.overload else r.unit
        if col == "ohms":
            return "" if (r.overload or not math.isfinite(r.ohms)) else f"{r.ohms:.9g}"
        if col == "temperature_C":
            return f"{temp:.4f}" if math.isfinite(temp) else ""
        if col == "current_A":
            return f"{self.current_a:g}" if self.current_a is not None else ""
        if col == "range":
            return self.meter.range_label if self.meter else ""
        if col == "mode":
            return ("Auto" if self.meter.auto else "Manual") if self.meter else ""
        if col == "overload":
            return int(r.overload)
        if col == "raw":
            return r.raw_hex
        return ""

    # ---- temperature ------------------------------------------------------- #
    def _temperature(self, ohms):
        m = self.model_var.get()
        try:
            if m in RTD_MODELS:
                if self.cal_on.get():
                    co = rtd.cvd_coeffs_from_points(self._collect_cal_points())
                    if co:
                        R0, A, B = co
                        return rtd.rtd_temperature(ohms, R0, A, B, self._param("C", rtd.C_IEC))
                return rtd.rtd_temperature(ohms, self._param("R0", 100), self._param("A", rtd.A_IEC),
                                           self._param("B", rtd.B_IEC), self._param("C", rtd.C_IEC))
            if m in NTC_MODELS:
                if self.cal_on.get():
                    co = rtd.steinhart_hart_from_points(self._collect_cal_points())
                    if co:
                        A, B, C = co
                        return rtd.ntc_sh_temperature(ohms, A, B, C)
                return rtd.ntc_beta_temperature(ohms, self._param("R0", 10000),
                                                self._param("T0", 25), self._param("Beta", 3950))
            if m.startswith("Linear"):
                return rtd.linear_temperature(ohms, self._param("R0"), self._param("T0"), self._param("alpha"))
            if m.startswith("Copper"):
                return rtd.copper_winding_rise(ohms, self._param("R1"), self._param("T1"), self._param("k", 235))
        except (ValueError, ZeroDivisionError):
            return float("nan")
        return float("nan")

    # ---- meter line -------------------------------------------------------- #
    def _refresh_meter_line(self):
        parts = []
        if self.last and self.last.ok and not self.last.overload:
            parts.append(f"range {band_label_for_ohms(self.last.ohms)}")
        if self.current_a is not None:
            parts.append(f"test current ≈ {format_amps(self.current_a)}")
        if self.meter:
            parts.append("Auto" if self.meter.auto else "Manual")
        self.meter_var.set("Meter:  " + "   ·   ".join(parts) if parts else "Meter: —")

    # ---- update loop ------------------------------------------------------- #
    def _drain(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "conn":
                    self.conn_var.set(payload)
                    if payload == "Disconnected":
                        self.btn_connect.configure(text="Connect")
                elif kind == "error":
                    self.conn_var.set("Error")
                    self.btn_connect.configure(text="Connect")
                    if self.acq:
                        self.acq.stop(); self.acq = None
                    messagebox.showerror(APP_NAME, payload)
                elif kind == "meter":
                    self.meter = payload
                    self._refresh_meter_line()
                elif kind == "reading":
                    self._on_reading(payload)
        except queue.Empty:
            pass
        self.after(80, self._drain)

    def _on_reading(self, r):
        self.last = r
        if self.show_debug.get():
            self.raw_var.set("raw: " + (r.raw_hex or "—") + (f"   ({r.note})" if r.note and not r.ok else ""))
        if not r.ok:
            return
        if r.overload:
            self.r_card["val"].configure(text="OL")
            self.r_card["sub"].configure(text="over-range")
            self.t_card["val"].configure(text="—")
            self.t_card["sub"].configure(text="")
            self.current_a = None
            self._refresh_meter_line()
            # still log the row so an over-range stretch doesn't leave a time gap
            t_rel = time.time() - self.t_start
            self.history.append((t_rel, float("nan"), float("nan")))
            self._csv_write(r, float("nan"), t_rel)
            return
        self.current_a = current_for_ohms(r.ohms)
        self._refresh_meter_line()
        self.r_card["val"].configure(text=f"{r.value:g} {r.unit}")
        self.r_card["sub"].configure(text=("" if r.unit == "Ω" else f"= {r.ohms:.6g} Ω"))
        if self.show_debug.get():
            self.decoded_var.set(f"WORD_A={r.word_a:04x}  WORD_B={r.word_b:04x}\n"
                                 f"rng={r.range_nibble:x}  unit(dec)={r.decimals_nibble:x}")
        temp = self._temperature(r.ohms)
        if self.cal_on.get():
            self._update_cal_info()
        if math.isfinite(temp):
            self.t_card["val"].configure(text=f"{temp:.2f} °C")
            self.t_card["sub"].configure(text=f"model: {self.model_var.get()}" + ("  (cal)" if self.cal_on.get() else ""))
        else:
            self.t_card["val"].configure(text="—")
            self.t_card["sub"].configure(text="check model parameters")

        t_rel = time.time() - self.t_start
        self.history.append((t_rel, temp if math.isfinite(temp) else float("nan"), r.ohms))
        if not self.paused:
            v = r.ohms if self.source_var.get().startswith("Resistance") else (temp if math.isfinite(temp) else float("nan"))
            self.chart.add(t_rel, v)
            self.chart.redraw()
            self._update_stats()
        self._csv_write(r, temp, t_rel)

    def _csv_write(self, r, temp, t_rel):
        if not self.csv_writer:
            return
        try:
            self.csv_writer.writerow([self._csv_value(c, r, temp, t_rel) for c in self.csv_columns])
            self.csv_file.flush()
        except (OSError, ValueError):
            self._stop_log()

    def _on_close(self):
        if self.acq:
            self.acq.stop()
        self._stop_log()
        self._keep_awake(False)
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
