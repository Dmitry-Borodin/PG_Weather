#!/usr/bin/env python3
"""
Analysis module for PG Weather Triage v2.0.

Scoring redesign:
  - Primary criterion: thermal window size (hours of strong thermals)
  - Deductions for overcast, stability, wind, danger
  - Hard rule: base < 2000m MSL → max MAYBE
  - Multi-model: if one model says no-fly → worsen score
  - Meteo-Parapente thermal data integration
"""

import statistics
from fetchers import _local_to_utc, ENSEMBLE_PARAMS, MODEL_LABELS

# ══════════════════════════════════════════════
# Constants
# ══════════════════════════════════════════════

WINDOW_START_H = 9   # 09:00 local
WINDOW_END_H = 18    # 18:00 local
ANALYSIS_HOURS = [f"{h:02d}:00" for h in range(8, 19)]

# Layer priority for merged profile (first match wins per field)
# Only one model per family will be present (due to fallback chains)
_LAYER_PRIORITY = [
    "icon_d2", "icon_eu", "icon_global",           # ICON family
    "ecmwf_ifs025", "ecmwf_ifs04", "ecmwf_hres",   # ECMWF family (+compat)
    "gfs_seamless", "gfs",                          # GFS (+compat)
    "icon_seamless",                                # legacy compat
]

# GFS-only fields (always sourced from GFS regardless of priority)
_GFS_ONLY_FIELDS = {"boundary_layer_height", "lifted_index", "convective_inhibition"}
# updraft: ICON D2 only (2 km, ≤48ч) — EU/Global return null (v2.3)

# Flag categories for scoring
CRITICAL_TAGS = {"SUSTAINED_WIND_700", "GUSTS_HIGH", "PRECIP_13", "NO_FLYABLE_WINDOW"}
QUALITY_TAGS  = {"OVERCAST", "STABLE", "SHORT_WINDOW", "GUST_FACTOR"}
DANGER_TAGS   = {"HIGH_CAPE", "VERY_UNSTABLE", "CAPE_RISING"}


# ══════════════════════════════════════════════
# Data Extraction Helpers
# ══════════════════════════════════════════════

def _find_hour_idx(times: list, target_date: str, hour: int,
                   utc_timestamps: bool = False) -> int | None:
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
    return round(arg ** (1 / 3), 2) if arg > 0 else None


# ══════════════════════════════════════════════
# Hourly Profile: Averaged (ICON + ECMWF) + GFS fallback
# ══════════════════════════════════════════════

def _get_val(hourly, times, hour_str, key):
    for i, t in enumerate(times):
        if hour_str in str(t):
            vals = hourly.get(key, [])
            return vals[i] if i < len(vals) else None
    return None


def _find_available_sources(sources: dict) -> dict:
    """Find which source key is available for each model family."""
    result = {}

    # ICON family
    for k in ("icon_d2", "icon_eu", "icon_global", "icon_seamless"):
        h = sources.get(k, {}).get("_hourly_raw", {})
        if h and h.get("time"):
            result["icon"] = (k, h, h.get("time", []))
            break

    # ECMWF family
    for k in ("ecmwf_ifs025", "ecmwf_ifs04", "ecmwf_hres"):
        h = sources.get(k, {}).get("_hourly_raw", {})
        if h and h.get("time"):
            result["ecmwf"] = (k, h, h.get("time", []))
            break

    # GFS family
    for k in ("gfs_seamless", "gfs"):
        h = sources.get(k, {}).get("_hourly_raw", {})
        if h and h.get("time"):
            result["gfs"] = (k, h, h.get("time", []))
            break

    return result


def _avg(a, b, decimals=2):
    """Average two values; return whichever is not None, or None."""
    if a is not None and b is not None:
        return round((a + b) / 2, decimals)
    return a if a is not None else b


