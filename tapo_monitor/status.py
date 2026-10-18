"""
Consulta de estado instantáneo y diagnóstico de salud del congelador en Tapo Monitor.
"""

from typing import Any, Sequence

ACTIVE_POWER_THRESHOLD_W: float = 5.0
MAX_CONSECUTIVE_IDLE_READINGS: int = 8


def _extract_current_power_w(device_data: dict[str, Any], recent_readings: Sequence[float]) -> float:
    """Extrae la potencia actual en vatios desde device_data o usa la última lectura disponible."""
    for key in ("current_power_w", "current_power", "power_w", "power", "current_consumption"):
        if key in device_data and device_data[key] is not None:
            try:
                return float(device_data[key])
            except (TypeError, ValueError):
                pass

    if "power_mw" in device_data and device_data["power_mw"] is not None:
        try:
            return float(device_data["power_mw"]) / 1000.0
        except (TypeError, ValueError):
            pass

    if recent_readings:
        return float(recent_readings[-1])

    return 0.0


def _count_trailing_idle_readings(readings: Sequence[float], threshold_w: float) -> int:
    """Cuenta cuántas lecturas consecutivas al final de la serie están por debajo del umbral."""
    count = 0
    for val in reversed(readings):
        if float(val) < threshold_w:
            count += 1
        else:
            break
    return count


def get_freezer_status(device_data: dict, recent_readings: list[float]) -> dict:
    """
    Evalúa el estado instantáneo y diagnóstico de salud del congelador.

    Args:
        device_data: Diccionario con el estado del enchufe (ej. ``device_on``, ``current_power_w``).
        recent_readings: Lista de lecturas recientes de potencia en vatios.

    Returns:
        Diccionario con:
        - ``is_on`` (bool): indica si el dispositivo está encendido.
        - ``current_power_w`` (float): potencia actual en vatios.
        - ``recent_avg_power_w`` (float): media aritmética de ``recent_readings`` (0.0 si está vacía).
        - ``health_status`` (str): ``"OK"``, ``"STANDBY"`` o ``"ALERT"``.
    """
    data = device_data if isinstance(device_data, dict) else {}
    readings = [float(r) for r in recent_readings] if recent_readings else []

    raw_on = data.get("device_on", data.get("is_on", False))
    is_on = bool(raw_on)

    current_power_w = _extract_current_power_w(data, readings)
    recent_avg_power_w = float(sum(readings) / len(readings)) if readings else 0.0

    has_recent_activity = any(r >= ACTIVE_POWER_THRESHOLD_W for r in readings)
    trailing_idle = _count_trailing_idle_readings(readings, ACTIVE_POWER_THRESHOLD_W)

    if not is_on:
        health_status = "ALERT"
    elif current_power_w >= ACTIVE_POWER_THRESHOLD_W:
        health_status = "OK"
    elif has_recent_activity and trailing_idle < MAX_CONSECUTIVE_IDLE_READINGS:
        health_status = "STANDBY"
    else:
        health_status = "ALERT"

    return {
        "is_on": is_on,
        "current_power_w": current_power_w,
        "recent_avg_power_w": recent_avg_power_w,
        "health_status": health_status,
    }
