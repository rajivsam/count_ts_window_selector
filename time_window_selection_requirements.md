# Selecting Time Windows for Count Time Series

This document is the authoritative requirements source for the project. Other
summaries must be derived from it rather than maintaining a parallel
requirements specification.

## Scope of the Current Iteration

The current iteration of this project is limited to higher-event-rate time series, where relevant observation windows are identified using the sample Fano factor. The workflow is intended for count data that are sufficiently dense for Poisson-like or near-Poisson behavior to be informative after the required suitability checks.

Low-event-rate and zero-heavy series require a different modeling approach, such as hurdle or zero-inflated count models. That workflow remains outside the scope of this iteration.

The system supports two complementary, non-conflicting analytical modes:

1. **Two-Stage Local Evaluation (Default):** Evaluates candidate observation windows within bounded, locally stationary macro-horizons. By separating the macro-horizon used for local stationarity from the micro-window used for count aggregation, the design reduces the risk that seasonal drift or other long-term non-stationarity will inflate sample variance and distort short-term Poisson diagnostics.
2. **Single-Stage Full-Range Evaluation:** Evaluates candidate observation windows across the entire available dataset (`horizon_window = None`) under a single global horizon. This mode selects a window width under which counts within each window follow a homogeneous Poisson process while allowing arrival intensity to vary across consecutive time-ordered windows.

## Overview

Count time series arise in many operational settings, including hospital admissions, emergency response demand, and retail transactions. In these applications, planning depends on estimating the expected number of events over a meaningful time window.

Real-world arrival rates often change over time, so applying a single global interval may not always provide a representative estimate. To address both local non-stationarity and global piecewise-homogeneous modeling, this project provides two workflows built on the same statistical core:

1. **Two-Stage Procedure (Macro-Horizon + Micro-Window):**
   - Select a bounded macro-horizon, `horizon_window`, over which the process is assumed to be locally stationary.
   - Evaluate candidate micro-window widths within that macro-horizon to identify scales that are consistent with Poisson behavior.
2. **Single-Stage Procedure (Full-Range Homogeneous Window):**
   - Evaluate candidate observation window widths directly over the entire dataset range (`horizon_window = None`).
   - Identify a window scale that behaves as a homogeneous Poisson process within individual windows, and arrange those windows in chronological order to observe how arrival intensity varies over time.

The sample Fano factor is the primary diagnostic in both modes:

\[
F = \frac{S^2}{\bar{N}}
\]

where \(\bar{N}\) is the mean count and \(S^2\) is the sample variance. Under a Poisson process, the variance is equal to the mean and the Fano factor is approximately 1.

## Problem Statement

Many planning decisions require forecasts based on counts aggregated over time. Hospital operations may require estimates of admissions and capacity, public safety agencies may plan around call volume by shift, and retailers may forecast demand by operational period.

These processes are often non-homogeneous. Depending on the operational goal and dataset characteristics, analysts need two capabilities:

- **Local Horizon Scoping:** Distinguishing the macro-horizon over which local behavior is estimated from the micro-window whose Poisson compatibility is evaluated, preventing seasonal or long-term drift from inflating variance.
- **Full-Range Piecewise-Homogeneous Modeling:** Selecting a single window width across the entire dataset where each window captures a homogeneous Poisson arrival rate, enabling time-ordered intensity tracking across the full observation span.

## Project Objective

This project will provide a modular, reproducible method for:

1. Extracting a complete, bounded temporal subset—or the full available dataset bounds—from timestamped events or a timestamp-indexed count series.
2. Constructing complete base-resolution counts, including explicit zero-count intervals.
3. Screening the extracted range for suitability before Fano-factor analysis.
4. Filtering candidate window widths using a minimum observation-coverage rule.
5. Computing sample Fano factors and chi-square confidence bands around the Poisson reference value of 1.
6. Supporting both:
   - **Two-stage evaluation:** Repeating the evaluation statelessly across a dataset to calibrate the `horizon_window` hyperparameter; and
   - **Single-stage evaluation:** Selecting a homogeneous Poisson window width directly across the entire dataset range.
