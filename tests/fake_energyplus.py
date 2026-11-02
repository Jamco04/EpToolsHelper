#!/usr/bin/env python3
"""Stand-in for EnergyPlus used by the tests: copies a real run's outputs and writes a small hourly .eso."""
import shutil
import sys
from pathlib import Path

DATA = Path(__file__).parent / "data"

if "--version" in sys.argv:
    print("EnergyPlus, Version 26.2.0-fake")
    sys.exit(0)
out = Path(sys.argv[sys.argv.index("-d") + 1])
idf = Path(sys.argv[-1]).read_text(encoding="latin-1")
scale = 2.0 if "1.4;" in idf or "1.4," in idf else 3.0          # pretend better windows need less heat
shutil.copy(DATA / "bungalow.err", out / "eplusout.err")
shutil.copy(DATA / "bungalow_table.html", out / "eplustbl.htm")
lines = ["Program Version,EnergyPlus, Version 26.2.0-fake",
         "1,5,Environment Title[],Latitude[deg],Longitude[deg],Time Zone[],Elevation[m]",
         "2,8,Day of Simulation[],Month[],Day of Month[],DST Indicator[1=yes 0=no],Hour[],StartMinute[],EndMinute[],DayType",
         "7,1,ZONE A IDEAL LOADS AIR,Zone Ideal Loads Supply Air Total Heating Energy [J] !Hourly",
         "8,1,ZONE B IDEAL LOADS AIR,Zone Ideal Loads Supply Air Total Heating Energy [J] !Hourly",
         "9,1,DistrictHeatingWater:Facility [J] !Hourly",
         "End of Data Dictionary",
         "1,TEST RUN,  53.43,  -6.25,   0.00,  85.00"]
for hour in range(1, 25):
    lines.append(f"2,1, 1, 1, 0,{hour:2d}, 0.00,60.00,Sunday")
    lines += [f"7,{scale * 3.6e6 * (hour % 3)}", f"8,{3.6e6}", f"9,{scale * 3.6e6 * (hour % 3) + 3.6e6}"]
lines += ["End of Data", " Number of Records Written=          100"]
(out / "eplusout.eso").write_text("\n".join(lines) + "\n")
