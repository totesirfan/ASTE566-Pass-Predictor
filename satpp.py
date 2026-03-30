#!/usr/bin/env python3
"""
ASTE 566 — Ground Communications for Satellite Operations
Satellite Pass Predictor

University of Southern California — Viterbi School of Engineering

Usage:  python satpp.py
"""

from __future__ import annotations

import csv
import curses
import json
import os
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone

# Fix curses ESC delay (Unix only — must be set before curses.initscr)
if sys.platform != "win32":
    os.environ.setdefault("ESCDELAY", "25")

def _ensure_deps():
    missing = []
    for mod, pkg in (("requests", "requests"), ("skyfield", "skyfield")):
        try:
            __import__(mod)
        except ImportError:
            missing.append(pkg)
    # Windows needs windows-curses since curses isn't in the stdlib
    if sys.platform == "win32":
        try:
            __import__("curses")
        except ImportError:
            missing.append("windows-curses")
    if missing:
        print(f"Installing dependencies: {', '.join(missing)}...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", *missing, "-q"])

_ensure_deps()

import requests
from skyfield.api import EarthSatellite, load as skyfield_load, wgs84

# ── Paths & Constants ─────────────────────────────────────────

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
GS_PATH = os.path.join(SCRIPT_DIR, "ground_station.json")
IDS_PATH = os.path.join(SCRIPT_DIR, "norad_ids.txt")
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "output")

CELESTRAK_URL = "https://celestrak.org/NORAD/elements/gp.php"
SATNOGS_URL = "https://db.satnogs.org/api/transmitters/"

DEFAULT_GRP = "Group 5"
CSV_HEADER = [
    "Pass #", "Satellite Name", "NORAD ID",
    "AOS (UTC)", "LOS (UTC)", "AOS (Local)", "LOS (Local)",
    "Duration (min)", "MaxEl (deg)", "GRP", "Antenna",
]

# ── Configuration ─────────────────────────────────────────────

def load_ground_station(path: str) -> dict:
    with open(path, "r") as f:
        gs = json.load(f)
    for key in ("latitude", "longitude", "altitude_m", "min_elevation_deg"):
        if key not in gs:
            sys.exit(f"Error: '{key}' missing from ground station config.")
    gs.setdefault("week_offset", 0)
    return gs


def load_norad_ids(path: str) -> list[tuple[str, str | None]]:
    entries = []
    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(None, 1)
            entries.append((parts[0], parts[1] if len(parts) > 1 else None))
    return entries


# ── TLE & Frequency ──────────────────────────────────────────

def fetch_tle(norad_id: str) -> tuple[str, str, str] | None:
    try:
        r = requests.get(CELESTRAK_URL, params={"CATNR": norad_id, "FORMAT": "TLE"}, timeout=15)
        r.raise_for_status()
        lines = r.text.strip().splitlines()
        if len(lines) < 3:
            return None
        return lines[0].strip(), lines[1].strip(), lines[2].strip()
    except requests.RequestException:
        return None


def fetch_frequency_info(norad_id: str) -> tuple[str, str]:
    try:
        r = requests.get(SATNOGS_URL, params={"satellite__norad_cat_id": norad_id, "format": "json"}, timeout=15)
        r.raise_for_status()
        active = [t for t in r.json() if t.get("alive") and t.get("downlink_low") and t.get("status") == "active"]
        if not active:
            return "N/A", "N/A"
        uhf = [t for t in active if t["downlink_low"] < 1e9]
        chosen = uhf[0] if uhf else active[0]
        freq_hz = chosen["downlink_low"]
        return f"{freq_hz / 1e6:.3f} MHz", "UHF" if freq_hz < 1e9 else "S-Band"
    except (requests.RequestException, KeyError, ValueError):
        return "N/A", "N/A"


# ── Time ──────────────────────────────────────────────────────

def get_week_boundaries(week_offset: int = 0) -> tuple[datetime, datetime]:
    now = datetime.now().astimezone()
    monday = now - timedelta(days=now.weekday()) + timedelta(weeks=week_offset)
    start = monday.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=7) - timedelta(seconds=1)
    return start, end


