"""string.Template loader + html.escape helpers.

Templates are .html files in this package's templates/ dir, using $name
placeholders (string.Template syntax). Loading is cached after first read.
All dynamic text inserted into a template is html.escaped by the caller via
esc(); template files themselves contain only static HTML + placeholders, so
they are trusted and not escaped.
"""
from __future__ import annotations

import html
from pathlib import Path
from string import Template

from eval import config

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
_CACHE: dict[str, Template] = {}


def _load(name: str) -> Template:
    if name not in _CACHE:
        _CACHE[name] = Template((_TEMPLATES_DIR / name).read_text(encoding="utf-8"))
    return _CACHE[name]


def render(name: str, **mapping) -> str:
    """Render templates/<name> with the given $placeholder -> value mapping."""
    return _load(name).safe_substitute(**mapping)


def base(title: str, body: str, *, nav_home: str = "", nav_dash: str = "",
         nav_classify: str = "", run_state: str = "", model_name: str = "") -> str:
    """Wrap a body fragment in the page shell. nav_home/nav_dash/nav_classify are
    the 'active' class for the corresponding header link; run_state is a small
    status string; model_name is the LLM model shown in the header bar
    (config.MODEL_NAME)."""
    return render("base.html",
                  title=html.escape(title), body=body,
                  nav_home=nav_home, nav_dash=nav_dash, nav_classify=nav_classify,
                  run_state=html.escape(run_state),
                  model_name=html.escape(model_name or config.MODEL_NAME))


def esc(x) -> str:
    """html.escape a value (None -> empty string)."""
    if x is None:
        return ""
    return html.escape(str(x))
