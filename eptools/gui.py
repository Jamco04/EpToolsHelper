"""eptools desktop window: check runs, browse a model, run a parameter study, view results.

Start it by double-clicking "eptools.pyw" (or "Start eptools.bat" on Windows), or with
    python -m eptools gui
Only the Python standard library (tkinter) is used.
"""

from __future__ import annotations

import csv
import glob
import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
import traceback
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import __version__, check, runner
from .costs import Tariffs, study_costs
from .idf_edit import IDF

DEFAULT_HOURLY = [
    "Zone Ideal Loads Supply Air Total Heating Energy",
    "DistrictHeatingWater:Facility",
]
DEFAULT_ADD = [
    "Output:Variable,*,Zone Ideal Loads Supply Air Total Heating Energy,Hourly;",
    "Output:Variable,*,Zone Mean Air Temperature,Hourly;",
    "Output:Meter,DistrictHeatingWater:Facility,Hourly;",
    "Output:Meter,Electricity:Facility,Hourly;",
]
VERDICT_COLOURS = {"FIX": "#b3261e", "CHECK": "#8a5a00", "IGNORE": "#2e6b4f",
                   "OK": "#2e6b4f", "SUSPECT": "#8a5a00", "FAILED": "#b3261e",
                   "NO OUTPUT": "#b3261e", "CANCELLED": "#666666"}
HOW_LABELS = {"set": "Set to each value", "multiply": "Multiply by each factor", "replace": "Replace a value"}


def open_path(path) -> None:
    """Open a file or folder with the system's default app."""
    path = str(path)
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)  # noqa: S606 - opening the user's own file
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except OSError as exc:
        messagebox.showerror("Couldn't open", f"{path}\n\n{exc}")


def find_energyplus() -> str:
    """Newest EnergyPlus install in the usual places, or ''."""
    patterns = ["C:/EnergyPlusV*/energyplus.exe", "C:/Program Files/EnergyPlusV*/energyplus.exe",
                "/Applications/EnergyPlus-*/energyplus", "/usr/local/EnergyPlus-*/energyplus"]
    found = [p for pat in patterns for p in glob.glob(pat)]
    return sorted(found)[-1] if found else ""


def scrolled(parent, widget_cls, **kw):
    """A widget with a vertical scrollbar, packed into its own frame."""
    frame = ttk.Frame(parent)
    w = widget_cls(frame, **kw)
    sb = ttk.Scrollbar(frame, orient="vertical", command=w.yview)
    w.configure(yscrollcommand=sb.set)
    w.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")
    return frame, w


class PathRow:
    """Label, entry and Browse button on one grid row."""

    def __init__(self, parent, row, label, kind, filetypes=None, hint=""):
        self.var = tk.StringVar()
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=3)
        ttk.Entry(parent, textvariable=self.var).grid(row=row, column=1, sticky="ew", pady=3)
        ttk.Button(parent, text="Browse…", command=self.browse).grid(row=row, column=2, padx=(6, 0), pady=3)
        if hint:
            ttk.Label(parent, text=hint, foreground="#55666f").grid(row=row, column=3, sticky="w", padx=(8, 0))
        self.kind, self.filetypes = kind, filetypes or [("All files", "*.*")]

    def browse(self):
        if self.kind == "dir":
            p = filedialog.askdirectory(mustexist=False)
        else:
            p = filedialog.askopenfilename(filetypes=self.filetypes)
        if p:
            self.var.set(p)

    def get(self) -> str:
        return self.var.get().strip()


# =============================================================================== Check tab


