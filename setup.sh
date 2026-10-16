#!/usr/bin/env bash
set -euo pipefail

# Directorio base donde se encuentra este script
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

echo "=== ConfiguraciÃ³n de Monitor de Congelador (Tapo P110) ==="
echo "Directorio del proyecto: $PROJECT_DIR"

# 1. Crear entorno virtual venv
if [ ! -d "$PROJECT_DIR/venv" ]; then
    echo "[1/4] Creando entorno virtual en $PROJECT_DIR/venv..."
    python3 -m venv "$PROJECT_DIR/venv" || python -m venv "$PROJECT_DIR/venv"
else
    echo "[1/4] El entorno virtual ya existe."
fi

# Determinar ruta de python y pip dentro del venv (soporta Linux/RPi y Windows Git Bash)
if [ -x "$PROJECT_DIR/venv/bin/python" ]; then
    VENV_PYTHON="$PROJECT_DIR/venv/bin/python"
    VENV_PIP="$PROJECT_DIR/venv/bin/pip"
elif [ -x "$PROJECT_DIR/venv/Scripts/python.exe" ]; then
    VENV_PYTHON="$PROJECT_DIR/venv/Scripts/python.exe"
    VENV_PIP="$PROJECT_DIR/venv/Scripts/pip.exe"
else
    echo "ERROR: No se encontrÃ³ el binario de Python dentro de venv."
    exit 1
fi

# 2. Instalar dependencias
echo "[2/4] Instalando dependencias desde requirements.txt..."
"$VENV_PYTHON" -m pip install --upgrade pip
"$VENV_PIP" install -r "$PROJECT_DIR/requirements.txt"

chmod +x "$PROJECT_DIR/check_freezer.py" 2>/dev/null || true

# 3. Validar archivo .env
echo "[3/4] Validando archivo .env..."
if [ ! -f "$PROJECT_DIR/.env" ]; then
    cp "$PROJECT_DIR/.env.example" "$PROJECT_DIR/.env"
    echo "AVISO: Se ha creado .env a partir de .env.example."
fi

# AÃ±adir tarea a crontab de forma idempotente (si crontab estÃ¡ disponible en el sistema)
CRON_CMD="*/15 * * * * $VENV_PYTHON $PROJECT_DIR/check_freezer.py >> $PROJECT_DIR/cron.log 2>&1"
if command -v crontab >/dev/null 2>&1; then
    echo "[3.5/4] Configurando tarea en crontab (cada 15 minutos)..."
    CURRENT_CRON="$(crontab -l 2>/dev/null || true)"
    if echo "$CURRENT_CRON" | grep -Fq "$PROJECT_DIR/check_freezer.py"; then
        echo "La tarea ya existe en crontab. No se duplicarÃ¡."
    else
        (echo "$CURRENT_CRON"; echo "$CRON_CMD") | sed '/^$/d' | crontab -
        echo "Tarea aÃ±adida a crontab correctamente:"
        echo "  $CRON_CMD"
    fi
else
    echo "AVISO: Comando 'crontab' no disponible en este entorno. En Raspberry Pi se aÃ±adirÃ¡ automÃ¡ticamente:"
    echo "  $CRON_CMD"
fi

# Comprobar si el .env aÃºn tiene los valores de plantilla antes de lanzar el test
if grep -Eq "192\.168\.1\.XXX|tu_email@dominio\.com|tu_password_tplink|123456789:ABCDEF" "$PROJECT_DIR/.env"; then
    echo "[4/4] El archivo .env contiene valores de plantilla por defecto."
    echo "      Edita $PROJECT_DIR/.env con tus credenciales reales y ejecuta:"
    echo "      $VENV_PYTHON $PROJECT_DIR/check_freezer.py --test"
    exit 0
fi

# 4. Ejecutar test de conexiÃ³n
echo "[4/4] Ejecutando test de conexiÃ³n (--test)..."
"$VENV_PYTHON" "$PROJECT_DIR/check_freezer.py" --test