7. Exporting planning-ready daily count tables, TSEDA-style intensity series, and chronological arrival-intensity plots.

## Architectural Principles

The analysis must be decomposed into modular, stateless components. Temporal subsetting, data suitability screening, candidate-window evaluation, and reporting must not depend on hidden state from earlier evaluations.

The selector must not retain a current horizon internally:

- For **two-stage analysis**, clients advance `start_date` externally and invoke the same horizon-scoped workflow repeatedly.
- For **single-stage full-range analysis**, clients pass `horizon_window = None` (or invoke the full-range wrapper) to evaluate the entire dataset bounds in a single stateless pass.

The design should distinguish the following terms:

- **Event data:** Individual timestamped arrivals.
- **Count series:** Timestamp-indexed, non-negative counts at a regular base resolution.
- **Base resolution:** The smallest regular sampling interval used to construct the input count series, such as one hour.
- **Candidate micro-window (or candidate window):** A proposed aggregation width expressed as a positive integer number of base-resolution intervals.
- **Macro-horizon:** The bounded interval over which suitability and Fano diagnostics are evaluated.
- **`horizon_window`:** The duration of a macro-horizon, or `None` when evaluating the entire dataset range.
- **Complete horizon:** A horizon whose requested half-open interval is fully covered by the source data.

## Temporal Subset Extraction

### Inputs

The temporal extraction component must accept:

- an event dataset or timestamp-indexed count series;
- the name of the datetime column;
- a `start_date` (optional when `horizon_window` is `None`);
- a `horizon_window` (a positive duration, or `None` for full-range evaluation);
- the base resolution used to construct regular counts; and
- an explicit boundary policy for incomplete horizons.

For count series, a count-column name must also be provided.

### Validation and Ordering

The datetime column must exist, contain valid timestamps, and contain no missing or invalid values. The software must establish a consistent timezone policy: values must be either consistently timezone-aware or consistently timezone-naive within an analysis.

All records must be sorted in ascending chronological order before extraction. Duplicate event timestamps are valid. Duplicate timestamps in a pre-aggregated count series must either be combined according to a documented rule or rejected explicitly.

### Evaluation Interval

For a supplied `start_date` and `horizon_window`, the end of the evaluation interval is:

\[
\text{end\_date} = \text{start\_date} + \text{horizon\_window}
\]

Extraction must use the half-open interval:

\[
[\text{start\_date}, \text{end\_date})
\]

A record at `start_date` is included, and a record at `end_date` is excluded.

### Full-Range Mode (`horizon_window = None`)

When `horizon_window` is `None`, the component evaluates the entire dataset range:

- If `start_date` is omitted, `start_date` defaults to the earliest timestamp in the validated dataset (aligned to the base-resolution grid as configured).
- `end_date` is derived automatically to form a complete right-open interval \([\text{start\_date}, \text{end\_date})\) covering the final observation at the configured base resolution.
- The resulting bounds must be reported explicitly in the evaluation metadata, and the horizon must be marked complete (`is_complete = True`).

### Boundary Completeness

When `horizon_window` is specified, an incomplete horizon must never be silently evaluated. If `start_date` precedes the earliest available timestamp, or `end_date` extends beyond the latest covered timestamp, the component must either:

1. raise a clear boundary error; or
2. truncate the interval and return explicit truncation metadata.

The preferred and default policy is to raise an error. Any truncation policy must report:

- requested start and end;
- actual start and end;
- whether the horizon is complete;
- the omitted or truncated prefix; and
- the omitted or truncated suffix.

### Base-Resolution Aggregation

After extraction, events must be aggregated into a complete, regular count series at the configured base resolution. Intervals with no observed events must be represented as zero counts; zero intervals must not be inferred only from timestamps that happen to appear in the input.

The resulting series must preserve chronological order and contain no negative, missing, or non-finite counts. Metadata should report the base resolution, number of events, number of base intervals, and any remainder that could not form a complete interval.

## Data Suitability Requirement

Before fitting or evaluating any Fano-factor statistic (in either two-stage or single-stage full-range mode), the software must screen the extracted range for sparsity and zero inflation. Two configurable parameters define the screening criteria:

