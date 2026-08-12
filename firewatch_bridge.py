#!/usr/bin/env python3
"""Bridge: firewatch → Automagic status file.

Runs firewatch --once with GPS, parses the result, and writes a simple
key=value status file to shared storage so Automagic flows can read it.

Usage:
  python firewatch_bridge.py              # use GPS, default radius
  python firewatch_bridge.py --area athens
  python firewatch_bridge.py --demo       # offline demo data
"""

import argparse
import csv
import datetime as dt
import io
import json
import math
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

STATUS_DIR = "/storage/emulated/0/AutoLogs/firewatch"
STATUS_FILE = f"{STATUS_DIR}/status.txt"
STATUS_LINE = f"{STATUS_DIR}/status_line.txt"  # pipe-delimited for Automagic
TRIGGER_FILE = f"{STATUS_DIR}/trigger.txt"

API = "https://firms.modaps.eosdis.nasa.gov/api/area/csv/{key}/{src}/{west},{south},{east},{north}/{days}"
DEFAULT_KEY = "82b260ac3b61b77e4b4215e94bbe4a43"
SOURCES = {"viirs": "VIIRS_SNPP_NRT", "noaa20": "VIIRS_NOAA20_NRT",
           "noaa21": "VIIRS_NOAA21_NRT", "modis": "MODIS_NRT", "all": "ALL"}
