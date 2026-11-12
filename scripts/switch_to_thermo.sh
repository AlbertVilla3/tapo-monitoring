#!/usr/bin/env bash
# scripts/switch_to_thermo.sh
# Prepara el entorno en la Raspberry Pi para monitorizar el nuevo termo:
# 1. Pausa el monitor del congelador (desactiva en crontab y detiene procesos activos).
# 2. Inicializa la base de datos aislada para el nuevo termo (data/new_water_heater_test.db).
# 3. Inicia el colector del nuevo termo (src.collector) en segundo plano (vía systemd o daemon).
# 4. Muestra el estado de procesos y las instrucciones para cambiar el enchufe.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

if [ -x "$PROJECT_DIR/venv/bin/python" ]; then
    VENV_PYTHON="$PROJECT_DIR/venv/bin/python"
else
    VENV_PYTHON="python3"
fi

echo "=========================================================================="
echo " 1. PAUSANDO MONITORIZACIÓN DEL CONGELADOR"
echo "=========================================================================="

# 1.1 Comentar la tarea periódica del congelador en crontab
if command -v crontab >/dev/null 2>&1; then
    CURRENT_CRON="$(crontab -l 2>/dev/null || true)"
    if echo "$CURRENT_CRON" | grep -Eq "^[^#].*check_freezer\.py"; then
        echo "--> Comentando tarea de check_freezer.py en crontab..."
        echo "$CURRENT_CRON" | sed 's/^\([^#].*check_freezer\.py.*\)/# \1/' | crontab -
        echo "✅ Crontab pausado para el congelador."
    else
        echo "--> Crontab ya no tenía check_freezer activo o ya estaba pausado."
    fi
fi

# 1.2 Detener servicios systemd del congelador si existieran
for svc in check_freezer freezer-monitor monitor-congelador; do
    if systemctl list-unit-files 2>/dev/null | grep -q "^${svc}\.service"; then
        echo "--> Deteniendo servicio $svc..."
        sudo systemctl stop "$svc" || true
        sudo systemctl disable "$svc" || true
    fi
done

# 1.3 Matar procesos en segundo plano de check_freezer (listener de Telegram, etc.)
echo "--> Deteniendo procesos residuales de check_freezer.py..."
pkill -f "check_freezer.py" || true
rm -f bot_listener.pid

echo ""
echo "=========================================================================="
echo " 2. PREPARANDO BASE DE DATOS Y ENTORNO PARA EL NUEVO TERMO"
echo "=========================================================================="
mkdir -p "$PROJECT_DIR/data"
"$VENV_PYTHON" -m src.db

echo ""
echo "=========================================================================="
echo " 3. ACTIVANDO COLECTOR DEL NUEVO TERMO (src.collector)"
echo "=========================================================================="

# Detener cualquier instancia anterior de src.collector
pkill -f "src.collector" || true
sleep 1

# Si systemd está disponible con fleck-monitor
STARTED_BY_SYSTEMD=false
if systemctl cat fleck-monitor.service >/dev/null 2>&1 || [ -f /etc/systemd/system/fleck-monitor.service ]; then
    echo "--> Activando servicio systemd fleck-monitor..."
    sudo systemctl daemon-reload || true
    sudo systemctl enable fleck-monitor || true
    sudo systemctl restart fleck-monitor || true
    sleep 2
    if systemctl is-active --quiet fleck-monitor; then
        echo "✅ Servicio systemd fleck-monitor iniciado correctamente."
        STARTED_BY_SYSTEMD=true
    fi
fi

if [ "$STARTED_BY_SYSTEMD" = false ]; then
    echo "--> Iniciando colector en segundo plano (nohup)..."
    nohup "$VENV_PYTHON" -m src.collector > "$PROJECT_DIR/collector.log" 2>&1 &
    COLLECTOR_PID=$!
    echo "$COLLECTOR_PID" > "$PROJECT_DIR/thermo_collector.pid"
    sleep 2
    if ps -p "$COLLECTOR_PID" > /dev/null 2>&1; then
        echo "✅ Colector iniciado en segundo plano (PID: $COLLECTOR_PID)."
    else
        echo "⚠️ Revisa collector.log para verificar el arranque."
    fi
fi

echo ""
echo "=========================================================================="
echo " 4. ESTADO FINAL DE PROCESOS"
echo "=========================================================================="
ps aux | grep python | grep -v grep || true

echo ""
echo "=========================================================================="
echo " 🎉 ¡ENTORNO PREPARADO PARA EL NUEVO TERMO!"
echo "=========================================================================="
echo "🔌 PASOS PARA COMPLETAR EL CAMBIO FÍSICO:"
echo "   1. Desenchufa el Tapo P110 del congelador (enchufa el congelador a la pared)."
echo "   2. Enchufa el Tapo P110 a la toma del nuevo termo y conecta el nuevo termo a él."
echo "   3. Espera ~15 segundos a que el enchufe se reconecte a tu Wi-Fi."
echo ""
echo "📊 COMANDOS ÚTILES DURANTE LA SEMANA:"
echo "   • Ver estado y consumo acumulado:  ./venv/bin/python -m src.report"
echo "   • Ver comparativa vs termo viejo:   ./venv/bin/python -m src.compare_report"
echo "   • Enviar comparativa a Telegram:   ./venv/bin/python -m src.compare_report --telegram"
echo "=========================================================================="
