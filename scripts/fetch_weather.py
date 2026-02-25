#!/usr/bin/env python3
"""
PG Weather Triage v2.0 — data fetcher, analyser & report generator.

Model families with fallback chains:
  ICON:  D2 (2km, 48h) → EU (7km, 120h) → Global (13km, 180h+)
  ECMWF: IFS 0.25° → IFS 0.4°
  GFS:   Seamless (always needed for BL/LI/CIN)

Scoring v2.0:
  Primary criterion = thermal window size.
  Deductions for overcast, stability, wind, danger.
  Hard rule: base < 2000m MSL → max MAYBE.
  Multi-model: if one model says no-fly → worsen score.
  Meteo-Parapente thermal data integration.

Использование:
    python scripts/fetch_weather.py
    python scripts/fetch_weather.py --date 2026-03-07
    python scripts/fetch_weather.py --date 2026-03-07 --locations lenggries,koessen
"""

import argparse
import json
import statistics
import subprocess
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# ── Module imports ──
from fetchers import (
    APP_VERSION, MODEL_LABELS,
    ICON_CHAIN, ECMWF_CHAIN, GFS_CHAIN,
    fetch_with_fallback, fetch_ecmwf_ens, fetch_icon_eu_eps,
    fetch_geosphere_arome, fetch_mosmix,
)
from analysis import (
    WINDOW_START_H, WINDOW_END_H,
    _extract_at_13_local, _extract_window_stats,
    estimate_cloudbase_msl, lapse_rate, estimate_wstar,
    build_hourly_profile, build_per_model_profiles, assess_per_model,
    compute_flyable_window, compute_flags,
    compute_model_agreement, compute_ensemble_uncertainty,
    compute_status, integrate_meteo_parapente,
)
from report import (
    print_triage, generate_markdown_report,
    generate_viewer_html, generate_single_report_html,
)

# ══════════════════════════════════════════════
# Constants & Configuration
# ══════════════════════════════════════════════

TZ_LOCAL = ZoneInfo("Europe/Berlin")
TZ_UTC = timezone.utc

LOCATIONS = {
    "lenggries":   {"lat": 47.68, "lon": 11.57, "elev": 700,  "peaks": 1800, "name": "Lenggries",   "geosphere_id": None,    "mosmix_id": "10963", "drive_h": 1.0},
    "wallberg":    {"lat": 47.64, "lon": 11.79, "elev": 1620, "peaks": 1722, "name": "Wallberg",    "geosphere_id": None,    "mosmix_id": "10963", "drive_h": 1.0},
    "koessen":     {"lat": 47.67, "lon": 12.40, "elev": 590,  "peaks": 1900, "name": "Kössen",      "geosphere_id": "11130", "mosmix_id": None,    "drive_h": 1.5},
    "innsbruck":   {"lat": 47.26, "lon": 11.39, "elev": 578,  "peaks": 2600, "name": "Innsbruck",   "geosphere_id": "11121", "mosmix_id": "11120", "drive_h": 2.0},
    "greifenburg": {"lat": 46.75, "lon": 13.18, "elev": 600,  "peaks": 2800, "name": "Greifenburg", "geosphere_id": "11204", "mosmix_id": None,    "drive_h": 4.0},
    "speikboden":  {"lat": 46.90, "lon": 11.87, "elev": 950,  "peaks": 2500, "name": "Speikboden",  "geosphere_id": None,    "mosmix_id": None,    "drive_h": 3.5},
    "bassano":     {"lat": 45.78, "lon": 11.73, "elev": 130,  "peaks": 1700, "name": "Bassano",     "geosphere_id": None,    "mosmix_id": None,    "drive_h": 5.0},
}


# ══════════════════════════════════════════════
# Time Utilities
# ══════════════════════════════════════════════

def _next_saturday() -> str:
    today = datetime.now().date()
    days_ahead = (5 - today.weekday()) % 7
    if days_ahead == 0 and today.weekday() != 5:
        days_ahead = 7
    return str(today + timedelta(days=days_ahead))


# ══════════════════════════════════════════════
# Source Lists
# ══════════════════════════════════════════════

# Deterministic families + ensemble + regional
ALL_SOURCES = [
    "icon", "ecmwf", "gfs",
    "ecmwf_ens", "icon_eu_eps",
    "geosphere_arome", "mosmix",
]

HEADLESS_SOURCES = ["meteo_parapente", "xccontest", "alptherm"]

