"""Rebuild docs/index.html from the template, the rules in eptools/check.py and the example .err.

    python docs/build_explainer.py
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from eptools.check import RULES  # noqa: E402


def js(obj) -> str:
    return json.dumps(obj).replace("</", "<\\/")


rules = [[r.id, r.pattern, r.verdict, r.title, r.meaning, r.action] for r in RULES]
example = (ROOT / "tests" / "data" / "bungalow.err").read_text(errors="replace")
page = (ROOT / "docs" / "explainer_template.html").read_text()
page = page.replace("__RULES__", js(rules)).replace("__EXAMPLE__", js(example))
html = "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">\n<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
head, _, body = page.partition("</style>")
(ROOT / "docs" / "index.html").write_text(html + head + "</style>\n</head><body>\n" + body + "\n</body></html>\n", encoding="utf-8")
print("Wrote docs/index.html")
