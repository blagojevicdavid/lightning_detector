# Lightning Detector

Python script that listens for live lightning strikes from the LightningMaps WebSocket server and filters them by configured locations. Each location has its own radius — strikes within the radius are logged to separate JSONL files.

A practical use case is automatically shutting down or powering on sensitive equipment at remote locations when lightning activity is detected nearby.

## Installation

```bash
python3 -m venv venv
source venv/bin/activate
pip install websockets
```

On Windows:
```powershell
python -m venv venv
venv\Scripts\Activate.ps1
pip install websockets
```

## Configuration

All locations are defined in `config.json`:

```json
{
  "locations": [
    {"name": "Banja Luka", "lat": 44.7722, "lon": 17.1910, "radius_km": 40},
    {"name": "Prnjavor",   "lat": 44.8686, "lon": 17.6608, "radius_km": 40}
  ]
}
```

| Field | Description |
|-------|-------------|
| `name` | Location name — also used to generate the output filename |
| `lat` | Latitude of the location center |
| `lon` | Longitude of the location center |
| `radius_km` | Radius in kilometers within which a strike is considered relevant |

Output files are generated automatically from the location name into the `web/` folder. For example, `Banja Luka` → `web/banja_luka.jsonl`.

## Running

```bash
source venv/bin/activate
python lightning_detector.py
```

On startup the script prints the loaded locations and confirms the connection:

```
Loaded 2 location(s):
  Banja Luka: (44.7722, 17.191) r=40 km → web/banja_luka.jsonl
  Prnjavor: (44.8686, 17.6608) r=40 km → web/prnjavor.jsonl
Connected to wss://live.lightningmaps.org:443/
```

Every received strike is printed as `[ALL]`. Strikes that fall within a location's radius are also printed as `[STRIKE]` and written to the corresponding log file:

```
[ALL]    time=2026-04-18T21:15:03+00:00 lat=44.82341 lon=17.54123
[STRIKE] Banja Luka time=2026-04-18T21:15:03+00:00 lat=44.82341 lon=17.54123 distance_km=18.43
```

## Output format

Each location gets its own JSONL file (one JSON record per line). Example record:

```json
{
  "logged_at": "2026-04-18T21:15:03.412Z",
  "strike_time": "2026-04-18T21:15:03+00:00",
  "lat": 44.82341,
  "lon": 17.54123,
  "distance_km": 18.43,
  "polarity": 1,
  "altitude_m": 0,
  "source_id": null
}
```

## On-strike hook

Inside `process_strike()` there is a marked place where you can add code that runs every time a strike hits a location's radius:

```python
# === on_strike: place code here that runs when a strike hits a location's radius ===
# Available variables:
#   loc          — {"name": ..., "lat": ..., "lon": ..., "radius_km": ..., "output_file": ...}
#   strike       — raw strike data with lat/lon/time/pol/alt
#   distance_km  — distance from the location center in kilometers
```

## Note

Blitzortung/LightningMaps publicly states that access to their live WebSocket servers is limited and should not be used for commercial or high-frequency workloads.
