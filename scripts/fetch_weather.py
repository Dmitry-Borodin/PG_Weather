#!/usr/bin/env python3
"""
Сбор прогнозных данных для метео-триажа XC closed routes.
Версия: 0.3

Источники:
  - Open-Meteo ICON-D2 (hi-res, 2 км)
  - Open-Meteo GFS (BL height, CAPE, CIN, LI)
  - Open-Meteo мультимодельное сравнение
  - GeoSphere Austria TAWES (текущие наблюдения)
  - GeoSphere NWP (прогноз CAPE, облачность, снеговая линия)
  - BrightSky (DWD wrapper)
  - DWD MOSMIX (114 параметров, облачность по уровням, радиация)
  - Meteo-Parapente (headless browser — термики, ветер hi-res)
  - XContest (headless browser — реальные полёты, sanity check)
  - ALPTHERM (headless browser — Thermikqualität, Австрия)

Выходные форматы:
  - JSON (машиночитаемые данные)
  - Markdown (человекочитаемый отчёт)
  - HTML viewer (index.html — единый интерфейс для всех отчётов)

Использование:
    python scripts/fetch_weather.py --date 2025-07-15
    python scripts/fetch_weather.py --date 2025-07-15 --locations lenggries,koessen
    python scripts/fetch_weather.py --date 2025-07-15 --sources all
"""

import argparse
import io
import json
import math
import subprocess
import shutil
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.parse import urlencode
from xml.etree import ElementTree as ET

# ──────────────────────────────────────────────
# Конфигурация районов
# ──────────────────────────────────────────────

LOCATIONS = {
    "lenggries":   {"lat": 47.68, "lon": 11.57, "elev": 700,  "peaks": 1800, "name": "Lenggries",   "geosphere_id": None,    "mosmix_id": "10963", "drive_h": 1.0},
    "wallberg":    {"lat": 47.64, "lon": 11.79, "elev": 1620, "peaks": 1722, "name": "Wallberg",    "geosphere_id": None,    "mosmix_id": "10963", "drive_h": 1.0},
    "koessen":     {"lat": 47.67, "lon": 12.40, "elev": 590,  "peaks": 1900, "name": "Kössen",      "geosphere_id": "11130", "mosmix_id": None,    "drive_h": 1.5},
    "innsbruck":   {"lat": 47.26, "lon": 11.39, "elev": 578,  "peaks": 2600, "name": "Innsbruck",   "geosphere_id": "11121", "mosmix_id": "11120", "drive_h": 2.0},
    "greifenburg": {"lat": 46.75, "lon": 13.18, "elev": 600,  "peaks": 2800, "name": "Greifenburg", "geosphere_id": "11204", "mosmix_id": None,    "drive_h": 4.0},
    "speikboden":  {"lat": 46.90, "lon": 11.87, "elev": 950,  "peaks": 2500, "name": "Speikboden",  "geosphere_id": None,    "mosmix_id": None,    "drive_h": 3.5},
    "bassano":     {"lat": 45.78, "lon": 11.73, "elev": 130,  "peaks": 1700, "name": "Bassano",     "geosphere_id": None,    "mosmix_id": None,    "drive_h": 5.0},
}

# GeoSphere Austria station IDs for observations
GEOSPHERE_STATIONS = {
    "11121": "Innsbruck Airport",
    "11320": "Innsbruck Uni",
    "11130": "Kufstein",
    "11204": "Lienz",
    "11279": "Kitzbühel",
    "11144": "Zell am See",
    "11364": "St. Johann im Pongau",
}

# Часы анализа для почасового профиля (Europe/Berlin)
ANALYSIS_HOURS = ["08:00", "09:00", "10:00", "11:00", "12:00",
                  "13:00", "14:00", "15:00", "16:00", "17:00", "18:00"]


def _fetch_json(url: str, timeout: int = 30) -> dict:
    """HTTP GET → JSON."""
    req = Request(url, headers={"User-Agent": "PG-Weather-Triage/0.3"})
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _fetch_bytes(url: str, timeout: int = 30) -> bytes:
    """HTTP GET → bytes."""
    req = Request(url, headers={"User-Agent": "PG-Weather-Triage/0.3"})
    with urlopen(req, timeout=timeout) as resp:
        return resp.read()


# ──────────────────────────────────────────────
# Источник 1: Open-Meteo ICON-D2 (hi-res)
# ──────────────────────────────────────────────

ICON_D2_PARAMS = [
    "temperature_2m", "dewpoint_2m",
    "windspeed_10m", "windgusts_10m", "winddirection_10m",
    "cloudcover", "cloudcover_low", "cloudcover_mid", "cloudcover_high",
    "precipitation", "cape",
    "temperature_850hPa", "temperature_700hPa",
    "windspeed_850hPa", "winddirection_850hPa",
    "windspeed_700hPa", "winddirection_700hPa",
]


def fetch_icon_d2(lat: float, lon: float, date: str) -> dict:
    """Open-Meteo ICON-D2 (2 км, до +48ч)."""
    params = {
        "latitude": lat, "longitude": lon,
        "hourly": ",".join(ICON_D2_PARAMS),
        "models": "icon_d2",
        "start_date": date, "end_date": date,
        "timezone": "Europe/Berlin",
        "windspeed_unit": "ms",
    }
    return _fetch_json(f"https://api.open-meteo.com/v1/dwd-icon?{urlencode(params)}")


# ──────────────────────────────────────────────
# Источник 2: Open-Meteo GFS (BL height, CAPE, CIN, LI)
# ──────────────────────────────────────────────

GFS_PARAMS = [
    "temperature_2m", "dewpoint_2m",
    "windspeed_10m", "windgusts_10m",
    "cape", "convective_inhibition", "lifted_index",
    "boundary_layer_height",
    "cloudcover", "precipitation",
    "temperature_850hPa", "temperature_700hPa", "temperature_500hPa",
    "windspeed_850hPa", "windspeed_700hPa",
    "winddirection_850hPa", "winddirection_700hPa",
]


def fetch_gfs(lat: float, lon: float, date: str) -> dict:
    """Open-Meteo GFS — единственная модель с BL height."""
    params = {
        "latitude": lat, "longitude": lon,
        "hourly": ",".join(GFS_PARAMS),
        "models": "gfs_seamless",
        "start_date": date, "end_date": date,
        "timezone": "Europe/Berlin",
        "windspeed_unit": "ms",
    }
    return _fetch_json(f"https://api.open-meteo.com/v1/gfs?{urlencode(params)}")


