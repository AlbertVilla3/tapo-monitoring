"""
src/archive_report.py - Generador de paquete histórico en frío (Cold Storage Archive)
para la monitorización del termo eléctrico Fleck Nilo 150 (60°C).

Genera un directorio autocontenido con:
  1. INFORME_HISTORICO_FLECK_NILO_150.html (Dashboard autocontenido offline + CSS imprimible a PDF + gráficas SVG)
  2. INFORME_HISTORICO_FLECK_NILO_150.md   (Informe ejecutivo y tablas completas en Markdown)
  3. resumen_metricas.json                 (Métricas globales, diarias, horarias y de ciclos)
  4. desglose_diario.csv                   (Serie diaria lista para Excel / hojas de cálculo)
  5. historial_ciclos.csv                  (Listado cronológico íntegro de todos los ciclos)
  6. fleck_monitor_test_frozen.db          (Copia SQLite consolidada sin WAL pendiente)
"""

from __future__ import annotations

import csv
import html
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from zoneinfo import ZoneInfo

    MADRID_TZ = ZoneInfo("Europe/Madrid")
except Exception:
    MADRID_TZ = None  # Fallback para entornos Windows sin tzdata

from src.config import (
    DB_PATH,
    ELECTRICITY_PRICE_EUR_KWH,
    FLECK_TARGET_TEMP_C,
    SPECIFIC_HEAT_WATER_WH_L_C,
    WATER_INLET_TEMP_C,
)
from src.db import get_db_connection
from src.report import calculate_metrics, format_cli_report

WEEKDAYS_ES = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]


