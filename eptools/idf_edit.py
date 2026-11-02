"""Read, edit and write EnergyPlus IDF files without any third-party packages.

Objects you don't touch are written back exactly as they were, comments included.
Objects you change are rewritten in the usual one-field-per-line layout.

Fields can be addressed by:
  * the name in the "!- Field Name" comment that DesignBuilder, IDF Editor and
    IDFVersionUpdater write on every line (units in braces are ignored), or
  * a 1-based position, where 1 is the field straight after the class name
    (usually the object's Name).

    idf = IDF.load("Bungalow Original.idf")
    for obj in idf.find("Material", "EPS*"):
        print(obj.name, obj.get("Thickness"))
    idf.set_field("Material", "EPS*", "Thickness", 0.1)
    idf.save("out.idf")
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path

UNITS_RE = re.compile(r"\{[^}]*\}")


def _clean(name: str) -> str:
    return UNITS_RE.sub("", name).strip().lower()


def _fmt(value) -> str:
    if isinstance(value, float):
        text = repr(value)
        return text[:-2] if text.endswith(".0") else text
    return str(value)


@dataclass
class IDFObject:
    cls: str
    values: list[str]
    names: list[str]            # field comment for each value ("" if none)
    span: tuple[int, int]       # character range in the original text
    dirty: bool = False

    @property
    def name(self) -> str:
        return self.values[0] if self.values else ""

    def index_of(self, fld) -> int:
        """0-based position of a field given by comment name or 1-based number."""
        if isinstance(fld, int) or (isinstance(fld, str) and fld.isdigit()):
            i = int(fld) - 1
            if i < 0:
                raise KeyError(f"Field numbers start at 1, got {fld}")
            return i
        target = _clean(fld)
        for i, n in enumerate(self.names):
            if _clean(n) == target:
                return i
        raise KeyError(f"{self.cls} '{self.name}' has no field called '{fld}'. "
                       f"Fields: {', '.join(n for n in self.names if n)}")

    def get(self, fld) -> str:
        i = self.index_of(fld)
        return self.values[i] if i < len(self.values) else ""

    def set(self, fld, value) -> tuple[str, str]:
        i = self.index_of(fld)
        while len(self.values) <= i:
            self.values.append("")
            self.names.append("")
        old = self.values[i]
        self.values[i] = _fmt(value)
        self.dirty = True
        return old, self.values[i]

    def render(self) -> str:
        lines = [f"  {self.cls},"]
        last = len(self.values) - 1
        for i, (v, n) in enumerate(zip(self.values, self.names)):
            text = f"    {v}{';' if i == last else ','}"
            lines.append(f"{text:<28} !- {n}" if n else text)
        if not self.values:
            lines[0] = f"  {self.cls};"
        return "\n".join(lines)


@dataclass
class IDF:
    text: str
    objects: list[IDFObject] = field(default_factory=list)
    appended: list[str] = field(default_factory=list)
    removed: set[int] = field(default_factory=set)

    # ------------------------------------------------------------------ load
    @classmethod
    def load(cls, path) -> "IDF":
        return cls.parse(Path(path).read_text(encoding="latin-1"))

    @classmethod
    def parse(cls, text: str) -> "IDF":
        idf = cls(text)
        tokens: list[str] = []
        names: list[str] = []
        buf = ""
        start = None
        pos = 0
        for line in text.splitlines(keepends=True):
            code, bang, comment = line.partition("!")
            field_name = comment[1:].strip() if bang and comment.startswith("-") else ""
            first_on_line = True
            col = 0
            for ch in code:
                if ch in ",;":
                    if start is None and buf.strip():
                        start = pos + code.index(buf.strip()[0])
                    if start is not None:
                        tokens.append(buf.strip())
                        names.append(field_name if first_on_line else "")
                        first_on_line = False
                    buf = ""
                    if ch == ";" and start is not None:
                        end = pos + col + 1
                        rest = line[col + 1:].rstrip("\r\n")
                        if not rest.strip() or rest.strip().startswith("!"):
                            end = pos + len(line.rstrip("\r\n"))   # take the trailing comment with the object
                        idf.objects.append(IDFObject(tokens[0], tokens[1:], names[1:], (start, end)))
                        tokens, names, start = [], [], None
                else:
                    if start is None and not ch.isspace() and not buf.strip():
                        start = pos + col
                    buf += ch
                col += 1
            pos += len(line)
        return idf

    # ------------------------------------------------------------------ query
    def find(self, cls: str, name: str = "*") -> list[IDFObject]:
        """Objects of a class whose name matches a glob pattern (case-insensitive)."""
        c, pat = cls.lower(), name.lower()
        return [o for i, o in enumerate(self.objects)
                if i not in self.removed and o.cls.lower() == c and fnmatch.fnmatchcase(o.name.lower(), pat)]

    def classes(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for i, o in enumerate(self.objects):
            if i not in self.removed:
                out[o.cls] = out.get(o.cls, 0) + 1
        return out

    # ------------------------------------------------------------------ edit
    def set_field(self, cls, name, fld, value) -> list[tuple[str, str, str]]:
        changes = []
        for o in self.find(cls, name):
            label = o.name
            old, new = o.set(fld, value)
            changes.append((label, old, new))
        return changes

    def multiply_field(self, cls, name, fld, factor) -> list[tuple[str, str, str]]:
        changes = []
        for o in self.find(cls, name):
            old = o.get(fld)
            try:
                num = float(old)
            except ValueError:
                raise ValueError(f"{cls} '{o.name}' field '{fld}' is '{old}', which isn't a number, so it can't be multiplied")
            o.set(fld, num * float(factor))
            changes.append((o.name, old, o.get(fld)))
        return changes

    def replace_value(self, cls, name, old_value, new_value, fld="*") -> list[tuple[str, str, str]]:
        """Replace every field equal to old_value (e.g. a 21 C setpoint in a Schedule:Compact)."""
        changes = []
        old_s = str(old_value).strip().lower()
        for o in self.find(cls, name):
            idxs = range(len(o.values)) if fld == "*" else [o.index_of(fld)]
            for i in idxs:
                if i < len(o.values) and _same(o.values[i], old_s):
                    o.values[i] = _fmt(new_value)
                    o.dirty = True
                    changes.append((o.name, str(old_value), o.values[i]))
        return changes

    def remove(self, cls, name="*") -> int:
        n = 0
        c, pat = cls.lower(), name.lower()
        for i, o in enumerate(self.objects):
            if i not in self.removed and o.cls.lower() == c and fnmatch.fnmatchcase(o.name.lower(), pat):
                self.removed.add(i)
                n += 1
        return n

    def add(self, idf_text: str) -> None:
        self.appended.append(idf_text.strip())

    # ------------------------------------------------------------------ save
    def to_text(self) -> str:
        nl = "\r\n" if "\r\n" in self.text else "\n"   # keep the file's own line endings
        out, cursor = [], 0
        for i, o in enumerate(self.objects):
            s, e = o.span
            out.append(self.text[cursor:s])
            if i in self.removed:
                out.append(f"! removed by eptools: {o.cls} {o.name}".rstrip())
            elif o.dirty:
                out.append(o.render().lstrip().replace("\n", nl))
            else:
                out.append(self.text[s:e])
            cursor = e
        out.append(self.text[cursor:])
        if self.appended:
            out.append(f"{nl}{nl}! ---- added by eptools ----{nl}")
            out.extend(f"  {t}{nl}" for t in self.appended)
        return "".join(out)

    def save(self, path) -> None:
        Path(path).write_text(self.to_text(), encoding="latin-1")


def _same(value: str, target_lower: str) -> bool:
    v = value.strip().lower()
    if v == target_lower:
        return True
    try:
        return float(v) == float(target_lower)
    except ValueError:
        return False