# ──────────────────────────────────────────────
# Источник 3: Open-Meteo мультимодельное сравнение
# ──────────────────────────────────────────────

MULTIMODEL_PARAMS = [
    "temperature_2m", "dewpoint_2m",
    "windspeed_10m", "windgusts_10m",
    "cape", "cloudcover", "precipitation",
]


def fetch_multimodel(lat: float, lon: float, date: str) -> dict:
    """ICON + GFS + ECMWF одним запросом."""
    params = {
        "latitude": lat, "longitude": lon,
        "hourly": ",".join(MULTIMODEL_PARAMS),
        "models": "icon_seamless,gfs_seamless,ecmwf_ifs025",
        "start_date": date, "end_date": date,
        "timezone": "Europe/Berlin",
        "windspeed_unit": "ms",
    }
    return _fetch_json(f"https://api.open-meteo.com/v1/forecast?{urlencode(params)}")


# ──────────────────────────────────────────────
# Источник 4: GeoSphere Austria — текущие наблюдения
# ──────────────────────────────────────────────

def fetch_geosphere_observations() -> dict:
    """Текущие данные со всех отслеживаемых станций."""
    ids = ",".join(GEOSPHERE_STATIONS.keys())
    url = f"https://dataset.api.hub.geosphere.at/v1/station/current/tawes-v1-10min?parameters=TL,FF,FFX,DD,RF&station_ids={ids}"
    return _fetch_json(url)


# ──────────────────────────────────────────────
# Источник 5: GeoSphere NWP (AROME-based 2.5 км)
# ──────────────────────────────────────────────

def fetch_geosphere_nwp(lat: float, lon: float) -> dict:
    """GeoSphere NWP прогноз: CAPE, CIN, облачность, ветер, снеговая линия."""
    url = (
        f"https://dataset.api.hub.geosphere.at/v1/timeseries/forecast/nwp-v1-1h-2500m"
        f"?parameters=t2m,cape,cin,tcc,u10m,v10m,ugust,vgust,snowlmt,rr_acc,grad"
        f"&lat_lon={lat},{lon}&output_format=geojson"
    )
    return _fetch_json(url)


# ──────────────────────────────────────────────
# Источник 6: BrightSky (DWD wrapper)
# ──────────────────────────────────────────────

def fetch_brightsky(lat: float, lon: float, date: str) -> dict:
    """BrightSky — удобный JSON wrapper для DWD MOSMIX."""
    url = f"https://api.brightsky.dev/weather?lat={lat}&lon={lon}&date={date}"
    return _fetch_json(url)


# ──────────────────────────────────────────────
# Источник 7: DWD MOSMIX (KMZ → XML)
# ──────────────────────────────────────────────

MOSMIX_PARAMS_OF_INTEREST = [
    "TTT",   # Temperature 2m (K)
    "Td",    # Dewpoint (K)
    "FF",    # Wind speed (m/s)
    "FX1",   # Max gust 1h (m/s)
    "DD",    # Wind direction (°)
    "N",     # Total cloud cover (%)
    "Neff",  # Effective cloud cover (%)
    "Nh",    # High cloud cover (%)
    "Nm",    # Mid cloud cover (%)
    "Nl",    # Low cloud cover (%)
    "PPPP",  # Pressure (Pa)
    "SunD1", # Sunshine duration 1h (s)
    "Rad1h", # Global irradiance 1h (kJ/m²)
    "RR1c",  # Precipitation 1h (kg/m²)
    "wwP",   # Weather probability (%)
    "R101",  # Precip prob >0.1mm (%)
]


def fetch_mosmix(station_id: str, date: str) -> dict:
    """Скачать MOSMIX_L KMZ для станции и извлечь прогноз на дату."""
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

    # Timestamps
    time_steps = root.findall(".//dwd:ForecastTimeSteps/dwd:TimeStep", ns)
    timestamps = [ts.text for ts in time_steps]

    # Find indices for target date
    target_indices = {}
    for i, ts in enumerate(timestamps):
        if date in ts:
            hour_part = ts.split("T")[1][:5]  # "06:00"
            target_indices[hour_part] = i

    if not target_indices:
        return {"error": f"No data for date {date}", "station": station_id}

    # Extract parameters
    result = {"station": station_id, "timestamps": {}, "hourly": {}}
    result["timestamps"] = {h: timestamps[i] for h, i in target_indices.items()}

    placemark = root.find(".//kml:Placemark", ns)
    if placemark is None:
        return {"error": "No Placemark in KML", "station": station_id}

    station_name = placemark.findtext("kml:name", default=station_id, namespaces=ns)
    result["station_name"] = station_name.strip()

    forecasts = placemark.findall(".//dwd:Forecast", ns)
    for fc in forecasts:
        param_name = fc.get(f"{{{ns['dwd']}}}elementName")
        if param_name not in MOSMIX_PARAMS_OF_INTEREST:
            continue

        value_text = fc.findtext("dwd:value", default="", namespaces=ns)
        values = value_text.split()

        hourly_vals = {}
        for hour, idx in target_indices.items():
            if idx < len(values):
                raw_val = values[idx].strip()
                if raw_val == "-999.00" or raw_val == "-":
                    hourly_vals[hour] = None
                else:
                    try:
                        v = float(raw_val)
                        if param_name in ("TTT", "Td"):
                            v = round(v - 273.15, 1)
                        elif param_name == "PPPP":
                            v = round(v / 100, 1)
                        else:
                            v = round(v, 1)
                        hourly_vals[hour] = v
                    except ValueError:
                        hourly_vals[hour] = None
            else:
                hourly_vals[hour] = None

        result["hourly"][param_name] = hourly_vals

    return result


# ──────────────────────────────────────────────
# Вычисления
# ──────────────────────────────────────────────

def estimate_cloudbase_msl(temp_c: float, dewpoint_c: float, station_elev_m: float) -> float:
    """LCL ≈ 125 × (T − Td) + elevation."""
    return 125.0 * (temp_c - dewpoint_c) + station_elev_m


def lapse_rate(t850: float, t700: float) -> float:
    """Lapse rate 850→700 гПа [°C/km]. Типичная Δz ≈ 1.5 км."""
    return (t850 - t700) / 1.5


def wind_from_uv(u: float, v: float) -> tuple:
    """(speed m/s, direction °) from u,v components."""
    speed = math.sqrt(u**2 + v**2)
    direction = (270 - math.degrees(math.atan2(v, u))) % 360
    return round(speed, 1), round(direction)