def _utc_iso_to_local_str(utc_iso: str) -> str:
    """Convierte un timestamp ISO UTC a representación local Europe/Madrid (YYYY-MM-DD HH:MM:SS)."""
    try:
        dt = datetime.fromisoformat(utc_iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if MADRID_TZ is not None:
            dt = dt.astimezone(MADRID_TZ)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return utc_iso[:19].replace("T", " ")


def build_extended_historical_data(db_path: Path = DB_PATH) -> Dict[str, Any]:
    """
    Calcula las métricas base de src.report y añade series históricas detalladas:
      - Desglose día por día (consumo, pérdidas, demanda, horas monitorizadas, litros)
      - Perfil horario medio (00h - 23h)
      - Registro completo de ciclos con horas locales y costes individuales
    """
    base = calculate_metrics(db_path)
    if not base.get("has_data"):
        return base

    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, timestamp_utc, timestamp_local, power_w, current_a FROM raw_samples ORDER BY id ASC;"
        )
        all_samples = cursor.fetchall()

        cursor.execute("SELECT * FROM heating_cycles ORDER BY start_time ASC, id ASC;")
        raw_cycles = cursor.fetchall()

    # Deduplicar ciclos exactamente igual que en calculate_metrics
    cycles: List[sqlite3.Row] = []
    seen_starts = set()
    for c in raw_cycles:
        if c["start_time"] in seen_starts:
            continue
        if cycles and c["start_time"] < cycles[-1]["end_time"]:
            continue
        seen_starts.add(c["start_time"])
        cycles.append(c)

    price = float(base["energy"]["electricity_price_eur_kwh"] or ELECTRICITY_PRICE_EUR_KWH)
    delta_t = max(FLECK_TARGET_TEMP_C - WATER_INLET_TEMP_C, 1.0)
    wh_per_liter = delta_t * SPECIFIC_HEAT_WATER_WH_L_C

    # 1. Inicializar acumuladores diarios y horarios
    daily_map: Dict[str, Dict[str, Any]] = {}
    hourly_slots: Dict[int, Dict[str, float]] = {
        h: {"energy_wh": 0.0, "monitored_sec": 0.0, "maint_starts": 0, "draw_starts": 0}
        for h in range(24)
    }

    def _ensure_day(day_key: str) -> Dict[str, Any]:
        if day_key not in daily_map:
            try:
                wd = WEEKDAYS_ES[datetime.strptime(day_key, "%Y-%m-%d").weekday()]
            except Exception:
                wd = ""
            daily_map[day_key] = {
                "date": day_key,
                "weekday": wd,
                "samples_count": 0,
                "monitored_sec": 0.0,
                "energy_wh": 0.0,
                "max_power_w": 0.0,
                "maint_cycles": 0,
                "maint_wh": 0.0,
                "draw_cycles": 0,
                "draw_wh": 0.0,
            }
        return daily_map[day_key]

    # Contar muestras y potencias máximas por día local
    for s in all_samples:
        local_str = str(s["timestamp_local"] or s["timestamp_utc"])
        day_key = local_str[:10]
        d = _ensure_day(day_key)
        d["samples_count"] += 1
        if s["power_w"] > d["max_power_w"]:
            d["max_power_w"] = float(s["power_w"])

    # Integración trapezoidal asignada al día y hora local de inicio del intervalo
    for i in range(len(all_samples) - 1):
        s1 = all_samples[i]
        s2 = all_samples[i + 1]
        try:
            t1 = datetime.fromisoformat(s1["timestamp_utc"].replace("Z", "+00:00")).timestamp()
            t2 = datetime.fromisoformat(s2["timestamp_utc"].replace("Z", "+00:00")).timestamp()
            dt_sec = max(0.0, min(t2 - t1, 120.0))
        except Exception:
            dt_sec = 15.0

        d_wh = ((float(s1["power_w"]) + float(s2["power_w"])) / 2.0) * (dt_sec / 3600.0)
        local_str = str(s1["timestamp_local"] or s1["timestamp_utc"])
        day_key = local_str[:10]
        d = _ensure_day(day_key)
        d["energy_wh"] += d_wh
        d["monitored_sec"] += dt_sec

        try:
            hour_int = int(local_str[11:13])
            if 0 <= hour_int <= 23:
                hourly_slots[hour_int]["energy_wh"] += d_wh
                hourly_slots[hour_int]["monitored_sec"] += dt_sec
        except Exception:
            pass

    if len(all_samples) == 1:
        s0 = all_samples[0]
        local_str = str(s0["timestamp_local"] or s0["timestamp_utc"])
        d = _ensure_day(local_str[:10])
        d["energy_wh"] += float(s0["power_w"]) * (15.0 / 3600.0)
        d["monitored_sec"] += 15.0

    # 2. Procesar historial detallado de ciclos cerrados (+ ciclo abierto si existiera)
    cycles_log: List[Dict[str, Any]] = []
    for idx, c in enumerate(cycles, start=1):
        start_local = _utc_iso_to_local_str(c["start_time"])
        end_local = _utc_iso_to_local_str(c["end_time"])
        day_key = start_local[:10]
        d = _ensure_day(day_key)

        e_wh = float(c["energy_wh"])
        e_kwh = e_wh / 1000.0
        ctype = str(c["cycle_type"])

        if ctype == "MAINTENANCE_LOSS":
            d["maint_cycles"] += 1
            d["maint_wh"] += e_wh
        else:
            d["draw_cycles"] += 1
            d["draw_wh"] += e_wh

        try:
            h_int = int(start_local[11:13])
            if 0 <= h_int <= 23:
                if ctype == "MAINTENANCE_LOSS":
                    hourly_slots[h_int]["maint_starts"] += 1
                else:
                    hourly_slots[h_int]["draw_starts"] += 1
        except Exception:
            pass

        cycles_log.append(
            {
                "index": idx,
                "start_local": start_local,
                "end_local": end_local,
                "start_utc": c["start_time"],
                "end_utc": c["end_time"],
                "duration_sec": int(c["duration_sec"]),
                "duration_min": round(int(c["duration_sec"]) / 60.0, 1),
                "energy_wh": round(e_wh, 2),
                "energy_kwh": round(e_kwh, 3),
                "cost_eur": round(e_kwh * price, 3),
                "avg_power_w": round(float(c["avg_power_w"]), 1),
                "max_power_w": round(float(c["max_power_w"]), 1),
                "power_mode": c["power_mode"],
                "cycle_type": ctype,
                "status": "CLOSED",
            }
        )

    oc = base.get("open_cycle")
    if oc:
        start_local = str(oc["start_local"])[:19].replace("T", " ")
        day_key = start_local[:10]
        d = _ensure_day(day_key)
        e_wh = float(oc["energy_wh"])
        e_kwh = e_wh / 1000.0
        ctype = str(oc["tentative_type"])
        if ctype == "MAINTENANCE_LOSS":
            d["maint_cycles"] += 1
            d["maint_wh"] += e_wh
        else:
            d["draw_cycles"] += 1
            d["draw_wh"] += e_wh

        cycles_log.append(
            {
                "index": len(cycles_log) + 1,
                "start_local": start_local,
                "end_local": "EN CURSO",
                "start_utc": oc["start_utc"],
                "end_utc": "",
                "duration_sec": int(oc["duration_sec"]),
                "duration_min": float(oc["duration_min"]),
                "energy_wh": round(e_wh, 2),
                "energy_kwh": round(e_kwh, 3),
                "cost_eur": round(e_kwh * price, 3),
                "avg_power_w": float(oc["avg_power_w"]),
                "max_power_w": float(oc["max_power_w"]),
                "power_mode": oc["power_mode"],
                "cycle_type": ctype,
                "status": "OPEN",
            }
        )

    # 3. Consolidar lista diaria ordenada
    daily_series: List[Dict[str, Any]] = []
    for day_key in sorted(daily_map.keys()):
        d = daily_map[day_key]
        hours_mon = d["monitored_sec"] / 3600.0
        tot_kwh = d["energy_wh"] / 1000.0
        maint_kwh = d["maint_wh"] / 1000.0
        draw_kwh = d["draw_wh"] / 1000.0
        standby_day_kwh = max(0.0, tot_kwh - maint_kwh - draw_kwh)
        liters_60c = (d["draw_wh"] / wh_per_liter) if wh_per_liter > 0 else 0.0
        is_complete = hours_mon >= 22.5

        daily_series.append(
            {
                "date": day_key,
                "weekday": d["weekday"],
                "hours_monitored": round(hours_mon, 2),
                "is_complete_day": is_complete,
                "samples_count": d["samples_count"],
                "total_kwh": round(tot_kwh, 3),
                "cost_eur": round(tot_kwh * price, 2),
                "maint_cycles": d["maint_cycles"],
                "maint_kwh": round(maint_kwh, 3),
                "draw_cycles": d["draw_cycles"],
                "draw_kwh": round(draw_kwh, 3),
                "standby_kwh": round(standby_day_kwh, 3),
                "total_cycles": d["maint_cycles"] + d["draw_cycles"],
                "max_power_w": round(d["max_power_w"], 1),
                "estimated_liters_60c": round(liters_60c, 1),
            }
        )

    complete_days = [d for d in daily_series if d["is_complete_day"]]
    complete_days_avg_kwh = (
        round(sum(d["total_kwh"] for d in complete_days) / len(complete_days), 3)
        if complete_days
        else base["energy"]["daily_avg_kwh"]
    )

    # 4. Consolidar perfil horario (00h a 23h)
    hourly_profile: List[Dict[str, Any]] = []
    for h in range(24):
        slot = hourly_slots[h]
        slot_hours = slot["monitored_sec"] / 3600.0
        avg_wh = (slot["energy_wh"] / slot_hours) if slot_hours > 0 else 0.0
        hourly_profile.append(
            {
                "hour": h,
                "label": f"{h:02d}:00",
                "total_kwh": round(slot["energy_wh"] / 1000.0, 3),
                "avg_wh_per_day_hour": round(avg_wh, 1),
                "monitored_days_equiv": round(slot_hours, 2),
                "maint_starts": int(slot["maint_starts"]),
                "draw_starts": int(slot["draw_starts"]),
            }
        )

    extended = dict(base)
    extended["generated_at_utc"] = datetime.now(timezone.utc).isoformat()
    extended["daily_series"] = daily_series
    extended["complete_days_count"] = len(complete_days)
    extended["complete_days_avg_kwh"] = complete_days_avg_kwh
    extended["hourly_profile"] = hourly_profile
    extended["cycles_log"] = cycles_log
    return extended


