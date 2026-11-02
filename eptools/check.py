#!/usr/bin/env python3
"""Explain an EnergyPlus run's warnings and flag runs that went wrong.

Point it at one or more run folders (or .err files). For each run it:
  * reads the .err file, groups the messages and labels each group
    FIX / CHECK / IGNORE with a plain-English explanation,
  * reads the .rvaudit file to spot a truncated CSV,
  * reads the HTML summary table for heating, hot water, floor area and unmet hours,
  * optionally compares everything against a baseline run.

Usage:
    python -m eptools check RUN [RUN ...] [--baseline RUN] [--html report.html] [--quiet]

RUN can be a folder that holds one EnergyPlus run, or a path to an .err file.
Only the Python standard library is used.
"""

from __future__ import annotations

import argparse
import html
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

FIX, CHECK, IGNORE = "FIX", "CHECK", "IGNORE"
ORDER = {FIX: 0, CHECK: 1, IGNORE: 2}

# ---------------------------------------------------------------------------
# Rules: first match wins. Each pattern is tested against the first line of a
# message (without the "** Warning **" prefix). Add to this list as you meet
# new messages.
# ---------------------------------------------------------------------------


@dataclass
class Rule:
    id: str
    pattern: str
    verdict: str
    title: str
    meaning: str
    action: str
    regex: re.Pattern = field(init=False)

    def __post_init__(self):
        self.regex = re.compile(self.pattern, re.IGNORECASE)