- `min_event_rate`: the minimum mean event count per base-resolution interval, with a default value of 5.0;
- `max_zero_window_proportion`: the maximum allowed proportion of zero-count base-resolution intervals, with a default value of 0.1, or 10%.

The sequence of checks must be:

1. Construct the complete base-resolution count series for the evaluation range.
2. Compute the observed event rate as the mean count.
3. Compute the zero-window proportion.
4. Compare the observed event rate against `min_event_rate`.
5. Compare the zero-window proportion against `max_zero_window_proportion`.
6. Stop immediately if either condition fails.

This screen must occur before any variance, Fano-factor, or candidate-window calculation because these diagnostics are not informative when the series is structurally sparse or dominated by zeros.

Failure diagnostics must identify the horizon (or indicate full-range mode), the failed criterion, the observed value, and the configured threshold. The diagnostic must explain that the data are too sparse or contain too many zeros for the current non-hurdle Poisson workflow and should recommend a hurdle or zero-inflated model (or, if full-range mode failed due to sparse sub-periods, evaluating bounded local macro-horizons).

The suitability output should include the observed event rate, zero-window proportion, and an explicit suitability status.

## Minimum Window Coverage Requirement

Candidate window widths must be evaluated independently before Fano-factor analysis. For a count series containing \(n\) base-resolution observations and a candidate width \(w\), the number of complete, non-overlapping windows is:

\[
k = \left\lfloor \frac{n}{w} \right\rfloor
\]

A candidate is retained only when:

- \(w\) is a positive integer; and
- \(k \geq \text{min\_windows}\).

The default for `min_windows` is 30 and must be configurable.

The implementation must report both accepted and rejected candidates. Non-positive widths are invalid input and must be reported explicitly rather than silently discarded. If no candidate meets the coverage requirement, the analysis must raise an informative error stating that the requested windows are too long for the available horizon or dataset range.

Incomplete windows at the end of the evaluation range must not be included in the Fano calculation.

## Horizon-Scoped and Full-Range Fano Factor Evaluation

Each retained candidate window must be processed through the following steps:

1. Aggregate the base-resolution counts into complete, non-overlapping windows.
2. Compute the mean count.
3. Compute the sample variance using \(k-1\) degrees of freedom.
4. Compute the Fano factor \(F = S^2 / \bar{N}\).
5. Compute chi-square confidence bounds around the Poisson reference value of 1.
6. Mark the candidate as accepted when its Fano factor lies inside the confidence bounds.

For a confidence level \(c\), define \(\alpha = 1 - c\). The lower and upper bounds are:

\[
F_{\text{lower}} =
\frac{\chi^2_{\alpha/2,\ k-1}}{k-1}
\]

\[
F_{\text{upper}} =
\frac{\chi^2_{1-\alpha/2,\ k-1}}{k-1}
\]

The default confidence level is 0.90 and must be configurable. The acceptance condition is:

\[
F_{\text{lower}} \leq F \leq F_{\text{upper}}
\]

A candidate with a zero mean cannot support a Fano-factor calculation and must produce an explicit diagnostic rather than a NaN or infinite result.

If no candidate window is accepted, the evaluation is still valid. It should report that no Poisson-compatible window was found rather than treating that outcome as an input error.

## Single-Stage Full-Range Homogeneous Poisson Analysis

In addition to the two-stage macro-horizon workflow, the package must support single-stage evaluation over the entire dataset range (`horizon_window = None`).

### Purpose and Statistical Interpretation

In single-stage mode, candidate window widths are tested directly against the full dataset. A window width \(w\) accepted under the Fano criterion models the arrival process as **piecewise-homogeneous**:

- Within each window of width \(w\), arrivals follow a homogeneous Poisson process with a constant window-level arrival intensity.
- Across consecutive windows ordered chronologically, the observed arrival intensity \(\lambda_i = N_i / \Delta t\) shows how the arrival rate varies over time.

### Full-Range Execution and Window Selection

