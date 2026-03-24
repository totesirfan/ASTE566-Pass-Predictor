#!/usr/bin/env python3
"""
ASTE 566 — Ground Communications for Satellite Operations
Satellite Pass Predictor

Predicts satellite passes for the current week (Monday–Sunday, local time)
using SGP4/SDP4 propagation via Skyfield — the same propagator used by GPredict.

TLE data is fetched from CelesTrak. Output is a formatted console table and CSV.
"""

from __future__ import annotations

import csv
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import requests
from skyfield.api import EarthSatellite, load, wgs84


# ---------------------------------------------------------------------------
# Configuration loading
# ---------------------------------------------------------------------------

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def load_ground_station(path: str) -> dict:
    """Load ground station parameters from a JSON file."""
    with open(path, "r") as f:
        gs = json.load(f)
    required = ["latitude", "longitude", "altitude_m", "min_elevation_deg"]
    for key in required:
        if key not in gs:
            sys.exit(f"Error: '{key}' missing from ground station config.")
    return gs


def load_norad_ids(path: str) -> list[tuple[str, str | None]]:
    """
    Parse norad_ids.txt.
    Returns list of (norad_id, optional_name) tuples.
    """
    entries = []
    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(None, 1)
            norad_id = parts[0]
            name = parts[1] if len(parts) > 1 else None
            entries.append((norad_id, name))
    return entries


# ---------------------------------------------------------------------------
# TLE fetching
# ---------------------------------------------------------------------------

CELESTRAK_URL = "https://celestrak.org/NORAD/elements/gp.php"


def fetch_tle(norad_id: str) -> tuple[str, str, str] | None:
    """
    Fetch TLE from CelesTrak for a given NORAD catalog number.
    Returns (name, line1, line2) or None on failure.
    """
    try:
        resp = requests.get(
            CELESTRAK_URL,
            params={"CATNR": norad_id, "FORMAT": "TLE"},
            timeout=15,
        )
        resp.raise_for_status()
        lines = resp.text.strip().splitlines()
        if len(lines) < 3:
            print(f"  WARNING: No TLE data returned for NORAD ID {norad_id}")
            return None
        return lines[0].strip(), lines[1].strip(), lines[2].strip()
    except requests.RequestException as e:
        print(f"  WARNING: Failed to fetch TLE for NORAD ID {norad_id}: {e}")
        return None


# ---------------------------------------------------------------------------
# Frequency / antenna type lookup
# ---------------------------------------------------------------------------

SATNOGS_URL = "https://db.satnogs.org/api/transmitters/"


def classify_antenna(freq_hz: int) -> str:
    """Classify a frequency as UHF or S-Band."""
    freq_mhz = freq_hz / 1e6
    if freq_mhz < 1000:
        return "UHF"
    else:
        return "S-Band"


def fetch_frequency_info(norad_id: str) -> tuple[str, str]:
    """
    Fetch the primary downlink frequency from SatNOGS DB.
    Returns (frequency_str, antenna_type) e.g. ("437.250 MHz", "UHF").
    Falls back to ("N/A", "N/A") on failure.
    """
    try:
        resp = requests.get(
            SATNOGS_URL,
            params={"satellite__norad_cat_id": norad_id, "format": "json"},
            timeout=15,
        )
        resp.raise_for_status()
        transmitters = resp.json()

        # Filter to active transmitters with a downlink frequency
        active = [
            t for t in transmitters
            if t.get("alive") and t.get("downlink_low") and t.get("status") == "active"
        ]
        if not active:
            return "N/A", "N/A"

        # Prefer UHF/VHF downlinks (< 1 GHz) first, then pick the first active one
        uhf_txs = [t for t in active if t["downlink_low"] < 1e9]
        chosen = uhf_txs[0] if uhf_txs else active[0]

        freq_hz = chosen["downlink_low"]
        freq_mhz = freq_hz / 1e6
        freq_str = f"{freq_mhz:.3f} MHz"
        antenna = classify_antenna(freq_hz)
        return freq_str, antenna

    except (requests.RequestException, KeyError, ValueError) as e:
        print(f"  WARNING: Could not fetch frequency for NORAD ID {norad_id}: {e}")
        return "N/A", "N/A"


# ---------------------------------------------------------------------------
# Time window
# ---------------------------------------------------------------------------

def get_week_boundaries() -> tuple[datetime, datetime]:
    """
    Return (start, end) of the current week in the local timezone.
    Monday 00:00:00 → Sunday 23:59:59.
    """
    now = datetime.now().astimezone()  # local tz-aware
    # Monday of this week
    monday = now - timedelta(days=now.weekday())
    start = monday.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=7) - timedelta(seconds=1)
    return start, end


