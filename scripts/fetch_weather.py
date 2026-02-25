#!/usr/bin/env python3
"""
Сбор прогнозных данных для метео-триажа XC closed routes.
Версия: 1.0

Модельный стек:
  D-5…D-1 (среднесрок):
    - ECMWF IFS HRES 0.25° (опорная/скелет)
    - ICON Seamless (Open-Meteo blend, расширен до 5 дней)
    - GFS (BL height, CAPE, CIN, LI — единственный с BL height)
    - Ансамбли: ECMWF ENS + ICON-EU EPS → p10/p50/p90/spread

  D-2…D-0 (ближний):
    - ICON-D2 2 km (локальный оверрайд 0–48ч)
    - GeoSphere AROME 2.5 km (хай-рез, полный ряд по окну)
    - DWD MOSMIX (точечный sanity-check, локальное время)

Временная привязка:
  - 13:00 Europe/Berlin с учётом DST (через zoneinfo)
  - Термическое окно 09:00–18:00 local

Слои данных:
  - at_13_local: значения в 13:00 local
  - thermal_window_stats: min/mean/max/head/tail по окну

BrightSky@13 (forecast) — УБРАН.

Использование:
    python scripts/fetch_weather.py
    python scripts/fetch_weather.py --date 2026-03-07
    python scripts/fetch_weather.py --date 2026-03-07 --locations lenggries,koessen
"""

import argparse
import io
import json
import math
import statistics
import subprocess
import shutil
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.parse import urlencode
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

# ══════════════════════════════════════════════
# Constants & Configuration
# ══════════════════════════════════════════════

APP_VERSION = "1.0"
TZ_LOCAL = ZoneInfo("Europe/Berlin")
TZ_UTC = timezone.utc

WINDOW_START_H = 9   # 09:00 local
WINDOW_END_H = 18    # 18:00 local
ANALYSIS_HOURS = [f"{h:02d}:00" for h in range(8, 19)]

LOCATIONS = {
    "lenggries":   {"lat": 47.68, "lon": 11.57, "elev": 700,  "peaks": 1800, "name": "Lenggries",   "geosphere_id": None,    "mosmix_id": "10963", "drive_h": 1.0},
    "wallberg":    {"lat": 47.64, "lon": 11.79, "elev": 1620, "peaks": 1722, "name": "Wallberg",    "geosphere_id": None,    "mosmix_id": "10963", "drive_h": 1.0},
    "koessen":     {"lat": 47.67, "lon": 12.40, "elev": 590,  "peaks": 1900, "name": "Kössen",      "geosphere_id": "11130", "mosmix_id": None,    "drive_h": 1.5},
    "innsbruck":   {"lat": 47.26, "lon": 11.39, "elev": 578,  "peaks": 2600, "name": "Innsbruck",   "geosphere_id": "11121", "mosmix_id": "11120", "drive_h": 2.0},
    "greifenburg": {"lat": 46.75, "lon": 13.18, "elev": 600,  "peaks": 2800, "name": "Greifenburg", "geosphere_id": "11204", "mosmix_id": None,    "drive_h": 4.0},
    "speikboden":  {"lat": 46.90, "lon": 11.87, "elev": 950,  "peaks": 2500, "name": "Speikboden",  "geosphere_id": None,    "mosmix_id": None,    "drive_h": 3.5},
    "bassano":     {"lat": 45.78, "lon": 11.73, "elev": 130,  "peaks": 1700, "name": "Bassano",     "geosphere_id": None,    "mosmix_id": None,    "drive_h": 5.0},
}

# ── Parameter sets per model ──

ECMWF_PARAMS = [
    "temperature_2m", "dewpoint_2m", "relative_humidity_2m",
    "windspeed_10m", "windgusts_10m", "winddirection_10m",
    "cloudcover", "cloudcover_low", "cloudcover_mid", "cloudcover_high",
    "precipitation", "cape",
    "shortwave_radiation", "direct_radiation", "sunshine_duration",
    "temperature_850hPa", "temperature_700hPa",
    "relative_humidity_850hPa", "relative_humidity_700hPa",
    "windspeed_850hPa", "winddirection_850hPa",
    "windspeed_700hPa", "winddirection_700hPa",
]

ICON_PARAMS = list(ECMWF_PARAMS)

ICON_D2_PARAMS = list(ECMWF_PARAMS)

GFS_PARAMS = list(ECMWF_PARAMS) + [
    "boundary_layer_height",
    "convective_inhibition", "lifted_index",
    "temperature_500hPa",
]

ENSEMBLE_PARAMS = [
    "temperature_2m", "windspeed_10m", "windgusts_10m",
    "cloudcover", "precipitation", "cape",
    "windspeed_850hPa",
]

GEOSPHERE_PARAMS = "t2m,cape,cin,tcc,lcc,mcc,hcc,u10m,v10m,ugust,vgust,snowlmt,rr,grad"

MOSMIX_PARAMS_OF_INTEREST = [
    "TTT", "Td", "FF", "FX1", "DD",
    "N", "Neff", "Nh", "Nm", "Nl",
    "PPPP", "SunD1", "Rad1h", "RR1c", "wwP", "R101",
]


# ══════════════════════════════════════════════
# Time Utilities
# ══════════════════════════════════════════════

def _next_saturday() -> str:
    today = datetime.now().date()
    days_ahead = (5 - today.weekday()) % 7
    if days_ahead == 0 and today.weekday() != 5:
        days_ahead = 7
    return str(today + timedelta(days=days_ahead))


def _local_to_utc(date_str: str, hour: int) -> datetime:
    """Convert local hour on date to UTC datetime."""
    from datetime import date as _date
    parts = date_str.split("-")
    d = _date(int(parts[0]), int(parts[1]), int(parts[2]))
    local_dt = datetime(d.year, d.month, d.day, hour, 0, 0, tzinfo=TZ_LOCAL)
    return local_dt.astimezone(TZ_UTC)


def _utc_hour_for_local(date_str: str, local_hour: int) -> str:
    """Return 'HH:MM' UTC corresponding to local_hour on date_str."""
    utc_dt = _local_to_utc(date_str, local_hour)
    return utc_dt.strftime("%H:%M")


# ══════════════════════════════════════════════
# HTTP Utilities
# ══════════════════════════════════════════════

def _fetch_json(url: str, timeout: int = 30) -> dict:
    req = Request(url, headers={"User-Agent": f"PG-Weather-Triage/{APP_VERSION}"})
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _fetch_bytes(url: str, timeout: int = 30) -> bytes:
    req = Request(url, headers={"User-Agent": f"PG-Weather-Triage/{APP_VERSION}"})
    with urlopen(req, timeout=timeout) as resp:
        return resp.read()


# ══════════════════════════════════════════════
# Data Extraction Helpers
# ══════════════════════════════════════════════

def _find_hour_idx(times: list, target_date: str, hour: int,
                   utc_timestamps: bool = False) -> int | None:
    """Find index of a local hour in a time array.

    utc_timestamps=False → times already in Europe/Berlin (Open-Meteo default).
    utc_timestamps=True  → times in UTC (GeoSphere / MOSMIX).
    """
    if utc_timestamps:
        utc_dt = _local_to_utc(target_date, hour)
        needle = utc_dt.strftime("%Y-%m-%dT%H:%M")
    else:
        needle = f"{target_date}T{hour:02d}:00"

    for i, t in enumerate(times):
        if needle in str(t):
            return i
    return None


def _safe_val(hourly: dict, key: str, idx: int):
    vals = hourly.get(key, [])
    return vals[idx] if idx is not None and idx < len(vals) else None


def _extract_at_13_local(hourly: dict, date: str, utc_ts: bool = False) -> dict:
    """Extract all values at 13:00 local from hourly dict."""
    times = hourly.get("time", [])
    idx = _find_hour_idx(times, date, 13, utc_ts)
    if idx is None:
        return {}
    out = {}
    for k, vals in hourly.items():
        if k == "time":
            continue
        out[k] = vals[idx] if idx < len(vals) else None
    return out


def _extract_window_stats(hourly: dict, date: str,
                          utc_ts: bool = False) -> dict:
    """min/mean/max/head/tail/trend over 09:00–18:00 local for each param."""
    times = hourly.get("time", [])
    idxs = []
    for h in range(WINDOW_START_H, WINDOW_END_H + 1):
        idx = _find_hour_idx(times, date, h, utc_ts)
        if idx is not None:
            idxs.append(idx)
    if not idxs:
        return {}

    stats = {}
    for k, vals in hourly.items():
        if k == "time":
            continue
        wv = [vals[i] for i in idxs if i < len(vals) and vals[i] is not None]
        if not wv:
            stats[k] = {"min": None, "mean": None, "max": None, "n": 0}
            continue
        s = {
            "min": round(min(wv), 2),
            "mean": round(statistics.mean(wv), 2),
            "max": round(max(wv), 2),
            "n": len(wv),
            "head": [round(v, 2) for v in wv[:2]],
            "tail": [round(v, 2) for v in wv[-2:]],
        }
        if len(wv) >= 4:
            early = statistics.mean(wv[:2])
            late = statistics.mean(wv[-2:])
            if early == 0:
                s["trend"] = "stable" if late == 0 else "rising"
            elif late > early * 1.3:
                s["trend"] = "rising"
            elif late < early * 0.7:
                s["trend"] = "falling"
            else:
                s["trend"] = "stable"
        stats[k] = s
    return stats


# ══════════════════════════════════════════════
# Open-Meteo: Generic Fetcher
# ══════════════════════════════════════════════