RULES = [
    Rule("readvars-lock", r"readvars\.audit", FIX,
         "Two runs clashed over ReadVarsESO's readvars.audit file",
         "The simulation itself finished, but the step that writes the CSV (ReadVarsESO) failed. ReadVarsESO always "
         "writes readvars.audit into the folder EnergyPlus was started from, not the output folder. When several runs "
         "start from the same folder at once, they fight over that one file and Windows locks out all but one.",
         "Re-run this case. Start each parallel run from its own folder (eptools 0.2.1 and later do this), "
         "or run one at a time. Don't use the numbers until the re-run passes."),
    Rule("version", r"Version: in IDF=.*not the same as expected", FIX,
         "Model is from a different EnergyPlus version",
         "The IDF was written for another version of EnergyPlus. Field layouts change between versions, so the file may be misread.",
         "Convert it with IDFVersionUpdater (PreProcess/IDFVersionUpdater in the EnergyPlus folder). Don't just edit the version number."),
    Rule("ip-range", r"^IP: |Out of .?range.? value|Invalid Numerics|blank required field|Missing required property|is not a valid|Value type", FIX,
         "Input value invalid or out of range",
         "A field holds a value outside its allowed range, text where a number should be, or is blank when it must be filled. In a parametric run this usually means the script wrote a bad value.",
         "The continuation lines name the object and field. Check the value your script substituted."),
    Rule("meter-missing", r"Output:Meter: invalid Key Name", IGNORE,
         "Requested meter doesn't exist",
         "The IDF asks for a meter that the model never creates, for example a gas meter in an all-electric house or a DesignBuilder end-use meter for equipment that isn't there.",
         "Ignore unless it is a meter you need. Delete the request to tidy the file. Check names in the .mdd file."),
    Rule("not-found", r"(not found|was not found|invalid .*entered|invalid .* name)", FIX,
         "A name refers to something that doesn't exist",
         "An object refers by name to a construction, material, schedule or zone that isn't in the file, often after a rename or a typo.",
         "Search the IDF for the name given and make the reference match exactly."),
    Rule("timestep", r"Timestep: Requested number \((\d+)\) is less than the suggested minimum", CHECK,
         "Timestep is coarser than recommended",
         "The model runs fewer timesteps per hour than EnergyPlus suggests (4 or more). Results are usually close, but heat balance and controls are less accurate.",
         "Run the base model once at Timestep 4 or 6 and compare annual heating. If the difference is small, keep the faster setting and say so in the method."),
    Rule("reverse-construction", r"does not have the same materials in the reverse order", IGNORE,
         "Partition constructions don't mirror exactly",
         "The two sides of an internal wall use constructions that are not exact mirror images. EnergyPlus checks the U-values and carries on when they match.",
         "Ignore when the continuation says 'Nominal U values are similar'. Fix only if they differ."),
    Rule("floor-area", r"Entered Zone Floor Area\(s\) differ", CHECK,
         "Entered floor areas differ from the geometry",
         "DesignBuilder writes each zone's floor area into the IDF using internal dimensions, while the drawn surfaces use a different line. EnergyPlus uses the entered area for W/m2 loads and per-area results.",
         "Not an error. Be consistent: quote kWh/m2 using the 'Total Building Area' in the HTML summary."),
    Rule("volume", r"Entered Zone Volumes differ", CHECK,
         "Entered zone volumes differ from the geometry",
         "Same cause as the floor-area message: DesignBuilder enters volumes from internal dimensions. Air volume affects infiltration in ACH and the zone air heat capacity.",
         "Not an error for these models. Only investigate if you change geometry."),
    Rule("unused-constructions", r"nominally unused constructions", IGNORE,
         "Some constructions are defined but not used",
         "The file contains constructions that no surface uses. They don't affect results.",
         "Ignore, unless a construction you changed in a parametric run is one of the unused ones, in which case your change did nothing."),
    Rule("comfort-schedules", r"has comfort related schedules but no thermal comfort model", IGNORE,
         "Comfort schedules without a comfort model",
         "DesignBuilder fills in clothing and air-velocity schedules for each People object but no comfort model is switched on, so they go unused.",
         "Ignore. These schedules don't affect energy results."),
    Rule("zero-cooling", r"Calculated design cooling load for zone=.* is zero", IGNORE,
         "No cooling needed on the summer design day",
         "Sizing found no cooling load. Irish dwellings without cooling setpoints are expected to show this.",
         "Ignore for heating-only studies."),
    Rule("zero-heating", r"Calculated design heating load for zone=.* is zero", CHECK,
         "No heating needed on the winter design day",
         "Sizing found no heating load for a zone that should need heat. That can point to a wrong thermostat schedule or design day.",
         "Check the zone's thermostat schedule and the winter design day."),
    Rule("psych-temp", r"Temperature out of range .*\(PsyPsatFnTemp\)", CHECK,
         "Impossible air temperature in a psychrometric calculation",
         "A routine was given an unrealistic temperature. In these models it comes from the ideal loads system (CalcPurchAirLoads) during the sizing calculation, before air flows are sized.",
         "Harmless if the recurring summary says every occurrence was 'during Sizing' and none during the run. If any happen during the run period, treat it as FIX."),
    Rule("vars-not-generated", r"Report Variables were requested but not generated", CHECK,
         "Requested outputs were not produced",
         "Some Output:Variable requests don't match anything in the model (no ventilation or mixing objects, for example) or are misspelt.",
         "Check that the variables you analyse are not in the list. Remove the rest. Copy names from the .rdd file."),
    Rule("unused-objects", r"unused (objects|schedules|week schedules|day schedules)", IGNORE,
         "Objects or schedules defined but not used",
         "Leftovers from the DesignBuilder export. They don't affect results.",
         "Ignore, unless a schedule you edited in a parametric run is unused."),
    Rule("weather-location", r"Weather file location will be used", IGNORE,
         "Weather file location overrides Site:Location",
         "The location in the EPW is used instead of the Site:Location object.",
         "Ignore if they describe the same place."),
    Rule("warmup", r"(did not converge|CheckWarmupConvergence|MaxNumberOfWarmupDays)", CHECK,
         "Warm-up did not converge",
         "EnergyPlus repeats the first day until temperatures settle. It ran out of warm-up days before that happened.",
         "Usually minor. If it appears in many runs, raise Maximum Number of Warmup Days in the Building object."),
    Rule("temp-bounds", r"(Temperature \(\w+\) out of bounds|zone air temperature.*out of|Zone Air Heat Balance)", FIX,
         "Zone heat balance failed",
         "A zone reached an impossible temperature. Usually a bad material property, schedule value or capacity.",
         "Look at the inputs changed for this run. Results from this run should not be used."),
    Rule("hvac-iter", r"(Maximum iterations|SimHVAC|exceeded)", CHECK,
         "HVAC solver hit its iteration limit",
         "The HVAC solution didn't converge in some timesteps.",
         "A few occurrences are common. Hundreds point to a control or sizing problem."),
]