def _render_daily_svg(daily_series: List[Dict[str, Any]]) -> str:
    """Genera un gráfico SVG vectorial autocontenido con el consumo diario apilado."""
    if not daily_series:
        return ""

    n = len(daily_series)
    width = max(680, n * 78 + 110)
    height = 300
    pad_left, pad_right, pad_top, pad_bottom = 58, 28, 36, 62
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - pad_bottom

    max_kwh = max(max(d["total_kwh"] for d in daily_series), 0.5) * 1.18
    step_x = plot_w / max(n, 1)
    bar_w = min(46.0, step_x * 0.62)

    svg = [
        f'<svg viewBox="0 0 {width} {height}" width="100%" style="max-width:{width}px;height:auto;font-family:system-ui,-apple-system,sans-serif;" role="img" aria-label="Consumo diario kWh">',
    ]

    # Líneas horizontales de rejilla
    for i in range(5):
        val = max_kwh * (i / 4.0)
        y = pad_top + plot_h - (val / max_kwh) * plot_h
        svg.append(
            f'<line x1="{pad_left}" y1="{y:.1f}" x2="{width - pad_right}" y2="{y:.1f}" stroke="#e2e8f0" stroke-dasharray="3,3" stroke-width="1"/>'
        )
        svg.append(
            f'<text x="{pad_left - 8}" y="{y + 4:.1f}" text-anchor="end" font-size="11" fill="#64748b">{val:.1f} kWh</text>'
        )

    # Barras por día (Demanda en azul + Mantenimiento en ámbar + resto/standby en gris)
    for idx, d in enumerate(daily_series):
        cx = pad_left + step_x * idx + step_x / 2.0
        bx = cx - bar_w / 2.0

        tot = d["total_kwh"]
        draw_v = min(d["draw_kwh"], tot)
        maint_v = min(d["maint_kwh"], max(0.0, tot - draw_v))
        other_v = max(0.0, tot - draw_v - maint_v)

        h_draw = (draw_v / max_kwh) * plot_h
        h_maint = (maint_v / max_kwh) * plot_h
        h_other = (other_v / max_kwh) * plot_h

        y_base = pad_top + plot_h
        if h_draw > 0:
            svg.append(
                f'<rect x="{bx:.1f}" y="{y_base - h_draw:.1f}" width="{bar_w:.1f}" height="{h_draw:.1f}" fill="#2563eb" rx="2"/>'
            )
        if h_maint > 0:
            svg.append(
                f'<rect x="{bx:.1f}" y="{y_base - h_draw - h_maint:.1f}" width="{bar_w:.1f}" height="{h_maint:.1f}" fill="#f59e0b" rx="2"/>'
            )
        if h_other > 0.5:
            svg.append(
                f'<rect x="{bx:.1f}" y="{y_base - h_draw - h_maint - h_other:.1f}" width="{bar_w:.1f}" height="{h_other:.1f}" fill="#94a3b8" rx="2"/>'
            )

        y_top = y_base - (tot / max_kwh) * plot_h
        svg.append(
            f'<text x="{cx:.1f}" y="{y_top - 6:.1f}" text-anchor="middle" font-size="11" font-weight="600" fill="#1e293b">{tot:.2f}</text>'
        )
        short_date = d["date"][5:]  # MM-DD
        partial_mark = "*" if not d["is_complete_day"] else ""
        svg.append(
            f'<text x="{cx:.1f}" y="{y_base + 18:.1f}" text-anchor="middle" font-size="11" fill="#334155">{d["weekday"]} {short_date}{partial_mark}</text>'
        )
        svg.append(
            f'<text x="{cx:.1f}" y="{y_base + 33:.1f}" text-anchor="middle" font-size="10" fill="#64748b">{d["cost_eur"]:.2f} €</text>'
        )

    svg.append(
        f'<line x1="{pad_left}" y1="{pad_top + plot_h}" x2="{width - pad_right}" y2="{pad_top + plot_h}" stroke="#94a3b8" stroke-width="1.2"/>'
    )
    svg.append("</svg>")
    return "\n".join(svg)