# ── Pass Prediction ───────────────────────────────────────────

def find_passes(satellite, gs_position, ts, t_start, t_end, min_el_deg):
    diff = satellite - gs_position
    step = 60
    total_sec = int((t_end.utc_datetime() - t_start.utc_datetime()).total_seconds())
    steps = total_sec // step + 1

    passes = []
    in_pass = False
    aos_time = None
    max_el = 0.0

    for i in range(steps + 1):
        base = t_start.utc_datetime()
        t = ts.utc(base.year, base.month, base.day, base.hour, base.minute, base.second + i * step)
        el = diff.at(t).altaz()[0].degrees

        if not in_pass and el >= min_el_deg:
            aos_time = _refine(diff, ts, t_start, (i - 1) * step, i * step, min_el_deg, True) if i > 0 else t
            in_pass = True
            max_el = el
        elif in_pass and el >= min_el_deg:
            max_el = max(max_el, el)
        elif in_pass and el < min_el_deg:
            los_time = _refine(diff, ts, t_start, (i - 1) * step, i * step, min_el_deg, False)
            passes.append({
                "aos_utc": aos_time.utc_datetime().replace(tzinfo=timezone.utc),
                "los_utc": los_time.utc_datetime().replace(tzinfo=timezone.utc),
                "max_el_deg": round(max_el, 1),
            })
            in_pass = False
            aos_time = None
            max_el = 0.0

    if in_pass and aos_time is not None:
        passes.append({
            "aos_utc": aos_time.utc_datetime().replace(tzinfo=timezone.utc),
            "los_utc": t_end.utc_datetime().replace(tzinfo=timezone.utc),
            "max_el_deg": round(max_el, 1),
        })
    return passes


def _refine(diff, ts, t_start, lo, hi, threshold, rising):
    base = t_start.utc_datetime()
    for _ in range(10):
        mid = (lo + hi) / 2.0
        t = ts.utc(base.year, base.month, base.day, base.hour, base.minute, base.second + mid)
        above = diff.at(t).altaz()[0].degrees >= threshold
        if rising == above:
            hi = mid
        else:
            lo = mid
    final = (lo + hi) / 2.0
    return ts.utc(base.year, base.month, base.day, base.hour, base.minute, base.second + final)


# ── Formatting ────────────────────────────────────────────────

def format_duration(td: timedelta) -> str:
    s = int(td.total_seconds())
    return f"{s // 60}.{s % 60:02d}"


def pass_to_csv_row(p: dict, idx: int) -> list:
    aos_l = p["aos_utc"].astimezone()
    los_l = p["los_utc"].astimezone()
    dur = p["los_utc"] - p["aos_utc"]
    return [
        idx, p["sat_name"], p["norad_id"],
        p["aos_utc"].strftime("%Y-%m-%d %H:%M:%S"),
        p["los_utc"].strftime("%Y-%m-%d %H:%M:%S"),
        aos_l.strftime("%Y-%m-%d %H:%M:%S"),
        los_l.strftime("%Y-%m-%d %H:%M:%S"),
        format_duration(dur), round(p["max_el_deg"], 1),
        DEFAULT_GRP, p.get("antenna", "N/A"),
    ]


TABLE_COLS = [
    ("#", 4), ("Satellite Name", 18), ("NORAD ID", 9),
    ("AOS (UTC)", 14), ("LOS (UTC)", 14),
    ("AOS (Local)", 14), ("LOS (Local)", 14),
    ("Duration", 9), ("Max Elevation", 14), ("Antenna", 8),
]


def pass_to_display_row(p: dict, idx: int) -> list[str]:
    aos, los = p["aos_utc"], p["los_utc"]
    aos_l, los_l = aos.astimezone(), los.astimezone()
    dur = los - aos
    return [
        str(idx), p["sat_name"][:18], p["norad_id"],
        aos.strftime("%m-%d %H:%M:%S"), los.strftime("%m-%d %H:%M:%S"),
        aos_l.strftime("%m-%d %H:%M:%S"), los_l.strftime("%m-%d %H:%M:%S"),
        format_duration(dur) + " min", f"{p['max_el_deg']:.1f}°",
        p.get("antenna", "N/A"),
    ]


