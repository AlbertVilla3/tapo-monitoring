#!/usr/bin/env bash
# run_fleck_collector.sh - Script de ejecución en background para Raspberry Pi (tmux o directo)
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

if [ -f "$PROJECT_DIR/venv/bin/python" ]; then
    PYTHON_BIN="$PROJECT_DIR/venv/bin/python"
elif [ -f "$PROJECT_DIR/.venv/bin/python" ]; then
    PYTHON_BIN="$PROJECT_DIR/.venv/bin/python"
else
    PYTHON_BIN="python3"
fi

echo "=== Lanzando Monitor de Termo / Aerotermo ==="
echo "Directorio: $PROJECT_DIR"
echo "Python    : $PYTHON_BIN"
echo "Base datos: $PROJECT_DIR/data"

exec "$PYTHON_BIN" -m src.collector
