#!/usr/bin/env python3
"""
ASTE 566 — Ground Communications for Satellite Operations
Satellite Pass Predictor

University of Southern California — Viterbi School of Engineering
Author: Irfan Annuar

Usage:  python satpp.py
"""

from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone


def _ensure_deps():
    missing = []
    for mod, pkg in (
        ("requests", "requests"),
        ("skyfield", "skyfield"),
        ("textual", "textual"),
        ("rich", "rich"),
    ):
        try:
            __import__(mod)
        except ImportError:
            missing.append(pkg)
    if missing:
        print(f"Installing dependencies: {', '.join(missing)}...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", *missing, "-q"])


_ensure_deps()

import numpy as np
import requests
from rich.text import Text
from skyfield.api import EarthSatellite, load as skyfield_load, wgs84

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import (
    DataTable,
    OptionList,
    Static,
)
from textual.widgets.option_list import Option

# ── Paths & Constants ─────────────────────────────────────────

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
GS_PATH = os.path.join(SCRIPT_DIR, "ground_station.json")
SECRETS_PATH = os.path.join(SCRIPT_DIR, ".secrets.json")
IDS_PATH = os.path.join(SCRIPT_DIR, "norad_ids.txt")
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "output")

CELESTRAK_URL = "https://celestrak.org/NORAD/elements/gp.php"
SPACETRACK_LOGIN_URL = "https://www.space-track.org/ajaxauth/login"
SPACETRACK_TLE_URL = "https://www.space-track.org/basicspacedata/query/class/gp/NORAD_CAT_ID/{ids}/orderby/NORAD_CAT_ID/format/3le"
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
    gs.setdefault("tle_source", "celestrak")
    return gs


def save_ground_station(path: str, gs: dict):
    with open(path, "w") as f:
        json.dump(gs, f, indent=2)
        f.write("\n")


def load_secrets(path: str) -> dict:
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return {}


def save_secrets(path: str, secrets: dict):
    with open(path, "w") as f:
        json.dump(secrets, f, indent=2)
        f.write("\n")


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


def save_norad_ids(path: str, sats: list[tuple[str, str | None]]):
    with open(path, "w") as f:
        f.write("# NORAD ID list — one per line\n# Format: NORAD_ID [optional name]\n\n")
        for nid, name in sats:
            f.write(f"{nid} {name}\n" if name else f"{nid}\n")


# ── TLE & Frequency ──────────────────────────────────────────


def fetch_tle(norad_id: str) -> tuple[str, str, str] | None:
    try:
        r = requests.get(CELESTRAK_URL, params={"CATNR": norad_id, "FORMAT": "TLE"}, timeout=15)
        if r.status_code == 429:
            raise RuntimeError(f"CelesTrak rate limit hit for NORAD ID {norad_id}")
        r.raise_for_status()
        lines = r.text.strip().splitlines()
        if len(lines) < 3:
            raise RuntimeError(f"No TLE found for NORAD ID {norad_id}")
        return lines[0].strip(), lines[1].strip(), lines[2].strip()
    except requests.RequestException as e:
        raise RuntimeError(f"Network error fetching TLE for {norad_id}: {e}")
    except RuntimeError:
        raise


def fetch_tles_spacetrack(
    norad_ids: list[str], username: str, password: str
) -> dict[str, tuple[str, str, str]]:
    result = {}
    try:
        session = requests.Session()
        resp = session.post(
            SPACETRACK_LOGIN_URL,
            data={"identity": username, "password": password},
            timeout=15,
        )
        resp.raise_for_status()
        if "Login Failed" in resp.text:
            return {}
        ids_str = ",".join(norad_ids)
        url = SPACETRACK_TLE_URL.format(ids=ids_str)
        resp = session.get(url, timeout=30)
        resp.raise_for_status()
        lines = [l.strip() for l in resp.text.strip().splitlines() if l.strip()]
        i = 0
        while i + 2 < len(lines):
            name, line1, line2 = lines[i], lines[i + 1], lines[i + 2]
            if line1.startswith("1 ") and line2.startswith("2 "):
                nid = line1[2:7].strip()
                result[nid] = (name, line1, line2)
                i += 3
            else:
                i += 1
        session.close()
    except requests.RequestException:
        pass
    return result