def _render_hourly_svg(hourly_profile: List[Dict[str, Any]]) -> str:
    """Genera un gráfico SVG con el perfil horario medio (Wh medios por hora del día 00-23h)."""
    if not hourly_profile:
        return ""

    width, height = 720, 240
    pad_left, pad_right, pad_top, pad_bottom = 56, 20, 28, 44
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - pad_bottom

    max_wh = max(max(h["avg_wh_per_day_hour"] for h in hourly_profile), 50.0) * 1.18
    step_x = plot_w / 24.0
    bar_w = step_x * 0.68

    svg = [
        f'<svg viewBox="0 0 {width} {height}" width="100%" style="max-width:{width}px;height:auto;font-family:system-ui,-apple-system,sans-serif;" role="img" aria-label="Perfil horario medio Wh">',
    ]

    for i in range(4):
        val = max_wh * (i / 3.0)
        y = pad_top + plot_h - (val / max_wh) * plot_h
        svg.append(
            f'<line x1="{pad_left}" y1="{y:.1f}" x2="{width - pad_right}" y2="{y:.1f}" stroke="#e2e8f0" stroke-dasharray="3,3" stroke-width="1"/>'
        )
        svg.append(
            f'<text x="{pad_left - 8}" y="{y + 4:.1f}" text-anchor="end" font-size="10" fill="#64748b">{val:.0f} Wh</text>'
        )

    for h in hourly_profile:
        idx = h["hour"]
        cx = pad_left + step_x * idx + step_x / 2.0
        bx = cx - bar_w / 2.0
        val = h["avg_wh_per_day_hour"]
        bh = (val / max_wh) * plot_h
        y_base = pad_top + plot_h
        color = "#0ea5e9" if h["draw_starts"] >= h["maint_starts"] and h["draw_starts"] > 0 else "#6366f1"
        if 1 <= idx <= 6 and h["draw_starts"] == 0:
            color = "#f59e0b"
        if bh > 0.5:
            svg.append(
                f'<rect x="{bx:.1f}" y="{y_base - bh:.1f}" width="{bar_w:.1f}" height="{bh:.1f}" fill="{color}" rx="2"/>'
            )
        if idx % 2 == 0:
            svg.append(
                f'<text x="{cx:.1f}" y="{y_base + 16:.1f}" text-anchor="middle" font-size="10" fill="#475569">{idx:02d}h</text>'
            )

    svg.append(
        f'<line x1="{pad_left}" y1="{pad_top + plot_h}" x2="{width - pad_right}" y2="{pad_top + plot_h}" stroke="#94a3b8" stroke-width="1.2"/>'
    )
    svg.append("</svg>")
    return "\n".join(svg)