def build_averaged_profile(per_model_profiles: dict, sources: dict,
                           date: str, loc: dict) -> dict:
    """Build averaged hourly profile from best ICON + best ECMWF.

    Common parameters: averaged when both models have data.
    GFS-only fields: boundary_layer_height, lifted_index, convective_inhibition.
    Updraft: ICON only.
    W*: computed from GFS BL height + averaged SW + averaged T.

    Returns dict with:
      - hourly_profile: averaged profile for scoring
      - thermal_window: detected from averaged profile
      - icon_source / ecmwf_source: which model was used
    """
    # Find best available per family
    icon_key, icon_prof = None, None
    for k in ("icon_d2", "icon_eu", "icon_global", "icon_seamless"):
        if k in per_model_profiles:
            icon_key, icon_prof = k, per_model_profiles[k]
            break

    ecmwf_key, ecmwf_prof = None, None
    for k in ("ecmwf_ifs025", "ecmwf_ifs04", "ecmwf_hres"):
        if k in per_model_profiles:
            ecmwf_key, ecmwf_prof = k, per_model_profiles[k]
            break

    gfs_key, gfs_prof = None, None
    for k in ("gfs_seamless", "gfs"):
        if k in per_model_profiles:
            gfs_key, gfs_prof = k, per_model_profiles[k]
            break

    n_hours = len(ANALYSIS_HOURS)
    profile = []
    for i in range(n_hours):
        iv = icon_prof[i] if icon_prof and i < len(icon_prof) else {}
        ev = ecmwf_prof[i] if ecmwf_prof and i < len(ecmwf_prof) else {}
        gv = gfs_prof[i] if gfs_prof and i < len(gfs_prof) else {}

        # Average common fields from ICON + ECMWF
        t2m = _avg(iv.get("temp_2m"), ev.get("temp_2m"))
        td = _avg(iv.get("dewpoint"), ev.get("dewpoint"))
        cloud = _avg(iv.get("cloudcover"), ev.get("cloudcover"), 0)
        cl_lo = _avg(iv.get("cloudcover_low"), ev.get("cloudcover_low"), 0)
        cl_mi = _avg(iv.get("cloudcover_mid"), ev.get("cloudcover_mid"), 0)
        cl_hi = _avg(iv.get("cloudcover_high"), ev.get("cloudcover_high"), 0)
        prec = _avg(iv.get("precipitation"), ev.get("precipitation"))
        ws10 = _avg(iv.get("wind_10m"), ev.get("wind_10m"))
        gust = _avg(iv.get("gusts"), ev.get("gusts"))
        ws850 = _avg(iv.get("wind_850"), ev.get("wind_850"))
        ws700 = _avg(iv.get("wind_700"), ev.get("wind_700"))
        rh850 = _avg(iv.get("rh_850"), ev.get("rh_850"), 0)
        rh700 = _avg(iv.get("rh_700"), ev.get("rh_700"), 0)
        lr = _avg(iv.get("lapse_rate"), ev.get("lapse_rate"), 1)
        cape_v = _avg(iv.get("cape"), ev.get("cape"), 0)
        sw = _avg(iv.get("shortwave_radiation"), ev.get("shortwave_radiation"), 0)

        # GFS-only fields
        bl = gv.get("bl_height")
        li = gv.get("lifted_index")
        cin = gv.get("cin")

        # Fallback: SW/CAPE from GFS if neither ICON nor ECMWF has it
        if sw is None:
            sw = gv.get("shortwave_radiation")
        if cape_v is None:
            cape_v = gv.get("cape")

        # ICON-only: updraft
        updraft_v = iv.get("updraft")

        # Derived fields computed on averaged values
        base_msl = estimate_cloudbase_msl(t2m, td, loc["elev"])
        ws_v = estimate_wstar(bl, sw, t2m)

        gust_factor = None
        if gust is not None and ws10 is not None:
            gust_factor = round(gust - ws10, 1)

        # Source tracking
        src_parts = []
        if icon_key:
            src_parts.append(icon_key)
        if ecmwf_key:
            src_parts.append(ecmwf_key)
        src_label = "avg" if len(src_parts) == 2 else (src_parts[0] if src_parts else None)

        profile.append({
            "hour": ANALYSIS_HOURS[i],
            "temp_2m": t2m, "dewpoint": td,
            "cloudbase_msl": base_msl,
            "cloudcover": cloud,
            "cloudcover_low": cl_lo, "cloudcover_mid": cl_mi, "cloudcover_high": cl_hi,
            "precipitation": prec,
            "wind_10m": ws10, "gusts": gust, "gust_factor": gust_factor,
            "wind_850": ws850, "wind_700": ws700,
            "rh_850": rh850, "rh_700": rh700,
            "lapse_rate": lr,
            "bl_height": bl, "cape": cape_v, "cin": cin, "lifted_index": li,
            "shortwave_radiation": sw,
            "updraft": round(updraft_v, 2) if updraft_v is not None else None,
            "wstar": ws_v,
            "_src": src_label,
            "_src_overrides": None,
        })

    # ── Thermal window detection ──
    window = _detect_thermal_window(profile, loc)

    return {
        "hourly_profile": profile,
        "thermal_window": window,
        "icon_source": icon_key,
        "ecmwf_source": ecmwf_key,
    }