1. Invoke the stateless evaluation pipeline with `horizon_window = None` over the full dataset range.
2. Apply the standard suitability checks (`min_event_rate`, `max_zero_window_proportion`) and coverage checks (\(k \ge \text{min\_windows}\)) across the entire series.
3. Evaluate all valid candidate window widths using the sample Fano factor and chi-square bounds.
4. **Candidate Ranking (Optional Stateless Helper):** When multiple candidate windows are accepted over the full range, provide a deterministic helper to rank accepted windows (e.g., by lowest dispersion deviation \(|F - 1|\), then narrowest confidence-band width `fano_upper - fano_lower`, with configurable preference for shorter or longer windows) while still returning all accepted candidates in the structured result.
5. **Non-Stationarity Diagnostic:** If no candidate window is accepted in single-stage mode due to overdispersion (\(F > F_{\text{upper}}\)), the diagnostic metadata should note that global non-stationarity across the full dataset may have inflated sample variance and recommend running the two-stage macro-horizon workflow.

### Chronological Arrival Intensity Series and Plotting

Once a homogeneous Poisson window width is selected in single-stage mode, the system must support constructing and visualizing the time-ordered arrival intensity series:

- **Time-Ordered Intensity Construction:** Partition the evaluation range into sequential, non-overlapping windows of width \(w\) in ascending chronological order and compute each window's start timestamp (`window_start`) and arrival intensity (`arrival_intensity` = count divided by window duration).
- **Intensity Variation Plot:** Provide a plotting utility that accepts the chronological intensity series (or TSEDA export) and plots `window_start` on the horizontal axis against `arrival_intensity` on the vertical axis. This visualization illustrates how the within-window homogeneous arrival intensity changes over time across the full dataset.

## Structured Evaluation Output

Both horizon-scoped and full-range evaluations must return a structured result containing:

- evaluation mode (`horizon_scoped` or `full_range`);
- requested and actual `start_date`;
- requested and actual `end_date`;
- `horizon_window` (duration or `None`);
- base resolution;
- boundary-completeness and truncation metadata;
- number of events and base-resolution intervals;
- observed event rate;
- zero-window proportion;
- suitability status and diagnostics;
- candidate micro-window inputs;
- valid and rejected candidates;
- accepted micro-windows; and
- whether at least one Poisson-compatible micro-window was found.

Each candidate result must include:

- `window_size`;
- number of complete micro-windows;
- mean count;
- sample variance;
- `fano_factor`;
- `fano_lower`;
- `fano_upper`;
- confidence-band width; and
- acceptance status.

Serialized timestamps should use ISO-8601 formatting.

## Two-Stage Horizon Hyperparameter Tuning

For two-stage analysis, the duration of `horizon_window`, denoted by \(H\), affects the bias-variance and statistical-power trade-off. A short horizon may better satisfy local stationarity but provide fewer observations. A long horizon may provide more statistical power but may combine different arrival regimes and increase bias.

### Inputs

The tuning component must accept:

- a list of candidate horizon lengths, for example 14, 21, 30, 45, 60, and 90 days;
- candidate micro-window widths;
- a positive `step_size`, such as one day;
- the suitability and Fano configuration; and
- a boundary policy.

### Sliding Evaluation

For each candidate horizon \(H\), the software must:

1. Select a valid `start_date`.
2. Extract the complete horizon beginning at that date.
3. Run the complete suitability and Fano-evaluation pipeline.
4. Increment `start_date` by `step_size`.
5. Repeat while another complete horizon fits within the dataset.

The selector remains stateless. The tuning utility coordinates repeated calls but does not add persistent state to the underlying selector.

### Summary Diagnostics

For each candidate horizon length, the tuning utility must calculate and return three diagnostics:

1. **Poisson Acceptance Rate**

   The proportion of suitability-valid stepped horizons for which at least one candidate micro-window is accepted:

   \[
   \frac{\text{valid horizons with at least one accepted micro-window}}
   {\text{all suitability-valid horizons}}
   \]

   Suitability-failed horizons are excluded from the denominator, because those horizons did not proceed to Fano evaluation. This conditioning must remain explicit to avoid presenting an acceptance rate over an unrepresentative or small valid sample as if it described every attempted horizon.

   The summary must therefore report the acceptance rate together with the counts from which it was derived:

   - number of attempted horizons;
   - number of suitability-valid horizons, which is the denominator;
   - number of suitability-failed horizons; and
   - number of suitability-valid horizons with at least one accepted micro-window, which is the numerator.

   When there are no suitability-valid horizons, the Poisson acceptance rate must be `null`, not 0, because no valid denominator exists. The output must also identify the zero-denominator condition in the tuning diagnostics. For example, an acceptance rate of 1.0 derived from 2 valid horizons out of 100 attempted horizons must remain distinguishable from 1.0 derived from all 100 horizons.

   A downstream selection rule may require a minimum number or proportion of suitability-valid horizons before treating a candidate horizon length as reliable. Any such threshold must be reported as a separate reliability criterion and must not be hidden by changing the acceptance-rate definition.

2. **Mean Confidence Band Width**

   The mean of `fano_upper - fano_lower` across valid candidate evaluations. The aggregation convention must be documented and consistent.

3. **Median Dispersion Deviation**

   The median of \(|F - 1|\) across valid candidate evaluations.

If a metric has no valid observations, its value must be `null` rather than NaN or a fabricated result. The reason that the metric is unavailable must be retained in the diagnostics.

### Comparison Output

The tuning result should provide one row per candidate horizon length with:

- `horizon_window`;
- `step_size`;
- number of attempted horizons;
- number of suitability-valid horizons;
- number of suitability-failed horizons;
- number of horizons with at least one accepted micro-window;
- Poisson acceptance rate;
- suitability-valid horizon proportion;
- mean confidence-band width; and
- median dispersion deviation.

The suitability-valid horizon proportion is:

\[
\frac{\text{suitability-valid horizons}}
{\text{attempted horizons}}
\]

It complements the conditional Poisson acceptance rate and makes sparse coverage visible to both readers and downstream automation. If no horizons were attempted, this proportion must be `null`.

The tuning output must not itself select an optimal \(H^*\). The comparison table is an intermediate result; recommendation is performed by a separate stateless function.

## Optimal Horizon Recommendation (Two-Stage Mode)

To preserve methodological transparency, the package must expose a separate `recommend_optimal_horizon` function. It accepts the comparison table from `tune_horizon_windows` and applies an explicit, configurable rule. The tuning function must never silently promote one horizon to recommended status.

### Selection Rule Configuration

```python
@dataclass(frozen=True)
class HorizonRecommendationConfig:
    min_valid_horizon_proportion: float = 0.80
    min_valid_horizons: int = 10
    acceptance_rate_tolerance: float = 0.02
    prefer_shorter_horizon: bool = True
```

Configuration validation must reject:

- a `min_valid_horizon_proportion` outside \((0, 1]\);
- a non-positive `min_valid_horizons`; and
- a negative `acceptance_rate_tolerance`.

### Multi-Stage Selection Procedure

The recommendation function applies the following deterministic stages:

1. **Coverage and Sample-Size Gate**

   Retain candidates only when:

   - `poisson_acceptance_rate` is not `null`;
   - `suitability_valid_horizon_proportion >= min_valid_horizon_proportion`; and
   - `suitability_valid_horizons >= min_valid_horizons`.

   If no candidate passes, return `recommended_horizon = null` and diagnostics identifying every failed threshold. This prevents a high acceptance rate based on very few valid horizons from being recommended.

2. **Acceptance-Rate Filtering**

   Among reliability-qualified candidates, calculate the maximum acceptance rate \(R_{\max}\) and retain candidates satisfying:

   \[
   R(H) \geq R_{\max} - \text{acceptance\_rate\_tolerance}
   \]

3. **Dispersion and Precision Tie-Breaking**

   Rank retained candidates lexicographically by:

   1. lowest `median_dispersion_deviation`;
   2. narrowest `mean_confidence_band_width`; and
   3. shortest `horizon_window` when `prefer_shorter_horizon=True`, otherwise longest.

   Missing dispersion or precision metrics prevent a candidate from being ranked and must be recorded in the audit.

The final horizon preference is a responsiveness policy: shorter horizons are favored by default because they may adapt more quickly to non-stationary regime shifts.

