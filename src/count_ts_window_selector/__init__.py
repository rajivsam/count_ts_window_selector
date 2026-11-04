from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np
from scipy import stats

DEFAULT_DATA_DIR = Path(__file__).resolve().parents[2] / "data"

__all__ = [
    "DataSuitabilityError",
    "CandidateWindowError",
    "NoPoissonCompatibleWindowError",
    "WindowSelectionConfig",
    "check_data_suitability",
    "filter_candidate_windows",
    "aggregate_counts_by_window",
    "compute_fano_factor",
    "compute_fano_confidence_interval",
    "select_time_window",
    "build_daily_window_table",
    "teseda_export",
    "main",
]


class DataSuitabilityError(ValueError):
    """Raised when a series is not suitable for the non-hurdle workflow."""


class CandidateWindowError(ValueError):
    """Raised when user-provided candidate windows do not meet coverage requirements."""


class NoPoissonCompatibleWindowError(ValueError):
    """Raised when no Poisson-compatible time window remains after filtering."""


@dataclass(frozen=True)
class WindowSelectionConfig:
    """Configuration for suitability checks, feature filtering, and interval tests."""

    min_event_rate: float = 5.0
    max_zero_window_proportion: float = 0.1
    min_windows: int = 30
    confidence_level: float = 0.90

    def __post_init__(self) -> None:
        if self.min_event_rate <= 0:
            raise ValueError("min_event_rate must be positive.")
        if not 0.0 <= self.max_zero_window_proportion <= 1.0:
            raise ValueError("max_zero_window_proportion must be between 0 and 1.")
        if self.min_windows <= 0:
            raise ValueError("min_windows must be positive.")
        if not 0.0 < self.confidence_level < 1.0:
            raise ValueError("confidence_level must be between 0 and 1.")


def _coerce_count_series(series: Sequence[float] | Iterable[float] | np.ndarray) -> np.ndarray:
    counts = np.asarray(list(series), dtype=float)
    if counts.ndim == 0:
        counts = counts.reshape(1)
    if counts.size == 0:
        raise ValueError("The count time series is empty.")
    if np.any(~np.isfinite(counts)):
        raise ValueError("The count time series contains NaN or infinite values.")
    if np.any(counts < 0):
        raise ValueError("The count time series contains negative counts; only non-negative counts are supported.")
    return counts


def check_data_suitability(
    series: Sequence[float] | Iterable[float] | np.ndarray,
    *,
    min_event_rate: float = 5.0,
    max_zero_window_proportion: float = 0.1,
) -> dict[str, float]:
    """Validate that the series is dense enough for a non-hurdle Poisson workflow.

    The method uses the raw observation interval as the zero-window scale before any
    window aggregation is evaluated. This is the required first suitability screen.
    """

    counts = _coerce_count_series(series)
    observed_event_rate = float(np.mean(counts))
    zero_window_proportion = float(np.mean(counts == 0))

    if observed_event_rate < min_event_rate:
        raise DataSuitabilityError(
            "Observed event rate "
            f"({observed_event_rate:.3f}) is below min_event_rate ({min_event_rate}). "
            "The data are too sparse for the current non-hurdle Poisson-based workflow. "
            "Consider sparse, zero-heavy, or hurdle-type count models; hurdle support is forthcoming in this software."
        )

    if zero_window_proportion > max_zero_window_proportion:
        raise DataSuitabilityError(
            "Proportion of zero-count windows "
            f"({zero_window_proportion:.3f}) exceeds max_zero_window_proportion ({max_zero_window_proportion:.3f}). "
            "The data contain an excessive number of zeros for the current non-hurdle Poisson-based workflow. "
            "Consider sparse, zero-heavy, or hurdle-type count models; hurdle support is forthcoming in this software."
        )

    return {
        "observed_event_rate": observed_event_rate,
        "zero_window_proportion": zero_window_proportion,
    }