SEVERE_RULE = Rule("severe", r".", FIX, "Severe error",
                   "EnergyPlus found something it considers wrong. Results from this run should not be trusted.",
                   "Read the message and its continuation lines, then look it up in the Input Output Reference or on unmethours.com.")
FATAL_RULE = Rule("fatal", r".", FIX, "Fatal error (run stopped)",
                  "The run stopped. The Severe messages before this line explain why.",
                  "Fix the first Severe message, then re-run.")
UNKNOWN_RULE = Rule("unknown", r".", CHECK, "Not in the rule list yet",
                    "This message isn't covered by eptools' rules yet.",
                    "Search the message on unmethours.com, then add a rule to RULES in eptools/check.py.")

# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

LEVEL_RE = re.compile(r"^\s*\*\*\s*(Warning|Severe|Fatal)\s*\*\*\s?(.*)$")
CONT_RE = re.compile(r"^\s*\*\*\s+~~~\s+\*\*\s?(.*)$")
STAR_RE = re.compile(r"^\s*\*{13}\s?(.*)$")
SUMMARY_RE = re.compile(r"EnergyPlus (Completed Successfully|Terminated).*?(\d+) Warning; (\d+) Severe")
RECUR_COUNT_RE = re.compile(r"occurred (\d+) total times")
RECUR_PHASE_RE = re.compile(r"during (Warmup|Sizing) (\d+) times")


@dataclass
class Message:
    level: str
    text: str
    detail: list[str] = field(default_factory=list)
    rule: Rule | None = None


@dataclass
class ErrReport:
    path: Path
    version: str = ""
    completed: bool | None = None
    n_warnings: int | None = None
    n_severe: int | None = None
    messages: list[Message] = field(default_factory=list)
    recurring: dict[str, dict] = field(default_factory=dict)


def classify(msg: Message) -> Rule:
    if msg.level == "Fatal":
        return FATAL_RULE
    for rule in RULES:
        if rule.regex.search(msg.text):
            return rule if msg.level == "Warning" or rule.verdict == FIX else SEVERE_RULE
    return SEVERE_RULE if msg.level == "Severe" else UNKNOWN_RULE


def parse_err(path: Path) -> ErrReport:
    rep = ErrReport(path)
    lines = path.read_text(errors="replace").splitlines()
    if lines and lines[0].startswith("Program Version"):
        m = re.search(r"Version ([\d.]+)", lines[0])
        rep.version = m.group(1) if m else ""
    current: Message | None = None
    in_recurring = False
    recur_key = None
    for line in lines:
        if "Recurring Error Summary" in line:
            in_recurring, current = True, None
            continue
        if "Final Error Summary" in line:
            in_recurring = False
        s = SUMMARY_RE.search(line)
        if s:
            rep.completed = s.group(1).startswith("Completed")
            rep.n_warnings, rep.n_severe = int(s.group(2)), int(s.group(3))
            continue
        if in_recurring:
            body = STAR_RE.match(line)
            inner = body.group(1) if body else line
            lv = LEVEL_RE.match(inner)
            if lv:
                recur_key = lv.group(2).strip()
                rep.recurring[recur_key] = {"total": 0, "Warmup": 0, "Sizing": 0}
            elif recur_key:
                c = RECUR_COUNT_RE.search(inner)
                if c:
                    rep.recurring[recur_key]["total"] = int(c.group(1))
                for phase, n in RECUR_PHASE_RE.findall(inner):
                    rep.recurring[recur_key][phase] = int(n)
            continue
        lv = LEVEL_RE.match(line)
        if lv:
            current = Message(lv.group(1), lv.group(2).strip())
            rep.messages.append(current)
            continue
        cont = CONT_RE.match(line)
        if cont and current:
            current.detail.append(cont.group(1).strip())
            continue
        star = STAR_RE.match(line)
        if star and current and star.group(1).startswith("Key="):
            current.detail.append(star.group(1).strip())
            continue
        if star and re.search(r"There are \d+ unused", star.group(1)):
            current = Message("Warning", star.group(1).strip())
            rep.messages.append(current)
            continue
        if star:
            current = None
    for m in rep.messages:
        m.rule = classify(m)
    if rep.completed is None and any(m.level == "Fatal" for m in rep.messages):
        rep.completed = False
    return rep


# ---------------------------------------------------------------------------
# Other files in the run folder
# ---------------------------------------------------------------------------


