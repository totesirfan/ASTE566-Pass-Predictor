# ASTE 566 — Satellite Pass Predictor

**Course:** ASTE 566 — Ground Communications for Satellite Operations
**University of Southern California**

A Python tool that predicts satellite passes over a ground station for the current week. It fetches live TLE data from [CelesTrak](https://celestrak.org/) and downlink frequency information from [SatNOGS](https://db.satnogs.org/), then computes pass windows using SGP4/SDP4 propagation via [Skyfield](https://rhodesmill.org/skyfield/).

---

## Project Structure

```
ASTE566-Pass-Predictor/
├── satpp.py                     # Main application (TUI)
├── ground_station.json          # Ground station configuration
├── norad_ids.txt                # List of satellites to track
├── output/                      # Generated CSV reports (auto-exported)
└── README.md
```

## Requirements

- Python 3.9+
- Internet connection (for fetching TLEs and frequency data)

Dependencies (`skyfield`, `requests`) are installed automatically on first run.

## Usage

```bash
python satpp.py
```

The main view shows the pass prediction table. Type commands directly and press Enter:

| Command | Action |
|---------|--------|
| `help`  | Show all keybindings and commands |
| `cfg`   | Edit ground station config (name, lat, lon, altitude, min elevation) |
| `sats`  | Manage satellite tracking list (add, remove, save) |
| `run`   | Run pass predictions |
| `export`| Re-export results to CSV |
| `quit`  | Exit |

CSV is automatically exported to `output/` after each prediction run.

**Keyboard shortcuts:** `R` run, `E` export CSV, `↑↓` navigate, `PgUp/PgDn` scroll, `ESC` close panel, `Q` quit.

## Configuration

### Ground Station (`ground_station.json`)

Defines the observer location. All fields are required. Editable from the TUI via the `cfg` command.

```json
{
  "name": "USC Ground Station",
  "latitude": 34.0208,
  "longitude": -118.2910,
  "altitude_m": 30,
  "min_elevation_deg": 5.0
}
```

| Field               | Description                                          |
|---------------------|------------------------------------------------------|
| `name`              | Human-readable station name                          |
| `latitude`          | Geodetic latitude in decimal degrees (positive = N)  |
| `longitude`         | Geodetic longitude in decimal degrees (positive = E) |
| `altitude_m`        | Altitude above sea level in meters                   |
| `min_elevation_deg` | Minimum elevation angle to consider a valid pass     |

### Satellite List (`norad_ids.txt`)

One satellite per line. Format: `NORAD_ID [optional name]`. Lines starting with `#` are comments. Editable from the TUI via the `sats` command.

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

1. **TLE Fetching** — Retrieves Two-Line Element sets from CelesTrak's GP API
2. **Frequency Lookup** — Queries SatNOGS transmitter database for active downlink frequencies; classifies as UHF (< 1 GHz) or S-Band (>= 1 GHz)
3. **Pass Prediction** — Uses Skyfield's `EarthSatellite` with a 60-second coarse scan to detect elevation threshold crossings, then refines AOS/LOS to ~1 second accuracy via bisection
4. **Time Window** — Automatically computes the current week (Monday–Sunday) in the local timezone

## Customization

- **Change ground station:** Type `cfg` in the TUI or edit `ground_station.json`
- **Change satellites:** Type `sats` in the TUI or edit `norad_ids.txt`
- **Change minimum elevation:** Modify `min_elevation_deg` in `ground_station.json`
- **Change group assignment:** Edit the `DEFAULT_GRP` variable in `satpp.py`

## Troubleshooting

| Issue | Solution |
|-------|----------|
| `WARNING: No TLE data returned` | NORAD ID may be invalid or satellite has decayed. Verify at [CelesTrak](https://celestrak.org/satcat/search.php) |
| `WARNING: Failed to fetch TLE` | Network error — check internet connection |
| `WARNING: Could not fetch frequency` | SatNOGS may not have data for this satellite; frequency will show as "N/A" |
| No passes found | Satellite may not pass over your station this week, or `min_elevation_deg` is too high |

## Data Sources

- **TLE Data:** [CelesTrak](https://celestrak.org/) (NORAD/USSPACECOM via GP API)
- **Frequency Data:** [SatNOGS Database](https://db.satnogs.org/)
- **Propagation:** SGP4/SDP4 via [Skyfield](https://rhodesmill.org/skyfield/)
