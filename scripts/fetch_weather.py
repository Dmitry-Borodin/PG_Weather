#!/usr/bin/env python3
"""
Сбор прогнозных данных для метео-триажа XC closed routes.
Версия: 0.2

Источники:
  - Open-Meteo ICON-D2 (hi-res, 2 км)
  - Open-Meteo GFS (BL height, CAPE, CIN, LI)
  - Open-Meteo мультимодельное сравнение
  - GeoSphere Austria TAWES (текущие наблюдения)
  - GeoSphere NWP (прогноз CAPE, облачность, снеговая линия)
  - BrightSky (DWD wrapper)

Использование:
    python scripts/fetch_weather.py --date 2025-07-15
    python scripts/fetch_weather.py --date 2025-07-15 --locations lenggries,koessen
    python scripts/fetch_weather.py --date 2025-07-15 --sources all
"""

import argparse
import json
import math
import sys
from datetime import datetime
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.parse import urlencode

# ──────────────────────────────────────────────
# Конфигурация районов
# ──────────────────────────────────────────────

LOCATIONS = {
    "lenggries":   {"lat": 47.68, "lon": 11.57, "elev": 700,  "peaks": 1800, "name": "Lenggries",   "geosphere_id": None},
    "wallberg":    {"lat": 47.64, "lon": 11.79, "elev": 1620, "peaks": 1722, "name": "Wallberg",    "geosphere_id": None},
    "koessen":     {"lat": 47.67, "lon": 12.40, "elev": 590,  "peaks": 1900, "name": "Kössen",      "geosphere_id": "11130"},
    "innsbruck":   {"lat": 47.26, "lon": 11.39, "elev": 578,  "peaks": 2600, "name": "Innsbruck",   "geosphere_id": "11121"},
    "greifenburg": {"lat": 46.75, "lon": 13.18, "elev": 600,  "peaks": 2800, "name": "Greifenburg", "geosphere_id": "11204"},
    "speikboden":  {"lat": 46.90, "lon": 11.87, "elev": 950,  "peaks": 2500, "name": "Speikboden",  "geosphere_id": None},
    "bassano":     {"lat": 45.78, "lon": 11.73, "elev": 130,  "peaks": 1700, "name": "Bassano",     "geosphere_id": None},
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


def _fetch_json(url: str, timeout: int = 30) -> dict:
    """HTTP GET → JSON."""
    req = Request(url, headers={"User-Agent": "PG-Weather-Triage/0.2"})
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


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
    # obs: {"T": ..., "FF": ..., "FFX": ..., "DD": ..., "RF": ...}
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
    if t is not None and t > 15:  # summer threshold
        flags.append(f"warm ({t:.1f}°C)")

    return {
        "foehn_likely": len(flags) >= 3,
        "foehn_possible": len(flags) >= 2,
        "flags": flags,
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
        "sources": {},
        "assessment": {},
    }

    lat, lon = loc["lat"], loc["lon"]

    # --- ICON-D2 ---
    if "icon_d2" in sources:
        try:
            d = fetch_icon_d2(lat, lon, date)
            h = d.get("hourly", {})
            at13 = {}
            for p in ICON_D2_PARAMS:
                at13[p] = _get_at_hour(h, "13:00", p)
            result["sources"]["icon_d2"] = {"at_13": at13, "hourly": h}
        except Exception as e:
            result["sources"]["icon_d2"] = {"error": str(e)}

    # --- GFS (BL height, CAPE, CIN, LI) ---
    if "gfs" in sources:
        try:
            d = fetch_gfs(lat, lon, date)
            h = d.get("hourly", {})
            at13 = {}
            for p in GFS_PARAMS:
                at13[p] = _get_at_hour(h, "13:00", p)
            result["sources"]["gfs"] = {"at_13": at13, "hourly": h}
        except Exception as e:
            result["sources"]["gfs"] = {"error": str(e)}

    # --- Multi-model comparison ---
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
                        # Calc wind speed from u,v
                        u = at12.get("u10m")
                        v = at12.get("v10m")
                        if u is not None and v is not None:
                            ws, wd = wind_from_uv(u, v)
                            at12["wind_speed_10m"] = ws
                            at12["wind_dir_10m"] = wd
                        ug = at12.get("ugust")
                        vg = at12.get("vgust")
                        if ug is not None and vg is not None:
                            gs, gd = wind_from_uv(ug, vg)
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

    # --- Основная оценка (из ICON-D2 + GFS) ---
    icon = result["sources"].get("icon_d2", {}).get("at_13", {})
    gfs = result["sources"].get("gfs", {}).get("at_13", {})

    t2m = icon.get("temperature_2m") or gfs.get("temperature_2m")
    td2m = icon.get("dewpoint_2m") or gfs.get("dewpoint_2m")
    ws850 = icon.get("windspeed_850hPa") or gfs.get("windspeed_850hPa")
    ws700 = icon.get("windspeed_700hPa") or gfs.get("windspeed_700hPa")
    gusts = icon.get("windgusts_10m") or gfs.get("windgusts_10m")
    cape = icon.get("cape") or gfs.get("cape")
    t850 = icon.get("temperature_850hPa") or gfs.get("temperature_850hPa")
    t700 = icon.get("temperature_700hPa") or gfs.get("temperature_700hPa")
    cloud = icon.get("cloudcover")
    precip = icon.get("precipitation") or gfs.get("precipitation")
    bl_h = gfs.get("boundary_layer_height")
    li = gfs.get("lifted_index")
    cin = gfs.get("convective_inhibition")

    # Расчёты
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

    # Стоп-фильтры
    flags = []
    if ws850 is not None and ws850 > 5.0:
        flags.append(f"WIND_850: {ws850:.1f} m/s > 5.0 (порог для closed route)")
    if gusts is not None and gusts > 10.0:
        flags.append(f"GUSTS: {gusts:.1f} m/s > 10.0")
    if base_margin is not None and base_margin < 1000:
        flags.append(f"LOW_BASE: {cloudbase_msl}m MSL, margin {base_margin}m < 1000m over {loc['peaks']}m peaks")
    if precip is not None and precip > 0.5:
        flags.append(f"PRECIP: {precip:.1f} mm/h @13:00")
    if cloud is not None and cloud > 80:
        flags.append(f"OVERCAST: {cloud:.0f}%")
    if lr is not None and lr < 5.5:
        flags.append(f"STABLE: lapse rate {lr}°C/km < 5.5 (weak thermals)")
    if cape is not None and cape > 1500:
        flags.append(f"HIGH_CAPE: {cape} J/kg — risk of overdevelopment/storms")
    if li is not None and li < -4:
        flags.append(f"VERY_UNSTABLE: LI={li} — storm risk")

    # Позитивные признаки
    positives = []
    if lr is not None and lr > 7.0:
        positives.append(f"STRONG_LAPSE: {lr}°C/km")
    if cape is not None and 300 < cape < 1500:
        positives.append(f"GOOD_CAPE: {cape} J/kg")
    if bl_h is not None and bl_h > 1500:
        positives.append(f"DEEP_BL: {bl_h}m")
    if cloudbase_msl is not None and base_margin is not None and base_margin > 1500:
        positives.append(f"HIGH_BASE: {cloudbase_msl}m MSL (+{base_margin}m over peaks)")

    # Quick status
    nogo_flags = sum(1 for f in flags if any(x in f for x in ["WIND_850", "GUSTS", "LOW_BASE", "PRECIP", "OVERCAST"]))
    if nogo_flags >= 2:
        status = "NO-GO"
    elif nogo_flags >= 1:
        status = "MAYBE"
    elif len(flags) == 0 and len(positives) >= 2:
        status = "GO-CANDIDATE"
    else:
        status = "MAYBE"

    assessment["flags"] = flags
    assessment["positives"] = positives
    assessment["status"] = status
    result["assessment"] = assessment

    return result


# ──────────────────────────────────────────────
# Вывод
# ──────────────────────────────────────────────

def print_triage(results: list):
    """Краткий вывод в stdout."""
    print("\n" + "=" * 70)
    print("  QUICK TRIAGE — XC Closed Route Assessment")
    print("=" * 70)

    for r in results:
        a = r.get("assessment", {})
        if "error" in r:
            print(f"\n  {r['location']}: ERROR — {r['error']}")
            continue

        status = a.get("status", "?")
        base = a.get("cloudbase_msl", "?")
        margin = a.get("base_margin_over_peaks", "?")
        ws850 = a.get("wind_850hPa_ms", "?")
        gusts = a.get("gusts_10m_ms", "?")
        cape = a.get("cape_J_per_kg", "?")
        lr_val = a.get("lapse_rate_C_per_km", "?")
        bl = a.get("boundary_layer_height_m", "?")
        li_val = a.get("lifted_index", "?")

        status_color = {"NO-GO": "🔴", "MAYBE": "🟡", "GO-CANDIDATE": "🟢"}.get(status, "⚪")

        print(f"\n  {status_color} {r['location']:15s} [{status}]")
        print(f"     Base: {base}m MSL (margin: {margin}m over peaks)")
        print(f"     Wind @850hPa: {ws850}m/s  |  Gusts: {gusts}m/s")
        print(f"     CAPE: {cape}J/kg  |  LI: {li_val}  |  Lapse: {lr_val}°C/km  |  BL: {bl}m")

        for flag in a.get("flags", []):
            print(f"     ⚠ {flag}")
        for pos in a.get("positives", []):
            print(f"     ✓ {pos}")

    # Мультимодельное сравнение если есть
    has_multi = any("multimodel" in r.get("sources", {}) for r in results)
    if has_multi:
        print(f"\n{'─' * 70}")
        print("  MODEL COMPARISON @13:00")
        print(f"{'─' * 70}")
        for r in results:
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


# ──────────────────────────────────────────────
# GeoSphere наблюдения + фён-детекция
# ──────────────────────────────────────────────

def print_observations():
    """Текущие наблюдения со станций Австрии + фён-чек."""
    try:
        data = fetch_geosphere_observations()
        features = data.get("features", [])
        timestamps = data.get("timestamps", [])
        ts = timestamps[-1] if timestamps else "?"

        print(f"\n{'─' * 70}")
        print(f"  CURRENT OBSERVATIONS (GeoSphere Austria) — {ts}")
        print(f"{'─' * 70}")

        innsbruck_obs = {}
        for feat in features:
            props = feat["properties"]
            sid = props.get("station", "?")
            sname = GEOSPHERE_STATIONS.get(sid, sid)
            params = props.get("parameters", {})

            t = params.get("TL", {}).get("data", [None])[0]
            ff = params.get("FF", {}).get("data", [None])[0]
            ffx = params.get("FFX", {}).get("data", [None])[0]
            dd = params.get("DD", {}).get("data", [None])[0]
            rf = params.get("RF", {}).get("data", [None])[0]

            def _fmt(v, unit=""):
                return f"{v}{unit}" if v is not None else "—"

            print(f"  {sname:30s}  T={_fmt(t, '°C')}  FF={_fmt(ff, 'm/s')}  FFX={_fmt(ffx, 'm/s')}  DD={_fmt(dd, '°')}  RH={_fmt(rf, '%')}")

            if sid in ("11121", "11320"):
                innsbruck_obs = {"T": t, "FF": ff, "FFX": ffx, "DD": dd, "RF": rf}

        # Фён-чек
        if innsbruck_obs:
            fc = foehn_check(innsbruck_obs)
            if fc["foehn_likely"]:
                print(f"\n  🔴 FOEHN LIKELY at Innsbruck: {', '.join(fc['flags'])}")
            elif fc["foehn_possible"]:
                print(f"\n  🟡 FOEHN POSSIBLE at Innsbruck: {', '.join(fc['flags'])}")
            else:
                print(f"\n  🟢 No foehn indicators at Innsbruck")

    except Exception as e:
        print(f"\n  GeoSphere observations: ERROR — {e}", file=sys.stderr)


# ──────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────

ALL_SOURCES = ["icon_d2", "gfs", "multimodel", "geosphere_nwp", "brightsky"]


def main():
    parser = argparse.ArgumentParser(description="PG Weather Triage — data fetcher")
    parser.add_argument("--date", required=True, help="Date YYYY-MM-DD")
    parser.add_argument("--locations", default="all", help="Comma-separated keys or 'all'")
    parser.add_argument("--sources", default="all",
                        help=f"Comma-separated sources or 'all'. Available: {','.join(ALL_SOURCES)}")
    parser.add_argument("--output", default=None, help="Output JSON (default: reports/<date>.json)")
    parser.add_argument("--no-observations", action="store_true", help="Skip GeoSphere current observations")
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

    # Наблюдения
    if not args.no_observations:
        print_observations()

    # Прогнозы
    results = []
    for key, loc in locs.items():
        print(f"\nFetching {loc['name']}...", file=sys.stderr)
        try:
            result = assess_location(key, loc, args.date, sources)
            results.append(result)
        except Exception as e:
            print(f"  ERROR for {loc['name']}: {e}", file=sys.stderr)
            results.append({"location": loc["name"], "key": key, "error": str(e)})

    # Вывод в stdout
    print_triage(results)

    # Сохранение JSON
    output_path = args.output
    if output_path is None:
        reports_dir = Path("reports")
        reports_dir.mkdir(exist_ok=True)
        output_path = str(reports_dir / f"{args.date}.json")

    # Убрать hourly из JSON (слишком много данных)
    for r in results:
        for src_name, src_data in r.get("sources", {}).items():
            if isinstance(src_data, dict) and "hourly" in src_data:
                del src_data["hourly"]

    output = {
        "generated_at": datetime.now(tz=__import__('datetime').timezone.utc).isoformat(),
        "date": args.date,
        "sources_used": sources,
        "locations": results,
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"Report saved to {output_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