def find_files(target: Path) -> dict[str, Path | None]:
    if target.is_file():
        folder, stem = target.parent, target.stem
    else:
        folder = target
        errs = sorted(folder.glob("*.err"))
        if not errs:
            raise SystemExit(f"No .err file found in {folder}")
        stem = errs[0].stem
    def pick(*names):
        for n in names:
            p = folder / n
            if p.exists():
                return p
        return None
    out = "eplusout" if stem == "eplusout" else stem
    return {
        "err": folder / f"{stem}.err",
        "rvaudit": pick(f"{out}.rvaudit", "readvars.rvaudit", "eplusout.rvaudit"),
        "table": pick(f"{out}Table.html", f"{out}Table.htm", "eplustbl.htm", "eplustbl.html"),
    }


def read_rvaudit(path: Path | None) -> list[str]:
    if not path:
        return []
    notes = []
    text = path.read_text(errors="replace")
    m = re.search(r"too many variables requested, will go with first\s+(\d+)", text)
    if m:
        notes.append(f"The CSV was cut off at {m.group(1)} columns. Outputs after that point are in the .eso/.sql file "
                     f"but NOT in the CSV. Request fewer variables, or read results from the .eso or .sql instead.")
    return notes


def _table_rows(doc: str, title: str) -> list[list[str]]:
    i = doc.find(f"<b>{title}")
    if i < 0:
        return []
    seg = doc[i:]
    seg = seg[: seg.find("</table>")]
    rows = []
    for r in re.findall(r"(?is)<tr>(.*?)</tr>", seg):
        cells = [html.unescape(re.sub(r"<[^>]+>", "", c)).strip()
                 for c in re.findall(r"(?is)<t[dh][^>]*>(.*?)</t[dh]>", r)]
        rows.append(cells)
    return rows


def _num(s: str) -> float | None:
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def read_table(path: Path | None) -> dict[str, float]:
    if not path:
        return {}
    doc = path.read_text(errors="replace")
    out: dict[str, float] = {}
    for row in _table_rows(doc, "Building Area"):
        if row and row[0] == "Total Building Area":
            out["floor_area_m2"] = _num(row[1])
    rows = _table_rows(doc, "End Uses")
    if rows:
        header = rows[0]
        for row in rows[1:]:
            if row and row[0] in ("Heating", "Water Systems"):
                total = sum(_num(v) or 0.0 for h, v in zip(header[1:], row[1:]) if "kWh" in h)
                out["heating_kwh" if row[0] == "Heating" else "hot_water_kwh"] = total
    rows = _table_rows(doc, "Time Setpoint Not Met")
    if rows:
        # Columns: During Heating, During Cooling, During Occupied Heating, During Occupied Cooling.
        # "During Heating" counts every hour the heating setpoint wasn't met, including nights and
        # empty-house hours; "During Occupied Heating" only counts hours when people are in.
        col = {h.split("[")[0].strip(): i for i, h in enumerate(rows[0])}
        for row in rows[1:]:
            if row and row[0] == "Facility":
                for key, head in (("unmet_heating_h", "During Heating"),
                                  ("unmet_occupied_heating_h", "During Occupied Heating")):
                    i = col.get(head)
                    if i is not None and i < len(row):
                        out[key] = _num(row[i])
    if out.get("floor_area_m2") and "heating_kwh" in out:
        out["heating_kwh_m2"] = out["heating_kwh"] / out["floor_area_m2"]
    return out


# ---------------------------------------------------------------------------
# Run summary
# ---------------------------------------------------------------------------


@dataclass
class Group:
    rule: Rule
    messages: list[Message]
    verdict: str
    note: str = ""


@dataclass
class RunResult:
    name: str
    err: ErrReport
    groups: list[Group]
    file_notes: list[str]
    numbers: dict[str, float]
    status: str
    baseline_notes: list[str] = field(default_factory=list)


