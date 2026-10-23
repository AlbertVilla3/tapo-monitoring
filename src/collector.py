"""
src/collector.py - Loop principal de muestreo, persistencia en SQLite, máquina
de estados de ciclos de calentamiento y control de duración del test para Fleck Nilo 150.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
import time
from datetime import datetime, timezone
try:
    from zoneinfo import ZoneInfo
    MADRID_TZ = ZoneInfo("Europe/Madrid")
except Exception:
    MADRID_TZ = timezone.utc

import requests

from src.config import (
    AEROTHERM_COP,
    CYCLE_END_CONSECUTIVE_SAMPLES,
    CYCLE_END_POWER_W,
    CYCLE_START_POWER_W,
    ELECTRICITY_PRICE_EUR_KWH,
    FLECK_CAPACITY_L,
    FLECK_TARGET_TEMP_C,
    MAINTENANCE_LOSS_MAX_WH,
    NIGHT_WINDOW_END_HOUR,
    NIGHT_WINDOW_START_HOUR,
    POLL_INTERVAL_SEC,
    POWER_MODE_THRESHOLD_W,
    TAPO_IP,
    TAPO_PASSWORD,
    TAPO_USERNAME,
    TELEGRAM_CHAT_ID,
    TELEGRAM_TOKEN,
    TEST_DURATION_DAYS,
    WATER_INLET_TEMP_C,
)
from src.db import (
    get_last_heating_cycle,
    get_metadata,
    init_db,
    insert_heating_cycle,
    insert_raw_sample,
    set_metadata,
)
from src.tapo_client import TapoP110Client

# Configuración del Logger
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("fleck_monitor.collector")


def send_telegram_alert(text: str) -> None:
    """Envía notificación opcional a Telegram si las credenciales están configuradas."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
    except Exception as exc:
        logger.debug("Error enviando alerta a Telegram: %s", exc)


def is_in_night_window(dt_local: datetime) -> bool:
    """Comprueba si una hora local de Madrid está entre 01:30 y 06:30."""
    hour_float = dt_local.hour + (dt_local.minute / 60.0)
    return NIGHT_WINDOW_START_HOUR <= hour_float < NIGHT_WINDOW_END_HOUR