def fetch_frequency_info(norad_id: str) -> tuple[str, str]:
    try:
        r = requests.get(
            SATNOGS_URL,
            params={"satellite__norad_cat_id": norad_id, "format": "json"},
            timeout=15,
        )
        r.raise_for_status()
        active = [
            t for t in r.json()
            if t.get("alive") and t.get("downlink_low") and t.get("status") == "active"
        ]
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
    n_steps = total_sec // step + 1

    base = t_start.utc_datetime()
    offsets = np.arange(n_steps + 1) * step
    times = ts.utc(base.year, base.month, base.day, base.hour, base.minute, base.second + offsets)
    els = diff.at(times).altaz()[0].degrees

    above = els >= min_el_deg
    passes = []
    in_pass = False
    aos_idx = 0
    max_el = 0.0

    for i in range(len(above)):
        if not in_pass and above[i]:
            aos_idx = i
            in_pass = True
            max_el = els[i]
        elif in_pass and above[i]:
            max_el = max(max_el, els[i])
        elif in_pass and not above[i]:
            aos_time = (
                _refine(diff, ts, t_start, offsets[aos_idx - 1], offsets[aos_idx], min_el_deg, True)
                if aos_idx > 0 else times[0]
            )
            los_time = _refine(diff, ts, t_start, offsets[i - 1], offsets[i], min_el_deg, False)
            passes.append({
                "aos_utc": aos_time.utc_datetime().replace(tzinfo=timezone.utc),
                "los_utc": los_time.utc_datetime().replace(tzinfo=timezone.utc),
                "max_el_deg": round(max_el, 1),
            })
            in_pass = False
            max_el = 0.0

    if in_pass:
        aos_time = (
            _refine(diff, ts, t_start, offsets[aos_idx - 1], offsets[aos_idx], min_el_deg, True)
            if aos_idx > 0 else times[0]
        )
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


def export_csv(passes: list[dict], week_offset: int) -> str | None:
    if not passes:
        return None
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    ws, we = get_week_boundaries(week_offset)
    name = f"passes_{ws.strftime('%Y-%m-%d')}_to_{we.strftime('%Y-%m-%d')}.csv"
    path = os.path.join(OUTPUT_DIR, name)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(CSV_HEADER)
        for i, p in enumerate(passes, 1):
            w.writerow(pass_to_csv_row(p, i))
    return name


# ── Textual TUI ──────────────────────────────────────────────

class SplashModal(ModalScreen):
    DEFAULT_CSS = """
    SplashModal {
        align: center middle;
        background: rgba(0, 0, 0, 0.6);
    }
    #splash-box {
        width: 50;
        height: auto;
        min-height: 5;
        padding: 1 2;
        border: solid #FFCC00;
        background: #1a1a1a;
        content-align: center middle;
    }
    #splash-text {
        width: 100%;
        text-align: center;
        color: #FFCC00;
        text-style: bold;
    }
    """

    def __init__(self, message: str, timeout: float = 2.0):
        super().__init__()
        self._message = message
        self.timeout = timeout

    def compose(self) -> ComposeResult:
        with Vertical(id="splash-box"):
            yield Static(self._message, id="splash-text")

    def on_mount(self) -> None:
        self.set_timer(self.timeout, self._close)

    def _close(self) -> None:
        if self.is_current:
            self.app.pop_screen()

    def on_key(self, event) -> None:
        if event.key in ("escape", "enter"):
            self._close()
            event.stop()


