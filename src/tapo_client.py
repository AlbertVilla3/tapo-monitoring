"""
src/tapo_client.py - Cliente asíncrono robusto para TP-Link Tapo P110 con reconexión
automática, exponential backoff y extracción de métricas de potencia y energía.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional, Tuple
from kasa import Discover

logger = logging.getLogger("fleck_monitor.tapo")


class TapoP110Client:
    """
    Cliente asíncrono para interactuar con el enchufe inteligente Tapo P110.
    Mantiene la sesión y reconecta automáticamente ante microcortes con backoff exponencial.
    """

    def __init__(
        self,
        ip: str,
        username: str,
        password: str,
        initial_backoff_sec: float = 2.0,
        max_backoff_sec: float = 30.0,
    ) -> None:
        self.ip: str = ip
        self.username: str = username
        self.password: str = password
        self.initial_backoff_sec: float = initial_backoff_sec
        self.max_backoff_sec: float = max_backoff_sec

        self._current_backoff: float = initial_backoff_sec
        self._dev: Any = None
        self._is_connected: bool = False

    async def connect(self) -> Any:
        """
        Intenta conectar con el Tapo P110 usando credenciales TP-Link (protocolo KLAP).
        """
        if not self.ip or not self.username or not self.password:
            raise ValueError(
                "Faltan credenciales del enchufe Tapo (TAPO_IP, TAPO_USERNAME o TAPO_PASSWORD)."
            )

        logger.debug("Conectando con Tapo P110 en %s...", self.ip)
        dev = await Discover.discover_single(
            self.ip,
            username=self.username,
            password=self.password,
            timeout=10,
        )
        await dev.update()
        self._dev = dev
        self._is_connected = True
        self._current_backoff = self.initial_backoff_sec
        logger.info("Conexión establecida con Tapo P110 (%s). Relé: %s", self.ip, "ON" if dev.is_on else "OFF")
        return dev

    async def disconnect(self) -> None:
        """Cierra la conexión y libera los recursos del cliente."""
        if self._dev is not None and hasattr(self._dev, "disconnect"):
            try:
                await self._dev.disconnect()
            except Exception as exc:
                logger.debug("Aviso al desconectar Tapo: %s", exc)
        self._dev = None
        self._is_connected = False

    def _extract_metrics(self, dev: Any) -> Tuple[float, float, int, bool]:
        """
        Extrae (power_w, current_a, today_energy_wh, is_on) soportando diferentes
        versiones del módulo Energy de python-kasa.
        """
        is_on = bool(getattr(dev, "is_on", True))
        power_w: Optional[float] = None
        today_energy_wh: Optional[int] = None
        current_a: Optional[float] = None

        # 1. Intentar a través de dev.modules
        energy_mod = None
        if hasattr(dev, "modules") and isinstance(dev.modules, dict):
            energy_mod = dev.modules.get("Energy") or dev.modules.get("energy")
            if energy_mod is None:
                for k, mod in dev.modules.items():
                    if str(k).lower().endswith("energy"):
                        energy_mod = mod
                        break

        if energy_mod is not None:
            # Potencia en W
            for attr in ("current_consumption", "power"):
                val = getattr(energy_mod, attr, None)
                if val is not None:
                    try:
                        power_w = float(val)
                        break
                    except (TypeError, ValueError):
                        pass

            # Energía de hoy en kWh -> convertir a Wh
            for attr in ("consumption_today", "today_consumption", "today_energy"):
                val = getattr(energy_mod, attr, None)
                if val is not None:
                    try:
                        today_energy_wh = int(round(float(val) * 1000.0))
                        break
                    except (TypeError, ValueError):
                        pass

            # Intensidad en Amperios
            for attr in ("current", "current_a", "amps"):
                val = getattr(energy_mod, attr, None)
                if val is not None:
                    try:
                        current_a = float(val)
                        break
                    except (TypeError, ValueError):
                        pass

        # 2. Fallbacks de compatibilidad vía emeter_realtime / emeter_today
        if power_w is None and hasattr(dev, "emeter_realtime"):
            try:
                rt = dev.emeter_realtime or {}
                if "power" in rt and rt["power"] is not None:
                    power_w = float(rt["power"])
                elif "power_mw" in rt and rt["power_mw"] is not None:
                    power_w = float(rt["power_mw"]) / 1000.0

                if current_a is None:
                    if "current" in rt and rt["current"] is not None:
                        current_a = float(rt["current"])
                    elif "current_ma" in rt and rt["current_ma"] is not None:
                        current_a = float(rt["current_ma"]) / 1000.0
            except Exception:
                pass

        if today_energy_wh is None and hasattr(dev, "emeter_today"):
            try:
                if dev.emeter_today is not None:
                    today_energy_wh = int(round(float(dev.emeter_today) * 1000.0))
            except Exception:
                pass

        # Valores seguros por defecto
        final_power_w = round(power_w if power_w is not None else 0.0, 2)
        final_energy_wh = today_energy_wh if today_energy_wh is not None else 0

        # Si el enchufe no reporta amperios directamente, estimar I = P / 230V
        if current_a is None or current_a < 0.001:
            final_current_a = round(final_power_w / 230.0, 2)
        else:
            final_current_a = round(current_a, 2)

        return final_power_w, final_current_a, final_energy_wh, is_on

    async def get_sample(self) -> Dict[str, Any]:
        """
        Obtiene una muestra de telemetría instantánea con reconexión automática
        y backoff exponencial en caso de fallo de red.
        """
        while True:
            try:
                if self._dev is None or not self._is_connected:
                    await self.connect()

                # Actualizar estado del dispositivo
                await self._dev.update()
                power_w, current_a, today_energy_wh, is_on = self._extract_metrics(self._dev)

                # Reset de backoff tras lectura exitosa
                self._current_backoff = self.initial_backoff_sec

                return {
                    "power_w": power_w,
                    "current_a": current_a,
                    "today_energy_wh": today_energy_wh,
                    "is_on": is_on,
                }

            except Exception as exc:
                logger.warning(
                    "Error al muestrear Tapo P110 (%s): %s. Reintentando en %.1fs...",
                    self.ip,
                    exc,
                    self._current_backoff,
                )
                await self.disconnect()
                await asyncio.sleep(self._current_backoff)
                self._current_backoff = min(self._current_backoff * 2.0, self.max_backoff_sec)