class CycleStateMachine:
    """
    Máquina de estados para detectar ciclos de calentamiento del termo Fleck Nilo 150.
    - Inicio: potencia > 100 W (standby < 5 W).
    - Fin: potencia < 20 W durante al menos 2 lecturas consecutivas (30s) para evitar
      cortes espurios por conmutación de tanque o resistencia.
    """

    def __init__(self, last_cycle_end_utc: str | None = None) -> None:
        self.is_heating: bool = False
        self.start_utc: str = ""
        self.start_local: str = ""
        self.start_timestamp: float = 0.0
        self.consecutive_low_samples: int = 0
        self.samples: list[tuple[float, float, float]] = []  # (ts, power_w, current_a)
        self.last_cycle_end_utc: str = last_cycle_end_utc or ""

    def on_sample(
        self,
        ts: float,
        ts_utc: str,
        ts_local: str,
        power_w: float,
        current_a: float,
    ) -> dict | None:
        """
        Procesa una muestra. Si un ciclo termina, devuelve un diccionario con los
        datos calculados del ciclo para persistir.
        """
        # Estado REPOSO (IDLE)
        if not self.is_heating:
            if self.last_cycle_end_utc and ts_utc <= self.last_cycle_end_utc:
                return None
            if power_w >= CYCLE_START_POWER_W:
                self.is_heating = True
                self.start_utc = ts_utc
                self.start_local = ts_local
                self.start_timestamp = ts
                self.consecutive_low_samples = 0
                self.samples = [(ts, power_w, current_a)]
                logger.info(
                    "🔥 INICIO CICLO CALENTAMIENTO detectado a las %s | Potencia: %.1f W (%.2f A)",
                    ts_local,
                    power_w,
                    current_a,
                )
            return None

        # Estado CALENTANDO (HEATING)
        self.samples.append((ts, power_w, current_a))

        if power_w < CYCLE_END_POWER_W:
            self.consecutive_low_samples += 1
            logger.debug(
                "Potencia baja (%.1f W) durante ciclo: lectura %d/%d",
                power_w,
                self.consecutive_low_samples,
                CYCLE_END_CONSECUTIVE_SAMPLES,
            )
        else:
            self.consecutive_low_samples = 0

        # Verificar fin de ciclo por lecturas bajas consecutivas
        if self.consecutive_low_samples >= CYCLE_END_CONSECUTIVE_SAMPLES:
            completed_cycle = self._finalize_cycle(end_utc=ts_utc, end_local=ts_local)
            self._reset()
            return completed_cycle

        return None

    def _finalize_cycle(self, end_utc: str, end_local: str) -> dict:
        """Calcula las métricas finales y clasificación del ciclo."""
        # Filtrar o calcular duración
        if not self.samples:
            return {}

        start_ts = self.samples[0][0]
        end_ts = self.samples[-1][0]
        duration_sec = int(round(max(end_ts - start_ts, 1.0)))

        # Integración numérica trapezoidal/rectangular de energía (Wh)
        total_energy_wh = 0.0
        for i in range(len(self.samples) - 1):
            t1, p1, _ = self.samples[i]
            t2, p2, _ = self.samples[i + 1]
            dt_hours = (t2 - t1) / 3600.0
            avg_p = (p1 + p2) / 2.0
            total_energy_wh += avg_p * dt_hours

        # Si el ciclo duró solo 1 muestra (caso raro)
        if len(self.samples) == 1:
            total_energy_wh = self.samples[0][1] * (POLL_INTERVAL_SEC / 3600.0)

        powers = [s[1] for s in self.samples]
        max_power_w = max(powers) if powers else 0.0

        # Para la potencia media y modo, excluir las muestras finales de corte por debajo de 20W
        active_samples = [s[1] for s in self.samples if s[1] >= CYCLE_END_POWER_W]
        if not active_samples:
            active_samples = powers
        avg_power_w = sum(active_samples) / len(active_samples)

        # Determinar modo de potencia
        # < 1600W -> HALF_1250W (una resistencia activa)
        # >= 1600W -> FULL_2500W (ambas resistencias activas)
        power_mode = "HALF_1250W" if avg_power_w < POWER_MODE_THRESHOLD_W else "FULL_2500W"

        # Clasificación heurística
        # MAINTENANCE_LOSS si < 350 Wh O si ocurre en ventana nocturna 01:30-06:30
        try:
            dt_start = datetime.fromisoformat(self.start_local.replace("Z", "+00:00"))
            in_night = is_in_night_window(dt_start)
        except Exception:
            in_night = False

        if total_energy_wh < MAINTENANCE_LOSS_MAX_WH or (in_night and total_energy_wh < 600.0):
            cycle_type = "MAINTENANCE_LOSS"
        else:
            cycle_type = "WATER_DRAW_RECOVERY"

        return {
            "start_time": self.start_utc,
            "end_time": end_utc,
            "duration_sec": duration_sec,
            "energy_wh": round(total_energy_wh, 2),
            "avg_power_w": round(avg_power_w, 2),
            "max_power_w": round(max_power_w, 2),
            "power_mode": power_mode,
            "cycle_type": cycle_type,
            "start_local": self.start_local,
            "end_local": end_local,
        }

    def _reset(self) -> None:
        self.is_heating = False
        self.start_utc = ""
        self.start_local = ""
        self.start_timestamp = 0.0
        self.consecutive_low_samples = 0
        self.samples = []

    def force_finish_on_shutdown(self) -> dict | None:
        """Cierra de forma segura un ciclo que estuviese en curso al recibir SIGINT/SIGTERM."""
        if not self.is_heating or not self.samples:
            return None
        now_dt_utc = datetime.now(timezone.utc)
        now_dt_local = datetime.now(MADRID_TZ)
        cycle = self._finalize_cycle(
            end_utc=now_dt_utc.isoformat(),
            end_local=now_dt_local.isoformat(),
        )
        self._reset()
        return cycle


