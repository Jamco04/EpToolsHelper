"""Run a matrix of EnergyPlus simulations and collect the results.

The study is described in a JSON file (see examples/study.json). For every
combination of model and parameter values the runner:

  1. copies the model, applies the edits and checks that every edit hit at least
     one object (a typo in an object name stops the study before anything runs),
  2. writes the edited IDF, params.json and changes.csv into its own run folder,
  3. runs EnergyPlus there,
  4. checks the run with eptools.check and reads the results,

then writes results.csv (one row per run) and check_report.html for the study.
"""

from __future__ import annotations

import csv
import itertools
import json
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from . import check
from .eso import ESO, stamp_text, to_kwh
from .idf_edit import IDF


class StudyError(Exception):
    pass


# --------------------------------------------------------------------------- config


@dataclass
class Edit:
    """One change to an IDF: set, multiply, replace a value, or fill a placeholder."""
    object: str = ""
    name: str = "*"
    field: str | int = ""
    action: str = "set"            # set | multiply | replace | placeholder
    old: str | None = None         # value to look for when action == replace
    placeholder: str = ""

    @classmethod
    def from_spec(cls, spec: dict) -> tuple["Edit", list]:
        """Build an Edit from a parameter (or fixed) entry and return it with its values."""
        if "placeholder" in spec:
            e = cls(action="placeholder", placeholder=spec["placeholder"])
        else:
            if "object" not in spec:
                raise StudyError(f"Each edit needs an 'object' (an IDF class such as Material): {spec}")
            e = cls(object=spec["object"], name=spec.get("name", "*"), field=spec.get("field", "*"))
            if "multiply" in spec:
                e.action = "multiply"
            elif "replace" in spec:
                e.action, e.old = "replace", str(spec["replace"])
            elif not e.field or e.field == "*":
                raise StudyError(f"Edit of {e.object} '{e.name}' needs a 'field' (name or number)")
        values = spec.get("multiply", spec.get("values", spec.get("value", spec.get("set"))))
        if values is None:
            raise StudyError(f"Edit {spec} needs 'values' (or 'multiply' for factors)")
        return e, values if isinstance(values, list) else [values]

    def apply(self, idf: IDF, value):
        if self.action == "set":
            return idf.set_field(self.object, self.name, self.field, value)
        if self.action == "multiply":
            return idf.multiply_field(self.object, self.name, self.field, value)
        if self.action == "replace":
            return idf.replace_value(self.object, self.name, self.old, value, self.field or "*")
        raise AssertionError(self.action)

    def describe(self) -> str:
        if self.action == "placeholder":
            return f"placeholder {self.placeholder}"
        what = {"set": "set", "multiply": "multiply", "replace": f"replace {self.old} in"}[self.action]
        fld = "" if self.action == "replace" and self.field in ("", "*") else f" field '{self.field}'"
        return f"{what} {self.object} '{self.name}'{fld}"


@dataclass
class Study:
    root: Path
    energyplus: str
    weather: Path
    output_dir: Path
    models: dict[str, Path]
    parameters: dict[str, tuple[Edit, list]]
    fixed: list[tuple[Edit, object]]
    outputs: dict
    collect_hourly: list[str]
    jobs: int = 1
    readvars: bool = True
    runs_csv: Path | None = None

    @classmethod
    def load(cls, path) -> "Study":
        path = Path(path).resolve()
        return cls.from_dict(json.loads(path.read_text()), path.parent)

    @classmethod
    def from_dict(cls, cfg: dict, root: Path) -> "Study":
        """Build a study from the same structure as the JSON file; relative paths resolve against root."""
        root = Path(root)
        rel = lambda p: (root / p).resolve() if not Path(p).is_absolute() else Path(p)
        models = cfg.get("models")
        if isinstance(models, list):
            models = {Path(m).stem: m for m in models}
        if not models:
            raise StudyError("The study needs at least one entry under 'models'")
        params = {k: Edit.from_spec(v) for k, v in cfg.get("parameters", {}).items()}
        fixed = []
        for spec in cfg.get("fixed", []):
            e, vals = Edit.from_spec(spec)
            fixed.append((e, vals[0]))
        return cls(
            root=root,
            energyplus=_exe(cfg.get("energyplus", "energyplus"), rel),
            weather=rel(cfg.get("weather") or _missing("weather")),
            output_dir=rel(cfg.get("output_dir", "runs")),
            models={k: rel(v) for k, v in models.items()},
            parameters=params,
            fixed=fixed,
            outputs=cfg.get("outputs", {}),
            collect_hourly=cfg.get("collect", {}).get("hourly", []),
            jobs=int(cfg.get("jobs", 1)),
            readvars=bool(cfg.get("readvars", True)),
            runs_csv=rel(cfg["runs_csv"]) if cfg.get("runs_csv") else None,
        )

    def combinations(self) -> list[dict]:
        """Every run as {'model': ..., param: value, ...}."""
        if self.runs_csv:
            with open(self.runs_csv, newline="") as f:
                rows = list(csv.DictReader(f))
            for r in rows:
                if r.get("model") not in self.models:
                    raise StudyError(f"runs.csv row {r} names a model that isn't in 'models'")
                for k in self.parameters:
                    if k not in r:
                        raise StudyError(f"runs.csv has no column for parameter '{k}'")
            return [{"model": r["model"], **{k: _num(r[k]) for k in self.parameters}} for r in rows]
        names = list(self.parameters)
        grids = [self.parameters[n][1] for n in names]
        return [{"model": m, **dict(zip(names, combo))}
                for m in self.models for combo in itertools.product(*grids)]


