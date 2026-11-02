"""Turn each run's hourly heat demand into heat pump electricity and cost.

Deliberately simple: a fixed seasonal COP, no tank and no controls, so the numbers are
easy to follow. Replace COP and prices with the project's own figures.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

HEAT_COLUMN = "DistrictHeatingWater:Facility [kWh]"   # space heating + hot water delivered by ideal loads
TIME_COLUMN = "timestamp (standard time, interval start)"


@dataclass
class Tariffs:
    cop: float = 3.0
    flat: float = 0.35          # EUR/kWh
    day: float = 0.40
    night: float = 0.20
    night_start: int = 23       # local clock hour
    night_end: int = 8

    def is_night(self, hour: int) -> bool:
        if self.night_start > self.night_end:
            return hour >= self.night_start or hour < self.night_end
        return self.night_start <= hour < self.night_end


def local_hour(timestamp: str, dst: str) -> int:
    """hourly.csv timestamps are standard time; add an hour while daylight saving applies."""
    hour = int(timestamp.split()[1].split(":")[0])
    return (hour + int(dst or 0)) % 24


def run_cost(hourly_csv: Path, t: Tariffs) -> dict | None:
    with open(hourly_csv, newline="") as f:
        reader = csv.DictReader(f)
        if HEAT_COLUMN not in (reader.fieldnames or []):
            return None
        heat = elec = peak = flat = day_night = night_kwh = 0.0
        for h in reader:
            q = float(h[HEAT_COLUMN])
            e = q / t.cop
            heat += q
            elec += e
            peak = max(peak, e)
            flat += e * t.flat
            if t.is_night(local_hour(h[TIME_COLUMN], h.get("dst", "0"))):
                day_night += e * t.night
                night_kwh += e
            else:
                day_night += e * t.day
    return {"heat_kwh": round(heat), "hp_electricity_kwh": round(elec), "hp_peak_kw": round(peak, 2),
            "night_share_pct": round(100 * night_kwh / elec, 1) if elec else 0.0,
            "cost_flat_eur": round(flat), "cost_day_night_eur": round(day_night)}


def study_costs(results_csv: Path, t: Tariffs) -> tuple[list[dict], Path]:
    """Cost every OK run listed in results.csv; write heat_pump_costs.csv next to it."""
    results_csv = Path(results_csv)
    out = []
    with open(results_csv, newline="") as f:
        rows = list(csv.DictReader(f))
    for row in rows:
        hourly = Path(row.get("folder", "")) / "hourly.csv"
        if row.get("status") in ("FAILED", "NO OUTPUT", "CANCELLED") or not hourly.exists():
            continue
        c = run_cost(hourly, t)
        if c:
            out.append({"run": row["run"], "model": row["model"], **c})
    path = results_csv.parent / "heat_pump_costs.csv"
    if out:
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(out[0]))
            w.writeheader()
            w.writerows(out)
    return out, path
