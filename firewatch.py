#!/usr/bin/env python3
"""Firewatch — live fire hotspot monitor for Termux.

Live data from NASA FIRMS (VIIRS/MODIS). Get a free API key at:
  https://earthdata.nasa.gov  ->  register ->  request FIRMS API key

Usage:
  python firewatch.py                  # default area (Athens)
  python firewatch.py --area thessaloniki
  python firewatch.py --lat 32.08 --lon 34.78 --radius 80
  python firewatch.py --zip 10563              # by postal code (Greece)
  python firewatch.py --zip 75001 --country France
  python firewatch.py --gps                   # device GPS via termux-api
  python firewatch.py --source all     # merge SNPP + NOAA-20 + NOAA-21 + MODIS
  python firewatch.py --once           # single snapshot, no TUI (cron-friendly)
  python firewatch.py --demo           # offline demo with sample data

Keys: q quit · r refresh now · c cycle area preset · z postal code · g GPS
      · h heading mode · [ / ] adjust offset · + / - radius · f confidence
Env:  FIRMS_API_KEY (or pass --key)
"""

import argparse
import csv
import datetime as dt
import json
import math
import os
import queue
import re
import select
import sys
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

try:
    from rich import box
    from rich.align import Align
    from rich.console import Console, Group
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
except ImportError:
    print("Missing dependency: pip install rich", file=sys.stderr)
    sys.exit(1)

CONFIG_PATH = Path.home() / ".config" / "firewatch.json"
API = "https://firms.modaps.eosdis.nasa.gov/api/area/csv/{key}/{src}/{west},{south},{east},{north}/{days}"

DEFAULT_KEY = "82b260ac3b61b77e4b4215e94bbe4a43"  # falls back when FIRMS_API_KEY unset

PRESETS = {
    "athens": (37.9838, 23.7275),
    "thessaloniki": (40.6401, 22.9444),
    "patras": (38.2466, 21.7346),
    "heraklion": (35.3387, 25.1442),
    "rhodes": (36.4349, 28.2176),
    "korinthos": (37.9380, 22.9326),
}
SOURCES = {"viirs": "VIIRS_SNPP_NRT", "noaa20": "VIIRS_NOAA20_NRT",
           "noaa21": "VIIRS_NOAA21_NRT", "modis": "MODIS_NRT",
           "all": "ALL"}
# "all" merges detections from every NRT satellite for cross-checking
MULTI_SRCS = ["VIIRS_SNPP_NRT", "VIIRS_NOAA20_NRT", "VIIRS_NOAA21_NRT", "MODIS_NRT"]
MAX_RADIUS = {"VIIRS_SNPP_NRT": 500, "VIIRS_NOAA20_NRT": 500, "VIIRS_NOAA21_NRT": 500,
              "MODIS_NRT": 2000, "ALL": 500}
MIN_RADIUS = 5
ZOOM = 1.4  # zoom factor per + / - press
WARN_KM = 10.0
MATCH_KM = 1.5  # snapshot compare / merge radius
FRP_EPS = 0.5  # MW threshold for up/down vs same
SATS = {"N": "SN", "NPP": "SN", "N20": "N20", "NOAA-20": "N20",
        "N21": "N21", "NOAA-21": "N21", "T": "MOD", "Terra": "MOD",
        "A": "MOD", "Aqua": "MOD"}

# (lat, lon, bright, acq_date, acq_time, satellite, instrument, confidence, frp, daynight)
DEMO_FIRES = [
    (31.95, 34.90, 345.2, "2026-08-10", "1130", "NPP", "VIIRS", 82, 3.4, "D"),
    (32.20, 34.60, 311.0, "2026-08-10", "1035", "NPP", "VIIRS", 64, 1.2, "D"),
    (32.10, 34.75, 355.8, "2026-08-10", "0830", "NPP", "VIIRS", 98, 6.1, "D"),
    (31.85, 34.95, 290.4, "2026-08-10", "0935", "NOAA-21", "VIIRS", 40, 0.7, "D"),
    (32.30, 34.85, 302.7, "2026-08-09", "2315", "NPP", "VIIRS", "h", 1.8, "N"),
]

console = Console()


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


def parse_acq(acq_date, acq_time):
    """Parse FIRMS acq_date + acq_time (HHMM, may lack leading zeros)."""
    try:
        m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", str(acq_date or ""))
        digits = re.sub(r"\D", "", str("" if acq_time is None else acq_time))
        if not m or not digits:
            return None
        digits = digits.zfill(4)
        hh, mm = int(digits[:2]), int(digits[2:4])
        if hh > 23 or mm > 59:
            return None
        return dt.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                           hh, mm, tzinfo=dt.timezone.utc)
    except (ValueError, TypeError):
        return None


def age_minutes(acq_date, acq_time):
    acq = parse_acq(acq_date, acq_time)
    if acq is None:
        return -1
    return max(0, int((dt.datetime.now(dt.timezone.utc) - acq).total_seconds() / 60))


def local_str(acq_date, acq_time):
    acq = parse_acq(acq_date, acq_time)
    if acq is None:
        return "--:--"
    return acq.astimezone().strftime("%H:%M")


def load_config():
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_config(cfg):
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    except OSError:
        pass


def bbox(lat, lon, radius):
    dlat = radius / 111.32
    dlon = radius / (111.32 * math.cos(math.radians(lat)))
    return lon - dlon, lat - dlat, lon + dlon, lat + dlat