### Structured Recommendation Output

The function must return:

- `recommended_horizon`: selected \(H^*\), or `null` if no candidate qualifies;
- `rule_config`: the exact parameters applied;
- `selected_metrics`: the complete selected summary row, or `null`; and
- `candidate_audit`: one audit record per candidate.

Each audit record must identify the horizon, relevant observed metrics, and one of:

- `selected`;
- `runner_up`; or
- `rejected`.

Rejection records must identify the stage and gate, including observed and required values for reliability or acceptance-rate failures.

### Performance Curves

Consumers may plot the horizon comparison table as performance curves:

- horizon length versus Poisson acceptance rate;
- horizon length versus suitability-valid horizon proportion;
- horizon length versus mean confidence-band width; and
- horizon length versus median dispersion deviation.

These plots are interpretive aids. They must not select or relabel an optimal horizon independently of `recommend_optimal_horizon`. A selection rule should consider the acceptance rate, confidence-band width, dispersion deviation, and adequacy of valid-horizon coverage together; a high acceptance rate based on very few valid horizons must not automatically win.

## Daily Window Table Requirement

A separate reporting function is required to build a tabular export from the underlying timestamp data.

The function must:

1. identify the first and last timestamp in the dataset;
2. determine the first and last complete calendar days;
3. omit incomplete leading and trailing days;
4. split every complete day into evenly spaced sub-day windows;
5. aggregate event counts into one column per sub-day window;
6. use the date as the first column and names such as `window_00`, `window_01`, and so on for count columns;
7. represent intervals with no events as zero; and
8. write the resulting data as a CSV.

The configured window width must divide a complete day evenly unless an explicit remainder policy is supplied. The default behavior should be to reject widths that leave a partial day segment.

The returned metadata must identify:

- the output file;
- first and last complete days;
- omitted leading and trailing bounds;
- configured window size;
- output columns; and
- number of exported rows.

This table is intended for day-by-day operational reporting and planning after a defensible window width has been selected.

## TSEDA Export and Intensity Trajectory Requirement

A second export function must produce a compact, TSEDA-style two-column intensity series using the same full-day bounds as the daily table export (or the explicit evaluation bounds when exporting directly from a single-stage full-range evaluation).

The CSV must contain:

- `window_start`: the beginning timestamp of the window, ordered chronologically;
- `arrival_intensity`: the count within the window divided by the window duration.

For a window width of 2 hours, the sampling period must be reported as `2 hours` and the intensity unit as `arrivals per hour`.

The returned metadata must include:

- sampling period;
- intensity unit;
- first and last complete days (or evaluation bounds);
- omitted leading and trailing bounds;
- output file; and
- number of exported rows.

The daily table and TSEDA exports must be functionally consistent for the same input dataset, window width, and full-day bounds.

### Selected Window TSEDA Series for Both Evaluation Modes

Both analytical modes must be able to persist the complete intensity trajectory
for one selected window width:

- single-stage mode exports the selected width over the full dataset by
  passing `horizon_window = None`;
- two-stage mode exports the selected width within one selected macro-horizon
  by passing its `start_date` and `horizon_window`.

The export must:

1. accept the selected positive-integer `window_size` in base-resolution
   units;
2. use the same complete evaluation bounds and non-overlapping aggregation as
the applicable selected evaluation mode;
3. calculate `arrival_intensity` as the count divided by the selected window
   duration in the configured rate unit;
4. associate each intensity with the temporal midpoint of its originating
   window, rather than its boundary start;
5. write a chronological two-column CSV containing `window_midpoint` and
   `arrival_intensity`;
6. return the output path, timestamp-coordinate convention, selected
   `window_size`, sampling period, intensity unit, evaluation bounds, omitted
   incomplete base intervals, and exported row count; and
7. allow the same midpoint-labelled series to be plotted as a time trajectory.

The TSEDA midpoint export must discard only incomplete trailing windows and
report that remainder explicitly. It must not silently shift, overlap, or
fabricate windows.

### Arrival Intensity Trajectory Visualization

