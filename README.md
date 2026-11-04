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
