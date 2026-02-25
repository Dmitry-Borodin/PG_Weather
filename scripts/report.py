#!/usr/bin/env python3
"""
Report generation module for PG Weather Triage v2.0.

Outputs: console (triage table), Markdown, HTML viewer.
"""

import json
import sys
from pathlib import Path

from fetchers import APP_VERSION, MODEL_LABELS, MOSMIX_PARAMS_OF_INTEREST

# ══════════════════════════════════════════════
# Constants
# ══════════════════════════════════════════════

STATUS_EMOJI = {
    "NO-GO": "🔴", "UNLIKELY": "🟠", "MAYBE": "🟡",
    "GO": "🟢", "STRONG": "💚", "NO DATA": "⚪",
}
STATUS_ORDER = {"STRONG": 0, "GO": 1, "MAYBE": 2, "UNLIKELY": 3, "NO-GO": 4, "NO DATA": 5}


# ══════════════════════════════════════════════
# Formatting Helpers
# ══════════════════════════════════════════════

def _fv(v, unit="", prec=None):
    if v is None:
        return "—"
    if isinstance(v, (int, float)) and prec is not None:
        return f"{v:.{prec}f}{unit}"
    return f"{v}{unit}"


def _v(val, unit="", precision=1):
    if val is None:
        return "—"
    if isinstance(val, float):
        return f"{val:.{precision}f}{unit}"
    return f"{val}{unit}"