def foehn_check(obs: dict) -> dict:
    """Простой фён-чекер по наблюдениям Innsbruck."""
    flags = []
    dd = obs.get("DD")
    ffx = obs.get("FFX")
    rf = obs.get("RF")
    t = obs.get("T")

    if dd is not None and 150 <= dd <= 230:
        flags.append(f"wind_from_south ({dd}°)")
    if ffx is not None and ffx > 8:
        flags.append(f"strong_gusts ({ffx:.1f} m/s)")
    if rf is not None and rf < 40:
        flags.append(f"dry_air (RH={rf:.0f}%)")
    if t is not None and t > 15:
        flags.append(f"warm ({t:.1f}°C)")

    return {
        "foehn_likely": len(flags) >= 3,
        "foehn_possible": len(flags) >= 2,
        "flags": flags,
    }


# ──────────────────────────────────────────────
# Почасовой анализ термического окна
# ──────────────────────────────────────────────

def analyze_hourly_window(hourly_icon: dict, hourly_gfs: dict, loc: dict) -> dict:
    """Анализ почасового профиля для определения термического окна."""
    profile = []
    times_icon = hourly_icon.get("time", [])
    times_gfs = hourly_gfs.get("time", [])

    def _get(hourly, times, key, hour_str):
        for i, t in enumerate(times):
            if hour_str in t:
                vals = hourly.get(key, [])
                return vals[i] if i < len(vals) else None
        return None

    for hour in ANALYSIS_HOURS:
        t2m = _get(hourly_icon, times_icon, "temperature_2m", hour)
        td = _get(hourly_icon, times_icon, "dewpoint_2m", hour)
        cloud = _get(hourly_icon, times_icon, "cloudcover", hour)
        precip = _get(hourly_icon, times_icon, "precipitation", hour)
        ws850 = _get(hourly_icon, times_icon, "windspeed_850hPa", hour)
        gusts = _get(hourly_icon, times_icon, "windgusts_10m", hour)
        t850 = _get(hourly_icon, times_icon, "temperature_850hPa", hour)
        t700 = _get(hourly_icon, times_icon, "temperature_700hPa", hour)

        # Fallback to GFS
        if t2m is None:
            t2m = _get(hourly_gfs, times_gfs, "temperature_2m", hour)
        if td is None:
            td = _get(hourly_gfs, times_gfs, "dewpoint_2m", hour)
        if cloud is None:
            cloud = _get(hourly_gfs, times_gfs, "cloudcover", hour)
        if precip is None:
            precip = _get(hourly_gfs, times_gfs, "precipitation", hour)
        if ws850 is None:
            ws850 = _get(hourly_gfs, times_gfs, "windspeed_850hPa", hour)
        if gusts is None:
            gusts = _get(hourly_gfs, times_gfs, "windgusts_10m", hour)
        if t850 is None:
            t850 = _get(hourly_gfs, times_gfs, "temperature_850hPa", hour)
        if t700 is None:
            t700 = _get(hourly_gfs, times_gfs, "temperature_700hPa", hour)

        bl = _get(hourly_gfs, times_gfs, "boundary_layer_height", hour)
        cape = _get(hourly_gfs, times_gfs, "cape", hour)

        base_msl = None
        if t2m is not None and td is not None:
            base_msl = round(estimate_cloudbase_msl(t2m, td, loc["elev"]))

        lr = None
        if t850 is not None and t700 is not None:
            lr = round(lapse_rate(t850, t700), 1)

        profile.append({
            "hour": hour,
            "temp_2m": t2m,
            "dewpoint": td,
            "cloudbase_msl": base_msl,
            "cloudcover": cloud,
            "precipitation": precip,
            "wind_850": ws850,
            "gusts": gusts,
            "lapse_rate": lr,
            "bl_height": bl,
            "cape": cape,
        })

    # Определение термического окна
    thermal_hours = []
    for p in profile:
        h = int(p["hour"].split(":")[0])
        if h < 9:
            continue
        is_thermal = True
        if p["cloudcover"] is not None and p["cloudcover"] > 70:
            is_thermal = False
        if p["precipitation"] is not None and p["precipitation"] > 0.3:
            is_thermal = False
        if is_thermal:
            thermal_hours.append(p)

    window = {"start": None, "end": None, "peak_hour": None,
              "duration_h": 0, "peak_lapse": None, "peak_cape": None}

    if thermal_hours:
        window["start"] = thermal_hours[0]["hour"]
        window["end"] = thermal_hours[-1]["hour"]
        window["duration_h"] = len(thermal_hours)

        best_lr = 0
        best_cape = 0
        peak_h = thermal_hours[0]["hour"]
        for th in thermal_hours:
            lr_val = th.get("lapse_rate") or 0
            cape_val = th.get("cape") or 0
            if lr_val > best_lr or (lr_val == best_lr and cape_val > best_cape):
                best_lr = lr_val
                best_cape = cape_val
                peak_h = th["hour"]
        window["peak_hour"] = peak_h
        window["peak_lapse"] = best_lr if best_lr > 0 else None
        window["peak_cape"] = best_cape if best_cape > 0 else None

    return {
        "hourly_profile": profile,
        "thermal_window": window,
    }


# ──────────────────────────────────────────────
# Сборка данных для одной локации
# ──────────────────────────────────────────────

def _get_at_hour(hourly: dict, hour: str, key: str):
    """Извлечь значение из hourly данных для конкретного часа."""
    times = hourly.get("time", [])
    values = hourly.get(key, [])
    for i, t in enumerate(times):
        if hour in t:
            return values[i] if i < len(values) else None
    return None


