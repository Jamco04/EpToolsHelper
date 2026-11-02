# eptools reference

Everything in the window can also be done from a terminal. This page also describes the
study file that **Save study…** writes. You don't need any of it to use the app.

Run `python -m eptools` with no arguments to open the window. `python -m eptools run`,
`check` and `inspect` are the command-line versions. `docs/index.html` is the warning
checker as a web page: drop an `.err` file onto it.

## Parameter studies from the command line

**1. Find what to change.** List the classes in a model, then look at one:

```
python -m eptools inspect "models/Bungalow Original.idf"
python -m eptools inspect "models/Bungalow Original.idf" Material --name "EPS*"
```

**2. Describe the study.** Copy `examples/study.json` and edit it. Paths are relative to the
study file.

```json
"parameters": {
  "wall_insulation_m": { "object": "Material", "name": "EPS Expanded Polystyrene*",
                         "field": "Thickness", "values": [0.0515, 0.1, 0.15] },
  "window_u":          { "object": "WindowMaterial:SimpleGlazingSystem", "field": "U-Factor",
                         "values": [3.0, 1.4] },
  "infiltration_factor": { "object": "ZoneInfiltration:DesignFlowRate", "field": "Design Flow Rate",
                           "multiply": [1.0, 0.5] },
  "heating_setpoint_c": { "object": "Schedule:Compact", "name": "*Heating Setpoint Schedule",
                          "replace": 21, "values": [20, 21] }
}
```

Every combination of every model and every parameter value is run. The example is
2 models × 3 × 2 × 2 × 2 = 48 runs. To run a hand-picked list instead, give
`"runs_csv": "runs.csv"` with a `model` column and one column per parameter.

| Key | Meaning |
| --- | --- |
| `object` | IDF class, e.g. `Material`. Case doesn't matter. |
| `name` | Object name, with `*` wildcards. Default `*` means every object of that class. |
| `field` | Field name as written in the `!- ...` comment (units are ignored) or its number, where 1 = Name. |
| `values` | Values to set. |
| `multiply` | Factors to multiply the existing value by, used instead of `values`. |
| `replace` + `values` | Replace every field equal to `replace` in the matched objects. Useful for setpoints inside `Schedule:Compact`. |
| `placeholder` + `values` | Replace a text tag such as `@@setpoint@@` anywhere in the file (jEPLus style). |

`fixed` takes the same kind of edits with a single `set`/`value`, applied to every run, for
example the timestep. `outputs.replace_existing` removes the model's own `Output:Variable`
and `Output:Meter` requests and `outputs.add` adds yours. That avoids the 255-column CSV
limit and most of the "requested but not generated" warnings. `collect.hourly` lists the
outputs to total across zones for each run.

**3. Check the edits before running anything.**

```
python -m eptools run my_study.json --dry-run
```

This writes `in.idf`, `params.json` and `changes.csv` for each run. `changes.csv` lists every
object that changed, with old and new values. If any edit matches nothing (a typo in a name,
say), the study stops with a message and nothing runs.

**4. Run a couple, then everything.**

```
python -m eptools run my_study.json --limit 2
python -m eptools run my_study.json --jobs 4
python -m eptools run my_study.json --jobs 4 --resume    # skip runs that already finished
```

`--jobs` runs several simulations at once. Each one starts EnergyPlus inside its own run folder.
If you drive EnergyPlus in parallel from your own script, do the same (for example
`subprocess.run(cmd, cwd=run_folder)`): with `-r`, ReadVarsESO writes `readvars.audit` into the
folder EnergyPlus was started from, so parallel runs started from one folder clash over it.

Before starting, the runner checks that each model's version matches EnergyPlus.

**5. Results.** These are written to the study's `output_dir`:

- `results.csv` has one row per run: the parameters, status (`OK`, `SUSPECT`, `FAILED`),
  warning and severe counts, heating and hot water kWh, kWh/m², unmet heating hours, and the
  annual total and peak of each `collect.hourly` output. There are two unmet-hours columns, both
  from the "Time Setpoint Not Met" table in the HTML report: `unmet_heating_h` counts every hour
  the heating setpoint wasn't reached (nights and empty-house hours too), and
  `unmet_occupied_heating_h` counts only the hours when people are in. Use the occupied figure
  for comfort.
- `check_report.html` explains the warnings of every run.
- `run_0001/` … each contain the edited model, the EnergyPlus output and `hourly.csv`.

The **Heat pump cost…** button (or `examples/heat_pump_cost.py`) goes from `hourly.csv` to
heat pump electricity and cost under a flat and a day/night tariff, and writes
`heat_pump_costs.csv`. It uses a fixed COP and placeholder prices, so replace both with the
project's own figures.

## Checking runs from the command line

```
python -m eptools check runs/example/run_0001
python -m eptools check runs/example/run_0* --quiet --baseline runs/example/run_0001 --html report.html
```

The exit code is 1 if any run failed, so batch scripts can react to it. A message the rules
don't recognise is shown as "Not in the rule list yet". Add a `Rule` to `RULES` in
`eptools/check.py`, then run `python docs/build_explainer.py` so the web page uses the
same rules.

## Things to know

- Edits by field name rely on the `!- Field name` comments that DesignBuilder, IDF Editor and
  IDFVersionUpdater write. Without those comments, use field numbers.
- DesignBuilder puts thicknesses in material names (`..._0.0515`). Changing the thickness
  doesn't rename the material.
- `replace` changes every matching value in the matched objects, design-day lines included.
  Check `changes.csv`.
- With ideal loads, heating and hot water are reported as `DistrictHeatingWater`. That is
  heat delivered, not electricity.
- `hourly.csv` timestamps are as EnergyPlus writes them, with a `dst` column that is 1 while
  daylight saving applies. Take this into account when lining up tariffs that follow clock time.
- The rules were written against EnergyPlus 26.2 and DesignBuilder exports. Labels are
  guidance. When in doubt, read the message and search it on [Unmet Hours](https://unmethours.com/).

## Tests

```
python -m unittest discover tests
```

The tests use `tests/fake_energyplus.py` in place of EnergyPlus, so they run anywhere. The
window test is skipped when there is no display.

## Building eptools.exe

`.github/workflows/build.yml` runs the tests and builds `eptools.exe` with PyInstaller on
every push to `main`, and publishes it as a GitHub Release named after the version in
`eptools/__init__.py`. Bump that version to make a new release. To build it yourself on Windows:

```
pip install pyinstaller
pyinstaller --onefile --windowed --name eptools eptools.pyw
```
