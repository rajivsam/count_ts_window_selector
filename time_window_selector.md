# Selecting Time Windows for Count Time Series

## Scope of the Current Iteration

The current iteration of this project is limited to higher-event-rate time series, where the relevant observation window is identified using the Fano factor. This workflow is intended for count data that are sufficiently dense for Poisson-like or near-Poisson behavior to be informative after the required suitability checks.

For low-event-rate or zero-heavy time series, the appropriate approach is not Fano-factor-based window selection. Instead, such data are better handled with hurdle-based or zero-inflated count models. That hurdle-based workflow is a separate methodological track and is expected to follow in a later implementation.

## Overview

Count time series arise in many operational settings, including hospital admissions, emergency response demand, and retail transactions. In these applications, planning depends on estimating the expected number of events over a relevant time window. A key challenge is that real-world event processes are often non-homogeneous: the arrival rate changes over time, so a single fixed interval may not be appropriate.

This project proposes a statistical method for identifying an appropriate observation window for count data, using the Fano factor as the primary diagnostic. The goal is to estimate the time scale over which the process can be treated as approximately Poisson with a stable arrival rate.

## Problem Statement

Many planning decisions require forecasts based on counts aggregated over time. For example:

- Hospital operations need estimates of daily admissions, ICU usage, and surgical demand.
- Public safety planning depends on emergency call volumes by shift or planning period.
- Retail demand forecasting often deals with low-count, weekly sales patterns where standard continuous-time methods perform poorly.

For these problems, the arrival rate may vary across time, and a naive model can misrepresent the data. The critical task is therefore to determine a window length for which the process is locally stationary and suitable for Poisson-based analysis.

## Project Objective

This project will develop and apply a method for selecting the window length of a count time series using the sample Fano factor:

\[
F = \frac{S^2}{\bar{N}}
\]

where \(\bar{N}\) is the mean count and \(S^2\) is the variance over the chosen interval. Under a Poisson process, the variance equals the mean, so the Fano factor is approximately 1. Deviations from 1 indicate underdispersion, overdispersion, or rate changes across scales.

## Methodology

We will evaluate candidate window lengths and compute the Fano factor for each. The selected window is the scale at which the process displays approximately Poisson behavior without evidence of strong non-stationarity or burstiness.

The proposed workflow is:

1. Assemble a count time series for the application domain.
2. Define a set of candidate window lengths consistent with the planning horizon and reporting schedule.
3. Aggregate the raw counts at each window size.
4. Compute the mean and variance of counts within each window.
5. Estimate the Fano factor across scales.
6. Identify the window where the Fano factor approaches 1 and remains stable over adjacent intervals.
7. Use that window for downstream Poisson or near-Poisson modeling.

This approach treats the Fano factor as a scale-dependent diagnostic rather than a single binary test. It helps distinguish short-range dependence from long-range drift in the underlying arrival process.

## Data Suitability Requirement

Before fitting or evaluating any Fano-factor-based window-selection procedure, the software must first screen the input count series for sparsity and zero inflation. In particular, we define two configurable parameters:

- `min_event_rate`: the minimum average event rate, with a default value of 5 events per observation interval.
- `max_zero_window_proportion`: the maximum allowed proportion of windows with zero counts, with a default value of 0.1 (10%).

These thresholds are intended to filter out series that are too sparse to be treated as standard Poisson-like count data and that are likely better handled by hurdle or zero-inflated models.

The sequence of checks should be:

1. Read the count data.
2. Compute the observed event rate for the series.
3. Compare it against the configured minimum event rate threshold.
4. Compute the proportion of windows with zero counts over the relevant aggregation scale.
5. Compare that zero-count proportion against `max_zero_window_proportion`.
6. If either condition fails, stop immediately and emit an informative error message that explicitly identifies which condition failed.

The error message should clearly explain whether the failure was due to: (a) the observed event rate being below `min_event_rate`, or (b) the proportion of zero-count windows exceeding `max_zero_window_proportion`. It should also explain that the data contain an excessive number of zeros or are too sparse for the current non-hurdle Poisson-based workflow. It should advise the user to consider packages or models designed for sparse, zero-heavy, or hurdle-type count data, and note that support for hurdle models is forthcoming in this software.

This screening check is the first validation step performed by the package after reading the input data. It must occur before any Fano-factor, variance, or window-selection calculations, because those diagnostics are not meaningful for series that are structurally dominated by zero inflation or hurdle behavior.

## Minimum Window Coverage Requirement

A second independent validation step should be applied to the candidate window lengths before any Fano-factor analysis is performed. The package should accept a user-specified list of candidate windows and then filter this set so that it retains only windows whose resulting aggregation yields at least a minimum number of windows, defined as `min_windows = 30`.

The procedure should be:

1. Receive the list of candidate window sizes to evaluate.
2. For each candidate window, determine how many non-overlapping windows it produces for the dataset.
3. Discard any candidate window that would leave fewer than 30 windows in the series.
4. If no candidate window satisfies the minimum-window requirement, stop with an informative error explaining that the requested windows are too long for the available data length.
5. Continue with the filtered set of candidate windows only.

This requirement ensures that the Fano-factor estimates are based on enough aggregated observations to be stable and statistically informative. The default minimum is 30 windows, and the threshold should be configurable for users who want a stricter or more relaxed rule.

## Processing Logic Requirement

The package should process an input time series of arrivals in the following order:

1. Check whether the time series is suitable for analysis with this package using the data-suitability tests described above.
2. If the data pass the suitability checks, determine the set of acceptable candidate windows from the user-provided window list by applying the minimum-window coverage rule (`min_windows = 30`).
3. For each acceptable candidate window, aggregate the arrival counts over that window and compute the Fano factor.
4. Compare the observed Fano factor against the Poisson reference value of 1 using a configurable confidence interval based on the chi-square distribution.
5. If a candidate window exists for which the Fano factor falls within the configured acceptable confidence interval around 1, treat that window as a valid Poisson-compatible window.
6. Report the acceptable windows and, for each such window, include:
   - the window size,
   - the Fano factor,
   - `fano_lower`, and
   - `fano_upper`, computed from the chi-square-based confidence limits.
7. If no candidate window satisfies the Poisson-within-confidence-interval criterion, report that no window could be found.

The confidence interval should be configurable by the user and expressed as a probability such as 0.90 for a 90% confidence interval. The package should use the configured confidence level to define the lower and upper Fano-factor bounds for acceptance. If the Fano factor is inside this interval, the window is considered statistically consistent with Poisson behavior under the chosen confidence level.

This processing flow defines the intended package behavior for higher-event-rate arrival time series. It is not intended for low-event-rate or hurdle-type series, which are handled by separate modeling approaches.

## Expected Contribution

The project will provide a practical, statistically grounded method for selecting time windows in non-homogeneous count processes. By identifying the window where the process is approximately stationary and Poisson-like, the method will improve planning models for operational systems with event-count data.

## Expected Outcomes

- A reproducible procedure for window selection in count time series.
- A data-driven criterion based on dispersion rather than arbitrary calendar choices.
- Improved modeling of non-homogeneous Poisson processes for forecasting and staffing decisions.

## Significance

The proposed method is relevant to any domain where demand is observed as event counts over time. It offers a principled way to move from raw event data to planning-ready models while respecting the scale-dependent nature of variability in real-world processes.