def _fetch_openmeteo(endpoint: str, model: str | None, lat: float,
                     lon: float, date: str, params: list,
                     extra: dict | None = None) -> dict:
    q = {
        "latitude": lat, "longitude": lon,
        "hourly": ",".join(params),
        "start_date": date, "end_date": date,
        "timezone": "Europe/Berlin",
        "windspeed_unit": "ms",
    }
    if model:
        q["models"] = model
    if extra:
        q.update(extra)
    base = f"https://api.open-meteo.com/v1/{endpoint}"
    return _fetch_json(f"{base}?{urlencode(q)}")


# ── Model ID mapping (source_key → exact Open-Meteo model string) ──
MODEL_IDS = {
    "ecmwf_hres": "ecmwf_ifs025",
    "icon_seamless": "icon_seamless",
    "icon_d2": "icon_d2",
    "gfs": "gfs_seamless",
    "ecmwf_ens": "ecmwf_ifs025",
    "icon_eu_eps": "icon_eu",
    "geosphere_arome": "arpege_seamless_2.5km",
    "mosmix": "mosmix_l",
}

MODEL_LABELS = {
    "ecmwf_hres": "ECMWF IFS HRES 0.25°",
    "icon_seamless": "ICON Seamless (Open-Meteo blend)",
    "icon_d2": "ICON-D2 2 km",
    "gfs": "GFS 0.25°",
    "ecmwf_ens": "ECMWF ENS 51-member",
    "icon_eu_eps": "ICON-EU EPS 40-member",
    "geosphere_arome": "GeoSphere AROME 2.5 km",
    "mosmix": "DWD MOSMIX_L",
}

# ── Individual model fetchers ──

def fetch_ecmwf_hres(lat, lon, date):
    """ECMWF IFS HRES 0.25° deterministic — base model D-5…D-1."""
    return _fetch_openmeteo("forecast", "ecmwf_ifs025", lat, lon, date, ECMWF_PARAMS)

def fetch_icon_seamless(lat, lon, date):
    """ICON Seamless (Open-Meteo blend) — D-5…D-1."""
    return _fetch_openmeteo("forecast", "icon_seamless", lat, lon, date, ICON_PARAMS)

def fetch_icon_d2(lat, lon, date):
    """ICON-D2 2 km hi-res — D-2…D-0 local override (0–48h)."""
    return _fetch_openmeteo("dwd-icon", "icon_d2", lat, lon, date, ICON_D2_PARAMS)

def fetch_gfs(lat, lon, date):
    """GFS — единственная модель с BL height, LI, CIN."""
    return _fetch_openmeteo("gfs", "gfs_seamless", lat, lon, date, GFS_PARAMS)


# ── Ensemble fetchers ──

def _fetch_ensemble(model: str, lat: float, lon: float, date: str) -> dict:
    q = {
        "latitude": lat, "longitude": lon,
        "hourly": ",".join(ENSEMBLE_PARAMS),
        "start_date": date, "end_date": date,
        "timezone": "Europe/Berlin",
        "windspeed_unit": "ms",
        "models": model,
    }
    base = "https://ensemble-api.open-meteo.com/v1/ensemble"
    return _fetch_json(f"{base}?{urlencode(q)}")


