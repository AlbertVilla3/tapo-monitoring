#!/usr/bin/env python3
"""
Monitor de Congelador para TP-Link Tapo P110.
Diseñado para ejecutarse periódicamente vía cron (ej. cada 15 minutos) y responder
a comandos en tiempo real en Telegram (/estado, /status, /consumo, /test, /ayuda).
"""

import argparse
import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from dotenv import load_dotenv
from kasa import Discover

from tapo_monitor import (
    calculate_consumption_summary,
    format_status_command_output,
    get_freezer_status,
)

# Configuración de rutas y logs
BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "state.json"
OFFSET_FILE = BASE_DIR / "telegram_offset.json"
PID_FILE = BASE_DIR / "bot_listener.pid"
CRON_LOG_FILE = BASE_DIR / "cron.log"
ENV_FILE = BASE_DIR / ".env"

MAX_RECENT_READINGS = 16  # 16 lecturas de 15 min = últimas 4 horas
MAX_HOURLY_BUCKETS = 48   # Últimas 48 horas para cubrir ventana de 24h
MAX_DAILY_BUCKETS = 21    # Últimos 21 días para cubrir ventanas de 7d y 14d

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("freezer_monitor")

LOG_LINE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}) (\d{2}):\d{2}:\d{2} \[INFO\] Lectura Tapo P110 -> "
    r"Relé: (ON|OFF) \| Potencia: ([\d.]+)W \| Energía hoy: ([\d.]+)"
)


