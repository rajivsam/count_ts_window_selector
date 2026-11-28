from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from count_ts_window_selector import (
    CandidateWindowError,
    DataSuitabilityError,
    HorizonBoundaryError,
    HorizonRecommendationConfig,
    WindowSelectionConfig,
    build_daily_window_table,
    build_arrival_intensity_series,
    check_data_suitability,
    evaluate_horizon,
    extract_horizon,
    filter_candidate_windows,
    recommend_optimal_horizon,
    rank_accepted_windows,
    save_tseda_arrival_intensity_series,
    select_time_window,
    teseda_export,
    tune_horizon_windows,
)

ROOT = Path(__file__).resolve().parents[1]
DATA_FILE = ROOT / "data" / "london-fire-brigade_timestamp_events.csv"


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


def test_extract_horizon_builds_complete_half_open_hourly_grid() -> None:
    timestamps = [
        datetime(2024, 1, 1, 0, 0),
        datetime(2024, 1, 1, 0, 15),
        datetime(2024, 1, 1, 2, 15),
        datetime(2024, 1, 1, 3, 0),
    ]

    horizon = extract_horizon(
        timestamps,
        start_date=datetime(2024, 1, 1),
        horizon_window=timedelta(hours=3),
        base_resolution="1h",
        boundary_policy="truncate",
    )

    assert horizon["counts"] == [2, 0, 1]
    assert horizon["number_of_base_intervals"] == 3
    assert horizon["number_of_events"] == 3
    assert horizon["boundary_metadata"]["is_complete"] is True


def test_extract_horizon_rejects_incomplete_boundary_by_default() -> None:
    timestamps = [datetime(2024, 1, 1, hour) for hour in range(10)]

    with pytest.raises(HorizonBoundaryError, match="not fully covered"):
        extract_horizon(
            timestamps,
            start_date=datetime(2023, 12, 31, 12),
            horizon_window="2 days",
        )


def test_evaluate_horizon_returns_all_candidate_diagnostics() -> None:
    start = datetime(2024, 1, 1)
    timestamps = [
        start
        + timedelta(hours=hour, minutes=minute)
        for hour in range(72)
        for minute in (5, 20, 35, 50)
    ]
    timestamps = [start, *timestamps, start + timedelta(days=3)]
    config = WindowSelectionConfig(
        min_event_rate=2.0,
        max_zero_window_proportion=0.1,
        min_windows=10,
    )

    result = evaluate_horizon(
        timestamps,
        [1, 2, 8],
        start_date=start,
        horizon_window="3 days",
        config=config,
    )

    assert result["suitability_status"] == "suitable"
    assert result["valid_candidate_windows"] == [1, 2]
    assert result["rejected_candidate_windows"][0]["window_size"] == 8
    assert all(
        {
            "window_size",
            "number_of_windows",
            "mean_count",
            "sample_variance",
            "fano_factor",
            "fano_lower",
            "fano_upper",
            "confidence_band_width",
            "accepted",
        }.issubset(candidate)
        for candidate in result["candidate_windows"]
    )


def test_full_range_evaluation_reports_mode_and_deterministic_selection() -> None:
    start = datetime(2024, 1, 1, 9, 30)
    daily_counts = [5, 10, 14, 7, 12, 8, 9, 7, 13, 8] * 4
    timestamps = [
        start + timedelta(days=day, minutes=event)
        for day, daily_count in enumerate(daily_counts)
        for event in range(daily_count)
    ]
    result = evaluate_horizon(
        timestamps,
        [1, 2],
        horizon_window=None,
        base_resolution="1d",
        config=WindowSelectionConfig(
            min_event_rate=1.0,
            min_windows=10,
            max_zero_window_proportion=0.1,
        ),
    )

    assert result["evaluation_mode"] == "full_range"
    assert result["horizon_window"] is None
    assert result["is_complete"] is True
    assert result["start_date"].endswith("T00:00:00")
    assert result["requested_candidate_windows"] == [1, 2]
    assert result["selected_window"] == result["ranked_accepted_windows"][0]


def test_rank_accepted_windows_prefers_width_after_statistical_ties() -> None:
    candidates = [
        {"window_size": 7, "fano_factor": 1.0, "confidence_band_width": 0.5},
        {"window_size": 1, "fano_factor": 1.0, "confidence_band_width": 0.5},
    ]

    assert rank_accepted_windows(candidates)[0]["window_size"] == 1
    assert rank_accepted_windows(
        candidates, prefer_shorter_window=False
    )[0]["window_size"] == 7


