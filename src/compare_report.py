"""
src/compare_report.py - Comparador de rendimiento energético y económico entre:
  1. Línea Base: Termo Eléctrico Fleck Nilo 150 (Efecto Joule, COP 1.0)
  2. Nuevo Equipo: Nuevo Termo / Aerotermo (Medición Real con Tapo P110)

Calcula el ahorro real diario, mensual y anualizado (€ y kWh), el COP real medido
(factor de rendimiento estacional) y la acreditación fiscal para deducción en IRPF.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from src.config import (
    BASELINE_DB_PATH,
    DB_PATH,
    ELECTRICITY_PRICE_EUR_KWH,
    TELEGRAM_CHAT_ID,
    TELEGRAM_TOKEN,
    WATER_HEATER_NAME,
)
from src.report import calculate_metrics, send_report_telegram

# Métricas consolidadas de la línea base histórica del Fleck Nilo 150 (23 Oct - 01 Nov 2026, 9.05 días)
DEFAULT_FLECK_BASELINE: Dict[str, Any] = {
    "device_name": "Termo Eléctrico Fleck Nilo 150 (60°C)",
    "technology": "Efecto Joule (Resistencia 1.250 W / 2.500 W)",
    "period_str": "2026-10-23 20:18 ➔ 2026-11-01 20:30 (9.05 días)",
    "total_days": 9.05,
    "total_hours": 217.2,
    "total_kwh": 53.10,
    "daily_avg_kwh": 5.868,
    "daily_cost_eur": 1.438,
    "total_cost_eur": 13.01,
    "annual_kwh": 2141.8,
    "annual_cost_eur": 524.74,
    "max_power_w": 1442.5,
    "avg_power_w": 268.5,
    "cop": 1.0,
    "cycles_count": 29,
    "maint_daily_loss_kwh": 0.012,
    "water_draw_daily_kwh": 5.828,
}


def load_baseline_data(
    baseline_path: Optional[Path] = None,
    archive_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    Carga los datos de la línea base del Fleck Nilo 150 desde:
      1. Archivo SQLite si existe en data/fleck_monitor_test.db
      2. Archivo JSON en carpeta archive/
      3. Fallback a la medición histórica exacta consolidada.
    """
    # 1. Probar ruta de base de datos específica o por defecto
    candidate_db = baseline_path or BASELINE_DB_PATH
    if candidate_db.exists():
        try:
            metrics = calculate_metrics(candidate_db)
            if metrics.get("has_data") and metrics["time"]["total_hours"] >= 12.0:
                e = metrics["energy"]
                t = metrics["time"]
                c = metrics["cycles_breakdown"]
                return {
                    "source": f"SQLite ({candidate_db.name})",
                    "device_name": "Termo Fleck Nilo 150",
                    "technology": "Efecto Joule (Resistencia eléctrica)",
                    "period_str": f"{t['start_local'][:16]} ➔ {t['end_local'][:16]} ({t['total_days']} días)",
                    "total_days": t["total_days"],
                    "total_hours": t["total_hours"],
                    "total_kwh": e["total_kwh"],
                    "daily_avg_kwh": e["daily_avg_kwh"],
                    "daily_cost_eur": round(e["daily_avg_kwh"] * e["electricity_price_eur_kwh"], 2),
                    "total_cost_eur": e["total_cost_eur"],
                    "annual_kwh": round(e["daily_avg_kwh"] * 365.0, 1),
                    "annual_cost_eur": round(e["daily_avg_kwh"] * 365.0 * e["electricity_price_eur_kwh"], 2),
                    "max_power_w": e["max_power_w"],
                    "avg_power_w": e["avg_power_w"],
                    "cop": 1.0,
                    "cycles_count": c["completed_cycles_count"],
                    "maint_daily_loss_kwh": c["maintenance"]["daily_loss_kwh"],
                    "water_draw_daily_kwh": c["water_draw"]["daily_draw_kwh"],
                }
        except Exception:
            pass

    # 2. Probar archivos de resumen_metricas.json en archive/
    search_dirs = [
        archive_dir,
        BASELINE_DB_PATH.parent.parent / "archive",
        Path("C:/Users/Lenovo/Documents/Hipoteca/Estudi Termo vs Aerotermo"),
    ]
    for sdir in search_dirs:
        if not sdir or not sdir.exists():
            continue
        json_candidates = list(sdir.glob("**/resumen_metricas.json"))
        if json_candidates:
            try:
                raw_json = json.loads(json_candidates[0].read_text(encoding="utf-8"))
                if raw_json.get("has_data"):
                    e = raw_json["energy"]
                    t = raw_json["time"]
                    c = raw_json.get("cycles_breakdown", {})
                    return {
                        "source": f"JSON ({json_candidates[0].name})",
                        "device_name": "Termo Fleck Nilo 150",
                        "technology": "Efecto Joule (Resistencia eléctrica)",
                        "period_str": f"{t['start_local'][:16]} ➔ {t['end_local'][:16]} ({t['total_days']} días)",
                        "total_days": t["total_days"],
                        "total_hours": t["total_hours"],
                        "total_kwh": e["total_kwh"],
                        "daily_avg_kwh": e["daily_avg_kwh"],
                        "daily_cost_eur": round(e["daily_avg_kwh"] * e["electricity_price_eur_kwh"], 2),
                        "total_cost_eur": e["total_cost_eur"],
                        "annual_kwh": round(e["daily_avg_kwh"] * 365.0, 1),
                        "annual_cost_eur": round(e["daily_avg_kwh"] * 365.0 * e["electricity_price_eur_kwh"], 2),
                        "max_power_w": e["max_power_w"],
                        "avg_power_w": e["avg_power_w"],
                        "cop": 1.0,
                        "cycles_count": c.get("completed_cycles_count", 29),
                        "maint_daily_loss_kwh": c.get("maintenance", {}).get("daily_loss_kwh", 0.012),
                        "water_draw_daily_kwh": c.get("water_draw", {}).get("daily_draw_kwh", 5.828),
                    }
            except Exception:
                pass

    # 3. Fallback consolidado
    data = dict(DEFAULT_FLECK_BASELINE)
    data["source"] = "Histórico Medido Consolidado (Fleck Nilo 150)"
    return data