def _detect_thermal_window(profile: list, loc: dict) -> dict:
    """Detect thermal window: hours with W*≥1.5, low precip, base>1000m MSL, cloud<70%."""
    thermal_hours = []
    for p in profile:
        h = int(p["hour"].split(":")[0])
        if h < 9:
            continue
        w = p.get("wstar")
        prec = p.get("precipitation")
        base = p.get("cloudbase_msl")
        cc = p.get("cloudcover")
        if w is None or w < 1.5:
            continue
        if prec is not None and prec > 0.5:
            continue
        if base is not None and base < 1000:
            continue
        if cc is not None and cc >= 70:
            continue
        thermal_hours.append(p)

    window = {"start": None, "end": None, "peak_hour": None,
              "duration_h": 0, "peak_lapse": None, "peak_cape": None}
    if thermal_hours:
        window["start"] = thermal_hours[0]["hour"]
        window["end"] = thermal_hours[-1]["hour"]
        window["duration_h"] = len(thermal_hours)
        best_lr, best_cape, peak_h = -999, -999, thermal_hours[0]["hour"]
        for th in thermal_hours:
            lr_v = th.get("lapse_rate") if th.get("lapse_rate") is not None else -999
            ca_v = th.get("cape") if th.get("cape") is not None else -999
            if lr_v > best_lr or (lr_v == best_lr and ca_v > best_cape):
                best_lr, best_cape, peak_h = lr_v, ca_v, th["hour"]
        window["peak_hour"] = peak_h
        window["peak_lapse"] = best_lr if best_lr > -999 else None
        window["peak_cape"] = best_cape if best_cape > -999 else None

    return window


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
# Per-Model Profiles & Assessment
# ══════════════════════════════════════════════

def build_per_model_profiles(sources: dict, date: str, loc: dict) -> dict:
    """Build separate hourly profile for each available deterministic model."""
    profiles = {}
    # All possible deterministic model keys
    model_keys = [
        "icon_d2", "icon_eu", "icon_global",
        "ecmwf_ifs025", "ecmwf_ifs04",
        "gfs_seamless",
        # backward compat
        "ecmwf_hres", "icon_seamless", "gfs",
    ]
    for model_key in model_keys:
        src = sources.get(model_key, {})
        h = src.get("_hourly_raw", {})
        if not h or not h.get("time"):
            continue
        times = h.get("time", [])
        profile = []
        for hour in ANALYSIS_HOURS:
            t2m = _get_val(h, times, hour, "temperature_2m")
            td = _get_val(h, times, hour, "dewpoint_2m")
            cloud = _get_val(h, times, hour, "cloudcover")
            prec = _get_val(h, times, hour, "precipitation")
            ws10 = _get_val(h, times, hour, "windspeed_10m")
            gust = _get_val(h, times, hour, "windgusts_10m")
            ws850 = _get_val(h, times, hour, "windspeed_850hPa")
            ws700 = _get_val(h, times, hour, "windspeed_700hPa")
            t850 = _get_val(h, times, hour, "temperature_850hPa")
            t700 = _get_val(h, times, hour, "temperature_700hPa")
            rh850 = _get_val(h, times, hour, "relative_humidity_850hPa")
            rh700 = _get_val(h, times, hour, "relative_humidity_700hPa")
            sw = _get_val(h, times, hour, "shortwave_radiation")
            cape_v = _get_val(h, times, hour, "cape")
            bl = _get_val(h, times, hour, "boundary_layer_height")
            li = _get_val(h, times, hour, "lifted_index")
            cin = _get_val(h, times, hour, "convective_inhibition")
            updraft_v = _get_val(h, times, hour, "updraft")

            base_msl = estimate_cloudbase_msl(t2m, td, loc["elev"])
            lr = lapse_rate(t850, t700)
            ws = estimate_wstar(bl, sw, t2m)

            gust_factor = None
            if gust is not None and ws10 is not None:
                gust_factor = round(gust - ws10, 1)

            profile.append({
                "hour": hour,
                "temp_2m": t2m, "dewpoint": td,
                "cloudbase_msl": base_msl,
                "cloudcover": cloud,
                "cloudcover_low": _get_val(h, times, hour, "cloudcover_low"),
                "cloudcover_mid": _get_val(h, times, hour, "cloudcover_mid"),
                "cloudcover_high": _get_val(h, times, hour, "cloudcover_high"),
                "precipitation": prec,
                "wind_10m": ws10, "gusts": gust, "gust_factor": gust_factor,
                "wind_850": ws850, "wind_700": ws700,
                "rh_850": rh850, "rh_700": rh700,
                "lapse_rate": lr,
                "bl_height": bl, "cape": cape_v, "cin": cin, "lifted_index": li,
                "shortwave_radiation": sw,
                "updraft": round(updraft_v, 2) if updraft_v is not None else None,
                "wstar": ws,
                "_src": model_key,
            })
        profiles[model_key] = profile
    return profiles