class CheckTab(ttk.Frame):
    def __init__(self, app, parent):
        super().__init__(parent, padding=10)
        self.app = app
        self.results: list[check.RunResult] = []

        top = ttk.Frame(self)
        top.pack(fill="x")
        ttk.Label(top, text="Pick .err files or run folders, then press Check. Nothing is sent anywhere.").pack(anchor="w")
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=6)
        ttk.Button(bar, text="Add .err files…", command=self.add_files).pack(side="left")
        ttk.Button(bar, text="Add run folder…", command=self.add_folder).pack(side="left", padx=6)
        ttk.Button(bar, text="Clear", command=self.clear).pack(side="left")
        ttk.Button(bar, text="Check", command=self.run_check, style="Accent.TButton").pack(side="left", padx=12)
        ttk.Button(bar, text="Save HTML report…", command=self.save_html).pack(side="left")

        base = ttk.Frame(self)
        base.pack(fill="x")
        base.columnconfigure(1, weight=1)
        self.baseline = PathRow(base, 0, "Compare with (optional):", "file",
                                [("EnergyPlus error file", "*.err"), ("All files", "*.*")],
                                "a baseline run's .err")

        tf, self.targets = scrolled(self, tk.Listbox, height=4, selectmode="extended")
        tf.pack(fill="x", pady=(6, 8))

        panes = ttk.PanedWindow(self, orient="vertical")
        panes.pack(fill="both", expand=True)
        cols = ("status", "warnings", "severe", "heating", "per_m2", "unmet", "unmet_occ")
        f1, self.runs = scrolled(panes, ttk.Treeview, columns=cols, height=5)
        for c, w, t in zip(("#0",) + cols, (240, 90, 80, 70, 110, 80, 110, 130),
                           ("Run", "Status", "Warnings", "Severe", "Heating kWh", "kWh/m²",
                            "Unmet heating h", "Unmet h occupied")):
            self.runs.heading(c, text=t)
            self.runs.column(c, width=w, anchor="w" if c == "#0" else "center")
        self.runs.bind("<<TreeviewSelect>>", lambda e: self.show_run())
        panes.add(f1, weight=1)

        lower = ttk.PanedWindow(panes, orient="horizontal")
        f2, self.groups = scrolled(lower, ttk.Treeview, columns=("count",), height=8)
        self.groups.heading("#0", text="Message group")
        self.groups.heading("count", text="Count")
        self.groups.column("#0", width=380)
        self.groups.column("count", width=60, anchor="center")
        for v, colour in VERDICT_COLOURS.items():
            self.groups.tag_configure(v, foreground=colour)
            self.runs.tag_configure(v, foreground=colour)
        self.groups.bind("<<TreeviewSelect>>", lambda e: self.show_group())
        lower.add(f2, weight=1)
        f3, self.detail = scrolled(lower, tk.Text, wrap="word", height=8, padx=8, pady=6)
        self.detail.tag_configure("h", font=("TkDefaultFont", 11, "bold"))
        self.detail.tag_configure("dim", foreground="#55666f")
        lower.add(f3, weight=2)
        panes.add(lower, weight=2)

    def add_files(self):
        for p in filedialog.askopenfilenames(filetypes=[("EnergyPlus error file", "*.err"), ("All files", "*.*")]):
            self.targets.insert("end", p)

    def add_folder(self):
        p = filedialog.askdirectory(mustexist=True)
        if p:
            self.targets.insert("end", p)

    def add_targets(self, paths, run_now=True):
        self.clear()
        for p in paths:
            self.targets.insert("end", str(p))
        if run_now:
            self.run_check()

    def clear(self):
        self.targets.delete(0, "end")
        self.runs.delete(*self.runs.get_children())
        self.groups.delete(*self.groups.get_children())
        self.detail.delete("1.0", "end")
        self.results = []

    def run_check(self):
        paths = list(self.targets.get(0, "end"))
        if not paths:
            messagebox.showinfo("Nothing to check", "Add an .err file or a run folder first.")
            return
        self.runs.delete(*self.runs.get_children())
        self.results = []
        base = None
        if self.baseline.get():
            try:
                base = check.summarise(Path(self.baseline.get()))
            except (OSError, SystemExit) as exc:
                messagebox.showerror("Baseline", f"Couldn't read the baseline:\n{exc}")
        for p in paths:
            try:
                r = check.summarise(Path(p))
            except (OSError, SystemExit) as exc:
                messagebox.showerror("Couldn't check", f"{p}\n\n{exc}")
                continue
            if base:
                check.compare(r, base)
            self.results.append(r)
            n = r.numbers
            fmt = lambda k, f="{:,.0f}": f.format(n[k]) if n.get(k) is not None else ""
            self.runs.insert("", "end", iid=str(len(self.results) - 1), text=f"{r.name}  ({Path(p).name})",
                             values=(r.status, r.err.n_warnings, r.err.n_severe, fmt("heating_kwh"),
                                     fmt("heating_kwh_m2"), fmt("unmet_heating_h", "{:g}"),
                                     fmt("unmet_occupied_heating_h", "{:g}")), tags=(r.status,))
        if self.results:
            self.runs.selection_set("0")

    def selected_run(self):
        sel = self.runs.selection()
        return self.results[int(sel[0])] if sel else None

    def show_run(self):
        r = self.selected_run()
        self.groups.delete(*self.groups.get_children())
        self.detail.delete("1.0", "end")
        if not r:
            return
        for i, g in enumerate(r.groups):
            self.groups.insert("", "end", iid=str(i), text=f"{g.verdict:<7} {g.rule.title}",
                               values=(len(g.messages),), tags=(g.verdict,))
        d = self.detail
        e = r.err
        d.insert("end", f"{r.name}: {r.status}\n", "h")
        d.insert("end", f"EnergyPlus {e.version}. "
                        f"{'Completed' if e.completed else 'Did not complete'} with {e.n_warnings} warnings "
                        f"and {e.n_severe} severe errors.\n\n")
        for note in r.file_notes:
            d.insert("end", "Note: ", "h")
            d.insert("end", note + "\n\n")
        if r.baseline_notes:
            d.insert("end", "Compared with the baseline\n", "h")
            for b in r.baseline_notes:
                d.insert("end", f"  • {b}\n")
            d.insert("end", "\n")
        d.insert("end", "Pick a message group on the left to see what it means.", "dim")

    def show_group(self):
        r, sel = self.selected_run(), self.groups.selection()
        if not r or not sel:
            return
        g = r.groups[int(sel[0])]
        d = self.detail
        d.delete("1.0", "end")
        d.insert("end", f"{g.verdict}: {g.rule.title}  (×{len(g.messages)})\n\n", "h")
        d.insert("end", "What it means\n", "h")
        d.insert("end", g.rule.meaning + "\n\n")
        d.insert("end", "What to do\n", "h")
        d.insert("end", g.rule.action + "\n\n")
        if g.note:
            d.insert("end", g.note + "\n\n")
        keys = sorted({k.split("VarName=")[1].split(",")[0] for m in g.messages for k in m.detail if "VarName=" in k})
        if keys:
            d.insert("end", "Outputs not produced\n", "h")
            d.insert("end", "\n".join(keys) + "\n\n")
        d.insert("end", "Messages\n", "h")
        for m in g.messages[:40]:
            d.insert("end", f"{m.level}: {m.text}\n")
            for line in m.detail[:4]:
                if not line.startswith("Key="):
                    d.insert("end", f"    {line}\n", "dim")
        if len(g.messages) > 40:
            d.insert("end", f"…and {len(g.messages) - 40} more\n", "dim")

    def save_html(self):
        if not self.results:
            messagebox.showinfo("Nothing to save", "Run a check first.")
            return
        p = filedialog.asksaveasfilename(defaultextension=".html", initialfile="check_report.html",
                                         filetypes=[("HTML", "*.html")])
        if p:
            check.write_html(self.results, Path(p))
            webbrowser.open(Path(p).resolve().as_uri())


