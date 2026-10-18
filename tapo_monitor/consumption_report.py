"""
Cálculo y formateo del resumen global de consumo por horas y semanas en Tapo Monitor.
"""

from typing import Any, Sequence

HOURLY_WINDOW_SIZE: int = 24
WEEKLY_WINDOW_DAYS: int = 7
BIWEEKLY_WINDOW_DAYS: int = 14


def _sanitize_series(values: Sequence[float] | None) -> list[float]:
    """Convierte una secuencia de lecturas a lista de flotantes válidos."""
    if not values:
        return []
    result: list[float] = []
    for val in values:
        if val is not None:
            result.append(float(val))
    return result


def _calc_window_stats(series: list[float], window: int) -> tuple[float, float, int]:
    """Calcula (total, promedio, cantidad) para los últimos `window` elementos de la serie."""
    if not series or window <= 0:
        return 0.0, 0.0, 0
    subset = series[-window:]
    count = len(subset)
    total = float(sum(subset))
    avg = float(total / count) if count > 0 else 0.0
    return total, avg, count


def calculate_consumption_summary(hourly_kwh: list[float], daily_kwh: list[float]) -> dict[str, Any]:
    """
    Calcula el consumo total y promedio de las últimas horas (últimas 24 h)
    y de las últimas semanas (últimos 7 y 14 días).

    Args:
        hourly_kwh: Lista cronológica de consumos horarios en kWh.
        daily_kwh: Lista cronológica de consumos diarios en kWh.

    Returns:
        Diccionario con los totales y medias de las últimas 24 horas, 7 días y 14 días.
    """
    clean_hourly = _sanitize_series(hourly_kwh)
    clean_daily = _sanitize_series(daily_kwh)

    last_24h_total, last_24h_avg, hours_count = _calc_window_stats(
        clean_hourly, HOURLY_WINDOW_SIZE
    )
    last_7d_total, last_7d_avg, days_7d_count = _calc_window_stats(
        clean_daily, WEEKLY_WINDOW_DAYS
    )
    last_14d_total, last_14d_avg, days_14d_count = _calc_window_stats(
        clean_daily, BIWEEKLY_WINDOW_DAYS
    )

    return {
        "last_24h_total_kwh": last_24h_total,
        "last_24h_avg_kwh": last_24h_avg,
        "hourly_total_kwh": last_24h_total,
        "hourly_avg_kwh": last_24h_avg,
        "hours_count": hours_count,
        "last_7d_total_kwh": last_7d_total,
        "last_7d_avg_kwh": last_7d_avg,
        "weekly_total_kwh": last_7d_total,
        "weekly_avg_kwh": last_7d_avg,
        "days_7d_count": days_7d_count,
        "last_14d_total_kwh": last_14d_total,
        "last_14d_avg_kwh": last_14d_avg,
        "days_14d_count": days_14d_count,
    }


def format_status_command_output(status_info: dict, consumption_summary: dict) -> str:
    """
    Genera un informe legible con el estado actual del congelador y el resumen
    de consumo energético de las últimas horas y semanas.

    Args:
        status_info: Diccionario devuelto por ``get_freezer_status``.
        consumption_summary: Diccionario devuelto por ``calculate_consumption_summary``.

    Returns:
        Cadena de texto formateada con indicadores de estado y consumo histórico.
    """
    info = status_info if isinstance(status_info, dict) else {}
    summary = consumption_summary if isinstance(consumption_summary, dict) else {}

    health_status = str(info.get("health_status", "ALERT")).upper()
    is_on = bool(info.get("is_on", False))
    power_state_str = "ON (Encendido)" if is_on else "OFF (Apagado)"

    current_power_w = float(info.get("current_power_w", 0.0))
    recent_avg_power_w = float(info.get("recent_avg_power_w", 0.0))

    last_24h_total = float(
        summary.get("last_24h_total_kwh", summary.get("hourly_total_kwh", 0.0))
    )
    last_24h_avg = float(
        summary.get("last_24h_avg_kwh", summary.get("hourly_avg_kwh", 0.0))
    )

    last_7d_total = float(
        summary.get("last_7d_total_kwh", summary.get("weekly_total_kwh", 0.0))
    )
    last_7d_avg = float(
        summary.get("last_7d_avg_kwh", summary.get("weekly_avg_kwh", 0.0))
    )

    last_14d_total = float(summary.get("last_14d_total_kwh", 0.0))
    last_14d_avg = float(summary.get("last_14d_avg_kwh", 0.0))

    lines = [
        "=== RESUMEN DE ESTADO Y CONSUMO DEL CONGELADOR ===",
        f"Estado de salud     : {health_status}",
        f"Enchufe inteligente : {power_state_str}",
        f"Potencia actual     : {current_power_w:.2f} W (Media reciente: {recent_avg_power_w:.2f} W)",
        "--- Consumo últimas horas (24h) ---",
        f"Últimas 24h         : {last_24h_total:.3f} kWh (Promedio horario: {last_24h_avg:.3f} kWh/h)",
        "--- Consumo acumulado semanal ---",
        f"Últimos 7 días      : {last_7d_total:.3f} kWh (Promedio diario: {last_7d_avg:.3f} kWh/día)",
        f"Últimos 14 días     : {last_14d_total:.3f} kWh (Promedio diario: {last_14d_avg:.3f} kWh/día)",
    ]
    return "\n".join(lines)