def assess_per_model(per_model_profiles: dict, loc: dict) -> dict:
    """Quick per-model flyability assessment (simplified 3-level status)."""
    assessments = {}
    for model_key, profile in per_model_profiles.items():
        flyable = compute_flyable_window(profile)
        tw = _detect_thermal_window(profile, loc)

        # Simplified assessment
        f_hours = flyable["continuous_flyable_hours"]
        t_hours = tw.get("duration_h", 0)

        # Quick wind check
        winds_700 = [p["wind_700"] for p in profile
                     if WINDOW_START_H <= int(p["hour"][:2]) <= WINDOW_END_H
                     and p.get("wind_700") is not None]
        high_wind = winds_700 and statistics.mean(winds_700) > 5.0

        # Quick precip check
        p13 = None
        for p in profile:
            if p["hour"] == "13:00":
                p13 = p.get("precipitation")
        has_precip = p13 is not None and p13 > 0.5

        # Quick status
        if has_precip or f_hours == 0 or high_wind:
            status = "NO-GO"
        elif t_hours <= 2 or f_hours < 4:
            status = "UNLIKELY"
        elif t_hours <= 4:
            status = "MAYBE"
        else:
            status = "GO"

        assessments[model_key] = {
            "flyable_hours": f_hours,
            "thermal_hours": t_hours,
            "status": status,
            "model_label": MODEL_LABELS.get(model_key, model_key),
        }
    return assessments


# ══════════════════════════════════════════════
# Flags & Metrics (window-based)
# ══════════════════════════════════════════════

