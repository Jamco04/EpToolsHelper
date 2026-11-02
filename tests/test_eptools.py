"""Run with:  python -m unittest discover tests"""

import csv
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DATA = ROOT / "tests" / "data"

from eptools import check, runner  # noqa: E402
from eptools.eso import ESO  # noqa: E402
from eptools.idf_edit import IDF  # noqa: E402


class IDFEditTests(unittest.TestCase):
    def setUp(self):
        self.idf = IDF.load(DATA / "tiny.idf")

    def test_untouched_file_round_trips_exactly(self):
        self.assertEqual(self.idf.to_text(), self.idf.text)

    def test_set_by_field_name_and_number(self):
        self.idf.set_field("Material", "EPS*", "Thickness", 0.15)
        self.idf.set_field("Timestep", "*", 1, 4)
        again = IDF.parse(self.idf.to_text())
        self.assertEqual(again.find("Material")[0].get("Thickness {m}"), "0.15")
        self.assertEqual(again.find("Timestep")[0].values, ["4"])

    def test_multiply_and_replace(self):
        self.idf.multiply_field("ZoneInfiltration:DesignFlowRate", "*", "Design Flow Rate", 0.5)
        hits = self.idf.replace_value("Schedule:Compact", "*Heating Setpoint*", 21, 20)
        again = IDF.parse(self.idf.to_text())
        self.assertEqual(again.find("ZoneInfiltration:DesignFlowRate")[0].get("Design Flow Rate"), "0.01")
        self.assertEqual(len(hits), 1)
        self.assertIn("20", again.find("Schedule:Compact")[0].values)
        self.assertNotIn("21", again.find("Schedule:Compact")[0].values)

    def test_unknown_field_is_an_error(self):
        with self.assertRaises(KeyError):
            self.idf.set_field("Material", "*", "Thicknes", 0.1)

    def test_no_match_returns_nothing(self):
        self.assertEqual(self.idf.set_field("Material", "Brick*", "Thickness", 0.1), [])