def format_markdown_archive(data: Dict[str, Any]) -> str:
    """Genera el documento histórico completo en formato Markdown."""
    if not data.get("has_data"):
        return "# Informe Histórico Fleck Nilo 150\n\nNo hay datos registrados.\n"

    t = data["time"]
    e = data["energy"]
    c = data["cycles_breakdown"]
    w = data["water"]
    a = data["aerotherm_simulation"]
    daily = data.get("daily_series", [])
    cycles_log = data.get("cycles_log", [])

    lines = [
        "# 📊 Informe Histórico de Monitorización: Termo Eléctrico Fleck Nilo 150 (60°C)",
        "",
        f"- **Periodo evaluado:** `{t['start_local'][:19]}` ➔ `{t['end_local'][:19]}` (hora local Europe/Madrid)",
        f"- **Duración total:** **{t['total_days']} días** ({t['total_hours']} horas) | **{t['samples_count']} muestras** cada ~15 s",
        f"- **Tarifa eléctrica aplicada:** `{e['electricity_price_eur_kwh']:.3f} €/kWh` (PVPC real con impuestos e IVA)",
        f"- **Equipo monitorizado:** Termo eléctrico Fleck Nilo 150 L (consigna fija 60°C, doble resistencia 1.250 W + 1.250 W)",
        "",
        "---",
        "",
        "## 1. Resumen Ejecutivo y Balance Energético",
        "",
        "| Indicador | Valor Medido | Coste / Detalle |",
        "| :--- | ---: | :--- |",
        f"| **Consumo Total Medido ($\\sum P \\cdot \\Delta t$)** | **{e['total_kwh']:.3f} kWh** | **{e['total_cost_eur']:.2f} €** en el periodo |",
        f"| **Promedio Diario Ponderado (24h)** | **{e['daily_avg_kwh']:.3f} kWh/día** | **{(e['daily_avg_kwh'] * e['electricity_price_eur_kwh']):.2f} €/día** |",
        f"| **Promedio en Días Completos ({data.get('complete_days_count', 0)} días)** | **{data.get('complete_days_avg_kwh', 0.0):.3f} kWh/día** | **{(data.get('complete_days_avg_kwh', 0.0) * e['electricity_price_eur_kwh']):.2f} €/día** |",
        f"| **Demanda Real de Uso (Duchas / ACS)** | **{c['water_draw']['kwh']:.3f} kWh** ({c['water_draw']['percentage']:.1f}%) | {c['water_draw']['count']} ciclos ({c['water_draw']['daily_draw_kwh']:.3f} kWh/día) |",
        f"| **Pérdidas Estáticas de Mantenimiento** | **{c['maintenance']['kwh']:.3f} kWh** ({c['maintenance']['percentage']:.1f}%) | {c['maintenance']['count']} ciclos ({c['maintenance']['daily_loss_kwh']:.3f} kWh/día · **{c['maintenance']['daily_loss_cost_eur']:.2f} €/día**) |",
        f"| **Consumo Basal / Standby** | **{c['standby_kwh']:.3f} kWh** | Electrónica de control en reposo |",
        f"| **Potencia Media / Pico Máximo** | **{e['avg_power_w']:.1f} W** / **{e['max_power_w']:.1f} W** | Modos: {c['power_modes']['half_1250w']['count']}x 1.250W · {c['power_modes']['full_2500w']['count']}x 2.500W |",
        f"| **Agua Caliente Estimada a 60°C** | **{w['estimated_liters_60c']:.1f} L** | **{w['daily_avg_liters_60c']:.1f} L/día** (~{w['equivalent_showers_approx']:.1f} duchas en total) |",
        "",
        "---",
        "",
        "## 2. Evolución Diaria (Día a Día)",
        "",
        "> *(Nota: los días de inicio o cierre con menos de 22,5 h registradas se marcan como parciales)*",
        "",
        "| Fecha | Día | Horas | Consumo Total (kWh) | Coste (€) | Uso / Duchas (kWh) | Pérdidas Estát. (kWh) | Ciclos (Uso / Pérd.) | ACS 60°C Est. (L) |",
        "| :--- | :---: | ---: | ---: | ---: | ---: | ---: | :---: | ---: |",
    ]

    for d in daily:
        tag = "" if d["is_complete_day"] else " *(parcial)*"
        lines.append(
            f"| `{d['date']}`{tag} | {d['weekday']} | {d['hours_monitored']:.1f} h | **{d['total_kwh']:.3f}** | {d['cost_eur']:.2f} € | {d['draw_kwh']:.3f} | {d['maint_kwh']:.3f} | {d['draw_cycles']} / {d['maint_cycles']} | {d['estimated_liters_60c']:.1f} L |"
        )

    lines.extend(
        [
            "",
            "---",
            "",
            f"## 3. Simulación Comparativa: Aerotermo ACS (COP {a['cop']:.1f}) y Deducción IRPF",
            "",
            "| Escenario | Termo Fleck Nilo (Efecto Joule) | Aerotermo Bomba de Calor (COP 3.0) | Ahorro Directo Estimado |",
            "| :--- | ---: | ---: | ---: |",
            f"| **Periodo medido ({t['total_days']} días)** | {e['total_kwh']:.3f} kWh ({e['total_cost_eur']:.2f} €) | {a['period_aerotherm_kwh']:.3f} kWh ({a['period_aerotherm_cost_eur']:.2f} €) | **{a['period_savings_eur']:.2f} €** ({a['period_savings_kwh']:.2f} kWh) |",
            f"| **Proyección 1 Año (365 días)** | {a['annual_fleck_kwh']:.0f} kWh ({a['annual_fleck_cost_eur']:.2f} €) | {a['annual_aerotherm_kwh']:.0f} kWh ({a['annual_aerotherm_cost_eur']:.2f} €) | **{a['annual_savings_eur']:.2f} €/año** ({a['annual_savings_kwh']:.0f} kWh/año) |",
            f"| **Proyección 5 Años** | {a['annual_fleck_cost_eur'] * 5:.2f} € | {a['annual_aerotherm_cost_eur'] * 5:.2f} € | **{a['annual_savings_eur'] * 5:.2f} €** |",
            f"| **Proyección 10 Años** | {a['annual_fleck_cost_eur'] * 10:.2f} € | {a['annual_aerotherm_cost_eur'] * 10:.2f} € | **{a['annual_savings_eur'] * 10:.2f} €** |",
            "",
            f"- **Reducción de Energía Primaria No Renovable:** `{a['reduction_pct']:.1f}%` (requisito RDL 19/2021 para deducción IRPF del 40%: $\\ge 30\\%$).",
            "",
            "---",
            "",
            f"## 4. Historial Completo de Ciclos de Calentamiento ({len(cycles_log)} ciclos)",
            "",
            "| # | Inicio (Local) | Fin (Local) | Duración | Energía (Wh) | Coste (€) | Pot. Media / Máx | Modo | Clasificación |",
            "| ---: | :--- | :--- | ---: | ---: | ---: | ---: | :---: | :--- |",
        ]
    )

    for cl in cycles_log:
        tipo_es = "📉 Mantenimiento" if cl["cycle_type"] == "MAINTENANCE_LOSS" else "🚿 Demanda / Ducha"
        modo_es = "1.250 W" if cl["power_mode"] == "HALF_1250W" else "2.500 W"
        lines.append(
            f"| {cl['index']} | `{cl['start_local']}` | `{cl['end_local']}` | {cl['duration_min']:.1f} min | {cl['energy_wh']:.0f} Wh ({cl['energy_kwh']:.3f} kWh) | {cl['cost_eur']:.3f} € | {cl['avg_power_w']:.0f} / {cl['max_power_w']:.0f} W | {modo_es} | {tipo_es} |"
        )

    lines.append("")
    return "\n".join(lines)