def compute_flags(profile: list, loc: dict, flyable: dict,
                  thermal_window: dict | None = None) -> tuple[list, list]:
    """Return (flags, positives) based on the full thermal window."""
    flags, positives = [], []
    peaks = loc["peaks"]
    tw_hours = (thermal_window or {}).get("duration_h", 0)

    winds_700, gusts_all, winds_10m, bases, capes, cins = [], [], [], [], [], []
    lapse_rates, bl_heights, wstars, sw_rads = [], [], [], []
    gust_factors = []

    for p in profile:
        h = int(p["hour"].split(":")[0])
        if h < WINDOW_START_H or h > WINDOW_END_H:
            continue
        if p.get("wind_700") is not None:
            winds_700.append(p["wind_700"])
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

    # ── Stop-flags (Critical) ──
    if winds_700 and statistics.mean(winds_700) > 5.0:
        mean_w = statistics.mean(winds_700)
        flags.append(("SUSTAINED_WIND_700",
                      f"mean {mean_w:.1f} m/s over window > 5.0 (closed route threshold)"))

    if gusts_all and statistics.mean(gusts_all) > 10.0:
        flags.append(("GUSTS_HIGH", f"mean {statistics.mean(gusts_all):.1f} m/s > 10.0 in window"))

    if gust_factors and max(gust_factors) > 7.0:
        flags.append(("GUST_FACTOR",
                      f"max gust−mean {max(gust_factors):.1f} m/s (turbulence risk)"))

    cfw = flyable.get("continuous_flyable_hours", 0)
    if cfw == 0:
        flags.append(("NO_FLYABLE_WINDOW", "no continuous flyable hour detected"))
    if 0 < tw_hours < 5:
        flags.append(("SHORT_WINDOW", f"thermal window {tw_hours}h < 5h"))

    if bases:
        cb_min = min(bases)
        margin_min = cb_min - peaks
        if margin_min < 1000:
            flags.append(("LOW_BASE",
                          f"min base {cb_min:.0f}m MSL, margin {margin_min:.0f}m < 1000m over {peaks}m peaks"))

    # Precipitation at 13:00
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
        if max_base > 3500:
            positives.append(("VERY_HIGH_BASE", f"max {max_base:.0f}m MSL (+{margin_max:.0f}m over peaks)"))
        elif margin_max > 1500:
            positives.append(("HIGH_BASE", f"max {max_base:.0f}m MSL (+{margin_max:.0f}m over peaks)"))
    if tw_hours >= 7:
        positives.append(("LONG_WINDOW", f"{tw_hours}h thermal window"))
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
    """Compare best ECMWF vs best ICON at 13:00 local."""
    # Find ECMWF and ICON data (using whatever model succeeded)
    ecmwf = {}
    for k in ("ecmwf_ifs025", "ecmwf_ifs04", "ecmwf_hres"):
        ecmwf = sources.get(k, {}).get("at_13_local", {})
        if ecmwf:
            break

    icon = {}
    for k in ("icon_d2", "icon_eu", "icon_global", "icon_seamless"):
        icon = sources.get(k, {}).get("at_13_local", {})
        if icon:
            break

    if not ecmwf or not icon:
        return {"agreement_score": None, "confidence": "UNKNOWN", "details": {}}

    tolerances = {
        "temperature_2m": 2.0, "windspeed_10m": 2.0,
        "windgusts_10m": 3.0, "cloudcover": 20.0,
        "precipitation": 0.5, "cape": 200.0,
        "windspeed_700hPa": 2.0,
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
# Score & Status — Redesigned (v2.0)
# ══════════════════════════════════════════════

def compute_status(flags, positives, agreement, ensemble_unc,
                   thermal_window, cloudbase_min,
                   per_model_assessments=None):
    """Compute score and status.

    v2.0 scoring: thermal window size is the primary criterion.
    Base score from window duration, then deductions for flags.
    Hard rules:
      - base < 2000m MSL → max MAYBE
      - 2+ critical flags → NO-GO
      - per-model disagreement → worsen score
    """
    tw_hours = thermal_window.get("duration_h", 0)

    # ── Base score from thermal window ──
    if tw_hours == 0:
        base_score = -6
    elif tw_hours <= 2:
        base_score = -2
    elif tw_hours <= 4:
        base_score = 1
    elif tw_hours <= 6:
        base_score = 4
    else:
        base_score = 6

    # ── Deductions ──
    n_crit = sum(1 for t, _ in flags if t in CRITICAL_TAGS)
    n_qual = sum(1 for t, _ in flags if t in QUALITY_TAGS)
    n_dang = sum(1 for t, _ in flags if t in DANGER_TAGS)
    n_base = sum(1 for t, _ in flags if t == "LOW_BASE")

    score = base_score
    score -= n_crit * 3
    score -= n_base * 2
    score -= n_qual * 1
    score -= n_dang * 1

    # ── Bonuses ──
    # VERY_HIGH_BASE gets +2, all others +1
    n_vhb = sum(1 for t, _ in positives if t == "VERY_HIGH_BASE")
    score += (len(positives) - n_vhb) * 1 + n_vhb * 2

    # ── Status from score ──
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

    # ══ Hard Rules ══

    # Rule 1: Multiple critical → NO-GO
    if n_crit >= 2 or (n_crit >= 1 and n_base >= 1):
        status = "NO-GO"
    # Rule 2: One critical + good status → MAYBE
    elif n_crit >= 1 and status in ("GO", "STRONG"):
        status = "MAYBE"

    # Rule 3: Base < 2000m MSL → max MAYBE
    if cloudbase_min is not None and cloudbase_min < 2000:
        if status in ("GO", "STRONG"):
            status = "MAYBE"
            flags.append(("LOW_BASE_HARD",
                          f"min base {cloudbase_min:.0f}m MSL < 2000m → max MAYBE"))

    # Rule 4: Per-model disagreement → worsen
    if per_model_assessments:
        bad_models = [k for k, m in per_model_assessments.items()
                      if m.get("status") in ("NO-GO", "UNLIKELY")]
        if bad_models and status in ("GO", "STRONG"):
            score -= len(bad_models)
            labels = [MODEL_LABELS.get(k, k) for k in bad_models]
            flags.append(("MODEL_DISAGREE",
                          f"{', '.join(labels)} → no-fly/unlikely"))
            if len(bad_models) >= 2:
                status = "UNLIKELY"
            else:
                status = "MAYBE"

    # Rule 5: Low model agreement → MAYBE
    conf = agreement.get("confidence", "UNKNOWN")
    if conf == "LOW" and status in ("GO", "STRONG"):
        status = "MAYBE"
        flags.append(("LOW_CONFIDENCE",
                      f"model agreement {agreement.get('agreement_score', '?')} → confidence LOW"))

    # Rule 6: Large ensemble spread → MAYBE
    for ens_name, ens_data in ensemble_unc.items():
        wind_sp = ens_data.get("windspeed_10m", {}).get("spread")
        cape_sp = ens_data.get("cape", {}).get("spread")
        if wind_sp is not None and wind_sp > 5 and status in ("GO", "STRONG"):
            status = "MAYBE"
            flags.append(("ENS_WIND_SPREAD", f"{ens_name} wind spread {wind_sp:.1f} m/s"))
        if cape_sp is not None and cape_sp > 1000 and status in ("GO", "STRONG"):
            status = "MAYBE"
            flags.append(("ENS_CAPE_SPREAD", f"{ens_name} CAPE spread {cape_sp:.0f} J/kg"))

    # Rule 7: No data
    if n_crit == 0 and n_qual == 0 and len(positives) == 0 and tw_hours == 0:
        status = "NO DATA"

    breakdown = {
        "tw_hours": tw_hours,
        "base_score": base_score,
        "n_critical": n_crit,
        "n_low_base": n_base,
        "n_quality": n_qual,
        "n_danger": n_dang,
        "n_positive": len(positives) - n_vhb,
        "n_very_high_base": n_vhb,
    }

    return score, status, breakdown


# ══════════════════════════════════════════════
# Meteo-Parapente Thermal Integration
# ══════════════════════════════════════════════

def integrate_meteo_parapente(result: dict) -> None:
    """Post-process: use Meteo-Parapente thermal data to adjust assessment.

    Called after headless scraper data is merged into results.
    Modifies result in-place.
    """
    mp = result.get("sources", {}).get("meteo_parapente", {})
    if not mp:
        return

    # Extract thermal data from captured API
    mp_apis = mp.get("captured_api", [])
    mp_data = None
    for a in mp_apis:
        if a.get("type") == "json" and "data.php" in (a.get("url") or ""):
            mp_data = a.get("data")
            break

    if not mp_data or not mp_data.get("data"):
        return

    hours_data = mp_data["data"]
    target_hours = [f"{h:02d}:00" for h in range(9, 18)]

    # Compute thermal metrics from Meteo-Parapente
    max_thermal = 0.0
    max_thermal_top = 0
    thermal_hours_count = 0
    max_pblh = 0

    for hr in target_hours:
        hd = hours_data.get(hr)
        if not hd:
            continue

        # PBL height
        pblh = hd.get("pblh", 0) or 0
        if pblh > max_pblh:
            max_pblh = pblh

        # Thermal strength
        ths = hd.get("ths", [])
        z = hd.get("z", [])
        if ths:
            mx = max(ths) if ths else 0
            if mx > max_thermal:
                max_thermal = mx
            if mx >= 0.5:
                thermal_hours_count += 1
            # Thermal top
            top = 0
            for i in range(len(ths)):
                if i < len(z) and ths[i] > 0.05:
                    top = z[i]
            if top > max_thermal_top:
                max_thermal_top = top

    # Store metrics
    assessment = result.get("assessment", {})
    assessment["mp_max_thermal_ms"] = round(max_thermal, 2)
    assessment["mp_thermal_top_m"] = max_thermal_top
    assessment["mp_thermal_hours"] = thermal_hours_count
    assessment["mp_pblh_max_m"] = max_pblh

    # Adjust flags/score based on MP data
    flags = assessment.get("flags", [])
    positives = assessment.get("positives", [])
    score = assessment.get("score", 0)
    status = assessment.get("status", "MAYBE")

    if max_thermal >= 1.5 and thermal_hours_count >= 3:
        positives.append({"tag": "MP_STRONG_THERMALS",
                         "msg": f"Meteo-Parapente: max {max_thermal:.1f} m/s, "
                                f"{thermal_hours_count}h, top {max_thermal_top}m"})
        score += 1
    elif max_thermal < 0.3 and thermal_hours_count <= 1:
        flags.append({"tag": "MP_WEAK_THERMALS",
                     "msg": f"Meteo-Parapente: max {max_thermal:.1f} m/s — weak thermals"})
        score -= 1
        if status in ("GO", "STRONG"):
            status = "MAYBE"

    assessment["flags"] = flags
    assessment["positives"] = positives
    assessment["score"] = score
    assessment["status"] = status