def filter_candidate_windows(
    series: Sequence[float] | Iterable[float] | np.ndarray,
    candidate_windows: Sequence[int] | Iterable[int],
    *,
    min_windows: int = 30,
) -> list[int]:
    """Keep only candidate window sizes that yield at least min_windows non-overlapping bins."""

    counts = _coerce_count_series(series)
    cleaned = []
    for candidate in candidate_windows:
        window_size = int(candidate)
        if window_size <= 0:
            continue
        if len(counts) // window_size >= min_windows:
            cleaned.append(window_size)

    if not cleaned:
        raise CandidateWindowError(
            f"Requested windows are too long for the available data length ({len(counts)} observations). "
            f"The package requires at least {min_windows} non-overlapping windows for each candidate."
        )

    return sorted(set(cleaned))


def aggregate_counts_by_window(
    series: Sequence[float] | Iterable[float] | np.ndarray,
    window_size: int,
) -> np.ndarray:
    """Aggregate a count series into non-overlapping windows of the requested size."""

    counts = _coerce_count_series(series)
    if window_size <= 0:
        raise ValueError("window_size must be a positive integer.")
    if len(counts) < window_size:
        raise CandidateWindowError(
            f"Window size {window_size} exceeds the data length ({len(counts)} observations)."
        )

    n_windows = len(counts) // window_size
    if n_windows < 1:
        raise CandidateWindowError(
            f"Window size {window_size} leaves no full non-overlapping windows for the input series."
        )

    truncated = counts[: n_windows * window_size]
    grouped = truncated.reshape(n_windows, window_size)
    return grouped.sum(axis=1)


def compute_fano_factor(windowed_counts: Sequence[float] | np.ndarray) -> dict[str, float]:
    """Return the sample Fano factor and the associated mean and variance."""

    counts = np.asarray(windowed_counts, dtype=float).reshape(-1)
    if counts.size == 0:
        raise ValueError("The aggregated count window is empty.")
    if counts.size < 2:
        raise ValueError("At least two aggregated window counts are required for a Fano-factor calculation.")
    if np.any(counts < 0):
        raise ValueError("Aggregated window counts must be non-negative.")

    mean_count = float(np.mean(counts))
    if mean_count <= 0:
        raise ValueError("The mean aggregated count must be positive for a Fano-factor analysis.")

    variance = float(np.var(counts, ddof=1))
    fano_factor = variance / mean_count
    return {
        "mean_count": mean_count,
        "variance": variance,
        "fano_factor": fano_factor,
    }


def compute_fano_confidence_interval(
    windowed_counts: Sequence[float] | np.ndarray,
    *,
    confidence_level: float = 0.90,
) -> dict[str, float]:
    """Compute chi-square-based Fano bounds for a Poisson reference around 1."""

    counts = np.asarray(windowed_counts, dtype=float).reshape(-1)
    if counts.size < 2:
        raise ValueError("At least two aggregated window counts are required for a confidence interval.")

    alpha = 1.0 - confidence_level
    degrees_of_freedom = counts.size - 1
    if degrees_of_freedom <= 0:
        raise ValueError("The number of aggregated windows must exceed 1 to compute a confidence interval.")

    lower_quantile = stats.chi2.ppf(alpha / 2.0, degrees_of_freedom)
    upper_quantile = stats.chi2.ppf(1.0 - alpha / 2.0, degrees_of_freedom)
    lower_bound = float(lower_quantile / degrees_of_freedom)
    upper_bound = float(upper_quantile / degrees_of_freedom)
    return {
        "fano_lower": lower_bound,
        "fano_upper": upper_bound,
    }