def _aggregate_ensemble(raw: dict, params: list) -> dict:
    """Aggregate ensemble members → p10/p50/p90/spread per timestep."""
    hourly = raw.get("hourly", {})
    times = hourly.get("time", [])
    result = {"time": times}

    for param in params:
        member_keys = sorted(k for k in hourly if k.startswith(f"{param}_member"))
        if not member_keys:
            continue
        p10, p50, p90, spread = [], [], [], []
        for i in range(len(times)):
            vals = []
            for mk in member_keys:
                v = hourly[mk][i] if i < len(hourly[mk]) else None
                if v is not None:
                    vals.append(v)
            if len(vals) >= 3:
                vals.sort()
                n = len(vals)
                i10 = max(0, int(n * 0.1))
                i90 = min(n - 1, int(n * 0.9))
                p10.append(round(vals[i10], 2))
                p50.append(round(vals[n // 2], 2))
                p90.append(round(vals[i90], 2))
                spread.append(round(vals[i90] - vals[i10], 2))
            else:
                p10.append(None); p50.append(None)
                p90.append(None); spread.append(None)
        result[f"{param}_p10"] = p10
        result[f"{param}_p50"] = p50
        result[f"{param}_p90"] = p90
        result[f"{param}_spread"] = spread
    return result


def fetch_ecmwf_ens(lat, lon, date):
    """ECMWF ENS 51-member → aggregated p10/p50/p90/spread."""
    raw = _fetch_ensemble("ecmwf_ifs025", lat, lon, date)
    return _aggregate_ensemble(raw, ENSEMBLE_PARAMS)


def fetch_icon_eu_eps(lat, lon, date):
    """ICON-EU EPS 40-member → aggregated p10/p50/p90/spread."""
    raw = _fetch_ensemble("icon_eu", lat, lon, date)
    return _aggregate_ensemble(raw, ENSEMBLE_PARAMS)


# ══════════════════════════════════════════════
# GeoSphere AROME 2.5 km
# ══════════════════════════════════════════════

# Map GeoSphere names → standard names
_GEO_MAP = {
    "t2m": "temperature_2m", "cape": "cape", "cin": "convective_inhibition",
    "tcc": "cloudcover", "lcc": "cloudcover_low", "mcc": "cloudcover_mid",
    "hcc": "cloudcover_high", "snowlmt": "snow_line",
    "rr": "precipitation", "grad": "shortwave_radiation",
}


def fetch_geosphere_arome(lat: float, lon: float) -> dict:
    """GeoSphere AROME 2.5 km NWP — full time series (UTC timestamps)."""
    url = (
        f"https://dataset.api.hub.geosphere.at/v1/timeseries/forecast/nwp-v1-1h-2500m"
        f"?parameters={GEOSPHERE_PARAMS}"
        f"&lat_lon={lat},{lon}&output_format=geojson"
    )
    raw = _fetch_json(url, timeout=45)

    timestamps = raw.get("timestamps", [])
    features = raw.get("features", [])
    if not features:
        return {"error": "No features", "hourly": {}}

    params = features[0].get("properties", {}).get("parameters", {})
    hourly = {"time": timestamps}

    # Copy raw params with standard names
    for geo_name, data_obj in params.items():
        std_name = _GEO_MAP.get(geo_name, geo_name)
        hourly[std_name] = data_obj.get("data", [])

    # Compute wind speed/dir from u,v components
    u10 = hourly.get("u10m", [])
    v10 = hourly.get("v10m", [])
    if u10 and v10:
        ws, wd = [], []
        for i in range(len(u10)):
            if u10[i] is not None and v10[i] is not None:
                s, d = wind_from_uv(u10[i], v10[i])
                ws.append(s); wd.append(d)
            else:
                ws.append(None); wd.append(None)
        hourly["windspeed_10m"] = ws
        hourly["winddirection_10m"] = wd

    ugust = hourly.get("ugust", [])
    vgust = hourly.get("vgust", [])
    if ugust and vgust:
        gs = []
        for i in range(len(ugust)):
            if ugust[i] is not None and vgust[i] is not None:
                s, _ = wind_from_uv(ugust[i], vgust[i])
                gs.append(s)
            else:
                gs.append(None)
        hourly["windgusts_10m"] = gs

    return {"hourly": hourly, "utc_timestamps": True}


# ══════════════════════════════════════════════
# DWD MOSMIX (KMZ → XML, local time conversion)
# ══════════════════════════════════════════════

def fetch_mosmix(station_id: str, date: str) -> dict:
    """MOSMIX_L KMZ → extract forecast for date with local time keys."""
    url = (
        f"https://opendata.dwd.de/weather/local_forecasts/mos/MOSMIX_L/"
        f"single_stations/{station_id}/kml/MOSMIX_L_LATEST_{station_id}.kmz"
    )
    raw = _fetch_bytes(url, timeout=60)
    zf = zipfile.ZipFile(io.BytesIO(raw))
    kml_name = [n for n in zf.namelist() if n.endswith(".kml")][0]
    kml_data = zf.read(kml_name)

    ns = {
        "kml": "http://www.opengis.net/kml/2.2",
        "dwd": "https://opendata.dwd.de/weather/lib/pointforecast_dwd_extension_V1_0.xsd",
    }
    root = ET.fromstring(kml_data)

    time_steps = root.findall(".//dwd:ForecastTimeSteps/dwd:TimeStep", ns)
    timestamps_utc = [ts.text for ts in time_steps]

    # Build mapping: local_hour_str → index (for target date)
    local_map = {}   # "HH:MM" → index
    utc_map = {}     # "HH:MM" → index  (kept for backward compat)
    for i, ts in enumerate(timestamps_utc):
        if date not in ts:
            continue
        # ts like "2026-02-28T12:00:00.000Z"
        utc_h = ts.split("T")[1][:5]
        utc_map[utc_h] = i
        # Convert to local
        try:
            utc_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            loc_dt = utc_dt.astimezone(TZ_LOCAL)
            local_h = loc_dt.strftime("%H:%M")
            local_map[local_h] = i
        except Exception:
            pass

    if not local_map:
        return {"error": f"No data for {date}", "station": station_id}

    placemark = root.find(".//kml:Placemark", ns)
    if placemark is None:
        return {"error": "No Placemark in KML", "station": station_id}

    station_name = placemark.findtext("kml:name", default=station_id, namespaces=ns).strip()

    forecasts = placemark.findall(".//dwd:Forecast", ns)
    hourly_local = {}  # param → {local_hour: value}
    for fc in forecasts:
        param_name = fc.get(f"{{{ns['dwd']}}}elementName")
        if param_name not in MOSMIX_PARAMS_OF_INTEREST:
            continue
        value_text = fc.findtext("dwd:value", default="", namespaces=ns)
        values = value_text.split()

        hvals = {}
        for local_h, idx in local_map.items():
            if idx < len(values):
                raw_val = values[idx].strip()
                if raw_val in ("-999.00", "-"):
                    hvals[local_h] = None
                else:
                    try:
                        v = float(raw_val)
                        if param_name in ("TTT", "Td"):
                            v = round(v - 273.15, 1)
                        elif param_name == "PPPP":
                            v = round(v / 100, 1)
                        else:
                            v = round(v, 1)
                        hvals[local_h] = v
                    except ValueError:
                        hvals[local_h] = None
            else:
                hvals[local_h] = None
        hourly_local[param_name] = hvals

    # Extract at_13_local
    at_13 = {}
    for p, hvals in hourly_local.items():
        at_13[p] = hvals.get("13:00")

    return {
        "station": station_id,
        "station_name": station_name,
        "model": "mosmix_l",
        "at_13_local": at_13,
        "hourly_local": hourly_local,
    }


# ══════════════════════════════════════════════
# Computations
# ══════════════════════════════════════════════

def estimate_cloudbase_msl(temp_c, dewpoint_c, elev_m):
    if temp_c is None or dewpoint_c is None:
        return None
    return round(125.0 * (temp_c - dewpoint_c) + elev_m)


def lapse_rate(t850, t700):
    if t850 is None or t700 is None:
        return None
    return round((t850 - t700) / 1.5, 1)


def estimate_wstar(bl_h, sw_rad, temp_c):
    """Deardorff W* [m/s]."""
    if not bl_h or bl_h <= 10 or not sw_rad or sw_rad <= 10 or temp_c is None:
        return None
    T_K = temp_c + 273.15
    if T_K < 200:
        return None
    H_s = 0.4 * sw_rad
    arg = (9.81 / T_K) * bl_h * H_s / (1.1 * 1005.0)
    return round(arg ** (1 / 3), 2) if arg > 0 else 0.0


def wind_from_uv(u, v):
    speed = math.sqrt(u**2 + v**2)
    direction = (270 - math.degrees(math.atan2(v, u))) % 360
    return round(speed, 1), round(direction)


# ══════════════════════════════════════════════
# Hourly Profile (combined best available)
# ══════════════════════════════════════════════

def _get_val(hourly, times, hour_str, key):
    for i, t in enumerate(times):
        if hour_str in str(t):
            vals = hourly.get(key, [])
            return vals[i] if i < len(vals) else None
    return None


# Layer names matching source keys (in priority order)
_LAYER_NAMES = ["icon_d2", "ecmwf_hres", "icon_seamless", "gfs"]


def build_hourly_profile(sources: dict, date: str, loc: dict) -> dict:
    """Combined hourly profile from best available deterministic model.

    Priority: ICON-D2 > ECMWF HRES > ICON Seamless > GFS (for surface/pressure).
    GFS always for BL height, LI, CIN.
    """
    # Collect hourly dicts (already in local TZ from Open-Meteo calls)
    h_d2     = sources.get("icon_d2", {}).get("_hourly_raw", {})
    h_ecmwf  = sources.get("ecmwf_hres", {}).get("_hourly_raw", {})
    h_icon   = sources.get("icon_seamless", {}).get("_hourly_raw", {})
    h_gfs    = sources.get("gfs", {}).get("_hourly_raw", {})

    layers = [
        ("icon_d2",       h_d2,    h_d2.get("time", [])),
        ("ecmwf_hres",    h_ecmwf, h_ecmwf.get("time", [])),
        ("icon_seamless", h_icon,  h_icon.get("time", [])),
        ("gfs",           h_gfs,   h_gfs.get("time", [])),
    ]
    gfs_layer = ("gfs", h_gfs, h_gfs.get("time", []))

    def _pick(key, hour):
        """Get (value, source_key) from first layer that has it."""
        for name, h, t in layers:
            v = _get_val(h, t, hour, key)
            if v is not None:
                return v, name
        return None, None

    def _pick_gfs(key, hour):
        v = _get_val(gfs_layer[1], gfs_layer[2], hour, key)
        return v, "gfs" if v is not None else None

    profile = []
    for hour in ANALYSIS_HOURS:
        src_map = {}  # field → source_key for this hour

        t2m, s   = _pick("temperature_2m", hour);       src_map["temp_2m"] = s
        td, s    = _pick("dewpoint_2m", hour);           src_map["dewpoint"] = s
        cloud, s = _pick("cloudcover", hour);            src_map["cloudcover"] = s
        cl_lo, s = _pick("cloudcover_low", hour);        src_map["cloudcover_low"] = s
        cl_mi, s = _pick("cloudcover_mid", hour);        src_map["cloudcover_mid"] = s
        cl_hi, s = _pick("cloudcover_high", hour);       src_map["cloudcover_high"] = s
        prec, s  = _pick("precipitation", hour);         src_map["precipitation"] = s
        ws10, s  = _pick("windspeed_10m", hour);         src_map["wind_10m"] = s
        gust, s  = _pick("windgusts_10m", hour);         src_map["gusts"] = s
        ws850, s = _pick("windspeed_850hPa", hour);      src_map["wind_850"] = s
        ws700, s = _pick("windspeed_700hPa", hour);      src_map["wind_700"] = s
        t850, s  = _pick("temperature_850hPa", hour);    src_map["t850"] = s
        t700, s  = _pick("temperature_700hPa", hour);    src_map["t700"] = s
        rh850, s = _pick("relative_humidity_850hPa", hour); src_map["rh_850"] = s
        rh700, s = _pick("relative_humidity_700hPa", hour); src_map["rh_700"] = s
        sw, s_sw = _pick("shortwave_radiation", hour);   src_map["shortwave_radiation"] = s_sw
        cape, s_cape = _pick("cape", hour);              src_map["cape"] = s_cape

        bl, s    = _pick_gfs("boundary_layer_height", hour); src_map["bl_height"] = s
        li, s    = _pick_gfs("lifted_index", hour);          src_map["lifted_index"] = s
        cin, s   = _pick_gfs("convective_inhibition", hour); src_map["cin"] = s
        if sw is None:
            sw, s_sw2 = _pick_gfs("shortwave_radiation", hour)
            if sw is not None:
                src_map["shortwave_radiation"] = s_sw2
        if cape is None:
            cape, s_cape2 = _pick_gfs("cape", hour)
            if cape is not None:
                src_map["cape"] = s_cape2

        base_msl = estimate_cloudbase_msl(t2m, td, loc["elev"])
        lr = lapse_rate(t850, t700)
        ws = estimate_wstar(bl, sw, t2m)

        gust_factor = None
        if gust is not None and ws10 is not None:
            gust_factor = round(gust - ws10, 1)

        # Compute dominant source for this hour (most common key)
        src_counts = {}
        for fld, sk in src_map.items():
            if sk is not None:
                src_counts[sk] = src_counts.get(sk, 0) + 1
        primary_src = max(src_counts, key=src_counts.get) if src_counts else None

        # Remove None values and fields matching primary from src_map
        src_detail = {fld: sk for fld, sk in src_map.items()
                      if sk is not None and sk != primary_src}

        profile.append({
            "hour": hour,
            "temp_2m": t2m, "dewpoint": td,
            "cloudbase_msl": base_msl,
            "cloudcover": cloud,
            "cloudcover_low": cl_lo, "cloudcover_mid": cl_mi, "cloudcover_high": cl_hi,
            "precipitation": prec,
            "wind_10m": ws10, "gusts": gust, "gust_factor": gust_factor,
            "wind_850": ws850, "wind_700": ws700,
            "rh_850": rh850, "rh_700": rh700,
            "lapse_rate": lr,
            "bl_height": bl, "cape": cape, "cin": cin, "lifted_index": li,
            "shortwave_radiation": sw,
            "wstar": ws,
            "_src": primary_src,
            "_src_overrides": src_detail if src_detail else None,
        })

    # ── Thermal window detection ──
    peaks = loc["peaks"]
    thermal_hours = []
    for p in profile:
        h = int(p["hour"].split(":")[0])
        if h < 9:
            continue
        w = p.get("wstar")
        prec = p.get("precipitation")
        base = p.get("cloudbase_msl")
        if w is None or w < 1.0:
            continue
        if prec is not None and prec > 0.5:
            continue
        if base is not None and (base - peaks) < 800:
            continue
        thermal_hours.append(p)

    window = {"start": None, "end": None, "peak_hour": None,
              "duration_h": 0, "peak_lapse": None, "peak_cape": None}
    if thermal_hours:
        window["start"] = thermal_hours[0]["hour"]
        window["end"] = thermal_hours[-1]["hour"]
        window["duration_h"] = len(thermal_hours)
        best_lr, best_cape, peak_h = 0, 0, thermal_hours[0]["hour"]
        for th in thermal_hours:
            lr_v = th.get("lapse_rate") or 0
            ca_v = th.get("cape") or 0
            if lr_v > best_lr or (lr_v == best_lr and ca_v > best_cape):
                best_lr, best_cape, peak_h = lr_v, ca_v, th["hour"]
        window["peak_hour"] = peak_h
        window["peak_lapse"] = best_lr if best_lr > 0 else None
        window["peak_cape"] = best_cape if best_cape > 0 else None

    return {"hourly_profile": profile, "thermal_window": window}


# ══════════════════════════════════════════════
# Continuous Flyable Window
# ══════════════════════════════════════════════

def compute_flyable_window(profile: list) -> dict:
    """Longest continuous stretch ≥1h where conditions are flyable."""
    entries = []
    for p in profile:
        h = int(p["hour"].split(":")[0])
        if h < WINDOW_START_H or h > WINDOW_END_H:
            continue
        reasons = []
        prec = p.get("precipitation")
        gust = p.get("gusts")
        ws10 = p.get("wind_10m")
        if prec is not None and prec > 0.5:
            reasons.append(f"precip={prec}")
        if gust is not None and gust > 12:
            reasons.append(f"gusts={gust}")
        if ws10 is not None and ws10 > 8:
            reasons.append(f"wind={ws10}")
        entries.append((h, len(reasons) == 0, "; ".join(reasons) if reasons else "ok"))

    max_start, max_len = None, 0
    cur_start, cur_len = None, 0
    for h, ok, _ in entries:
        if ok:
            if cur_start is None:
                cur_start = h
            cur_len += 1
            if cur_len > max_len:
                max_start, max_len = cur_start, cur_len
        else:
            cur_start, cur_len = None, 0

    return {
        "continuous_flyable_hours": max_len,
        "flyable_start": f"{max_start:02d}:00" if max_start else None,
        "flyable_end": f"{max_start + max_len - 1:02d}:00" if max_start and max_len else None,
    }


# ══════════════════════════════════════════════
# Flags & Metrics (window-based)
# ══════════════════════════════════════════════

def compute_flags(profile: list, loc: dict, flyable: dict) -> tuple[list, list]:
    """Return (flags, positives) based on the full thermal window."""
    flags, positives = [], []
    peaks = loc["peaks"]

    # Collect window-hour values
    winds_850, gusts_all, winds_10m, bases, capes, cins = [], [], [], [], [], []
    lapse_rates, bl_heights, wstars, sw_rads = [], [], [], []
    gust_factors = []
    for p in profile:
        h = int(p["hour"].split(":")[0])
        if h < WINDOW_START_H or h > WINDOW_END_H:
            continue
        if p.get("wind_850") is not None:
            winds_850.append(p["wind_850"])
        if p.get("gusts") is not None:
            gusts_all.append(p["gusts"])
        if p.get("wind_10m") is not None:
            winds_10m.append(p["wind_10m"])
        if p.get("cloudbase_msl") is not None:
            bases.append(p["cloudbase_msl"])
        if p.get("cape") is not None:
            capes.append(p["cape"])
        if p.get("cin") is not None:
            cins.append(p["cin"])
        if p.get("lapse_rate") is not None:
            lapse_rates.append(p["lapse_rate"])
        if p.get("bl_height") is not None:
            bl_heights.append(p["bl_height"])
        if p.get("wstar") is not None:
            wstars.append(p["wstar"])
        if p.get("shortwave_radiation") is not None:
            sw_rads.append(p["shortwave_radiation"])
        if p.get("gust_factor") is not None:
            gust_factors.append(p["gust_factor"])

    # ── Stop-flags ──
    # Sustained background wind > 5 m/s over window
    if winds_850 and statistics.mean(winds_850) > 5.0:
        mean_w = statistics.mean(winds_850)
        flags.append(("SUSTAINED_WIND_850",
                      f"mean {mean_w:.1f} m/s over window > 5.0 (closed route threshold)"))

    # Max gusts > 10 m/s
    if gusts_all and max(gusts_all) > 10.0:
        flags.append(("GUSTS_HIGH", f"max {max(gusts_all):.1f} m/s > 10.0 in window"))

    # Gust factor (turbulence indicator)
    if gust_factors and max(gust_factors) > 7.0:
        flags.append(("GUST_FACTOR",
                      f"max gust−mean {max(gust_factors):.1f} m/s (turbulence risk)"))

    # Continuous flyable window < 5h
    cfw = flyable.get("continuous_flyable_hours", 0)
    if 0 < cfw < 5:
        flags.append(("SHORT_WINDOW", f"continuous flyable {cfw}h < 5h"))
    elif cfw == 0:
        flags.append(("NO_FLYABLE_WINDOW", "no continuous flyable hour detected"))

    # Cloud base
    if bases:
        cb_min = min(bases)
        cb_typ = statistics.median(bases)
        margin_min = cb_min - peaks
        margin_typ = cb_typ - peaks
        if margin_min < 1000:
            flags.append(("LOW_BASE",
                          f"min base {cb_min:.0f}m MSL, margin {margin_min:.0f}m < 1000m over {peaks}m peaks"))
    else:
        cb_min, cb_typ = None, None
        margin_min, margin_typ = None, None

    # Precipitation check at 13:00
    p13 = None
    for p in profile:
        if p["hour"] == "13:00":
            p13 = p.get("precipitation")
    if p13 is not None and p13 > 0.5:
        flags.append(("PRECIP_13", f"{p13:.1f} mm/h @13:00"))

    # Cloud cover at 13:00
    cc13 = None
    for p in profile:
        if p["hour"] == "13:00":
            cc13 = p.get("cloudcover")
    if cc13 is not None and cc13 > 80:
        flags.append(("OVERCAST", f"{cc13:.0f}% @13:00"))

    # Lapse rate
    if lapse_rates and statistics.mean(lapse_rates) < 5.5:
        flags.append(("STABLE", f"mean lapse {statistics.mean(lapse_rates):.1f}°C/km < 5.5 (weak thermals)"))

    # High CAPE (overdevelopment risk)
    if capes and max(capes) > 1500:
        flags.append(("HIGH_CAPE", f"max {max(capes):.0f} J/kg — overdevelopment risk"))
        # Check CAPE trend
        if len(capes) >= 4:
            early_cape = statistics.mean(capes[:2])
            late_cape = statistics.mean(capes[-2:])
            if late_cape > early_cape * 1.5 and late_cape > 800:
                flags.append(("CAPE_RISING", f"CAPE rising: {early_cape:.0f}→{late_cape:.0f} J/kg"))

    # LI storm risk
    li_vals = [p.get("lifted_index") for p in profile
               if p["hour"] == "13:00" and p.get("lifted_index") is not None]
    if li_vals and li_vals[0] is not None and li_vals[0] < -4:
        flags.append(("VERY_UNSTABLE", f"LI={li_vals[0]} — storm risk"))

    # ── Positive indicators ──
    if lapse_rates and max(lapse_rates) > 7.0:
        positives.append(("STRONG_LAPSE", f"max {max(lapse_rates):.1f}°C/km"))
    if capes:
        peak_cape = max(capes)
        if 300 < peak_cape < 1500:
            positives.append(("GOOD_CAPE", f"peak {peak_cape:.0f} J/kg"))
    if bl_heights and max(bl_heights) > 1500:
        positives.append(("DEEP_BL", f"max {max(bl_heights):.0f}m"))
    if bases:
        max_base = max(bases)
        margin_max = max_base - peaks
        if margin_max > 1500:
            positives.append(("HIGH_BASE", f"max {max_base:.0f}m MSL (+{margin_max:.0f}m over peaks)"))
    if cfw >= 7:
        positives.append(("LONG_WINDOW", f"{cfw}h continuous flyable window"))
    if cc13 is not None and cc13 < 30:
        positives.append(("CLEAR_SKY", f"{cc13:.0f}% @13:00"))
    if wstars and max(wstars) >= 1.5:
        positives.append(("GOOD_WSTAR", f"max W*={max(wstars):.1f} m/s"))
    if sw_rads and max(sw_rads) > 600:
        positives.append(("STRONG_SUN", f"max SW radiation {max(sw_rads):.0f} W/m²"))

    return flags, positives


# ══════════════════════════════════════════════
# Model Agreement & Ensemble Uncertainty
# ══════════════════════════════════════════════

def compute_model_agreement(sources: dict) -> dict:
    """Compare ECMWF HRES vs ICON Seamless at 13:00 local."""
    ecmwf = sources.get("ecmwf_hres", {}).get("at_13_local", {})
    icon = sources.get("icon_seamless", {}).get("at_13_local", {})

    if not ecmwf or not icon:
        return {"agreement_score": None, "confidence": "UNKNOWN", "details": {}}

    tolerances = {
        "temperature_2m": 2.0, "windspeed_10m": 2.0,
        "windgusts_10m": 3.0, "cloudcover": 20.0,
        "precipitation": 0.5, "cape": 200.0,
        "windspeed_850hPa": 2.0,
    }

    agrees, details = [], {}
    for param, tol in tolerances.items():
        ve = ecmwf.get(param)
        vi = icon.get(param)
        if ve is None or vi is None:
            continue
        diff = abs(ve - vi)
        ok = diff <= tol
        agrees.append(ok)
        details[param] = {
            "ecmwf": round(ve, 2), "icon": round(vi, 2),
            "diff": round(diff, 2), "agree": ok,
        }

    if not agrees:
        return {"agreement_score": None, "confidence": "UNKNOWN", "details": details}

    score = sum(agrees) / len(agrees)
    if score >= 0.8:
        conf = "HIGH"
    elif score >= 0.5:
        conf = "MEDIUM"
    else:
        conf = "LOW"

    return {"agreement_score": round(score, 2), "confidence": conf, "details": details}


def compute_ensemble_uncertainty(sources: dict) -> dict:
    """Summarise ensemble spread at 13:00 local."""
    result = {}
    for ens_name in ("ecmwf_ens", "icon_eu_eps"):
        at13 = sources.get(ens_name, {}).get("at_13_local", {})
        if not at13:
            continue
        ens_detail = {}
        for param in ENSEMBLE_PARAMS:
            p50_k = f"{param}_p50"
            spr_k = f"{param}_spread"
            p50 = at13.get(p50_k)
            spread = at13.get(spr_k)
            if p50 is not None or spread is not None:
                ens_detail[param] = {"p50": p50, "spread": spread,
                                     "p10": at13.get(f"{param}_p10"),
                                     "p90": at13.get(f"{param}_p90")}
        if ens_detail:
            result[ens_name] = ens_detail
    return result


# ══════════════════════════════════════════════
# Score & Status
# ══════════════════════════════════════════════

CRITICAL_TAGS = {"SUSTAINED_WIND_850", "GUSTS_HIGH", "PRECIP_13", "NO_FLYABLE_WINDOW"}
QUALITY_TAGS  = {"OVERCAST", "STABLE", "SHORT_WINDOW", "GUST_FACTOR"}
DANGER_TAGS   = {"HIGH_CAPE", "VERY_UNSTABLE", "CAPE_RISING"}


def compute_status(flags, positives, agreement, ensemble_unc):
    n_crit = sum(1 for t, _ in flags if t in CRITICAL_TAGS)
    n_qual = sum(1 for t, _ in flags if t in QUALITY_TAGS)
    n_dang = sum(1 for t, _ in flags if t in DANGER_TAGS)
    n_base = sum(1 for t, _ in flags if t == "LOW_BASE")
    n_pos  = len(positives)

    score = -n_crit * 3 - n_base * 2 - n_qual * 1 - n_dang * 1 + n_pos * 2

    if score <= -5:
        status = "NO-GO"
    elif score <= -2:
        status = "UNLIKELY"
    elif score <= 1:
        status = "MAYBE"
    elif score <= 4:
        status = "GO"
    else:
        status = "STRONG"

    if n_crit >= 2 or (n_crit >= 1 and n_base >= 1):
        status = "NO-GO"
    elif n_crit >= 1 and status in ("GO", "STRONG"):
        status = "MAYBE"

    # High ensemble spread → downgrade to MARGINAL at most
    conf = agreement.get("confidence", "UNKNOWN")
    if conf == "LOW" and status in ("GO", "STRONG"):
        status = "MAYBE"
        flags.append(("LOW_CONFIDENCE",
                      f"model agreement {agreement.get('agreement_score', '?')} → confidence LOW"))

    # Large ensemble spread → at most MAYBE
    for ens_name, ens_data in ensemble_unc.items():
        wind_sp = ens_data.get("windspeed_10m", {}).get("spread")
        cape_sp = ens_data.get("cape", {}).get("spread")
        if wind_sp is not None and wind_sp > 5 and status in ("GO", "STRONG"):
            status = "MAYBE"
            flags.append(("ENS_WIND_SPREAD", f"{ens_name} wind spread {wind_sp:.1f} m/s"))
        if cape_sp is not None and cape_sp > 1000 and status in ("GO", "STRONG"):
            status = "MAYBE"
            flags.append(("ENS_CAPE_SPREAD", f"{ens_name} CAPE spread {cape_sp:.0f} J/kg"))

    # Insufficient data
    key_vals = [
        any(t == "SUSTAINED_WIND_850" for t, _ in flags) or len([p for p in positives if p[0] == "GOOD_WSTAR"]) > 0,
    ]
    # Simple check: if no profile data at all
    if n_crit == 0 and n_qual == 0 and n_pos == 0:
        status = "NO DATA"

    return score, status


# ══════════════════════════════════════════════
# Location Assessment (orchestrator)
# ══════════════════════════════════════════════

ALL_SOURCES = [
    "ecmwf_hres", "icon_seamless", "icon_d2", "gfs",
    "ecmwf_ens", "icon_eu_eps",
    "geosphere_arome", "mosmix",
]

HEADLESS_SOURCES = ["meteo_parapente", "xccontest", "alptherm"]


def assess_location(loc_key: str, loc: dict, date: str, sources_list: list) -> dict:
    result = {
        "location": loc["name"], "key": loc_key, "date": date,
        "drive_h": loc.get("drive_h", "?"),
        "sources": {}, "assessment": {},
    }
    lat, lon = loc["lat"], loc["lon"]

    # ── Fetch deterministic models ──
    for src_name, fetcher, params_ref in [
        ("ecmwf_hres",    fetch_ecmwf_hres,    ECMWF_PARAMS),
        ("icon_seamless", fetch_icon_seamless,  ICON_PARAMS),
        ("icon_d2",       fetch_icon_d2,        ICON_D2_PARAMS),
        ("gfs",           fetch_gfs,            GFS_PARAMS),
    ]:
        if src_name not in sources_list:
            continue
        try:
            d = fetcher(lat, lon, date)
            h = d.get("hourly", {})
            at13 = _extract_at_13_local(h, date)
            tw_stats = _extract_window_stats(h, date)
            result["sources"][src_name] = {
                "model_id": MODEL_IDS.get(src_name, src_name),
                "model_label": MODEL_LABELS.get(src_name, src_name),
                "at_13_local": at13,
                "thermal_window_stats": tw_stats,
                "_hourly_raw": h,   # kept in memory for profile, stripped from JSON output
            }
        except Exception as e:
            result["sources"][src_name] = {"error": str(e)}

    # ── Fetch ensemble models ──
    for src_name, fetcher in [
        ("ecmwf_ens", fetch_ecmwf_ens),
        ("icon_eu_eps", fetch_icon_eu_eps),
    ]:
        if src_name not in sources_list:
            continue
        try:
            agg = fetcher(lat, lon, date)
            at13 = _extract_at_13_local(agg, date)
            tw_stats = _extract_window_stats(agg, date)
            result["sources"][src_name] = {
                "model_id": MODEL_IDS.get(src_name, src_name),
                "model_label": MODEL_LABELS.get(src_name, src_name),
                "at_13_local": at13,
                "thermal_window_stats": tw_stats,
            }
        except Exception as e:
            result["sources"][src_name] = {"error": str(e)}

    # ── GeoSphere AROME ──
    if "geosphere_arome" in sources_list:
        try:
            geo = fetch_geosphere_arome(lat, lon)
            h = geo.get("hourly", {})
            utc_ts = geo.get("utc_timestamps", False)
            at13 = _extract_at_13_local(h, date, utc_ts)
            tw_stats = _extract_window_stats(h, date, utc_ts)
            result["sources"]["geosphere_arome"] = {
                "model_id": MODEL_IDS.get("geosphere_arome"),
                "model_label": MODEL_LABELS.get("geosphere_arome"),
                "at_13_local": at13,
                "thermal_window_stats": tw_stats,
            }
        except Exception as e:
            result["sources"]["geosphere_arome"] = {"error": str(e)}

    # ── MOSMIX ──
    if "mosmix" in sources_list and loc.get("mosmix_id"):
        try:
            mos = fetch_mosmix(loc["mosmix_id"], date)
            if "error" not in mos:
                result["sources"]["mosmix"] = mos
            else:
                result["sources"]["mosmix"] = {"error": mos["error"]}
        except Exception as e:
            result["sources"]["mosmix"] = {"error": str(e)}

    # ── Build hourly profile ──
    hourly_analysis = build_hourly_profile(result["sources"], date, loc)
    result["hourly_analysis"] = hourly_analysis

    # ── Flyable window ──
    flyable = compute_flyable_window(hourly_analysis["hourly_profile"])

    # ── Flags & Metrics ──
    flags, positives = compute_flags(
        hourly_analysis["hourly_profile"], loc, flyable)

    # ── Model Agreement ──
    agreement = compute_model_agreement(result["sources"])
    ensemble_unc = compute_ensemble_uncertainty(result["sources"])

    # ── Score & Status ──
    score, status = compute_status(flags, positives, agreement, ensemble_unc)

    # ── Build assessment (backward-compat + enriched) ──
    # Pick best values at 13:00 from priority: ICON-D2 > ECMWF > ICON > GFS
    _BEST_ORDER = ("icon_d2", "ecmwf_hres", "icon_seamless", "gfs")
    _at13_src = {}  # track which source provided each assessment field

    def _best13(key, field_name=None):
        for src in _BEST_ORDER:
            v = result["sources"].get(src, {}).get("at_13_local", {}).get(key)
            if v is not None:
                if field_name:
                    _at13_src[field_name] = src
                return v
        return None

    t2m = _best13("temperature_2m", "temp_2m")
    td  = _best13("dewpoint_2m", "dewpoint_2m")
    cbm = estimate_cloudbase_msl(t2m, td, loc["elev"])
    ws850 = _best13("windspeed_850hPa", "wind_850hPa_ms")
    ws700 = _best13("windspeed_700hPa", "wind_700hPa_ms")
    gusts = _best13("windgusts_10m", "gusts_10m_ms")
    cape  = _best13("cape", "cape_J_per_kg")
    t850  = _best13("temperature_850hPa", "t850")
    t700  = _best13("temperature_700hPa", "t700")
    cloud = _best13("cloudcover", "cloudcover_pct")
    prec  = _best13("precipitation", "precipitation_mm")
    lr    = lapse_rate(t850, t700)
    bl_h  = result["sources"].get("gfs", {}).get("at_13_local", {}).get("boundary_layer_height")
    if bl_h is not None:
        _at13_src["boundary_layer_height_m"] = "gfs"
    sw    = _best13("shortwave_radiation", "shortwave_radiation")
    li    = result["sources"].get("gfs", {}).get("at_13_local", {}).get("lifted_index")
    if li is not None:
        _at13_src["lifted_index"] = "gfs"
    cin   = result["sources"].get("gfs", {}).get("at_13_local", {}).get("convective_inhibition")
    if cin is not None:
        _at13_src["cin_J_per_kg"] = "gfs"
    ws_v  = estimate_wstar(bl_h, sw, t2m)

    bm = (cbm - loc["peaks"]) if cbm is not None else None
    tw = hourly_analysis.get("thermal_window", {})

    # Window-based stats for assessment
    profile = hourly_analysis["hourly_profile"]
    winds_850_win = [p["wind_850"] for p in profile
                     if WINDOW_START_H <= int(p["hour"][:2]) <= WINDOW_END_H
                     and p["wind_850"] is not None]
    gusts_win = [p["gusts"] for p in profile
                 if WINDOW_START_H <= int(p["hour"][:2]) <= WINDOW_END_H
                 and p["gusts"] is not None]
    gf_win = [p["gust_factor"] for p in profile
              if WINDOW_START_H <= int(p["hour"][:2]) <= WINDOW_END_H
              and p["gust_factor"] is not None]
    bases_win = [p["cloudbase_msl"] for p in profile
                 if WINDOW_START_H <= int(p["hour"][:2]) <= WINDOW_END_H
                 and p["cloudbase_msl"] is not None]

    assessment = {
        # At 13:00 local
        "temp_2m": t2m, "dewpoint_2m": td,
        "cloudbase_msl": cbm, "base_margin_over_peaks": bm,
        "wind_850hPa_ms": ws850, "wind_700hPa_ms": ws700,
        "gusts_10m_ms": gusts,
        "cape_J_per_kg": cape, "cin_J_per_kg": cin, "lifted_index": li,
        "boundary_layer_height_m": bl_h,
        "lapse_rate_C_per_km": lr, "wstar_ms": ws_v,
        "cloudcover_pct": cloud,
        "cloudcover_low_pct": _best13("cloudcover_low", "cloudcover_low_pct"),
        "cloudcover_mid_pct": _best13("cloudcover_mid", "cloudcover_mid_pct"),
        "cloudcover_high_pct": _best13("cloudcover_high", "cloudcover_high_pct"),
        "precipitation_mm": prec,
        "relative_humidity_2m": _best13("relative_humidity_2m", "relative_humidity_2m"),
        "relative_humidity_850": _best13("relative_humidity_850hPa", "relative_humidity_850"),
        "relative_humidity_700": _best13("relative_humidity_700hPa", "relative_humidity_700"),
        "shortwave_radiation": sw,

        # Thermal window
        "thermal_window_start": tw.get("start"),
        "thermal_window_end": tw.get("end"),
        "thermal_window_hours": tw.get("duration_h", 0),
        "thermal_window_peak": tw.get("peak_hour"),

        # Window-based metrics
        "sustained_wind_850_mean": round(statistics.mean(winds_850_win), 1) if winds_850_win else None,
        "max_gust_window": round(max(gusts_win), 1) if gusts_win else None,
        "max_gust_factor_window": round(max(gf_win), 1) if gf_win else None,
        "continuous_flyable_hours": flyable["continuous_flyable_hours"],
        "flyable_start": flyable.get("flyable_start"),
        "flyable_end": flyable.get("flyable_end"),
        "cb_min_msl": round(min(bases_win)) if bases_win else None,
        "cb_typ_msl": round(statistics.median(bases_win)) if bases_win else None,

        # Flags & status
        "flags": [{"tag": t, "msg": m} for t, m in flags],
        "positives": [{"tag": t, "msg": m} for t, m in positives],
        "score": score,
        "status": status,

        # Model agreement & uncertainty
        "model_agreement": agreement,
        "ensemble_uncertainty": ensemble_unc,

        # Source provenance: which model provided each @13 field
        "_sources": _at13_src,
    }
    result["assessment"] = assessment
    return result


# ══════════════════════════════════════════════
# CONSOLE OUTPUT
# ══════════════════════════════════════════════

STATUS_EMOJI = {
    "NO-GO": "🔴", "UNLIKELY": "🟠", "MAYBE": "🟡",
    "GO": "🟢", "STRONG": "💚", "NO DATA": "⚪",
}
STATUS_ORDER = {"STRONG": 0, "GO": 1, "MAYBE": 2, "UNLIKELY": 3, "NO-GO": 4, "NO DATA": 5}


def _fv(v, unit="", prec=None):
    if v is None:
        return "—"
    if isinstance(v, (int, float)) and prec is not None:
        return f"{v:.{prec}f}{unit}"
    return f"{v}{unit}"


def print_triage(results, forecast_date):
    print("\n" + "=" * 76)
    print(f"  QUICK TRIAGE v{APP_VERSION} — Forecast for {forecast_date}")
    print("=" * 76)

    sorted_r = sorted(results, key=lambda r: STATUS_ORDER.get(
        r.get("assessment", {}).get("status", "NO-GO"), 5))

    for r in sorted_r:
        a = r.get("assessment", {})
        if "error" in r and "assessment" not in r:
            print(f"\n  {r['location']}: ERROR — {r['error']}")
            continue
        st = a.get("status", "?")
        em = STATUS_EMOJI.get(st, "⚪")
        print(f"\n  {em} {r['location']:15s} [{st}]  (score: {a.get('score', '?')})")
        print(f"     Base: {_fv(a.get('cloudbase_msl'),' m',0)} MSL "
              f"(min: {_fv(a.get('cb_min_msl'),' m',0)}, "
              f"typ: {_fv(a.get('cb_typ_msl'),' m',0)}) "
              f"margin: {_fv(a.get('base_margin_over_peaks'),' m',0)}")
        print(f"     Wind @850: {_fv(a.get('wind_850hPa_ms'),' m/s',1)} "
              f"(mean window: {_fv(a.get('sustained_wind_850_mean'),' m/s',1)})  |  "
              f"Gusts: {_fv(a.get('gusts_10m_ms'),' m/s',1)} "
              f"(max window: {_fv(a.get('max_gust_window'),' m/s',1)})  |  "
              f"GF max: {_fv(a.get('max_gust_factor_window'),' m/s',1)}")
        print(f"     CAPE: {_fv(a.get('cape_J_per_kg'),'',0)}  |  "
              f"LI: {_fv(a.get('lifted_index'))}  |  "
              f"Lapse: {_fv(a.get('lapse_rate_C_per_km'),' °C/km',1)}  |  "
              f"BL: {_fv(a.get('boundary_layer_height_m'),' m',0)}  |  "
              f"W*: {_fv(a.get('wstar_ms'),' m/s',2)}")
        cfw = a.get("continuous_flyable_hours", 0)
        fs = a.get("flyable_start", "—")
        fe = a.get("flyable_end", "—")
        twh = a.get("thermal_window_hours", 0)
        tws = a.get("thermal_window_start", "—")
        twe = a.get("thermal_window_end", "—")
        print(f"     Flyable: {cfw}h ({fs}–{fe})  |  "
              f"Thermal window: {twh}h ({tws}–{twe})")

        # Cloud layers
        print(f"     Cloud: {_fv(a.get('cloudcover_pct'),'%',0)} "
              f"(L{_fv(a.get('cloudcover_low_pct'),'%',0)} "
              f"M{_fv(a.get('cloudcover_mid_pct'),'%',0)} "
              f"H{_fv(a.get('cloudcover_high_pct'),'%',0)})  |  "
              f"SW rad: {_fv(a.get('shortwave_radiation'),' W/m²',0)}")
        print(f"     RH 2m: {_fv(a.get('relative_humidity_2m'),'%',0)}  |  "
              f"RH 850: {_fv(a.get('relative_humidity_850'),'%',0)}  |  "
              f"RH 700: {_fv(a.get('relative_humidity_700'),'%',0)}")

        # Model agreement
        ma = a.get("model_agreement", {})
        conf = ma.get("confidence", "?")
        ascore = ma.get("agreement_score")
        if ascore is not None:
            print(f"     Model agreement: {ascore:.0%} — confidence: {conf}")

        # Ensemble uncertainty
        eu = a.get("ensemble_uncertainty", {})
        for ens_n, ens_d in eu.items():
            spreads = []
            for p, pd in ens_d.items():
                sp = pd.get("spread")
                if sp is not None:
                    spreads.append(f"{p}±{sp:.1f}")
            if spreads:
                print(f"     {ens_n} spread: {', '.join(spreads[:4])}")

        for f in a.get("flags", []):
            print(f"     ⚠ {f['tag']}: {f['msg']}")
        for p in a.get("positives", []):
            print(f"     ✓ {p['tag']}: {p['msg']}")

        # Source provenance
        src_map = a.get("_sources", {})
        if src_map:
            used = sorted(set(src_map.values()))
            labels = [f"{s} ({MODEL_LABELS.get(s, s)})" for s in used]
            print(f"     Models @13: {', '.join(labels)}")

    print(f"\n{'=' * 76}\n")


# ══════════════════════════════════════════════
# MARKDOWN REPORT
# ══════════════════════════════════════════════

def _v(val, unit="", precision=1):
    if val is None:
        return "—"
    if isinstance(val, float):
        return f"{val:.{precision}f}{unit}"
    return f"{val}{unit}"


def generate_markdown_report(results, date, gen_time):
    L = []
    L.append(f"# ✈️ PG Weather Triage v{APP_VERSION}")
    L.append(f"\n**Forecast for: {date}**\n")
    L.append(f"*Generated: {gen_time}*\n")

    sorted_r = sorted(results, key=lambda r: STATUS_ORDER.get(
        r.get("assessment", {}).get("status", "NO-GO"), 5))

    # ── Summary table ──
    L.append("## 📊 Summary\n")
    L.append("| Location | Drive | Status | Base @13 | Margin | W850 mean | Gusts max | CAPE | Lapse | BL | W* | Flyable | Confidence |")
    L.append("|----------|-------|--------|----------|--------|-----------|-----------|------|-------|----|-----|---------|------------|")
    for r in sorted_r:
        a = r.get("assessment", {})
        s = a.get("status", "?")
        em = STATUS_EMOJI.get(s, "⚪")
        cfw = a.get("continuous_flyable_hours", 0)
        fw_str = f"{cfw}h" if cfw else "—"
        conf = a.get("model_agreement", {}).get("confidence", "?")
        L.append(
            f"| {r['location']} | {_v(r.get('drive_h'),'h')} "
            f"| {em} **{s}** "
            f"| {_v(a.get('cloudbase_msl'),'m')} "
            f"| {_v(a.get('base_margin_over_peaks'),'m')} "
            f"| {_v(a.get('sustained_wind_850_mean'),'m/s')} "
            f"| {_v(a.get('max_gust_window'),'m/s')} "
            f"| {_v(a.get('cape_J_per_kg'),'',0)} "
            f"| {_v(a.get('lapse_rate_C_per_km'),'°C/km')} "
            f"| {_v(a.get('boundary_layer_height_m'),'m',0)} "
            f"| {_v(a.get('wstar_ms'),'',2)} "
            f"| {fw_str} "
            f"| {conf} |"
        )
    L.append("")

    # ── Per-location details ──
    for r in sorted_r:
        a = r.get("assessment", {})
        s = a.get("status", "?")
        em = STATUS_EMOJI.get(s, "⚪")
        loc = LOCATIONS.get(r.get("key"), {})

        L.append(f"---\n")
        L.append(f"## {em} {r['location']} — **{s}** (score: {a.get('score','?')})\n")

        L.append("### Key Metrics @13:00 local")
        L.append(f"- **Cloud Base**: {_v(a.get('cloudbase_msl'),'m')} MSL "
                 f"(min: {_v(a.get('cb_min_msl'),'m')}, typ: {_v(a.get('cb_typ_msl'),'m')}) "
                 f"margin: {_v(a.get('base_margin_over_peaks'),'m')} over {loc.get('peaks','?')}m")
        L.append(f"- **Wind @850hPa**: {_v(a.get('wind_850hPa_ms'),'m/s')} "
                 f"(window mean: {_v(a.get('sustained_wind_850_mean'),'m/s')})  |  "
                 f"**@700hPa**: {_v(a.get('wind_700hPa_ms'),'m/s')}")
        L.append(f"- **Gusts**: {_v(a.get('gusts_10m_ms'),'m/s')} "
                 f"(window max: {_v(a.get('max_gust_window'),'m/s')})  |  "
                 f"**Gust factor max**: {_v(a.get('max_gust_factor_window'),'m/s')}")
        L.append(f"- **CAPE**: {_v(a.get('cape_J_per_kg'),'J/kg',0)}  |  "
                 f"**LI**: {_v(a.get('lifted_index'))}  |  "
                 f"**CIN**: {_v(a.get('cin_J_per_kg'),'J/kg',0)}")
        L.append(f"- **Lapse**: {_v(a.get('lapse_rate_C_per_km'),'°C/km')}  |  "
                 f"**BL**: {_v(a.get('boundary_layer_height_m'),'m',0)}  |  "
                 f"**W***: {_v(a.get('wstar_ms'),' m/s',2)}")
        L.append(f"- **Cloud**: {_v(a.get('cloudcover_pct'),'%',0)} "
                 f"(low {_v(a.get('cloudcover_low_pct'),'%',0)}, "
                 f"mid {_v(a.get('cloudcover_mid_pct'),'%',0)}, "
                 f"high {_v(a.get('cloudcover_high_pct'),'%',0)})  |  "
                 f"**Precip**: {_v(a.get('precipitation_mm'),'mm')}")
        L.append(f"- **SW radiation**: {_v(a.get('shortwave_radiation'),' W/m²',0)}  |  "
                 f"**RH** 2m={_v(a.get('relative_humidity_2m'),'%',0)} "
                 f"850={_v(a.get('relative_humidity_850'),'%',0)} "
                 f"700={_v(a.get('relative_humidity_700'),'%',0)}")

        cfw = a.get("continuous_flyable_hours", 0)
        L.append(f"- **Flyable window**: {cfw}h "
                 f"({a.get('flyable_start','—')}–{a.get('flyable_end','—')})")
        twh = a.get("thermal_window_hours", 0)
        if twh > 0:
            L.append(f"- **Thermal window**: {a.get('thermal_window_start')}–"
                     f"{a.get('thermal_window_end')} ({twh}h), peak @{a.get('thermal_window_peak')}")
        L.append("")

        # Data sources @13:00
        src_map = a.get("_sources", {})
        if src_map:
            used = sorted(set(src_map.values()))
            labels = [f"**{s}** ({MODEL_LABELS.get(s, s)})" for s in used]
            L.append(f"*Data sources @13: {', '.join(labels)}*")
            L.append("")

        # Flags
        if a.get("flags"):
            L.append("**⚠ Warnings:**")
            for f in a["flags"]:
                L.append(f"- {f['tag']}: {f['msg']}")
            L.append("")
        if a.get("positives"):
            L.append("**✓ Positive indicators:**")
            for p in a["positives"]:
                L.append(f"- {p['tag']}: {p['msg']}")
            L.append("")

        # Model agreement
        ma = a.get("model_agreement", {})
        if ma.get("agreement_score") is not None:
            L.append(f"### Model Agreement: {ma['agreement_score']:.0%} — {ma['confidence']}")
            det = ma.get("details", {})
            if det:
                L.append("| Param | ECMWF | ICON | Diff | Agree |")
                L.append("|-------|-------|------|------|-------|")
                for p, d in det.items():
                    L.append(f"| {p} | {_v(d.get('ecmwf'))} | {_v(d.get('icon'))} "
                             f"| {_v(d.get('diff'))} | {'✓' if d.get('agree') else '✗'} |")
            L.append("")

        # Ensemble uncertainty
        eu = a.get("ensemble_uncertainty", {})
        if eu:
            L.append("### Ensemble Uncertainty")
            for ens_n, ens_d in eu.items():
                L.append(f"\n**{ens_n}**")
                L.append("| Param | p10 | p50 | p90 | Spread |")
                L.append("|-------|-----|-----|-----|--------|")
                for p, pd in ens_d.items():
                    L.append(f"| {p} | {_v(pd.get('p10'))} | {_v(pd.get('p50'))} "
                             f"| {_v(pd.get('p90'))} | {_v(pd.get('spread'))} |")
            L.append("")

        # Hourly profile
        hp = r.get("hourly_analysis", {}).get("hourly_profile", [])
        if hp:
            L.append("### Hourly Profile")
            L.append("| Hour | Src | T | Base | Cloud | CL/CM/CH | Precip | W10 | Gust | GF | W850 | Lapse | BL | CAPE | SW | W* |")
            L.append("|------|-----|---|------|-------|----------|--------|-----|------|----|------|-------|----|------|----|----|")
            for item in hp:
                cl = f"{_v(item.get('cloudcover_low'),'',0)}/{_v(item.get('cloudcover_mid'),'',0)}/{_v(item.get('cloudcover_high'),'',0)}"
                src_tag = item.get('_src', '?')
                ovr = item.get('_src_overrides')
                if ovr:
                    src_tag += '+'
                L.append(
                    f"| {item['hour']} "
                    f"| {src_tag} "
                    f"| {_v(item['temp_2m'],'°C')} "
                    f"| {_v(item['cloudbase_msl'],'m',0)} "
                    f"| {_v(item['cloudcover'],'%',0)} "
                    f"| {cl} "
                    f"| {_v(item['precipitation'],'mm')} "
                    f"| {_v(item.get('wind_10m'),'',1)} "
                    f"| {_v(item['gusts'],'',1)} "
                    f"| {_v(item.get('gust_factor'),'',1)} "
                    f"| {_v(item['wind_850'],'',1)} "
                    f"| {_v(item['lapse_rate'],'',1)} "
                    f"| {_v(item['bl_height'],'',0)} "
                    f"| {_v(item['cape'],'',0)} "
                    f"| {_v(item.get('shortwave_radiation'),'',0)} "
                    f"| {_v(item.get('wstar'),'',2)} |"
                )
            L.append("")

        # MOSMIX
        mos = r.get("sources", {}).get("mosmix", {})
        if mos and "error" not in mos:
            L.append(f"### DWD MOSMIX ({mos.get('station_name','?')}) — local time")
            mh = mos.get("hourly_local", {})
            if mh:
                all_hours = sorted(set().union(*(v.keys() for v in mh.values())))
                target_h = [h for h in all_hours if h in ("09:00", "12:00", "13:00", "15:00", "18:00")]
                if target_h:
                    params_avail = [p for p in MOSMIX_PARAMS_OF_INTEREST if p in mh]
                    L.append("| Param | " + " | ".join(target_h) + " |")
                    L.append("|-------" + "|------" * len(target_h) + "|")
                    for p in params_avail[:14]:
                        vals = [_v(mh[p].get(h)) for h in target_h]
                        L.append(f"| {p} | " + " | ".join(vals) + " |")
            L.append("")

        # GeoSphere thermal_window_stats
        geo_tw = r.get("sources", {}).get("geosphere_arome", {}).get("thermal_window_stats", {})
        if geo_tw:
            L.append("### GeoSphere AROME — thermal window stats")
            L.append("| Param | Min | Mean | Max | Trend |")
            L.append("|-------|-----|------|-----|-------|")
            for p in ("temperature_2m", "cape", "cloudcover", "windspeed_10m",
                      "windgusts_10m", "shortwave_radiation", "precipitation"):
                s = geo_tw.get(p)
                if s and s.get("n", 0) > 0:
                    L.append(f"| {p} | {_v(s['min'])} | {_v(s['mean'])} "
                             f"| {_v(s['max'])} | {s.get('trend','—')} |")
            L.append("")

        L.append("---\n")

    # ── Doubts/Uncertainties block ──
    L.append("## ⚠️ Сомнения / Неуверенности (Model Divergence)\n")
    any_doubt = False
    for r in sorted_r:
        a = r.get("assessment", {})
        ma = a.get("model_agreement", {})
        eu = a.get("ensemble_uncertainty", {})
        issues = []
        if ma.get("confidence") == "LOW":
            issues.append(f"Model agreement LOW ({ma.get('agreement_score', '?')})")
            det = ma.get("details", {})
            for p, d in det.items():
                if not d.get("agree"):
                    issues.append(f"  • {p}: ECMWF={d.get('ecmwf')} vs ICON={d.get('icon')}")
        for ens_n, ens_d in eu.items():
            for p, pd in ens_d.items():
                sp = pd.get("spread")
                if sp is not None and ((p == "windspeed_10m" and sp > 4)
                        or (p == "cape" and sp > 600)
                        or (p == "cloudcover" and sp > 40)):
                    issues.append(f"{ens_n}: high spread in {p} (±{sp})")
        if issues:
            any_doubt = True
            L.append(f"**{r['location']}**:")
            for iss in issues:
                L.append(f"- {iss}")
            L.append("")
    if not any_doubt:
        L.append("No significant model divergence detected.\n")

    return "\n".join(L)


# ══════════════════════════════════════════════
# HTML VIEWER
# ══════════════════════════════════════════════

def _render_viewer_html(report_map: dict) -> str:
    template_path = Path(__file__).parent / "viewer_template.html"
    with open(template_path, "r", encoding="utf-8") as f:
        template = f.read()
    data_json = json.dumps(report_map, ensure_ascii=False)
    return template.replace("__REPORTS_DATA__", data_json)


def generate_viewer_html(reports_dir: Path):
    json_files = sorted(reports_dir.glob("*.json"), reverse=True)
    all_reports = {}
    for jf in json_files:
        try:
            with open(jf, "r", encoding="utf-8") as f:
                all_reports[jf.stem] = json.load(f)
        except Exception:
            continue
    if not all_reports:
        print("  No JSON reports found, skipping viewer", file=sys.stderr)
        return
    html = _render_viewer_html(all_reports)
    viewer_path = reports_dir / "index.html"
    with open(viewer_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"HTML viewer:     {viewer_path}", file=sys.stderr)


def generate_single_report_html(reports_dir: Path, report_key: str, report_data: dict):
    html = _render_viewer_html({report_key: report_data})
    dated_path = reports_dir / f"{report_key}.html"
    with open(dated_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"HTML report:     {dated_path}", file=sys.stderr)
    latest_path = reports_dir / "latest.html"
    with open(latest_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"HTML latest:     {latest_path}", file=sys.stderr)


# ══════════════════════════════════════════════
# Headless Scraper Integration
# ══════════════════════════════════════════════

def run_headless_scraper(date, locs, headless_sources):
    deno = shutil.which("deno")
    if not deno:
        print("  deno not found — skipping headless sources", file=sys.stderr)
        return []
    scraper_path = Path(__file__).parent / "scraper.ts"
    if not scraper_path.exists():
        print(f"  {scraper_path} not found — skipping headless", file=sys.stderr)
        return []
    locs_json = json.dumps(
        [{"key": k, "lat": v["lat"], "lon": v["lon"]} for k, v in locs.items()])
    cmd = [deno, "run", "-A", str(scraper_path),
           "--date", date, "--locations", locs_json,
           "--sources", ",".join(headless_sources)]
    print(f"\nRunning headless scraper: {', '.join(headless_sources)}...", file=sys.stderr)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        if proc.stderr:
            for line in proc.stderr.strip().split("\n"):
                print(f"  [scraper] {line}", file=sys.stderr)
        if proc.returncode != 0:
            print(f"  Scraper exited {proc.returncode}", file=sys.stderr)
            return []
        return json.loads(proc.stdout.strip()) if proc.stdout.strip() else []
    except subprocess.TimeoutExpired:
        print("  Scraper timed out (180s)", file=sys.stderr)
        return []
    except Exception as e:
        print(f"  Scraper error: {e}", file=sys.stderr)
        return []


# ══════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description=f"PG Weather Triage v{APP_VERSION} — data fetcher & report")
    parser.add_argument("--date", default=None,
                        help="Forecast date YYYY-MM-DD (default: next Saturday)")
    parser.add_argument("--locations", default="all",
                        help="Comma-separated keys or 'all'")
    parser.add_argument("--sources", default="all",
                        help=f"Comma-separated or 'all'. Available: {','.join(ALL_SOURCES)}")
    parser.add_argument("--output-dir", default="reports")
    parser.add_argument("--no-markdown", action="store_true")
    parser.add_argument("--no-viewer", action="store_true")
    parser.add_argument("--no-scraper", action="store_true")
    parser.add_argument("--headless-sources", default="meteo_parapente",
                        help=f"Available: {','.join(HEADLESS_SOURCES)}")
    args = parser.parse_args()

    forecast_date = args.date or _next_saturday()
    print(f"Forecast date: {forecast_date}", file=sys.stderr)
    print(f"Version: {APP_VERSION}", file=sys.stderr)
    print(f"Model stack: ECMWF HRES + ICON-seamless + ICON-D2 + GFS "
          f"+ ECMWF ENS + ICON-EU EPS + GeoSphere AROME + MOSMIX", file=sys.stderr)

    sources = ALL_SOURCES if args.sources == "all" else [s.strip() for s in args.sources.split(",")]

    if args.locations == "all":
        locs = LOCATIONS
    else:
        keys = [k.strip() for k in args.locations.split(",")]
        locs = {k: LOCATIONS[k] for k in keys if k in LOCATIONS}
        if not locs:
            print(f"No valid locations. Available: {', '.join(LOCATIONS.keys())}",
                  file=sys.stderr)
            sys.exit(1)

    now = datetime.now(tz=TZ_UTC)
    gen_time = now.strftime("%Y-%m-%d %H:%M UTC")
    ts_suffix = now.strftime("%Y%m%d_%H%M")

    # ── Fetch all locations ──
    results = []
    for key, loc in locs.items():
        print(f"\nFetching {loc['name']}...", file=sys.stderr)
        try:
            res = assess_location(key, loc, forecast_date, sources)
            results.append(res)
        except Exception as e:
            print(f"  ERROR {loc['name']}: {e}", file=sys.stderr)
            results.append({"location": loc["name"], "key": key, "error": str(e),
                            "assessment": {"status": "ERROR", "score": -99}})

    # ── Headless scraper ──
    if not args.no_scraper:
        hs = [s.strip() for s in args.headless_sources.split(",")]
        hs = [s for s in hs if s in HEADLESS_SOURCES]
        if hs:
            scraper_data = run_headless_scraper(forecast_date, locs, hs)
            results_by_key = {r.get("key"): r for r in results}
            for sd in scraper_data:
                src = sd.get("source", "unknown")
                lk = sd.get("location")
                if lk in results_by_key:
                    results_by_key[lk].setdefault("sources", {})[src] = sd
                else:
                    for r in results:
                        r.setdefault("sources", {})[src] = sd

    # Console
    print_triage(results, forecast_date)

    # Output
    out_dir = Path(args.output_dir)
    out_dir.mkdir(exist_ok=True)

    # ── Clean JSON (strip _hourly_raw) ──
    json_results = []
    for r in results:
        rc = dict(r)
        clean_src = {}
        for sn, sd in rc.get("sources", {}).items():
            if isinstance(sd, dict):
                clean = {k: v for k, v in sd.items() if not k.startswith("_")}
                clean_src[sn] = clean
            else:
                clean_src[sn] = sd
        rc["sources"] = clean_src
        json_results.append(rc)

    json_output = {
        "generated_at": gen_time,
        "forecast_date": forecast_date,
        "app_version": APP_VERSION,
        "model_stack": {
            "deterministic": ["ecmwf_hres", "icon_seamless", "icon_d2", "gfs"],
            "ensemble": ["ecmwf_ens", "icon_eu_eps"],
            "regional": ["geosphere_arome", "mosmix"],
        },
        "locations": json_results,
    }

    file_stem = f"{forecast_date}_{ts_suffix}"
    json_path = out_dir / f"{file_stem}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(json_output, f, ensure_ascii=False, indent=2)
    print(f"JSON report:     {json_path}", file=sys.stderr)

    # ── Markdown ──
    if not args.no_markdown:
        md = generate_markdown_report(results, forecast_date, gen_time)
        md_path = out_dir / f"{file_stem}.md"
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(md)
        print(f"Markdown report: {md_path}", file=sys.stderr)

    # ── HTML ──
    if not args.no_viewer:
        generate_viewer_html(out_dir)
        generate_single_report_html(out_dir, file_stem, json_output)


if __name__ == "__main__":
    main()