def fetch_fires(key, src, lat, lon, radius, days, min_conf=1):
    """Fetch FIRMS CSV for one source (or all, merged) and return parsed fires."""
    west, south, east, north = bbox(lat, lon, radius)

    def fetch_one(one_src):
        url = API.format(key=key, src=one_src, west=west, south=south,
                         east=east, north=north, days=days)
        req = urllib.request.Request(url, headers={"User-Agent": "firewatch/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return parse_csv(resp.read().decode("utf-8"), lat, lon, min_conf)

    if src == "ALL":
        with ThreadPoolExecutor(max_workers=len(MULTI_SRCS)) as ex:
            lists = list(ex.map(fetch_one, MULTI_SRCS))
        return merge_fires([f for sub in lists for f in sub])
    return fetch_one(src)


def merge_fires(fires, km=1.5):
    """Cluster nearby detections — the same fire seen by several satellites —
    keeping the strongest reading and tracking which satellites saw it."""
    merged = []
    for f in sorted(fires, key=lambda x: x["frp"], reverse=True):
        for m in merged:
            if haversine_km(f["lat"], f["lon"], m["lat"], m["lon"]) <= km:
                m["sats"].add(f["sat"])
                break
        else:
            f["sats"] = {f["sat"]}
            merged.append(f)
    return merged


def same_scene(a, b):
    """True when two snapshots share area/source/conf so FRP compare is valid."""
    if not a or not b:
        return False
    return (a.get("radius") == b.get("radius")
            and a.get("src") == b.get("src")
            and (a.get("conf") if a.get("conf") is not None else 1)
            == (b.get("conf") if b.get("conf") is not None else 1)
            and abs((a.get("lat") or 0) - (b.get("lat") or 0)) < 0.02
            and abs((a.get("lon") or 0) - (b.get("lon") or 0)) < 0.02)


def diff_fires(prev, curr):
    """Match fires within MATCH_KM; attach trend/d_frp/pct like the web app."""
    used = set()
    out = []
    for e in curr:
        best_i, best_d = -1, MATCH_KM
        for i, p in enumerate(prev):
            if i in used:
                continue
            d = haversine_km(e["lat"], e["lon"], p["lat"], p["lon"])
            if d <= best_d:
                best_d, best_i = d, i
        nf = dict(e)
        if best_i < 0:
            nf.update(trend="new", d_frp=0.0, d_bright=0.0, pct=None)
            out.append(nf)
            continue
        used.add(best_i)
        p = prev[best_i]
        d_frp = e["frp"] - p["frp"]
        d_bright = e["bright"] - p["bright"]
        pct = (d_frp / p["frp"] * 100.0) if p["frp"] >= 0.05 else None
        if d_frp > FRP_EPS:
            trend = "up"
        elif d_frp < -FRP_EPS:
            trend = "down"
        else:
            trend = "same"
        nf.update(trend=trend, d_frp=d_frp, d_bright=d_bright, pct=pct)
        out.append(nf)
    return out


def trend_mark(f):
    t = f.get("trend")
    if t == "new":
        return " +"
    if t not in ("up", "down"):
        return ""
    arrow = "↑" if t == "up" else "↓"
    pct = f.get("pct")
    if pct is None or not math.isfinite(pct):
        return f" {arrow}"
    return f" {arrow}{min(999, abs(round(pct)))}%"


def sats_str(f):
    sats = f.get("sats") or {f.get("sat", "?")}
    return ",".join(sorted({SATS.get(s, s[:3]) for s in sats}))


def src_label(src):
    if src == "ALL":
        return "4 satellites (VIIRS+MODIS)"
    return src.replace("_NRT", "")


_ADMIN_PREFIX = re.compile(
    r"^(?:Δήμος|Περιφερειακή Ενότητα|Δημοτική Ενότητα|Περιφέρεια|"
    r"Municipality of|City of|Region of|District of|County of)\s+")


def place_name(place):
    """Short human place name from a Nominatim result: town/city if available,
    otherwise municipality/county with Greek/English admin prefixes stripped."""
    a = place.get("address") or {}
    for key in ("city", "town", "village", "municipality", "county", "state"):
        v = a.get(key)
        if v:
            n = _ADMIN_PREFIX.sub("", v).strip()
            if n:
                return n
    parts = [p.strip() for p in place.get("display_name", "").split(",") if p.strip()]
    return parts[1] if len(parts) > 1 else (place.get("name") or "custom")


def geocode_zip(code, country):
    qs = urllib.parse.urlencode({
        "format": "json", "postalcode": code, "country": country, "limit": 1,
        "addressdetails": 1,
    })
    req = urllib.request.Request(
        f"https://nominatim.openstreetmap.org/search?{qs}",
        headers={"User-Agent": "firewatch/1.0 (Termux)"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if not data:
        raise ValueError(f"no place found for postal code {code} in {country}")
    place = data[0]
    return float(place["lat"]), float(place["lon"]), place_name(place)


GEO_CACHE_PATH = Path.home() / ".config" / "firewatch-geo.json"
geo_places = {}
geo_queue = queue.Queue()
geo_seen = set()


def load_geo_cache():
    try:
        return json.loads(GEO_CACHE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


geo_places = load_geo_cache()


def save_geo_cache():
    try:
        GEO_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        GEO_CACHE_PATH.write_text(json.dumps(geo_places, ensure_ascii=False, indent=1))
    except OSError:
        pass


def geocode_reverse(lat, lon):
    """Nearest place name for fire coordinates, e.g. 'Αμφίπολη ·62052'."""
    qs = urllib.parse.urlencode({"lat": f"{lat:.5f}", "lon": f"{lon:.5f}",
                                 "format": "jsonv2", "addressdetails": "1", "zoom": 14})
    req = urllib.request.Request(
        f"https://nominatim.openstreetmap.org/reverse?{qs}",
        headers={"User-Agent": "firewatch/1.0 (Termux)"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        place = json.loads(resp.read().decode("utf-8"))
    name = place_name(place) or f"{lat:.2f}, {lon:.2f}"
    code = (place.get("address") or {}).get("postcode")
    return f"{name} ·{code}" if code else name


def fire_place_key(f):
    return f"{f['lat']:.3f},{f['lon']:.3f}"


def near_str(f):
    return geo_places.get(fire_place_key(f), "…")


def queue_fire_places(fires):
    for f in fires:
        key = fire_place_key(f)
        if key not in geo_places and key not in geo_seen:
            geo_seen.add(key)
            geo_queue.put(key)


def geo_worker():
    while True:
        key = geo_queue.get()
        if key not in geo_places:
            lat, lon = (float(v) for v in key.split(","))
            try:
                geo_places[key] = geocode_reverse(lat, lon)
            except Exception:
                geo_places[key] = f"{lat:.2f}, {lon:.2f}"
            save_geo_cache()
        time.sleep(1.1)  # Nominatim: max ~1 request per second


def gps_location(timeout=30):
    """Device GPS via termux-api. Returns (lat, lon, accuracy_m)."""
    import shutil
    import subprocess
    if not shutil.which("termux-location"):
        raise ValueError(
            "termux-location not found — run: pkg install termux-api  "
            "(and install the Termux:API app from F-Droid)")
    last = "unknown"
    for extra, tmo in ((["-p", "gps"], timeout), (["-p", "network"], 15), ([], 10)):
        try:
            out = subprocess.run(["termux-location", *extra], capture_output=True,
                                 text=True, timeout=tmo)
        except subprocess.TimeoutExpired:
            last = "GPS fix timed out"
            continue
        if out.returncode != 0:
            last = out.stderr.strip() or "provider error"
            continue
        try:
            data = json.loads(out.stdout)
        except json.JSONDecodeError:
            last = "unexpected output"
            continue
        if data.get("latitude") is not None and data.get("longitude") is not None:
            return (float(data["latitude"]), float(data["longitude"]),
                    float(data.get("accuracy") or 0))
        last = "no coordinates"
    raise ValueError(f"no GPS fix ({last}) — grant location permission to Termux")


def gps_name(accuracy, lat=None, lon=None):
    """Build GPS area label, reverse-geocoding if coordinates given."""
    if lat is not None and lon is not None:
        try:
            place = geocode_reverse(lat, lon)
            if accuracy:
                return f"{place} (gps ±{accuracy:.0f}m)"
            return f"{place} (gps)"
        except Exception:
            pass
    return f"gps (\u00b1{accuracy:.0f} m)" if accuracy else "gps"


def get_heading(timeout=3):
    """Read phone compass azimuth (0-360°, north=0) via termux-sensor.
    Returns (azimuth_degrees, error_msg_or_None)."""
    import shutil
    import subprocess
    if not shutil.which("termux-sensor"):
        return None, "install termux-api: pkg install termux-api"
    # Sensor names match termux-sensor -l output (UPPERCASE with underscores)
    sensors = ["ORIENTATION", "ROTATION_VECTOR",
               "GAME_ROTATION_VECTOR", "GEOMAGNETIC_ROTATION_VECTOR"]
    for sensor in sensors:
        try:
            out = subprocess.run(
                ["termux-sensor", "-s", sensor, "-n", "1"],
                capture_output=True, text=True, timeout=timeout)
            if out.returncode != 0:
                continue
            data = json.loads(out.stdout)
            vals = data.get(sensor, {}).get("values", [])
            if vals and len(vals) >= 1 and vals[0] is not None:
                if "ROTATION" in sensor or "GEOMAGNETIC" in sensor:
                    # Quaternion [x, y, z, w] → azimuth
                    x, y, z, w = float(vals[0]), float(vals[1]), float(vals[2]), float(vals[3])
                    sin = 2.0 * (w * z + x * y)
                    cos = 1.0 - 2.0 * (y * y + z * z)
                    azimuth = math.degrees(math.atan2(sin, cos)) % 360
                else:
                    # ORIENTATION returns [azimuth, pitch, roll]
                    azimuth = float(vals[0]) % 360
                return azimuth, None
        except (subprocess.TimeoutExpired, json.JSONDecodeError,
                ValueError, OSError, IndexError):
            continue
    return None, "no orientation sensor available"


def heading_compass(deg):
    """8-point compass from heading."""
    return ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][
        int((deg + 22.5) // 45) % 8]


def relative_bearing(true_bearing, heading, offset=0):
    """Fire bearing relative to phone heading (0° = ahead)."""
    h = (heading + offset) % 360
    return (true_bearing - h + 360) % 360


def relative_dir(rel_deg):
    """Relative direction arrow from bearing (0° = ahead)."""
    if rel_deg < 22.5 or rel_deg >= 337.5:
        return "↑ ahead"
    if rel_deg < 67.5:
        return "↗ R"
    if rel_deg < 112.5:
        return "→ R"
    if rel_deg < 157.5:
        return "↘ R"
    if rel_deg < 202.5:
        return "↓ back"
    if rel_deg < 247.5:
        return "↙ L"
    if rel_deg < 292.5:
        return "← L"
    return "↖ L"


def compass_bar(heading, offset=0):
    """One-line compass with phone heading highlighted (Rich markup string)."""
    h = (heading + offset) % 360
    marks = [" N ", "NE ", " E ", "SE ", " S ", "SW ", " W ", "NW "]
    idx = int((h + 22.5) // 45) % 8
    parts = []
    for i, m in enumerate(marks):
        if i == idx:
            parts.append(f"[bold reverse]{m}[/]")
        else:
            parts.append(f"[dim]{m}[/]")
    return "".join(parts)


def parse_csv(text, center_lat, center_lon, min_conf=1):
    rows = list(csv.DictReader(text.splitlines()))
    fires = []
    need = 1 if min_conf is None else min_conf
    for r in rows:
        try:
            lat, lon = float(r["latitude"]), float(r["longitude"])
        except (KeyError, ValueError):
            continue
        raw_conf = r.get("confidence", "")
        if conf_value(raw_conf) < need:
            continue
        brightness = r.get("bright_ti4") or r.get("brightness") or "0"
        fires.append({
            "lat": lat,
            "lon": lon,
            "bright": float(brightness),
            "frp": float(r.get("frp", 0) or 0),
            "conf": raw_conf,
            "sat": r.get("satellite", ""),
            "instrument": r.get("instrument", ""),
            "daynight": r.get("daynight", ""),
            "acq_date": r.get("acq_date", ""),
            "acq_time": r.get("acq_time", "0000"),
            "dist": haversine_km(center_lat, center_lon, lat, lon),
            "bearing": bearing_deg(center_lat, center_lon, lat, lon),
        })
    return fires


def demo_fires(center_lat, center_lon, min_conf=1):
    need = 1 if min_conf is None else min_conf
    out = []
    for lat, lon, bright, d, t, sat, inst, conf, frp, dn in DEMO_FIRES:
        if conf_value(conf) < need:
            continue
        out.append({
            "lat": lat, "lon": lon, "bright": bright, "frp": frp, "conf": conf,
            "sat": sat, "instrument": inst, "daynight": dn,
            "acq_date": d, "acq_time": t,
            "dist": haversine_km(center_lat, center_lon, lat, lon),
            "bearing": bearing_deg(center_lat, center_lon, lat, lon),
        })
    return out


def friendly_age(age_min):
    if age_min < 0:
        return "unknown"
    if age_min < 60:
        return f"{age_min} min"
    if age_min < 24 * 60:
        h = age_min // 60
        return f"{h} hr" + ("s" if h > 1 else "")
    return f"{age_min // 1440} day" + ("s" if age_min >= 2880 else "")


def friendly_dir(deg):
    return ["north", "northeast", "east", "southeast", "south",
            "southwest", "west", "northwest"][int((deg + 22.5) // 45) % 8]


def conf_value(c):
    """Map FIRMS confidence (numeric or l/n/h) to 0-100 for filtering."""
    if c is None or c == "":
        return 100
    s = str(c).strip().lower()
    if s in ("l", "low"):
        return 30
    if s in ("n", "nominal"):
        return 50
    if s in ("h", "high"):
        return 80
    try:
        n = float(s)
        return max(0, min(100, n))
    except ValueError:
        return 100


def conf_word(conf):
    try:
        v = float(conf)
    except (TypeError, ValueError):
        c = str(conf).lower()
        return "high" if c.startswith("h") else "low" if c.startswith("l") else "nominal"
    return "high" if v >= 80 else "nominal" if v >= 50 else "low"


def size_label(f):
    if f["frp"] >= 10:
        return "LARGE", "bold red"
    if f["frp"] >= 2 or f["bright"] >= 330:
        return "medium", "yellow"
    return "small", "dim"


def risk_style(f):
    d = f["dist"]
    if d < 5:
        return "bold red"
    if d < WARN_KM:
        return "yellow"
    if d < 30:
        return "cyan"
    return ""


def fire_lines(fires, ctx, expert=False, max_fires=25, heading=None, offset=0):
    """Tabular fire list with aligned columns and headers, closest fire first.
    When heading is set, Dir column shows relative bearing (↑ ahead, etc.)."""
    table = Table(box=box.SIMPLE_HEAD, expand=False, padding=(0, 1),
                  border_style="dim", show_header=True)

    if expert:
        table.add_column("Time", no_wrap=True)
        table.add_column("Place", max_width=22, no_wrap=True)
        table.add_column("Dist", justify="right", no_wrap=True)
        table.add_column("FRP", justify="right", no_wrap=True)
        table.add_column("Bright", justify="right", no_wrap=True)
        table.add_column("Dir", no_wrap=True)
        table.add_column("Conf", no_wrap=True)
        table.add_column("Sat", no_wrap=True)
        table.add_column("D/N", no_wrap=True)
        table.add_column("Age", justify="right", no_wrap=True)
    else:
        table.add_column("Time", no_wrap=True)
        table.add_column("Place", max_width=20, no_wrap=True)
        table.add_column("Dist", justify="right", no_wrap=True)
        table.add_column("Intensity", no_wrap=True)
        table.add_column("Dir", no_wrap=True)
        table.add_column("Detail", no_wrap=True)
        table.add_column("Age", justify="right", no_wrap=True)

    for i, f in enumerate(sorted(fires, key=lambda f: f["dist"])[:max_fires]):
        age = age_minutes(f["acq_date"], f["acq_time"])
        size, size_style = size_label(f)
        cw = conf_word(f["conf"])
        conf_style = "green" if cw == "high" else "yellow" if cw == "nominal" else "dim"
        near = near_str(f)
        sats = f.get("sats") or {f.get("sat", "?")}
        risk = risk_style(f)
        main = ("bold " if i < 3 else "") + risk

        time_cell = Text(local_str(f["acq_date"], f["acq_time"]), style=main)
        place_cell = Text(near, style=main + (" dim" if near == "…" else ""))
        dist_cell = Text(f"{f['dist']:.1f} km", style=main)

        if heading is not None:
            rel = relative_bearing(f["bearing"], heading, offset)
            dir_str = relative_dir(rel)
            dir_style = "bold yellow" if rel < 45 or rel >= 315 else main
        else:
            dir_str = compass(f["bearing"])
            dir_style = main

        mark = trend_mark(f)
        if expert:
            frp_cell = Text(f"{f['frp']:.1f}{mark}", style=main)
            bright_cell = Text(f"{f['bright']:.0f} K", style=main)
            if f.get("trend") not in (None, "new", "same") and f.get("d_bright"):
                db = round(f["d_bright"])
                if db:
                    bright_cell.append(f" {db:+d}", style="dim")
            dir_cell = Text(dir_str, style=dir_style)
            conf_cell = Text(f"{cw} {f['conf']}", style=conf_style)
            sat_cell = Text(sats_str(f),
                            style="cyan" if len(sats) > 1 else main)
            dn_cell = Text(f["daynight"] or "--", style=main)
            age_str = f"{age}m" if age >= 0 else "?"
            age_cell = Text(age_str, style=main)
            table.add_row(time_cell, place_cell, dist_cell, frp_cell,
                          bright_cell, dir_cell, conf_cell, sat_cell,
                          dn_cell, age_cell)
        else:
            intensity = Text()
            intensity.append(size, style=size_style)
            intensity.append(f" {f['frp']:.1f}MW{mark}", style=main)
            dir_cell = Text(dir_str, style=dir_style)
            detail = Text()
            detail.append(cw, style=conf_style)
            detail.append(" ")
            detail.append(sats_str(f),
                          style="cyan" if len(sats) > 1 else None)
            detail.append(" ")
            detail.append(f["daynight"] or "--")
            # compact age: "21h" / "5m" / "2d"
            if age < 0:
                age_str = "?"
            elif age < 60:
                age_str = f"{age}m"
            elif age < 1440:
                age_str = f"{age // 60}h"
            else:
                age_str = f"{age // 1440}d"
            age_cell = Text(age_str, style=main)
            table.add_row(time_cell, place_cell, dist_cell, intensity,
                          dir_cell, detail, age_cell)

    return table


def status_text(fires, ctx, nearest):
    if not fires:
        return (f"[green]✅ All clear[/] — no fires detected within "
                f"[bold]{ctx['radius']:.0f} km[/] of [bold]{ctx['area']}[/]")
    age = age_minutes(nearest["acq_date"], nearest["acq_time"])
    return (f"[bold red]🔥 {len(fires)} fire{'s' if len(fires) > 1 else ''} detected[/] "
            f"within [bold]{ctx['radius']:.0f} km[/] of [bold]{ctx['area']}[/] — "
            f"closest about [bold]{nearest['dist']:.1f} km[/] to the "
            f"{friendly_dir(nearest['bearing'])}, detected {friendly_age(age)} ago")


def risk_gauge(fires):
    """One-line threat gauge from the closest fire's distance and intensity."""
    nearest = min((f for f in fires), key=lambda f: f["dist"], default=None)
    if nearest is None:
        return "[green]risk: ● calm — no active fires[/green]"
    d = nearest["dist"]
    big = nearest["frp"] >= 10
    if d < 5 or (d < WARN_KM and big):
        word, style, frac = "🔥 DANGER", "bold red", 1.0
    elif d < WARN_KM:
        word, style, frac = "⚠ close", "yellow", 0.7
    elif d < 30:
        word, style, frac = "● watch", "cyan", 0.4
    else:
        word, style, frac = "● calm", "green", 0.2
    bar = "█" * round(frac * 10) + "░" * (10 - round(frac * 10))
    return f"[{style}]risk: {bar} {word}[/{style}]"


def mini_map(fires, ctx, width=None, height=None):
    """Braille heat-map: 2×4 dots per character for smooth circles.
    Background color maps fire intensity (red→orange→yellow).
    Adapts to terminal size on each render."""
    if width is None:
        width = max(30, min(console.width - 6, 70))
    if height is None:
        height = max(6, min(20, (console.height - 22) // 2))
    cx, cy = ctx["lat"], ctx["lon"]
    r = ctx["radius"]
    dlat = r / 111.32
    dlon = r / (111.32 * math.cos(math.radians(cx)))
    cos_clat = math.cos(math.radians(cx))

    dots_h = width * 2    # each braille char = 2 horizontal dots
    dots_v = height * 4   # each braille char = 4 vertical dots
    step_lat = 2 * dlat / max(dots_v - 1, 1)
    step_lon = 2 * dlon / max(dots_h - 1, 1)

    # Map fires to dot clusters (small radius for visibility)
    fire_dots = {}
    fire_radius = 3  # dots from center
    for f in fires:
        fcol = int((f["lon"] - (cy - dlon)) / step_lon)
        frow = int(((cx + dlat) - f["lat"]) / step_lat)
        for dr in range(-fire_radius, fire_radius + 1):
            for dc in range(-fire_radius, fire_radius + 1):
                d = math.sqrt(dr * dr + dc * dc)
                if d > fire_radius:
                    continue
                row, col = frow + dr, fcol + dc
                if 0 <= row < dots_v and 0 <= col < dots_h:
                    k = (row, col)
                    if k not in fire_dots or f["frp"] > fire_dots[k]["frp"]:
                        fire_dots[k] = f

    center_row, center_col = dots_v // 2, dots_h // 2
    ring_tol = max(step_lat * 111.32, step_lon * 111.32 * cos_clat) * 1.5

    # Heading arrow marker on the ring
    heading = ctx.get("heading")
    heading_dots = {}
    if heading is not None:
        h_rad = math.radians(heading)
        ring_r = dots_v // 2
        ring_c = dots_h // 2
        h_row = int(center_row - ring_r * math.cos(h_rad))
        h_col = int(center_col + ring_c * math.sin(h_rad))
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                rp, cp = h_row + dr, h_col + dc
                if 0 <= rp < dots_v and 0 <= cp < dots_h:
                    heading_dots[(rp, cp)] = True

    # Braille dot → (row_offset, col_offset) within a 4×2 sub-cell
    DOT_RC = [(0, 0), (1, 0), (2, 0), (0, 1), (1, 1), (2, 1), (3, 0), (3, 1)]

    def fire_bg(f):
        frp = f["frp"]
        if frp >= 10:
            return "on #cc2200"
        if frp >= 5:
            return "on #ee5500"
        if frp >= 2:
            return "on #ee8800"
        return "on #cc9900"

    rows = []
    for char_row in range(height):
        line = Text()
        for char_col in range(width):
            base_r = char_row * 4
            base_c = char_col * 2

            # If this cell covers the center, show a full block marker
            if (base_r <= center_row < base_r + 4 and
                    base_c <= center_col < base_c + 2):
                ch = "\u28ff"  # full braille block
                style = "bold cyan"
                line.append(ch, style=style)
                continue

            bits = 0
            bg = None
            has_fire, has_heading, has_ring = False, False, False

            for di, (dr, dc) in enumerate(DOT_RC):
                ri, ci = base_r + dr, base_c + dc
                lat = cx + dlat - ri * step_lat
                lon = cy - dlon + ci * step_lon

                fd = fire_dots.get((ri, ci))
                if fd:
                    bits |= (1 << di)
                    has_fire = True
                    bg = fire_bg(fd)
                    continue

                if (ri, ci) in heading_dots:
                    bits |= (1 << di)
                    has_heading = True
                    continue

                dx = (lon - cy) * 111.32 * cos_clat
                dy = (lat - cx) * 111.32
                if abs(math.sqrt(dx * dx + dy * dy) - r) <= ring_tol:
                    bits |= (1 << di)

            if bits == 0:
                line.append(" ")
                continue

            ch = chr(0x2800 + bits)
            if has_fire:
                style = f"bold white {bg}"
            elif has_heading:
                style = "bold yellow"
            else:
                style = "dim"
            line.append(ch, style=style)
        rows.append(line)

    legend = Text("", style="dim")
    legend.append("◎ you   ")
    legend.append("⣿", style="bold white on #cc2200")
    legend.append(" large   ")
    legend.append("⣿", style="bold white on #ee8800")
    legend.append(" medium   ")
    legend.append("⣿", style="bold white on #cc9900")
    legend.append(" small   ")
    legend.append("· ring")

    map_content = Align.center(Group(*rows, legend))
    return Panel(map_content, border_style="cyan", expand=True,
                 title=f"scanned area — {r:.0f} km around {ctx['area']}",
                 title_align="left")


def sparkline(counts, width=18):
    """Mini trendline from fire-count history using Unicode blocks."""
    if not counts or len(counts) < 2:
        return ""
    bars = "▁▂▃▄▅▆▇█"
    recent = counts[-width:]
    mn, mx = min(recent), max(recent)
    if mn == mx:
        return bars[0] * len(recent)
    return "".join(bars[min(7, int((v - mn) / (mx - mn) * 7))] for v in recent)


def clock_line(ctx, fires):
    ages = [age_minutes(f["acq_date"], f["acq_time"]) for f in fires]
    ages = [a for a in ages if a >= 0]
    last = f" · last detection {friendly_age(min(ages))} ago" if ages else ""
    nxt = f" · next refresh {ctx['countdown']}s" if ctx.get("countdown") else ""
    return f"[dim]🕐 {ctx.get('clock') or ctx['updated']:%H:%M:%S} · data {ctx['updated']:%H:%M:%S}{last}{nxt}[/dim]"


def snapshot(fires, ctx, error=None, expert=False):
    heading = ctx.get("heading")
    offset = ctx.get("heading_offset", 0)
    nearest = min((f for f in fires), key=lambda f: f["dist"], default=None)
    warn = nearest is not None and nearest["dist"] < WARN_KM
    lines = [status_text(fires, ctx, nearest)]
    if warn:
        age = age_minutes(nearest["acq_date"], nearest["acq_time"])
        lines.insert(0, f"[bold white on red]⚠ CLOSE FIRE — about {nearest['dist']:.1f} km "
                        f"{friendly_dir(nearest['bearing'])}, detected {friendly_age(age)} ago[/]")
    lines.append(risk_gauge(fires))
    lines.append(clock_line(ctx, fires))
    if ctx.get("spark"):
        lines.append(f"[dim]{ctx['spark']}[/]")
    if heading is not None:
        lines.append(f"📱 heading [bold]{heading:.0f}° {heading_compass(heading)}[/]"
                     f"{'  offset ' + str(offset) + '°' if offset else ''}")
        lines.append(compass_bar(heading, offset))
    lines.append(f"[dim]{ctx['area']} · {ctx['lat']:.3f}, {ctx['lon']:.3f} · radius {ctx['radius']:.0f} km · "
                 f"{src_label(ctx['src'])} · checked {ctx['updated']:%H:%M:%S}[/dim]")
    if error:
        lines.append(f"[red]error: {error}[/red]")
    header = Panel("\n".join(lines), border_style="red" if warn else "cyan",
                   title="🔥 FIREWATCH", title_align="left")
    parts = [header]
    parts.append(mini_map(fires, ctx))
    if fires:
        title = f"{len(fires)} hotspot{'s' if len(fires) > 1 else ''} — closest first"
        max_fires = max(3, min(25, console.height - 18))
        parts.append(Panel(fire_lines(fires, ctx, expert, max_fires, heading, offset),
                           border_style="cyan", title=title, title_align="left"))
    return Group(*parts)


def parse_area(args, cfg):
    if args.area and args.area.lower() in PRESETS:
        lat, lon = PRESETS[args.area.lower()]
        return args.area.lower(), lat, lon
    if args.lat is not None and args.lon is not None:
        name = args.area or "custom"
        return name, args.lat, args.lon
    if cfg.get("lat") is not None and cfg.get("lon") is not None:
        return cfg.get("area", "custom"), cfg["lat"], cfg["lon"]
    name = cfg.get("area") or "athens"
    lat, lon = PRESETS.get(name, PRESETS["athens"])
    return name, lat, lon


def apply_zip(args, cfg):
    """Geocode --zip into (lat, lon, name); returns error string or None."""
    if not args.zip:
        return None, None, None, None
    country = args.country or cfg.get("country") or "Greece"
    try:
        lat, lon, name = geocode_zip(args.zip.strip(), country)
    except Exception as e:
        return None, None, None, f"postal code {args.zip} in {country}: {e}"
    return lat, lon, name, None


def run_once(args, cfg):
    key = args.key or os.environ.get("FIRMS_API_KEY") or DEFAULT_KEY
    area, lat, lon = parse_area(args, cfg)
    zlat, zlon, zname, zerr = apply_zip(args, cfg)
    if zerr:
        console.print(f"[red]error: {zerr}[/red]")
        return 1
    if zlat is not None:
        area, lat, lon = zname, zlat, zlon
    elif args.gps:
        try:
            glat, glon, gacc = gps_location()
        except Exception as e:
            console.print(f"[red]error: {e}[/red]")
            return 1
        area = gps_name(gacc, glat, glon); lat, lon = glat, glon
    src_key = args.source or cfg.get("source") or "all"
    if src_key not in SOURCES:
        src_key = "all"
    src = SOURCES[src_key]
    radius = args.radius or cfg.get("radius") or 60
    radius = min(radius, MAX_RADIUS[src])
    min_conf = args.conf if args.conf is not None else cfg.get("conf", 1)
    min_conf = min(100, max(1, int(min_conf) or 1))
    now = dt.datetime.now()
    ctx = dict(area=area, lat=lat, lon=lon, radius=radius, src=src, conf=min_conf,
               updated=now, clock=now)
    error = None
    if args.demo:
        fires = demo_fires(lat, lon, min_conf)
    elif not key:
        fires = []
        error = "No API key. Set FIRMS_API_KEY or pass --key. Get one free at https://earthdata.nasa.gov"
    else:
        try:
            fires = fetch_fires(key, src, lat, lon, radius, args.days, min_conf)
        except Exception as e:
            fires = []
            error = f"{type(e).__name__}: {e}"
    if args.heading:
        h, herr = get_heading()
        if herr:
            error = (error + "; " if error else "") + f"heading: {herr}"
        else:
            ctx["heading"] = h
            ctx["heading_offset"] = args.offset or cfg.get("heading_offset", 0)
    ctx["spark"] = sparkline([len(fires)])
    console.print(snapshot(fires, ctx, error))
    return 1 if error else 0


def run_tui(args, cfg):
    console.clear()
    key = args.key or os.environ.get("FIRMS_API_KEY") or DEFAULT_KEY
    area, lat, lon = parse_area(args, cfg)
    country = args.country or cfg.get("country") or "Greece"
    zlat, zlon, zname, zerr = apply_zip(args, cfg)
    if zlat is not None:
        area, lat, lon = zname, zlat, zlon
    src_key = args.source or cfg.get("source") or "all"
    if src_key not in SOURCES:
        src_key = "all"
    src = SOURCES[src_key]
    radius = min(args.radius or cfg.get("radius") or 60, MAX_RADIUS[src])
    interval = args.interval if args.interval is not None else cfg.get("interval") or 300
    interval = max(60, int(interval) or 300)
    min_conf = args.conf if args.conf is not None else cfg.get("conf", 1)
    min_conf = min(100, max(1, int(min_conf) or 1))

    ctx = dict(area=area, lat=lat, lon=lon, radius=radius, src=src, conf=min_conf,
               updated=dt.datetime.now())
    fires, error = [], None
    countdown = 0
    detail = False
    fire_history = []  # fire counts for sparkline
    prev_snap = None  # last raw snapshot for FRP compare

    # heading / compass state
    heading_active = args.heading or cfg.get("heading", False)
    heading_offset = args.offset or cfg.get("heading_offset", 0)
    heading_val = None
    heading_err = None
    heading_lock = threading.Lock()
    heading_stop = threading.Event()

    def heading_poller():
        nonlocal heading_val, heading_err
        while not heading_stop.is_set():
            h, e = get_heading(timeout=2)
            with heading_lock:
                heading_val = h
                heading_err = e if h is None else None
            time.sleep(1.5)

    def start_heading_thread():
        heading_stop.clear()
        t = threading.Thread(target=heading_poller, daemon=True)
        t.start()
        return t

    heading_thread = None
    if heading_active:
        h, herr = get_heading()
        if herr:
            error = f"heading sensor: {herr}"
            heading_active = False
        else:
            heading_val = h
            heading_thread = start_heading_thread()

    if args.gps and zlat is None:
        try:
            glat, glon, gacc = gps_location()
            area = gps_name(gacc, glat, glon); lat, lon = glat, glon
        except Exception as e:
            error = f"GPS: {e}"

    def fetch_once():
        """Return (fires, error) for the current area/radius/source."""
        try:
            if args.demo:
                return demo_fires(lat, lon, min_conf), None
            if not key:
                return [], "No API key. Set FIRMS_API_KEY or pass --key. Get one free at https://earthdata.nasa.gov"
            return fetch_fires(key, src, lat, lon, radius, args.days, min_conf), None
        except Exception as e:
            return [], f"{type(e).__name__}: {e}"

    fetch_seq = 0

    def start_fetch():
        # fetch in a daemon thread so keypresses never block on the network;
        # results from superseded zoom/area/source presses are dropped
        nonlocal fetch_seq
        fetch_seq += 1
        seq = fetch_seq

        def work():
            nonlocal fires, error, prev_snap
            new_fires, new_err = fetch_once()
            if seq == fetch_seq:
                ctx["updated"] = dt.datetime.now()
                ctx["conf"] = min_conf
                scene = dict(area=area, lat=lat, lon=lon, radius=radius,
                             src=src, conf=min_conf)
                raw = new_fires
                if (not new_err and prev_snap and prev_snap.get("fires")
                        and same_scene(prev_snap, scene)):
                    fires = diff_fires(prev_snap["fires"], raw)
                else:
                    fires = raw
                error = new_err
                if not new_err:
                    prev_snap = dict(scene, fires=raw)
                fire_history.append(len(raw))
                if len(fire_history) > 60:
                    fire_history[:] = fire_history[-60:]
                queue_fire_places(raw)

        threading.Thread(target=work, daemon=True).start()

    threading.Thread(target=geo_worker, daemon=True).start()
    fires, error = fetch_once()
    prev_snap = dict(area=area, lat=lat, lon=lon, radius=radius, src=src,
                    conf=min_conf, fires=list(fires))
    fire_history.append(len(fires))
    queue_fire_places(fires)

    fd = sys.stdin.fileno()
    saved_tty = None
    if sys.stdin.isatty():
        import termios
        import tty
        saved_tty = termios.tcgetattr(fd)
        tty.setcbreak(fd)
    try:
        # screen=True (alternate screen, like htop): full clean redraw every
        # refresh — in-place cursor erase leaves stacked copies when the panel
        # is taller than the terminal (Termux)
        with Live(console=console, auto_refresh=False, screen=True) as live:
            def show():
                with heading_lock:
                    ctx["heading"] = heading_val if heading_active else None
                    ctx["heading_offset"] = heading_offset
                ctx["spark"] = sparkline(fire_history)
                h_tag = ("[bold yellow]h heading[/]"
                         if heading_active else "[cyan]h[/] heading")
                offset_hint = " \\[ \\] adj" if heading_active else ""
                footer = (f"[cyan]q[/] quit · [cyan]r[/] refresh · [cyan]c[/] area · "
                          f"[cyan]z[/] zip · [cyan]g[/] gps · [cyan]s[/] src · "
                          f"[cyan]f[/] conf {min_conf} · "
                          f"[cyan]d[/] {'expert' if detail else 'simple'} · "
                          f"{h_tag}{offset_hint} · "
                          f"[cyan]+[/]/[cyan]-[/] zoom"
                          if key or args.demo else
                          f"[cyan]q[/] quit · [cyan]r[/] refresh · [cyan]c[/] area · "
                          f"[cyan]+[/]/[cyan]-[/] radius   [dim]no API key — data off[/]")
                ctx["countdown"] = max(countdown, 0)
                ctx["clock"] = dt.datetime.now()
                live.update(Panel(snapshot(fires, ctx, error, detail), border_style="cyan",
                                  title=footer, title_align="left"))
                live.refresh()

            def refetch():
                nonlocal countdown
                ctx.update(area=area, lat=lat, lon=lon, radius=radius, src=src,
                           conf=min_conf)
                start_fetch()
                countdown = interval
                show()

            while True:
                countdown -= 1
                show()

                if countdown <= 0:
                    refetch()

                r, _, _ = select.select([fd], [], [], 1)
                if not r:
                    continue
                # raw fd read: sys.stdin.read(1) buffers extra queued bytes,
                # which then never become visible to select() again
                ch = os.read(fd, 1).decode("latin-1", errors="replace")
                if ch in ("q", "\x03"):
                    heading_stop.set()
                    save_config({"area": area, "lat": lat, "lon": lon,
                                 "radius": radius, "source": src_key,
                                 "conf": min_conf, "interval": interval,
                                 "country": country,
                                 "heading": heading_active,
                                 "heading_offset": heading_offset})
                    break
                if ch == "r":
                    refetch()
                if ch == "c":
                    names = list(PRESETS)
                    idx = names.index(area) if area in names else -1
                    area = names[(idx + 1) % len(names)]
                    lat, lon = PRESETS[area]
                    save_config({"area": area, "radius": radius, "source": src_key})
                    refetch()
                if ch == "+":
                    radius = max(round(radius / ZOOM), MIN_RADIUS)
                    refetch()
                if ch == "-":
                    radius = min(round(radius * ZOOM), MAX_RADIUS[src])
                    refetch()
                if ch == "s":
                    keys = list(SOURCES)
                    src_key = keys[(keys.index(src_key) + 1) % len(keys)]
                    src = SOURCES[src_key]
                    radius = min(radius, MAX_RADIUS[src])
                    save_config({"area": area, "radius": radius, "source": src_key})
                    refetch()
                if ch == "d":
                    detail = not detail
                    show()
                if ch == "f":
                    live.stop()
                    if saved_tty is not None:
                        termios.tcsetattr(fd, termios.TCSADRAIN, saved_tty)
                    try:
                        raw = input(f"confidence 1-100 (now {min_conf}): ").strip()
                    except EOFError:
                        raw = ""
                    if saved_tty is not None:
                        tty.setcbreak(fd)
                    live.start()
                    if raw:
                        try:
                            min_conf = min(100, max(1, int(raw)))
                        except ValueError:
                            error = "invalid confidence (use 1-100)"
                            continue
                        ctx["conf"] = min_conf
                        prev_snap = None
                        save_config({"area": area, "lat": lat, "lon": lon,
                                     "radius": radius, "source": src_key,
                                     "conf": min_conf, "interval": interval,
                                     "country": country})
                        refetch()
                if ch == "h":
                    if heading_active:
                        heading_stop.set()
                        heading_active = False
                        with heading_lock:
                            heading_val = None
                    else:
                        h, herr = get_heading()
                        if herr:
                            error = f"heading sensor: {herr}"
                        else:
                            heading_active = True
                            heading_val = h
                            heading_offset = args.offset or cfg.get("heading_offset", 0)
                            heading_thread = start_heading_thread()
                    save_config({"area": area, "lat": lat, "lon": lon,
                                 "radius": radius, "source": src_key,
                                 "heading": heading_active,
                                 "heading_offset": heading_offset})
                    show()
                if ch in ("[", "{") and heading_active:
                    heading_offset = (heading_offset - 5) % 360
                    save_config({"area": area, "lat": lat, "lon": lon,
                                 "radius": radius, "source": src_key,
                                 "heading": heading_active,
                                 "heading_offset": heading_offset})
                    show()
                if ch in ("]", "}") and heading_active:
                    heading_offset = (heading_offset + 5) % 360
                    save_config({"area": area, "lat": lat, "lon": lon,
                                 "radius": radius, "source": src_key,
                                 "heading": heading_active,
                                 "heading_offset": heading_offset})
                    show()
                if ch == "z":
                    live.stop()
                    if saved_tty is not None:
                        termios.tcsetattr(fd, termios.TCSADRAIN, saved_tty)
                    try:
                        raw = input(f"postal code (country: {country}) e.g. 10563 or 75001,France: ").strip()
                    except EOFError:
                        raw = ""
                    if saved_tty is not None:
                        tty.setcbreak(fd)
                    live.start()
                    if not raw:
                        continue
                    code, _, ctry = raw.partition(",")
                    ctry = ctry.strip() or country
                    try:
                        nlat, nlon, nname = geocode_zip(code.strip(), ctry)
                    except Exception as e:
                        error = f"postal code {code} in {ctry}: {e}"
                        continue
                    area, lat, lon, country = nname, nlat, nlon, ctry
                    error = None
                    save_config({"area": area, "lat": lat, "lon": lon, "country": country,
                                 "radius": radius, "source": src_key})
                    refetch()
                if ch == "g":
                    live.stop()
                    if saved_tty is not None:
                        termios.tcsetattr(fd, termios.TCSADRAIN, saved_tty)
                    try:
                        print("acquiring GPS fix (up to ~30 s)...", flush=True)
                        glat, glon, gacc = gps_location()
                        area = gps_name(gacc, glat, glon); lat, lon = glat, glon
                        error = None
                        save_config({"area": area, "lat": lat, "lon": lon,
                                     "radius": radius, "source": src_key})
                    except Exception as e:
                        error = f"GPS: {e}"
                    finally:
                        if saved_tty is not None:
                            tty.setcbreak(fd)
                        live.start()
                    refetch()
    finally:
        heading_stop.set()
        if saved_tty is not None:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved_tty)
        ctx["heading"] = heading_val if heading_active else None
        ctx["heading_offset"] = heading_offset
        console.print(Panel(snapshot(fires, ctx, error, detail), border_style="cyan",
                            title="firewatch — last view", title_align="left"))


def main():
    ap = argparse.ArgumentParser(description="Live fire hotspot monitor (NASA FIRMS)")
    ap.add_argument("--area", help="preset area: " + ", ".join(PRESETS) + " (default from config)")
    ap.add_argument("--lat", type=float, help="custom center latitude")
    ap.add_argument("--lon", type=float, help="custom center longitude")
    ap.add_argument("--zip", help="set area by postal code (e.g. 10563)")
    ap.add_argument("--gps", action="store_true",
                    help="use device GPS (needs termux-api: pkg install termux-api)")
    ap.add_argument("--country", help="country for postal codes (default Greece)")
    ap.add_argument("--radius", type=float, help="radius in km (default 60)")
    ap.add_argument("--source", choices=SOURCES, default=None,
                    help="satellite: viirs, noaa20, noaa21, modis, or all (default: all / config)")
    ap.add_argument("--days", type=int, default=1, help="days of data (1-10)")
    ap.add_argument("--conf", type=int, default=None,
                    help="min confidence 1-100 (default 1 / config); VIIRS l/n/h mapped")
    ap.add_argument("--interval", type=int, default=None,
                    help="refresh seconds (default 300 / config)")
    ap.add_argument("--key", help="FIRMS API key (or env FIRMS_API_KEY)")
    ap.add_argument("--heading", action="store_true",
                    help="use phone compass to orient radar (needs termux-api)")
    ap.add_argument("--offset", type=float, default=0,
                    help="compass calibration offset in degrees (e.g. --offset -5)")
    ap.add_argument("--once", action="store_true", help="print one snapshot and exit")
    ap.add_argument("--demo", action="store_true", help="use bundled sample data (offline)")
    args = ap.parse_args()

    cfg = load_config()
    if args.demo:
        args.key = "demo"
    try:
        sys.exit(run_once(args, cfg) if args.once else run_tui(args, cfg))
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