def format_table_row(cols: list[str], widths: list[int], sep: str = "  ") -> str:
    return sep.join(str(v)[:w].ljust(w) for v, w in zip(cols, widths))


# ── Colors ────────────────────────────────────────────────────

C_BRAND = 1
C_GOLD = 2
C_TEXT = 3
C_DIM = 4
C_ROW_A = 5
C_ROW_B = 6
C_SELECT = 7
C_BORDER = 8
C_INPUT = 9
C_SECTION = 10


def init_colors():
    curses.start_color()
    curses.use_default_colors()
    if curses.COLORS >= 256:
        CARDINAL, GOLD, LIGHT, FAINT, MID = 124, 220, 249, 242, 239
        curses.init_pair(C_BRAND, GOLD, CARDINAL)
        curses.init_pair(C_GOLD, GOLD, -1)
        curses.init_pair(C_TEXT, LIGHT, -1)
        curses.init_pair(C_DIM, FAINT, -1)
        curses.init_pair(C_ROW_A, LIGHT, -1)
        curses.init_pair(C_ROW_B, LIGHT, -1)
        curses.init_pair(C_SELECT, GOLD, -1)
        curses.init_pair(C_BORDER, MID, -1)
        curses.init_pair(C_INPUT, GOLD, -1)
        curses.init_pair(C_SECTION, GOLD, -1)
    else:
        curses.init_pair(C_BRAND, curses.COLOR_YELLOW, curses.COLOR_RED)
        for c in (C_GOLD, C_SELECT, C_INPUT, C_SECTION):
            curses.init_pair(c, curses.COLOR_YELLOW, -1)
        for c in (C_TEXT, C_DIM, C_ROW_A, C_ROW_B, C_BORDER):
            curses.init_pair(c, curses.COLOR_WHITE, -1)


# ── TUI Application ──────────────────────────────────────────