TCSS = """
* {
    scrollbar-background: $surface-darken-2;
    scrollbar-color: #990000;
    scrollbar-color-hover: #FFCC00;
    scrollbar-color-active: #FFCC00;
    link-color: #FFCC00;
    link-background-hover: #990000;
}

Screen {
    background: $surface;
    layout: vertical;
}

#brand-pad-top, #brand-pad-bot {
    width: 100%;
    height: 1;
    max-height: 1;
    background: #990000;
}

#brand {
    width: 100%;
    height: 1;
    background: #990000;
    color: #FFCC00;
    text-align: center;
    text-style: bold;
    content-align: center middle;
}

#info-bar {
    width: 100%;
    height: 1;
    background: $surface-darken-1;
    color: $text-muted;
    padding: 0 1;
}

#status-bar {
    width: 100%;
    height: 1;
    padding: 0 1;
    color: $text;
}

#progress-bar {
    width: 100%;
    height: 1;
    display: none;
    color: #FFCC00;
}

#progress-bar.visible {
    display: block;
}

#main-area {
    width: 100%;
    height: 1fr;
}

#pass-table {
    width: 1fr;
    height: 100%;
}

DataTable {
    height: 100%;
}

DataTable > .datatable--header {
    background: $surface-darken-1;
    color: #FFCC00;
    text-style: bold;
}

DataTable > .datatable--cursor {
    background: #FFCC00;
    color: #1a1a1a;
    text-style: bold;
}

#sidebar {
    width: 50;
    height: 100%;
    display: none;
    border-left: solid #990000;
    background: $surface-darken-1;
}

#sidebar.visible {
    display: block;
}

#panel-options {
    height: 1fr;
    background: $surface-darken-1;
}

OptionList {
    background: $surface-darken-1;
}

OptionList > .option-list--option-highlighted {
    background: #FFCC00 20%;
    color: #FFCC00;
    text-style: bold;
}

OptionList > .option-list--option {
    padding: 0 1;
}

#controls-bar {
    width: 100%;
    height: 1;
    background: $surface-darken-2;
    color: $text-muted;
    padding: 0 0;
}

DataTable:focus {
    border: none;
}

OptionList:focus {
    border: none;
}
"""


