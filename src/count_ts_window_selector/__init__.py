from __future__ import annotations

import argparse
import csv
import json
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
from scipy import stats

DEFAULT_DATA_DIR = Path(__file__).resolve().parents[2] / "data"

__all__ = [
    "DataSuitabilityError",
    "CandidateWindowError",
    "NoPoissonCompatibleWindowError",
    "HorizonBoundaryError",
    "WindowSelectionConfig",
    "HorizonRecommendationConfig",
    "check_data_suitability",
    "filter_candidate_windows",
    "aggregate_counts_by_window",
    "compute_fano_factor",
    "compute_fano_confidence_interval",
    "select_time_window",
    "extract_horizon",
    "evaluate_horizon",
    "tune_horizon_windows",
    "recommend_optimal_horizon",
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


class HorizonBoundaryError(ValueError):
    """Raised when a requested horizon is not fully covered by the dataset."""


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


@dataclass(frozen=True)
class HorizonRecommendationConfig:
    """Reliability gates and tie-breakers for horizon recommendations."""

    min_valid_horizon_proportion: float = 0.80
    min_valid_horizons: int = 10
    acceptance_rate_tolerance: float = 0.02
    prefer_shorter_horizon: bool = True

    def __post_init__(self) -> None:
        if not 0.0 < self.min_valid_horizon_proportion <= 1.0:
            raise ValueError("min_valid_horizon_proportion must be in (0, 1].")
        if self.min_valid_horizons <= 0:
            raise ValueError("min_valid_horizons must be positive.")
        if self.acceptance_rate_tolerance < 0:
            raise ValueError("acceptance_rate_tolerance must be non-negative.")


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
        try:
            window_size = int(candidate)
        except (TypeError, ValueError) as exc:
            raise CandidateWindowError(
                f"Candidate micro-window {candidate!r} is not an integer."
            ) from exc
        if isinstance(candidate, bool) or float(candidate) != window_size:
            raise CandidateWindowError(
                f"Candidate micro-window {candidate!r} is not an integer."
            )
        if window_size <= 0:
            raise CandidateWindowError(
                f"Candidate micro-window {candidate!r} must be a positive integer."
            )
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


_DURATION_PATTERN = re.compile(
    r"^\s*(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>d(?:ays?)?|h(?:ours?)?|m(?:in(?:utes?)?)?|s(?:ec(?:onds?)?)?)?\s*$",
    re.IGNORECASE,
)


def _coerce_datetime(value: datetime | date | str, *, parameter: str) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, time.min)
    if not isinstance(value, str):
        raise TypeError(f"{parameter} must be a datetime, date, or ISO-8601 string.")
    return _parse_timestamp(value)


def _coerce_duration(
    value: timedelta | int | float | str,
    *,
    parameter: str,
    numeric_unit: str,
) -> timedelta:
    if isinstance(value, timedelta):
        result = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        multiplier = {"days": 86400.0, "hours": 3600.0, "minutes": 60.0, "seconds": 1.0}[numeric_unit]
        result = timedelta(seconds=float(value) * multiplier)
    elif isinstance(value, str):
        match = _DURATION_PATTERN.fullmatch(value)
        if match is None:
            raise ValueError(
                f"{parameter} must be a timedelta or a value such as '14 days', '2h', or '30m'."
            )
        amount = float(match.group("value"))
        unit_text = (match.group("unit") or numeric_unit).lower()
        if unit_text.startswith("d"):
            result = timedelta(days=amount)
        elif unit_text.startswith("h"):
            result = timedelta(hours=amount)
        elif unit_text.startswith("m"):
            result = timedelta(minutes=amount)
        else:
            result = timedelta(seconds=amount)
    else:
        raise TypeError(f"{parameter} must be a timedelta, number, or duration string.")

    if result <= timedelta(0):
        raise ValueError(f"{parameter} must be positive.")
    return result