class App:
    def __init__(self):
        self.gs = load_ground_station(GS_PATH)
        self.sats = load_norad_ids(IDS_PATH)
        self.passes: list[dict] = []
        self.rows: list[list[str]] = []
        self.status = "Ready — press R to run or type a command"
        self.scroll = 0
        self.sel = 0
        self.panel: str | None = None
        self.psel = 0
        self.editing: int | None = None
        self.inp = ""
        self.running = False
        self.progress = (0, 0)
        self.cmd = ""

    def run(self, stdscr):
        self.scr = stdscr
        curses.curs_set(0)
        init_colors()
        stdscr.timeout(50)
        while True:
            self.draw()
            try:
                k = stdscr.getch()
            except curses.error:
                continue
            if k == -1:
                continue
            if self.handle(k):
                break

    # ── Input ─────────────────────────────────────────────────

    def handle(self, k: int) -> bool:
        if self.panel and self.editing is not None:
            return self._edit_key(k)
        if self.panel:
            return self._panel_key(k)

        if k == 27:
            self.cmd = ""
            return False
        if k in (10, 13, curses.KEY_ENTER):
            return self._run_cmd()
        if k in (curses.KEY_BACKSPACE, 127, 8):
            self.cmd = self.cmd[:-1]
            return False

        if not self.cmd:
            if k in (ord("q"), ord("Q")):
                return True
            if k in (ord("r"), ord("R")) and not self.running:
                self._predict_start()
                return False
            if k in (ord("e"), ord("E")):
                name = self._export()
                if name:
                    self.status = f"Exported → {name}"
                else:
                    self.status = "No data — run predictions first."
                return False
            if k == curses.KEY_UP and self.sel > 0:
                self.sel -= 1
                return False
            if k == curses.KEY_DOWN and self.sel < len(self.rows) - 1:
                self.sel += 1
                return False
            if k == curses.KEY_PPAGE:
                self.sel = max(0, self.sel - 10)
                return False
            if k == curses.KEY_NPAGE:
                self.sel = min(max(0, len(self.rows) - 1), self.sel + 10)
                return False

        if 32 <= k < 127:
            self.cmd += chr(k)
        return False

    def _run_cmd(self) -> bool:
        c = self.cmd.strip().lower()
        self.cmd = ""
        if not c:
            return False
        if c in ("cfg", "config", "gs"):
            self.panel, self.psel, self.editing = "cfg", 0, None
        elif c in ("sats", "satellites", "sat"):
            self.panel, self.psel, self.editing = "sats", 0, None
        elif c in ("help", "h", "?"):
            self.panel = "help"
        elif c in ("q", "quit", "exit"):
            return True
        elif c in ("run", "r") and not self.running:
            self._predict_start()
        elif c in ("export", "e"):
            name = self._export()
            self.status = f"Exported → {name}" if name else "No data — run predictions first."
        else:
            self.status = f'Unknown: "{c}" — try help, cfg, sats, run, export'
        return False

    def _panel_key(self, k: int) -> bool:
        if k == 27 or k == ord("q"):
            self.panel = None
        elif k == curses.KEY_UP and self.psel > 0:
            self.psel -= 1
        elif k == curses.KEY_DOWN:
            self.psel += 1
        elif k in (10, 13, curses.KEY_ENTER):
            if self.panel == "cfg":
                self._cfg_action()
            elif self.panel == "sats":
                self._sat_action()
        elif k in (curses.KEY_BACKSPACE, 127, 8, curses.KEY_DC):
            if self.panel == "sats" and 0 <= self.psel < len(self.sats):
                removed = self.sats.pop(self.psel)
                self.status = f"Removed {removed[1] or removed[0]}."
                if self.psel >= len(self.sats) and self.psel > 0:
                    self.psel -= 1
        return False

    def _edit_key(self, k: int) -> bool:
        if k == 27:
            self.editing, self.inp = None, ""
        elif k in (10, 13, curses.KEY_ENTER):
            self._commit_edit()
        elif k in (curses.KEY_BACKSPACE, 127, 8):
            self.inp = self.inp[:-1]
        elif 32 <= k < 127:
            self.inp += chr(k)
        return False

    # ── Config ────────────────────────────────────────────────

    CFG_KEYS = ["name", "latitude", "longitude", "altitude_m", "min_elevation_deg", "week_offset"]
    CFG_LABELS = ["Station Name", "Latitude (Degrees North)", "Longitude (Degrees East)",
                  "Altitude (Meters ASL)", "Minimum Elevation (Degrees)", "Week Offset (0=this week, 1=next week)"]

    def _cfg_action(self):
        if self.psel < len(self.CFG_KEYS):
            self.editing = self.psel
            self.inp = str(self.gs[self.CFG_KEYS[self.psel]])
        elif self.psel == len(self.CFG_KEYS):
            with open(GS_PATH, "w") as f:
                json.dump(self.gs, f, indent=2)
                f.write("\n")
            self.status = "Ground station configuration saved."

    def _commit_edit(self):
        if self.panel == "sats" and self.editing == -1:
            self._commit_add_sat()
            return
        if self.editing is None:
            return
        key = self.CFG_KEYS[self.editing]
        val = self.inp.strip()
        if key == "name":
            self.gs[key] = val
        elif key == "week_offset":
            try:
                v = int(val)
                if v < 0:
                    raise ValueError
                self.gs[key] = v
            except ValueError:
                self.status = "Week offset must be a non-negative integer."
        else:
            try:
                self.gs[key] = float(val)
            except ValueError:
                self.status = f"Invalid number for {key}"
        self.editing, self.inp = None, ""

    # ── Satellites ────────────────────────────────────────────

    def _sat_action(self):
        if self.editing == -1:
            self._commit_add_sat()
        elif self.psel == len(self.sats):
            self.editing, self.inp = -1, ""
        elif self.psel == len(self.sats) + 1:
            with open(IDS_PATH, "w") as f:
                f.write("# NORAD ID list — one per line\n# Format: NORAD_ID [optional name]\n\n")
                for nid, name in self.sats:
                    f.write(f"{nid} {name}\n" if name else f"{nid}\n")
            self.status = f"Saved {len(self.sats)} satellites."

    def _commit_add_sat(self):
        val = self.inp.strip()
        self.editing, self.inp = None, ""
        if not val:
            return
        parts = val.split(None, 1)
        nid = parts[0]
        name = parts[1] if len(parts) > 1 else None
        if not nid.isdigit():
            self.status = "NORAD ID must be a number."
            return
        if nid in {n for n, _ in self.sats}:
            self.status = f"NORAD ID {nid} already tracked."
            return
        self.sats.append((nid, name))
        self.status = f"Added {name or nid} to tracking list."

    # ── Predictions ───────────────────────────────────────────

    def _predict_start(self):
        self.running = True
        self.progress = (0, len(self.sats))
        self.status = "Initializing..."
        threading.Thread(target=self._predict, daemon=True).start()

    def _predict(self):
        try:
            ts = skyfield_load.timescale()
            ws, we = get_week_boundaries(int(self.gs.get("week_offset", 0)))
            t0 = ts.from_datetime(ws.astimezone(timezone.utc))
            t1 = ts.from_datetime(we.astimezone(timezone.utc))
            gs_pos = wgs84.latlon(self.gs["latitude"], self.gs["longitude"],
                                  elevation_m=self.gs["altitude_m"])
            min_el = self.gs["min_elevation_deg"]
            result = []
            total = len(self.sats)

            for i, (nid, uname) in enumerate(self.sats):
                self.progress = (i, total)
                label = uname or f"NORAD ID {nid}"

                self.status = f"[{i+1}/{total}] {label} — Fetching TLE from CelesTrak..."
                tle = fetch_tle(nid)
                if tle is None:
                    continue
                sat_name = uname or tle[0].strip()

                self.status = f"[{i+1}/{total}] {sat_name} — Querying SatNOGS for frequency..."
                freq, ant = fetch_frequency_info(nid)

                self.status = f"[{i+1}/{total}] {sat_name} — Computing pass windows (SGP4)..."
                sat = EarthSatellite(tle[1], tle[2], sat_name, ts)
                for p in find_passes(sat, gs_pos, ts, t0, t1, min_el):
                    p.update(sat_name=sat_name, norad_id=nid, frequency=freq, antenna=ant)
                    result.append(p)

            self.progress = (total, total)
            result.sort(key=lambda p: p["aos_utc"])
            self.passes = result
            self.rows = [pass_to_display_row(p, i+1) for i, p in enumerate(result)]
            self.sel = self.scroll = 0

            # Auto-export CSV
            csv_name = self._export()

            station = self.gs.get("name", "N/A")
            week = f"{ws.strftime('%b %d')} — {we.strftime('%b %d, %Y')}"
            exported = f"  ·  Exported → {csv_name}" if csv_name else ""
            self.status = f"Done — {len(result)} passes across {total} satellites  ·  {station}  ·  {week}{exported}"
        except Exception as e:
            self.status = f"Error: {e}"
        finally:
            self.running = False
            self.progress = (0, 0)

    def _export(self) -> str | None:
        if not self.passes:
            return None
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        ws, we = get_week_boundaries(int(self.gs.get("week_offset", 0)))
        name = f"passes_{ws.strftime('%Y-%m-%d')}_to_{we.strftime('%Y-%m-%d')}.csv"
        path = os.path.join(OUTPUT_DIR, name)
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(CSV_HEADER)
            for i, p in enumerate(self.passes, 1):
                w.writerow(pass_to_csv_row(p, i))
        return name

    # ── Drawing ───────────────────────────────────────────────

    def _put(self, y, x, text, w, attr=0):
        h, sw = self.scr.getmaxyx()
        if y < 0 or y >= h or x >= sw:
            return
        text = text[:w]
        if y == h - 1:
            text = text[:max(0, sw - x - 1)]
        try:
            self.scr.addnstr(y, x, text, len(text), attr)
        except curses.error:
            pass

    def draw(self):
        scr = self.scr
        scr.erase()
        h, w = scr.getmaxyx()
        if h < 10 or w < 50:
            scr.addstr(0, 0, "Terminal too small")
            scr.refresh()
            return

        BRAND = curses.color_pair(C_BRAND) | curses.A_BOLD
        GOLD = curses.color_pair(C_GOLD) | curses.A_BOLD
        TEXT = curses.color_pair(C_TEXT)
        DIM = curses.color_pair(C_DIM)
        BOR = curses.color_pair(C_BORDER)

        # Brand bar
        self._put(0, 0, " USC ASTE 566 — SATELLITE PASS PREDICTOR ".center(w), w, BRAND)

        # Separator
        self._put(1, 0, "─" * w, w, BOR)

        # Status bar
        self._put(2, 0, f" {self.status}".ljust(w), w, TEXT)

        # Progress bar (extra row when running)
        if self.running and self.progress[1] > 0:
            frac = self.progress[0] / self.progress[1]
            filled = int(frac * w)
            pct = f" {int(frac * 100)}% ".ljust(w)
            self._put(3, 0, pct[:filled], filled, curses.color_pair(C_BRAND))
            self._put(3, filled, pct[filled:], w - filled, GOLD)
            self._put(4, 0, "─" * w, w, BOR)
            top = 5
        else:
            self._put(3, 0, "─" * w, w, BOR)
            top = 4

        # Bottom: separator + command
        self._put(h - 2, 0, "─" * w, w, BOR)
        if self.cmd:
            self._put(h - 1, 0, f"  ❯ {self.cmd}█".ljust(w), w, GOLD)
        else:
            self._put(h - 1, 0, "  R run  ·  E export  ·  Q quit  ·  type: help  cfg  sats".ljust(w), w, DIM)

        # Body
        body_h = h - 2 - top
        pw = min(46, w * 2 // 5) if self.panel else 0
        tw = w - pw

        self._draw_table(top, 0, tw, body_h)
        if self.panel and pw > 0:
            self._draw_panel(top, tw, pw, body_h)

        scr.refresh()

    def _draw_table(self, top, left, width, height):
        if height < 3 or width < 30:
            return
        widths = [cw for _, cw in TABLE_COLS]
        headers = [n for n, _ in TABLE_COLS]
        HDR = curses.color_pair(C_SECTION) | curses.A_BOLD

        self._put(top, left, " " + format_table_row(headers, widths), width, HDR)
        self._put(top + 1, left, "─" * width, width, curses.color_pair(C_BORDER))

        vis = height - 2
        if self.sel < self.scroll:
            self.scroll = self.sel
        elif self.sel >= self.scroll + vis:
            self.scroll = self.sel - vis + 1

        if not self.rows:
            self._put(top + 3, left + 2, "No pass data — press R to run predictions", width - 2, curses.color_pair(C_DIM))
            return

        for i in range(vis):
            ri = self.scroll + i
            if ri >= len(self.rows):
                break
            line = " " + format_table_row(self.rows[ri], widths)
            if ri == self.sel:
                attr = curses.color_pair(C_SELECT) | curses.A_BOLD
            elif ri % 2:
                attr = curses.color_pair(C_ROW_B)
            else:
                attr = curses.color_pair(C_ROW_A)
            self._put(top + 2 + i, left, line.ljust(width)[:width], width, attr)

    def _draw_panel(self, top, left, width, height):
        BOR = curses.color_pair(C_BORDER)
        for y in range(top, top + height):
            self._put(y, left, "│", 1, BOR)
        x, pw = left + 2, width - 3
        {"help": self._p_help, "cfg": self._p_cfg, "sats": self._p_sats}[self.panel](top, x, pw, height)

    def _p_help(self, top, x, w, h):
        SEC = curses.color_pair(C_SECTION) | curses.A_BOLD
        TXT = curses.color_pair(C_TEXT)
        DIM = curses.color_pair(C_DIM)
        lines = [
            (SEC, "COMMANDS"),   (0, ""),
            (TXT, "  help       Show this panel"),
            (TXT, "  cfg        Ground station config"),
            (TXT, "  sats       Satellite tracking list"),
            (TXT, "  run        Run pass predictions"),
            (TXT, "  export     Export results to CSV"),
            (TXT, "  quit       Exit"), (0, ""),
            (SEC, "SHORTCUTS"), (0, ""),
            (TXT, "  R          Run predictions"),
            (TXT, "  E          Export CSV"),
            (TXT, "  ↑ ↓        Navigate table"),
            (TXT, "  PgUp/Dn    Scroll 10 rows"),
            (TXT, "  Q          Quit"), (0, ""),
            (SEC, "PANELS"), (0, ""),
            (TXT, "  ↑ ↓        Select field / item"),
            (TXT, "  Enter      Edit / confirm"),
            (TXT, "  Delete     Remove satellite"),
            (TXT, "  ESC / Q    Close panel"),
        ]
        for i, (a, t) in enumerate(lines):
            if i >= h:
                break
            self._put(top + i, x, t.ljust(w), w, a or DIM)

    def _p_cfg(self, top, x, w, h):
        SEC = curses.color_pair(C_SECTION) | curses.A_BOLD
        TXT = curses.color_pair(C_TEXT)
        DIM = curses.color_pair(C_DIM)
        SEL = curses.color_pair(C_SELECT) | curses.A_BOLD
        INP = curses.color_pair(C_INPUT) | curses.A_BOLD

        y = top
        self._put(y, x, " GROUND STATION ".ljust(w), w, SEC); y += 1
        self._put(y, x, "─" * w, w, curses.color_pair(C_BORDER)); y += 1

        for i, (key, label) in enumerate(zip(self.CFG_KEYS, self.CFG_LABELS)):
            if y + 2 >= top + h:
                break
            self._put(y, x, f"  {label}", w, DIM); y += 1
            val = str(self.gs[key])
            if self.editing == i:
                self._put(y, x, f"  ▸ {self.inp}█".ljust(w), w, INP)
            elif i == self.psel:
                self._put(y, x, f"  ▸ {val}".ljust(w), w, SEL)
            else:
                self._put(y, x, f"    {val}".ljust(w), w, TXT)
            y += 1

        y += 1
        if y < top + h:
            s = self.psel == len(self.CFG_KEYS)
            m = "▸" if s else " "
            self._put(y, x, f"  {m} [ Save Configuration ]".ljust(w), w, SEL if s else DIM)
        if self.psel > len(self.CFG_KEYS):
            self.psel = len(self.CFG_KEYS)

    def _p_sats(self, top, x, w, h):
        SEC = curses.color_pair(C_SECTION) | curses.A_BOLD
        TXT = curses.color_pair(C_TEXT)
        DIM = curses.color_pair(C_DIM)
        SEL = curses.color_pair(C_SELECT) | curses.A_BOLD
        INP = curses.color_pair(C_INPUT) | curses.A_BOLD

        n = len(self.sats)
        y = top
        self._put(y, x, f" SATELLITES ({n}) ".ljust(w), w, SEC); y += 1
        self._put(y, x, "─" * w, w, curses.color_pair(C_BORDER)); y += 1

        for i, (nid, name) in enumerate(self.sats):
            if y >= top + h - 5:
                break
            s = i == self.psel
            m = "▸" if s else " "
            self._put(y, x, f"  {m} {nid:>6}  {name or '—'}"[:w].ljust(w), w, SEL if s else TXT)
            y += 1

        y += 1
        if y < top + h:
            if self.editing == -1:
                self._put(y, x, f"  ▸ NORAD ID [name]: {self.inp}█".ljust(w)[:w], w, INP)
            elif self.psel == n:
                self._put(y, x, "  ▸ [ Add New Satellite ]".ljust(w), w, SEL)
            else:
                self._put(y, x, "    [ Add New Satellite ]".ljust(w), w, DIM)

        y += 1
        if y < top + h:
            s = self.psel == n + 1
            m = "▸" if s else " "
            self._put(y, x, f"  {m} [ Save List ]".ljust(w), w, SEL if s else DIM)

        y += 2
        if y < top + h:
            self._put(y, x, "  Delete key removes selected", w, DIM)
        if self.psel > n + 1:
            self.psel = n + 1


# ── Entry point ───────────────────────────────────────────────

if __name__ == "__main__":
    curses.wrapper(lambda stdscr: App().run(stdscr))