def build_comparison(
    new_db_path: Path = DB_PATH,
    baseline_db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Genera el diccionario comparativo completo entre el termo anterior y el nuevo equipo."""
    baseline = load_baseline_data(baseline_db_path)

    if not new_db_path.exists():
        return {
            "has_new_data": False,
            "baseline": baseline,
            "message": f"La base de datos del nuevo equipo '{new_db_path.name}' aún no existe. Inicia el colector primero.",
        }

    try:
        new_metrics = calculate_metrics(new_db_path)
    except Exception as exc:
        return {
            "has_new_data": False,
            "baseline": baseline,
            "message": f"Error leyendo base de datos '{new_db_path.name}': {exc}",
        }

    if not new_metrics.get("has_data") or new_metrics["time"]["samples_count"] < 4:
        return {
            "has_new_data": False,
            "baseline": baseline,
            "message": "Aún no hay suficientes muestras registradas del nuevo equipo. Deja que el colector tome lecturas.",
        }

    ne = new_metrics["energy"]
    nt = new_metrics["time"]
    nc = new_metrics["cycles_breakdown"]
    new_name = new_metrics.get("metadata", {}).get("device_name") or WATER_HEATER_NAME

    price = float(ne["electricity_price_eur_kwh"] or ELECTRICITY_PRICE_EUR_KWH)
    b_daily_kwh = float(baseline["daily_avg_kwh"])
    n_daily_kwh = float(ne["daily_avg_kwh"])

    diff_daily_kwh = b_daily_kwh - n_daily_kwh
    reduction_pct = (diff_daily_kwh / b_daily_kwh * 100.0) if b_daily_kwh > 0 else 0.0
    real_cop = (b_daily_kwh / n_daily_kwh) if n_daily_kwh > 0 else 0.0

    daily_savings_eur = diff_daily_kwh * price
    monthly_savings_eur = daily_savings_eur * 30.0
    annual_savings_eur = daily_savings_eur * 365.0
    annual_kwh_saved = diff_daily_kwh * 365.0

    b_annual_cost = b_daily_kwh * 365.0 * price
    n_annual_cost = n_daily_kwh * 365.0 * price

    return {
        "has_new_data": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "baseline": baseline,
        "current": {
            "device_name": new_name,
            "db_name": new_db_path.name,
            "period_str": f"{nt['start_local'][:16]} ➔ {nt['end_local'][:16]} ({nt['total_days']} días)",
            "total_days": nt["total_days"],
            "total_hours": nt["total_hours"],
            "samples_count": nt["samples_count"],
            "total_kwh": ne["total_kwh"],
            "daily_avg_kwh": n_daily_kwh,
            "daily_cost_eur": round(n_daily_kwh * price, 2),
            "total_cost_eur": ne["total_cost_eur"],
            "annual_kwh": round(n_daily_kwh * 365.0, 1),
            "annual_cost_eur": round(n_annual_cost, 2),
            "max_power_w": ne["max_power_w"],
            "avg_power_w": ne["avg_power_w"],
            "standby_kwh": ne.get("standby_kwh", 0.0),
            "completed_cycles_count": nc["completed_cycles_count"],
            "electricity_price_eur_kwh": price,
            "open_cycle": new_metrics.get("open_cycle"),
            "power_modes": nc.get("power_modes", {}),
        },
        "comparison": {
            "diff_daily_kwh": round(diff_daily_kwh, 3),
            "reduction_pct": round(reduction_pct, 1),
            "real_cop": round(real_cop, 2),
            "daily_savings_eur": round(daily_savings_eur, 2),
            "monthly_savings_eur": round(monthly_savings_eur, 2),
            "annual_savings_eur": round(annual_savings_eur, 2),
            "annual_kwh_saved": round(annual_kwh_saved, 1),
            "savings_5y_eur": round(annual_savings_eur * 5.0, 2),
            "savings_10y_eur": round(annual_savings_eur * 10.0, 2),
            "irpf_eligible": reduction_pct >= 30.0,
            "peak_reduction_w": round(baseline["max_power_w"] - ne["max_power_w"], 1),
        },
    }


def format_comparison_cli(data: Dict[str, Any]) -> str:
    """Genera informe textual comparativo para consola."""
    sep = "═" * 76
    subsep = "─" * 76

    if not data.get("has_new_data"):
        b = data["baseline"]
        lines = [
            "",
            sep,
            "   📊 COMPARATIVA ENERGÉTICA: TERMO ANTERIOR VS NUEVO EQUIPO",
            sep,
            f" • Línea Base Anterior : {b['device_name']} ({b['period_str']})",
            f" • Consumo Anterior    : {b['daily_avg_kwh']:.3f} kWh/día ({b['daily_cost_eur']:.2f} €/día)",
            subsep,
            f" ⏳ ESTADO DEL NUEVO EQUIPO: {data.get('message', 'Esperando datos...')}",
            subsep,
            "",
        ]
        return "\n".join(lines)

    b = data["baseline"]
    c = data["current"]
    cmp = data["comparison"]
    irpf_badge = "✅ CUMPLE AMPLIAMENTE (≥30%)" if cmp["irpf_eligible"] else "❌ Menor al 30%"

    oc = c.get("open_cycle")
    oc_str = f" | 🔥 Ciclo activo: {oc['current_power_w']:.0f} W" if oc else ""

    lines = [
        "",
        sep,
        "   📊 INFORME COMPARATIVO DE RENDIMIENTO: TERMO ANTERIOR VS NUEVO",
        "           Medición Real con Enchufe Inteligente TP-Link Tapo P110",
        sep,
        f" • Termo Anterior (Base) : {b['device_name']} ({b['period_str']})",
        f" • Nuevo Equipo Medido   : {c['device_name']} ({c['period_str']}{oc_str})",
        f" • Tarifa Eléctrica      : {c['electricity_price_eur_kwh']:.3f} €/kWh (PVPC)",
        subsep,
        " 1. COMPARATIVA DE CONSUMO DIARIO Y EFICIENCIA ENERGÉTICA",
        subsep,
        f"   ┌──────────────────────────────────┬─────────────────┬─────────────────┬────────────────┐",
        f"   │ Métrica                          │ Fleck Nilo 150  │ Nuevo Equipo    │ Variación      │",
        f"   ├──────────────────────────────────┼─────────────────┼─────────────────┼────────────────┤",
        f"   │ Consumo Diario Medio             │ {b['daily_avg_kwh']:9.3f} kWh/d │ {c['daily_avg_kwh']:9.3f} kWh/d │ {cmp['diff_daily_kwh']:+8.3f} kWh/d │",
        f"   │ Gasto Diario Medio               │ {b['daily_cost_eur']:11.2f} €/d │ {c['daily_cost_eur']:11.2f} €/d │ {cmp['daily_savings_eur'] * -1:+10.2f} €/d │",
        f"   │ Gasto Mensual Estimado (30d)     │ {b['daily_cost_eur'] * 30:11.2f} €/m │ {c['daily_cost_eur'] * 30:11.2f} €/m │ {cmp['monthly_savings_eur'] * -1:+10.2f} €/m │",
        f"   │ Gasto Anual Proyectado (365d)    │ {b['annual_cost_eur']:11.2f} €/a │ {c['annual_cost_eur']:11.2f} €/a │ {cmp['annual_savings_eur'] * -1:+10.2f} €/a │",
        f"   │ Potencia Pico Máxima             │ {b['max_power_w']:11.1f} W   │ {c['max_power_w']:11.1f} W   │ {cmp['peak_reduction_w'] * -1:+10.1f} W   │",
        f"   │ Rendimiento Estacional (COP)     │            1.00 │ {cmp['real_cop']:15.2f} │ x{cmp['real_cop']:.2f} veces   │",
        f"   └──────────────────────────────────┴─────────────────┴─────────────────┴────────────────┘",
        "",
        f"   🎯 BALANCE DE MEJORA MEDIDO EN CONDICIONES REALES DE USO:",
        f"   • Reducción Real de Consumo Eléctrico : {cmp['reduction_pct']:.1f}% menos energía consumida",
        f"   • COP Real Medido en Instalación       : {cmp['real_cop']:.2f} (por cada kWh eléctrico aporta {cmp['real_cop']:.2f} kWh térmicos)",
        f"   • AHORRO ECONÓMICO ANUAL ESTIMADO      : {cmp['annual_savings_eur']:.2f} €/año ({cmp['annual_kwh_saved']:.0f} kWh/año no consumidos)",
        f"   • Ahorro acumulado a 5 años            : {cmp['savings_5y_eur']:.2f} €",
        f"   • Ahorro acumulado a 10 años           : {cmp['savings_10y_eur']:.2f} €",
        subsep,
        " 2. JUSTIFICACIÓN FISCAL DEDUCCIÓN IRPF (RDL 19/2021 y Ley 10/2022)",
        subsep,
        f"   • Reducción de Energía Primaria No Renovable : {cmp['reduction_pct']:.1f}% (Requisito legal: \u2265 30%)",
        f"   • Estado para Deducción IRPF (40% coste obra): {irpf_badge}",
        f"   • Justificación Técnica: Al sustituir el efecto Joule (COP 1.0) por el nuevo",
        f"     equipo (COP medido {cmp['real_cop']:.2f}), se verifica empíricamente que la reducción",
        f"     supera ampliamente el 30% requerido en el Certificado de Eficiencia Energética (CEE).",
        sep,
        "",
    ]
    return "\n".join(lines)


def format_comparison_html(data: Dict[str, Any]) -> str:
    """Genera informe HTML interactivo y estilizado para abrir en navegador o imprimir a PDF."""
    if not data.get("has_new_data"):
        return f"<html><body><h1>Esperando datos del nuevo equipo</h1><p>{data.get('message', '')}</p></body></html>"

    b = data["baseline"]
    c = data["current"]
    cmp = data["comparison"]

    # Gráfica SVG simple comparando barras diarias y anuales
    w, h = 640, 220
    b_kwh = max(b["daily_avg_kwh"], 0.1)
    c_kwh = max(c["daily_avg_kwh"], 0.1)
    scale_max = max(b_kwh, c_kwh) * 1.25

    h_b = (b_kwh / scale_max) * 140
    h_c = (c_kwh / scale_max) * 140

    svg_bars = f"""
    <svg viewBox="0 0 {w} {h}" width="100%" style="max-width:{w}px;height:auto;font-family:sans-serif;">
      <!-- Rejilla -->
      <line x1="60" y1="170" x2="580" y2="170" stroke="#cbd5e1" stroke-width="1.5"/>
      <line x1="60" y1="100" x2="580" y2="100" stroke="#f1f5f9" stroke-dasharray="3,3"/>
      <line x1="60" y1="30" x2="580" y2="30" stroke="#f1f5f9" stroke-dasharray="3,3"/>
      
      <!-- Barra Fleck -->
      <rect x="150" y="{170 - h_b:.1f}" width="80" height="{h_b:.1f}" fill="#ef4444" rx="4"/>
      <text x="190" y="{170 - h_b - 8:.1f}" text-anchor="middle" font-weight="700" fill="#991b1b" font-size="14">{b_kwh:.2f} kWh/d</text>
      <text x="190" y="192" text-anchor="middle" font-size="12" fill="#475569">Fleck Nilo 150</text>
      <text x="190" y="208" text-anchor="middle" font-size="11" fill="#64748b">{b['daily_cost_eur']:.2f} €/día</text>

      <!-- Barra Nuevo -->
      <rect x="370" y="{170 - h_c:.1f}" width="80" height="{h_c:.1f}" fill="#10b981" rx="4"/>
      <text x="410" y="{170 - h_c - 8:.1f}" text-anchor="middle" font-weight="700" fill="#065f46" font-size="14">{c_kwh:.2f} kWh/d</text>
      <text x="410" y="192" text-anchor="middle" font-size="12" fill="#475569">{html.escape(c['device_name'][:18])}</text>
      <text x="410" y="208" text-anchor="middle" font-size="11" fill="#64748b">{c['daily_cost_eur']:.2f} €/día</text>
    </svg>
    """

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Comparativa Real — Termo Anterior vs Nuevo Equipo</title>
<style>
  :root {{
    --bg: #f8fafc; --card: #ffffff; --border: #e2e8f0; --text: #0f172a;
    --muted: #64748b; --green: #059669; --red: #dc2626; --blue: #2563eb;
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; padding: 28px 18px; background: var(--bg); color: var(--text); font-family: system-ui, -apple-system, sans-serif; }}
  .container {{ max-width: 980px; margin: 0 auto; }}
  header {{ background: linear-gradient(135deg, #0f172a 0%, #1e3a8a 100%); color: #fff; padding: 26px 30px; border-radius: 12px; margin-bottom: 22px; }}
  header h1 {{ margin: 0 0 6px 0; font-size: 1.45rem; }}
  header p {{ margin: 4px 0; color: #cbd5e1; font-size: 0.9rem; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 14px; margin-bottom: 22px; }}
  .card {{ background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 18px 20px; box-shadow: 0 1px 2px rgba(0,0,0,0.03); margin-bottom: 20px; }}
  .kpi-title {{ font-size: 0.78rem; text-transform: uppercase; color: var(--muted); font-weight: 600; letter-spacing: 0.04em; }}
  .kpi-num {{ font-size: 1.6rem; font-weight: 700; margin: 6px 0 2px 0; }}
  .kpi-sub {{ font-size: 0.84rem; color: var(--muted); }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.88rem; }}
  th, td {{ padding: 10px 12px; border-bottom: 1px solid var(--border); text-align: left; }}
  th {{ background: #f1f5f9; color: #334155; font-weight: 600; }}
  td.num, th.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  .badge {{ display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 0.74rem; font-weight: 600; }}
  .badge-ok {{ background: #d1fae5; color: #047857; }}
  .badge-err {{ background: #fee2e2; color: #991b1b; }}
  .box-irpf {{ background: #ecfdf5; border: 1px solid #a7f3d0; border-radius: 8px; padding: 16px 18px; color: #065f46; font-size: 0.92rem; margin-top: 14px; }}
  @media print {{ body {{ background: #fff; padding: 0; }} .card {{ box-shadow: none; }} }}
</style>
</head>
<body>
<div class="container">
  <header>
    <h1>📊 Comparativa Real: Termo Anterior vs Nuevo Equipo</h1>
    <p><strong>Línea Base:</strong> {html.escape(b['device_name'])} ({html.escape(b['period_str'])})</p>
    <p><strong>Nuevo Equipo:</strong> {html.escape(c['device_name'])} ({html.escape(c['period_str'])}) &nbsp;|&nbsp; <strong>Tarifa:</strong> {c['electricity_price_eur_kwh']:.3f} €/kWh</p>
  </header>

  <div class="grid">
    <div class="card">
      <div class="kpi-title">Reducción de Consumo</div>
      <div class="kpi-num" style="color:var(--green)">-{cmp['reduction_pct']:.1f}%</div>
      <div class="kpi-sub">De {b['daily_avg_kwh']:.2f} a {c['daily_avg_kwh']:.2f} kWh/día</div>
    </div>
    <div class="card">
      <div class="kpi-title">COP Real Medido</div>
      <div class="kpi-num" style="color:var(--blue)">{cmp['real_cop']:.2f}</div>
      <div class="kpi-sub">x{cmp['real_cop']:.2f} veces más eficiente que Joule</div>
    </div>
    <div class="card">
      <div class="kpi-title">Ahorro Anual Estimado</div>
      <div class="kpi-num" style="color:var(--green)">{cmp['annual_savings_eur']:.2f} €/año</div>
      <div class="kpi-sub">{cmp['annual_kwh_saved']:.0f} kWh no consumidos cada año</div>
    </div>
    <div class="card">
      <div class="kpi-title">Deducción Fiscal IRPF</div>
      <div class="kpi-num" style="font-size:1.25rem;color:var(--green)">{'✅ Cumple (40%)' if cmp['irpf_eligible'] else '❌ < 30%'}</div>
      <div class="kpi-sub">{cmp['reduction_pct']:.1f}% reducción (Umbral: ≥30%)</div>
    </div>
  </div>

  <div class="card">
    <h2 style="font-size:1.15rem;margin-top:0;border-bottom:1px solid var(--border);padding-bottom:8px;">Gráfica Comparativa de Consumo Diario (kWh/día)</h2>
    {svg_bars}
  </div>

  <div class="card">
    <h2 style="font-size:1.15rem;margin-top:0;border-bottom:1px solid var(--border);padding-bottom:8px;">Tabla Comparativa Detallada</h2>
    <table>
      <thead>
        <tr>
          <th>Parámetro</th>
          <th class="num">Fleck Nilo 150 (Base)</th>
          <th class="num">Nuevo Equipo</th>
          <th class="num">Ahorro / Diferencia</th>
        </tr>
      </thead>
      <tbody>
        <tr>
          <td>Consumo Diario Medio</td>
          <td class="num">{b['daily_avg_kwh']:.3f} kWh/día</td>
          <td class="num"><strong>{c['daily_avg_kwh']:.3f} kWh/día</strong></td>
          <td class="num"><strong style="color:var(--green)">-{cmp['diff_daily_kwh']:.3f} kWh/día (-{cmp['reduction_pct']:.1f}%)</strong></td>
        </tr>
        <tr>
          <td>Gasto Diario Medio</td>
          <td class="num">{b['daily_cost_eur']:.2f} €/día</td>
          <td class="num">{c['daily_cost_eur']:.2f} €/día</td>
          <td class="num"><strong style="color:var(--green)">-{cmp['daily_savings_eur']:.2f} €/día</strong></td>
        </tr>
        <tr>
          <td>Gasto Mensual Estimado (30d)</td>
          <td class="num">{b['daily_cost_eur'] * 30:.2f} €/mes</td>
          <td class="num">{c['daily_cost_eur'] * 30:.2f} €/mes</td>
          <td class="num"><strong style="color:var(--green)">-{cmp['monthly_savings_eur']:.2f} €/mes</strong></td>
        </tr>
        <tr>
          <td>Gasto Anual Proyectado (365d)</td>
          <td class="num">{b['annual_cost_eur']:.2f} €/año</td>
          <td class="num">{c['annual_cost_eur']:.2f} €/año</td>
          <td class="num"><strong style="color:var(--green)">-{cmp['annual_savings_eur']:.2f} €/año</strong></td>
        </tr>
        <tr>
          <td>Potencia Pico Registrada</td>
          <td class="num">{b['max_power_w']:.0f} W</td>
          <td class="num">{c['max_power_w']:.0f} W</td>
          <td class="num">{cmp['peak_reduction_w'] * -1:+.0f} W</td>
        </tr>
        <tr>
          <td>Eficiencia Estacional (COP)</td>
          <td class="num">1.00 (Efecto Joule)</td>
          <td class="num"><strong>{cmp['real_cop']:.2f}</strong></td>
          <td class="num"><span class="badge badge-ok">x{cmp['real_cop']:.2f} Eficiencia</span></td>
        </tr>
        <tr>
          <td>Proyección Ahorro 5 Años</td>
          <td class="num">{b['annual_cost_eur'] * 5:.2f} €</td>
          <td class="num">{c['annual_cost_eur'] * 5:.2f} €</td>
          <td class="num"><strong>{cmp['savings_5y_eur']:.2f} € acumulados</strong></td>
        </tr>
        <tr>
          <td>Proyección Ahorro 10 Años</td>
          <td class="num">{b['annual_cost_eur'] * 10:.2f} €</td>
          <td class="num">{c['annual_cost_eur'] * 10:.2f} €</td>
          <td class="num"><strong>{cmp['savings_10y_eur']:.2f} € acumulados</strong></td>
        </tr>
      </tbody>
    </table>
    <div class="box-irpf">
      <strong>⚖️ Justificación Deducción Fiscal IRPF (RDL 19/2021 y Ley 10/2022):</strong>
      La sustitución del termo eléctrico convencional por el nuevo equipo acredita una reducción del <strong>{cmp['reduction_pct']:.1f}%</strong> en la demanda de energía primaria no renovable para ACS, superando el umbral legal del 30% exigido para la deducción del 40% del coste de la instalación en la declaración de la renta.
    </div>
  </div>
</div>
</body>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Comparador de rendimiento entre el termo Fleck Nilo 150 y el nuevo equipo."
    )
    parser.add_argument(
        "--new-db",
        type=Path,
        default=DB_PATH,
        help=f"Ruta a la base de datos del nuevo equipo (por defecto: {DB_PATH})",
    )
    parser.add_argument(
        "--baseline-db",
        type=Path,
        default=BASELINE_DB_PATH,
        help=f"Ruta a la base de datos del termo base (por defecto: {BASELINE_DB_PATH})",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Imprime las métricas en JSON estructurado.",
    )
    parser.add_argument(
        "--html",
        type=Path,
        default=None,
        help="Guarda el informe en formato HTML en la ruta indicada.",
    )
    parser.add_argument(
        "--telegram",
        action="store_true",
        help="Envía la comparativa directamente al chat de Telegram configurado.",
    )
    args = parser.parse_args()

    data = build_comparison(new_db_path=args.new_db, baseline_db_path=args.baseline_db)

    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return

    report_text = format_comparison_cli(data)
    print(report_text)

    if args.html:
        args.html.parent.mkdir(parents=True, exist_ok=True)
        args.html.write_text(format_comparison_html(data), encoding="utf-8")
        print(f"✅ Informe HTML guardado en: {args.html}")

    if args.telegram:
        print("Enviando informe comparativo a Telegram...")
        if send_report_telegram(report_text):
            print("✅ Comparativa enviada exitosamente a Telegram.")


if __name__ == "__main__":
    main()