def _iso_datetime(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _load_temporal_records(
    data: str | Path | Sequence[datetime | Mapping[str, Any]] | Iterable[datetime | Mapping[str, Any]],
    *,
    datetime_column: str,
    count_column: str | None,
) -> tuple[list[datetime], list[float | None]]:
    if isinstance(data, (str, Path)):
        path = _resolve_data_file(data)
        with path.open("r", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None:
                raise ValueError(f"The file {path} does not appear to contain a CSV header row.")
            if datetime_column not in reader.fieldnames:
                raise ValueError(
                    f"Expected a '{datetime_column}' column in {path}, found {reader.fieldnames}."
                )
            if count_column is not None and count_column not in reader.fieldnames:
                raise ValueError(
                    f"Expected a '{count_column}' column in {path}, found {reader.fieldnames}."
                )
            raw_records: list[datetime | Mapping[str, Any]] = list(reader)
    else:
        raw_records = list(data)

    timestamps: list[datetime] = []
    values: list[float | None] = []
    for record in raw_records:
        if isinstance(record, datetime):
            raw_timestamp = record
            raw_count = None
        elif isinstance(record, Mapping):
            if datetime_column not in record:
                raise ValueError(f"Record is missing the '{datetime_column}' datetime column.")
            raw_timestamp = record[datetime_column]
            raw_count = record.get(count_column) if count_column is not None else None
            if count_column is not None and raw_count is None:
                raise ValueError(f"Record is missing the '{count_column}' count column.")
        else:
            raise TypeError("Temporal data must contain datetime values or mappings with a datetime column.")

        timestamp = _coerce_datetime(raw_timestamp, parameter=datetime_column)
        timestamps.append(timestamp)
        values.append(float(raw_count) if raw_count is not None else None)

    if not timestamps:
        raise ValueError("The temporal dataset is empty.")
    awareness = [timestamp.utcoffset() is not None for timestamp in timestamps]
    if any(awareness) and not all(awareness):
        raise ValueError("The datetime column mixes timezone-aware and timezone-naive values.")

    ordered = sorted(zip(timestamps, values), key=lambda item: item[0])
    return [item[0] for item in ordered], [item[1] for item in ordered]


def extract_horizon(
    data: str | Path | Sequence[datetime | Mapping[str, Any]] | Iterable[datetime | Mapping[str, Any]],
    *,
    start_date: datetime | date | str | None = None,
    horizon_window: timedelta | int | float | str | None = None,
    datetime_column: str = "timestamp",
    count_column: str | None = None,
    base_resolution: timedelta | int | float | str = "1h",
    boundary_policy: str = "error",
) -> dict[str, Any]:
    """Extract a complete half-open horizon and aggregate it to a regular count grid."""

    if boundary_policy not in {"error", "truncate"}:
        raise ValueError("boundary_policy must be either 'error' or 'truncate'.")

    resolution = _coerce_duration(
        base_resolution,
        parameter="base_resolution",
        numeric_unit="hours",
    )
    timestamps, supplied_counts = _load_temporal_records(
        data,
        datetime_column=datetime_column,
        count_column=count_column,
    )
    source_start = timestamps[0]
    source_end = timestamps[-1]
    requested_start = source_start if start_date is None else _coerce_datetime(
        start_date,
        parameter="start_date",
    )
    horizon = (
        None
        if horizon_window is None
        else _coerce_duration(horizon_window, parameter="horizon_window", numeric_unit="days")
    )
    requested_end = source_end + resolution if horizon is None else requested_start + horizon

    starts_before_source = requested_start < source_start
    ends_after_source = requested_end > (source_end + resolution if horizon is None else source_end)
    if (starts_before_source or ends_after_source) and boundary_policy == "error":
        raise HorizonBoundaryError(
            f"Requested horizon [{_iso_datetime(requested_start)}, {_iso_datetime(requested_end)}) "
            f"is not fully covered by the available bounds [{_iso_datetime(source_start)}, "
            f"{_iso_datetime(source_end)}]."
        )

    actual_start = max(requested_start, source_start)
    available_end = source_end + resolution if horizon is None else source_end
    actual_end = min(requested_end, available_end)
    if actual_end <= actual_start:
        raise HorizonBoundaryError("The requested horizon does not overlap the available dataset.")

    available_seconds = (actual_end - actual_start).total_seconds()
    resolution_seconds = resolution.total_seconds()
    complete_intervals = int(available_seconds // resolution_seconds)
    remainder_seconds = available_seconds - complete_intervals * resolution_seconds
    aggregated_end = actual_start + timedelta(seconds=complete_intervals * resolution_seconds)
    if complete_intervals < 1:
        raise HorizonBoundaryError(
            "The requested horizon contains fewer than one complete base-resolution interval."
        )

    counts = np.zeros(complete_intervals, dtype=float)
    selected_event_count = 0
    for timestamp, supplied_count in zip(timestamps, supplied_counts):
        if not (actual_start <= timestamp < aggregated_end):
            continue
        index = int((timestamp - actual_start).total_seconds() // resolution_seconds)
        if index >= complete_intervals:
            continue
        increment = 1.0 if supplied_count is None else supplied_count
        if not np.isfinite(increment) or increment < 0:
            raise ValueError("Count values must be finite and non-negative.")
        counts[index] += increment
        selected_event_count += 1 if supplied_count is None else int(supplied_count)

    boundary_metadata = {
        "requested_start": _iso_datetime(requested_start),
        "requested_end": _iso_datetime(requested_end),
        "actual_start": _iso_datetime(actual_start),
        "actual_end": _iso_datetime(aggregated_end),
        "source_start": _iso_datetime(source_start),
        "source_end": _iso_datetime(source_end),
        "is_complete": not (starts_before_source or ends_after_source),
        "truncated_prefix": (
            _iso_datetime(source_start)
            if starts_before_source and boundary_policy == "truncate"
            else None
        ),
        "truncated_suffix": (
            _iso_datetime(aggregated_end)
            if ends_after_source and boundary_policy == "truncate"
            else None
        ),
        "remainder_seconds": remainder_seconds,
    }
    interval_starts = [
        actual_start + timedelta(seconds=index * resolution_seconds)
        for index in range(complete_intervals)
    ]
    return {
        "start_date": boundary_metadata["actual_start"],
        "end_date": boundary_metadata["actual_end"],
        "requested_start_date": boundary_metadata["requested_start"],
        "requested_end_date": boundary_metadata["requested_end"],
        "horizon_window": (
            str(horizon) if horizon is not None else str(aggregated_end - actual_start)
        ),
        "base_resolution": str(resolution),
        "base_resolution_seconds": resolution_seconds,
        "is_complete": boundary_metadata["is_complete"],
        "boundary_metadata": boundary_metadata,
        "number_of_events": selected_event_count,
        "number_of_base_intervals": complete_intervals,
        "interval_starts": interval_starts,
        "counts": counts.tolist(),
    }


def evaluate_horizon(
    data: str | Path | Sequence[datetime | Mapping[str, Any]] | Iterable[datetime | Mapping[str, Any]],
    candidate_windows: Sequence[int] | Iterable[int],
    *,
    start_date: datetime | date | str | None = None,
    horizon_window: timedelta | int | float | str | None = None,
    datetime_column: str = "timestamp",
    count_column: str | None = None,
    base_resolution: timedelta | int | float | str = "1h",
    boundary_policy: str = "error",
    config: WindowSelectionConfig | None = None,
) -> dict[str, Any]:
    """Evaluate candidate micro-windows within one temporal macro-horizon."""

    if config is None:
        config = WindowSelectionConfig()
    horizon = extract_horizon(
        data,
        start_date=start_date,
        horizon_window=horizon_window,
        datetime_column=datetime_column,
        count_column=count_column,
        base_resolution=base_resolution,
        boundary_policy=boundary_policy,
    )
    counts = np.asarray(horizon["counts"], dtype=float)
    suitability = check_data_suitability(
        counts,
        min_event_rate=config.min_event_rate,
        max_zero_window_proportion=config.max_zero_window_proportion,
    )
    requested_candidates: list[int] = []
    for candidate in candidate_windows:
        try:
            window_size = int(candidate)
        except (TypeError, ValueError) as exc:
            raise CandidateWindowError(
                f"Candidate micro-window {candidate!r} is not an integer."
            ) from exc
        if isinstance(candidate, bool) or float(candidate) != window_size or window_size <= 0:
            raise CandidateWindowError(
                f"Candidate micro-window {candidate!r} must be a positive integer."
            )
        requested_candidates.append(window_size)

    valid_candidates = filter_candidate_windows(
        counts,
        requested_candidates,
        min_windows=config.min_windows,
    )
    valid_set = set(valid_candidates)
    rejected_candidates = [
        {
            "window_size": candidate,
            "number_of_windows": len(counts) // candidate,
            "required_windows": config.min_windows,
            "reason": "insufficient_coverage",
        }
        for candidate in sorted(set(requested_candidates))
        if candidate not in valid_set
    ]

    candidates: list[dict[str, Any]] = []
    for window_size in valid_candidates:
        aggregated = aggregate_counts_by_window(counts, window_size)
        fano = compute_fano_factor(aggregated)
        interval = compute_fano_confidence_interval(
            aggregated,
            confidence_level=config.confidence_level,
        )
        lower = float(interval["fano_lower"])
        upper = float(interval["fano_upper"])
        fano_value = float(fano["fano_factor"])
        accepted = lower <= fano_value <= upper
        candidates.append(
            {
                "window_size": int(window_size),
                "number_of_windows": int(len(aggregated)),
                "mean_count": float(fano["mean_count"]),
                "sample_variance": float(fano["variance"]),
                "variance": float(fano["variance"]),
                "fano_factor": fano_value,
                "fano_lower": lower,
                "fano_upper": upper,
                "confidence_band_width": upper - lower,
                "confidence_level": config.confidence_level,
                "accepted": accepted,
            }
        )

    accepted_candidates = [candidate for candidate in candidates if candidate["accepted"]]
    return {
        **horizon,
        "observed_event_rate": suitability["observed_event_rate"],
        "zero_window_proportion": suitability["zero_window_proportion"],
        "suitability_status": "suitable",
        "suitability_diagnostics": suitability,
        "min_windows": config.min_windows,
        "confidence_level": config.confidence_level,
        "candidate_windows": candidates,
        "valid_candidate_windows": valid_candidates,
        "rejected_candidate_windows": rejected_candidates,
        "accepted_windows": accepted_candidates,
        "poisson_compatible_window_found": bool(accepted_candidates),
    }


def tune_horizon_windows(
    data: str | Path | Sequence[datetime | Mapping[str, Any]] | Iterable[datetime | Mapping[str, Any]],
    candidate_horizons: Sequence[timedelta | int | float | str],
    candidate_windows: Sequence[int] | Iterable[int],
    *,
    step_size: timedelta | int | float | str = "1d",
    datetime_column: str = "timestamp",
    count_column: str | None = None,
    base_resolution: timedelta | int | float | str = "1h",
    boundary_policy: str = "error",
    config: WindowSelectionConfig | None = None,
) -> list[dict[str, Any]]:
    """Compare horizon lengths by repeatedly evaluating complete sliding horizons."""

    if config is None:
        config = WindowSelectionConfig()
    if boundary_policy not in {"error", "truncate"}:
        raise ValueError("boundary_policy must be either 'error' or 'truncate'.")
    timestamps, values = _load_temporal_records(
        data,
        datetime_column=datetime_column,
        count_column=count_column,
    )
    records = [
        (
            timestamp
            if count_column is None
            else {datetime_column: timestamp, count_column: value}
        )
        for timestamp, value in zip(timestamps, values)
    ]
    candidate_micro_windows = list(candidate_windows)
    source_end = timestamps[-1]
    step = _coerce_duration(step_size, parameter="step_size", numeric_unit="days")
    summaries: list[dict[str, Any]] = []

    for raw_horizon in candidate_horizons:
        horizon = _coerce_duration(
            raw_horizon,
            parameter="candidate_horizon",
            numeric_unit="days",
        )
        start = timestamps[0]
        attempted = 0
        suitability_valid = 0
        suitability_failed = 0
        coverage_failed = 0
        accepted_horizons = 0
        confidence_widths: list[float] = []
        dispersion_deviations: list[float] = []
        accepted_results: list[dict[str, Any]] = []

        while start + horizon <= source_end:
            attempted += 1
            try:
                result = evaluate_horizon(
                    records,
                    candidate_micro_windows,
                    start_date=start,
                    horizon_window=horizon,
                    datetime_column=datetime_column,
                    count_column=count_column,
                    base_resolution=base_resolution,
                    boundary_policy=boundary_policy,
                    config=config,
                )
            except DataSuitabilityError:
                suitability_failed += 1
            except CandidateWindowError:
                suitability_valid += 1
                coverage_failed += 1
            else:
                suitability_valid += 1
                if result["poisson_compatible_window_found"]:
                    accepted_horizons += 1
                    accepted_results.append(result)
                for candidate in result["candidate_windows"]:
                    confidence_widths.append(float(candidate["confidence_band_width"]))
                    dispersion_deviations.append(abs(float(candidate["fano_factor"]) - 1.0))
            start += step

        summaries.append(
            {
                "horizon_window": str(horizon),
                "horizon_window_days": horizon.total_seconds() / 86400.0,
                "step_size": str(step),
                "attempted_horizons": attempted,
                "suitability_valid_horizons": suitability_valid,
                "suitability_failed_horizons": suitability_failed,
                "candidate_coverage_failed_horizons": coverage_failed,
                "accepted_horizons": accepted_horizons,
                "poisson_acceptance_rate": (
                    accepted_horizons / suitability_valid if suitability_valid else None
                ),
                "suitability_valid_horizon_proportion": (
                    suitability_valid / attempted if attempted else None
                ),
                "mean_confidence_band_width": (
                    float(np.mean(confidence_widths)) if confidence_widths else None
                ),
                "median_dispersion_deviation": (
                    float(np.median(dispersion_deviations))
                    if dispersion_deviations
                    else None
                ),
                "accepted_results": accepted_results,
                "diagnostics": {
                    "zero_suitability_valid_horizons": suitability_valid == 0,
                    "attempted_but_invalid": attempted == 0,
                },
            }
        )

    return summaries


def recommend_optimal_horizon(
    comparison_table: Sequence[Mapping[str, Any]] | Iterable[Mapping[str, Any]],
    *,
    config: HorizonRecommendationConfig | None = None,
) -> dict[str, Any]:
    """Apply an explicit deterministic rule to a horizon tuning comparison table."""

    if config is None:
        config = HorizonRecommendationConfig()
    candidates = [dict(candidate) for candidate in comparison_table]
    audit: list[dict[str, Any]] = []
    qualified: list[tuple[dict[str, Any], dict[str, Any], float]] = []

    for candidate in candidates:
        acceptance_rate = candidate.get("poisson_acceptance_rate")
        valid_proportion = candidate.get("suitability_valid_horizon_proportion")
        valid_count = candidate.get("suitability_valid_horizons")
        try:
            horizon_days = float(candidate["horizon_window_days"])
        except (KeyError, TypeError, ValueError):
            try:
                horizon_days = (
                    _coerce_duration(
                        candidate["horizon_window"],
                        parameter="horizon_window",
                        numeric_unit="days",
                    ).total_seconds()
                    / 86400.0
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    "Each comparison row must provide horizon_window_days or a valid horizon_window."
                ) from exc

        failures: list[dict[str, Any]] = []
        if acceptance_rate is None:
            failures.append(
                {
                    "gate": "poisson_acceptance_rate_available",
                    "observed": None,
                    "required": "not null",
                }
            )
        if valid_proportion is None or valid_proportion < config.min_valid_horizon_proportion:
            failures.append(
                {
                    "gate": "min_valid_horizon_proportion",
                    "observed": valid_proportion,
                    "required": config.min_valid_horizon_proportion,
                }
            )
        if valid_count is None or valid_count < config.min_valid_horizons:
            failures.append(
                {
                    "gate": "min_valid_horizons",
                    "observed": valid_count,
                    "required": config.min_valid_horizons,
                }
            )

        audit_entry = {
            "horizon_window": candidate.get("horizon_window"),
            "horizon_window_days": horizon_days,
            "outcome": "rejected" if failures else "qualified",
            "stage": "reliability_gate" if failures else None,
            "failures": failures,
            "metrics": {
                "poisson_acceptance_rate": acceptance_rate,
                "suitability_valid_horizon_proportion": valid_proportion,
                "suitability_valid_horizons": valid_count,
                "median_dispersion_deviation": candidate.get(
                    "median_dispersion_deviation"
                ),
                "mean_confidence_band_width": candidate.get(
                    "mean_confidence_band_width"
                ),
            },
        }
        audit.append(audit_entry)
        if not failures:
            qualified.append((candidate, audit_entry, horizon_days))

    if not qualified:
        return {
            "recommended_horizon": None,
            "rule_config": asdict(config),
            "selected_metrics": None,
            "candidate_audit": audit,
            "diagnostics": {
                "status": "no_reliable_candidate",
                "message": "No candidate passed the coverage and sample-size gate.",
            },
        }

    maximum_acceptance = max(
        float(candidate["poisson_acceptance_rate"])
        for candidate, _, _ in qualified
    )
    acceptance_floor = maximum_acceptance - config.acceptance_rate_tolerance
    retained: list[tuple[dict[str, Any], dict[str, Any], float]] = []
    for candidate, audit_entry, horizon_days in qualified:
        acceptance_rate = float(candidate["poisson_acceptance_rate"])
        if acceptance_rate < acceptance_floor:
            audit_entry.update(
                {
                    "outcome": "rejected",
                    "stage": "acceptance_rate_filter",
                    "failures": [
                        {
                            "gate": "acceptance_rate_tolerance",
                            "observed": acceptance_rate,
                            "required": acceptance_floor,
                            "maximum_observed": maximum_acceptance,
                            "tolerance": config.acceptance_rate_tolerance,
                        }
                    ],
                }
            )
        else:
            retained.append((candidate, audit_entry, horizon_days))

    rankable: list[tuple[dict[str, Any], dict[str, Any], float]] = []
    for candidate, audit_entry, horizon_days in retained:
        dispersion = candidate.get("median_dispersion_deviation")
        width = candidate.get("mean_confidence_band_width")
        missing_metrics = [
            name
            for name, value in (
                ("median_dispersion_deviation", dispersion),
                ("mean_confidence_band_width", width),
            )
            if value is None
        ]
        if missing_metrics:
            audit_entry.update(
                {
                    "outcome": "rejected",
                    "stage": "dispersion_precision_tie_break",
                    "failures": [
                        {
                            "gate": "tie_break_metrics_available",
                            "observed": missing_metrics,
                            "required": "not null",
                        }
                    ],
                }
            )
        else:
            rankable.append((candidate, audit_entry, horizon_days))

    if not rankable:
        return {
            "recommended_horizon": None,
            "rule_config": asdict(config),
            "selected_metrics": None,
            "candidate_audit": audit,
            "diagnostics": {
                "status": "no_rankable_candidate",
                "maximum_acceptance_rate": maximum_acceptance,
                "acceptance_rate_floor": acceptance_floor,
            },
        }

    horizon_direction = 1.0 if config.prefer_shorter_horizon else -1.0
    ranked = sorted(
        rankable,
        key=lambda item: (
            float(item[0]["median_dispersion_deviation"]),
            float(item[0]["mean_confidence_band_width"]),
            horizon_direction * item[2],
        ),
    )
    selected, selected_audit, _ = ranked[0]
    selected_audit.update(
        {
            "outcome": "selected",
            "stage": "dispersion_precision_tie_break",
            "failures": [],
        }
    )
    for _, audit_entry, _ in ranked[1:]:
        audit_entry.update(
            {
                "outcome": "runner_up",
                "stage": "dispersion_precision_tie_break",
                "failures": [],
            }
        )

    return {
        "recommended_horizon": selected["horizon_window"],
        "rule_config": asdict(config),
        "selected_metrics": selected,
        "candidate_audit": audit,
        "diagnostics": {
            "status": "recommended",
            "maximum_acceptance_rate": maximum_acceptance,
            "acceptance_rate_floor": acceptance_floor,
            "ranked_candidates": [
                candidate["horizon_window"] for candidate, _, _ in ranked
            ],
        },
    }


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
