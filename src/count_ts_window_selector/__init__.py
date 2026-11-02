from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from typing import Iterable, Sequence

import numpy as np
from scipy import stats

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
