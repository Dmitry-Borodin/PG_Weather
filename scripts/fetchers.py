#!/usr/bin/env python3
"""
Data fetching module for PG Weather Triage v2.0.

Model families with fallback chains:
  ICON:  D2 (2km, 48h) → EU (7km, 120h) → Global (13km, 180h+)
  ECMWF: IFS 0.25° → IFS 0.4°
  GFS:   Seamless (auto-blend, always needed for BL/LI/CIN)

Ensemble:
  ECMWF ENS (51 members) / ICON-EU EPS (40 members)

Regional:
  GeoSphere AROME 2.5 km / DWD MOSMIX
"""

import io
import json
import math
import sys
import time
import zipfile
from datetime import datetime, timezone
from urllib.error import URLError
from urllib.request import urlopen, Request
from urllib.parse import urlencode
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

# ══════════════════════════════════════════════
# Constants
# ══════════════════════════════════════════════

APP_VERSION = "2.0"
TZ_LOCAL = ZoneInfo("Europe/Berlin")
TZ_UTC = timezone.utc

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

ICON_PARAMS = list(ECMWF_PARAMS) + ["updraft"]
ICON_D2_PARAMS = list(ECMWF_PARAMS) + ["updraft"]

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

# ── Model metadata ──

MODEL_LABELS = {
    # ICON family
    "icon_d2":         "ICON-D2 2 km",
    "icon_eu":         "ICON-EU 7 km",
    "icon_global":     "ICON Global 13 km",
    # ECMWF family
    "ecmwf_ifs025":    "ECMWF IFS HRES 0.25°",
    "ecmwf_ifs04":     "ECMWF IFS 0.4°",
    # GFS
    "gfs_seamless":    "GFS Seamless",
    # Ensemble
    "ecmwf_ens":       "ECMWF ENS 51-member",
    "icon_eu_eps":     "ICON-EU EPS 40-member",
    # Regional
    "geosphere_arome": "GeoSphere AROME 2.5 km",
    "mosmix":          "DWD MOSMIX_L",
    # Backward-compat aliases (old source keys)
    "ecmwf_hres":      "ECMWF IFS HRES 0.25°",
    "icon_seamless":   "ICON Seamless (Open-Meteo blend)",
    "gfs":             "GFS 0.25°",
}

# ── Fallback chains ──
# Each entry: (source_key, api_endpoint, model_name, param_list)

ICON_CHAIN = [
    ("icon_d2",     "dwd-icon", "icon_d2",     ICON_D2_PARAMS),
    ("icon_eu",     "dwd-icon", "icon_eu",     ICON_PARAMS),
    ("icon_global", "dwd-icon", "icon_global", ICON_PARAMS),
]

ECMWF_CHAIN = [
    ("ecmwf_ifs025", "forecast", "ecmwf_ifs025", ECMWF_PARAMS),
    ("ecmwf_ifs04",  "forecast", "ecmwf_ifs04",  ECMWF_PARAMS),
]

GFS_CHAIN = [
    ("gfs_seamless", "gfs", "gfs_seamless", GFS_PARAMS),
]


# ══════════════════════════════════════════════
# Time Utilities
# ══════════════════════════════════════════════

def _local_to_utc(date_str: str, hour: int) -> datetime:
    """Convert local hour on date to UTC datetime."""
    from datetime import date as _date
    parts = date_str.split("-")
    d = _date(int(parts[0]), int(parts[1]), int(parts[2]))
    local_dt = datetime(d.year, d.month, d.day, hour, 0, 0, tzinfo=TZ_LOCAL)
    return local_dt.astimezone(TZ_UTC)


# ══════════════════════════════════════════════
# HTTP Utilities
# ══════════════════════════════════════════════

_MAX_RETRIES = 3
_RETRY_DELAY = 2  # seconds


def _fetch_with_retry(url: str, timeout: int = 30) -> bytes:
    """Fetch URL with retries on transient errors (SSL, connection, timeout)."""
    last_err = None
    for attempt in range(_MAX_RETRIES):
        try:
            req = Request(url, headers={"User-Agent": f"PG-Weather-Triage/{APP_VERSION}"})
            with urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except (URLError, OSError, TimeoutError) as e:
            last_err = e
            # Don't retry HTTP 4xx errors (bad request, not found, etc.)
            if hasattr(e, 'code') and 400 <= e.code < 500:
                raise
            if attempt < _MAX_RETRIES - 1:
                time.sleep(_RETRY_DELAY * (attempt + 1))
    raise last_err


def _fetch_json(url: str, timeout: int = 30) -> dict:
    return json.loads(_fetch_with_retry(url, timeout).decode())


def _fetch_bytes(url: str, timeout: int = 30) -> bytes:
    return _fetch_with_retry(url, timeout)


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


def _has_valid_data(result: dict, date: str) -> bool:
    """Check if API result contains actual non-null data for target date."""
    h = result.get("hourly", {})
    times = h.get("time", [])
    if not times:
        return False
    if not any(date in str(t) for t in times):
        return False
    for k, v in h.items():
        if k == "time":
            continue
        if isinstance(v, list) and any(x is not None for x in v):
            return True
    return False


# ══════════════════════════════════════════════
# Fallback Chain Fetcher
# ══════════════════════════════════════════════

def fetch_with_fallback(chain: list, lat: float, lon: float,
                        date: str) -> tuple[str | None, dict]:
    """Try models in chain order, return (key, data) for first success.

    Returns (None, {"error": ...}) if all fail.
    """
    errors = []
    for key, endpoint, model, params in chain:
        try:
            data = _fetch_openmeteo(endpoint, model, lat, lon, date, params)
            if _has_valid_data(data, date):
                print(f"    ✓ {key} ({MODEL_LABELS.get(key, key)})", file=sys.stderr)
                return key, data
            else:
                errors.append(f"{key}: no data for {date}")
                print(f"    ○ {key}: no data for {date}", file=sys.stderr)
        except Exception as e:
            errors.append(f"{key}: {e}")
            print(f"    ✗ {key}: {e}", file=sys.stderr)
    return None, {"error": f"All models failed: {'; '.join(errors)}"}


# ══════════════════════════════════════════════
# Wind Helper
# ══════════════════════════════════════════════

def wind_from_uv(u, v):
    speed = math.sqrt(u**2 + v**2)
    direction = (270 - math.degrees(math.atan2(v, u))) % 360
    return round(speed, 1), round(direction)


# ══════════════════════════════════════════════
# Ensemble Fetchers
# ══════════════════════════════════════════════

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

    local_map = {}
    for i, ts in enumerate(timestamps_utc):
        if date not in ts:
            continue
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
    hourly_local = {}
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