def assess_location(loc_key: str, loc: dict, date: str, sources: list) -> dict:
    """Сбор данных из всех источников для одной локации."""
    result = {
        "location": loc["name"],
        "key": loc_key,
        "date": date,
        "drive_h": loc.get("drive_h", "?"),
        "sources": {},
        "assessment": {},
    }

    lat, lon = loc["lat"], loc["lon"]
    hourly_icon = {}
    hourly_gfs = {}

    # --- ICON-D2 ---
    if "icon_d2" in sources:
        try:
            d = fetch_icon_d2(lat, lon, date)
            h = d.get("hourly", {})
            hourly_icon = h
            at13 = {}
            for p in ICON_D2_PARAMS:
                at13[p] = _get_at_hour(h, "13:00", p)
            result["sources"]["icon_d2"] = {"at_13": at13, "hourly": h}
        except Exception as e:
            result["sources"]["icon_d2"] = {"error": str(e)}

    # --- GFS ---
    if "gfs" in sources:
        try:
            d = fetch_gfs(lat, lon, date)
            h = d.get("hourly", {})
            hourly_gfs = h
            at13 = {}
            for p in GFS_PARAMS:
                at13[p] = _get_at_hour(h, "13:00", p)
            result["sources"]["gfs"] = {"at_13": at13, "hourly": h}
        except Exception as e:
            result["sources"]["gfs"] = {"error": str(e)}

    # --- Multi-model ---
    if "multimodel" in sources:
        try:
            d = fetch_multimodel(lat, lon, date)
            h = d.get("hourly", {})
            at13 = {}
            for key in h:
                if key != "time":
                    at13[key] = _get_at_hour(h, "13:00", key)
            result["sources"]["multimodel"] = {"at_13": at13}
        except Exception as e:
            result["sources"]["multimodel"] = {"error": str(e)}

    # --- GeoSphere NWP ---
    if "geosphere_nwp" in sources:
        try:
            d = fetch_geosphere_nwp(lat, lon)
            timestamps = d.get("timestamps", [])
            features = d.get("features", [])
            at12 = {}
            if features:
                params = features[0].get("properties", {}).get("parameters", {})
                for i, t in enumerate(timestamps):
                    if "12:00" in t or "13:00" in t:
                        for pname, pdata in params.items():
                            vals = pdata.get("data", [])
                            at12[pname] = vals[i] if i < len(vals) else None
                        u = at12.get("u10m")
                        v = at12.get("v10m")
                        if u is not None and v is not None:
                            ws, wd = wind_from_uv(u, v)
                            at12["wind_speed_10m"] = ws
                            at12["wind_dir_10m"] = wd
                        ug = at12.get("ugust")
                        vg = at12.get("vgust")
                        if ug is not None and vg is not None:
                            gs, _ = wind_from_uv(ug, vg)
                            at12["gust_speed"] = gs
                        break
            result["sources"]["geosphere_nwp"] = {"at_13_approx": at12, "time": "nearest 12/13 UTC"}
        except Exception as e:
            result["sources"]["geosphere_nwp"] = {"error": str(e)}

    # --- BrightSky ---
    if "brightsky" in sources:
        try:
            d = fetch_brightsky(lat, lon, date)
            weather = d.get("weather", [])
            at13 = {}
            for w in weather:
                if "13:00" in w.get("timestamp", ""):
                    at13 = w
                    break
            result["sources"]["brightsky"] = {"at_13": at13}
        except Exception as e:
            result["sources"]["brightsky"] = {"error": str(e)}

    # --- DWD MOSMIX ---
    if "mosmix" in sources and loc.get("mosmix_id"):
        try:
            d = fetch_mosmix(loc["mosmix_id"], date)
            if "error" not in d:
                at13 = {}
                for param_name, hourly_vals in d.get("hourly", {}).items():
                    at13[param_name] = hourly_vals.get("12:00") or hourly_vals.get("15:00")
                result["sources"]["mosmix"] = {
                    "station_name": d.get("station_name", "?"),
                    "at_13": at13,
                    "hourly": d.get("hourly", {}),
                }
            else:
                result["sources"]["mosmix"] = {"error": d["error"]}
        except Exception as e:
            result["sources"]["mosmix"] = {"error": str(e)}

    # --- Почасовой анализ ---
    hourly_analysis = analyze_hourly_window(hourly_icon, hourly_gfs, loc)
    result["hourly_analysis"] = hourly_analysis

    # --- Основная оценка (ICON-D2 + GFS) ---
    icon = result["sources"].get("icon_d2", {}).get("at_13", {})
    gfs_data = result["sources"].get("gfs", {}).get("at_13", {})

    t2m = icon.get("temperature_2m") or gfs_data.get("temperature_2m")
    td2m = icon.get("dewpoint_2m") or gfs_data.get("dewpoint_2m")
    ws850 = icon.get("windspeed_850hPa") or gfs_data.get("windspeed_850hPa")
    ws700 = icon.get("windspeed_700hPa") or gfs_data.get("windspeed_700hPa")
    gusts = icon.get("windgusts_10m") or gfs_data.get("windgusts_10m")
    cape = icon.get("cape") or gfs_data.get("cape")
    t850 = icon.get("temperature_850hPa") or gfs_data.get("temperature_850hPa")
    t700 = icon.get("temperature_700hPa") or gfs_data.get("temperature_700hPa")
    cloud = icon.get("cloudcover")
    precip = icon.get("precipitation") or gfs_data.get("precipitation")
    bl_h = gfs_data.get("boundary_layer_height")
    li = gfs_data.get("lifted_index")
    cin = gfs_data.get("convective_inhibition")

    cloudbase_msl = None
    if t2m is not None and td2m is not None:
        cloudbase_msl = round(estimate_cloudbase_msl(t2m, td2m, loc["elev"]))

    lr = None
    if t850 is not None and t700 is not None:
        lr = round(lapse_rate(t850, t700), 1)

    base_margin = None
    if cloudbase_msl is not None:
        base_margin = cloudbase_msl - loc["peaks"]

    assessment = {
        "temp_2m": t2m,
        "dewpoint_2m": td2m,
        "cloudbase_msl": cloudbase_msl,
        "base_margin_over_peaks": base_margin,
        "wind_850hPa_ms": ws850,
        "wind_700hPa_ms": ws700,
        "gusts_10m_ms": gusts,
        "cape_J_per_kg": cape,
        "cin_J_per_kg": cin,
        "lifted_index": li,
        "boundary_layer_height_m": bl_h,
        "lapse_rate_C_per_km": lr,
        "cloudcover_pct": cloud,
        "precipitation_mm": precip,
    }

    # Термическое окно
    tw = hourly_analysis.get("thermal_window", {})
    assessment["thermal_window_start"] = tw.get("start")
    assessment["thermal_window_end"] = tw.get("end")
    assessment["thermal_window_hours"] = tw.get("duration_h", 0)
    assessment["thermal_window_peak"] = tw.get("peak_hour")

    # ── Стоп-фильтры ──
    flags = []
    if ws850 is not None and ws850 > 5.0:
        flags.append(("WIND_850", f"{ws850:.1f} m/s > 5.0 (порог для closed route)"))
    if gusts is not None and gusts > 10.0:
        flags.append(("GUSTS", f"{gusts:.1f} m/s > 10.0"))
    if base_margin is not None and base_margin < 1000:
        flags.append(("LOW_BASE", f"{cloudbase_msl}m MSL, margin {base_margin}m < 1000m over {loc['peaks']}m peaks"))
    if precip is not None and precip > 0.5:
        flags.append(("PRECIP", f"{precip:.1f} mm/h @13:00"))
    if cloud is not None and cloud > 80:
        flags.append(("OVERCAST", f"{cloud:.0f}%"))
    if lr is not None and lr < 5.5:
        flags.append(("STABLE", f"lapse rate {lr}°C/km < 5.5 (weak thermals)"))
    if cape is not None and cape > 1500:
        flags.append(("HIGH_CAPE", f"{cape} J/kg — risk of overdevelopment/storms"))
    if li is not None and li < -4:
        flags.append(("VERY_UNSTABLE", f"LI={li} — storm risk"))
    if tw.get("duration_h", 0) > 0 and tw["duration_h"] < 5:
        flags.append(("SHORT_WINDOW", f"thermal window {tw['duration_h']}h < 5h"))

    # ── Позитивные признаки ──
    positives = []
    if lr is not None and lr > 7.0:
        positives.append(("STRONG_LAPSE", f"{lr}°C/km"))
    if cape is not None and 300 < cape < 1500:
        positives.append(("GOOD_CAPE", f"{cape} J/kg"))
    if bl_h is not None and bl_h > 1500:
        positives.append(("DEEP_BL", f"{bl_h}m"))
    if cloudbase_msl is not None and base_margin is not None and base_margin > 1500:
        positives.append(("HIGH_BASE", f"{cloudbase_msl}m MSL (+{base_margin}m over peaks)"))
    if tw.get("duration_h", 0) >= 7:
        positives.append(("LONG_WINDOW", f"{tw['duration_h']}h thermal window"))
    if cloud is not None and cloud < 30:
        positives.append(("CLEAR_SKY", f"{cloud:.0f}%"))

    # ── Composite scoring ──
    CRITICAL_TAGS = {"WIND_850", "GUSTS", "PRECIP"}
    QUALITY_TAGS = {"OVERCAST", "STABLE", "SHORT_WINDOW"}

    n_critical = sum(1 for tag, _ in flags if tag in CRITICAL_TAGS)
    n_quality = sum(1 for tag, _ in flags if tag in QUALITY_TAGS)
    n_danger = sum(1 for tag, _ in flags if tag in ("HIGH_CAPE", "VERY_UNSTABLE"))
    n_base = sum(1 for tag, _ in flags if tag == "LOW_BASE")
    n_positive = len(positives)

    score = 0
    score -= n_critical * 3
    score -= n_base * 2
    score -= n_quality * 1
    score -= n_danger * 1
    score += n_positive * 2

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

    if n_critical >= 2 or (n_critical >= 1 and n_base >= 1):
        status = "NO-GO"
    elif n_critical >= 1 and status in ("GO", "STRONG"):
        status = "MAYBE"

    assessment["flags"] = [{"tag": t, "msg": m} for t, m in flags]
    assessment["positives"] = [{"tag": t, "msg": m} for t, m in positives]
    assessment["score"] = score
    assessment["status"] = status
    result["assessment"] = assessment

    return result