def select_time_window(
    series: Sequence[float] | Iterable[float] | np.ndarray,
    candidate_windows: Sequence[int] | Iterable[int],
    *,
    config: WindowSelectionConfig | None = None,
) -> list[dict[str, float | int]]:
    """Validate the series, filter candidate windows, and return Poisson-compatible windows.

    The result is a list of dictionaries with keys: window_size, fano_factor,
    fano_lower, and fano_upper.
    """

    if config is None:
        config = WindowSelectionConfig()

    counts = _coerce_count_series(series)
    check_data_suitability(
        counts,
        min_event_rate=config.min_event_rate,
        max_zero_window_proportion=config.max_zero_window_proportion,
    )

    valid_windows = filter_candidate_windows(
        counts,
        candidate_windows,
        min_windows=config.min_windows,
    )

    acceptable_windows: list[dict[str, float | int]] = []
    for window_size in valid_windows:
        aggregated = aggregate_counts_by_window(counts, window_size)
        fano_summary = compute_fano_factor(aggregated)
        interval = compute_fano_confidence_interval(
            aggregated,
            confidence_level=config.confidence_level,
        )
        fano_value = float(fano_summary["fano_factor"])
        fano_lower = float(interval["fano_lower"])
        fano_upper = float(interval["fano_upper"])

        if fano_lower <= fano_value <= fano_upper:
            acceptable_windows.append(
                {
                    "window_size": int(window_size),
                    "fano_factor": fano_value,
                    "fano_lower": fano_lower,
                    "fano_upper": fano_upper,
                }
            )

    return acceptable_windows


def _resolve_data_file(
    data_file: str | Path | None,
    *,
    data_dir: str | Path | None = None,
) -> Path:
    """Resolve a dataset path, defaulting to the repository data directory when possible."""

    base_dir = Path(data_dir) if data_dir is not None else DEFAULT_DATA_DIR
    if data_file is None:
        if base_dir.exists() and base_dir.is_dir():
            csv_files = sorted(base_dir.glob("*.csv"))
            if not csv_files:
                raise FileNotFoundError(f"No CSV files were found in {base_dir}.")
            if len(csv_files) > 1:
                raise ValueError(
                    "Multiple CSV files were found in the default data directory. "
                    "Please pass data_file explicitly."
                )
            return csv_files[0]
        if base_dir.exists() and base_dir.is_file():
            return base_dir
        raise FileNotFoundError(f"No dataset file found at {base_dir}.")

    resolved = Path(data_file)
    if resolved.is_dir():
        csv_files = sorted(resolved.glob("*.csv"))
        if not csv_files:
            raise FileNotFoundError(f"No CSV files were found in {resolved}.")
        if len(csv_files) > 1:
            raise ValueError(
                f"Multiple CSV files were found in {resolved}. Please select a single file to export."
            )
        return csv_files[0]

    return resolved


def _parse_timestamp(raw_value: str) -> datetime:
    """Parse ISO-like timestamps drawn from the repository datasets."""

    text = str(raw_value).strip()
    if not text:
        raise ValueError("Encountered an empty timestamp value.")

    if text.endswith("Z"):
        text = text[:-1] + "+00:00"

    if "." in text:
        base, fraction = text.rsplit(".", 1)
        if len(fraction) > 6 and fraction[-1].isdigit():
            text = base + "." + fraction[:6]

    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass

    for fmt in (
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
    ):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue

    raise ValueError(f"Could not parse timestamp value: {raw_value!r}")


def _load_event_timestamps(
    data_file: str | Path | None = None,
    *,
    data_dir: str | Path | None = None,
    timestamp_column: str = "timestamp",
) -> list[datetime]:
    """Load a timestamp column from a CSV file."""

    path = _resolve_data_file(data_file, data_dir=data_dir)
    with path.open("r", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"The file {path} does not appear to contain a CSV header row.")

        if timestamp_column not in reader.fieldnames:
            if len(reader.fieldnames) == 1 and reader.fieldnames[0].strip().lower() == timestamp_column:
                values = [row[reader.fieldnames[0]] for row in reader]
            else:
                raise ValueError(
                    f"Expected a '{timestamp_column}' column in {path}, found {reader.fieldnames}."
                )
        else:
            values = [row[timestamp_column] for row in reader]

    timestamps = [_parse_timestamp(value) for value in values if str(value).strip()]
    if not timestamps:
        raise ValueError(f"No valid timestamps were found in {path}.")

    return sorted(timestamps)


