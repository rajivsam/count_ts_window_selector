from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

import pytest

from count_ts_window_selector import (
    CandidateWindowError,
    DataSuitabilityError,
    WindowSelectionConfig,
    build_daily_window_table,
    check_data_suitability,
    filter_candidate_windows,
    select_time_window,
    teseda_export,
)

ROOT = Path(__file__).resolve().parents[1]
DATA_FILE = ROOT / "data" / "london-fire-brigade_timestamp_events.csv"


def _hourly_counts_from_csv(path: Path) -> list[int]:
    timestamps: list[datetime] = []
    with path.open("r", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            raw = str(row["timestamp"]).strip()
            if "." in raw:
                base, frac = raw.rsplit(".", 1)
                raw = base + "." + frac[:6]
            timestamps.append(datetime.strptime(raw, "%Y-%m-%d %H:%M:%S.%f"))

    hourly_counts: dict[datetime, int] = {}
    for ts in timestamps:
        key = ts.replace(minute=0, second=0, microsecond=0)
        hourly_counts[key] = hourly_counts.get(key, 0) + 1
    return [hourly_counts[key] for key in sorted(hourly_counts)]


def test_data_suitability_rejects_sparse_series() -> None:
    with pytest.raises(DataSuitabilityError):
        check_data_suitability([0, 0, 0, 0, 1], min_event_rate=5.0, max_zero_window_proportion=0.1)


def test_candidate_windows_are_filtered_by_minimum_coverage() -> None:
    with pytest.raises(CandidateWindowError):
        filter_candidate_windows([1, 2, 3, 4, 5] * 5, [30], min_windows=30)


def test_select_time_window_processes_candidate_windows() -> None:
    counts = [8, 9, 7, 10, 11, 6, 9, 8, 10, 7, 9, 8, 11, 7, 9, 10, 8, 6, 9, 7]
    config = WindowSelectionConfig(min_event_rate=2.0, max_zero_window_proportion=0.5, min_windows=3, confidence_level=0.90)

    results = select_time_window(counts, [1, 2, 3, 4, 5], config=config)

    assert isinstance(results, list)
    assert results
    assert all({"window_size", "fano_factor", "fano_lower", "fano_upper"}.issubset(result) for result in results)


def test_daily_window_table_uses_full_days_only_and_writes_csv(tmp_path: Path) -> None:
    summary = build_daily_window_table(
        data_file=DATA_FILE,
        window_size_hours=2,
        output_path=tmp_path / "daily_window_table.csv",
    )

    assert summary["first_full_day"] == "2024-01-02"
    assert summary["last_full_day"] == "2024-01-29"
    assert summary["omitted_prefix"] is not None
    assert summary["omitted_suffix"] is not None
    assert len(summary["rows"]) == 28
    assert Path(summary["output_file"]).exists()
    assert summary["columns"][0] == "window_00"
    assert summary["rows"][0]["date"] == "2024-01-02"


def test_teseda_export_uses_two_columns_and_reports_sampling_period(tmp_path: Path) -> None:
    summary = teseda_export(
        data_file=DATA_FILE,
        window_size_hours=2,
        output_path=tmp_path / "teseda_export.csv",
    )

    assert summary["sampling_period"] == "2 hours"
    assert summary["intensity_unit"] == "arrivals per hour"
    assert summary["first_full_day"] == "2024-01-02"
    assert summary["last_full_day"] == "2024-01-29"
    assert summary["rows"][0]["window_start"].startswith("2024-01-02T00:00:00")
    assert len(summary["rows"]) == 28 * 12
    assert Path(summary["output_file"]).exists()
