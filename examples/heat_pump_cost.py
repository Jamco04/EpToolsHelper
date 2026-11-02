"""Estimate heat pump electricity and cost for every run of a study.

The same calculation is behind the "Heat pump cost" button in the eptools window.

    python examples/heat_pump_cost.py runs/example

Reads runs/example/results.csv and each run's hourly.csv, and writes
runs/example/heat_pump_costs.csv. Change the figures below to the project's own.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from eptools.costs import Tariffs, study_costs  # noqa: E402

TARIFFS = Tariffs(cop=3.0, flat=0.35, day=0.40, night=0.20, night_start=23, night_end=8)

if __name__ == "__main__":
    folder = Path(sys.argv[1] if len(sys.argv) > 1 else "runs/example")
    rows, path = study_costs(folder / "results.csv", TARIFFS)
    print(f"Wrote {path} ({len(rows)} runs)" if rows else "No runs with an hourly DistrictHeatingWater:Facility column")