# =============================================================================== Model tab


class ModelTab(ttk.Frame):
    def __init__(self, app, parent):
        super().__init__(parent, padding=10)
        self.app = app
        self.idf: IDF | None = None
        self.path = ""
        bar = ttk.Frame(self)
        bar.pack(fill="x")
        ttk.Button(bar, text="Open model (.idf)…", command=self.open_model).pack(side="left")
        self.title = ttk.Label(bar, text="  No model open")
        self.title.pack(side="left")
        ttk.Button(bar, text="Use selected field as a study parameter", command=self.use_as_param).pack(side="right")
        ttk.Label(self, text="Pick an object type, then an object, to see its fields and current values.",
                  foreground="#55666f").pack(anchor="w", pady=(6, 4))

        search = ttk.Frame(self)
        search.pack(fill="x", pady=(0, 4))
        ttk.Label(search, text="Filter object types:").pack(side="left")
        self.filter = tk.StringVar()
        self.filter.trace_add("write", lambda *a: self.fill_classes())
        ttk.Entry(search, textvariable=self.filter, width=30).pack(side="left", padx=6)

        panes = ttk.PanedWindow(self, orient="horizontal")
        panes.pack(fill="both", expand=True)
        f1, self.classes = scrolled(panes, tk.Listbox, exportselection=False, width=34)
        f2, self.objects = scrolled(panes, tk.Listbox, exportselection=False, width=40)
        f3, self.fields = scrolled(panes, ttk.Treeview, columns=("value", "field"), show="headings")
        self.fields.heading("value", text="Value")
        self.fields.heading("field", text="Field")
        self.fields.column("value", width=260)
        self.fields.column("field", width=320)
        panes.add(f1, weight=1)
        panes.add(f2, weight=1)
        panes.add(f3, weight=2)
        self.classes.bind("<<ListboxSelect>>", lambda e: self.fill_objects())
        self.objects.bind("<<ListboxSelect>>", lambda e: self.fill_fields())
        self.fields.bind("<Double-1>", lambda e: self.use_as_param())

    def open_model(self, path=None):
        path = path or filedialog.askopenfilename(filetypes=[("EnergyPlus model", "*.idf"), ("All files", "*.*")])
        if not path:
            return
        try:
            self.idf = IDF.load(path)
        except OSError as exc:
            messagebox.showerror("Couldn't open", str(exc))
            return
        self.path = path
        ver = self.idf.find("Version")
        v = ver[0].values[0] if ver and ver[0].values else "?"
        self.title.configure(text=f"  {Path(path).name}   (EnergyPlus version {v}, {len(self.idf.objects)} objects)")
        self.fill_classes()

    def fill_classes(self):
        self.classes.delete(0, "end")
        self.objects.delete(0, "end")
        self.fields.delete(*self.fields.get_children())
        if not self.idf:
            return
        f = self.filter.get().lower()
        self._classes = [c for c in sorted(self.idf.classes(), key=str.lower) if f in c.lower()]
        for c in self._classes:
            self.classes.insert("end", f"{c}  ({self.idf.classes()[c]})")

    def current_class(self):
        sel = self.classes.curselection()
        return self._classes[sel[0]] if sel else None

    def fill_objects(self):
        self.objects.delete(0, "end")
        self.fields.delete(*self.fields.get_children())
        cls = self.current_class()
        if cls:
            self._objects = self.idf.find(cls)
            for o in self._objects:
                self.objects.insert("end", o.name)

    def current_object(self):
        sel = self.objects.curselection()
        return self._objects[sel[0]] if sel else None

    def fill_fields(self):
        self.fields.delete(*self.fields.get_children())
        o = self.current_object()
        if o:
            for i, (v, n) in enumerate(zip(o.values, o.names), start=1):
                self.fields.insert("", "end", iid=str(i), values=(v, f"{i}. {n}" if n else f"{i}."))

    def use_as_param(self):
        o, sel = self.current_object(), self.fields.selection()
        if not o or not sel:
            messagebox.showinfo("Pick a field", "Pick an object type, an object and then the field to vary.")
            return
        i = int(sel[0])
        name = o.names[i - 1] if i - 1 < len(o.names) and o.names[i - 1] else str(i)
        field_name = check_units_free(name)
        self.app.study_tab.edit_parameter(prefill={
            "object": o.cls, "name": o.name, "field": field_name, "how": "set",
            "values": o.values[i - 1] if i - 1 < len(o.values) else ""})
        self.app.notebook.select(self.app.study_tab)


def check_units_free(name: str) -> str:
    return name.split("{")[0].strip()


# =============================================================================== Study tab


