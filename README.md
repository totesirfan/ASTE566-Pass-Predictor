# ASTE 566 — Satellite Pass Predictor

**Course:** ASTE 566 — Ground Communications for Satellite Operations
**University of Southern California — Viterbi School of Engineering**
**Author:** Irfan Annuar

A Python TUI application that predicts satellite passes over a ground station for a selected week. It fetches live TLE data from [CelesTrak](https://celestrak.org/) or [Space-Track](https://www.space-track.org/) and downlink frequency information from [SatNOGS](https://db.satnogs.org/), then computes pass windows using vectorized SGP4/SDP4 propagation via [Skyfield](https://rhodesmill.org/skyfield/). Built with [Textual](https://textual.textualize.io/) for a modern terminal UI with USC cardinal and gold theming.

---

## Project Structure

```
ASTE566-Pass-Predictor/
├── satpp.py                              # Main application (Textual TUI)
├── ground_station.json                   # Ground station configuration
├── norad_ids.txt                         # List of satellites to track
├── .secrets.json                         # Space-Track credentials (gitignored)
├── output/                               # Generated CSV reports (auto-exported)
├── Satellite Pass Predictor (Mac).command
├── Satellite Pass Predictor (Linux).sh
├── Satellite Pass Predictor (Windows).bat
└── README.md
```

## Requirements

- Python 3.9+
- Internet connection (for fetching TLEs and frequency data)

Dependencies (`skyfield`, `requests`, `numpy`, `textual`, `rich`) are installed automatically on first run.

## Quick Start

Double-click the launcher for your platform:

| Platform | Launcher |
|----------|----------|
| macOS    | `Satellite Pass Predictor (Mac).command` |
| Linux    | `Satellite Pass Predictor (Linux).sh` |
| Windows  | `Satellite Pass Predictor (Windows).bat` |

Or run directly:

```bash
python satpp.py
```

## Controls

All interaction is keyboard-driven. The controls bar at the bottom shows available keys.

### Global

| Key       | Action                     |
|-----------|----------------------------|
| `R`       | Run pass predictions       |
| `E`       | Export results to CSV       |
| `1`       | Open Config panel          |
| `2`       | Open Satellites panel      |
| `3`       | Open Help panel            |
| `Esc`     | Close panel / cancel edit  |
| `Q`       | Quit                       |

### Panels

| Key       | Action                     |
|-----------|----------------------------|
| `↑ ↓`    | Navigate options           |
| `Enter`   | Select / edit / toggle     |
| `← →`    | Change week (on week row)  |
| `Esc`     | Cancel edit / close panel  |

### Inline Editing

When you press Enter on an editable field, it enters edit mode directly on the row. Type the new value, then:
- **Enter** to confirm
- **Esc** to cancel

For satellites, pressing Enter opens edit mode with the current value pre-filled. Clear the value and press Enter to delete the satellite.

### Elevation Color Coding

| Color       | Elevation    | Quality |
|-------------|-------------|---------|
| Green/bold  | 60 deg+     | High (best passes) |
| Normal      | 15–60 deg   | Medium  |
| Gray        | < 15 deg    | Low     |

## Configuration

### Ground Station (`ground_station.json`)

Defines the observer location and prediction settings. All fields are editable from the TUI via the Config panel (`1`).

```json
{
  "name": "USC Ground Station",
  "latitude": 34.0208,
  "longitude": -118.2910,
  "altitude_m": 30,
  "min_elevation_deg": 5.0,
  "week_offset": 0,
  "tle_source": "celestrak"
}
```

| Field               | Description                                          |
|---------------------|------------------------------------------------------|
| `name`              | Human-readable station name                          |
| `latitude`          | Geodetic latitude in decimal degrees (positive = N)  |
| `longitude`         | Geodetic longitude in decimal degrees (positive = E) |
| `altitude_m`        | Altitude above sea level in meters                   |
| `min_elevation_deg` | Minimum elevation angle to consider a valid pass     |
| `week_offset`       | Week selection: 0 = current week, 1 = next week, etc. |
| `tle_source`        | TLE data source: `celestrak` or `spacetrack`         |

### TLE Sources

| Source | Description |
|--------|-------------|
| **CelesTrak** (default) | Free, no login required. Fetches TLEs individually per satellite (4 concurrent). May rate-limit with large satellite lists. |
| **Space-Track** | Requires a free account at [space-track.org](https://www.space-track.org/auth/createAccount). Fetches all TLEs in a single batch request. Recommended for large satellite lists. |

To use Space-Track, select it in the Config panel and enter your credentials. Credentials are stored in `.secrets.json` (gitignored, never committed).

### Satellite List (`norad_ids.txt`)

One satellite per line. Format: `NORAD_ID [optional name]`. Lines starting with `#` are comments. Editable from the TUI via the Satellites panel (`2`).

```
# NORAD ID list — one per line
36797 AISSAT 1
25338 NOAA-15
64559 PADRE
60246 CATSAT
```

NORAD catalog IDs can be found at [CelesTrak](https://celestrak.org/satcat/search.php) or [N2YO](https://www.n2yo.com/).

## Output

### CSV Export

Automatically exported to `output/` after each prediction run. Can also be manually triggered with `E`.

Saved to `output/passes_YYYY-MM-DD_to_YYYY-MM-DD.csv` with the following columns:

| Column           | Description                                      |
|------------------|--------------------------------------------------|
| Pass #           | Sequential pass number (sorted by AOS time)      |
| Satellite Name   | Name from `norad_ids.txt` or TLE header          |
| NORAD ID         | NORAD catalog number                             |
| AOS (UTC)        | Acquisition of Signal — UTC timestamp            |
| LOS (UTC)        | Loss of Signal — UTC timestamp                   |
| AOS (Local)      | AOS in local timezone                            |
| LOS (Local)      | LOS in local timezone                            |
| Duration (min)   | Pass duration in `M.SS` format (e.g., `9.45`)   |
| MaxEl (deg)      | Maximum elevation angle during the pass          |
| GRP              | Group assignment (default: "Group 5")            |
| Antenna          | Antenna type based on frequency (UHF or S-Band)  |

## How It Works

1. **TLE Fetching** — Retrieves Two-Line Element sets from CelesTrak (parallel individual requests) or Space-Track (single authenticated batch request)
2. **Frequency Lookup** — Queries SatNOGS transmitter database in parallel (8 concurrent) for active downlink frequencies; classifies as UHF (< 1 GHz) or S-Band (>= 1 GHz)
3. **Pass Prediction** — Uses Skyfield's `EarthSatellite` with vectorized numpy computation for a 60-second coarse scan to detect elevation threshold crossings, then refines AOS/LOS to ~1 second accuracy via bisection
4. **Time Window** — Computes the selected week (Monday–Sunday) based on the week offset setting
5. **Progress** — Real-time progress bar with percentage and ETA displayed during computation

## Troubleshooting

| Issue | Solution |
|-------|----------|
| `No TLE found for NORAD ID` | NORAD ID may be invalid or satellite has decayed. Verify at [CelesTrak](https://celestrak.org/satcat/search.php) |
| `CelesTrak rate limit hit` | Too many requests — switch to Space-Track in Config for batch fetching |
| `Space-Track login failed` | Check credentials in Config — ensure your account is active at [space-track.org](https://www.space-track.org/) |
| `Network error fetching TLE` | Check internet connection |
| Frequency shows "N/A" | SatNOGS may not have data for this satellite |
| No passes found | Satellite may not pass over your station that week, or `min_elevation_deg` is too high |

## Data Sources

- **TLE Data:** [CelesTrak](https://celestrak.org/) (NORAD/USSPACECOM via GP API) or [Space-Track](https://www.space-track.org/) (authenticated batch API)
- **Frequency Data:** [SatNOGS Database](https://db.satnogs.org/)
- **Propagation:** SGP4/SDP4 via [Skyfield](https://rhodesmill.org/skyfield/)
- **TUI Framework:** [Textual](https://textual.textualize.io/)