# ══════════════════════════════════════════════
# CONSOLE OUTPUT
# ══════════════════════════════════════════════

STATUS_EMOJI = {
    "NO-GO": "🔴", "UNLIKELY": "🟠", "MAYBE": "🟡",
    "GO": "🟢", "STRONG": "💚",
}

STATUS_ORDER = {"STRONG": 0, "GO": 1, "MAYBE": 2, "UNLIKELY": 3, "NO-GO": 4}


def print_triage(results: list):
    """Краткий вывод в stdout."""
    print("\n" + "=" * 70)
    print("  QUICK TRIAGE — XC Closed Route Assessment")
    print("=" * 70)

    sorted_results = sorted(results, key=lambda r: STATUS_ORDER.get(
        r.get("assessment", {}).get("status", "NO-GO"), 5))

    for r in sorted_results:
        a = r.get("assessment", {})
        if "error" in r and "assessment" not in r:
            print(f"\n  {r['location']}: ERROR — {r['error']}")
            continue

        status = a.get("status", "?")
        base = a.get("cloudbase_msl", "?")
        margin = a.get("base_margin_over_peaks", "?")
        ws850 = a.get("wind_850hPa_ms", "?")
        gusts_val = a.get("gusts_10m_ms", "?")
        cape_val = a.get("cape_J_per_kg", "?")
        lr_val = a.get("lapse_rate_C_per_km", "?")
        bl = a.get("boundary_layer_height_m", "?")
        li_val = a.get("lifted_index", "?")
        tw_start = a.get("thermal_window_start", "—")
        tw_end = a.get("thermal_window_end", "—")
        tw_h = a.get("thermal_window_hours", 0)

        emoji = STATUS_EMOJI.get(status, "⚪")

        print(f"\n  {emoji} {r['location']:15s} [{status}]  (score: {a.get('score', '?')})")
        print(f"     Base: {base}m MSL (margin: {margin}m over peaks)")
        print(f"     Wind @850hPa: {ws850}m/s  |  Gusts: {gusts_val}m/s")
        print(f"     CAPE: {cape_val}J/kg  |  LI: {li_val}  |  Lapse: {lr_val}°C/km  |  BL: {bl}m")
        if tw_h > 0:
            print(f"     Thermal window: {tw_start}–{tw_end} ({tw_h}h)")
        else:
            print(f"     Thermal window: none detected")

        for f in a.get("flags", []):
            print(f"     ⚠ {f['tag']}: {f['msg']}")
        for p in a.get("positives", []):
            print(f"     ✓ {p['tag']}: {p['msg']}")

    # Мультимодельное сравнение
    has_multi = any("multimodel" in r.get("sources", {}) for r in results)
    if has_multi:
        print(f"\n{'─' * 70}")
        print("  MODEL COMPARISON @13:00")
        print(f"{'─' * 70}")
        for r in sorted_results:
            mm = r.get("sources", {}).get("multimodel", {}).get("at_13", {})
            if mm:
                print(f"\n  {r['location']}:")
                models = ["icon_seamless", "gfs_seamless", "ecmwf_ifs025"]
                for param_base in ["temperature_2m", "cape", "cloudcover", "windspeed_10m"]:
                    vals = []
                    for m in models:
                        key = f"{param_base}_{m}"
                        v = mm.get(key)
                        vals.append(f"{v}" if v is not None else "—")
                    print(f"     {param_base:20s}  ICON={vals[0]:>8s}  GFS={vals[1]:>8s}  ECMWF={vals[2]:>8s}")

    print(f"\n{'=' * 70}\n")