# Map legacy source names to family names
_SOURCE_ALIASES = {
    "ecmwf_hres": "ecmwf", "ecmwf_ifs025": "ecmwf", "ecmwf_ifs04": "ecmwf",
    "icon_d2": "icon", "icon_seamless": "icon", "icon_eu": "icon", "icon_global": "icon",
    "gfs_seamless": "gfs",
}


def _normalize_sources(raw_list: list) -> list:
    """Convert legacy source names to family names."""
    result = []
    for s in raw_list:
        mapped = _SOURCE_ALIASES.get(s, s)
        if mapped not in result:
            result.append(mapped)
    return result


# ══════════════════════════════════════════════
# Location Assessment (orchestrator)
# ══════════════════════════════════════════════

def assess_location(loc_key: str, loc: dict, date: str, sources_list: list) -> dict:
    result = {
        "location": loc["name"], "key": loc_key, "date": date,
        "drive_h": loc.get("drive_h", "?"),
        "peaks": loc["peaks"],
        "sources": {}, "assessment": {},
    }
    lat, lon = loc["lat"], loc["lon"]

    # ── Fetch deterministic models with fallback chains ──

    # ICON family: D2 → EU → Global
    if "icon" in sources_list:
        print(f"  ICON family:", file=sys.stderr)
        icon_key, icon_data = fetch_with_fallback(ICON_CHAIN, lat, lon, date)
        if icon_key:
            h = icon_data.get("hourly", {})
            at13 = _extract_at_13_local(h, date)
            tw_stats = _extract_window_stats(h, date)
            result["sources"][icon_key] = {
                "model_id": icon_key,
                "model_label": MODEL_LABELS.get(icon_key, icon_key),
                "at_13_local": at13,
                "thermal_window_stats": tw_stats,
                "_hourly_raw": h,
                "_family": "icon",
            }
        else:
            result["sources"]["icon"] = {"error": icon_data.get("error", "failed")}

    # ECMWF family: IFS 0.25° → IFS 0.4°
    if "ecmwf" in sources_list:
        print(f"  ECMWF family:", file=sys.stderr)
        ecmwf_key, ecmwf_data = fetch_with_fallback(ECMWF_CHAIN, lat, lon, date)
        if ecmwf_key:
            h = ecmwf_data.get("hourly", {})
            at13 = _extract_at_13_local(h, date)
            tw_stats = _extract_window_stats(h, date)
            result["sources"][ecmwf_key] = {
                "model_id": ecmwf_key,
                "model_label": MODEL_LABELS.get(ecmwf_key, ecmwf_key),
                "at_13_local": at13,
                "thermal_window_stats": tw_stats,
                "_hourly_raw": h,
                "_family": "ecmwf",
            }
        else:
            result["sources"]["ecmwf"] = {"error": ecmwf_data.get("error", "failed")}

    # GFS (always needed for BL height, LI, CIN)
    if "gfs" in sources_list:
        print(f"  GFS:", file=sys.stderr)
        gfs_key, gfs_data = fetch_with_fallback(GFS_CHAIN, lat, lon, date)
        if gfs_key:
            h = gfs_data.get("hourly", {})
            at13 = _extract_at_13_local(h, date)
            tw_stats = _extract_window_stats(h, date)
            result["sources"][gfs_key] = {
                "model_id": gfs_key,
                "model_label": MODEL_LABELS.get(gfs_key, gfs_key),
                "at_13_local": at13,
                "thermal_window_stats": tw_stats,
                "_hourly_raw": h,
                "_family": "gfs",
            }
        else:
            result["sources"]["gfs"] = {"error": gfs_data.get("error", "failed")}

    # ── Fetch ensemble models ──
    for src_name, fetcher in [
        ("ecmwf_ens", fetch_ecmwf_ens),
        ("icon_eu_eps", fetch_icon_eu_eps),
    ]:
        if src_name not in sources_list:
            continue
        try:
            print(f"  {src_name}...", file=sys.stderr, end=" ")
            agg = fetcher(lat, lon, date)
            at13 = _extract_at_13_local(agg, date)
            tw_stats = _extract_window_stats(agg, date)
            result["sources"][src_name] = {
                "model_id": src_name,
                "model_label": MODEL_LABELS.get(src_name, src_name),
                "at_13_local": at13,
                "thermal_window_stats": tw_stats,
            }
            print("✓", file=sys.stderr)
        except Exception as e:
            print(f"✗ {e}", file=sys.stderr)
            result["sources"][src_name] = {"error": str(e)}

    # ── GeoSphere AROME ──
    if "geosphere_arome" in sources_list:
        try:
            print(f"  geosphere_arome...", file=sys.stderr, end=" ")
            geo = fetch_geosphere_arome(lat, lon)
            h = geo.get("hourly", {})
            utc_ts = geo.get("utc_timestamps", False)
            at13 = _extract_at_13_local(h, date, utc_ts)
            tw_stats = _extract_window_stats(h, date, utc_ts)
            result["sources"]["geosphere_arome"] = {
                "model_id": "geosphere_arome",
                "model_label": MODEL_LABELS.get("geosphere_arome"),
                "at_13_local": at13,
                "thermal_window_stats": tw_stats,
            }
            print("✓", file=sys.stderr)
        except Exception as e:
            print(f"✗ {e}", file=sys.stderr)
            result["sources"]["geosphere_arome"] = {"error": str(e)}

    # ── MOSMIX ──
    if "mosmix" in sources_list and loc.get("mosmix_id"):
        try:
            print(f"  mosmix...", file=sys.stderr, end=" ")
            mos = fetch_mosmix(loc["mosmix_id"], date)
            if "error" not in mos:
                result["sources"]["mosmix"] = mos
                print("✓", file=sys.stderr)
            else:
                result["sources"]["mosmix"] = {"error": mos["error"]}
                print(f"✗ {mos['error']}", file=sys.stderr)
        except Exception as e:
            print(f"✗ {e}", file=sys.stderr)
            result["sources"]["mosmix"] = {"error": str(e)}

    # ── Build combined hourly profile ──
    hourly_analysis = build_hourly_profile(result["sources"], date, loc)
    result["hourly_analysis"] = hourly_analysis

    # ── Build per-model profiles ──
    per_model_profiles = build_per_model_profiles(result["sources"], date, loc)
    per_model_assessment = assess_per_model(per_model_profiles, loc)

    # Store per-model profiles in hourly_analysis (for viewer)
    hourly_analysis["model_profiles"] = {
        k: v for k, v in per_model_profiles.items()
    }

    # ── Flyable window ──
    flyable = compute_flyable_window(hourly_analysis["hourly_profile"])

    # ── Thermal window ──
    tw = hourly_analysis.get("thermal_window", {})

    # ── Flags & Metrics ──
    flags, positives = compute_flags(
        hourly_analysis["hourly_profile"], loc, flyable, tw)

    # ── Model Agreement ──
    agreement = compute_model_agreement(result["sources"])
    ensemble_unc = compute_ensemble_uncertainty(result["sources"])

    # ── Score & Status (thermal-window-centric) ──
    profile = hourly_analysis["hourly_profile"]
    bases_win = [p["cloudbase_msl"] for p in profile
                 if WINDOW_START_H <= int(p["hour"][:2]) <= WINDOW_END_H
                 and p["cloudbase_msl"] is not None]
    cb_min = min(bases_win) if bases_win else None

    score, status, breakdown = compute_status(
        flags, positives, agreement, ensemble_unc,
        tw, cb_min, per_model_assessment)

    # ── Build assessment dict ──
    # Dynamic best-order based on what's available
    _best_order = []
    for k in ("icon_d2", "icon_eu", "icon_global", "icon_seamless"):
        if k in result["sources"] and "error" not in result["sources"][k]:
            _best_order.append(k)
            break
    for k in ("ecmwf_ifs025", "ecmwf_ifs04", "ecmwf_hres"):
        if k in result["sources"] and "error" not in result["sources"][k]:
            _best_order.append(k)
            break
    for k in ("gfs_seamless", "gfs"):
        if k in result["sources"] and "error" not in result["sources"][k]:
            _best_order.append(k)
            break

    _at13_src = {}

    def _best13(key, field_name=None):
        for src in _best_order:
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

    # GFS-only fields
    gfs_src = None
    for k in ("gfs_seamless", "gfs"):
        if k in result["sources"] and "error" not in result["sources"].get(k, {}):
            gfs_src = k
            break

    bl_h = result["sources"].get(gfs_src, {}).get("at_13_local", {}).get("boundary_layer_height") if gfs_src else None
    if bl_h is not None:
        _at13_src["boundary_layer_height_m"] = gfs_src
    sw = _best13("shortwave_radiation", "shortwave_radiation")
    li = result["sources"].get(gfs_src, {}).get("at_13_local", {}).get("lifted_index") if gfs_src else None
    if li is not None:
        _at13_src["lifted_index"] = gfs_src
    # CIN (GFS only — ECMWF/ICON accept param but return null)
    cin = result["sources"].get(gfs_src, {}).get("at_13_local", {}).get("convective_inhibition") if gfs_src else None
    if cin is not None:
        _at13_src["cin_J_per_kg"] = gfs_src
    ws_v = estimate_wstar(bl_h, sw, t2m)
    # Updraft: ICON-native convective updraft velocity
    updraft_13 = _best13("updraft", "updraft_ms")

    bm = (cbm - loc["peaks"]) if cbm is not None else None

    # Window metrics
    winds_850_win = [p["wind_850"] for p in profile
                     if WINDOW_START_H <= int(p["hour"][:2]) <= WINDOW_END_H
                     and p["wind_850"] is not None]
    gusts_win = [p["gusts"] for p in profile
                 if WINDOW_START_H <= int(p["hour"][:2]) <= WINDOW_END_H
                 and p["gusts"] is not None]
    gf_win = [p["gust_factor"] for p in profile
              if WINDOW_START_H <= int(p["hour"][:2]) <= WINDOW_END_H
              and p["gust_factor"] is not None]

    assessment = {
        # At 13:00 local
        "temp_2m": t2m, "dewpoint_2m": td,
        "cloudbase_msl": cbm, "base_margin_over_peaks": bm,
        "wind_850hPa_ms": ws850, "wind_700hPa_ms": ws700,
        "gusts_10m_ms": gusts,
        "cape_J_per_kg": cape, "cin_J_per_kg": cin, "lifted_index": li,
        "boundary_layer_height_m": bl_h,
        "lapse_rate_C_per_km": lr, "wstar_ms": ws_v,
        "updraft_ms": updraft_13,
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
        "mean_gust_window": round(statistics.mean(gusts_win), 1) if gusts_win else None,
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
        "score_breakdown": breakdown,
        "status": status,

        # Model agreement & uncertainty
        "model_agreement": agreement,
        "ensemble_uncertainty": ensemble_unc,

        # Per-model assessment
        "per_model_assessment": per_model_assessment,

        # Source provenance
        "_sources": _at13_src,
    }
    result["assessment"] = assessment
    return result


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
    print(f"Model stack: ICON (D2→EU→Global) + ECMWF (0.25°→0.4°) + GFS "
          f"+ ECMWF ENS + ICON-EU EPS + GeoSphere AROME + MOSMIX", file=sys.stderr)
    print(f"Scoring: thermal-window-centric v2.0", file=sys.stderr)

    if args.sources == "all":
        sources = list(ALL_SOURCES)
    else:
        raw = [s.strip() for s in args.sources.split(",")]
        sources = _normalize_sources(raw)

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

            # ── Post-process with Meteo-Parapente thermal data ──
            for r in results:
                integrate_meteo_parapente(r)

    # Console
    print_triage(results, forecast_date)

    # Output
    out_dir = Path(args.output_dir)
    out_dir.mkdir(exist_ok=True)

    # ── Clean JSON (strip _hourly_raw and model_profiles) ──
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

        # Strip model_profiles from hourly_analysis (too large for JSON)
        ha = rc.get("hourly_analysis", {})
        if "model_profiles" in ha:
            # Keep summary, not full profiles
            mp_summary = {}
            for mk, mp in ha["model_profiles"].items():
                mp_summary[mk] = {"hours": len(mp), "model_label": MODEL_LABELS.get(mk, mk)}
            ha["model_profiles_summary"] = mp_summary
            del ha["model_profiles"]

        json_results.append(rc)

    # Determine which model keys were used
    det_models = []
    for family, chain in [("ICON", ICON_CHAIN), ("ECMWF", ECMWF_CHAIN), ("GFS", GFS_CHAIN)]:
        for key, _, _, _ in chain:
            det_models.append(key)

    json_output = {
        "generated_at": gen_time,
        "forecast_date": forecast_date,
        "app_version": APP_VERSION,
        "model_stack": {
            "deterministic_families": {
                "icon": [k for k, _, _, _ in ICON_CHAIN],
                "ecmwf": [k for k, _, _, _ in ECMWF_CHAIN],
                "gfs": [k for k, _, _, _ in GFS_CHAIN],
            },
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
        md = generate_markdown_report(results, forecast_date, gen_time, LOCATIONS)
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