class ParameterDialog(tk.Toplevel):
    def __init__(self, study_tab, initial: dict):
        super().__init__(study_tab)
        self.title("Study parameter")
        self.transient(study_tab.winfo_toplevel())
        self.study_tab = study_tab
        self.result = None
        self.idf = study_tab.first_model_idf()
        f = ttk.Frame(self, padding=12)
        f.pack(fill="both", expand=True)
        f.columnconfigure(1, weight=1)
        self.vars = {k: tk.StringVar(value=str(initial.get(k, ""))) for k in
                     ("pname", "object", "name", "field", "how", "old", "values")}
        if not self.vars["name"].get():
            self.vars["name"].set("*")
        self.vars["how"].set(HOW_LABELS.get(initial.get("how", "set"), HOW_LABELS["set"]))
        r = 0

        def row(label, widget, hint=""):
            nonlocal r
            ttk.Label(f, text=label).grid(row=r, column=0, sticky="w", pady=4, padx=(0, 8))
            widget.grid(row=r, column=1, sticky="ew", pady=4)
            if hint:
                ttk.Label(f, text=hint, foreground="#55666f").grid(row=r + 1, column=1, sticky="w")
                r += 1
            r += 1

        row("Parameter name", ttk.Entry(f, textvariable=self.vars["pname"], width=40),
            "Short column name for results.csv, e.g. wall_insulation_m")
        classes = sorted(self.idf.classes(), key=str.lower) if self.idf else []
        row("Object type", ttk.Combobox(f, textvariable=self.vars["object"], values=classes))
        row("Object name", ttk.Entry(f, textvariable=self.vars["name"]),
            "Exact name, or a pattern with * (e.g. EPS* or *Heating Setpoint Schedule). * alone = all.")
        self.field_box = ttk.Combobox(f, textvariable=self.vars["field"])
        row("Field", self.field_box, "Field name as shown in the model browser, or its number (1 = Name)")
        row("How", ttk.Combobox(f, textvariable=self.vars["how"], values=list(HOW_LABELS.values()), state="readonly"))
        self.old_entry = ttk.Entry(f, textvariable=self.vars["old"])
        row("Value to replace", self.old_entry, "Only for 'Replace a value', e.g. 21 to change a 21 °C setpoint")
        row("Values", ttk.Entry(f, textvariable=self.vars["values"]),
            "Separate with commas, e.g. 0.05, 0.1, 0.15")
        self.preview = ttk.Label(f, text="", foreground="#0e6e7a", wraplength=460, justify="left")
        self.preview.grid(row=r, column=0, columnspan=2, sticky="w", pady=(8, 4))
        r += 1
        b = ttk.Frame(f)
        b.grid(row=r, column=0, columnspan=2, sticky="e", pady=(8, 0))
        ttk.Button(b, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(b, text="OK", command=self.ok).pack(side="right", padx=6)
        if initial.get("old") is not None:
            self.vars["old"].set(initial["old"])
        for k in ("object", "name", "field", "how", "old"):
            self.vars[k].trace_add("write", lambda *a: self.update_preview())
        self.update_preview()
        self.grab_set()
        self.wait_window()

    def how(self) -> str:
        label = self.vars["how"].get()
        return next((k for k, v in HOW_LABELS.items() if v == label), "set")

    def update_preview(self):
        self.old_entry.configure(state="normal" if self.how() == "replace" else "disabled")
        if not self.idf:
            self.preview.configure(text="Add a model on the Study tab to check this parameter against it.")
            return
        objs = self.idf.find(self.vars["object"].get().strip(), self.vars["name"].get().strip() or "*")
        if objs:
            self.field_box.configure(values=[check_units_free(n) for n in objs[0].names if n])
        fld = self.vars["field"].get().strip()
        if not objs:
            self.preview.configure(text="Matches no objects in the first model yet.", foreground="#b3261e")
            return
        text = f"Matches {len(objs)} object(s) in {Path(self.study_tab.models[0]).name}."
        if self.how() == "replace":
            old = self.vars["old"].get().strip()
            n = sum(1 for o in objs for v in o.values if old and v.strip() == old)
            text += f" '{old}' appears {n} time(s) in them." if old else ""
        elif fld:
            try:
                vals = sorted({o.get(int(fld) if fld.isdigit() else fld) for o in objs})
                text += f" Current value(s): {', '.join(vals[:6])}{' …' if len(vals) > 6 else ''}"
            except KeyError:
                self.preview.configure(text=text + " But it has no field called that.", foreground="#b3261e")
                return
        self.preview.configure(text=text, foreground="#0e6e7a")

    def ok(self):
        v = {k: x.get().strip() for k, x in self.vars.items()}
        if not v["pname"] or not v["object"] or not v["values"]:
            messagebox.showerror("Missing", "Give the parameter a name, an object type and some values.", parent=self)
            return
        if self.how() != "replace" and not v["field"]:
            messagebox.showerror("Missing", "Pick the field to change.", parent=self)
            return
        try:
            values = [runner._num(x.strip()) for x in v["values"].split(",") if x.strip()]
        except ValueError:
            values = []
        if not values:
            messagebox.showerror("Values", "Enter at least one value.", parent=self)
            return
        spec = {"object": v["object"], "name": v["name"] or "*"}
        if v["field"]:
            spec["field"] = int(v["field"]) if v["field"].isdigit() else v["field"]
        if self.how() == "multiply":
            spec["multiply"] = values
        else:
            if self.how() == "replace":
                if not v["old"]:
                    messagebox.showerror("Missing", "Enter the value to replace.", parent=self)
                    return
                spec["replace"] = runner._num(v["old"])
            spec["values"] = values
        self.result = (v["pname"], spec)
        self.destroy()


class StudyTab(ttk.Frame):
    def __init__(self, app, parent):
        super().__init__(parent, padding=10)
        self.app = app
        self.models: list[str] = []
        self.parameters: dict[str, dict] = {}
        self.extra_fixed: list[dict] = []
        self.runs_csv = None
        self.study_path = None
        self.cancel = threading.Event()
        self.queue: queue.Queue = queue.Queue()
        self._idf_cache = {}

        bar = ttk.Frame(self)
        bar.pack(fill="x")
        ttk.Button(bar, text="Open study…", command=self.open_study).pack(side="left")
        ttk.Button(bar, text="Save study…", command=self.save_study).pack(side="left", padx=6)
        self.study_label = ttk.Label(bar, text="  New study (not saved)")
        self.study_label.pack(side="left")

        files = ttk.LabelFrame(self, text="Files", padding=8)
        files.pack(fill="x", pady=(8, 4))
        files.columnconfigure(1, weight=1)
        self.ep = PathRow(files, 0, "EnergyPlus program", "file",
                          [("EnergyPlus", "energyplus*"), ("All files", "*.*")])
        self.ep.var.set(find_energyplus())
        self.weather = PathRow(files, 1, "Weather file", "file", [("EnergyPlus weather", "*.epw"), ("All", "*.*")])
        self.outdir = PathRow(files, 2, "Save runs in", "dir")

        mid = ttk.Frame(self)
        mid.pack(fill="both", expand=True, pady=4)
        mid.columnconfigure(0, weight=1)
        mid.columnconfigure(1, weight=3)
        mid.rowconfigure(0, weight=1)

        mf = ttk.LabelFrame(mid, text="Models", padding=8)
        mf.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        lf, self.model_list = scrolled(mf, tk.Listbox, height=6)
        lf.pack(fill="both", expand=True)
        mb = ttk.Frame(mf)
        mb.pack(fill="x", pady=(6, 0))
        ttk.Button(mb, text="Add…", command=self.add_models).pack(side="left")
        ttk.Button(mb, text="Remove", command=self.remove_model).pack(side="left", padx=6)
        ttk.Button(mb, text="Browse", command=self.browse_model).pack(side="left")

        pf = ttk.LabelFrame(mid, text="Parameters (every combination is run)", padding=8)
        pf.grid(row=0, column=1, sticky="nsew")
        cols = ("object", "name", "field", "how", "values")
        tf, self.param_tree = scrolled(pf, ttk.Treeview, columns=cols, height=6)
        for c, t, w in zip(("#0",) + cols, ("Parameter", "Object type", "Object name", "Field", "How", "Values"),
                           (150, 170, 170, 120, 120, 160)):
            self.param_tree.heading(c, text=t)
            self.param_tree.column(c, width=w)
        tf.pack(fill="both", expand=True)
        self.param_tree.bind("<Double-1>", lambda e: self.edit_parameter())
        pb = ttk.Frame(pf)
        pb.pack(fill="x", pady=(6, 0))
        ttk.Button(pb, text="Add…", command=lambda: self.edit_parameter(new=True)).pack(side="left")
        ttk.Button(pb, text="Edit…", command=self.edit_parameter).pack(side="left", padx=6)
        ttk.Button(pb, text="Remove", command=self.remove_parameter).pack(side="left")
        self.count_label = ttk.Label(pb, text="")
        self.count_label.pack(side="right")

        opts = ttk.LabelFrame(self, text="Options", padding=8)
        opts.pack(fill="x", pady=4)
        ttk.Label(opts, text="Timesteps per hour").grid(row=0, column=0, sticky="w")
        self.timestep = tk.StringVar(value="Keep model's")
        ttk.Combobox(opts, textvariable=self.timestep, width=14, state="readonly",
                     values=["Keep model's", "1", "2", "4", "6", "10", "12"]).grid(row=0, column=1, sticky="w", padx=6)
        ttk.Label(opts, text="Runs at once").grid(row=0, column=2, sticky="w", padx=(16, 0))
        self.jobs = tk.IntVar(value=max(1, (os.cpu_count() or 2) // 2))
        ttk.Spinbox(opts, from_=1, to=64, textvariable=self.jobs, width=5).grid(row=0, column=3, sticky="w", padx=6)
        ttk.Label(opts, text="Only the first").grid(row=0, column=4, sticky="w", padx=(16, 0))
        self.limit = tk.IntVar(value=0)
        ttk.Spinbox(opts, from_=0, to=100000, textvariable=self.limit, width=7).grid(row=0, column=5, sticky="w", padx=6)
        ttk.Label(opts, text="runs (0 = all)").grid(row=0, column=6, sticky="w")
        self.resume = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="Skip runs that already finished", variable=self.resume).grid(
            row=0, column=7, sticky="w", padx=(16, 0))
        self.replace_outputs = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="Replace the model's own output requests with this hourly list (recommended):",
                        variable=self.replace_outputs).grid(row=1, column=0, columnspan=8, sticky="w", pady=(8, 2))
        self.outputs_text = tk.Text(opts, height=4, wrap="none")
        self.outputs_text.grid(row=2, column=0, columnspan=8, sticky="ew")
        self.outputs_text.insert("1.0", "\n".join(DEFAULT_ADD))
        opts.columnconfigure(7, weight=1)

        go = ttk.Frame(self)
        go.pack(fill="x", pady=(6, 4))
        ttk.Button(go, text="Check edits (no simulation)", command=lambda: self.start(dry_run=True)).pack(side="left")
        self.run_btn = ttk.Button(go, text="Run study", command=self.start, style="Accent.TButton")
        self.run_btn.pack(side="left", padx=6)
        self.stop_btn = ttk.Button(go, text="Stop", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left")
        self.progress = ttk.Progressbar(go, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True, padx=10)
        self.progress_label = ttk.Label(go, text="")
        self.progress_label.pack(side="left")

        lf2, self.log = scrolled(self, tk.Text, height=8, wrap="word")
        lf2.pack(fill="both", expand=True)
        self.log.tag_configure("err", foreground="#b3261e")
        self.after(150, self.poll)
        self.refresh_count()

    # ---------------------------------------------------------------- models & parameters
    def add_models(self):
        for p in filedialog.askopenfilenames(filetypes=[("EnergyPlus model", "*.idf"), ("All files", "*.*")]):
            if p not in self.models:
                self.models.append(p)
                self.model_list.insert("end", Path(p).name)
        self.refresh_count()

    def remove_model(self):
        for i in reversed(self.model_list.curselection()):
            self.model_list.delete(i)
            del self.models[i]
        self.refresh_count()

    def browse_model(self):
        sel = self.model_list.curselection()
        path = self.models[sel[0]] if sel else (self.models[0] if self.models else None)
        if path:
            self.app.model_tab.open_model(path)
            self.app.notebook.select(self.app.model_tab)

    def first_model_idf(self):
        if not self.models:
            return None
        p = self.models[0]
        if p not in self._idf_cache:
            try:
                self._idf_cache[p] = IDF.load(p)
            except OSError:
                return None
        return self._idf_cache[p]

    def edit_parameter(self, new=False, prefill=None):
        old_name = None
        initial = {}
        if prefill:
            initial = dict(prefill)
            initial["pname"] = runner._slug(f"{prefill['field']}")
        elif not new:
            sel = self.param_tree.selection()
            if not sel:
                messagebox.showinfo("Pick a parameter", "Select a parameter to edit, or press Add.")
                return
            old_name = sel[0]
            s = self.parameters[old_name]
            how = "multiply" if "multiply" in s else "replace" if "replace" in s else "set"
            initial = {"pname": old_name, "object": s["object"], "name": s.get("name", "*"),
                       "field": s.get("field", ""), "how": how, "old": s.get("replace"),
                       "values": ", ".join(str(x) for x in s.get("multiply", s.get("values", [])))}
        dlg = ParameterDialog(self, initial)
        if dlg.result:
            pname, spec = dlg.result
            if old_name and old_name != pname:
                self.parameters.pop(old_name, None)
            self.parameters[pname] = spec
            self.refresh_params()

    def remove_parameter(self):
        for iid in self.param_tree.selection():
            self.parameters.pop(iid, None)
        self.refresh_params()

    def refresh_params(self):
        self.param_tree.delete(*self.param_tree.get_children())
        for pname, s in self.parameters.items():
            if "placeholder" in s:
                how, obj = "placeholder", s["placeholder"]
            else:
                how = "multiply" if "multiply" in s else f"replace {s['replace']}" if "replace" in s else "set"
                obj = s.get("object", "")
            vals = s.get("multiply", s.get("values", []))
            self.param_tree.insert("", "end", iid=pname, text=pname,
                                   values=(obj, s.get("name", "*"), s.get("field", ""), how,
                                           ", ".join(str(v) for v in vals)))
        self.refresh_count()

    def refresh_count(self):
        n = len(self.models)
        for s in self.parameters.values():
            n *= len(s.get("multiply", s.get("values", [1])))
        self.count_label.configure(text=f"{n} run(s) in total" if self.models else "Add at least one model")

    # ---------------------------------------------------------------- study file
    @staticmethod
    def _int(var, default):
        try:
            return int(var.get())
        except (tk.TclError, ValueError):
            return default

    def to_config(self) -> dict:
        outputs = [l.strip() for l in self.outputs_text.get("1.0", "end").splitlines() if l.strip()]
        hourly = []
        for line in outputs:
            parts = [p.strip() for p in line.rstrip(";").split(",")]
            if parts[0].lower() == "output:variable" and len(parts) >= 3:
                hourly.append(parts[2])
            elif parts[0].lower() == "output:meter" and len(parts) >= 2:
                hourly.append(parts[1])
        fixed = list(self.extra_fixed)
        if self.timestep.get().isdigit():
            fixed.append({"object": "Timestep", "field": 1, "set": int(self.timestep.get())})
        names = {}
        for p in self.models:
            stem = runner._slug(Path(p).stem)
            names[stem if stem not in names else f"{stem}_{len(names)}"] = p
        cfg = {
            "energyplus": self.ep.get() or "energyplus",
            "weather": self.weather.get(),
            "output_dir": self.outdir.get() or str(Path.home() / "eptools-runs"),
            "jobs": self._int(self.jobs, 1),
            "models": names,
            "fixed": fixed,
            "parameters": self.parameters,
            "outputs": {"replace_existing": bool(self.replace_outputs.get()), "add": outputs},
            "collect": {"hourly": hourly},
        }
        if self.runs_csv:
            cfg["runs_csv"] = self.runs_csv
        return cfg

    def save_study(self):
        p = filedialog.asksaveasfilename(defaultextension=".json", initialfile="study.json",
                                         filetypes=[("Study file", "*.json")])
        if p:
            Path(p).write_text(json.dumps(self.to_config(), indent=2))
            self.study_path = p
            self.study_label.configure(text=f"  {Path(p).name}")

    def open_study(self, path=None):
        path = path or filedialog.askopenfilename(filetypes=[("Study file", "*.json"), ("All files", "*.*")])
        if not path:
            return
        try:
            cfg = json.loads(Path(path).read_text())
        except (OSError, ValueError) as exc:
            messagebox.showerror("Couldn't open study", str(exc))
            return
        root = Path(path).resolve().parent
        absp = lambda p: str((root / p).resolve()) if p and not Path(p).is_absolute() else (p or "")
        ep = cfg.get("energyplus", "")
        self.ep.var.set(absp(ep) if ("/" in ep or "\\" in ep) else ep or find_energyplus())
        self.weather.var.set(absp(cfg.get("weather", "")))
        self.outdir.var.set(absp(cfg.get("output_dir", "runs")))
        models = cfg.get("models", {})
        models = models.values() if isinstance(models, dict) else models
        self.models = [absp(m) for m in models]
        self.model_list.delete(0, "end")
        for m in self.models:
            self.model_list.insert("end", Path(m).name)
        self.parameters = dict(cfg.get("parameters", {}))
        self.extra_fixed = []
        self.timestep.set("Keep model's")
        for f in cfg.get("fixed", []):
            if f.get("object", "").lower() == "timestep":
                self.timestep.set(str(f.get("set", f.get("value", ""))))
            else:
                self.extra_fixed.append(f)
        out = cfg.get("outputs", {})
        self.replace_outputs.set(bool(out.get("replace_existing", False)))
        self.outputs_text.delete("1.0", "end")
        self.outputs_text.insert("1.0", "\n".join(out.get("add", [])))
        self.jobs.set(int(cfg.get("jobs", self.jobs.get())))
        self.runs_csv = absp(cfg["runs_csv"]) if cfg.get("runs_csv") else None
        self.study_path = path
        self.study_label.configure(text=f"  {Path(path).name}")
        self._idf_cache.clear()
        self.refresh_params()

    # ---------------------------------------------------------------- running
    def write(self, text, tag=None):
        self.log.insert("end", text + "\n", tag)
        self.log.see("end")

    def start(self, dry_run=False):
        if not self.models:
            messagebox.showinfo("No models", "Add at least one model (.idf).")
            return
        if not self.weather.get():
            messagebox.showinfo("No weather file", "Pick the weather file (.epw).")
            return
        cfg = self.to_config()
        try:
            study = runner.Study.from_dict(cfg, Path.cwd())
        except runner.StudyError as exc:
            messagebox.showerror("Study", str(exc))
            return
        self.log.delete("1.0", "end")
        self.cancel.clear()
        self.run_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.progress.configure(value=0, maximum=1)
        self.progress_label.configure(text="")
        q = self.queue
        # Read every Tk variable here: Tk must only be touched from this (the main) thread.
        try:
            limit = int(self.limit.get() or 0) or None
            jobs = max(1, int(self.jobs.get() or 1))
        except (tk.TclError, ValueError):
            messagebox.showinfo("Options", "'Runs at once' and 'Only the first' must be whole numbers.")
            self.finish()
            return
        resume = bool(self.resume.get())

        def work():
            try:
                code = runner.run_study(study, dry_run=dry_run, jobs=jobs, resume=resume, limit=limit,
                                        log=lambda t: q.put(("log", t)),
                                        on_run=lambda d, n, row: q.put(("progress", (d, n, row))),
                                        cancel=self.cancel)
                q.put(("done", (code, dry_run, study.output_dir)))
            except runner.StudyError as exc:
                q.put(("error", str(exc)))
            except Exception:  # show anything unexpected instead of dying silently
                q.put(("error", traceback.format_exc()))

        threading.Thread(target=work, daemon=True).start()

    def stop(self):
        self.cancel.set()
        self.write("Stopping: runs already going will finish, the rest are skipped.")

    def poll(self):
        try:
            while True:
                kind, data = self.queue.get_nowait()
                if kind == "log":
                    self.write(data)
                elif kind == "progress":
                    done, total, row = data
                    self.progress.configure(maximum=total, value=done)
                    self.progress_label.configure(text=f"{done} / {total}")
                elif kind == "error":
                    self.write("Stopped: " + data, "err")
                    self.finish()
                    messagebox.showerror("Study stopped", data[:1500])
                elif kind == "done":
                    code, dry, out = data
                    self.finish()
                    if dry:
                        self.write("Each run folder now has in.idf, params.json and changes.csv. "
                                   "Open changes.csv to see exactly what was changed.")
                        if messagebox.askyesno("Edits checked", "Every edit matched the model. Open the runs folder?"):
                            open_path(out)
                    else:
                        res = Path(out) / "results.csv"
                        if res.exists():
                            self.app.results_tab.load(res)
                            self.app.notebook.select(self.app.results_tab)
        except queue.Empty:
            pass
        self.after(150, self.poll)

    def finish(self):
        self.run_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")


# =============================================================================== Results tab


class ResultsTab(ttk.Frame):
    def __init__(self, app, parent):
        super().__init__(parent, padding=10)
        self.app = app
        self.path: Path | None = None
        self.rows: list[dict] = []
        self.sort_col, self.sort_desc = None, False
        bar = ttk.Frame(self)
        bar.pack(fill="x")
        ttk.Button(bar, text="Open results.csv…", command=self.open_results).pack(side="left")
        ttk.Button(bar, text="Open run folder", command=self.open_folder).pack(side="left", padx=6)
        ttk.Button(bar, text="Explain this run's warnings", command=self.check_run).pack(side="left")
        ttk.Button(bar, text="Open full warning report", command=self.open_report).pack(side="left", padx=6)
        ttk.Button(bar, text="Open in spreadsheet", command=lambda: self.path and open_path(self.path)).pack(side="left")
        ttk.Button(bar, text="Heat pump cost…", command=self.costs).pack(side="right")
        self.label = ttk.Label(self, text="Run a study, or open a results.csv from an earlier one. "
                                          "Click a column heading to sort.", foreground="#55666f")
        self.label.pack(anchor="w", pady=6)
        frame = ttk.Frame(self)
        frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(frame, show="headings")
        ys = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        xs = ttk.Scrollbar(frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        xs.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        for v, colour in VERDICT_COLOURS.items():
            self.tree.tag_configure(v, foreground=colour)
        self.tree.bind("<Double-1>", lambda e: self.check_run())

    def open_results(self):
        p = filedialog.askopenfilename(filetypes=[("Results", "results.csv"), ("CSV", "*.csv")])
        if p:
            self.load(Path(p))

    def load(self, path: Path, extra: dict | None = None):
        self.path = Path(path)
        with open(self.path, newline="") as f:
            self.rows = list(csv.DictReader(f))
        if extra:
            for r in self.rows:
                r.update(extra.get(r.get("run"), {}))
        cols = [c for c in (self.rows[0].keys() if self.rows else []) if c != "folder"]
        self.tree.configure(columns=cols)
        for c in cols:
            self.tree.heading(c, text=c, command=lambda c=c: self.sort(c))
            self.tree.column(c, width=max(80, min(260, 8 * len(c) + 24)), anchor="w")
        self.fill()
        failed = sum(r.get("status") in ("FAILED", "NO OUTPUT") for r in self.rows)
        self.label.configure(text=f"{self.path}  ·  {len(self.rows)} runs"
                                  + (f"  ·  {failed} failed: don't use their numbers" if failed else "")
                                  + "  ·  double-click a run to explain its warnings")

    def fill(self):
        self.tree.delete(*self.tree.get_children())
        cols = self.tree["columns"]
        for i, r in enumerate(self.rows):
            self.tree.insert("", "end", iid=str(i), values=[r.get(c, "") for c in cols],
                             tags=(r.get("status", ""),))

    def sort(self, col):
        self.sort_desc = not self.sort_desc if self.sort_col == col else False
        self.sort_col = col

        def key(r):
            v = r.get(col, "")
            try:
                return (0, float(v))
            except (TypeError, ValueError):
                return (1, str(v))
        self.rows.sort(key=key, reverse=self.sort_desc)
        self.fill()

    def selected(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("Pick a run", "Select a run in the table first.")
            return None
        return self.rows[int(sel[0])]

    def open_folder(self):
        r = self.selected()
        if r and r.get("folder"):
            open_path(r["folder"])

    def check_run(self):
        r = self.selected()
        if r and r.get("folder") and (Path(r["folder"]) / "eplusout.err").exists():
            self.app.check_tab.add_targets([r["folder"]])
            self.app.notebook.select(self.app.check_tab)
        elif r:
            messagebox.showinfo("No output", "This run has no eplusout.err, so EnergyPlus didn't run or was stopped.")

    def open_report(self):
        if self.path and (self.path.parent / "check_report.html").exists():
            webbrowser.open((self.path.parent / "check_report.html").resolve().as_uri())
        else:
            messagebox.showinfo("No report", "There's no check_report.html next to this results.csv.")

    def costs(self):
        if not self.path:
            messagebox.showinfo("No results", "Open a results.csv first.")
            return
        dlg = tk.Toplevel(self)
        dlg.title("Heat pump cost estimate")
        dlg.transient(self.winfo_toplevel())
        f = ttk.Frame(dlg, padding=12)
        f.pack()
        ttk.Label(f, text="A simple estimate: heat delivered ÷ COP, priced hour by hour.\n"
                          "Uses each run's hourly DistrictHeatingWater:Facility.", foreground="#55666f").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        t = Tariffs()
        fields = [("Seasonal COP", "cop"), ("Flat rate (€/kWh)", "flat"), ("Day rate (€/kWh)", "day"),
                  ("Night rate (€/kWh)", "night"), ("Night starts (hour)", "night_start"),
                  ("Night ends (hour)", "night_end")]
        vars_ = {}
        for i, (label, k) in enumerate(fields, start=1):
            ttk.Label(f, text=label).grid(row=i, column=0, sticky="w", pady=2)
            vars_[k] = tk.StringVar(value=str(getattr(t, k)))
            ttk.Entry(f, textvariable=vars_[k], width=10).grid(row=i, column=1, sticky="w", padx=8)

        def go():
            try:
                tt = Tariffs(**{k: (int if k.startswith("night_") and k != "night" else float)(v.get())
                                for k, v in vars_.items()})
            except ValueError:
                messagebox.showerror("Numbers", "Enter numbers only.", parent=dlg)
                return
            rows, out = study_costs(self.path, tt)
            dlg.destroy()
            if not rows:
                messagebox.showinfo("No hourly heat", "No run has an hourly.csv with DistrictHeatingWater:Facility. "
                                    "Keep that line in the study's hourly output list and run again.")
                return
            self.load(self.path, extra={r["run"]: {k: v for k, v in r.items() if k not in ("run", "model")}
                                        for r in rows})
            messagebox.showinfo("Done", f"Added cost columns for {len(rows)} runs.\nAlso saved to {out}")

        ttk.Button(f, text="Calculate", command=go).grid(row=len(fields) + 1, column=1, sticky="e", pady=(10, 0))


# =============================================================================== App


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title(f"eptools {__version__}: EnergyPlus run checker and parameter studies")
        root.geometry("1180x800")
        root.minsize(900, 600)
        style = ttk.Style(root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        elif "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("Accent.TButton", font=("TkDefaultFont", 10, "bold"))
        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill="both", expand=True)
        self.check_tab = CheckTab(self, self.notebook)
        self.model_tab = ModelTab(self, self.notebook)
        self.study_tab = StudyTab(self, self.notebook)
        self.results_tab = ResultsTab(self, self.notebook)
        self.notebook.add(self.check_tab, text="  Explain warnings  ")
        self.notebook.add(self.study_tab, text="  Parameter study  ")
        self.notebook.add(self.results_tab, text="  Results  ")
        self.notebook.add(self.model_tab, text="  Model browser  ")
        status = ttk.Label(root, text="No AI and no internet: everything is worked out on this computer from the files you pick.",
                           foreground="#55666f", padding=(10, 2))
        status.pack(fill="x")


def main(argv=None) -> int:
    root = tk.Tk()
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