def test_full_range_intensity_series_uses_selected_window_rate() -> None:
    start = datetime(2024, 1, 1)
    timestamps = [
        start + timedelta(hours=hour)
        for hour in range(5 * 24)
    ]

    series = build_arrival_intensity_series(
        timestamps,
        window_size=2,
        base_resolution="1d",
    )

    assert series["evaluation_mode"] == "full_range"
    assert series["window_size"] == 2
    assert series["intensity_unit"] == "arrivals per hour"
    assert series["remainder_base_intervals"] == 1
    assert series["number_of_complete_windows"] == 2
    assert series["rows"][0]["window_start"] == "2024-01-01T00:00:00"
    assert series["rows"][0]["window_midpoint"] == "2024-01-02T00:00:00"
    assert len(series["rows"]) == 2


def test_save_tseda_selected_window_series_uses_midpoints(tmp_path: Path) -> None:
    start = datetime(2024, 1, 1)
    timestamps = [
        start + timedelta(hours=hour)
        for hour in range(5 * 24)
    ]
    output_path = tmp_path / "selected_window_teseda.csv"

    summary = save_tseda_arrival_intensity_series(
        timestamps,
        window_size=2,
        base_resolution="1d",
        rate_unit="day",
        output_path=output_path,
    )

    assert summary["output_file"] == str(output_path)
    assert summary["timestamp_coordinate"] == "window_midpoint"
    assert summary["columns"] == ["window_midpoint", "arrival_intensity"]
    assert summary["number_of_complete_windows"] == 2
    assert summary["number_of_exported_rows"] == 2
    assert summary["remainder_base_intervals"] == 1
    rows = output_path.read_text().splitlines()
    assert rows[0] == "window_midpoint,arrival_intensity"
    assert rows[1].startswith("2024-01-02T00:00:00,")


