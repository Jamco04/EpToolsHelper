# Changes

## 0.2.1

**Running several simulations at once now works.** Before this, a batch started with
"Runs at once" above 1 could finish with only one run passing and the rest failing with a
file-lock error mentioning `readvars.audit`.

- *Cause:* with `-r`, EnergyPlus runs ReadVarsESO to make the CSV. ReadVarsESO writes its log,
  `readvars.audit`, into the folder EnergyPlus was **started from**, not the run's output folder,
  and EnergyPlus then moves it to `eplusout.rvaudit`. eptools started every run from the same
  folder, so runs going at the same time fought over one file, and Windows won't let a second
  program move a file that another one still has open.
- *Fix:* each run now starts EnergyPlus inside its own run folder (`cwd` in `runner.simulate`).
- The simulation itself had finished in the failed runs, but EnergyPlus marks the whole run as
  terminated, so re-run them. Tick **Skip runs that already finished** to re-run only the failed ones.
- The warning checker now recognises this error and explains it, for runs made by other scripts.

**Occupied unmet heating hours.** Results, the warning report and the Explain warnings table now
show two figures, both read from the "Time Setpoint Not Met" table of the HTML report:

- `unmet_heating_h`: every hour the heating setpoint wasn't reached, including nights and
  empty-house hours.
- `unmet_occupied_heating_h`: only the hours when the zone is occupied. Use this one for comfort.

Both are read by column name, and fractional hours such as 0.5 are no longer rounded to 0.

## 0.2.0

Desktop window with four tabs: Explain warnings, Parameter study, Results, Model browser.
