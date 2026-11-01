#!/usr/bin/env bash
# scripts/switch_to_freezer.sh
# 1. Cierra limpiamente el monitor temporal del termo Fleck Nilo 150
# 2. Genera el paquete histórico en frío (HTML offline, Markdown, CSVs, JSON y copia SQLite)
# 3. Prepara state.json, restaura crontab y levanta el listener de Telegram para el congelador
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

if [ -x "$PROJECT_DIR/venv/bin/python" ]; then
    VENV_PYTHON="$PROJECT_DIR/venv/bin/python"
else
    VENV_PYTHON="python3"
fi

echo "=========================================================================="
echo " 1. CERRANDO MONITORIZACIÓN DEL TERMO FLECK NILO 150"
echo "=========================================================================="

if systemctl list-unit-files 2>/dev/null | grep -q "fleck-monitor.service"; then
    if systemctl is-active --quiet fleck-monitor 2>/dev/null; then
        echo "--> Deteniendo servicio systemd fleck-monitor (cerrando ciclo si estaba activo)..."
        sudo systemctl stop fleck-monitor || true
    fi
    if systemctl is-enabled --quiet fleck-monitor 2>/dev/null; then
        echo "--> Deshabilitando arranque automático de fleck-monitor..."
        sudo systemctl disable fleck-monitor || true
    fi
fi

if pgrep -f "src.collector" >/dev/null 2>&1; then
    echo "--> Enviando SIGTERM a procesos residuales de src.collector..."
    pkill -SIGTERM -f "src.collector" || true
    sleep 2
fi
echo "✅ Colector del termo detenido limpiamente."

echo ""
echo "=========================================================================="
echo " 2. GENERANDO INFORME HISTÓRICO EN FRÍO (ARCHIVO PERMANENTE)"
echo "=========================================================================="
"$VENV_PYTHON" -m src.report --archive "$@"

ARCHIVE_LATEST="$(ls -dt "$PROJECT_DIR"/archive/fleck_nilo_* 2>/dev/null | head -n 1 || true)"

echo ""
echo "=========================================================================="
echo " 3. PREPARANDO MONITOR DEL CONGELADOR (AISLANDO ENERGÍA PREVIA DE HOY)"
echo "=========================================================================="
"$VENV_PYTHON" "$PROJECT_DIR/check_freezer.py" --prepare-freezer

echo ""
echo "=========================================================================="
echo " 4. RESTAURANDO TAREA PERIÓDICA EN CRONTAB (CADA 15 MIN)"
echo "=========================================================================="
CRON_CMD="*/15 * * * * $VENV_PYTHON $PROJECT_DIR/check_freezer.py >> $PROJECT_DIR/cron.log 2>&1"
if command -v crontab >/dev/null 2>&1; then
    CURRENT_CRON="$(crontab -l 2>/dev/null || true)"
    if echo "$CURRENT_CRON" | grep -Eq "^#[[:space:]]*.*check_freezer\.py"; then
        echo "--> Descomentando tarea existente de check_freezer.py en crontab..."
        echo "$CURRENT_CRON" | sed 's/^#[[:space:]]*\(.*check_freezer\.py.*\)/\1/' | crontab -
    elif echo "$CURRENT_CRON" | grep -Fq "check_freezer.py"; then
        echo "--> La tarea de check_freezer.py ya estaba activa en crontab."
    else
        echo "--> Añadiendo tarea de check_freezer.py a crontab..."
        (echo "$CURRENT_CRON"; echo "$CRON_CMD") | sed '/^$/d' | crontab -
    fi
    echo "✅ Crontab activo:"
    crontab -l | grep "check_freezer.py" || true
fi

# Si existía algún servicio systemd previo del congelador deshabilitado, reactivarlo
for svc in check_freezer freezer-monitor monitor-congelador; do
    if systemctl list-unit-files 2>/dev/null | grep -q "^${svc}\.service"; then
        echo "--> Reactivando servicio systemd detectado: $svc"
        sudo systemctl enable "$svc" || true
        sudo systemctl start "$svc" || true
    fi
done

echo ""
echo "=========================================================================="
echo " 5. ESTADO FINAL DE PROCESOS Y SIGUIENTES PASOS"
echo "=========================================================================="
ps aux | grep python | grep -v grep || true
echo ""
echo "🎉 ¡TODO PREPARADO EN LA RASPBERRY PI!"
if [ -n "$ARCHIVE_LATEST" ]; then
    echo "📂 Archivo en frío guardado en: $ARCHIVE_LATEST"
    echo "💻 Para copiar el paquete histórico a tu PC Windows (PowerShell):"
    echo "   scp -r $(whoami)@$(hostname).local:$ARCHIVE_LATEST ./archive/"
fi
echo ""
echo "🔌 AHORA YA PUEDES MOVER EL ENCHUFE FÍSICAMENTE:"
echo "   1. Desenchufa el Tapo P110 del termo eléctrico."
echo "   2. Enchúfalo en la toma del congelador y conecta el congelador al Tapo P110."
echo "   3. Espera ~15 segundos a que conecte al Wi-Fi y envía /estado o /test en Telegram."