def get_observations_data() -> tuple:
    """Fetch and process GeoSphere observations. Returns (obs_info dict, raw data)."""
    try:
        obs_raw = fetch_geosphere_observations()
        return obs_raw, True
    except Exception as e:
        print(f"\n  GeoSphere observations: ERROR — {e}", file=sys.stderr)
        return {}, False


def print_observations(obs_data: dict) -> dict:
    """Текущие наблюдения со станций Австрии + фён-чек. Returns obs_info."""
    foehn_result = {"status": "unknown", "flags": []}
    try:
        features = obs_data.get("features", [])
        timestamps = obs_data.get("timestamps", [])
        ts = timestamps[-1] if timestamps else "?"

        print(f"\n{'─' * 70}")
        print(f"  CURRENT OBSERVATIONS (GeoSphere Austria) — {ts}")
        print(f"{'─' * 70}")

        innsbruck_obs = {}
        obs_list = []
        for feat in features:
            props = feat["properties"]
            sid = props.get("station", "?")
            sname = GEOSPHERE_STATIONS.get(sid, sid)
            params_data = props.get("parameters", {})

            t_val = params_data.get("TL", {}).get("data", [None])[0]
            ff = params_data.get("FF", {}).get("data", [None])[0]
            ffx = params_data.get("FFX", {}).get("data", [None])[0]
            dd = params_data.get("DD", {}).get("data", [None])[0]
            rf = params_data.get("RF", {}).get("data", [None])[0]

            def _fmt(v, unit=""):
                return f"{v}{unit}" if v is not None else "—"

            print(f"  {sname:30s}  T={_fmt(t_val, '°C')}  FF={_fmt(ff, 'm/s')}  FFX={_fmt(ffx, 'm/s')}  DD={_fmt(dd, '°')}  RH={_fmt(rf, '%')}")

            obs_list.append({"station": sname, "station_id": sid,
                             "T": t_val, "FF": ff, "FFX": ffx, "DD": dd, "RF": rf})

            if sid in ("11121", "11320"):
                innsbruck_obs = {"T": t_val, "FF": ff, "FFX": ffx, "DD": dd, "RF": rf}

        if innsbruck_obs:
            fc = foehn_check(innsbruck_obs)
            foehn_result = fc
            if fc["foehn_likely"]:
                print(f"\n  🔴 FOEHN LIKELY at Innsbruck: {', '.join(fc['flags'])}")
            elif fc["foehn_possible"]:
                print(f"\n  🟡 FOEHN POSSIBLE at Innsbruck: {', '.join(fc['flags'])}")
            else:
                print(f"\n  🟢 No foehn indicators at Innsbruck")

        return {"timestamp": ts, "stations": obs_list, "foehn": foehn_result}

    except Exception as e:
        print(f"\n  GeoSphere observations: ERROR — {e}", file=sys.stderr)
        return {"timestamp": "?", "stations": [], "foehn": foehn_result}


# ══════════════════════════════════════════════
# MARKDOWN REPORT
# ══════════════════════════════════════════════

def _v(val, unit="", precision=1):
    """Format value for display."""
    if val is None:
        return "—"
    if isinstance(val, float):
        return f"{val:.{precision}f}{unit}"
    return f"{val}{unit}"