def summarise(target: Path) -> RunResult:
    files = find_files(target)
    err = parse_err(files["err"])
    by_rule: dict[str, list[Message]] = {}
    for m in err.messages:
        by_rule.setdefault(m.rule.id, []).append(m)
    groups = []
    for rid, msgs in by_rule.items():
        rule = msgs[0].rule
        verdict, note = rule.verdict, ""
        if rid == "psych-temp":
            rec = next((v for k, v in err.recurring.items() if "PsyPsatFnTemp" in k), None)
            if rec:
                in_run = rec["total"] - rec["Warmup"] - rec["Sizing"]
                note = f"{rec['total']} occurrences: {rec['Sizing']} during sizing, {rec['Warmup']} during warm-up, {in_run} during the run."
                verdict = IGNORE if in_run == 0 and rec["Warmup"] == 0 else FIX
        groups.append(Group(rule, msgs, verdict, note))
    groups.sort(key=lambda g: (ORDER[g.verdict], -len(g.messages)))

    if err.completed is False or (err.n_severe or 0) > 0:
        status = "FAILED"
    elif any(g.verdict == FIX for g in groups):
        status = "SUSPECT"
    else:
        status = "OK"
    folder = files["err"].parent
    name = folder.name if not target.is_file() else files["err"].stem
    return RunResult(name, err, groups, read_rvaudit(files["rvaudit"]), read_table(files["table"]), status)


def compare(run: RunResult, base: RunResult) -> None:
    def counts(r):
        return {g.rule.id: len(g.messages) for g in r.groups}
    rc, bc = counts(run), counts(base)
    for rid in sorted(set(rc) | set(bc)):
        if rc.get(rid, 0) != bc.get(rid, 0):
            title = next((g.rule.title for g in run.groups + base.groups if g.rule.id == rid), rid)
            run.baseline_notes.append(f"'{title}': {bc.get(rid, 0)} in baseline, {rc.get(rid, 0)} in this run")
    for key, label in [("heating_kwh", "Space heating"), ("hot_water_kwh", "Hot water"),
                       ("unmet_heating_h", "Unmet heating hours"),
                       ("unmet_occupied_heating_h", "Unmet heating hours while occupied")]:
        a, b = run.numbers.get(key), base.numbers.get(key)
        if a is not None and b:
            run.baseline_notes.append(f"{label}: {b:,.0f} -> {a:,.0f} ({(a - b) / b:+.1%})")


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def print_text(run: RunResult, quiet: bool) -> None:
    e = run.err
    print("=" * 78)
    print(f"{run.name}   [{run.status}]   EnergyPlus {e.version}")
    if e.n_warnings is not None:
        print(f"  {'Completed' if e.completed else 'Did NOT complete'}: {e.n_warnings} warnings, {e.n_severe} severe")
    n = run.numbers
    if n:
        bits = []
        if "heating_kwh" in n:
            bits.append(f"heating {n['heating_kwh']:,.0f} kWh")
        if "heating_kwh_m2" in n:
            bits.append(f"{n['heating_kwh_m2']:.0f} kWh/m2")
        if "hot_water_kwh" in n:
            bits.append(f"hot water {n['hot_water_kwh']:,.0f} kWh")
        if "floor_area_m2" in n:
            bits.append(f"floor {n['floor_area_m2']:.1f} m2")
        if n.get("unmet_heating_h") is not None:
            bits.append(f"unmet heating {n['unmet_heating_h']:g} h")
        if n.get("unmet_occupied_heating_h") is not None:
            bits.append(f"of which occupied {n['unmet_occupied_heating_h']:g} h")
        print("  " + ", ".join(bits))
    for note in run.file_notes:
        print(f"  ! {note}")
    for g in run.groups:
        if quiet and g.verdict == IGNORE:
            continue
        print(f"\n  [{g.verdict}] {g.rule.title}  (x{len(g.messages)})")
        if not quiet:
            print(f"      What it means: {g.rule.meaning}")
            print(f"      What to do:    {g.rule.action}")
        if g.note:
            print(f"      {g.note}")
        examples = [m.text for m in g.messages[:3]]
        for ex in examples:
            print(f"      e.g. {ex[:110]}")
        keys = [d for m in g.messages for d in m.detail if d.startswith("Key=")]
        if keys:
            names = sorted({re.sub(r"Key=[^,]*, VarName=([^,]+),.*", r"\1", k) for k in keys})
            print(f"      Variables not produced: {', '.join(names)}")
    if run.baseline_notes:
        print("\n  Compared with baseline:")
        for b in run.baseline_notes:
            print(f"    - {b}")
    print()