def _missing(key: str):
    raise StudyError(f"The study needs a '{key}' entry")


def _exe(path: str, rel) -> str:
    """A bare command name is looked up on PATH; anything with a folder in it is a path."""
    return str(rel(path)) if ("/" in path or "\\" in path) else path


def _num(v):
    try:
        f = float(v)
        return int(f) if f.is_integer() and "." not in str(v) else f
    except (TypeError, ValueError):
        return v


# --------------------------------------------------------------------------- prepare


@dataclass
class Run:
    id: str
    model: str
    params: dict
    folder: Path
    idf_text: str = ""
    changes: list = field(default_factory=list)


def prepare(study: Study) -> list[Run]:
    """Build every edited IDF in memory. Raises StudyError if any edit matches nothing."""
    base = {name: IDF.load(p).text for name, p in study.models.items()}
    runs = []
    problems = []
    for n, combo in enumerate(study.combinations(), start=1):
        model = combo["model"]
        params = {k: v for k, v in combo.items() if k != "model"}
        run = Run(f"run_{n:04d}", model, params, study.output_dir / f"run_{n:04d}")
        text = base[model]
        for pname, (edit, _) in study.parameters.items():
            if edit.action == "placeholder":
                if edit.placeholder not in text:
                    problems.append(f"{model}: placeholder {edit.placeholder} (parameter '{pname}') is not in the model")
                text = text.replace(edit.placeholder, str(params[pname]))
                run.changes.append((pname, "placeholder", edit.placeholder, "", params[pname]))
        idf = IDF.parse(text)
        for edit, value in study.fixed:
            hits = edit.apply(idf, value)
            if not hits:
                problems.append(f"{model}: fixed edit '{edit.describe()}' matched nothing")
            run.changes += [("(fixed)", edit.object, obj, old, new) for obj, old, new in hits]
        for pname, (edit, _) in study.parameters.items():
            if edit.action == "placeholder":
                continue
            try:
                hits = edit.apply(idf, params[pname])
            except (KeyError, ValueError) as exc:
                problems.append(f"{model}: parameter '{pname}' ({edit.describe()}): {exc}")
                continue
            if not hits:
                problems.append(f"{model}: parameter '{pname}' ({edit.describe()}) matched nothing")
            run.changes += [(pname, edit.object, obj, old, new) for obj, old, new in hits]
        _apply_outputs(idf, study.outputs)
        run.idf_text = idf.to_text()
        runs.append(run)
    if problems:
        unique = list(dict.fromkeys(problems))
        raise StudyError("Some edits did not apply:\n  - " + "\n  - ".join(unique))
    return runs


def _apply_outputs(idf: IDF, outputs: dict) -> None:
    if outputs.get("replace_existing"):
        for cls in ("Output:Variable", "Output:Meter", "Output:Meter:MeterFileOnly",
                    "Output:Meter:Cumulative", "Output:Meter:Cumulative:MeterFileOnly"):
            idf.remove(cls)
    if outputs.get("sqlite") and not idf.find("Output:SQLite"):
        idf.add("Output:SQLite, SimpleAndTabular;")
    for line in outputs.get("add", []):
        idf.add(line if line.rstrip().endswith(";") else line + ";")


# --------------------------------------------------------------------------- run


