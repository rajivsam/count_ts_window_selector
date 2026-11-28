# count-ts-window-selector

A Python package for selecting Poisson-compatible observation windows in higher-event-rate count time series.

## Overview

This package implements the workflow described in the project proposal:

1. Validate series density and zero inflation before any Fano analysis.
2. Filter candidate window sizes to those that retain at least 30 non-overlapping windows.
3. Aggregate counts over each candidate window.
4. Compute the Fano factor and chi-square-based confidence interval around 1.
5. Return only windows whose Fano factor remains inside the configured confidence band.

The package intentionally targets higher-event-rate count series and explicitly rejects sparse, zero-heavy, or hurdle-style data before Fano-factor evaluation.

## Installation

With uv:

```bash
uv sync
```

## Usage

```python
from count_ts_window_selector import WindowSelectionConfig, select_time_window

counts = [
    12, 10, 9, 11, 13, 8, 14, 11, 10, 9,
    12, 10, 11, 13, 9, 10, 8, 12, 13, 11,
    10, 11, 8, 12, 10, 9, 13, 11, 12, 10,
]

config = WindowSelectionConfig(
    min_event_rate=5.0,
    max_zero_window_proportion=0.1,
    min_windows=30,
    confidence_level=0.90,
)
results = select_time_window(counts, [2, 3, 5, 10, 15], config=config)
print(results)
```

## Horizon-scoped analysis

Timestamp data can be evaluated inside a bounded macro-horizon before applying
the micro-window Fano analysis:

```python
from count_ts_window_selector import WindowSelectionConfig, evaluate_horizon

result = evaluate_horizon(
    "data/london-fire-brigade_timestamp_events.csv",
    [1, 2, 3],
    start_date="2024-01-02",
    horizon_window="7 days",
    base_resolution="1d",
    boundary_policy="error",
    config=WindowSelectionConfig(min_windows=2),
)

print(result["suitability_status"])
print(result["accepted_windows"])
```

## Single-stage full-range analysis

Set `horizon_window=None` to evaluate the complete aligned dataset range. The
result reports the evaluation mode, retains every accepted candidate, and
deterministically ranks them for a single selected window:

```python
from count_ts_window_selector import (
    build_arrival_intensity_series,
    plot_arrival_intensity,
    save_tseda_arrival_intensity_series,
)

full_range = evaluate_horizon(
    "data/london-fire-brigade_timestamp_events.csv",
    [1, 2, 3, 4, 5, 7],
    horizon_window=None,
    base_resolution="1d",
    config=WindowSelectionConfig(min_event_rate=1.0, min_windows=2),
)

selected = full_range["selected_window"]
if selected is not None:
    intensity = build_arrival_intensity_series(
        "data/london-fire-brigade_timestamp_events.csv",
        window_size=selected["window_size"],
        base_resolution="1d",
    )
    plot_arrival_intensity(intensity)
    export = save_tseda_arrival_intensity_series(
        "data/london-fire-brigade_timestamp_events.csv",
        window_size=selected["window_size"],
        output_path="data/london_single_window_teseda.csv",
        base_resolution="1d",
        rate_unit="day",
    )
```

`selected_window` is ranked by dispersion deviation from 1, confidence-band
width, and window length. If no full-range candidate is accepted and
overdispersion is observed, diagnostics recommend the bounded two-stage
workflow rather than treating the failure as an invalid input.

The selected-window TSEDA CSV is saved with `window_midpoint` and
`arrival_intensity` columns. The midpoint represents the temporal center of
each complete, non-overlapping selected window.

The same export supports two-stage output: supply the selected macro-horizon's
`start_date` and `horizon_window` along with its selected micro-window. Omit
`horizon_window` for a full-range single-stage export.

```python
two_stage_export = save_tseda_arrival_intensity_series(
    "data/london-fire-brigade_timestamp_events.csv",
    window_size=1,
    start_date="2024-01-02",
    horizon_window="7 days",
    output_path="data/london_two_stage_teseda.csv",
    base_resolution="1d",
    rate_unit="day",
)
```

The extracted interval is half-open: `[start_date, start_date + horizon_window)`.
Incomplete requested horizons raise `HorizonBoundaryError` by default. Complete
base-resolution intervals are constructed explicitly, including zero-count
intervals.

Candidate horizon lengths can be compared by stepping through the available
dataset:

```python
from count_ts_window_selector import WindowSelectionConfig, tune_horizon_windows

summaries = tune_horizon_windows(
    "data/london-fire-brigade_timestamp_events.csv",
    candidate_horizons=["7 days", "14 days", "21 days"],
    candidate_windows=[1, 2, 3],
    step_size="1 day",
    base_resolution="1d",
    config=WindowSelectionConfig(min_windows=2),
)
```

Each summary reports attempted, suitability-valid, and suitability-failed
horizons alongside the Poisson acceptance rate. The acceptance rate is
conditional on suitability, and is `None` when no suitable horizon exists.

Apply a separate, explicit recommendation rule to the comparison table:

```python
from count_ts_window_selector import (
    HorizonRecommendationConfig,
    recommend_optimal_horizon,
)

recommendation = recommend_optimal_horizon(
    summaries,
    config=HorizonRecommendationConfig(
        min_valid_horizon_proportion=0.80,
        min_valid_horizons=10,
        acceptance_rate_tolerance=0.02,
        prefer_shorter_horizon=True,
    ),
)
print(recommendation["recommended_horizon"])
print(recommendation["candidate_audit"])
```

Tuning only compares horizons; recommendation is deliberately a separate
stateless operation. If no candidate passes the reliability gates, the
recommended horizon is `None` and the per-candidate audit identifies the
failed observed-versus-required thresholds.

## Daily tabular export

```python
from count_ts_window_selector import build_daily_window_table

summary = build_daily_window_table(
    data_file="data/london-fire-brigade_timestamp_events.csv",
    window_size_hours=2,
)
print(summary["output_file"])
print(summary["first_full_day"], summary["last_full_day"])
print(summary["omitted_prefix"])
print(summary["omitted_suffix"])
```

This writes a day-by-day table with one row per full calendar day and one column per selected window within the day. Partial leading or trailing days are omitted and reported in the metadata.

## TSEDA-ready export

```python
from count_ts_window_selector import teseda_export

summary = teseda_export(
    data_file="data/london-fire-brigade_timestamp_events.csv",
    window_size_hours=2,
)
print(summary["output_file"])
print(summary["sampling_period"])  # 2 hours
print(summary["intensity_unit"])  # arrivals per hour
```

This writes a two-column series in the form `window_start,arrival_intensity`, where the intensity is the count in each window divided by the selected window width. The sampling period is equal to the chosen window width and the normalized intensity is reported in per-hour units when the window is measured in hours.

## CLI

```bash
uv run count-ts-window-selector 12 10 9 11 13 8 14 11 10 9 --candidate-window 5 --candidate-window 10 --candidate-window 15
```

The package is intended for non-hurdle, Poisson-like count processes. Sparse or zero-heavy data should be addressed with hurdle or zero-inflated models instead.
