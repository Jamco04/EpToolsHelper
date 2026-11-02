"""Read time series out of an EnergyPlus .eso file (no third-party packages).

The .eso always contains every Output:Variable and Output:Meter result, unlike the
CSV, which ReadVarsESO cuts off at 255 columns.

    eso = ESO.load("eplusout.eso")
    eso.find("Ideal Loads Supply Air Total Heating Energy", "Hourly")
    series = eso.series(variable_id)   # list of ((month, day, hour, minute, dst), value)

Timestamps are in standard time, as EnergyPlus reports them. dst is 1 when
daylight saving was in force, so local clock time is one hour later.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

DICT_RE = re.compile(r"^(\d+),(\d+),(?:([^,\[]*),)?(.*?) \[([^\]]*)\] !(\w+)")  # meters have no key
TIME_CODES = {"2": "hourly", "3": "daily", "4": "monthly", "5": "runperiod", "6": "annual"}


@dataclass
class Variable:
    id: str
    key: str
    name: str
    units: str
    frequency: str

    @property
    def label(self) -> str:
        return f"{self.key}:{self.name} [{self.units}]({self.frequency})"


@dataclass
class ESO:
    variables: dict[str, Variable] = field(default_factory=dict)
    # environment name -> variable id -> list of (timestamp, value)
    data: dict[str, dict[str, list]] = field(default_factory=dict)

    @classmethod
    def load(cls, path) -> "ESO":
        eso = cls()
        lines = Path(path).read_text(errors="replace").splitlines()
        i = 1
        while i < len(lines) and not lines[i].startswith("End of Data Dictionary"):
            m = DICT_RE.match(lines[i])
            if m and int(m.group(1)) > 6:
                vid, _, key, name, units, freq = m.groups()
                eso.variables[vid] = Variable(vid, (key or "Meter").strip(), name.strip(), units, freq)
            i += 1
        env = None
        stamp = None
        for line in lines[i + 1:]:
            if line.startswith("End of Data"):
                break
            code, _, rest = line.partition(",")
            if code == "1":
                env = rest.split(",")[0].strip()
                eso.data.setdefault(env, {})
                continue
            if code in TIME_CODES:
                p = [x.strip() for x in rest.split(",")]
                if code == "2":   # day, month, day of month, dst, hour, start min, end min, daytype
                    stamp = (int(p[1]), int(p[2]), int(p[4]), int(float(p[5])), int(p[3]))
                elif code == "3":
                    stamp = (int(p[1]), int(p[2]), 0, 0, int(p[3]))
                elif code == "4":
                    stamp = (int(p[1]), 0, 0, 0, 0)
                else:
                    stamp = (0, 0, 0, 0, 0)
                continue
            if env is not None and code in eso.variables:
                value = float(rest.split(",")[0])
                eso.data[env].setdefault(code, []).append((stamp, value))
        return eso

    @property
    def main_environment(self) -> str | None:
        """The environment with the most records: the annual run, not the design days."""
        if not self.data:
            return None
        return max(self.data, key=lambda e: sum(len(v) for v in self.data[e].values()))

    def find(self, name_contains: str, frequency: str | None = None, key: str = "*") -> list[Variable]:
        n = name_contains.lower()
        return [v for v in self.variables.values()
                if n in v.name.lower()
                and (frequency is None or v.frequency.lower() == frequency.lower())
                and (key == "*" or v.key.lower() == key.lower())]

    def series(self, var_id: str, environment: str | None = None) -> list:
        env = environment or self.main_environment
        return self.data.get(env, {}).get(var_id, [])


def to_kwh(value: float, units: str) -> float:
    return value / 3.6e6 if units == "J" else value / 1000.0 if units == "W" else value


def stamp_text(stamp) -> str:
    month, day, hour, minute = stamp[:4]  # hour is EnergyPlus's 1-24 hour ending; minute is the interval start
    if month and day:
        return f"{month:02d}-{day:02d} {max(hour - 1, 0):02d}:{minute:02d}"
    return f"month {month}" if month else "run period"