def format_html_archive(data: Dict[str, Any]) -> str:
    """Genera un informe HTML ejecutivo 100% autocontenido (offline + imprimible a PDF)."""
    if not data.get("has_data"):
        return "<html><body><h1>Sin datos disponibles</h1></body></html>"

    t = data["time"]
    e = data["energy"]
    c = data["cycles_breakdown"]
    w = data["water"]
    a = data["aerotherm_simulation"]
    daily = data.get("daily_series", [])
    cycles_log = data.get("cycles_log", [])

    daily_svg = _render_daily_svg(daily)
    hourly_svg = _render_hourly_svg(data.get("hourly_profile", []))

    daily_rows_html = []
    for d in daily:
        badge = (
            '<span class="badge badge-ok">24h</span>'
            if d["is_complete_day"]
            else '<span class="badge badge-muted">Parcial</span>'
        )
        daily_rows_html.append(
            f"""
            <tr>
                <td><strong>{html.escape(d['date'])}</strong> ({html.escape(d['weekday'])}) {badge}</td>
                <td class="num">{d['hours_monitored']:.1f} h</td>
                <td class="num"><strong>{d['total_kwh']:.3f} kWh</strong></td>
                <td class="num"><strong>{d['cost_eur']:.2f} €</strong></td>
                <td class="num">{d['draw_kwh']:.3f} kWh ({d['draw_cycles']})</td>
                <td class="num">{d['maint_kwh']:.3f} kWh ({d['maint_cycles']})</td>
                <td class="num">{d['estimated_liters_60c']:.1f} L</td>
                <td class="num">{d['max_power_w']:.0f} W</td>
            </tr>
            """
        )

    cycle_rows_html = []
    for cl in cycles_log:
        is_maint = cl["cycle_type"] == "MAINTENANCE_LOSS"
        badge_cls = "badge-warn" if is_maint else "badge-blue"
        label = "Pérdida Mantenimiento" if is_maint else "Demanda / Ducha"
        modo = "1.250 W" if cl["power_mode"] == "HALF_1250W" else "2.500 W"
        cycle_rows_html.append(
            f"""
            <tr>
                <td class="num">{cl['index']}</td>
                <td><code>{html.escape(cl['start_local'])}</code></td>
                <td><code>{html.escape(cl['end_local'])}</code></td>
                <td class="num">{cl['duration_min']:.1f} min</td>
                <td class="num"><strong>{cl['energy_wh']:.0f} Wh</strong> ({cl['energy_kwh']:.3f} kWh)</td>
                <td class="num">{cl['cost_eur']:.3f} €</td>
                <td class="num">{cl['avg_power_w']:.0f} W / {cl['max_power_w']:.0f} W</td>
                <td>{modo}</td>
                <td><span class="badge {badge_cls}">{label}</span></td>
            </tr>
            """
        )

    daily_cost = e["daily_avg_kwh"] * e["electricity_price_eur_kwh"]
    annual_maint_eur = c["maintenance"]["daily_loss_cost_eur"] * 365.0

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Informe Histórico — Termo Fleck Nilo 150 (60°C)</title>
<style>
  :root {{
    --bg: #f8fafc;
    --card: #ffffff;
    --border: #e2e8f0;
    --text: #0f172a;
    --muted: #64748b;
    --blue: #2563eb;
    --amber: #d97706;
    --green: #059669;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 32px 20px;
    background: var(--bg); color: var(--text);
    font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    line-height: 1.5;
  }}
  .container {{ max-width: 1080px; margin: 0 auto; }}
  header {{
    background: linear-gradient(135deg, #0f172a 0%, #1e3a8a 100%);
    color: #fff; padding: 28px 32px; border-radius: 14px; margin-bottom: 24px;
  }}
  header h1 {{ margin: 0 0 8px 0; font-size: 1.55rem; }}
  header p {{ margin: 4px 0; color: #cbd5e1; font-size: 0.94rem; }}
  .kpi-grid {{
    display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    gap: 16px; margin-bottom: 24px;
  }}
  .card {{
    background: var(--card); border: 1px solid var(--border);
    border-radius: 12px; padding: 20px 22px; margin-bottom: 22px;
    box-shadow: 0 1px 2px rgba(15,23,42,0.04);
  }}
  .kpi-card {{ margin-bottom: 0; }}
  .kpi-label {{ font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.04em; color: var(--muted); font-weight: 600; }}
  .kpi-value {{ font-size: 1.65rem; font-weight: 700; margin: 6px 0 4px 0; color: var(--text); }}
  .kpi-sub {{ font-size: 0.85rem; color: var(--muted); }}
  h2 {{ font-size: 1.18rem; margin: 0 0 14px 0; padding-bottom: 8px; border-bottom: 1px solid var(--border); }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.88rem; }}
  th, td {{ padding: 9px 10px; border-bottom: 1px solid var(--border); text-align: left; }}
  th {{ background: #f1f5f9; font-weight: 600; color: #334155; }}
  td.num, th.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  .badge {{
    display: inline-block; padding: 2px 8px; border-radius: 999px;
    font-size: 0.75rem; font-weight: 600;
  }}
  .badge-blue {{ background: #dbeafe; color: #1d4ed8; }}
  .badge-warn {{ background: #fef3c7; color: #b45309; }}
  .badge-ok {{ background: #d1fae5; color: #047857; }}
  .badge-muted {{ background: #e2e8f0; color: #475569; }}
  .legend {{ display: flex; gap: 18px; font-size: 0.84rem; color: var(--muted); margin-bottom: 10px; flex-wrap: wrap; }}
  .dot {{ display: inline-block; width: 11px; height: 11px; border-radius: 3px; margin-right: 5px; vertical-align: middle; }}
  .highlight-box {{
    background: #ecfdf5; border: 1px solid #a7f3d0; border-radius: 10px;
    padding: 16px 18px; margin-top: 14px; color: #064e3b; font-size: 0.92rem;
  }}
  @media print {{
    body {{ background: #fff; padding: 0; }}
    .card {{ box-shadow: none; break-inside: avoid; }}
    header {{ -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
  }}
</style>
</head>
<body>
<div class="container">
  <header>
    <h1>📊 Archivo Histórico: Termo Eléctrico Fleck Nilo 150 (60°C)</h1>
    <p><strong>Periodo monitorizado:</strong> {html.escape(t['start_local'][:19])} ➔ {html.escape(t['end_local'][:19])} ({t['total_days']} días · {t['total_hours']} h · {t['samples_count']} muestras a 15s)</p>
    <p><strong>Sensor:</strong> TP-Link Tapo P110 (Integración exacta ∑ P · Δt) &nbsp;|&nbsp; <strong>Tarifa PVPC:</strong> {e['electricity_price_eur_kwh']:.3f} €/kWh</p>
  </header>

  <div class="kpi-grid">
    <div class="card kpi-card">
      <div class="kpi-label">Consumo Total Periodo</div>
      <div class="kpi-value">{e['total_kwh']:.2f} kWh</div>
      <div class="kpi-sub">Coste real: <strong>{e['total_cost_eur']:.2f} €</strong> ({t['total_days']} días)</div>
    </div>
    <div class="card kpi-card">
      <div class="kpi-label">Promedio Diario</div>
      <div class="kpi-value">{e['daily_avg_kwh']:.2f} kWh/día</div>
      <div class="kpi-sub"><strong>{daily_cost:.2f} €/día</strong> · Días 24h: {data.get('complete_days_avg_kwh', 0.0):.2f} kWh/día</div>
    </div>
    <div class="card kpi-card">
      <div class="kpi-label">Demanda Real (Duchas/ACS)</div>
      <div class="kpi-value" style="color:var(--blue)">{c['water_draw']['percentage']:.1f}%</div>
      <div class="kpi-sub">{c['water_draw']['kwh']:.2f} kWh en {c['water_draw']['count']} ciclos ({w['daily_avg_liters_60c']:.0f} L/día a 60°C)</div>
    </div>
    <div class="card kpi-card">
      <div class="kpi-label">Pérdida Estática Depósito</div>
      <div class="kpi-value" style="color:var(--amber)">{c['maintenance']['percentage']:.1f}%</div>
      <div class="kpi-sub">{c['maintenance']['daily_loss_kwh']:.2f} kWh/día ({c['maintenance']['daily_loss_cost_eur']:.2f} €/día · ~{annual_maint_eur:.0f} €/año solo mantener)</div>
    </div>
  </div>

  <div class="card">
    <h2>1. Evolución Diaria del Consumo Eléctrico</h2>
    <div class="legend">
      <span><i class="dot" style="background:#2563eb"></i>Demanda Real / Duchas (≥ 350 Wh)</span>
      <span><i class="dot" style="background:#f59e0b"></i>Pérdidas de Mantenimiento Estático (&lt; 350 Wh / nocturno)</span>
      <span>* Día de inicio o cierre parcial (&lt; 22,5 h)</span>
    </div>
    {daily_svg}
    <div style="overflow-x:auto; margin-top:16px;">
      <table>
        <thead>
          <tr>
            <th>Fecha</th>
            <th class="num">Horas</th>
            <th class="num">Total Día</th>
            <th class="num">Coste</th>
            <th class="num">Uso / Duchas</th>
            <th class="num">Pérdidas Estát.</th>
            <th class="num">ACS 60°C Est.</th>
            <th class="num">Pico W</th>
          </tr>
        </thead>
        <tbody>
          {''.join(daily_rows_html)}
        </tbody>
      </table>
    </div>
  </div>

  <div class="card">
    <h2>2. Perfil Horario Medio (00:00 – 23:00, Hora Local)</h2>
    <div class="legend">
      <span>Distribución media de energía consumida en cada franja horaria a lo largo de todos los días monitorizados.</span>
    </div>
    {hourly_svg}
  </div>

  <div class="card">
    <h2>3. Estudio de Amortización: Sustitución por Aerotermo ACS (COP {a['cop']:.1f})</h2>
    <table>
      <thead>
        <tr>
          <th>Horizonte Temporal</th>
          <th class="num">Fleck Nilo 150 (Joule)</th>
          <th class="num">Aerotermo (COP {a['cop']:.1f})</th>
          <th class="num">Energía Ahorrada</th>
          <th class="num">Ahorro Económico</th>
        </tr>
      </thead>
      <tbody>
        <tr>
          <td>Periodo medido ({t['total_days']} días)</td>
          <td class="num">{e['total_kwh']:.2f} kWh ({e['total_cost_eur']:.2f} €)</td>
          <td class="num">{a['period_aerotherm_kwh']:.2f} kWh ({a['period_aerotherm_cost_eur']:.2f} €)</td>
          <td class="num">{a['period_savings_kwh']:.2f} kWh</td>
          <td class="num"><strong>{a['period_savings_eur']:.2f} €</strong></td>
        </tr>
        <tr>
          <td><strong>Proyección 1 Año (365 días)</strong></td>
          <td class="num">{a['annual_fleck_kwh']:.0f} kWh ({a['annual_fleck_cost_eur']:.2f} €)</td>
          <td class="num">{a['annual_aerotherm_kwh']:.0f} kWh ({a['annual_aerotherm_cost_eur']:.2f} €)</td>
          <td class="num">{a['annual_savings_kwh']:.0f} kWh/año</td>
          <td class="num"><strong style="color:var(--green)">{a['annual_savings_eur']:.2f} €/año</strong></td>
        </tr>
        <tr>
          <td>Acumulado a 5 años</td>
          <td class="num">{a['annual_fleck_cost_eur'] * 5:.2f} €</td>
          <td class="num">{a['annual_aerotherm_cost_eur'] * 5:.2f} €</td>
          <td class="num">{a['annual_savings_kwh'] * 5:.0f} kWh</td>
          <td class="num"><strong>{a['annual_savings_eur'] * 5:.2f} €</strong></td>
        </tr>
        <tr>
          <td>Acumulado a 10 años</td>
          <td class="num">{a['annual_fleck_cost_eur'] * 10:.2f} €</td>
          <td class="num">{a['annual_aerotherm_cost_eur'] * 10:.2f} €</td>
          <td class="num">{a['annual_savings_kwh'] * 10:.0f} kWh</td>
          <td class="num"><strong>{a['annual_savings_eur'] * 10:.2f} €</strong></td>
        </tr>
      </tbody>
    </table>
    <div class="highlight-box">
      <strong>⚖️ Elegibilidad Deducción Fiscal IRPF (RDL 19/2021):</strong> La sustitución del termo de efecto Joule por bomba de calor ACS (COP {a['cop']:.1f}) supone una reducción estimada del <strong>{a['reduction_pct']:.1f}%</strong> en el consumo de energía primaria no renovable de ACS (umbral legal ≥ 30% para deducción del 40% en IRPF mediante Certificado de Eficiencia Energética antes y después).
    </div>
  </div>

  <div class="card">
    <h2>4. Registro Cronológico Completo de Ciclos ({len(cycles_log)} encendidos)</h2>
    <p style="font-size:0.85rem;color:var(--muted);margin-top:0;">
      Régimen resistencias: <strong>1.250 W</strong> ({c['power_modes']['half_1250w']['count']} ciclos · {c['power_modes']['half_1250w']['total_min']} min · {c['power_modes']['half_1250w']['kwh']:.3f} kWh) &nbsp;|&nbsp;
      <strong>2.500 W</strong> ({c['power_modes']['full_2500w']['count']} ciclos · {c['power_modes']['full_2500w']['total_min']} min · {c['power_modes']['full_2500w']['kwh']:.3f} kWh) &nbsp;|&nbsp;
      Standby acumulado: {c['standby_kwh']:.3f} kWh
    </p>
    <div style="overflow-x:auto;">
      <table>
        <thead>
          <tr>
            <th class="num">#</th>
            <th>Inicio (Local)</th>
            <th>Fin (Local)</th>
            <th class="num">Duración</th>
            <th class="num">Energía</th>
            <th class="num">Coste</th>
            <th class="num">Pot. Media / Máx</th>
            <th>Modo</th>
            <th>Clasificación</th>
          </tr>
        </thead>
        <tbody>
          {''.join(cycle_rows_html)}
        </tbody>
      </table>
    </div>
  </div>
</div>
</body>
</html>
"""


def export_cold_archive(
    db_path: Path = DB_PATH,
    archive_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    Genera el paquete completo de archivo histórico en frío y devuelve las rutas creadas
    junto con el diccionario de datos calculados.
    """
    data = build_extended_historical_data(db_path)
    if not data.get("has_data"):
        raise RuntimeError(data.get("message", "No hay datos en la base de datos para archivar."))

    if archive_dir is None:
        start_tag = str(data["time"]["start_local"])[:10].replace("-", "")
        end_tag = str(data["time"]["end_local"])[:10].replace("-", "")
        archive_dir = db_path.resolve().parent.parent / "archive" / f"fleck_nilo_{start_tag}_{end_tag}"

    archive_dir.mkdir(parents=True, exist_ok=True)

    # 1. HTML interactivo / imprimible a PDF
    html_path = archive_dir / "INFORME_HISTORICO_FLECK_NILO_150.html"
    html_path.write_text(format_html_archive(data), encoding="utf-8")

    # 2. Markdown completo
    md_path = archive_dir / "INFORME_HISTORICO_FLECK_NILO_150.md"
    md_path.write_text(format_markdown_archive(data), encoding="utf-8")

    # 3. Informe texto plano tipo consola
    txt_path = archive_dir / "RESUMEN_CONSOLA.txt"
    txt_path.write_text(format_cli_report(data), encoding="utf-8")

    # 4. JSON completo de métricas
    json_path = archive_dir / "resumen_metricas.json"
    json_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    # 5. CSV desglose diario
    daily_csv_path = archive_dir / "desglose_diario.csv"
    daily_fields = [
        "date",
        "weekday",
        "hours_monitored",
        "is_complete_day",
        "samples_count",
        "total_kwh",
        "cost_eur",
        "draw_kwh",
        "draw_cycles",
        "maint_kwh",
        "maint_cycles",
        "standby_kwh",
        "estimated_liters_60c",
        "max_power_w",
    ]
    with daily_csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=daily_fields)
        writer.writeheader()
        for row in data.get("daily_series", []):
            writer.writerow({k: row.get(k) for k in daily_fields})

    # 6. CSV historial de ciclos
    cycles_csv_path = archive_dir / "historial_ciclos.csv"
    cycle_fields = [
        "index",
        "start_local",
        "end_local",
        "duration_min",
        "duration_sec",
        "energy_wh",
        "energy_kwh",
        "cost_eur",
        "avg_power_w",
        "max_power_w",
        "power_mode",
        "cycle_type",
        "status",
    ]
    with cycles_csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=cycle_fields)
        writer.writeheader()
        for row in data.get("cycles_log", []):
            writer.writerow({k: row.get(k) for k in cycle_fields})

    # 7. Copia congelada e íntegra de SQLite (consolidando WAL con backup nativo)
    frozen_db_path = archive_dir / "fleck_monitor_test_frozen.db"
    if frozen_db_path.exists():
        frozen_db_path.unlink()
    src_conn = sqlite3.connect(str(db_path), timeout=30.0)
    dst_conn = sqlite3.connect(str(frozen_db_path))
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()

    return {
        "archive_dir": archive_dir,
        "html_path": html_path,
        "md_path": md_path,
        "txt_path": txt_path,
        "json_path": json_path,
        "daily_csv_path": daily_csv_path,
        "cycles_csv_path": cycles_csv_path,
        "frozen_db_path": frozen_db_path,
        "data": data,
    }
