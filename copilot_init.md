# Copilot project bootstrap for count_ts_window_selector

## Project purpose
This project evaluates timestamp event datasets and identifies a Poisson-compatible observation window using count-time-series heuristics.

The core package is `count_ts_window_selector`, defined in `src/count_ts_window_selector/__init__.py` and `src/count_ts_window_selector/__main__.py`.

Primary workflow:
- load a timestamp CSV with a single `timestamp` column
- convert timestamps to hourly counts or other aggregated counts
- validate data suitability for the non-hurdle Poisson workflow
- filter candidate windows by minimum coverage (`min_windows`)
- compute Fano factors and a chi-square confidence interval around 1
- return candidate windows that remain compatible with Poisson behavior

## Repository layout
- `src/count_ts_window_selector/` — package implementation
- `data/` — timestamp datasets for analysis
- `notebooks/` — exploratory notebooks for specific datasets
- `README.md` — project overview and usage examples
- `time_window_selector.md` — design notes / proposal
- `pyproject.toml` — project metadata and Python dependencies

## Environment
Use the project virtual environment created by uv:

```bash
cd /home/rajiv/programming/count_ts_window_selector
uv sync
```

For Python commands, prefer:

```bash
./.venv/bin/python
```

For package-run commands:

```bash
uv run python ...
```

## Typical notebook pattern
Notebook cells should follow this pattern when reading a dataset:

```python
from collections import defaultdict
from datetime import datetime
from pathlib import Path
import csv
import sys

ROOT = Path.cwd().resolve()
candidate_roots = [ROOT, *ROOT.parents]
for candidate in candidate_roots:
    if (candidate / "src" / "count_ts_window_selector").exists():
        ROOT = candidate
        break

if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

DATA_FILE = ROOT / "data" / "<dataset_name>.csv"

timestamps = []
with DATA_FILE.open() as f:
    reader = csv.DictReader(f)
    for row in reader:
        raw = row["timestamp"]
        if "." in raw:
            base, frac = raw.split(".", 1)
            raw = base + "." + frac[:6]
        timestamps.append(datetime.strptime(raw, "%Y-%m-%d %H:%M:%S.%f"))
```

Then aggregate by hour:

```python
hourly_counts = defaultdict(int)
for dt in timestamps:
    key = dt.replace(minute=0, second=0, microsecond=0)
    hourly_counts[key] += 1

series = [hourly_counts[k] for k in sorted(hourly_counts)]
```

## Config and usage
The package exposes `WindowSelectionConfig` and `select_time_window`.

```python
from count_ts_window_selector import WindowSelectionConfig, select_time_window

config = WindowSelectionConfig(
    min_event_rate=5.0,
    max_zero_window_proportion=0.1,
    min_windows=30,
    confidence_level=0.90,
)

results = select_time_window(series, [1, 2, 3, 4, 6, 8, 12, 24], config=config)
```

## Data conventions
- all timestamp datasets are a single-column CSV named like `*_timestamp_events.csv`
- the column name is usually `timestamp`
- values are full ISO-like timestamps with nanoseconds
- custom parsing may be needed to normalize fractional seconds before `datetime.strptime`

## Expected output interpretation
- `observed_event_rate` should be high enough to pass `min_event_rate`
- `zero_window_proportion` should remain below `max_zero_window_proportion`
- a 1-hour window often serves as the most defensible default if it is closest to a Fano factor of 1 while staying inside the confidence interval

## Project-specific guidance
- do not assume the notebook working directory is the repository root
- when running notebooks from a nested folder, walk up parent directories until the root with `src/count_ts_window_selector` is found
- prefer project-local imports over changing the interpreter environment unnecessarily
- keep visual analysis concise: hourly counts plot and Fano-factor plot are the usual deliverables
- output should focus on the Poisson-like suitability decision and recommended time window, not on raw timestamp dumps

## Quick validation commands
```bash
cd /home/rajiv/programming/count_ts_window_selector
./.venv/bin/python - <<'PY'
from count_ts_window_selector import WindowSelectionConfig, check_data_suitability
print('project import OK')
PY
```

## Notes for future agents
This project is intentionally about higher-event-rate count data. It is not meant for sparse or hurdle-style series; use the package only after the data-suitability screen passes.