class SatPassPredictor(App):
    CSS = TCSS
    TITLE = "ASTE 566 — Satellite Pass Predictor"
    COMMANDS = set()
    DESIGN = {"dark": {"accent": "#FFCC00", "primary": "#990000"}}

    BINDINGS = [
        Binding("r", "run_predictions", "Run", show=True, priority=True),
        Binding("e", "export", "Export CSV", show=True, priority=True),
        Binding("left", "week_prev", priority=True, show=False),
        Binding("right", "week_next", priority=True, show=False),
        Binding("1", "panel('cfg')", "Config", show=True, priority=True),
        Binding("2", "panel('sats')", "Sats", show=True, priority=True),
        Binding("3", "panel('help')", "Help", show=True, priority=True),
        Binding("escape", "escape_action", "Close/Cancel", show=True, priority=True),
        Binding("q", "quit", "Quit", show=True, priority=True),
    ]

    status_text: reactive[str] = reactive("Ready — press R to run predictions")
    sidebar_mode: reactive[str] = reactive("")

    def __init__(self):
        super().__init__()
        self.gs = load_ground_station(GS_PATH)
        self.secrets = load_secrets(SECRETS_PATH)
        self.secrets.setdefault("spacetrack_user", "")
        self.secrets.setdefault("spacetrack_pass", "")
        self.sats = load_norad_ids(IDS_PATH)
        self.passes: list[dict] = []
        self.last_run: datetime | None = None
        self.last_csv: str | None = None
        self._predicting = False
        self._predict_t0: float = 0.0
        self._editing_key: str | None = None
        self._editing_option_idx: int = -1
        self._editing_buf: str = ""
        self._editing_original: str = ""
        self._editing_label: str = ""
        self._editing_sat_idx: int = -1

    def compose(self) -> ComposeResult:
        yield Static(" ", id="brand-pad-top")
        yield Static(" USC ASTE 566 — SATELLITE PASS PREDICTOR — Irfan Annuar ", id="brand")
        yield Static(" ", id="brand-pad-bot")
        yield Static(self._info_text(), id="info-bar")
        yield Static(self.status_text, id="status-bar")
        yield Static("", id="progress-bar")
        with Horizontal(id="main-area"):
            with Vertical(id="pass-table"):
                yield DataTable(id="table")
            with Vertical(id="sidebar"):
                yield OptionList(id="panel-options")
        yield Static(
            " Controls:  R Run  |  E Export  |  1 Config  |  2 Sats  |  3 Help  |  Esc Close  |  Q Quit",
            id="controls-bar",
        )

    def on_mount(self) -> None:
        table = self.query_one("#table", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_columns(
            "#", "Satellite", "NORAD ID",
            "AOS (UTC)", "LOS (UTC)",
            "AOS (Local)", "LOS (Local)",
            "Duration", "Max El", "Antenna",
        )
        self.query_one("#progress-bar", Static).can_focus = False
        table.focus()
        self._refresh_info()

    # ── Reactive watchers ────────────────────────────────────

    def watch_status_text(self, value: str) -> None:
        try:
            self.query_one("#status-bar", Static).update(f" {value}")
        except Exception:
            pass

    def watch_sidebar_mode(self, value: str) -> None:
        sidebar = self.query_one("#sidebar")
        sidebar.set_class(bool(value), "visible")
        self._cancel_edit()
        if value:
            self._build_panel()
            ol = self.query_one("#panel-options", OptionList)
            # Skip disabled headers/separators to highlight first selectable item
            for i in range(ol.option_count):
                opt = ol.get_option_at_index(i)
                if not opt.disabled:
                    ol.highlighted = i
                    break
            ol.focus()
        else:
            self.query_one("#table", DataTable).focus()

    # ── Info bar ─────────────────────────────────────────────

    def _info_text(self) -> str:
        ws, we = get_week_boundaries(int(self.gs.get("week_offset", 0)))
        station = self.gs.get("name", "N/A")
        src = self.gs.get("tle_source", "celestrak").capitalize()
        min_el = self.gs.get("min_elevation_deg", 0)
        week_str = f"{ws.strftime('%b %d')} — {we.strftime('%b %d, %Y')}"
        info = f" {station}  |  {week_str}  |  {src}  |  {min_el} deg min"
        if self.last_run:
            ago = int((datetime.now().astimezone() - self.last_run).total_seconds())
            info += f"  |  ran {ago}s ago" if ago < 60 else f"  |  ran {ago // 60}m ago"
        if self.last_csv:
            info += f"  |  {self.last_csv}"
        return info

    def _refresh_info(self) -> None:
        try:
            self.query_one("#info-bar", Static).update(self._info_text())
        except Exception:
            pass

    # ── Inline editing ───────────────────────────────────────

    def _start_edit(self, key: str, label: str, default: str, prefill: bool = False) -> None:
        ol = self.query_one("#panel-options", OptionList)
        self._editing_key = key
        self._editing_buf = default if prefill else ""
        self._editing_original = default
        self._editing_label = label
        self._editing_option_idx = ol.highlighted
        self._update_edit_display()

    def _update_edit_display(self) -> None:
        ol = self.query_one("#panel-options", OptionList)
        if self._editing_option_idx >= 0:
            display = f"  {self._editing_label}: {self._editing_buf}█"
            ol.replace_option_prompt_at_index(self._editing_option_idx, Text(display, style="bold #FFCC00"))

    def _commit_edit(self) -> None:
        key = self._editing_key
        value = self._editing_buf.strip()
        idx = self._editing_option_idx
        self._editing_key = None

        if not key:
            self._rebuild_and_restore(idx)
            return
        if not value:
            if key == "edit_sat":
                # Empty = delete satellite
                si = self._editing_sat_idx
                if 0 <= si < len(self.sats):
                    removed = self.sats.pop(si)
                    self.status_text = f"Removed {removed[1] or removed[0]}"
                self._refresh_info()
                self._rebuild_and_restore(min(idx, len(self.sats) + 1))
                return
            # Empty input — keep original value
            self._rebuild_and_restore(idx)
            return

        if key == "name":
            self.gs[key] = value
        elif key in ("latitude", "longitude", "altitude_m", "min_elevation_deg"):
            try:
                self.gs[key] = float(value)
            except ValueError:
                self.status_text = f"Invalid number for {key}"
                self._rebuild_and_restore(idx)
                return
        elif key in ("spacetrack_user", "spacetrack_pass"):
            self.secrets[key] = value
            save_secrets(SECRETS_PATH, self.secrets)
        elif key == "add_sat":
            parts = value.split(None, 1)
            nid = parts[0]
            sname = parts[1] if len(parts) > 1 else None
            if not nid.isdigit():
                self.status_text = "NORAD ID must be a number."
                self._rebuild_and_restore(idx)
                return
            if nid in {n for n, _ in self.sats}:
                self.status_text = f"NORAD ID {nid} already tracked."
                self._rebuild_and_restore(idx)
                return
            self.sats.append((nid, sname))
            self.status_text = f"Added {sname or nid}"
        elif key == "edit_sat":
            parts = value.split(None, 1)
            nid = parts[0]
            sname = parts[1] if len(parts) > 1 else None
            if not nid.isdigit():
                self.status_text = "NORAD ID must be a number."
                self._rebuild_and_restore(idx)
                return
            si = self._editing_sat_idx
            if 0 <= si < len(self.sats):
                self.sats[si] = (nid, sname)
                self.status_text = f"Updated {sname or nid}"

        self._refresh_info()
        self._rebuild_and_restore(idx)

    def _cancel_edit(self) -> None:
        if self._editing_key:
            idx = self._editing_option_idx
            self._editing_key = None
            self._rebuild_and_restore(idx)

    def _rebuild_and_restore(self, idx: int) -> None:
        self._build_panel()
        ol = self.query_one("#panel-options", OptionList)
        if 0 <= idx < ol.option_count:
            ol.highlighted = idx

    def on_key(self, event) -> None:
        if not self._editing_key:
            return
        key = event.key
        if key == "enter":
            self._commit_edit()
            event.stop()
        elif key == "escape":
            self._cancel_edit()
            event.stop()
        elif key == "backspace":
            self._editing_buf = self._editing_buf[:-1]
            self._update_edit_display()
            event.stop()
        elif event.character and event.character.isprintable():
            self._editing_buf += event.character
            self._update_edit_display()
            event.stop()
        else:
            event.stop()  # consume all keys during editing

    # ── Actions ──────────────────────────────────────────────

    def action_run_predictions(self) -> None:
        if not self._predicting:
            self._predict()

    def action_export(self) -> None:
        name = export_csv(self.passes, int(self.gs.get("week_offset", 0)))
        if name:
            self.last_csv = name
            self.status_text = f"Exported -> {name}"
            self._refresh_info()
            self.push_screen(SplashModal(f"Exported to\n{name}", timeout=1.0))
        else:
            self.push_screen(SplashModal("No data — run predictions first.", timeout=1.0))

    def _is_week_highlighted(self) -> bool:
        if self.sidebar_mode != "cfg":
            return False
        ol = self.query_one("#panel-options", OptionList)
        idx = ol.highlighted
        if idx is None or idx < 0 or idx >= ol.option_count:
            return False
        opt = ol.get_option_at_index(idx)
        return opt.id == "cfg_week"

    def action_week_prev(self) -> None:
        if self._editing_key or not self._is_week_highlighted():
            return
        cur = int(self.gs.get("week_offset", 0))
        if cur > 0:
            self.gs["week_offset"] = cur - 1
            self._refresh_info()
            self._rebuild_and_restore(self.query_one("#panel-options", OptionList).highlighted)
            self.status_text = "Week changed — press R to run"

    def action_week_next(self) -> None:
        if self._editing_key or not self._is_week_highlighted():
            return
        self.gs["week_offset"] = int(self.gs.get("week_offset", 0)) + 1
        self._refresh_info()
        self._rebuild_and_restore(self.query_one("#panel-options", OptionList).highlighted)
        self.status_text = "Week changed — press R to run"

    def action_panel(self, name: str) -> None:
        if self._editing_key:
            return
        self.sidebar_mode = "" if self.sidebar_mode == name else name

    def action_escape_action(self) -> None:
        if self._editing_key:
            self._cancel_edit()
        elif self.sidebar_mode:
            self.sidebar_mode = ""

    # ── Panel building ───────────────────────────────────────

    def _build_panel(self) -> None:
        ol = self.query_one("#panel-options", OptionList)
        ol.clear_options()

        if self.sidebar_mode == "cfg":
            self._build_cfg_panel(ol)
        elif self.sidebar_mode == "sats":
            self._build_sats_panel(ol)
        elif self.sidebar_mode == "help":
            self._build_help_panel(ol)

    def _build_cfg_panel(self, ol: OptionList) -> None:
        ws, we = get_week_boundaries(int(self.gs.get("week_offset", 0)))
        src = self.gs.get("tle_source", "celestrak")
        st_user = self.secrets.get("spacetrack_user", "") or "(not set)"
        st_pass_display = "****" if self.secrets.get("spacetrack_pass") else "(not set)"

        ol.add_option(Option(Text("GROUND STATION", style="bold #FFCC00"), disabled=True))
        ol.add_option(None)
        ol.add_option(Option(Text(f"  Station: {self.gs.get('name', 'N/A')}"), id="cfg_name"))
        ol.add_option(Option(Text(f"  Latitude: {self.gs.get('latitude', 0)}"), id="cfg_lat"))
        ol.add_option(Option(Text(f"  Longitude: {self.gs.get('longitude', 0)}"), id="cfg_lon"))
        ol.add_option(Option(Text(f"  Altitude: {self.gs.get('altitude_m', 0)} m"), id="cfg_alt"))
        ol.add_option(Option(Text(f"  Min Elev: {self.gs.get('min_elevation_deg', 0)} deg"), id="cfg_elev"))
        ol.add_option(None)
        week_label = f"  ◀ {ws.strftime('%b %d')} — {we.strftime('%b %d, %Y')} ▶"
        ol.add_option(Option(Text(week_label, style="bold #FFCC00"), id="cfg_week"))
        ol.add_option(None)
        ol.add_option(Option(Text("TLE SOURCE", style="bold #FFCC00"), disabled=True))
        ct = "(*)" if src == "celestrak" else "   "
        st = "(*)" if src == "spacetrack" else "   "
        ol.add_option(Option(Text(f"  {ct} CelesTrak", style="bold" if src == "celestrak" else ""), id="cfg_tle_ct"))
        ol.add_option(Option(Text(f"  {st} Space-Track", style="bold" if src == "spacetrack" else ""), id="cfg_tle_st"))
        ol.add_option(None)
        ol.add_option(Option(Text(f"  ST Username: {st_user}"), id="cfg_st_user"))
        ol.add_option(Option(Text(f"  ST Password: {st_pass_display}"), id="cfg_st_pass"))
        ol.add_option(None)
        ol.add_option(Option(Text("  [ Save Configuration ]", style="bold #FFCC00"), id="cfg_save"))

    def _build_sats_panel(self, ol: OptionList) -> None:
        ol.add_option(Option(Text(f"SATELLITES ({len(self.sats)})", style="bold #FFCC00"), disabled=True))
        ol.add_option(None)
        for i, (nid, name) in enumerate(self.sats):
            ol.add_option(Option(Text(f"  {nid:>6}  {name or '—'}"), id=f"sat_{i}"))
        ol.add_option(Option(Text("  [ + Add Satellite ]", style="bold #FFCC00"), id="sat_add"))
        ol.add_option(None)
        ol.add_option(Option(Text("  [ Save List ]", style="bold #FFCC00"), id="sat_save"))
        ol.add_option(Option(Text("  [ Delete Last ]", style="#aa3333"), id="sat_del"))

    def _build_help_panel(self, ol: OptionList) -> None:
        items = [
            ("GLOBAL KEYS", True),
            ("  R           Run predictions", False),
            ("  E           Export CSV", False),
            ("  ← →         Change week", False),
            ("  ↑ ↓         Navigate table", False),
            ("  1 / 2 / 3   Config / Sats / Help", False),
            ("  Esc         Close / cancel edit", False),
            ("  Q           Quit", False),
            ("", True),
            ("PANELS", True),
            ("  ↑ ↓         Navigate options", False),
            ("  Enter       Select / edit / toggle", False),
            ("  Esc         Close panel", False),
            ("", True),
            ("ELEVATION COLORS", True),
            ("  ■ 60 deg+   High (best)", False),
            ("  ■ 15-60     Medium", False),
            ("  ■ <15 deg   Low", False),
        ]
        for label, is_header in items:
            if not label:
                ol.add_option(None)
            elif is_header:
                ol.add_option(Option(Text(label, style="bold #FFCC00"), disabled=True))
            else:
                ol.add_option(Option(Text(label), disabled=True))

    # ── Panel interaction ────────────────────────────────────

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        oid = event.option.id or ""
        ol = self.query_one("#panel-options", OptionList)
        idx = ol.highlighted

        # Config panel
        if oid == "cfg_name":
            self._start_edit("name", "Station", str(self.gs.get("name", "")))
        elif oid == "cfg_lat":
            self._start_edit("latitude", "Latitude", str(self.gs.get("latitude", "")))
        elif oid == "cfg_lon":
            self._start_edit("longitude", "Longitude", str(self.gs.get("longitude", "")))
        elif oid == "cfg_alt":
            self._start_edit("altitude_m", "Altitude", str(self.gs.get("altitude_m", "")))
        elif oid == "cfg_elev":
            self._start_edit("min_elevation_deg", "Min Elev", str(self.gs.get("min_elevation_deg", "")))
        elif oid == "cfg_tle_ct":
            self.gs["tle_source"] = "celestrak"
            self._rebuild_and_restore(idx)
            self._refresh_info()
            self.status_text = "TLE source: CelesTrak"
        elif oid == "cfg_tle_st":
            self.gs["tle_source"] = "spacetrack"
            self._rebuild_and_restore(idx)
            self._refresh_info()
            self.status_text = "TLE source: Space-Track"
        elif oid == "cfg_st_user":
            self._start_edit("spacetrack_user", "ST Username", self.secrets.get("spacetrack_user", ""))
        elif oid == "cfg_st_pass":
            self._start_edit("spacetrack_pass", "ST Password", "")
        elif oid == "cfg_save":
            save_ground_station(GS_PATH, self.gs)
            save_secrets(SECRETS_PATH, self.secrets)
            self.status_text = "Configuration saved."

        # Satellites panel
        elif oid == "sat_add":
            self._start_edit("add_sat", "NORAD ID [name]", "")
        elif oid == "sat_save":
            save_norad_ids(IDS_PATH, self.sats)
            self.status_text = f"Saved {len(self.sats)} satellites."
        elif oid == "sat_del":
            if self.sats:
                removed = self.sats.pop()
                self.status_text = f"Removed {removed[1] or removed[0]}"
                self._rebuild_and_restore(idx)
        elif oid.startswith("sat_"):
            si = int(oid.split("_")[1])
            if 0 <= si < len(self.sats):
                nid, sname = self.sats[si]
                self._editing_sat_idx = si
                self._start_edit("edit_sat", "NORAD ID [name]", f"{nid} {sname}" if sname else nid, prefill=True)

    # ── Predictions ──────────────────────────────────────────

    def _update_progress(self, pct: float) -> None:
        pct = max(0.0, min(100.0, pct))
        elapsed = time.monotonic() - self._predict_t0
        if pct > 1:
            eta_sec = elapsed / pct * (100 - pct)
            if eta_sec < 60:
                eta = f" ~{int(eta_sec)}s left"
            else:
                eta = f" ~{int(eta_sec) // 60}m{int(eta_sec) % 60:02d}s left"
        else:
            eta = ""

        try:
            w = self.query_one("#progress-bar", Static).size.width or 80
        except Exception:
            w = 80

        label = f" {pct:.0f}%{eta} "
        bar_w = max(w - len(label), 10)
        filled = int(pct / 100 * bar_w)
        display = "█" * filled + " " * (bar_w - filled) + label

        try:
            self.call_from_thread(
                self.query_one("#progress-bar", Static).update, display
            )
        except Exception:
            pass

    @work(thread=True, exclusive=True)
    def _predict(self) -> None:
        self._predicting = True
        self._predict_t0 = time.monotonic()
        pbar = self.query_one("#progress-bar", Static)
        self.call_from_thread(pbar.set_class, True, "visible")

        try:
            ts = skyfield_load.timescale()
            ws, we = get_week_boundaries(int(self.gs.get("week_offset", 0)))
            t0 = ts.from_datetime(ws.astimezone(timezone.utc))
            t1 = ts.from_datetime(we.astimezone(timezone.utc))
            gs_pos = wgs84.latlon(
                self.gs["latitude"], self.gs["longitude"],
                elevation_m=self.gs["altitude_m"],
            )
            min_el = self.gs["min_elevation_deg"]
            total = len(self.sats)
            tle_source = self.gs.get("tle_source", "celestrak")
            tle_data: dict[str, tuple[str, str, str]] = {}
            freq_data: dict[str, tuple[str, str]] = {}

            nid_list = [nid for nid, _ in self.sats]

            # Phase 1a: Fetch TLEs
            if tle_source == "spacetrack":
                st_user = self.secrets.get("spacetrack_user", "")
                st_pass = self.secrets.get("spacetrack_pass", "")
                if not st_user or not st_pass:
                    self.status_text = "Error: Space-Track credentials not set — open Config (1)."
                    return
                self.status_text = f"Logging into Space-Track and fetching {total} TLEs..."
                tle_data = fetch_tles_spacetrack(nid_list, st_user, st_pass)
                if not tle_data:
                    self.status_text = "Error: Space-Track login failed or no data."
                    return
                self._update_progress(20)
            else:
                tle_errors: list[str] = []
                fetched = [0]

                def fetch_tle_task(nid):
                    try:
                        tle = fetch_tle(nid)
                    except RuntimeError as e:
                        tle_errors.append(str(e))
                        tle = None
                    fetched[0] += 1
                    self._update_progress(20 * fetched[0] / max(total, 1))
                    self.status_text = f"Fetched TLE {fetched[0]}/{total}..."
                    return nid, tle

                with ThreadPoolExecutor(max_workers=4) as pool:
                    for nid, tle in pool.map(lambda n: fetch_tle_task(n), nid_list):
                        if tle is not None:
                            tle_data[nid] = tle

                if tle_errors:
                    self.status_text = f"TLE warnings: {'; '.join(tle_errors[:3])}"

            # Phase 1b: Fetch frequencies
            self.status_text = "Fetching frequency data from SatNOGS..."
            freq_fetched = [0]

            def fetch_freq_task(nid):
                freq, ant = fetch_frequency_info(nid)
                freq_fetched[0] += 1
                self._update_progress(20 + 20 * freq_fetched[0] / max(total, 1))
                self.status_text = f"Fetched frequency {freq_fetched[0]}/{total}..."
                return nid, freq, ant

            with ThreadPoolExecutor(max_workers=8) as pool:
                for nid, freq, ant in pool.map(lambda n: fetch_freq_task(n), nid_list):
                    freq_data[nid] = (freq, ant)

            # Phase 2: Compute passes
            result = []
            sats_with_tle = [(nid, uname) for nid, uname in self.sats if nid in tle_data]
            compute_total = len(sats_with_tle)

            for i, (nid, uname) in enumerate(sats_with_tle):
                tle = tle_data[nid]
                freq, ant = freq_data[nid]
                sat_name = uname or tle[0].strip()

                self._update_progress(40 + 60 * i / max(compute_total, 1))
                self.status_text = f"[{i + 1}/{compute_total}] {sat_name} — SGP4..."

                sat = EarthSatellite(tle[1], tle[2], sat_name, ts)
                for p in find_passes(sat, gs_pos, ts, t0, t1, min_el):
                    p.update(sat_name=sat_name, norad_id=nid, frequency=freq, antenna=ant)
                    result.append(p)

            self._update_progress(100)
            result.sort(key=lambda p: p["aos_utc"])
            self.passes = result

            csv_name = export_csv(result, int(self.gs.get("week_offset", 0)))
            self.last_run = datetime.now().astimezone()
            self.last_csv = csv_name

            self.call_from_thread(self._populate_table)

            skipped = total - compute_total
            station = self.gs.get("name", "N/A")
            week = f"{ws.strftime('%b %d')} — {we.strftime('%b %d, %Y')}"
            skip_msg = f"  |  {skipped} TLE failed" if skipped else ""
            elapsed = time.monotonic() - self._predict_t0
            self.status_text = (
                f"Done — {len(result)} passes / {compute_total} sats"
                f"  |  {station}  |  {week}{skip_msg}"
                f"  |  {elapsed:.1f}s"
            )
            if csv_name:
                msg = f"Exported {len(result)} passes to\n{csv_name}"
            else:
                msg = f"Done — {len(result)} passes found\nNo passes to export"
            self.call_from_thread(self.push_screen, SplashModal(msg, timeout=1.0))
        except Exception as e:
            self.status_text = f"Error: {e}"
        finally:
            self._predicting = False
            self.call_from_thread(pbar.set_class, False, "visible")
            self.call_from_thread(self._refresh_info)

    def _populate_table(self) -> None:
        table = self.query_one("#table", DataTable)
        table.clear()

        for i, p in enumerate(self.passes):
            aos, los = p["aos_utc"], p["los_utc"]
            aos_l, los_l = aos.astimezone(), los.astimezone()
            dur = los - aos
            el = p["max_el_deg"]

            if el >= 60:
                style = "bold #55ff55"
            elif el < 15:
                style = "#666666"
            else:
                style = ""

            def s(val, st=style):
                return Text(str(val), style=st) if st else str(val)

            table.add_row(
                s(i + 1), s(p["sat_name"][:20]), s(p["norad_id"]),
                s(aos.strftime("%m-%d %H:%M:%S")), s(los.strftime("%m-%d %H:%M:%S")),
                s(aos_l.strftime("%m-%d %H:%M:%S")), s(los_l.strftime("%m-%d %H:%M:%S")),
                s(f"{format_duration(dur)} min"), s(f"{el:.1f} deg"),
                s(p.get("antenna", "N/A")),
            )


# ── Entry point ──────────────────────────────────────────────

if __name__ == "__main__":
    SatPassPredictor().run()
