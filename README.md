# Monitor de Congelador (TP-Link Tapo P110 + Telegram)

Sistema ligero en Python pensado para ejecutarse cada 15 minutos mediante `cron` en una **Raspberry Pi**. Monitoriza un congelador conectado a un enchufe inteligente **TP-Link Tapo P110** y envía alertas a **Telegram** si:
- Se pierde la conexión o hay un corte eléctrico prolongado (`> 2 horas`).
- Alguien apaga el relé del enchufe inteligente (lo vuelve a encender automáticamente).
- El congelador lleva más de 2 horas sin consumir energía (`0W`, p. ej. botón físico apagado o avería del compresor).

---

## Despliegue en la Raspberry Pi desde GitHub

1. **Clonar el repositorio en `~/monitor_congelador`:**
   ```bash
   git clone https://github.com/AlbertVilla3/tapo-monitoring.git ~/monitor_congelador
   cd ~/monitor_congelador
   ```

2. **Ejecutar el instalador (`setup.sh`):**
   ```bash
   chmod +x setup.sh
   ./setup.sh
   ```
   Esto creará el entorno virtual `venv`, instalará las dependencias, generará el archivo `.env` a partir de `.env.example` y registrará la tarea en `crontab` cada 15 minutos sin duplicarla.

3. **Configurar las credenciales en `.env`:**
   ```bash
   nano .env
   ```
   Rellena los datos de tu enchufe y de tu bot de Telegram:
   ```ini
   TAPO_IP=192.168.1.XXX
   TAPO_USER=tu_email@dominio.com
   TAPO_PASS=tu_password_tplink
   TELEGRAM_TOKEN=123456789:ABCDEF_tu_token_de_botfather
   TELEGRAM_CHAT_ID=123456789
   POWER_THRESHOLD_W=5.0
   MAX_INACTIVE_SECONDS=7200
   ```

4. **Probar la conexión y el envío a Telegram:**
   ```bash
   ./venv/bin/python check_freezer.py --test
   ```
   O vuelve a ejecutar `./setup.sh` para validar y lanzar el test automáticamente.

---

## ⚡ Monitorización Temporal del Termo Eléctrico (Fleck Nilo 150 a 60°C)

Este módulo permite monitorizar temporalmente (2 a 3 días) el consumo de un termo eléctrico **Fleck Nilo 150** (potencia máx. 2.500 W con resistencias independientes 1.250 W + 1.250 W) reutilizando el enchufe **Tapo P110** sin mezclar ni corromper los históricos del congelador.

Toda la información se aísla en SQLite (`data/fleck_monitor_test.db` con modo WAL) en dos tablas:
- `raw_samples`: Muestreo de potencia cada 15 segundos (`power_w`, `current_a`, `today_energy_wh`).
- `heating_cycles`: Ciclos completos con inicio, fin, energía integrada (`Wh`), potencia media, modo (`HALF_1250W` vs `FULL_2500W`) y tipo de ciclo (`MAINTENANCE_LOSS` vs `WATER_DRAW_RECOVERY`).

### Ejecución en segundo plano en Raspberry Pi

#### Opción A: Con `tmux` (rápido y directo)
```bash
tmux new -s termo
./venv/bin/python -m src.collector
# Para desacoplar la sesión: Ctrl+B y luego D
# Para volver a entrar: tmux attach -t termo
```

#### Opción B: Con `systemd` (servicio persistente a reinicios)
```bash
sudo cp systemd/fleck-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now fleck-monitor.service

# Ver logs en vivo:
journalctl -u fleck-monitor.service -f
```

### Generación del Informe y Simulación Aerotermia (COP 3.0)

En cualquier momento durante o al finalizar los 3 días de prueba:

```bash
# Ver informe ejecutivo en consola
./venv/bin/python -m src.report

# Enviar informe formateado a Telegram
./venv/bin/python -m src.report --telegram

# Exportar métricas completas en JSON
./venv/bin/python -m src.report --json
```

El informe desglosa:
1. **Pérdidas estáticas de mantenimiento** (calentar agua que se enfría en el depósito sin usarse) vs **Demanda real de duchas**.
2. **Arranques del termo**, duración media y horas a 1.250 W vs 2.500 W.
3. **Litros de agua caliente a 60°C** consumidos estimados por termodinámica ($\Delta T = 45^\circ\text{C}$).
4. **Simulación comparativa con Aerotermo (COP 3.0)**: kWh ahorrados, reducción porcentual del 66.7% y cálculo del ahorro anual para la **deducción fiscal en IRPF** (supera el requisito legal de $\ge 30\%$ de reducción de energía primaria no renovable según RDL 19/2021).