# ---------------------------------------------------------------------------
# Pass prediction
# ---------------------------------------------------------------------------

def find_passes(satellite, gs_position, ts, t_start, t_end, min_el_deg):
    """
    Find all passes of *satellite* over *gs_position* between t_start and t_end.

    Uses Skyfield's difference geometry and a time-stepping approach:
      - Coarse scan at 60 s steps to detect elevation threshold crossings
      - Refine AOS/LOS to ~1 s accuracy via bisection
      - Track max elevation during each pass

    Returns a list of dicts with keys:
        aos_utc, los_utc, max_el_deg
    """
    diff = satellite - gs_position

    # Build a time array spanning the week at 60-second intervals
    step_seconds = 60
    total_seconds = int((t_end.utc_datetime() - t_start.utc_datetime()).total_seconds())
    steps = total_seconds // step_seconds + 1

    passes = []
    in_pass = False
    aos_time = None
    max_el = 0.0

    for i in range(steps + 1):
        t = ts.utc(
            t_start.utc_datetime().year,
            t_start.utc_datetime().month,
            t_start.utc_datetime().day,
            t_start.utc_datetime().hour,
            t_start.utc_datetime().minute,
            t_start.utc_datetime().second + i * step_seconds,
        )
        alt, _, _ = diff.at(t).altaz()
        el = alt.degrees

        if not in_pass and el >= min_el_deg:
            # AOS detected — refine by bisecting the previous interval
            if i > 0:
                aos_time = _refine_crossing(
                    diff, ts, t_start, (i - 1) * step_seconds, i * step_seconds, min_el_deg, rising=True
                )
            else:
                aos_time = t
            in_pass = True
            max_el = el
        elif in_pass and el >= min_el_deg:
            if el > max_el:
                max_el = el
        elif in_pass and el < min_el_deg:
            # LOS detected — refine
            los_time = _refine_crossing(
                diff, ts, t_start, (i - 1) * step_seconds, i * step_seconds, min_el_deg, rising=False
            )
            passes.append({
                "aos_utc": aos_time.utc_datetime().replace(tzinfo=timezone.utc),
                "los_utc": los_time.utc_datetime().replace(tzinfo=timezone.utc),
                "max_el_deg": round(max_el, 1),
            })
            in_pass = False
            aos_time = None
            max_el = 0.0

    # Handle pass that extends past the window
    if in_pass and aos_time is not None:
        passes.append({
            "aos_utc": aos_time.utc_datetime().replace(tzinfo=timezone.utc),
            "los_utc": t_end.utc_datetime().replace(tzinfo=timezone.utc),
            "max_el_deg": round(max_el, 1),
        })

    return passes


def _refine_crossing(diff, ts, t_start, sec_lo, sec_hi, threshold, rising):
    """Bisect to find the elevation threshold crossing to ~1 s accuracy."""
    base = t_start.utc_datetime()
    for _ in range(10):  # ~1 s precision after 10 iterations of 60 s window
        sec_mid = (sec_lo + sec_hi) / 2.0
        t_mid = ts.utc(base.year, base.month, base.day,
                       base.hour, base.minute, base.second + sec_mid)
        alt, _, _ = diff.at(t_mid).altaz()
        above = alt.degrees >= threshold
        if rising:
            if above:
                sec_hi = sec_mid
            else:
                sec_lo = sec_mid
        else:
            if above:
                sec_lo = sec_mid
            else:
                sec_hi = sec_mid
    # Return the midpoint as the crossing time
    sec_final = (sec_lo + sec_hi) / 2.0
    return ts.utc(base.year, base.month, base.day,
                  base.hour, base.minute, base.second + sec_final)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def format_duration_min(td: timedelta) -> str:
    """Format a timedelta as minutes.seconds (e.g. 9.45 = 9 min 45 sec)."""
    total_sec = int(td.total_seconds())
    minutes = total_sec // 60
    seconds = total_sec % 60
    return str(minutes) + "." + str(seconds).zfill(2)


def utc_to_local(dt_utc: datetime) -> datetime:
    """Convert a UTC datetime to the local timezone."""
    return dt_utc.astimezone()