def test_save_tseda_selected_window_series_supports_two_stage_horizon(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "two_stage_selected_window_teseda.csv"

    summary = save_tseda_arrival_intensity_series(
        DATA_FILE,
        window_size=1,
        start_date="2024-01-02",
        horizon_window="7 days",
        output_path=output_path,
        base_resolution="1d",
        rate_unit="day",
    )

    assert summary["evaluation_mode"] == "horizon_scoped"
    assert summary["start_date"] == "2024-01-02T00:00:00"
    assert summary["end_date"] == "2024-01-09T00:00:00"
    assert summary["number_of_exported_rows"] == 7
    assert len(output_path.read_text().splitlines()) == 8


def test_full_range_overdispersion_recommends_two_stage_analysis() -> None:
    start = datetime(2024, 1, 1)
    timestamps = [
        start + timedelta(days=day)
        for day in range(20)
        for _ in range((day % 5) * 20 + 10)
    ]
    result = evaluate_horizon(
        timestamps,
        [1],
        horizon_window=None,
        base_resolution="1h",
        config=WindowSelectionConfig(
            min_event_rate=0.1,
            max_zero_window_proportion=1.0,
            min_windows=10,
        ),
    )

    assert not result["poisson_compatible_window_found"]
    assert result["diagnostics"]["global_non_stationarity_suspected"] is True
    assert "two-stage workflow" in result["diagnostics"]["message"]


def test_tuning_reports_conditional_acceptance_and_valid_coverage() -> None:
    start = datetime(2024, 1, 1)
    timestamps = [
        start + timedelta(hours=hour, minutes=minute)
        for hour in range(72)
        for minute in (5, 20, 35, 50)
    ]
    config = WindowSelectionConfig(
        min_event_rate=2.0,
        max_zero_window_proportion=0.1,
        min_windows=6,
    )

    summaries = tune_horizon_windows(
        timestamps,
        candidate_horizons=["1 day", "2 days"],
        candidate_windows=[1, 2],
        step_size="1 day",
        config=config,
    )

    one_day = summaries[0]
    assert one_day["attempted_horizons"] == 2
    assert one_day["suitability_valid_horizons"] == 2
    assert one_day["suitability_failed_horizons"] == 0
    assert one_day["poisson_acceptance_rate"] == (
        one_day["accepted_horizons"] / one_day["suitability_valid_horizons"]
    )
    assert one_day["suitability_valid_horizon_proportion"] == 1.0


def test_tuning_uses_null_acceptance_when_no_horizons_are_suitable() -> None:
    start = datetime(2024, 1, 1)
    timestamps = [start + timedelta(hours=hour) for hour in range(30)]
    config = WindowSelectionConfig(
        min_event_rate=5.0,
        max_zero_window_proportion=0.1,
        min_windows=6,
    )

    summary = tune_horizon_windows(
        timestamps,
        candidate_horizons=["1 day"],
        candidate_windows=[1],
        step_size="1 day",
        config=config,
    )[0]

    assert summary["suitability_valid_horizons"] == 0
    assert summary["suitability_failed_horizons"] > 0
    assert summary["poisson_acceptance_rate"] is None
    assert summary["diagnostics"]["zero_suitability_valid_horizons"] is True


def test_horizon_recommendation_uses_tolerance_then_deterministic_ties() -> None:
    comparison_table = [
        {
            "horizon_window": "14 days, 0:00:00",
            "horizon_window_days": 14.0,
            "poisson_acceptance_rate": 0.96,
            "suitability_valid_horizon_proportion": 0.95,
            "suitability_valid_horizons": 19,
            "median_dispersion_deviation": 0.10,
            "mean_confidence_band_width": 1.20,
        },
        {
            "horizon_window": "7 days, 0:00:00",
            "horizon_window_days": 7.0,
            "poisson_acceptance_rate": 0.95,
            "suitability_valid_horizon_proportion": 1.0,
            "suitability_valid_horizons": 24,
            "median_dispersion_deviation": 0.10,
            "mean_confidence_band_width": 1.20,
        },
        {
            "horizon_window": "21 days, 0:00:00",
            "horizon_window_days": 21.0,
            "poisson_acceptance_rate": 0.92,
            "suitability_valid_horizon_proportion": 0.90,
            "suitability_valid_horizons": 10,
            "median_dispersion_deviation": 0.05,
            "mean_confidence_band_width": 0.80,
        },
    ]

    recommendation = recommend_optimal_horizon(comparison_table)

    assert recommendation["recommended_horizon"] == "7 days, 0:00:00"
    assert recommendation["rule_config"]["prefer_shorter_horizon"] is True
    assert recommendation["selected_metrics"] == comparison_table[1]
    assert [item["outcome"] for item in recommendation["candidate_audit"]] == [
        "runner_up",
        "selected",
        "rejected",
    ]
    assert recommendation["candidate_audit"][2]["stage"] == "acceptance_rate_filter"
    assert (
        recommendation["candidate_audit"][2]["failures"][0]["observed"]
        == 0.92
    )
    assert recommendation["diagnostics"]["acceptance_rate_floor"] == pytest.approx(0.94)


def test_horizon_recommendation_returns_null_without_reliable_candidates() -> None:
    recommendation = recommend_optimal_horizon(
        [
            {
                "horizon_window": "3 days",
                "horizon_window_days": 3.0,
                "poisson_acceptance_rate": 1.0,
                "suitability_valid_horizon_proportion": 0.2,
                "suitability_valid_horizons": 2,
                "median_dispersion_deviation": 0.01,
                "mean_confidence_band_width": 1.0,
            }
        ]
    )

    assert recommendation["recommended_horizon"] is None
    assert recommendation["selected_metrics"] is None
    assert recommendation["candidate_audit"][0]["outcome"] == "rejected"
    assert {
        failure["gate"] for failure in recommendation["candidate_audit"][0]["failures"]
    } == {"min_valid_horizon_proportion", "min_valid_horizons"}


def test_horizon_recommendation_prefers_longer_when_configured() -> None:
    rows = [
        {
            "horizon_window": "7 days",
            "horizon_window_days": 7.0,
            "poisson_acceptance_rate": 0.95,
            "suitability_valid_horizon_proportion": 1.0,
            "suitability_valid_horizons": 20,
            "median_dispersion_deviation": 0.1,
            "mean_confidence_band_width": 1.0,
        },
        {
            "horizon_window": "14 days",
            "horizon_window_days": 14.0,
            "poisson_acceptance_rate": 0.95,
            "suitability_valid_horizon_proportion": 1.0,
            "suitability_valid_horizons": 20,
            "median_dispersion_deviation": 0.1,
            "mean_confidence_band_width": 1.0,
        },
    ]

    recommendation = recommend_optimal_horizon(
        rows,
        config=HorizonRecommendationConfig(prefer_shorter_horizon=False),
    )

    assert recommendation["recommended_horizon"] == "14 days"


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"min_valid_horizon_proportion": 0.0}, "min_valid_horizon_proportion"),
        ({"min_valid_horizon_proportion": 1.1}, "min_valid_horizon_proportion"),
        ({"min_valid_horizons": 0}, "min_valid_horizons"),
        ({"acceptance_rate_tolerance": -0.01}, "acceptance_rate_tolerance"),
    ],
)
def test_horizon_recommendation_config_validation(
    kwargs: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        HorizonRecommendationConfig(**kwargs)  # type: ignore[arg-type]