def energyplus_version(exe: str) -> str:
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=60,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StudyError(f"Could not start EnergyPlus at '{exe}': {exc}. Set 'energyplus' in the study file "
                         f"to the full path of energyplus.exe (Windows) or energyplus (Mac/Linux).")
    m = re.search(r"Version (\d+\.\d+)", out.stdout + out.stderr)
    return m.group(1) if m else ""


def model_version(text: str) -> str:
    v = IDF.parse(text).find("Version")
    return v[0].values[0] if v and v[0].values else ""


def write_inputs(run: Run) -> None:
    run.folder.mkdir(parents=True, exist_ok=True)
    (run.folder / "in.idf").write_text(run.idf_text, encoding="latin-1")
    (run.folder / "params.json").write_text(json.dumps({"model": run.model, **run.params}, indent=2))
    with open(run.folder / "changes.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["parameter", "object_type", "object_name", "old_value", "new_value"])
        w.writerows(run.changes)


def already_done(run: Run) -> bool:
    err, idf = run.folder / "eplusout.err", run.folder / "in.idf"
    return (err.exists() and idf.exists() and "Completed Successfully" in err.read_text(errors="replace")
            and idf.read_text(encoding="latin-1") == run.idf_text)


def simulate(study: Study, run: Run, timeout: int) -> str:
    # EnergyPlus refuses to overwrite some files from an earlier run, and stale
    # files look exactly like new results, so each run starts from a clean folder.
    if run.folder.exists():
        shutil.rmtree(run.folder)
    write_inputs(run)
    folder = run.folder.resolve()
    cmd = [study.energyplus, "-w", str(Path(study.weather).resolve()), "-d", str(folder)]
    if study.readvars:
        cmd.append("-r")
    cmd.append(str(folder / "in.idf"))
    try:
        # CREATE_NO_WINDOW stops a console window flashing up for every run when
        # the study is started from the desktop window on Windows.
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        # cwd=folder matters when runs go in parallel. EnergyPlus -d only moves the main
        # outputs: ReadVarsESO (-r) writes readvars.audit into the folder EnergyPlus was
        # started from, and EnergyPlus then moves it to eplusout.rvaudit (ExpandObjects and
        # EPMacro also use that folder). If every run starts from the same folder, parallel
        # runs fight over one readvars.audit and, on Windows, all but one fail with a
        # "file in use" error. Starting each run in its own folder keeps them apart.
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, creationflags=flags,
                           cwd=folder)
        (run.folder / "stdout.txt").write_text(p.stdout + "\n" + p.stderr)
        return "ran" if p.returncode == 0 else f"exit code {p.returncode}"
    except subprocess.TimeoutExpired:
        return f"timed out after {timeout}s"


# --------------------------------------------------------------------------- collect


def collect(study: Study, run: Run, how: str) -> tuple[dict, check.RunResult | None]:
    row = {"run": run.id, "model": run.model, **run.params, "energyplus": how}
    if how == "cancelled":
        row["status"] = "CANCELLED"
        return row, None
    err = run.folder / "eplusout.err"
    if not err.exists():
        row["status"] = "NO OUTPUT"
        return row, None
    res = check.summarise(run.folder)
    res.name = run.id
    row.update(status=res.status, warnings=res.err.n_warnings, severe=res.err.n_severe)
    for k in ("heating_kwh", "hot_water_kwh", "floor_area_m2", "heating_kwh_m2",
              "unmet_heating_h", "unmet_occupied_heating_h"):
        v = res.numbers.get(k)
        row[k] = round(v, 2) if isinstance(v, float) else v
    eso_path = run.folder / "eplusout.eso"
    if study.collect_hourly and eso_path.exists():
        row.update(_hourly(ESO.load(eso_path), study.collect_hourly, run.folder / "hourly.csv"))
    row["folder"] = str(run.folder)
    return row, res


def _hourly(eso: ESO, names: list[str], out_path: Path) -> dict:
    """Sum each requested hourly output over all keys (zones), save hourly.csv, return totals and peaks."""
    columns, summary = {}, {}
    for name in names:
        found = eso.find(name, "Hourly")
        if not found:
            summary[f"{_slug(name)}_annual_kwh"] = "not in output"
            continue
        # Only energy can be added up across zones; temperatures etc. stay in the .eso/.csv.
        found = [v for v in found if v.units in ("J", "W")]
        if not found:
            continue
        total: dict = {}
        for var in found:
            for stamp, value in eso.series(var.id):
                total[stamp] = total.get(stamp, 0.0) + to_kwh(value, var.units)
        columns[name] = total
        summary[f"{_slug(name)}_annual_kwh"] = round(sum(total.values()), 1)
        summary[f"{_slug(name)}_peak_kw"] = round(max(total.values(), default=0.0), 3)
    if columns:
        stamps = sorted(set().union(*[c.keys() for c in columns.values()]))
        with open(out_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["timestamp (standard time, interval start)", "dst"] + [f"{n} [kWh]" for n in columns])
            for s in stamps:
                w.writerow([stamp_text(s), s[4]] + [round(columns[n].get(s, 0.0), 5) for n in columns])
    return summary


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