def _find_full_day_bounds(timestamps: Sequence[datetime]) -> tuple[date, date, dict[str, object | None]]:
    """Return the first and last full calendar days in the data and any omitted partial days."""

    first_ts = timestamps[0]
    last_ts = timestamps[-1]

    first_day = first_ts.date()
    last_day = last_ts.date()

    first_full_day = first_day
    if first_ts.time() != time(0, 0):
        first_full_day = first_day + timedelta(days=1)

    last_full_day = last_day
    if last_ts.time() != time(23, 59, 59, 999999):
        last_full_day = last_day - timedelta(days=1)

    omits: dict[str, object | None] = {
        "prefix": None,
        "suffix": None,
    }
    if first_full_day != first_day:
        start_value = datetime.combine(first_day, time.min)
        end_value = datetime.combine(first_day, time.max)
        omits["prefix"] = {
            "start": start_value.isoformat(timespec="microseconds"),
            "end": end_value.isoformat(timespec="microseconds"),
        }
    if last_full_day != last_day:
        start_value = datetime.combine(last_day, time.min)
        end_value = datetime.combine(last_day, time.max)
        omits["suffix"] = {
            "start": start_value.isoformat(timespec="microseconds"),
            "end": end_value.isoformat(timespec="microseconds"),
        }

    return first_full_day, last_full_day, omits


def build_daily_window_table(
    data_file: str | Path | None = None,
    *,
    window_size_hours: int = 1,
    data_dir: str | Path | None = None,
    timestamp_column: str = "timestamp",
    output_path: str | Path | None = None,
) -> dict[str, object]:
    """Build a daily tabular export with one row per day and one column per time-window block.

    The method starts at the first full calendar day and ends at the last full calendar day.
    Any leading or trailing partial days are omitted and reported in the returned metadata.
    """

    if window_size_hours <= 0:
        raise ValueError("window_size_hours must be a positive integer.")
    if 24 % window_size_hours != 0:
        raise ValueError("window_size_hours must divide evenly into 24 hours to form a full day table.")

    source_path = _resolve_data_file(data_file, data_dir=data_dir)
    timestamps = _load_event_timestamps(source_path, timestamp_column=timestamp_column)
    first_full_day, last_full_day, omitted = _find_full_day_bounds(timestamps)

    if first_full_day > last_full_day:
        raise ValueError(
            "The dataset does not contain a full day of timestamps after trimming partial calendar days. "
            "Please provide a longer observation period."
        )

    windows_per_day = 24 // window_size_hours
    day_totals: dict[date, list[int]] = {
        day: [0 for _ in range(windows_per_day)] for day in (first_full_day + timedelta(days=i) for i in range((last_full_day - first_full_day).days + 1))
    }

    for ts in timestamps:
        current_day = ts.date()
        if current_day < first_full_day or current_day > last_full_day:
            continue
        day_start = datetime.combine(current_day, time.min)
        elapsed_seconds = (ts - day_start).total_seconds()
        window_index = int(elapsed_seconds // (window_size_hours * 3600))
        if 0 <= window_index < windows_per_day:
            day_totals[current_day][window_index] += 1

    columns = [f"window_{idx:02d}" for idx in range(windows_per_day)]
    rows: list[dict[str, object]] = []
    for day in [first_full_day + timedelta(days=i) for i in range((last_full_day - first_full_day).days + 1)]:
        row: dict[str, object] = {"date": day.isoformat()}
        for idx, value in enumerate(day_totals[day]):
            row[f"window_{idx:02d}"] = int(value)
        rows.append(row)

    if output_path is None:
        output_path = source_path.parent / f"{source_path.stem}_daily_window_{window_size_hours}h.csv"
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["date", *columns])
        for row in rows:
            writer.writerow([row["date"], *[row[column] for column in columns]])

    return {
        "source_file": str(source_path),
        "output_file": str(output_path),
        "window_size_hours": int(window_size_hours),
        "windows_per_day": int(windows_per_day),
        "first_full_day": first_full_day.isoformat(),
        "last_full_day": last_full_day.isoformat(),
        "omitted_prefix": omitted.get("prefix"),
        "omitted_suffix": omitted.get("suffix"),
        "rows": rows,
        "columns": columns,
    }


