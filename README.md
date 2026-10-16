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
