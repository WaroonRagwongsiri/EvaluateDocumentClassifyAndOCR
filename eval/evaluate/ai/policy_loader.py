"""Loads attachment-type metadata (policy/TABLE.md), per-type extraction schemas
(policy/N_fieldname.jsonc), and per-type process pipelines (policy/PIPELINES.jsonc)."""
import json
import re
from pathlib import Path

POLICY_DIR = Path(__file__).parent / "policy"
TABLE_PATH = POLICY_DIR / "TABLE.md"
PIPELINES_PATH = POLICY_DIR / "PIPELINES.jsonc"

_ROW_RE = re.compile(
    r"^\|\s*(\d+)\s*\|[^|]*\|\s*`attachment\.(\w+)`\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*$",
    re.MULTILINE,
)


def list_attachment_types() -> list[dict]:
    """Parse policy/TABLE.md into [{"number", "field", "description_th", "mandatory", "skip"}, ...]."""
    text = TABLE_PATH.read_text(encoding="utf-8")
    types = []
    for match in _ROW_RE.finditer(text):
        number, field, description_th, note = match.groups()
        types.append(
            {
                "number": int(number),
                "field": field,
                "description_th": description_th,
                "mandatory": "บังคับ" in note,
                "skip": "skip" in note.lower(),
            }
        )
    return types


def list_classifiable_types() -> list[dict]:
    """Same as list_attachment_types(), minus rows marked "skip" in TABLE.md
    (catch-all/non-visual types like another_document, officer_document_requested,
    and applicant_signature — not meaningful for the classifier to pick between).
    Each entry additionally carries "title_indicators" mined from its schema,
    used by the classification prompt."""
    types = [t for t in list_attachment_types() if not t["skip"]]
    for t in types:
        t["title_indicators"] = title_indicators(t["field"])
    return types


def _find_schema_path(field_name: str) -> Path | None:
    matches = list(POLICY_DIR.glob(f"*_{field_name}.jsonc"))
    return matches[0] if matches else None


def load_schema(field_name: str) -> str:
    """Return the raw .jsonc text for an attachment type (e.g. "factory_building_diagram").

    Raises FileNotFoundError with a clear message for certifier types folded into
    their base document's schema (e.g. factory_building_plan_certifier is just a
    field inside factory_building_plan's schema, not its own file) or any type
    with no schema file yet.
    """
    path = _find_schema_path(field_name)
    if path is None:
        raise FileNotFoundError(
            f'No policy schema file found for "attachment.{field_name}" — it may be folded '
            "into another type's schema (e.g. a certifier field merged into its base "
            "document), or its schema hasn't been drafted yet."
        )
    return path.read_text(encoding="utf-8")


def is_mockup(field_name: str) -> bool:
    """True if the type's schema is a speculative draft (no real source document yet)."""
    return "// MOCKUP:" in load_schema(field_name)


def _strip_jsonc_comments(text: str) -> str:
    """Remove // line comments so .jsonc config files can be json.loads()'d.
    (Keep URLs out of config values — 'http://...' would be eaten.)"""
    text = re.sub(r"^\s*//.*$", "", text, flags=re.MULTILINE)
    text = re.sub(r'\s+//[^"\n]*$', "", text, flags=re.MULTILINE)
    return text


_DEFAULT_PIPELINE = {
    "steps": ["rotation", "extract"],
    "extract_mode": "direct",
    "prompts": {},
}


def load_all_pipelines() -> dict:
    """Raw dict of every entry in policy/PIPELINES.jsonc (including "default"),
    comments stripped, unmerged — used by the pipeline config editor page."""
    if not PIPELINES_PATH.exists():
        return {"default": dict(_DEFAULT_PIPELINE)}
    return json.loads(_strip_jsonc_comments(PIPELINES_PATH.read_text(encoding="utf-8")))


def _extract_header_comments(text: str) -> str:
    """Leading blank/comment lines right after PIPELINES.jsonc's opening '{', kept
    when the file is rewritten by the config editor so hand-written docs survive."""
    lines = text.splitlines()
    header, started = [], False
    for line in lines:
        stripped = line.strip()
        if not started:
            if stripped == "{":
                started = True
            continue
        if stripped == "" or stripped.startswith("//"):
            header.append(line)
        else:
            break
    return "\n".join(header)


def save_pipelines(config: dict) -> None:
    """Overwrite policy/PIPELINES.jsonc with `config` (the full raw dict, as returned
    by load_all_pipelines() with edits applied). Preserves the file's leading
    documentation comment block; any commented-out/disabled example entries are
    NOT preserved (the file is normalized to plain JSON + that header on save)."""
    header = _extract_header_comments(PIPELINES_PATH.read_text(encoding="utf-8")) \
        if PIPELINES_PATH.exists() else ""
    body_lines = json.dumps(config, ensure_ascii=False, indent=4).splitlines()
    if header:
        body_lines = [body_lines[0], header, ""] + body_lines[1:]
    PIPELINES_PATH.write_text("\n".join(body_lines) + "\n", encoding="utf-8")


def get_pipeline(field_name: str, valid_steps=None) -> dict:
    """Return the type's process pipeline from policy/PIPELINES.jsonc, merged over
    the "default" entry. Shape: {"steps": [...], "extract_mode": str, "prompts": {...}}.
    "prompts" are per-type preconfigured prompt overrides (still editable in the UI).
    """
    cfg = load_all_pipelines()
    default = cfg.get("default", _DEFAULT_PIPELINE)
    pipeline = {**default, **cfg.get(field_name, {})}
    pipeline.setdefault("prompts", {})
    if valid_steps is not None:
        unknown = [s for s in pipeline["steps"] if s not in valid_steps]
        if unknown:
            raise ValueError(
                f"PIPELINES.jsonc: unknown step(s) {unknown} for '{field_name}' — "
                f"valid steps: {sorted(valid_steps)}"
            )
    return pipeline


def title_indicators(field_name: str) -> list[str]:
    """Document-title indicators for a type, mined from its schema's "เป็น..." boolean
    keys (each schema starts with is-this-document-X checks whose key text closely
    matches the real document's printed title/heading)."""
    try:
        schema = load_schema(field_name)
    except FileNotFoundError:
        return []
    return [m[len("เป็น"):] for m in re.findall(r'"(เป็น[^"]+)"\s*:', schema)]