def generate_markdown_report(results: list, obs_info: dict, date: str, gen_time: str) -> str:
    """Генерация Markdown-отчёта."""
    lines = []
    lines.append(f"# ✈️ PG Weather Triage — {date}")
    lines.append("")
    lines.append(f"*Generated: {gen_time}*")
    lines.append("")

    sorted_r = sorted(results, key=lambda r: STATUS_ORDER.get(
        r.get("assessment", {}).get("status", "NO-GO"), 5))

    # ── Summary table ──
    lines.append("## 📊 Summary")
    lines.append("")
    lines.append("| Location | Drive | Status | Base @13 | Margin | Wind 850 | Gusts | CAPE | Lapse | BL | Window |")
    lines.append("|----------|-------|--------|----------|--------|----------|-------|------|-------|----|--------|")

    for r in sorted_r:
        a = r.get("assessment", {})
        s = a.get("status", "?")
        emoji = STATUS_EMOJI.get(s, "⚪")
        tw_h = a.get("thermal_window_hours", 0)
        tw_str = f"{a.get('thermal_window_start','')}-{a.get('thermal_window_end','')}" if tw_h > 0 else "—"
        lines.append(
            f"| {r['location']} | {_v(r.get('drive_h'),'h')} "
            f"| {emoji} **{s}** "
            f"| {_v(a.get('cloudbase_msl'),'m')} "
            f"| {_v(a.get('base_margin_over_peaks'),'m')} "
            f"| {_v(a.get('wind_850hPa_ms'),'m/s')} "
            f"| {_v(a.get('gusts_10m_ms'),'m/s')} "
            f"| {_v(a.get('cape_J_per_kg'),'',0)} "
            f"| {_v(a.get('lapse_rate_C_per_km'),'°C/km')} "
            f"| {_v(a.get('boundary_layer_height_m'),'m',0)} "
            f"| {tw_str} |"
        )
    lines.append("")

    # ── Observations ──
    if obs_info.get("stations"):
        lines.append("## 🌡️ Current Observations (GeoSphere Austria)")
        lines.append(f"*{obs_info.get('timestamp', '?')}*")
        lines.append("")
        lines.append("| Station | T | Wind | Gusts | Dir | RH |")
        lines.append("|---------|---|------|-------|-----|----|")
        for st in obs_info.get("stations", []):
            lines.append(
                f"| {st['station']} "
                f"| {_v(st['T'],'°C')} "
                f"| {_v(st['FF'],'m/s')} "
                f"| {_v(st['FFX'],'m/s')} "
                f"| {_v(st['DD'],'°',0)} "
                f"| {_v(st['RF'],'%',0)} |"
            )

        fc = obs_info.get("foehn", {})
        if fc.get("foehn_likely"):
            lines.append(f"\n🔴 **FOEHN LIKELY** at Innsbruck: {', '.join(fc['flags'])}")
        elif fc.get("foehn_possible"):
            lines.append(f"\n🟡 **Foehn possible** at Innsbruck: {', '.join(fc['flags'])}")
        else:
            lines.append(f"\n🟢 No foehn indicators at Innsbruck")
        lines.append("")

    # ── Location details ──
    lines.append("---")
    lines.append("")

    for r in sorted_r:
        a = r.get("assessment", {})
        s = a.get("status", "?")
        emoji = STATUS_EMOJI.get(s, "⚪")

        lines.append(f"## {emoji} {r['location']} — **{s}** (score: {a.get('score','?')})")
        lines.append("")

        lines.append("### Key Metrics @13:00")
        loc_data = LOCATIONS.get(r.get("key"), {})
        lines.append(f"- **Cloud Base**: {_v(a.get('cloudbase_msl'),'m')} MSL "
                     f"(margin: {_v(a.get('base_margin_over_peaks'),'m')} over {loc_data.get('peaks','?')}m peaks)")
        lines.append(f"- **Wind @850hPa**: {_v(a.get('wind_850hPa_ms'),'m/s')}  |  "
                     f"**@700hPa**: {_v(a.get('wind_700hPa_ms'),'m/s')}")
        lines.append(f"- **Gusts**: {_v(a.get('gusts_10m_ms'),'m/s')}")
        lines.append(f"- **CAPE**: {_v(a.get('cape_J_per_kg'),'J/kg',0)}  |  "
                     f"**LI**: {_v(a.get('lifted_index'))}  |  "
                     f"**CIN**: {_v(a.get('cin_J_per_kg'),'J/kg',0)}")
        lines.append(f"- **Lapse rate**: {_v(a.get('lapse_rate_C_per_km'),'°C/km')}  |  "
                     f"**BL height**: {_v(a.get('boundary_layer_height_m'),'m',0)}")
        lines.append(f"- **Cloud cover**: {_v(a.get('cloudcover_pct'),'%',0)}  |  "
                     f"**Precip**: {_v(a.get('precipitation_mm'),'mm')}")
        tw_h = a.get("thermal_window_hours", 0)
        if tw_h > 0:
            lines.append(f"- **Thermal window**: {a.get('thermal_window_start')}–"
                         f"{a.get('thermal_window_end')} ({tw_h}h), peak @{a.get('thermal_window_peak')}")
        else:
            lines.append("- **Thermal window**: none detected")
        lines.append("")

        if a.get("flags"):
            lines.append("**⚠ Warnings:**")
            for f in a["flags"]:
                lines.append(f"- {f['tag']}: {f['msg']}")
            lines.append("")

        if a.get("positives"):
            lines.append("**✓ Positive indicators:**")
            for p in a["positives"]:
                lines.append(f"- {p['tag']}: {p['msg']}")
            lines.append("")

        # Hourly profile
        hp = r.get("hourly_analysis", {}).get("hourly_profile", [])
        if hp:
            lines.append("### Hourly Profile")
            lines.append("| Hour | T | Base MSL | Cloud | Precip | W850 | Gusts | Lapse | BL | CAPE |")
            lines.append("|------|---|----------|-------|--------|------|-------|-------|----|------|")
            for h_item in hp:
                lines.append(
                    f"| {h_item['hour']} "
                    f"| {_v(h_item['temp_2m'],'°C')} "
                    f"| {_v(h_item['cloudbase_msl'],'m',0)} "
                    f"| {_v(h_item['cloudcover'],'%',0)} "
                    f"| {_v(h_item['precipitation'],'mm')} "
                    f"| {_v(h_item['wind_850'],'',1)} "
                    f"| {_v(h_item['gusts'],'',1)} "
                    f"| {_v(h_item['lapse_rate'],'',1)} "
                    f"| {_v(h_item['bl_height'],'',0)} "
                    f"| {_v(h_item['cape'],'',0)} |"
                )
            lines.append("")

        # MOSMIX
        mosmix = r.get("sources", {}).get("mosmix", {})
        if mosmix and "error" not in mosmix:
            lines.append(f"### DWD MOSMIX ({mosmix.get('station_name','?')})")
            mh = mosmix.get("hourly", {})
            if mh:
                all_hours = sorted(set().union(*(v.keys() for v in mh.values())))
                target_hours = [h for h in all_hours if h in ("06:00", "09:00", "12:00", "15:00", "18:00")]
                if target_hours:
                    param_list = [p for p in MOSMIX_PARAMS_OF_INTEREST if p in mh]
                    lines.append("| Param | " + " | ".join(target_hours) + " |")
                    lines.append("|-------" + "|------" * len(target_hours) + "|")
                    for p in param_list[:12]:
                        vals = [_v(mh[p].get(h)) for h in target_hours]
                        lines.append(f"| {p} | " + " | ".join(vals) + " |")
            lines.append("")

        # Model comparison
        mm = r.get("sources", {}).get("multimodel", {}).get("at_13", {})
        if mm:
            lines.append("### Model Comparison @13:00")
            lines.append("| Parameter | ICON | GFS | ECMWF |")
            lines.append("|-----------|------|-----|-------|")
            models = ["icon_seamless", "gfs_seamless", "ecmwf_ifs025"]
            for param_base in ["temperature_2m", "dewpoint_2m", "cape", "cloudcover",
                               "windspeed_10m", "windgusts_10m", "precipitation"]:
                vals = [_v(mm.get(f"{param_base}_{m}")) for m in models]
                lines.append(f"| {param_base} | " + " | ".join(vals) + " |")
            lines.append("")

        lines.append("---")
        lines.append("")

    return "\n".join(lines)



# ══════════════════════════════════════════════
# HTML VIEWER
# ══════════════════════════════════════════════


def generate_viewer_html(reports_dir: Path):
    """Generate self-contained HTML viewer (index.html) embedding all JSON reports."""
    json_files = sorted(reports_dir.glob("*.json"), reverse=True)

    all_reports = {}
    for jf in json_files:
        try:
            with open(jf, "r", encoding="utf-8") as f:
                all_reports[jf.stem] = json.load(f)
        except Exception:
            continue

    if not all_reports:
        print("  No JSON reports found, skipping viewer generation", file=sys.stderr)
        return

    template_path = Path(__file__).parent / "viewer_template.html"
    with open(template_path, "r", encoding="utf-8") as f:
        template = f.read()

    data_json = json.dumps(all_reports, ensure_ascii=False)
    html = template.replace("__REPORTS_DATA__", data_json)

    viewer_path = reports_dir / "index.html"
    with open(viewer_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"HTML viewer:     {viewer_path}", file=sys.stderr)