def seed_history_from_cron_log() -> Tuple[List[float], Dict[str, float], Dict[str, float]]:
    """
    Si state.json aún no tiene histórico de lecturas/horas/días, reconstruye el
    histórico real a partir de cron.log para que /estado y /consumo muestren datos
    reales desde el primer momento.
    """
    recent_readings: List[float] = []
    hourly_buckets: Dict[str, float] = {}
    daily_buckets: Dict[str, float] = {}

    if not CRON_LOG_FILE.exists():
        return recent_readings, hourly_buckets, daily_buckets

    try:
        prev_day: Optional[str] = None
        prev_energy: Optional[float] = None
        with CRON_LOG_FILE.open("r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                m = LOG_LINE_RE.match(line.strip())
                if not m:
                    continue
                day_str, hour_str, _relay, power_str, energy_str = m.groups()
                power_w = float(power_str)
                energy_kwh = float(energy_str)
                hour_key = f"{day_str} {hour_str}"

                recent_readings.append(power_w)
                daily_buckets[day_str] = energy_kwh

                if prev_day == day_str and prev_energy is not None and energy_kwh >= prev_energy:
                    delta = round(energy_kwh - prev_energy, 4)
                else:
                    delta = 0.0
                hourly_buckets[hour_key] = round(hourly_buckets.get(hour_key, 0.0) + delta, 4)
                prev_day = day_str
                prev_energy = energy_kwh
    except Exception as exc:
        logger.warning("No se pudo reconstruir histórico desde cron.log: %s", exc)

    return (
        recent_readings[-MAX_RECENT_READINGS:],
        dict(list(hourly_buckets.items())[-MAX_HOURLY_BUCKETS:]),
        dict(list(daily_buckets.items())[-MAX_DAILY_BUCKETS:]),
    )


def load_state(now: float) -> Dict[str, Any]:
    """Carga el estado persistente desde state.json o inicializa uno por defecto."""
    default_state: Dict[str, Any] = {
        "last_active_ts": now,
        "last_energy": -1.0,
        "alert_sent": False,
        "recent_readings": [],
        "hourly_buckets": {},
        "daily_buckets": {},
    }
    if not STATE_FILE.exists():
        seeded_readings, seeded_hourly, seeded_daily = seed_history_from_cron_log()
        default_state["recent_readings"] = seeded_readings
        default_state["hourly_buckets"] = seeded_hourly
        default_state["daily_buckets"] = seeded_daily
        return default_state

    try:
        with STATE_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
            recent_readings = data.get("recent_readings")
            hourly_buckets = data.get("hourly_buckets")
            daily_buckets = data.get("daily_buckets")

            if not recent_readings and not hourly_buckets and not daily_buckets:
                recent_readings, hourly_buckets, daily_buckets = seed_history_from_cron_log()

            return {
                "last_active_ts": float(data.get("last_active_ts", now)),
                "last_energy": float(data.get("last_energy", -1.0)),
                "alert_sent": bool(data.get("alert_sent", False)),
                "recent_readings": list(recent_readings or [])[-MAX_RECENT_READINGS:],
                "hourly_buckets": dict(list((hourly_buckets or {}).items())[-MAX_HOURLY_BUCKETS:]),
                "daily_buckets": dict(list((daily_buckets or {}).items())[-MAX_DAILY_BUCKETS:]),
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


def update_history_in_state(
    state: Dict[str, Any],
    now: float,
    current_power: float,
    today_energy: Optional[float],
    delta_energy: float,
) -> None:
    """Actualiza las series históricas de potencia, consumo horario y consumo diario."""
    dt = datetime.fromtimestamp(now)
    day_key = dt.strftime("%Y-%m-%d")
    hour_key = dt.strftime("%Y-%m-%d %H")

    readings = list(state.get("recent_readings", []))
    readings.append(round(current_power, 2))
    state["recent_readings"] = readings[-MAX_RECENT_READINGS:]

    hourly_buckets: Dict[str, float] = dict(state.get("hourly_buckets", {}))
    if delta_energy >= 0:
        hourly_buckets[hour_key] = round(hourly_buckets.get(hour_key, 0.0) + delta_energy, 4)
    state["hourly_buckets"] = dict(list(hourly_buckets.items())[-MAX_HOURLY_BUCKETS:])

    if today_energy is not None:
        daily_buckets: Dict[str, float] = dict(state.get("daily_buckets", {}))
        daily_buckets[day_key] = round(today_energy, 4)
        state["daily_buckets"] = dict(list(daily_buckets.items())[-MAX_DAILY_BUCKETS:])


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
        logger.info("Mensaje enviado a Telegram: %s", text.splitlines()[0] if text else "")
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
        for attr in ("current_consumption", "power"):
            val = getattr(energy_mod, attr, None)
            if val is not None:
                try:
                    current_power = float(val)
                    break
                except (TypeError, ValueError):
                    pass

        for attr in ("consumption_today", "today_consumption", "today_energy"):
            val = getattr(energy_mod, attr, None)
            if val is not None:
                try:
                    today_energy = float(val)
                    break
                except (TypeError, ValueError):
                    pass

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


async def build_live_status_report(
    tapo_ip: str,
    tapo_user: str,
    tapo_pass: str,
    max_inactive_seconds: int,
) -> str:
    """
    Conecta en tiempo real con el Tapo P110 y genera el informe completo combinando
    TASK-007 (get_freezer_status) y TASK-008 (calculate_consumption_summary + format_status_command_output).
    """
    now = time.time()
    state = load_state(now)
    dev = None
    try:
        dev = await connect_tapo(tapo_ip, tapo_user, tapo_pass, retries=1)
        current_power, today_energy = extract_energy_metrics(dev)
        is_on = bool(dev.is_on)
    except Exception as exc:
        inactive_mins = int((now - state["last_active_ts"]) // 60)
        return (
            "⚠️ No se pudo conectar en este momento con el enchufe Tapo P110.\n"
            f"Error: {exc}\n"
            f"Última actividad registrada: hace {inactive_mins} min."
        )
    finally:
        if dev is not None and hasattr(dev, "disconnect"):
            try:
                await dev.disconnect()
            except Exception:
                pass

    recent_readings = list(state.get("recent_readings", []))
    recent_with_live = (recent_readings + [current_power])[-MAX_RECENT_READINGS:]

    hourly_buckets: Dict[str, float] = dict(state.get("hourly_buckets", {}))
    daily_buckets: Dict[str, float] = dict(state.get("daily_buckets", {}))
    if today_energy is not None:
        day_key = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        daily_buckets[day_key] = round(today_energy, 4)

    hourly_kwh = list(hourly_buckets.values())
    daily_kwh = list(daily_buckets.values())

    status_info = get_freezer_status(
        {"is_on": is_on, "current_power_w": current_power},
        recent_with_live,
    )
    consumption_summary = calculate_consumption_summary(hourly_kwh, daily_kwh)
    report = format_status_command_output(status_info, consumption_summary)

    inactive_mins = 0 if current_power >= 5.0 else int((now - state["last_active_ts"]) // 60)
    max_mins = int(max_inactive_seconds // 60)
    extra_lines = [
        "--- Diagnóstico Operativo ---",
        f"Consumo hoy (Tapo)  : {today_energy if today_energy is not None else 'N/A'} kWh",
        f"Tiempo sin motor    : {inactive_mins} min (Umbral alerta: {max_mins} min)",
    ]
    return report + "\n" + "\n".join(extra_lines)


def ensure_listener_running() -> None:
    """Asegura que el proceso ligero de escucha de comandos de Telegram esté activo en segundo plano."""
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text(encoding="utf-8").strip())
            os.kill(pid, 0)
            return  # Ya está en ejecución
        except (ValueError, OSError):
            pass

    try:
        log_file = (BASE_DIR / "bot_listener.log").open("a", encoding="utf-8")
        proc = subprocess.Popen(
            [sys.executable, str( Path(__file__).resolve() ), "--listen"],
            cwd=str(BASE_DIR),
            stdout=log_file,
            stderr=log_file,
            start_new_session=True,
        )
        PID_FILE.write_text(str(proc.pid), encoding="utf-8")
        logger.info("Listener de comandos de Telegram iniciado en segundo plano (PID %d).", proc.pid)
    except Exception as exc:
        logger.warning("No se pudo iniciar el listener en segundo plano: %s", exc)


def load_last_update_id() -> int:
    """Lee el último update_id procesado de Telegram."""
    if not OFFSET_FILE.exists():
        return 0
    try:
        data = json.loads(OFFSET_FILE.read_text(encoding="utf-8"))
        return int(data.get("last_update_id", 0))
    except Exception:
        return 0


def save_last_update_id(update_id: int) -> None:
    """Guarda el último update_id procesado de Telegram."""
    try:
        OFFSET_FILE.write_text(json.dumps({"last_update_id": update_id}), encoding="utf-8")
    except Exception as exc:
        logger.warning("No se pudo guardar offset de Telegram: %s", exc)


async def handle_telegram_command(
    command: str,
    chat_id: str,
    telegram_token: str,
    tapo_ip: str,
    tapo_user: str,
    tapo_pass: str,
    max_inactive_seconds: int,
) -> None:
    """Procesa un comando recibido desde el chat de Telegram."""
    cmd = command.split()[0].split("@")[0].lower()

    if cmd in ("/estado", "/status", "/consumo", "/resumen"):
        report = await build_live_status_report(
            tapo_ip, tapo_user, tapo_pass, max_inactive_seconds
        )
        send_telegram_message(telegram_token, chat_id, report)

    elif cmd == "/test":
        dev = None
        try:
            dev = await connect_tapo(tapo_ip, tapo_user, tapo_pass, retries=1)
            current_power, today_energy = extract_energy_metrics(dev)
            relay_state = "ON" if dev.is_on else "OFF"
            msg = (
                f"🧪 Test de Monitor Congelador OK\n"
                f"Relé: {relay_state} | Potencia actual: {current_power} W | "
                f"Energía hoy: {today_energy if today_energy is not None else 'N/A'} kWh"
            )
            send_telegram_message(telegram_token, chat_id, msg)
        except Exception as exc:
            send_telegram_message(telegram_token, chat_id, f"❌ Error en test de conexión: {exc}")
        finally:
            if dev is not None and hasattr(dev, "disconnect"):
                try:
                    await dev.disconnect()
                except Exception:
                    pass

    elif cmd in ("/ayuda", "/help", "/start"):
        help_msg = (
            "❄️ Comandos disponibles del Monitor de Congelador:\n"
            "/estado o /status - Estado instantáneo, salud y resumen de consumo\n"
            "/consumo o /resumen - Resumen de consumo (24h, 7 días y 14 días)\n"
            "/test - Prueba en vivo de conexión con el enchufe Tapo P110\n"
            "/ayuda - Muestra esta lista de comandos"
        )
        send_telegram_message(telegram_token, chat_id, help_msg)


async def run_telegram_listener() -> int:
    """
    Bucle ligero de long-polling para responder al instante en Telegram a
    /estado, /status, /consumo, /resumen, /test y /ayuda.
    """
    load_dotenv(dotenv_path=ENV_FILE)
    telegram_token = os.getenv("TELEGRAM_TOKEN", "").strip()
    allowed_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    if not telegram_token or not allowed_chat_id:
        logger.error("Faltan TELEGRAM_TOKEN o TELEGRAM_CHAT_ID para iniciar listener.")
        return 1

    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    logger.info("Iniciando escucha de comandos de Telegram para chat_id=%s...", allowed_chat_id)

    # Registrar comandos en el menú de Telegram para que aparezcan al escribir '/'
    try:
        requests.post(
            f"https://api.telegram.org/bot{telegram_token}/setMyCommands",
            json={
                "commands": [
                    {"command": "estado", "description": "Estado actual y diagnóstico de salud del congelador"},
                    {"command": "consumo", "description": "Resumen de consumo de las últimas 24h, 7 y 14 días"},
                    {"command": "test", "description": "Test de conexión en tiempo real con el Tapo P110"},
                    {"command": "ayuda", "description": "Ver comandos disponibles"},
                ]
            },
            timeout=10,
        )
    except Exception:
        pass

    last_update_id = load_last_update_id()
    url = f"https://api.telegram.org/bot{telegram_token}/getUpdates"

    while True:
        try:
            resp = await asyncio.to_thread(
                requests.get,
                url,
                params={"offset": last_update_id + 1, "timeout": 25, "allowed_updates": ["message"]},
                timeout=35,
            )
            if resp.status_code == 409:
                logger.warning(
                    "Conflicto 409 en getUpdates (otro bot usa este mismo token). Cerrando listener."
                )
                return 0

            resp.raise_for_status()
            payload = resp.json()

            for item in payload.get("result", []):
                update_id = int(item.get("update_id", 0))
                if update_id > last_update_id:
                    last_update_id = update_id
                    save_last_update_id(last_update_id)

                msg = item.get("message") or {}
                chat = msg.get("chat") or {}
                chat_id = str(chat.get("id", ""))
                text = (msg.get("text") or "").strip()

                if not text.startswith("/") or chat_id != allowed_chat_id:
                    continue

                load_dotenv(dotenv_path=ENV_FILE, override=True)
                tapo_ip = os.getenv("TAPO_IP", "").strip()
                tapo_user = os.getenv("TAPO_USER", "").strip()
                tapo_pass = os.getenv("TAPO_PASS", "").strip()
                try:
                    max_inactive_seconds = int(os.getenv("MAX_INACTIVE_SECONDS", "4500"))
                except ValueError:
                    max_inactive_seconds = 4500

                logger.info("Comando recibido en Telegram: %s", text)
                await handle_telegram_command(
                    text,
                    chat_id,
                    telegram_token,
                    tapo_ip,
                    tapo_user,
                    tapo_pass,
                    max_inactive_seconds,
                )

        except Exception as exc:
            logger.warning("Error en ciclo de escucha de Telegram: %s", exc)
            await asyncio.sleep(5)


async def run_monitor(test_mode: bool = False, status_mode: bool = False) -> int:
    """Ejecuta el flujo principal de monitorización, el modo estado (--status) o el modo test (--test)."""
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
        max_inactive_seconds = int(os.getenv("MAX_INACTIVE_SECONDS", "4500"))
    except ValueError:
        max_inactive_seconds = 4500

    if not tapo_ip or not tapo_user or not tapo_pass:
        logger.error("Faltan variables obligatorias en .env (TAPO_IP, TAPO_USER, TAPO_PASS).")
        return 1

    # Asegurar que el listener de comandos de Telegram esté corriendo en background
    if os.name != "nt":
        ensure_listener_running()

    if status_mode:
        report = await build_live_status_report(
            tapo_ip, tapo_user, tapo_pass, max_inactive_seconds
        )
        print(report)
        send_telegram_message(telegram_token, telegram_chat_id, report)
        return 0

    now = time.time()
    state = load_state(now)

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

            state["last_energy"] = today_energy

        # Actualizar histórico de lecturas (24h, 7d, 14d) para TASK-007 y TASK-008
        update_history_in_state(state, now, current_power, today_energy, delta_energy)

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
    parser.add_argument(
        "--status",
        action="store_true",
        help="Muestra por consola y envía a Telegram el informe completo de estado y consumo (24h, 7d, 14d).",
    )
    parser.add_argument(
        "--listen",
        action="store_true",
        help="Inicia el listener ligero para responder al instante a comandos en Telegram (/estado, /consumo).",
    )
    args = parser.parse_args()
    if args.listen:
        exit_code = asyncio.run(run_telegram_listener())
    else:
        exit_code = asyncio.run(run_monitor(test_mode=args.test, status_mode=args.status))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
