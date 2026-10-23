"""
src/report.py - Generador de informe analítico CLI para el test de monitorización
del termo eléctrico Fleck Nilo 150 y simulación comparativa con Aerotermo (COP 3.0).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

from src.config import (
    AEROTHERM_COP,
    DB_PATH,
    ELECTRICITY_PRICE_EUR_KWH,
    FLECK_CAPACITY_L,
    FLECK_TARGET_TEMP_C,
    SPECIFIC_HEAT_WATER_WH_L_C,
    TELEGRAM_CHAT_ID,
    TELEGRAM_TOKEN,
    WATER_INLET_TEMP_C,
)
from src.db import get_db_connection


def calculate_metrics(db_path: Path = DB_PATH) -> Dict[str, Any]:
    """Extrae y calcula todas las métricas energéticas, térmicas y económicas de la prueba."""
    if not db_path.exists():
        raise FileNotFoundError(
            f"No se encontró la base de datos en '{db_path}'. Asegúrate de haber iniciado src.collector primero."
        )

    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()

        # Metadatos
        cursor.execute("SELECT key, value FROM test_metadata;")
        metadata = {row["key"]: row["value"] for row in cursor.fetchall()}

        # Muestras crudas
        cursor.execute(
            """
            SELECT 
                COUNT(*) as count,
                MIN(timestamp_utc) as min_ts_utc,
                MAX(timestamp_utc) as max_ts_utc,
                MIN(timestamp_local) as min_ts_local,
                MAX(timestamp_local) as max_ts_local,
                AVG(power_w) as avg_power_w,
                MAX(power_w) as max_power_w
            FROM raw_samples;
            """
        )
        samples_summary = cursor.fetchone()

        # Ciclos de calentamiento
        cursor.execute(
            """
            SELECT * FROM heating_cycles ORDER BY start_time ASC;
            """
        )
        cycles = cursor.fetchall()

    samples_count = samples_summary["count"] or 0
    if samples_count == 0:
        return {"has_data": False, "message": "Aún no hay muestras registradas en raw_samples."}

    # Cálculo de tiempo total monitorizado
    start_dt = datetime.fromisoformat(samples_summary["min_ts_utc"].replace("Z", "+00:00"))
    end_dt = datetime.fromisoformat(samples_summary["max_ts_utc"].replace("Z", "+00:00"))
    total_seconds = max((end_dt - start_dt).total_seconds(), 1.0)
    total_hours = total_seconds / 3600.0
    total_days = total_seconds / 86400.0

    # Desglose de ciclos por tipo
    maint_cycles: List[sqlite3.Row] = [c for c in cycles if c["cycle_type"] == "MAINTENANCE_LOSS"]
    draw_cycles: List[sqlite3.Row] = [c for c in cycles if c["cycle_type"] == "WATER_DRAW_RECOVERY"]

    # Desglose de ciclos por potencia
    half_cycles: List[sqlite3.Row] = [c for c in cycles if c["power_mode"] == "HALF_1250W"]
    full_cycles: List[sqlite3.Row] = [c for c in cycles if c["power_mode"] == "FULL_2500W"]

    # Energías
    total_cycle_energy_wh = sum(c["energy_wh"] for c in cycles)
    maint_energy_wh = sum(c["energy_wh"] for c in maint_cycles)
    draw_energy_wh = sum(c["energy_wh"] for c in draw_cycles)

    half_energy_wh = sum(c["energy_wh"] for c in half_cycles)
    full_energy_wh = sum(c["energy_wh"] for c in full_cycles)

    half_duration_sec = sum(c["duration_sec"] for c in half_cycles)
    full_duration_sec = sum(c["duration_sec"] for c in full_cycles)
    total_cycle_duration_sec = sum(c["duration_sec"] for c in cycles)

    # Si hay standby residual fuera de ciclos, calcular energía total integrada de samples
    # Cada muestra representa aprox. POLL_INTERVAL_SEC (15s)
    # E_total_samples = avg_power_w * total_hours
    total_electrical_energy_kwh = (samples_summary["avg_power_w"] * total_hours) / 1000.0
    total_cycle_kwh = total_cycle_energy_wh / 1000.0
    maint_kwh = maint_energy_wh / 1000.0
    draw_kwh = draw_energy_wh / 1000.0

    maint_pct = (maint_kwh / total_cycle_kwh * 100.0) if total_cycle_kwh > 0 else 0.0
    draw_pct = (draw_kwh / total_cycle_kwh * 100.0) if total_cycle_kwh > 0 else 0.0

    # Estimación de litros de agua caliente a 60°C
    delta_t = max(FLECK_TARGET_TEMP_C - WATER_INLET_TEMP_C, 1.0)
    wh_per_liter = delta_t * SPECIFIC_HEAT_WATER_WH_L_C  # aprox 45 * 1.163 = 52.335 Wh / L
    estimated_hot_water_liters = (draw_energy_wh / wh_per_liter) if wh_per_liter > 0 else 0.0
    liters_per_day = estimated_hot_water_liters / max(total_days, 0.001)

    # Simulación Aerotermo (COP 3.0)
    # La bomba de calor consume 1 / COP de la energía térmica útil entregada
    aerotherm_cop = AEROTHERM_COP if AEROTHERM_COP > 0 else 3.0
    aerotherm_period_kwh = total_electrical_energy_kwh / aerotherm_cop

    fleck_period_cost_eur = total_electrical_energy_kwh * ELECTRICITY_PRICE_EUR_KWH
    aerotherm_period_cost_eur = aerotherm_period_kwh * ELECTRICITY_PRICE_EUR_KWH
    period_savings_eur = fleck_period_cost_eur - aerotherm_period_cost_eur
    period_savings_kwh = total_electrical_energy_kwh - aerotherm_period_kwh

    # Proyección Anual (365 días)
    annual_fleck_kwh = (total_electrical_energy_kwh / max(total_days, 0.001)) * 365.0
    annual_aerotherm_kwh = annual_fleck_kwh / aerotherm_cop
    annual_fleck_cost_eur = annual_fleck_kwh * ELECTRICITY_PRICE_EUR_KWH
    annual_aerotherm_cost_eur = annual_aerotherm_kwh * ELECTRICITY_PRICE_EUR_KWH
    annual_savings_eur = annual_fleck_cost_eur - annual_aerotherm_cost_eur
    annual_savings_kwh = annual_fleck_kwh - annual_aerotherm_kwh

    # Reducción de consumo (justificación IRPF)
    reduction_pct = ((annual_fleck_kwh - annual_aerotherm_kwh) / annual_fleck_kwh * 100.0) if annual_fleck_kwh > 0 else 0.0

    return {
        "has_data": True,
        "metadata": metadata,
        "time": {
            "start_local": samples_summary["min_ts_local"],
            "end_local": samples_summary["max_ts_local"],
            "total_hours": round(total_hours, 2),
            "total_days": round(total_days, 2),
            "samples_count": samples_count,
        },
        "energy": {
            "total_kwh": round(total_electrical_energy_kwh, 3),
            "daily_avg_kwh": round(total_electrical_energy_kwh / max(total_days, 0.001), 3),
            "total_cost_eur": round(fleck_period_cost_eur, 2),
            "electricity_price_eur_kwh": ELECTRICITY_PRICE_EUR_KWH,
            "avg_power_w": round(samples_summary["avg_power_w"], 1),
            "max_power_w": round(samples_summary["max_power_w"], 1),
        },
        "cycles_breakdown": {
            "total_starts": len(cycles),
            "avg_duration_min": round((total_cycle_duration_sec / len(cycles) / 60.0), 1) if cycles else 0.0,
            "maintenance": {
                "count": len(maint_cycles),
                "kwh": round(maint_kwh, 3),
                "percentage": round(maint_pct, 1),
                "daily_loss_kwh": round(maint_kwh / max(total_days, 0.001), 3),
                "daily_loss_cost_eur": round((maint_kwh / max(total_days, 0.001)) * ELECTRICITY_PRICE_EUR_KWH, 2),
                "avg_cycle_wh": round(maint_energy_wh / len(maint_cycles), 1) if maint_cycles else 0.0,
            },
            "water_draw": {
                "count": len(draw_cycles),
                "kwh": round(draw_kwh, 3),
                "percentage": round(draw_pct, 1),
                "daily_draw_kwh": round(draw_kwh / max(total_days, 0.001), 3),
                "avg_cycle_kwh": round((draw_kwh / len(draw_cycles)), 3) if draw_cycles else 0.0,
            },
            "power_modes": {
                "half_1250w": {
                    "count": len(half_cycles),
                    "total_min": round(half_duration_sec / 60.0, 1),
                    "kwh": round(half_energy_wh / 1000.0, 3),
                },
                "full_2500w": {
                    "count": len(full_cycles),
                    "total_min": round(full_duration_sec / 60.0, 1),
                    "kwh": round(full_energy_wh / 1000.0, 3),
                },
            },
        },
        "water": {
            "estimated_liters_60c": round(estimated_hot_water_liters, 1),
            "daily_avg_liters_60c": round(liters_per_day, 1),
            "equivalent_showers_approx": round(estimated_hot_water_liters / 25.0, 1),  # ~25L de agua a 60°C por ducha estándar
            "target_temp_c": FLECK_TARGET_TEMP_C,
            "inlet_temp_c": WATER_INLET_TEMP_C,
        },
        "aerotherm_simulation": {
            "cop": aerotherm_cop,
            "period_aerotherm_kwh": round(aerotherm_period_kwh, 3),
            "period_aerotherm_cost_eur": round(aerotherm_period_cost_eur, 2),
            "period_savings_kwh": round(period_savings_kwh, 3),
            "period_savings_eur": round(period_savings_eur, 2),
            "annual_fleck_kwh": round(annual_fleck_kwh, 1),
            "annual_aerotherm_kwh": round(annual_aerotherm_kwh, 1),
            "annual_fleck_cost_eur": round(annual_fleck_cost_eur, 2),
            "annual_aerotherm_cost_eur": round(annual_aerotherm_cost_eur, 2),
            "annual_savings_kwh": round(annual_savings_kwh, 1),
            "annual_savings_eur": round(annual_savings_eur, 2),
            "reduction_pct": round(reduction_pct, 1),
            "irpf_eligible": reduction_pct >= 30.0,
        },
    }


def format_cli_report(data: Dict[str, Any]) -> str:
    """Genera el informe textual con diseño visual tipo terminal ejecutiva."""
    if not data.get("has_data"):
        return f"\n⚠️  {data.get('message', 'No hay datos disponibles.')}\n"

    t = data["time"]
    e = data["energy"]
    c = data["cycles_breakdown"]
    w = data["water"]
    a = data["aerotherm_simulation"]

    sep = "═" * 74
    subsep = "─" * 74

    lines = [
        "",
        sep,
        "   📊 INFORME DE MONITORIZACIÓN: TERMO ELÉCTRICO FLECK NILO 150 (60°C)",
        "           Prueba Temporal de Rendimiento, Demanda y Aerotermia",
        sep,
        f" • Periodo Evaluado   : {t['start_local'][:19]} ➔ {t['end_local'][:19]}",
        f" • Duración Total     : {t['total_hours']} horas ({t['total_days']} días) | {t['samples_count']} muestras",
        f" • Tarifa Eléctrica   : {e['electricity_price_eur_kwh']:.3f} €/kWh",
        subsep,
        " 1. BALANCE ENERGÉTICO GLOBAL DE LA PRUEBA",
        subsep,
        f"   Consumo Total Medido        : {e['total_kwh']:8.3f} kWh   ({e['total_cost_eur']:6.2f} €)",
        f"   Promedio Diario             : {e['daily_avg_kwh']:8.3f} kWh/día ({(e['daily_avg_kwh'] * e['electricity_price_eur_kwh']):6.2f} €/día)",
        f"   Potencia Media Global       : {e['avg_power_w']:8.1f} W",
        f"   Potencia Máxima Registrada  : {e['max_power_w']:8.1f} W",
        subsep,
        " 2. DESGLOSE TÉRMICO: PÉRDIDAS ESTÁTICAS vs DEMANDA REAL (DUCHAS)",
        subsep,
        f"   Arranques Totales Termostato: {c['total_starts']} ciclos (Duración media: {c['avg_duration_min']} min)",
        "",
        f"   📉 PÉRDIDAS DE MANTENIMIENTO (Agua enfriándose en el tanque sin usar):",
        f"      • Número de ciclos      : {c['maintenance']['count']:3d} ciclos ({c['maintenance']['percentage']:5.1f}% de la energía)",
        f"      • Energía Desperdiciada : {c['maintenance']['kwh']:7.3f} kWh ({c['maintenance']['daily_loss_kwh']:.3f} kWh/día)",
        f"      • Coste Pérdidas Estát. : {c['maintenance']['daily_loss_cost_eur'] * t['total_days']:6.2f} € en la prueba ({c['maintenance']['daily_loss_cost_eur']:.2f} €/día)",
        f"      • Media por ciclo pérd. : {c['maintenance']['avg_cycle_wh']:.0f} Wh",
        "",
        f"   🚿 DEMANDA REAL DE USO (Extracciones de agua caliente / Duchas):",
        f"      • Número de ciclos      : {c['water_draw']['count']:3d} ciclos ({c['water_draw']['percentage']:5.1f}% de la energía)",
        f"      • Energía Calentamiento : {c['water_draw']['kwh']:7.3f} kWh ({c['water_draw']['daily_draw_kwh']:.3f} kWh/día)",
        f"      • Media por ciclo ducha : {c['water_draw']['avg_cycle_kwh']:.3f} kWh",
        subsep,
        " 3. RÉGIMEN DE FUNCIONAMIENTO DE LAS RESISTENCIAS (Fleck Nilo 150)",
        subsep,
        f"   • Modo Resistencia 1.250 W : {c['power_modes']['half_1250w']['count']:3d} veces | {c['power_modes']['half_1250w']['total_min']:6.1f} min | {c['power_modes']['half_1250w']['kwh']:6.3f} kWh",
        f"   • Modo Doble Res. 2.500 W  : {c['power_modes']['full_2500w']['count']:3d} veces | {c['power_modes']['full_2500w']['total_min']:6.1f} min | {c['power_modes']['full_2500w']['kwh']:6.3f} kWh",
        subsep,
        " 4. BALANCE HIDRÁULICO ESTIMADO",
        subsep,
        f"   • Salto Térmico (\u0394T)       : {w['inlet_temp_c']:.0f}°C (red) ➔ {w['target_temp_c']:.0f}°C (termo) [\u0394T = 45°C]",
        f"   • Agua Caliente a 60°C     : {w['estimated_liters_60c']:7.1f} Litros consumidos en la prueba",
        f"   • Promedio Diario Agua ACS : {w['daily_avg_liters_60c']:7.1f} L/día a 60°C (~{w['equivalent_showers_approx']:.1f} duchas equivalentes)",
        subsep,
        " 5. SIMULACIÓN COMPARATIVA: AEROTERMO ACS (BOMBA DE CALOR COP 3.0)",
        subsep,
        f"   Métricas en el periodo de prueba ({t['total_days']} días):",
        f"   ┌───────────────────────────┬───────────────┬───────────────┬───────────────┐",
        f"   │ Equipo                    │ Consumo (kWh) │ Gasto (€)     │ Ahorro Directo│",
        f"   ├───────────────────────────┼───────────────┼───────────────┼───────────────┤",
        f"   │ Termo Fleck Nilo (Joule)  │ {e['total_kwh']:11.3f} kWh │ {e['total_cost_eur']:11.2f} € │     Base      │",
        f"   │ Aerotermo (COP 3.0)       │ {a['period_aerotherm_kwh']:11.3f} kWh │ {a['period_aerotherm_cost_eur']:11.2f} € │ {a['period_savings_eur']:9.2f} € │",
        f"   └───────────────────────────┴───────────────┴───────────────┴───────────────┘",
        "",
        f"   🔮 PROYECCIÓN ANUALIZADA (365 días):",
        f"   • Consumo Anual Fleck Nilo  : {a['annual_fleck_kwh']:8.1f} kWh/año  ➔  {a['annual_fleck_cost_eur']:7.2f} €/año",
        f"   • Consumo Anual Aerotermo   : {a['annual_aerotherm_kwh']:8.1f} kWh/año  ➔  {a['annual_aerotherm_cost_eur']:7.2f} €/año",
        f"   • AHORRO ECONÓMICO ANUAL    : {a['annual_savings_eur']:8.2f} €/año  ({a['annual_savings_kwh']:.0f} kWh/año no consumidos)",
        f"   • Ahorro acumulado a 5 años : {a['annual_savings_eur'] * 5:8.2f} €",
        f"   • Ahorro acumulado a 10 años: {a['annual_savings_eur'] * 10:8.2f} €",
        "",
        f"   ⚖️  JUSTIFICACIÓN FISCAL DEDUCCIÓN IRPF (RDL 19/2021 y Ley 10/2022):",
        f"   • Reducción de Energía Primaria No Renovable estimada: {a['reduction_pct']:.1f}% (Requisito legal: \u2265 30%)",
        f"   • Estado para Deducción IRPF (40% o 60% coste obra) : {'✅ CUMPLE AMPLIAMENTE (66.7% reducción)' if a['irpf_eligible'] else '❌ No alcanza el 30%'}",
        f"   • Justificación: Al triplicar la eficiencia estacional (COP 3.0 vs efecto Joule COP 1.0),",
        f"     el paso a Aerotermia reduce 2 de cada 3 kWh consumidos, acreditando el salto",
        f"     de letra en el Certificado de Eficiencia Energética (CEE) antes/después.",
        sep,
        "",
    ]
    return "\n".join(lines)


def send_report_telegram(report_text: str) -> bool:
    """Envía el informe ejecutivo a Telegram si está configurado."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Aviso: No se configuraron TELEGRAM_TOKEN o TELEGRAM_CHAT_ID en .env.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    # Telegram mensaje máximo 4096 caracteres
    chunks = [report_text[i:i + 4000] for i in range(0, len(report_text), 4000)]
    for chunk in chunks:
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": f"```\n{chunk}\n```",
            "parse_mode": "Markdown",
        }
        try:
            r = requests.post(url, json=payload, timeout=15)
            r.raise_for_status()
        except Exception as exc:
            print(f"Error al enviar a Telegram: {exc}")
            return False
    return True


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generador de informes analíticos para el test del termo Fleck Nilo 150."
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DB_PATH,
        help=f"Ruta a la base de datos SQLite (por defecto: {DB_PATH})",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Imprime las métricas calculadas en formato JSON estructurado.",
    )
    parser.add_argument(
        "--export-json",
        type=Path,
        default=None,
        help="Guarda las métricas en un archivo JSON específico.",
    )
    parser.add_argument(
        "--telegram",
        action="store_true",
        help="Envía el informe formateado directamente al chat de Telegram configurado.",
    )
    args = parser.parse_args()

    try:
        data = calculate_metrics(args.db)
    except Exception as exc:
        print(f"Error calculando métricas: {exc}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return

    if args.export_json:
        args.export_json.parent.mkdir(parents=True, exist_ok=True)
        args.export_json.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Métricas exportadas a: {args.export_json}")

    report_output = format_cli_report(data)
    print(report_output)

    if args.telegram:
        print("Enviando informe a Telegram...")
        if send_report_telegram(report_output):
            print("✅ Informe enviado exitosamente a Telegram.")


if __name__ == "__main__":
    main()