To support single-stage full-range analysis, a plotting function must accept the time-ordered TSEDA intensity series (`window_start`, `arrival_intensity`) and generate a time-series chart showing `arrival_intensity` across sequential windows (`window_start` in chronological order). This plot demonstrates how the within-window homogeneous arrival intensity evolves over the full observation period.

## Processing Logic

The end-to-end evaluation must execute in the following order:

1. Validate and sort the input data.
2. Define the half-open evaluation interval—either from `start_date` and `horizon_window` (two-stage mode) or across the full dataset bounds when `horizon_window = None` (single-stage mode).
3. Enforce boundary completeness or emit explicit truncation metadata.
4. Extract the requested evaluation range.
5. Aggregate the records into complete base-resolution counts.
6. Screen the range for event-rate and zero-inflation suitability.
7. Filter candidate windows by minimum coverage.
8. Aggregate counts for each retained candidate window.
9. Compute Fano statistics and chi-square confidence bounds.
10. Return accepted windows, rejected candidates, and all diagnostic metadata.

For two-stage analysis, the horizon-tuning component coordinates repeated executions of this workflow without introducing state into the core selector. For single-stage analysis, the selected window width can be passed directly to the midpoint-based TSEDA intensity export and chronological intensity plotting utilities.

## Configuration

The primary analysis configuration must expose the current Fano-selection defaults:

```python
@dataclass(frozen=True)
class WindowSelectionConfig:
    min_event_rate: float = 5.0
    max_zero_window_proportion: float = 0.1
    min_windows: int = 30
    confidence_level: float = 0.90
```

`horizon_window`, the base resolution, the step size, and the boundary policy are temporal evaluation parameters and may be supplied separately.

Configuration validation must reject:

- a non-positive `min_event_rate`;
- a zero proportion outside the interval from 0 to 1;
- a non-positive `min_windows`;
- a confidence level outside the open interval from 0 to 1;
- a non-positive horizon duration (when `horizon_window` is not `None`) or non-positive step size; and
- unsupported or ambiguous temporal resolutions.

## Error Handling

The package should use explicit domain-specific errors for invalid or unusable inputs. At minimum, the design should distinguish:

- timestamp parsing and validation errors;
- horizon boundary errors;
- data suitability errors;
- candidate-window coverage errors;
- empty or zero-duration input errors; and
- invalid frequency or temporal-resolution errors.

Errors must identify the affected input or horizon and should include observed and required values where applicable. Invalid input must not silently fall back to a different configuration.

## Expected Contribution

The project will provide a practical, statistically grounded method for selecting observation windows in count processes across both locally stationary and piecewise-homogeneous regimes. Separating macro-horizon stationarity tuning from micro-window dispersion testing prevents long-term drift from being conflated with short-term variability, while single-stage full-range evaluation enables direct identification of homogeneous Poisson windows and visualization of how arrival intensity varies over time.

This design delivers a reproducible procedure for converting raw event timestamps into:

- locally evaluated and full-range Poisson diagnostics;
- data-driven observation window recommendations;
- evidence for selecting a stable macro-horizon in two-stage workflows;
- time-ordered arrival-intensity trajectories in single-stage workflows; and
- planning-ready count and arrival-intensity datasets.

## Expected Outcomes

- A reproducible procedure supporting both two-stage horizon-scoped and single-stage full-range time-window selection.
- A clear distinction between macro-horizon stationarity, micro-window aggregation, and full-range piecewise-homogeneous windows.
- Explicit handling of incomplete temporal boundaries.
- A data-driven criterion based on sample dispersion rather than arbitrary calendar choices.
- Reproducible comparison of candidate horizon lengths in two-stage mode.
- Consistent daily-count, TSEDA intensity exports, and chronological arrival-intensity plots.
- Test coverage for boundary validation, suitability failures, insufficient coverage, confidence calculations, two-stage horizon tuning, single-stage full-range evaluation, and export/plotting consistency.

## Significance

The proposed method is relevant to domains where demand is observed as event counts over time. It offers a principled way to move from raw arrivals to planning-ready models while respecting the scale-dependent nature of variability, local stationarity assumptions, and time-varying arrival intensities across operational windows.