WARN_KM = 10.0


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def bearing_deg(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def compass(deg):
    dirs = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    return dirs[int((deg + 22.5) // 45) % 8]


def bbox(lat, lon, radius):
    dlat = radius / 111.32
    dlon = radius / (111.32 * math.cos(math.radians(lat)))
    return lon - dlon, lat - dlat, lon + dlon, lat + dlat


def gps_location(timeout=30):
    try:
        out = subprocess.run(["termux-location"], capture_output=True, text=True,
                             timeout=timeout)
        data = json.loads(out.stdout)
        return data["latitude"], data["longitude"], data.get("accuracy", 0)
    except Exception as e:
        raise RuntimeError(f"GPS failed: {e}")


def fetch_fires(key, src, lat, lon, radius, days):
    west, south, east, north = bbox(lat, lon, radius)
    url = API.format(key=key, src=src, west=west, south=south,
                     east=east, north=north, days=days)
    req = urllib.request.Request(url, headers={"User-Agent": "firewatch-bridge/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8")
    fires = []
    for row in csv.DictReader(io.StringIO(raw)):
        try:
            flat = float(row["latitude"])
            flon = float(row["longitude"])
            frp = float(row.get("frp", 0) or 0)
            dist = haversine_km(lat, lon, flat, flon)
            if dist <= radius:
                fires.append({
                    "lat": flat, "lon": flon, "dist": dist,
                    "bearing": bearing_deg(lat, lon, flat, flon),
                    "frp": frp, "sat": row.get("satellite", "?"),
                    "acq_date": row.get("acq_date", ""),
                    "acq_time": row.get("acq_time", ""),
                })
        except (ValueError, KeyError):
            continue
    fires.sort(key=lambda f: f["dist"])
    return fires


def write_status(fires, error, updated, lat, lon):
    os.makedirs(STATUS_DIR, exist_ok=True)
    nearest = fires[0] if fires else None
    count = len(fires)
    closest_km = f"{nearest['dist']:.1f}" if nearest else "none"
    closest_dir = compass(nearest["bearing"]) if nearest else "-"
    risk = "high" if nearest and nearest["dist"] < 5 else (
           "medium" if nearest and nearest["dist"] < WARN_KM else (
           "low" if fires else "none"))
    ts = updated.strftime("%H:%M:%S")

    lines = [
        f"COUNT={count}",
        f"CLOSEST_KM={closest_km}",
        f"CLOSEST_DIR={closest_dir}",
        f"RISK={risk}",
        f"UPDATED={ts}",
        f"LAT={lat:.4f}",
        f"LON={lon:.4f}",
        f"ERROR={error or ''}",
    ]
    with open(STATUS_FILE, "w") as f:
        f.write("\n".join(lines) + "\n")

    # Pipe-delimited single line for easy Automagic parsing via split()
    # Format: count|closest_km|closest_dir|risk|updated|lat|lon|error|
    pipe = f"{count}|{closest_km}|{closest_dir}|{risk}|{ts}|{lat:.4f}|{lon:.4f}|{error or ''}|"
    with open(STATUS_LINE, "w") as f:
        f.write(pipe + "\n")

    # Also write JSON for richer clients
    detail = {
        "count": count, "closest_km": nearest["dist"] if nearest else None,
        "closest_dir": closest_dir, "risk": risk, "updated": ts,
        "lat": lat, "lon": lon, "error": error,
        "fires": [{"lat": f["lat"], "lon": f["lon"], "dist": round(f["dist"], 1),
                    "dir": compass(f["bearing"]), "frp": f["frp"]}
                  for f in fires[:10]],
    }
    with open(f"{STATUS_DIR}/detail.json", "w") as f:
        json.dump(detail, f, indent=2)


def notify_automagic():
    """Send broadcast to Automagic so it knows fresh data is ready."""
    os.system(
        "am broadcast -a ch.gridvision.ppam.androidautomagic.action.EXECUTE_FLOW "
        "-e flowName 'Firewatch.Update.Widget' >/dev/null 2>&1"
    )


def main():
    ap = argparse.ArgumentParser(description="Firewatch → Automagic bridge")
    ap.add_argument("--area", help="Preset area name")
    ap.add_argument("--lat", type=float, help="Latitude")
    ap.add_argument("--lon", type=float, help="Longitude")
    ap.add_argument("--radius", type=float, default=60, help="Scan radius in km")
    ap.add_argument("--days", type=int, default=2, help="Days of data to fetch")
    ap.add_argument("--demo", action="store_true", help="Use demo data")
    ap.add_argument("--source", default="viirs", choices=SOURCES)
    ap.add_argument("--key", help="FIRMS API key")
    args = ap.parse_args()

    key = args.key or os.environ.get("FIRMS_API_KEY") or DEFAULT_KEY
    src = SOURCES[args.source]
    error = None
    lat, lon = args.lat, args.lon

    if args.demo:
        lat, lon = lat or 37.9838, lon or 23.7275  # Athens for demo
    elif lat is None or lon is None:
        try:
            lat, lon, _ = gps_location()
        except Exception as e:
            # Fallback: last known from status file
            try:
                with open(STATUS_FILE) as f:
                    for line in f:
                        if line.startswith("LAT="):
                            lat = float(line.split("=")[1])
                        if line.startswith("LON="):
                            lon = float(line.split("=")[1])
            except Exception:
                pass
            if lat is None:
                error = f"GPS failed: {e}"
                lat, lon = 37.9838, 23.7275  # Athens fallback

    now = dt.datetime.now()

    if args.demo:
        fires = [
            {"lat": lat + 0.02, "lon": lon + 0.03, "dist": 5.2,
             "bearing": 45, "frp": 3.4, "sat": "NPP",
             "acq_date": now.strftime("%Y-%m-%d"), "acq_time": now.strftime("%H%M")},
            {"lat": lat + 0.05, "lon": lon - 0.02, "dist": 8.7,
             "bearing": 120, "frp": 1.8, "sat": "NOAA-20",
             "acq_date": now.strftime("%Y-%m-%d"), "acq_time": now.strftime("%H%M")},
        ]
    else:
        try:
            fires = fetch_fires(key, src, lat, lon, args.radius, args.days)
        except Exception as e:
            fires = []
            error = f"{type(e).__name__}: {e}"

    write_status(fires, error, now, lat, lon)
    notify_automagic()

    if error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
    print(f"OK: {len(fires)} fires, risk={_risk_from_fires(fires)}")
    sys.exit(0)


def _risk_from_fires(fires):
    if not fires:
        return "none"
    d = fires[0]["dist"]
    if d < 5:
        return "high"
    if d < WARN_KM:
        return "medium"
    return "low"


if __name__ == "__main__":
    main()