class CheckTests(unittest.TestCase):
    def test_real_bungalow_err(self):
        rep = check.parse_err(DATA / "bungalow.err")
        self.assertTrue(rep.completed)
        self.assertEqual((rep.n_warnings, rep.n_severe), (49, 0))
        ids = {m.rule.id for m in rep.messages}
        self.assertNotIn("unknown", ids)
        self.assertEqual(rep.recurring["Temperature out of range [-100. to 200.] (PsyPsatFnTemp)"]["Sizing"], 8)

    def test_folder_summary(self):
        with tempfile.TemporaryDirectory() as d:
            shutil.copy(DATA / "bungalow.err", Path(d) / "eplusout.err")
            shutil.copy(DATA / "bungalow_table.html", Path(d) / "eplustbl.htm")
            shutil.copy(DATA / "bungalow.rvaudit", Path(d) / "eplusout.rvaudit")
            res = check.summarise(Path(d))
        self.assertEqual(res.status, "OK")
        self.assertAlmostEqual(res.numbers["heating_kwh"], 10574.11, places=2)
        self.assertEqual(res.numbers["unmet_heating_h"], 487.0)
        self.assertTrue(any("255" in n for n in res.file_notes))

    def test_occupied_unmet_hours_read_by_column_name(self):
        # Like Eoghan's run: 141 h unmet in total but only 0.5 h while the house is occupied.
        doc = (DATA / "bungalow_table.html").read_text(errors="replace")
        facility = ('<td align="right">Facility</td>\n    <td align="right">     487.00</td>\n'
                    '    <td align="right">       0.00</td>\n    <td align="right">       0.00</td>')
        self.assertIn(facility, doc)
        doc = doc.replace(facility, facility.replace("487.00", "141.00").rsplit("0.00", 1)[0] + "0.50</td>")
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "eplustbl.htm"
            p.write_text(doc)
            n = check.read_table(p)
        self.assertEqual(n["unmet_heating_h"], 141.0)
        self.assertEqual(n["unmet_occupied_heating_h"], 0.5)

    def test_readvars_lock_is_explained(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "eplusout.err"
            p.write_text("Program Version,EnergyPlus, Version 26.2.0\n"
                         "   ** Severe  ** filesystem error: cannot remove: The process cannot access the file "
                         "because it is being used by another process. [readvars.audit]\n"
                         "   ************* EnergyPlus Terminated--Fatal Error Detected. 0 Warning; 1 Severe Errors;\n")
            res = check.summarise(p)
        self.assertEqual(res.groups[0].rule.id, "readvars-lock")

    def test_failed_run(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "eplusout.err"
            p.write_text("Program Version,EnergyPlus, Version 26.2.0\n"
                         '   ** Severe  ** Version: in IDF="22.1" not the same as expected="26.2"\n'
                         "   **  Fatal  ** IP: Errors occurred on processing input file.\n"
                         "   ************* EnergyPlus Terminated--Fatal Error Detected. 0 Warning; 1 Severe Errors;\n")
            res = check.summarise(p)
        self.assertEqual(res.status, "FAILED")
        self.assertEqual(res.groups[0].rule.id, "version")


class RunnerTests(unittest.TestCase):
    def test_study_end_to_end_with_fake_energyplus(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            study = {
                "energyplus": str(ROOT / "tests" / "fake_energyplus.py"),
                "weather": str(DATA / "tiny.epw"),
                "output_dir": str(d / "runs"),
                "models": {"tiny": str(DATA / "tiny.idf")},
                "fixed": [{"object": "Timestep", "field": 1, "set": 4}],
                "parameters": {
                    "window_u": {"object": "WindowMaterial:SimpleGlazingSystem", "field": "U-Factor", "values": [3.0, 1.4]},
                    "setpoint": {"object": "Schedule:Compact", "name": "*Heating*", "replace": 21, "values": [20, 21]},
                },
                "outputs": {"replace_existing": True,
                            "add": ["Output:Variable,*,Zone Ideal Loads Supply Air Total Heating Energy,Hourly;"]},
                "collect": {"hourly": ["Zone Ideal Loads Supply Air Total Heating Energy"]},
            }
            cfg = d / "study.json"
            cfg.write_text(json.dumps(study))
            self.assertEqual(runner.run_study(str(cfg)), 0)
            rows = list(csv.DictReader(open(d / "runs" / "results.csv")))
            self.assertEqual(len(rows), 4)
            by_u = {r["window_u"]: float(r["zone_ideal_loads_supply_air_total_heating_energy_annual_kwh"]) for r in rows}
            self.assertLess(by_u["1.4"], by_u["3.0"])
            text = (d / "runs" / "run_0001" / "in.idf").read_text(encoding="latin-1")
            self.assertNotIn("Zone Mean Air Temperature,Monthly", text)
            self.assertTrue((d / "runs" / "check_report.html").exists())

    def test_parallel_runs_do_not_share_readvars_audit(self):
        # EnergyPlus -r leaves readvars.audit in the folder it was started from. If every run
        # starts from the same folder, parallel runs fight over that one file and all but one fail.
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            study = runner.Study.from_dict({
                "energyplus": str(ROOT / "tests" / "fake_energyplus.py"),
                "weather": str(DATA / "tiny.epw"),
                "output_dir": str(d / "runs"),
                "models": {"tiny": str(DATA / "tiny.idf")},
                "parameters": {"window_u": {"object": "WindowMaterial:SimpleGlazingSystem",
                                            "field": "U-Factor", "values": [3.0, 2.0, 1.4, 1.0]}},
                "jobs": 4,
            }, d)
            self.assertEqual(runner.run_study(study, log=lambda t: None), 0)
            with open(d / "runs" / "results.csv", newline="") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual([r["status"] for r in rows], ["OK"] * 4)
            for n in range(1, 5):
                self.assertTrue((d / "runs" / f"run_{n:04d}" / "eplusout.rvaudit").exists())
            self.assertFalse((Path.cwd() / "readvars.audit").exists())

    def test_results_include_occupied_unmet_hours(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            study = runner.Study.from_dict({
                "energyplus": str(ROOT / "tests" / "fake_energyplus.py"),
                "weather": str(DATA / "tiny.epw"), "output_dir": str(d / "runs"),
                "models": {"tiny": str(DATA / "tiny.idf")}}, d)
            runner.run_study(study, log=lambda t: None)
            with open(d / "runs" / "results.csv", newline="") as f:
                row = next(csv.DictReader(f))
            self.assertEqual(row["unmet_heating_h"], "487.0")
            self.assertEqual(row["unmet_occupied_heating_h"], "0.0")

    def test_typo_stops_before_running(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "study.json"
            cfg.write_text(json.dumps({
                "weather": str(DATA / "tiny.epw"), "output_dir": str(Path(d) / "runs"),
                "models": [str(DATA / "tiny.idf")],
                "parameters": {"u": {"object": "WindowMaterial:SimpleGlazingSystem", "name": "Simple 9",
                                     "field": "U-Factor", "values": [1.4]}}}))
            with self.assertRaises(runner.StudyError):
                runner.run_study(str(cfg))


class ESOTests(unittest.TestCase):
    def test_meters_and_variables(self):
        with tempfile.TemporaryDirectory() as d:
            import subprocess
            out = Path(d)
            subprocess.run([sys.executable, str(ROOT / "tests" / "fake_energyplus.py"), "-d", str(out), str(DATA / "tiny.idf")], check=True)
            eso = ESO.load(out / "eplusout.eso")
        self.assertEqual(len(eso.find("Ideal Loads", "Hourly")), 2)
        meter = eso.find("DistrictHeatingWater:Facility")[0]
        self.assertEqual(meter.key, "Meter")
        self.assertEqual(len(eso.series(meter.id)), 24)


class CostTests(unittest.TestCase):
    def test_night_hours_follow_clock_time(self):
        from eptools.costs import Tariffs, local_hour, run_cost
        t = Tariffs(cop=2.0, flat=0.30, day=0.40, night=0.20, night_start=23, night_end=8)
        self.assertTrue(t.is_night(23) and t.is_night(7) and not t.is_night(8))
        self.assertEqual(local_hour("07-01 22:00", "1"), 23)   # 22:00 standard time = 23:00 summer clock
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "hourly.csv"
            p.write_text('"timestamp (standard time, interval start)",dst' ",DistrictHeatingWater:Facility [kWh]\n"
                         "01-01 03:00,0,4\n01-01 12:00,0,4\n")
            c = run_cost(p, t)
        self.assertEqual(c["hp_electricity_kwh"], 4)
        self.assertEqual(c["night_share_pct"], 50.0)
        self.assertAlmostEqual(c["cost_day_night_eur"], round(2 * 0.20 + 2 * 0.40))


class GuiSmokeTest(unittest.TestCase):
    """Opens the window, loads the example .err and closes again. Skipped without a display."""

    def test_window_opens_and_checks_a_file(self):
        try:
            import tkinter as tk
            root = tk.Tk()
        except Exception as exc:  # no tkinter or no display
            self.skipTest(f"no GUI available: {exc}")
        try:
            from eptools import gui
            app = gui.App(root)
            app.check_tab.add_targets([str(DATA / "bungalow.err")])
            root.update()
            self.assertEqual(len(app.check_tab.results), 1)
            self.assertEqual(app.check_tab.results[0].status, "OK")
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