class FleckMonitorCollector:
    """Loop principal de recolección de métricas del termo Fleck Nilo."""

    def __init__(self) -> None:
        self.tapo_client = TapoP110Client(
            ip=TAPO_IP,
            username=TAPO_USERNAME,
            password=TAPO_PASSWORD,
        )
        self.state_machine = CycleStateMachine()
        self._stop_event = asyncio.Event()

    def stop(self) -> None:
        """Señaliza la parada limpia del loop."""
        self._stop_event.set()

    async def run(self) -> None:
        """Ejecuta el loop periódico de adquisición."""
        init_db()

        # Recuperar el último ciclo registrado para evitar arranques solapados en reinicios
        last_cycle = get_last_heating_cycle()
        if last_cycle:
            self.state_machine.last_cycle_end_utc = last_cycle["end_time"]
            logger.info("Último ciclo registrado concluido en: %s", last_cycle["end_time"])

        # Registrar o recuperar metadatos del test
        test_start_str = get_metadata("test_start_utc")
        now_utc = datetime.now(timezone.utc)
        now_local = datetime.now(MADRID_TZ)

        if not test_start_str:
            set_metadata("test_start_utc", now_utc.isoformat())
            set_metadata("test_start_local", now_local.isoformat())
            set_metadata("test_duration_days", str(TEST_DURATION_DAYS))
            set_metadata("status", "RUNNING")
            logger.info("🚀 Iniciando NUEVO test de monitorización para Fleck Nilo 150.")
            send_telegram_alert(
                f"🚀 [Fleck Nilo 150] Test iniciado.\n"
                f"Duración prevista: {TEST_DURATION_DAYS} días.\n"
                f"Muestreo cada {POLL_INTERVAL_SEC}s. Enchufe Tapo: {TAPO_IP}"
            )
            test_start_dt = now_utc
        else:
            test_start_dt = datetime.fromisoformat(test_start_str.replace("Z", "+00:00"))
            logger.info(
                "Reanudando test de Fleck Nilo 150 iniciado el %s (UTC).",
                test_start_dt.strftime("%Y-%m-%d %H:%M:%S"),
            )

        test_duration_seconds = TEST_DURATION_DAYS * 86400.0
        test_completed_notified = False

        logger.info("=== Monitor Fleck Nilo 150 Activo ===")
        logger.info("IP Tapo P110: %s | Intervalo: %ds", TAPO_IP, POLL_INTERVAL_SEC)
        logger.info("Base de datos: data/fleck_monitor_test.db")

        while not self._stop_event.is_set():
            loop_start = time.time()
            now_dt_utc = datetime.now(timezone.utc)
            now_dt_local = datetime.now(MADRID_TZ)
            ts_utc_str = now_dt_utc.isoformat()
            ts_local_str = now_dt_local.isoformat()

            # 1. Comprobar si se ha cumplido la duración del test
            elapsed_sec = (now_dt_utc - test_start_dt).total_seconds()
            if elapsed_sec >= test_duration_seconds and not test_completed_notified:
                test_completed_notified = True
                set_metadata("status", "COMPLETED")
                msg = (
                    f"🏁 [Fleck Nilo 150] ¡Periodo de prueba completado ({TEST_DURATION_DAYS} días)!\n"
                    f"El colector sigue registrando datos de fondo. "
                    f"Ejecuta en tu terminal:\n`python -m src.report` para ver el informe final."
                )
                logger.info(msg)
                send_telegram_alert(msg)

            # 2. Adquirir muestra del Tapo P110
            try:
                sample = await self.tapo_client.get_sample()
                power_w = sample["power_w"]
                current_a = sample["current_a"]
                today_energy_wh = sample["today_energy_wh"]

                # 3. Guardar muestra instantánea en raw_samples
                insert_raw_sample(
                    timestamp_utc=ts_utc_str,
                    timestamp_local=ts_local_str,
                    power_w=power_w,
                    current_a=current_a,
                    today_energy_wh=today_energy_wh,
                )

                # 4. Alimentar máquina de estados de ciclos
                cycle_data = self.state_machine.on_sample(
                    ts=loop_start,
                    ts_utc=ts_utc_str,
                    ts_local=ts_local_str,
                    power_w=power_w,
                    current_a=current_a,
                )

                if cycle_data:
                    # Guardar ciclo completado en BD
                    insert_heating_cycle(
                        start_time=cycle_data["start_time"],
                        end_time=cycle_data["end_time"],
                        duration_sec=cycle_data["duration_sec"],
                        energy_wh=cycle_data["energy_wh"],
                        avg_power_w=cycle_data["avg_power_w"],
                        max_power_w=cycle_data["max_power_w"],
                        power_mode=cycle_data["power_mode"],
                        cycle_type=cycle_data["cycle_type"],
                    )

                    dur_min = round(cycle_data["duration_sec"] / 60.0, 1)
                    kwh = cycle_data["energy_wh"] / 1000.0
                    cost = kwh * ELECTRICITY_PRICE_EUR_KWH
                    c_type_label = (
                        "📉 Pérdida Estática (Mantenimiento)"
                        if cycle_data["cycle_type"] == "MAINTENANCE_LOSS"
                        else "🚿 Demanda / Ducha (Recuperación)"
                    )

                    logger.info(
                        "✅ CICLO COMPLETADO: %s | Modo: %s | Duración: %s min | "
                        "Energía: %.3f kWh (%.2f €) | Pot. Media: %.1f W | Tipo: %s",
                        cycle_data["end_local"],
                        cycle_data["power_mode"],
                        dur_min,
                        kwh,
                        cost,
                        cycle_data["avg_power_w"],
                        c_type_label,
                    )

            except Exception as exc:
                logger.error("Error en ciclo de recolección: %s", exc)

            # Esperar hasta el siguiente intervalo de muestreo
            elapsed = time.time() - loop_start
            sleep_time = max(0.0, POLL_INTERVAL_SEC - elapsed)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=sleep_time)
            except asyncio.TimeoutError:
                pass

        # Limpieza al detener
        logger.info("Deteniendo colector de telemetría...")
        in_progress_cycle = self.state_machine.force_finish_on_shutdown()
        if in_progress_cycle:
            logger.info("Guardando ciclo en curso antes de apagar...")
            insert_heating_cycle(
                start_time=in_progress_cycle["start_time"],
                end_time=in_progress_cycle["end_time"],
                duration_sec=in_progress_cycle["duration_sec"],
                energy_wh=in_progress_cycle["energy_wh"],
                avg_power_w=in_progress_cycle["avg_power_w"],
                max_power_w=in_progress_cycle["max_power_w"],
                power_mode=in_progress_cycle["power_mode"],
                cycle_type=in_progress_cycle["cycle_type"],
            )

        await self.tapo_client.disconnect()
        logger.info("Colector detenido limpiamente.")


async def main_async() -> None:
    collector = FleckMonitorCollector()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, collector.stop)
        except NotImplementedError:
            # En Windows add_signal_handler no siempre está implementado en el selector loop
            pass

    try:
        await collector.run()
    except (KeyboardInterrupt, SystemExit):
        collector.stop()


def main() -> None:
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        logger.info("Interrupción por teclado (Ctrl+C). Saliendo.")


if __name__ == "__main__":
    main()
