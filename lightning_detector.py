from __future__ import annotations

import asyncio
import json
import math
import os
import re
import signal
import sys
import time
from datetime import datetime, timezone
from typing import Any


CONFIG_FILE = "config.json"
WS_URL = "wss://live.lightningmaps.org:443/"
RECONNECT_DELAY = 5.0
LIGHTNINGMAPS_PROTOCOL_VERSION = 24


def load_config() -> list[dict[str, Any]]:
    with open(CONFIG_FILE, encoding="utf-8") as f:
        data = json.load(f)

    for loc in data["locations"]:
        slug = re.sub(r"[^a-z0-9]+", "_", loc["name"].lower()).strip("_")
        loc["output_file"] = os.path.join("web", f"{slug}.jsonl")

    return data["locations"]


def write_locations_json(locations: list[dict[str, Any]]) -> None:
    entries = [
        {"name": loc["name"], "file": os.path.basename(loc["output_file"])}
        for loc in locations
    ]
    path = os.path.join("web", "locations.json")
    os.makedirs("web", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)


# ── Geometry ──────────────────────────────────────────────────────────────────

def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    lat1_r, lat2_r = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1_r) * math.cos(lat2_r) * math.sin(dlon / 2) ** 2
    return 6371.0 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def build_combined_handshake(locations: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute a bbox covering all locations, then build the LightningMaps subscribe payload."""
    max_lat = max_lon = -math.inf
    min_lat = min_lon = math.inf

    for loc in locations:
        lat, lon = loc["lat"], loc["lon"]
        r = max(loc["radius_km"], 80.0)
        lat_d = r / 111.32
        lon_d = r / (111.32 * max(math.cos(math.radians(lat)), 0.1))
        max_lat = max(max_lat, lat + lat_d)
        min_lat = min(min_lat, lat - lat_d)
        max_lon = max(max_lon, lon + lon_d)
        min_lon = min(min_lon, lon - lon_d)

    return {
        "v": LIGHTNINGMAPS_PROTOCOL_VERSION,
        "i": {}, "s": False,
        "x": 0, "w": 0, "tx": 0, "tw": 0,
        "a": 4, "z": 7,
        "b": True, "h": "", "l": 0, "t": 0,
        "from_lightningmaps_org": True,
        "p": [round(max_lat, 1), round(max_lon, 1), round(min_lat, 1), round(min_lon, 1)],
        "r": "A",
    }


# ── Message parsing ───────────────────────────────────────────────────────────

def decode_blitzortung_payload(payload: bytes | str) -> bytes:
    """Decode LZW-like WebSocket payload back into JSON bytes."""
    text = payload.decode() if isinstance(payload, bytes) else payload
    chars = list(text)
    if not chars:
        return b""

    dictionary: dict[int, str] = {}
    current = previous = chars[0]
    output = [current]
    next_code = 256

    for ch in chars[1:]:
        raw = ord(ch)
        value = ch if raw < 256 else dictionary.get(raw) or (previous + current)
        output.append(value)
        current = value[0]
        dictionary[next_code] = previous + current
        next_code += 1
        previous = value

    return "".join(output).encode()


def parse_message(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, bytes):
        candidates = [raw]
    elif isinstance(raw, str):
        candidates = [raw.encode("utf-8", errors="ignore")]
    else:
        candidates = [str(raw).encode("utf-8")]

    try:
        candidates.append(decode_blitzortung_payload(candidates[0]))
    except Exception:
        pass

    for candidate in candidates:
        try:
            parsed = json.loads(candidate.decode("utf-8"))
        except Exception:
            continue
        if isinstance(parsed, dict):
            return [parsed]
        if isinstance(parsed, list):
            return [item for item in parsed if isinstance(item, dict)]

    return []


def extract_strikes_from_message(record: dict[str, Any]) -> list[dict[str, Any]]:
    strokes = record.get("strokes")
    if isinstance(strokes, list):
        return [s for s in strokes if isinstance(s, dict) and {"lat", "lon", "time"} <= s.keys()]
    return []


def extract_strike(record: dict[str, Any]) -> dict[str, Any] | None:
    if {"lat", "lon", "time"} <= record.keys():
        return record
    for key in ("strokes", "strikes", "data", "points", "items"):
        nested = record.get(key)
        if isinstance(nested, list):
            for item in nested:
                if isinstance(item, dict) and {"lat", "lon", "time"} <= item.keys():
                    return item
    return None


def is_heartbeat(record: dict[str, Any]) -> bool:
    return set(record.keys()) == {"time"}


# ── Strike handling ───────────────────────────────────────────────────────────

def format_timestamp(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "unknown"
    seconds = float(value)
    if seconds > 1e15:
        seconds /= 1e9
    elif seconds > 1e12:
        seconds /= 1e3
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()


def build_log_record(strike: dict[str, Any], distance_km: float) -> dict[str, Any]:
    return {
        "logged_at": datetime.now(tz=timezone.utc).isoformat(),
        "strike_time": format_timestamp(strike.get("time")),
        "lat": float(strike["lat"]),
        "lon": float(strike["lon"]),
        "distance_km": round(distance_km, 2),
        "polarity": strike.get("pol"),
        "altitude_m": strike.get("alt"),
        "source_id": strike.get("id"),
    }


def append_to_log(location: dict[str, Any], strike: dict[str, Any], distance_km: float) -> None:
    path = location["output_file"]
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    record = build_log_record(strike, distance_km)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=True) + "\n")


def process_strike(locations: list[dict[str, Any]], strike: dict[str, Any]) -> None:
    lat = float(strike["lat"])
    lon = float(strike["lon"])
    timestamp = format_timestamp(strike.get("time"))

    print(f"[ALL]    time={timestamp} lat={lat:.5f} lon={lon:.5f}", flush=True)

    for loc in locations:
        distance_km = haversine_km(loc["lat"], loc["lon"], lat, lon)
        if distance_km > loc["radius_km"]:
            continue

        print(
            f"[STRIKE] {loc['name']} time={timestamp} "
            f"lat={lat:.5f} lon={lon:.5f} distance_km={distance_km:.2f}",
            flush=True,
        )
        append_to_log(loc, strike, distance_km)

        # === on_strike: ===
        # Available variables:
        #   loc          — {"name": ..., "lat": ..., "lon": ..., "radius_km": ..., "output_file": ...}
        #   strike       — raw strike data with lat/lon/time/pol/alt
        #   distance_km  — distance from the location center in kilometers


# ── WebSocket loop ────────────────────────────────────────────────────────────

async def consume_stream(locations: list[dict[str, Any]], stop_event: asyncio.Event) -> None:
    import websockets
    from websockets.exceptions import ConnectionClosed

    handshake = build_combined_handshake(locations)
    seen: set[tuple[Any, Any, Any]] = set()

    while not stop_event.is_set():
        try:
            async with websockets.connect(WS_URL, ping_interval=20) as ws:
                await ws.send(json.dumps(handshake))
                print(f"Connected to {WS_URL}", flush=True)

                async for raw in ws:
                    if isinstance(raw, str):
                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError:
                            msg = None
                    else:
                        msg = None

                    # LightningMaps challenge–response
                    if isinstance(msg, dict) and "k" in msg:
                        reply = {"k": ((msg["k"] * 3604) % 7081 * time.time() * 10)}
                        await ws.send(json.dumps(reply))
                        continue

                    # Primary path: LightningMaps strokes packet
                    strikes = []
                    if isinstance(msg, dict) and "strokes" in msg:
                        strikes = extract_strikes_from_message(msg)

                    # Fallback path: Blitzortung or other format
                    if not strikes:
                        for record in parse_message(raw):
                            if is_heartbeat(record):
                                continue
                            s = extract_strike(record)
                            if s:
                                strikes.append(s)

                    for strike in strikes:
                        sig = (
                            strike.get("time"),
                            round(float(strike.get("lat", 0)), 5),
                            round(float(strike.get("lon", 0)), 5),
                        )
                        if sig in seen:
                            continue
                        seen.add(sig)
                        if len(seen) > 20000:
                            seen.clear()
                        try:
                            process_strike(locations, strike)
                        except (TypeError, ValueError, KeyError):
                            pass

                    if stop_event.is_set():
                        break

        except ConnectionClosed as exc:
            print(f"WebSocket closed: {exc}", file=sys.stderr, flush=True)
        except OSError as exc:
            print(f"Connection error: {exc}", file=sys.stderr, flush=True)
        except Exception as exc:
            print(f"Unexpected error: {exc}", file=sys.stderr, flush=True)

        if not stop_event.is_set():
            await asyncio.sleep(RECONNECT_DELAY)


# ── Entry point ───────────────────────────────────────────────────────────────

async def async_main() -> int:
    locations = load_config()
    write_locations_json(locations)

    print(f"Loaded {len(locations)} location(s):", flush=True)
    for loc in locations:
        print(f"  {loc['name']}: ({loc['lat']}, {loc['lon']}) r={loc['radius_km']} km → {loc['output_file']}", flush=True)

    stop_event = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    await consume_stream(locations, stop_event)
    return 0


def main() -> int:
    try:
        return asyncio.run(async_main())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
