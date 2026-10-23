"""
tests/test_fleck_monitor.py - Tests unitarios para el monitor de termo Fleck Nilo 150.
"""

from __future__ import annotations

import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.collector import CycleStateMachine
from src.db import (
    get_all_cycles,
    get_metadata,
    get_raw_samples_count,
    init_db,
    insert_heating_cycle,
    insert_raw_sample,
    set_metadata,
)
from src.report import calculate_metrics, format_cli_report


@pytest.fixture
def temp_db(tmp_path: Path) -> Path:
    db_file = tmp_path / "test_fleck.db"
    init_db(db_file)
    return db_file


def test_db_schema_and_metadata(temp_db: Path):
    init_db(temp_db)
    set_metadata("test_start_utc", "2026-10-23T12:00:00Z", db_path=temp_db)
    val = get_metadata("test_start_utc", db_path=temp_db)
    assert val == "2026-10-23T12:00:00Z"

    # Insert sample
    insert_raw_sample(
        timestamp_utc="2026-10-23T12:00:00Z",
        timestamp_local="2026-10-23T14:00:00+02:00",
        power_w=1250.0,
        current_a=5.43,
        today_energy_wh=100,
        db_path=temp_db,
    )
    assert get_raw_samples_count(temp_db) == 1


def test_state_machine_maintenance_cycle():
    sm = CycleStateMachine()

    # Standby
    c1 = sm.on_sample(100.0, "2026-10-23T02:00:00Z", "2026-10-23T04:00:00+02:00", 2.5, 0.01)
    assert c1 is None
    assert not sm.is_heating

    # Trigger cycle start
    c2 = sm.on_sample(115.0, "2026-10-23T02:00:15Z", "2026-10-23T04:00:15+02:00", 1250.0, 5.4)
    assert sm.is_heating
    assert c2 is None

    # Heating for a short maintenance period (e.g. 100 Wh)
    sm.on_sample(130.0, "2026-10-23T02:00:30Z", "2026-10-23T04:00:30+02:00", 1250.0, 5.4)
    sm.on_sample(145.0, "2026-10-23T02:00:45Z", "2026-10-23T04:00:45+02:00", 1250.0, 5.4)

    # First low sample (< 20W) -> not finished yet (needs 2 consecutive)
    c3 = sm.on_sample(160.0, "2026-10-23T02:01:00Z", "2026-10-23T04:01:00+02:00", 1.8, 0.01)
    assert c3 is None
    assert sm.is_heating

    # Second low sample -> cycle finalized!
    c4 = sm.on_sample(175.0, "2026-10-23T02:01:15Z", "2026-10-23T04:01:15+02:00", 1.5, 0.01)
    assert c4 is not None
    assert not sm.is_heating
    assert c4["power_mode"] == "HALF_1250W"
    assert c4["cycle_type"] == "MAINTENANCE_LOSS"
    assert c4["duration_sec"] > 0


def test_state_machine_water_draw_cycle():
    sm = CycleStateMachine()

    # Trigger cycle start with 2500W
    sm.on_sample(100.0, "2026-10-23T18:00:00Z", "2026-10-23T20:00:00+02:00", 2480.0, 10.7)
    assert sm.is_heating

    # Simulate long cycle with > 350 Wh
    # 2480 W * 600 s = 2480 * (600/3600) = 413.3 Wh
    sm.on_sample(700.0, "2026-10-23T18:10:00Z", "2026-10-23T20:10:00+02:00", 2480.0, 10.7)

    # End cycle with 2 low samples
    sm.on_sample(715.0, "2026-10-23T18:10:15Z", "2026-10-23T20:10:15+02:00", 5.0, 0.02)
    c = sm.on_sample(730.0, "2026-10-23T18:10:30Z", "2026-10-23T20:10:30+02:00", 4.8, 0.02)

    assert c is not None
    assert c["power_mode"] == "FULL_2500W"
    assert c["cycle_type"] == "WATER_DRAW_RECOVERY"
    assert c["energy_wh"] >= 350.0


def test_report_calculations(temp_db: Path):
    # Insert simulated samples over 1 day
    # Base standby sample at 00:00
    insert_raw_sample("2026-10-23T00:00:00Z", "2026-10-23T02:00:00+02:00", 3.0, 0.01, 0, temp_db)
    # Maintenance cycle: 200 Wh, 1250W
    insert_heating_cycle(
        start_time="2026-10-23T04:00:00Z",
        end_time="2026-10-23T04:10:00Z",
        duration_sec=600,
        energy_wh=200.0,
        avg_power_w=1200.0,
        max_power_w=1250.0,
        power_mode="HALF_1250W",
        cycle_type="MAINTENANCE_LOSS",
        db_path=temp_db,
    )
    # Water draw cycle: 2000 Wh, 2500W
    insert_heating_cycle(
        start_time="2026-10-23T20:00:00Z",
        end_time="2026-10-23T20:50:00Z",
        duration_sec=3000,
        energy_wh=2000.0,
        avg_power_w=2400.0,
        max_power_w=2490.0,
        power_mode="FULL_2500W",
        cycle_type="WATER_DRAW_RECOVERY",
        db_path=temp_db,
    )
    # Ending sample at 24h
    insert_raw_sample("2026-10-24T00:00:00Z", "2026-10-24T02:00:00+02:00", 3.0, 0.01, 2200, temp_db)

    metrics = calculate_metrics(temp_db)
    assert metrics["has_data"] is True
    assert metrics["cycles_breakdown"]["total_starts"] == 2
    assert metrics["cycles_breakdown"]["maintenance"]["count"] == 1
    assert metrics["cycles_breakdown"]["water_draw"]["count"] == 1

    # Water volume check
    # 2000 Wh / 52.335 Wh/L ~ 38.2 Litros
    assert metrics["water"]["estimated_liters_60c"] > 35.0

    # Aerotherm simulation check
    assert metrics["aerotherm_simulation"]["cop"] == 3.0
    assert metrics["aerotherm_simulation"]["irpf_eligible"] is True
    assert metrics["aerotherm_simulation"]["reduction_pct"] > 60.0

    # Text report formatting
    txt = format_cli_report(metrics)
    assert "FLECK NILO 150" in txt
    assert "AEROTERMO" in txt
    assert "IRPF" in txt