# --------------------------------------------------------------------------- main entry


def run_study(config, dry_run=False, jobs=None, resume=False, timeout=3600, limit=None,
              log=print, on_run=None, cancel=None) -> int:
    """Run a study. config is a JSON path or a Study.

    log(text) receives progress lines; on_run(done, total, row) is called after each run;
    setting the threading.Event cancel stops runs that haven't started yet.
    """
    study = config if isinstance(config, Study) else Study.load(config)
    if not study.weather.exists():
        raise StudyError(f"Weather file not found: {study.weather}")
    for name, p in study.models.items():
        if not p.exists():
            raise StudyError(f"Model '{name}' not found: {p}")

    runs = prepare(study)
    if limit:
        runs = runs[:limit]
    log(f"{len(runs)} runs: {len(study.models)} model(s) x "
          f"{' x '.join(str(len(v)) for _, v in study.parameters.values()) or '1'} parameter combinations")
    for pname, (edit, values) in study.parameters.items():
        hits = sum(1 for c in runs[0].changes if c[0] == pname)
        log(f"  {pname}: {edit.describe()} -> {values}  ({hits} object(s) changed in run_0001)")

    if dry_run:
        for run in runs:
            write_inputs(run)
        log(f"Dry run: wrote in.idf, params.json and changes.csv for each run under {study.output_dir}")
        return 0

    ep_ver = energyplus_version(study.energyplus)
    for name, p in study.models.items():
        mv = model_version(IDF.load(p).text)
        if ep_ver and mv and mv.split(".")[:2] != ep_ver.split(".")[:2]:
            raise StudyError(f"Model '{name}' is version {mv} but EnergyPlus is {ep_ver}. "
                             f"Convert it with IDFVersionUpdater first.")
    log(f"EnergyPlus {ep_ver}, {jobs or study.jobs} at a time")

    study.output_dir.mkdir(parents=True, exist_ok=True)
    rows, results = {}, {}
    workers = jobs or study.jobs

    def one(run: Run):
        if cancel is not None and cancel.is_set():
            return run, "cancelled"
        how = "skipped (already done)" if resume and already_done(run) else simulate(study, run, timeout)
        return run, how

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(one, r) for r in runs]
        for fut in as_completed(futures):
            run, how = fut.result()
            row, res = collect(study, run, how)
            rows[run.id], results[run.id] = row, res
            if on_run:
                on_run(len(rows), len(runs), row)
            log(f"  {run.id} {run.model:<20} {row.get('status', ''):<9} "
                  f"heating={row.get('heating_kwh', '')} kWh  warnings={row.get('warnings', '')}")

    ordered = [rows[r.id] for r in runs]
    fields = list(dict.fromkeys(k for row in ordered for k in row))
    with open(study.output_dir / "results.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(ordered)
    checked = [results[r.id] for r in runs if results[r.id]]
    if checked:
        check.write_html(checked, study.output_dir / "check_report.html")
    bad = [r["run"] for r in ordered if r.get("status") in ("FAILED", "NO OUTPUT")]
    log(f"\nResults: {study.output_dir / 'results.csv'}")
    log(f"Warnings explained: {study.output_dir / 'check_report.html'}")
    if bad:
        log(f"{len(bad)} run(s) failed: {', '.join(bad)}. Open their eplusout.err before using any results.")
    return 1 if bad else 0


def inspect(model: str, cls: str | None = None, name: str = "*") -> None:
    idf = IDF.load(model)
    if not cls:
        for c, n in sorted(idf.classes().items()):
            print(f"{n:5d}  {c}")
        return
    objs = idf.find(cls, name)
    if not objs:
        print(f"No {cls} objects named '{name}' in {model}")
    for o in objs:
        print(f"\n{o.cls}")
        for i, (v, n) in enumerate(zip(o.values, o.names), start=1):
            print(f"  {i:3d}  {v:<40} {n}")


if __name__ == "__main__":
    sys.exit(run_study(sys.argv[1]))
