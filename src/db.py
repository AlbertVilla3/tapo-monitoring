"""
src/db.py - Gestión de la base de datos SQLite aislada para el test
del termo Fleck Nilo 150 (data/fleck_monitor_test.db).
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Generator, List, Optional, Tuple

from src.config import DB_PATH


@contextmanager
def get_db_connection(db_path: Path = DB_PATH) -> Generator[sqlite3.Connection, None, None]:
    """
    Context manager para conexiones SQLite seguras con WAL mode habilitado
    para alta fiabilidad en Raspberry Pi.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: Path = DB_PATH) -> None:
    """Inicializa el esquema de tablas si no existen."""
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()

        # Tabla de muestras instantáneas (muestreo cada 15s)
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS raw_samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp_utc TEXT NOT NULL,
                timestamp_local TEXT NOT NULL,
                power_w REAL NOT NULL,
                current_a REAL NOT NULL,
                today_energy_wh INTEGER NOT NULL
            );
            """
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_raw_samples_ts ON raw_samples(timestamp_utc);"
        )

        # Tabla de ciclos de calentamiento detectados
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS heating_cycles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                duration_sec INTEGER NOT NULL,
                energy_wh REAL NOT NULL,
                avg_power_w REAL NOT NULL,
                max_power_w REAL NOT NULL,
                power_mode TEXT NOT NULL CHECK(power_mode IN ('HALF_1250W', 'FULL_2500W')),
                cycle_type TEXT NOT NULL CHECK(cycle_type IN ('MAINTENANCE_LOSS', 'WATER_DRAW_RECOVERY'))
            );
            """
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_heating_cycles_start ON heating_cycles(start_time);"
        )

        # Tabla de metadatos de la prueba (inicio, fin, estado)
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS test_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )


def insert_raw_sample(
    timestamp_utc: str,
    timestamp_local: str,
    power_w: float,
    current_a: float,
    today_energy_wh: int,
    db_path: Path = DB_PATH,
) -> int:
    """Inserta una muestra puntual en raw_samples."""
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO raw_samples (
                timestamp_utc, timestamp_local, power_w, current_a, today_energy_wh
            ) VALUES (?, ?, ?, ?, ?);
            """,
            (timestamp_utc, timestamp_local, round(power_w, 2), round(current_a, 2), int(today_energy_wh)),
        )
        return cursor.lastrowid or 0


def insert_heating_cycle(
    start_time: str,
    end_time: str,
    duration_sec: int,
    energy_wh: float,
    avg_power_w: float,
    max_power_w: float,
    power_mode: str,
    cycle_type: str,
    db_path: Path = DB_PATH,
) -> int:
    """Inserta un ciclo de calentamiento concluido."""
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO heating_cycles (
                start_time, end_time, duration_sec, energy_wh,
                avg_power_w, max_power_w, power_mode, cycle_type
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                start_time,
                end_time,
                int(duration_sec),
                round(energy_wh, 2),
                round(avg_power_w, 2),
                round(max_power_w, 2),
                power_mode,
                cycle_type,
            ),
        )
        return cursor.lastrowid or 0


def get_metadata(key: str, default: Optional[str] = None, db_path: Path = DB_PATH) -> Optional[str]:
    """Obtiene un valor de test_metadata."""
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM test_metadata WHERE key = ?;", (key,))
        row = cursor.fetchone()
        return str(row["value"]) if row else default


def set_metadata(key: str, value: str, db_path: Path = DB_PATH) -> None:
    """Establece o actualiza un valor en test_metadata."""
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO test_metadata (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value;
            """,
            (key, str(value)),
        )


def get_raw_samples_count(db_path: Path = DB_PATH) -> int:
    """Devuelve el total de muestras en raw_samples."""
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) AS cnt FROM raw_samples;")
        row = cursor.fetchone()
        return int(row["cnt"]) if row else 0


def get_all_cycles(db_path: Path = DB_PATH) -> List[sqlite3.Row]:
    """Devuelve todos los ciclos ordenados cronológicamente."""
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM heating_cycles ORDER BY start_time ASC;")
        return cursor.fetchall()


def get_first_and_last_sample_timestamps(db_path: Path = DB_PATH) -> Tuple[Optional[str], Optional[str]]:
    """Devuelve (primer_timestamp, ultimo_timestamp) de raw_samples."""
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT MIN(timestamp_utc) as first_ts, MAX(timestamp_utc) as last_ts FROM raw_samples;")
        row = cursor.fetchone()
        if row and row["first_ts"]:
            return row["first_ts"], row["last_ts"]
        return None, None