# ══════════════════════════════════════════════
# Console Output
# ══════════════════════════════════════════════

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
        print(f"     Wind @700: {_fv(a.get('wind_700hPa_ms'),' m/s',1)} "
              f"(mean window: {_fv(a.get('sustained_wind_700_mean'),' m/s',1)})  |  "
              f"Gusts: {_fv(a.get('gusts_10m_ms'),' m/s',1)} "
              f"(mean window: {_fv(a.get('mean_gust_window'),' m/s',1)})  |  "
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

        # Per-model assessment
        pma = a.get("per_model_assessment", {})
        if pma:
            parts = []
            for mk, md in pma.items():
                parts.append(f"{mk}:{md.get('status','?')}({md.get('thermal_hours',0)}h)")
            print(f"     Per-model: {', '.join(parts)}")

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
            tag = f['tag'] if isinstance(f, dict) else f[0]
            msg = f['msg'] if isinstance(f, dict) else f[1]
            print(f"     ⚠ {tag}: {msg}")
        for p in a.get("positives", []):
            tag = p['tag'] if isinstance(p, dict) else p[0]
            msg = p['msg'] if isinstance(p, dict) else p[1]
            print(f"     ✓ {tag}: {msg}")

        # Meteo-Parapente thermals
        mp_th = a.get("mp_max_thermal_ms")
        if mp_th is not None:
            print(f"     MP Thermals: {mp_th} m/s max, top {a.get('mp_thermal_top_m','?')}m, "
                  f"{a.get('mp_thermal_hours','?')}h, PBLH {a.get('mp_pblh_max_m','?')}m")

        # Source provenance
        src_map = a.get("_sources", {})
        if src_map:
            used = sorted(set(src_map.values()))
            labels = [f"{s} ({MODEL_LABELS.get(s, s)})" for s in used]
            print(f"     Models @13: {', '.join(labels)}")

    print(f"\n{'=' * 76}\n")


# ══════════════════════════════════════════════
# Markdown Report
# ══════════════════════════════════════════════

def generate_markdown_report(results, date, gen_time, locations_dict):
    """Generate Markdown report. locations_dict provides peaks info."""
    L = []
    L.append(f"# ✈️ PG Weather Triage v{APP_VERSION}")
    L.append(f"\n**Forecast for: {date}**\n")
    L.append(f"*Generated: {gen_time}*\n")

    sorted_r = sorted(results, key=lambda r: STATUS_ORDER.get(
        r.get("assessment", {}).get("status", "NO-GO"), 5))

    # ── Summary table ──
    L.append("## 📊 Summary\n")
    L.append("| Location | Drive | Status | Base @13 | Margin | W700 mean | Gusts max | CAPE | Lapse | BL | W* | Thermal | Flyable | Confidence |")
    L.append("|----------|-------|--------|----------|--------|-----------|-----------|------|-------|----|-----|---------|---------|------------|")
    for r in sorted_r:
        a = r.get("assessment", {})
        s = a.get("status", "?")
        em = STATUS_EMOJI.get(s, "⚪")
        cfw = a.get("continuous_flyable_hours", 0)
        fw_str = f"{cfw}h" if cfw else "—"
        tw_str = f"{a.get('thermal_window_hours', 0)}h" if a.get('thermal_window_hours') else "—"
        conf = a.get("model_agreement", {}).get("confidence", "?")
        L.append(
            f"| {r['location']} | {_v(r.get('drive_h'),'h')} "
            f"| {em} **{s}** "
            f"| {_v(a.get('cloudbase_msl'),'m')} "
            f"| {_v(a.get('base_margin_over_peaks'),'m')} "
            f"| {_v(a.get('sustained_wind_700_mean'),'m/s')} "
            f"| {_v(a.get('mean_gust_window'),'m/s')} "
            f"| {_v(a.get('cape_J_per_kg'),'',0)} "
            f"| {_v(a.get('lapse_rate_C_per_km'),'°C/km')} "
            f"| {_v(a.get('boundary_layer_height_m'),'m',0)} "
            f"| {_v(a.get('wstar_ms'),'',2)} "
            f"| {tw_str} "
            f"| {fw_str} "
            f"| {conf} |"
        )
    L.append("")

    # ── Per-location details ──
    for r in sorted_r:
        a = r.get("assessment", {})
        s = a.get("status", "?")
        em = STATUS_EMOJI.get(s, "⚪")
        loc = locations_dict.get(r.get("key"), {})

        L.append(f"---\n")
        L.append(f"## {em} {r['location']} — **{s}** (score: {a.get('score','?')})\n")

        L.append("### Key Metrics @13:00 local")
        L.append(f"- **Cloud Base**: {_v(a.get('cloudbase_msl'),'m')} MSL "
                 f"(min: {_v(a.get('cb_min_msl'),'m')}, typ: {_v(a.get('cb_typ_msl'),'m')}) "
                 f"margin: {_v(a.get('base_margin_over_peaks'),'m')} over {loc.get('peaks','?')}m")
        L.append(f"- **Wind @700hPa**: {_v(a.get('wind_700hPa_ms'),'m/s')} "
                 f"(window mean: {_v(a.get('sustained_wind_700_mean'),'m/s')})  |  "
                 f"**@700hPa**: {_v(a.get('wind_700hPa_ms'),'m/s')}")
        L.append(f"- **Gusts**: {_v(a.get('gusts_10m_ms'),'m/s')} "
                 f"(window mean: {_v(a.get('mean_gust_window'),'m/s')})  |  "
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

        # Per-model assessment
        pma = a.get("per_model_assessment", {})
        if pma:
            L.append("### Per-Model Assessment")
            L.append("| Model | Thermal h | Flyable h | Status |")
            L.append("|-------|-----------|-----------|--------|")
            for mk, md in pma.items():
                label = md.get("model_label", mk)
                L.append(f"| {label} | {md.get('thermal_hours', '—')} "
                         f"| {md.get('flyable_hours', '—')} | {md.get('status', '?')} |")
            L.append("")

        # Flags
        if a.get("flags"):
            L.append("**⚠ Warnings:**")
            for f in a["flags"]:
                tag = f['tag'] if isinstance(f, dict) else f[0]
                msg = f['msg'] if isinstance(f, dict) else f[1]
                L.append(f"- {tag}: {msg}")
            L.append("")
        if a.get("positives"):
            L.append("**✓ Positive indicators:**")
            for p in a["positives"]:
                tag = p['tag'] if isinstance(p, dict) else p[0]
                msg = p['msg'] if isinstance(p, dict) else p[1]
                L.append(f"- {tag}: {msg}")
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

        # Family hourly tables (ICON + ECMWF + GFS)
        ha = r.get("hourly_analysis", {})
        mps = ha.get("model_profiles", {})
        icon_src = ha.get("icon_source")
        ecmwf_src = ha.get("ecmwf_source")

        def _render_md_family(model_key, label):
            mp = mps.get(model_key)
            if not mp:
                return
            L.append(f"### {label}")
            L.append("| Hour | T | Base | Cloud | CL/CM/CH | Precip | W10 | Gust | GF | W850 | Lapse | CAPE | SW |")
            L.append("|------|---|------|-------|----------|--------|-----|------|----|------|-------|----|-----|")
            for item in mp:
                cl = f"{_v(item.get('cloudcover_low'),'',0)}/{_v(item.get('cloudcover_mid'),'',0)}/{_v(item.get('cloudcover_high'),'',0)}"
                L.append(
                    f"| {item['hour']} "
                    f"| {_v(item.get('temp_2m'),'°C')} "
                    f"| {_v(item.get('cloudbase_msl'),'m',0)} "
                    f"| {_v(item.get('cloudcover'),'%',0)} "
                    f"| {cl} "
                    f"| {_v(item.get('precipitation'),'mm')} "
                    f"| {_v(item.get('wind_10m'),'',1)} "
                    f"| {_v(item.get('gusts'),'',1)} "
                    f"| {_v(item.get('gust_factor'),'',1)} "
                    f"| {_v(item.get('wind_850'),'',1)} "
                    f"| {_v(item.get('lapse_rate'),'',1)} "
                    f"| {_v(item.get('cape'),'',0)} "
                    f"| {_v(item.get('shortwave_radiation'),'',0)} |"
                )
            L.append("")

        if icon_src:
            _render_md_family(icon_src, f"ICON ({icon_src})")
        if ecmwf_src:
            _render_md_family(ecmwf_src, f"ECMWF ({ecmwf_src})")
        L.append("*Score uses averaged ICON+ECMWF values; GFS for BL/W*/LI/CIN.*\n")

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
# HTML Viewer
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
