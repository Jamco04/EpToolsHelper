# eptools

A desktop app that helps you run EnergyPlus models many times with different inputs, and
understand what comes out. Every file is picked with a Browse button, so no coding is needed.

- **Explain warnings:** labels each warning in an `.err` file **FIX**, **CHECK** or **IGNORE** and explains it in plain English.
- **Parameter study:** changes chosen inputs (insulation, window U-value, setpoints…) and runs every combination.
- **Results:** one row per run with heating, hot water, unmet hours and warnings, plus a heat pump cost estimate.
- **Model browser:** shows every object and field in a model.

Nothing is sent over the internet. All checks are fixed rules run on your own computer.

## Install (once)

1. Install [EnergyPlus](https://github.com/NatLabRockies/EnergyPlus/releases).
2. Download **`eptools.exe`** from the [latest release](../../releases/latest).

That's it. Double-click `eptools.exe` to open the app.

> No exe, or not on Windows? Install [Python](https://www.python.org/downloads/) (tick
> "Add python.exe to PATH"), download this repo with **Code → Download ZIP**, unzip it and
> double-click **`Start eptools.bat`**. On a Mac or Linux run `python3 -m eptools` in the
> unzipped folder.

## Use it

**Check a run you already have**

1. Open the **Explain warnings** tab and press **Add .err files…** (or **Add run folder…**).
2. Pick a row on the left to read what each warning means and what to do about it.

**Run a parameter study**

1. Open the **Parameter study** tab. Browse to the **weather file** (`.epw`) and a folder to **save runs in**.
   EnergyPlus is found automatically. If it isn't, Browse to `energyplus.exe`.
2. Under **Models**, press **Add…** and pick your `.idf` files.
3. Under **Parameters**, press **Add…**. Choose the object type, object name and field, and type
   the values separated by commas (e.g. `0.05, 0.1, 0.15`). The dialog shows the current value,
   so you can see straight away if you picked the wrong thing.
4. Press **Check edits** to test without simulating, then **Run study**.
5. The **Results** tab opens when it's done. Double-click a run to see its warnings, or press
   **Heat pump cost…** for electricity and tariff costs.

Use **Save study…** to keep the set-up and **Open study…** to load it again.

> **Tip:** all models must be on the same version as your EnergyPlus. The Model browser shows
> each model's version. Update older ones with **IDFVersionUpdater**, which is in the
> `PreProcess` folder of the EnergyPlus install.

## Good to know

- With ideal loads heating, heating and hot water are reported as `DistrictHeatingWater`.
  That is heat delivered, not electricity.
- Labels are guidance. If unsure about a warning, search the message on [Unmet Hours](https://unmethours.com/).
- Each run's folder has `changes.csv` listing exactly what was changed in the model.

Command-line use, the study file format and how to add new warning rules are in
[docs/REFERENCE.md](docs/REFERENCE.md).