def write_html(runs: list[RunResult], path: Path) -> None:
    colour = {FIX: "#b3261e", CHECK: "#8a5a00", IGNORE: "#2e6b4f", "OK": "#2e6b4f", "SUSPECT": "#8a5a00", "FAILED": "#b3261e"}
    esc = html.escape
    parts = ["<!doctype html><meta charset='utf-8'><title>EnergyPlus run check</title>",
             "<style>body{font:15px/1.5 system-ui,sans-serif;max-width:960px;margin:24px auto;padding:0 16px;color:#1b2429}"
             "h2{margin-top:32px}.tag{display:inline-block;color:#fff;border-radius:4px;padding:0 6px;font-size:12px;font-weight:600}"
             "td,th{text-align:left;padding:4px 10px;border-bottom:1px solid #ddd;vertical-align:top}table{border-collapse:collapse;width:100%}"
             "small{color:#555}</style><h1>EnergyPlus run check</h1>"]
    parts.append("<table><tr><th>Run</th><th>Status</th><th>Warnings</th><th>Severe</th><th>Heating kWh</th><th>kWh/m2</th><th>Unmet heating h</th><th>Unmet h occupied</th></tr>")
    for r in runs:
        n = r.numbers
        parts.append(f"<tr><td><a href='#{esc(r.name)}'>{esc(r.name)}</a></td>"
                     f"<td><span class='tag' style='background:{colour[r.status]}'>{r.status}</span></td>"
                     f"<td>{r.err.n_warnings}</td><td>{r.err.n_severe}</td>"
                     f"<td>{n.get('heating_kwh', 0):,.0f}</td><td>{n.get('heating_kwh_m2', 0):.0f}</td>"
                     f"<td>{(n.get('unmet_heating_h') or 0):g}</td>"
                     f"<td>{(n.get('unmet_occupied_heating_h') or 0):g}</td></tr>")
    parts.append("</table>")
    for r in runs:
        parts.append(f"<h2 id='{esc(r.name)}'>{esc(r.name)} <span class='tag' style='background:{colour[r.status]}'>{r.status}</span></h2>")
        for note in r.file_notes:
            parts.append(f"<p><b>Note:</b> {esc(note)}</p>")
        if r.baseline_notes:
            parts.append("<p><b>Compared with baseline:</b></p><ul>" + "".join(f"<li>{esc(b)}</li>" for b in r.baseline_notes) + "</ul>")
        parts.append("<table><tr><th>Verdict</th><th>Message</th><th>Count</th><th>What it means / what to do</th></tr>")
        for g in r.groups:
            ex = "<br>".join(esc(m.text[:140]) for m in g.messages[:2])
            parts.append(f"<tr><td><span class='tag' style='background:{colour[g.verdict]}'>{g.verdict}</span></td>"
                         f"<td><b>{esc(g.rule.title)}</b><br><small>{ex}</small></td><td>{len(g.messages)}</td>"
                         f"<td>{esc(g.rule.meaning)}<br><b>Do:</b> {esc(g.rule.action)}"
                         + (f"<br><i>{esc(g.note)}</i>" if g.note else "") + "</td></tr>")
        parts.append("</table>")
    path.write_text("\n".join(parts), encoding="utf-8")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="eptools check", description="Explain EnergyPlus warnings and flag bad runs.")
    ap.add_argument("runs", nargs="+", type=Path, help="run folders or .err files")
    ap.add_argument("--baseline", type=Path, help="run folder or .err file to compare against")
    ap.add_argument("--html", type=Path, help="also write an HTML report here")
    ap.add_argument("--quiet", action="store_true", help="hide IGNORE items and long explanations")
    args = ap.parse_args(argv)

    base = summarise(args.baseline) if args.baseline else None
    targets = []
    for t in args.runs:   # expand wildcards ourselves, since Windows cmd doesn't
        targets += sorted(Path().glob(str(t))) if any(c in str(t) for c in "*?[") else [t]
    results = []
    for target in targets:
        r = summarise(target)
        if base:
            compare(r, base)
        results.append(r)
        print_text(r, args.quiet)
    if args.html:
        write_html(results, args.html)
        print(f"HTML report written to {args.html}")
    failed = sum(r.status == "FAILED" for r in results)
    suspect = sum(r.status == "SUSPECT" for r in results)
    print(f"{len(results)} run(s): {len(results) - failed - suspect} OK, {suspect} suspect, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
