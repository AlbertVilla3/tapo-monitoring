#!/usr/bin/env python3
"""
Monitor de Congelador para TP-Link Tapo P110.
Diseñado para ejecutarse periódicamente vía cron (ej. cada 15 minutos).
Detecta pérdida de conexión/corriente, apagado accidental del enchufe inteligente,
o apagado del botón físico del congelador (0W prolongado), enviando alertas a Telegram.
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import requests
from dotenv import load_dotenv
from kasa import Discover

# Configuración de rutas y logs
BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "state.json"
ENV_FILE = BASE_DIR / ".env"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("freezer_monitor")


def load_state(now: float) -> Dict[str, Any]:
    """Carga el estado persistente desde state.json o inicializa uno por defecto."""
    default_state: Dict[str, Any] = {
        "last_active_ts": now,
        "last_energy": -1.0,
        "alert_sent": False,
    }
    if not STATE_FILE.exists():
        return default_state

    try:
        with STATE_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
            return {
                "last_active_ts": float(data.get("last_active_ts", now)),
                "last_energy": float(data.get("last_energy", -1.0)),
                "alert_sent": bool(data.get("alert_sent", False)),
            }
    except Exception as exc:
        logger.warning("No se pudo leer %s (%s). Reiniciando estado.", STATE_FILE, exc)
        return default_state


def save_state(state: Dict[str, Any]) -> None:
    """Guarda el estado actual en state.json de forma atómica."""
    tmp_file = STATE_FILE.with_suffix(".json.tmp")
    try:
        with tmp_file.open("w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        tmp_file.replace(STATE_FILE)
    except Exception as exc:
        logger.error("Error guardando estado en %s: %s", STATE_FILE, exc)


def send_telegram_message(token: str, chat_id: str, text: str) -> bool:
    """Envía un mensaje a Telegram usando la API HTTP Bot."""
    if not token or not chat_id:
        logger.error("Faltan credenciales de Telegram (TELEGRAM_TOKEN o TELEGRAM_CHAT_ID).")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
    }
    try:
        response = requests.post(url, json=payload, timeout=15)
        response.raise_for_status()
        logger.info("Mensaje enviado a Telegram: %s", text)
        return True
    except Exception as exc:
        logger.error("Error enviando mensaje a Telegram: %s", exc)
        return False


def extract_energy_metrics(dev: Any) -> Tuple[float, Optional[float]]:
    """
    Extrae (current_power_w, today_energy) soportando distintas versiones de python-kasa.
    Maneja valores None de forma segura.
    """
    energy_mod = None
    if hasattr(dev, "modules") and isinstance(dev.modules, dict):
        energy_mod = dev.modules.get("Energy") or dev.modules.get("energy")
        if energy_mod is None:
            for key, mod in dev.modules.items():
                if str(key).lower().endswith("energy"):
                    energy_mod = mod
                    break

    current_power: Optional[float] = None
    today_energy: Optional[float] = None

    if energy_mod is not None:
        # Potencia instantánea (W)
        for attr in ("current_consumption", "power"):
            val = getattr(energy_mod, attr, None)
            if val is not None:
                try:
                    current_power = float(val)
                    break
                except (TypeError, ValueError):
                    pass

        # Consumo acumulado hoy (kWh / Wh)
        for attr in ("consumption_today", "today_consumption", "today_energy"):
            val = getattr(energy_mod, attr, None)
            if val is not None:
                try:
                    today_energy = float(val)
                    break
                except (TypeError, ValueError):
                    pass

    # Fallback a emeter_realtime si la versión de python-kasa lo expone en el dispositivo
    if current_power is None and hasattr(dev, "emeter_realtime"):
        try:
            rt = dev.emeter_realtime or {}
            if "power" in rt and rt["power"] is not None:
                current_power = float(rt["power"])
            elif "power_mw" in rt and rt["power_mw"] is not None:
                current_power = float(rt["power_mw"]) / 1000.0
        except Exception:
            pass

    if today_energy is None and hasattr(dev, "emeter_today"):
        try:
            if dev.emeter_today is not None:
                today_energy = float(dev.emeter_today)
        except Exception:
            pass

    return (round(current_power, 2) if current_power is not None else 0.0, today_energy)


async def connect_tapo(ip: str, username: str, password: str, retries: int = 2) -> Any:
    """Conecta con el enchufe Tapo P110 con reintentos breves ante microcortes Wi-Fi."""
    last_err: Optional[Exception] = None
    for attempt in range(1, retries + 2):
        dev = None
        try:
            dev = await Discover.discover_single(
                ip,
                username=username,
                password=password,
                timeout=10,
            )
            await dev.update()
            return dev
        except Exception as exc:
            last_err = exc
            if dev is not None and hasattr(dev, "disconnect"):
                try:
                    await dev.disconnect()
                except Exception:
                    pass
            if attempt <= retries:
                logger.warning(
                    "Intento %d/%d fallido conectando a Tapo (%s): %s. Reintentando...",
                    attempt,
                    retries + 1,
                    ip,
                    exc,
                )
                await asyncio.sleep(3)
    raise RuntimeError(f"No se pudo conectar con Tapo P110 en {ip}: {last_err}") from last_err


async def run_monitor(test_mode: bool = False) -> int:
    """Ejecuta el flujo principal de monitorización o el modo de prueba (--test)."""
    load_dotenv(dotenv_path=ENV_FILE)

    tapo_ip = os.getenv("TAPO_IP", "").strip()
    tapo_user = os.getenv("TAPO_USER", "").strip()
    tapo_pass = os.getenv("TAPO_PASS", "").strip()
    telegram_token = os.getenv("TELEGRAM_TOKEN", "").strip()
    telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    try:
        power_threshold_w = float(os.getenv("POWER_THRESHOLD_W", "5.0"))
    except ValueError:
        power_threshold_w = 5.0

    try:
        energy_delta_threshold_kwh = float(os.getenv("ENERGY_DELTA_THRESHOLD_KWH", "0.002"))
    except ValueError:
        energy_delta_threshold_kwh = 0.002

    try:
        max_inactive_seconds = int(os.getenv("MAX_INACTIVE_SECONDS", "7200"))
    except ValueError:
        max_inactive_seconds = 7200

    if not tapo_ip or not tapo_user or not tapo_pass:
        logger.error("Faltan variables obligatorias en .env (TAPO_IP, TAPO_USER, TAPO_PASS).")
        return 1

    now = time.time()
    state = load_state(now)

    # Si el archivo state.json no existía aún, lo persistimos con el timestamp inicial
    if not STATE_FILE.exists():
        save_state(state)

    dev = None
    try:
        # 1. Control de Conexión (Offline / Corte eléctrico)
        try:
            dev = await connect_tapo(tapo_ip, tapo_user, tapo_pass)
        except Exception as conn_err:
            logger.error("Fallo de conexión con el Tapo P110: %s", conn_err)
            if test_mode:
                return 1

            inactive_seconds = now - state["last_active_ts"]
            logger.info(
                "Enchufe sin conexión. Tiempo desde última actividad: %ds (Umbral: %ds)",
                int(inactive_seconds),
                max_inactive_seconds,
            )
            if inactive_seconds > max_inactive_seconds and not state["alert_sent"]:
                msg = (
                    "⚠️ ALERTA CONGELADOR: Sin conexión con el enchufe Tapo P110 "
                    "desde hace >2h. Posible corte de luz en la línea o caída de Wi-Fi."
                )
                if send_telegram_message(telegram_token, telegram_chat_id, msg):
                    state["alert_sent"] = True
            save_state(state)
            return 0

        # Modo Test (--test flag)
        if test_mode:
            current_power, today_energy = extract_energy_metrics(dev)
            relay_state = "ON" if dev.is_on else "OFF"
            print("=== TEST DE CONEXIÓN TAPO P110 ===")
            print(f"IP Dispositivo       : {tapo_ip}")
            print(f"Estado del Relé      : {relay_state}")
            print(f"Potencia actual (W)  : {current_power} W")
            print(f"Energía acumulada hoy: {today_energy if today_energy is not None else 'N/A'}")
            test_msg = f"🧪 Test de Monitor Congelador OK - Potencia actual: {current_power} W"
            ok = send_telegram_message(telegram_token, telegram_chat_id, test_msg)
            if not ok:
                logger.error("El test de Telegram ha fallado. Revisa TELEGRAM_TOKEN y TELEGRAM_CHAT_ID.")
                return 1
            return 0

        # 2. Control del Relé del Enchufe (Auto-encendido)
        if not dev.is_on:
            logger.warning("¡El relé del enchufe Tapo P110 estaba en OFF! Forzando encendido...")
            await dev.turn_on()
            await asyncio.sleep(1)
            await dev.update()
            relay_msg = (
                "⚠️ ALERTA CONGELADOR: El enchufe inteligente Tapo P110 estaba en OFF. "
                "Se ha forzado su encendido automático para proteger el congelador."
            )
            send_telegram_message(telegram_token, telegram_chat_id, relay_msg)

        # 3. Lectura de Consumo (Detección de botón del congelador apagado)
        current_power, today_energy = extract_energy_metrics(dev)
        last_energy = state.get("last_energy", -1.0)

        # Control de reseteo a medianoche e incremento real de energía (ignorando ~0.7W de standby)
        energy_increased = False
        delta_energy = 0.0
        if today_energy is not None:
            if last_energy >= 0 and today_energy < last_energy:
                logger.info(
                    "Reseteo diario de energía detectado (anterior: %s -> actual: %s).",
                    last_energy,
                    today_energy,
                )
            elif last_energy >= 0:
                delta_energy = round(today_energy - last_energy, 4)
                if delta_energy >= energy_delta_threshold_kwh:
                    energy_increased = True

            # Actualizamos siempre el último valor conocido de energía del día
            state["last_energy"] = today_energy

        is_active = (current_power > power_threshold_w) or energy_increased

        logger.info(
            "Lectura Tapo P110 -> Relé: %s | Potencia: %.2fW | Energía hoy: %s (Δ: +%.3f) | Activo: %s",
            "ON" if dev.is_on else "OFF",
            current_power,
            today_energy,
            delta_energy,
            is_active,
        )

        if is_active:
            if state["alert_sent"]:
                recovery_msg = (
                    f"✅ CONGELADOR RECUPERADO: Se vuelve a detectar consumo eléctrico "
                    f"({current_power}W)."
                )
                send_telegram_message(telegram_token, telegram_chat_id, recovery_msg)
            state["last_active_ts"] = now
            state["alert_sent"] = False
        else:
            inactive_seconds = now - state["last_active_ts"]
            mins = int(inactive_seconds // 60)
            logger.info(
                "Sin actividad eléctrica en esta lectura. Inactivo durante %d min (%ds / %ds).",
                mins,
                int(inactive_seconds),
                max_inactive_seconds,
            )
            if inactive_seconds > max_inactive_seconds and not state["alert_sent"]:
                alert_msg = (
                    f"🚨 ALERTA CONGELADOR APAGADO: Lleva {mins} minutos sin consumir energía "
                    f"(Potencia actual: {current_power}W). Revisa si alguien ha pulsado el botón de apagado."
                )
                if send_telegram_message(telegram_token, telegram_chat_id, alert_msg):
                    state["alert_sent"] = True

        save_state(state)
        return 0

    finally:
        if dev is not None and hasattr(dev, "disconnect"):
            try:
                await dev.disconnect()
            except Exception:
                pass


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Monitor de congelador mediante enchufe inteligente TP-Link Tapo P110."
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Conecta al Tapo P110, muestra métricas por consola y envía un mensaje de prueba a Telegram.",
    )
    args = parser.parse_args()
    exit_code = asyncio.run(run_monitor(test_mode=args.test))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