def pass_to_row(p: dict, idx: int) -> list:
    """Convert a pass dict to a table row."""
    aos_local = utc_to_local(p["aos_utc"])
    los_local = utc_to_local(p["los_utc"])
    duration = p["los_utc"] - p["aos_utc"]
    return [
        idx,
        p["sat_name"],
        p["norad_id"],
        str(p["aos_utc"].strftime("%Y-%m-%d %H:%M:%S")),
        str(p["los_utc"].strftime("%Y-%m-%d %H:%M:%S")),
        str(aos_local.strftime("%Y-%m-%d %H:%M:%S")),
        str(los_local.strftime("%Y-%m-%d %H:%M:%S")),
        format_duration_min(duration),
        str(round(p["max_el_deg"], 1)),
        DEFAULT_GRP,
        p.get("antenna", "N/A"),
    ]


DEFAULT_GRP = "Group 5"

HEADER = [
    "Pass #", "Satellite Name", "NORAD ID",
    "AOS (UTC)", "LOS (UTC)",
    "AOS (Local)", "LOS (Local)",
    "Duration (min)", "MaxEl (deg)", "GRP", "Antenna",
]

def print_and_export(all_passes: list[dict], output_dir: str):
    """Print a PrettyTable to console and export CSV."""
    if not all_passes:
        print("\nNo passes found for the given parameters.")
        return

    # Sort by AOS time
    all_passes.sort(key=lambda p: p["aos_utc"])

    all_rows = []
    for idx, p in enumerate(all_passes, start=1):
        row = pass_to_row(p, idx)
        all_rows.append(row)

    # Console output — plain text, tab-separated
    week_start, week_end = get_week_boundaries()
    local_tz_name = week_start.strftime("%Z")
    print(f"\nSATELLITE PASS PREDICTIONS")
    print(f"Week: {week_start.strftime('%Y-%m-%d')} to {week_end.strftime('%Y-%m-%d')}")
    print(f"Local timezone: {local_tz_name}")
    print(f"Total passes: {len(all_passes)}")
    print()
    print("\t".join(str(h) for h in HEADER))
    for row in all_rows:
        print("\t".join(str(c) for c in row))

    # --- Export ---
    os.makedirs(output_dir, exist_ok=True)

    week_label = f"{week_start.strftime('%Y-%m-%d')}_to_{week_end.strftime('%Y-%m-%d')}"
    csv_path = os.path.join(output_dir, f"passes_{week_label}.csv")
    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(HEADER)
        writer.writerows(all_rows)
    print(f"\nCSV exported to: {csv_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    gs_path = os.path.join(SCRIPT_DIR, "ground_station.json")
    ids_path = os.path.join(SCRIPT_DIR, "norad_ids.txt")
    output_dir = os.path.join(SCRIPT_DIR, "output")

    print("Loading ground station config...")
    gs = load_ground_station(gs_path)
    print(f"  Station: {gs.get('name', 'N/A')}")
    print(f"  Location: {gs['latitude']}°N, {gs['longitude']}°E, {gs['altitude_m']}m")
    print(f"  Min elevation: {gs['min_elevation_deg']}°")

    print("\nLoading NORAD ID list...")
    entries = load_norad_ids(ids_path)
    print(f"  {len(entries)} satellite(s) loaded")

    # Skyfield timescale & ground station position
    ts = load.timescale()
    gs_position = wgs84.latlon(
        gs["latitude"], gs["longitude"], elevation_m=gs["altitude_m"]
    )
    min_el = gs["min_elevation_deg"]

    # Week boundaries
    week_start, week_end = get_week_boundaries()
    print(f"\nPrediction window (local): {week_start} → {week_end}")
    t_start = ts.from_datetime(week_start.astimezone(timezone.utc))
    t_end = ts.from_datetime(week_end.astimezone(timezone.utc))

    # Fetch TLEs and predict passes
    all_passes = []
    for norad_id, user_name in entries:
        print(f"\nFetching TLE for NORAD ID {norad_id}...")
        tle_data = fetch_tle(norad_id)
        if tle_data is None:
            continue
        tle_name, tle_line1, tle_line2 = tle_data
        sat_name = user_name if user_name else tle_name.strip()
        print(f"  Satellite: {sat_name}")

        freq_str, antenna = fetch_frequency_info(norad_id)
        print(f"  Frequency: {freq_str} ({antenna})")

        satellite = EarthSatellite(tle_line1, tle_line2, sat_name, ts)
        print(f"  Computing passes (min el {min_el}°)...")
        passes = find_passes(satellite, gs_position, ts, t_start, t_end, min_el)
        print(f"  Found {len(passes)} pass(es)")

        for p in passes:
            p["sat_name"] = sat_name
            p["norad_id"] = norad_id
            p["frequency"] = freq_str
            p["antenna"] = antenna
        all_passes.extend(passes)

    # Output
    print_and_export(all_passes, output_dir)


if __name__ == "__main__":
    main()