# ══════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════

ALL_SOURCES = ["icon_d2", "gfs", "multimodel", "geosphere_nwp", "brightsky", "mosmix"]
HEADLESS_SOURCES = ["meteo_parapente", "xccontest", "alptherm"]


def run_headless_scraper(date: str, locs: dict, headless_sources: list) -> list:
    """Запуск Deno-скрапера для источников, требующих headless browser."""
    deno = shutil.which("deno")
    if not deno:
        print("  deno not found — skipping headless sources", file=sys.stderr)
        return []

    scraper_path = Path(__file__).parent / "scraper.ts"
    if not scraper_path.exists():
        print(f"  {scraper_path} not found — skipping headless sources", file=sys.stderr)
        return []

    locations_json = json.dumps(
        [{"key": k, "lat": v["lat"], "lon": v["lon"]} for k, v in locs.items()]
    )

    cmd = [
        deno, "run", "-A", str(scraper_path),
        "--date", date,
        "--locations", locations_json,
        "--sources", ",".join(headless_sources),
    ]

    print(f"\nRunning headless scraper: {', '.join(headless_sources)}...", file=sys.stderr)
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=180
        )
        if proc.stderr:
            for line in proc.stderr.strip().split("\n"):
                print(f"  [scraper] {line}", file=sys.stderr)
        if proc.returncode != 0:
            print(f"  Scraper exited with code {proc.returncode}", file=sys.stderr)
            return []
        if not proc.stdout.strip():
            return []
        return json.loads(proc.stdout.strip())
    except subprocess.TimeoutExpired:
        print("  Scraper timed out (180s)", file=sys.stderr)
        return []
    except Exception as e:
        print(f"  Scraper error: {e}", file=sys.stderr)
        return []


def main():
    parser = argparse.ArgumentParser(description="PG Weather Triage — data fetcher & report generator")
    parser.add_argument("--date", required=True, help="Date YYYY-MM-DD")
    parser.add_argument("--locations", default="all", help="Comma-separated keys or 'all'")
    parser.add_argument("--sources", default="all",
                        help=f"Comma-separated sources or 'all'. Available: {','.join(ALL_SOURCES)}")
    parser.add_argument("--output-dir", default="reports", help="Output directory (default: reports)")
    parser.add_argument("--no-observations", action="store_true", help="Skip GeoSphere current observations")
    parser.add_argument("--no-markdown", action="store_true", help="Skip Markdown report generation")
    parser.add_argument("--no-viewer", action="store_true", help="Skip HTML viewer (index.html) generation")
    parser.add_argument("--no-scraper", action="store_true", help="Skip headless browser sources")
    parser.add_argument("--headless-sources", default="meteo_parapente",
                        help=f"Comma-separated headless sources. Available: {','.join(HEADLESS_SOURCES)}")
    args = parser.parse_args()

    sources = ALL_SOURCES if args.sources == "all" else [s.strip() for s in args.sources.split(",")]

    if args.locations == "all":
        locs = LOCATIONS
    else:
        keys = [k.strip() for k in args.locations.split(",")]
        locs = {k: LOCATIONS[k] for k in keys if k in LOCATIONS}
        if not locs:
            print(f"No valid locations. Available: {', '.join(LOCATIONS.keys())}", file=sys.stderr)
            sys.exit(1)

    gen_time = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Наблюдения
    obs_info = {"timestamp": "?", "stations": [], "foehn": {}}
    if not args.no_observations:
        try:
            obs_raw = fetch_geosphere_observations()
            obs_info = print_observations(obs_raw)
        except Exception as e:
            print(f"\n  GeoSphere observations: ERROR — {e}", file=sys.stderr)

    # Прогнозы
    results = []
    for key, loc in locs.items():
        print(f"\nFetching {loc['name']}...", file=sys.stderr)
        try:
            result = assess_location(key, loc, args.date, sources)
            results.append(result)
        except Exception as e:
            print(f"  ERROR for {loc['name']}: {e}", file=sys.stderr)
            results.append({"location": loc["name"], "key": key, "error": str(e),
                            "assessment": {"status": "ERROR", "score": -99}})

    # ── Headless scraper ──
    scraper_data = []
    if not args.no_scraper:
        hs = [s.strip() for s in args.headless_sources.split(",")]
        hs = [s for s in hs if s in HEADLESS_SOURCES]
        if hs:
            scraper_data = run_headless_scraper(args.date, locs, hs)
            # Merge scraper results into location results
            for sd in scraper_data:
                src = sd.get("source", "unknown")
                loc_key = sd.get("location")
                for r in results:
                    if r.get("key") == loc_key:
                        r.setdefault("sources", {})[src] = sd
                        break

    # Console output
    print_triage(results)

    # Output directory
    out_dir = Path(args.output_dir)
    out_dir.mkdir(exist_ok=True)

    # ── JSON ──
    json_results = []
    for r in results:
        r_copy = dict(r)
        clean_sources = {}
        for src_name, src_data in r_copy.get("sources", {}).items():
            if isinstance(src_data, dict):
                # Keep hourly for MOSMIX (viewer uses it), strip raw hourly from others
                skip_keys = set() if src_name == "mosmix" else {"hourly"}
                clean = {k: v for k, v in src_data.items() if k not in skip_keys}
                clean_sources[src_name] = clean
            else:
                clean_sources[src_name] = src_data
        r_copy["sources"] = clean_sources
        json_results.append(r_copy)

    json_output = {
        "generated_at": gen_time,
        "date": args.date,
        "sources_used": sources,
        "observations": obs_info,
        "locations": json_results,
    }

    json_path = out_dir / f"{args.date}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(json_output, f, ensure_ascii=False, indent=2)
    print(f"JSON report:     {json_path}", file=sys.stderr)

    # ── Markdown ──
    if not args.no_markdown:
        md_content = generate_markdown_report(results, obs_info, args.date, gen_time)
        md_path = out_dir / f"{args.date}.md"
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(md_content)
        print(f"Markdown report: {md_path}", file=sys.stderr)

    # ── HTML Viewer ──
    if not args.no_viewer:
        generate_viewer_html(out_dir)


if __name__ == "__main__":
    main()