def teseda_export(
    data_file: str | Path | None = None,
    *,
    window_size_hours: int = 1,
    data_dir: str | Path | None = None,
    timestamp_column: str = "timestamp",
    output_path: str | Path | None = None,
    intensity_unit: str | None = None,
) -> dict[str, object]:
    """Create a two-column TSEDA-ready export: window_start and arrival_intensity.

    The arrival intensity is normalized to a per-hour rate: count / window_size_hours.
    The output includes the sampling period (the selected window width) and the intensity unit.
    """

    if window_size_hours <= 0:
        raise ValueError("window_size_hours must be a positive integer.")
    if 24 % window_size_hours != 0:
        raise ValueError("window_size_hours must divide evenly into 24 hours to produce a regular daily grid.")

    source_path = _resolve_data_file(data_file, data_dir=data_dir)
    timestamps = _load_event_timestamps(source_path, timestamp_column=timestamp_column)
    first_full_day, last_full_day, omitted = _find_full_day_bounds(timestamps)

    if first_full_day > last_full_day:
        raise ValueError(
            "The dataset does not contain a full day of timestamps after trimming partial calendar days. "
            "Please provide a longer observation period."
        )

    windows_per_day = 24 // window_size_hours
    day_range = [first_full_day + timedelta(days=i) for i in range((last_full_day - first_full_day).days + 1)]
    rows: list[dict[str, object]] = []

    for day in day_range:
        day_start = datetime.combine(day, time.min)
        for idx in range(windows_per_day):
            window_start = day_start + timedelta(hours=idx * window_size_hours)
            window_end = window_start + timedelta(hours=window_size_hours)
            count = 0
            for ts in timestamps:
                if ts.date() != day:
                    continue
                if window_start <= ts < window_end:
                    count += 1
            intensity = count / float(window_size_hours)
            rows.append({
                "window_start": window_start.isoformat(timespec="seconds"),
                "arrival_intensity": float(intensity),
            })

    if output_path is None:
        output_path = source_path.parent / f"{source_path.stem}_teseda_{window_size_hours}h.csv"
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["window_start", "arrival_intensity"])
        for row in rows:
            writer.writerow([row["window_start"], row["arrival_intensity"]])

    if intensity_unit is None:
        intensity_unit = "arrivals per hour"

    return {
        "source_file": str(source_path),
        "output_file": str(output_path),
        "sampling_period_hours": int(window_size_hours),
        "sampling_period": f"{window_size_hours} hours",
        "intensity_unit": intensity_unit,
        "first_full_day": first_full_day.isoformat(),
        "last_full_day": last_full_day.isoformat(),
        "omitted_prefix": omitted.get("prefix"),
        "omitted_suffix": omitted.get("suffix"),
        "rows": rows,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Select Poisson-compatible time windows for higher-event-rate count time series."
    )
    parser.add_argument("counts", nargs="+", type=float, help="Observed count values in the time series.")
    parser.add_argument(
        "--candidate-window",
        dest="candidate_windows",
        type=int,
        action="append",
        default=None,
        help="Candidate window size to test. Repeat the flag to test multiple windows.",
    )
    parser.add_argument("--min-event-rate", type=float, default=5.0, help="Minimum average event rate required.")
    parser.add_argument(
        "--max-zero-window-proportion",
        type=float,
        default=0.1,
        help="Maximum allowed proportion of zero-count windows.",
    )
    parser.add_argument("--min-windows", type=int, default=30, help="Minimum number of non-overlapping windows required.")
    parser.add_argument(
        "--confidence-level",
        type=float,
        default=0.90,
        help="Confidence level used for Fano-factor acceptance bounds.",
    )

    args = parser.parse_args(argv)
    config = WindowSelectionConfig(
        min_event_rate=args.min_event_rate,
        max_zero_window_proportion=args.max_zero_window_proportion,
        min_windows=args.min_windows,
        confidence_level=args.confidence_level,
    )

    candidate_windows = args.candidate_windows or [1, 2, 3, 5, 10, 15, 30, 60]

    try:
        results = select_time_window(args.counts, candidate_windows, config=config)
    except (DataSuitabilityError, CandidateWindowError, ValueError) as exc:
        parser.exit(1, f"Error: {exc}\n")

    if not results:
        print("No candidate window satisfied the Poisson-compatible Fano criterion.")
        return 0

    print(json.dumps(results, indent=2))
    return 0
