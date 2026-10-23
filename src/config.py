"""
src/config.py - Configuración y parámetros técnicos para la monitorización
del termo eléctrico Fleck Nilo 150 con Tapo P110.
"""

from __future__ import annotations

import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR: Path = Path(__file__).resolve().parent.parent
DATA_DIR: Path = BASE_DIR / "data"
DB_PATH: Path = DATA_DIR / "fleck_monitor_test.db"
ENV_FILE: Path = BASE_DIR / ".env"

# Cargar variables de entorno desde el .env raíz
load_dotenv(dotenv_path=ENV_FILE)

# Credenciales Tapo P110 (admite ambas variantes de nombres)
TAPO_IP: str = os.getenv("TAPO_IP", "192.168.1.41").strip()
TAPO_USERNAME: str = (
    os.getenv("TAPO_USERNAME") or os.getenv("TAPO_USER") or ""
).strip()
TAPO_PASSWORD: str = (
    os.getenv("TAPO_PASSWORD") or os.getenv("TAPO_PASS") or ""
).strip()

# Notificaciones opcionales a Telegram
TELEGRAM_TOKEN: str = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# Parámetros del test y muestreo
def _get_float_env(key: str, default: float) -> float:
    try:
        val = os.getenv(key)
        return float(val) if val is not None else default
    except (ValueError, TypeError):
        return default

def _get_int_env(key: str, default: int) -> int:
    try:
        val = os.getenv(key)
        return int(val) if val is not None else default
    except (ValueError, TypeError):
        return default

POLL_INTERVAL_SEC: int = _get_int_env("POLL_INTERVAL_SEC", 15)
TEST_DURATION_DAYS: int = _get_int_env("TEST_DURATION_DAYS", 3)
ELECTRICITY_PRICE_EUR_KWH: float = _get_float_env("ELECTRICITY_PRICE_EUR_KWH", 0.245)
AEROTHERM_COP: float = _get_float_env("AEROTHERM_COP", 3.0)

# Parámetros térmicos e hidráulicos (Fleck Nilo 150)
FLECK_CAPACITY_L: float = 150.0
FLECK_POWER_HALF_W: float = 1250.0
FLECK_POWER_FULL_W: float = 2500.0
WATER_INLET_TEMP_C: float = _get_float_env("WATER_INLET_TEMP_C", 15.0)
WATER_TARGET_TEMP_C: float = _get_float_env("WATER_TARGET_TEMP_C", 60.0)
FLECK_TARGET_TEMP_C: float = WATER_TARGET_TEMP_C

# Constante termodinámica del agua: 4.186 kJ / (kg * K) = 1.163 Wh / (L * °C)
SPECIFIC_HEAT_WATER_WH_L_C: float = 1.163

# Criterios heurísticos de detección y clasificación de ciclos
CYCLE_START_POWER_W: float = 100.0   # Standby del Fleck Nilo < 5W
CYCLE_END_POWER_W: float = 20.0      # Límite inferior para fin de calentamiento
CYCLE_END_CONSECUTIVE_SAMPLES: int = 2  # 2 lecturas consecutivas (30s con poll de 15s)
POWER_MODE_THRESHOLD_W: float = 1600.0  # < 1600W -> HALF_1250W, >= 1600W -> FULL_2500W

# Umbrales de clasificación de ciclo
MAINTENANCE_LOSS_MAX_WH: float = 350.0

# Franja nocturna típica de pérdidas estáticas sin extracción: 01:30 - 06:30
NIGHT_WINDOW_START_HOUR: float = 1.5   # 01:30
NIGHT_WINDOW_END_HOUR: float = 6.5     # 06:30

# Asegurar existencia del directorio de datos
DATA_DIR.mkdir(parents=True, exist_ok=True)
