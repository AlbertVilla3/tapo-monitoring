#!/usr/bin/env bash
# scripts/fix_environment.sh - Detiene monitor congelador, limpia BD y saca reporte
set -e

cd "$(dirname "$0")/.."

echo "======================================================================"
echo " 1. DETENER MONITOR DEL CONGELADOR (check_freezer.py / PID 14279)"
echo "======================================================================"

# 1.1 Comprobar si systemd (root o user) tiene algún servicio de check_freezer
for svc in check_freezer freezer-monitor monitor-congelador; do
    if systemctl is-active --quiet "$svc" 2>/dev/null; then
        echo "--> Deteniendo servicio systemd: $svc"
        sudo systemctl stop "$svc" || true
        sudo systemctl disable "$svc" || true
    fi
    if systemctl --user is-active --quiet "$svc" 2>/dev/null; then
        echo "--> Deteniendo servicio systemd user: $svc"
        systemctl --user stop "$svc" || true
        systemctl --user disable "$svc" || true
    fi
done

# 1.2 Detener procesos activos de check_freezer.py (incluido PID 14279)
echo "--> Matando procesos activos de check_freezer.py..."
pkill -f "check_freezer.py" || true
rm -f bot_listener.pid

# 1.3 Comprobar crontab
if crontab -l 2>/dev/null | grep -q "check_freezer.py"; then
    echo "--> Comentando tarea de check_freezer en crontab para evitar reinicios..."
    crontab -l | sed 's/^\(.*check_freezer\.py.*\)/# \1/' | crontab -
    echo "    (Guardada copia comentada en crontab)"
else
    echo "--> No hay entradas activas de check_freezer en crontab."
fi

echo ""
echo "======================================================================"
echo " 2. SANEAMIENTO DE DUPLICADOS EN SQLITE (heating_cycles)"
echo "======================================================================"
./venv/bin/python -m src.db

echo ""
echo "======================================================================"
echo " 3. VERIFICACIÓN DE PROCESOS PYTHON ACTIVOS"
echo "======================================================================"
ps aux | grep python | grep -v grep || true

echo ""
echo "======================================================================"
echo " 4. INFORME ACTUALIZADO (src.report)"
echo "======================================================================"
./venv/bin/python -m src.report
