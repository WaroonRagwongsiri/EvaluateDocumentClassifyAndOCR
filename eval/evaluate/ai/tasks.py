"""Fixed prompt text + response parsers for the 3 test-harness task modes."""
import json
import re

EXTRACTION_PROMPT = """You are a document data extraction assistant.
Look at the provided document image(s) — this may be one or more pages of the
same document — and extract all visible text and structured fields (e.g. names,
dates, ids, amounts, addresses, form labels/values) into a single JSON object
that combines information across all provided pages (e.g. a line-items table
split across pages should become one merged array). If a field is illegible or
absent, omit it rather than guessing.

Respond with ONLY the JSON object, no markdown code fences, no commentary."""

ROTATION_PROMPT = """Look at this document image and find a short line of normal, upright-when-correctly-oriented
printed text in it. Focus on which edge of the IMAGE FRAME the TOP of the letters currently faces.

Answer with exactly one word, no punctuation, no other text:
- "top" if the text reads normally, left-to-right, top-of-letters facing the top edge of the image (no rotation)
- "bottom" if the image is upside-down (top-of-letters facing the bottom edge of the image)
- "left" if the top-of-letters faces the LEFT edge of the image (you'd tilt your head left to read it)
- "right" if the top-of-letters faces the RIGHT edge of the image (you'd tilt your head right to read it)"""

# Degrees to rotate the image CLOCKWISE to correct it, given which edge the
# top-of-text currently faces. Asking the model directly for a clockwise/degree
# answer is unreliable (it confuses rotation direction); asking it to identify
# an edge is a plain perception question and empirically far more accurate.
EDGE_TO_CW_CORRECTION = {"top": 0, "left": 90, "bottom": 180, "right": 270}

SIGNATURE_PROMPT = """Look at this document image and find any handwritten
signatures present.

For each signature found, output its bounding box using this exact JSON
format, with coordinates normalized to a 0-1000 scale (0,0 = top-left,
1000,1000 = bottom-right of the image):

{"bbox_2d": [x1, y1, x2, y2], "label": "signature"}

If there are multiple signatures, output a JSON array of such objects.
If there is no signature, output an empty JSON array: []

Respond with ONLY the JSON, no markdown code fences, no commentary."""

OCR_PROMPT = """You are an OCR assistant. Look at the provided document image(s) — this may
be one or more pages of the same document — and transcribe ALL visible text exactly as it
appears, in natural reading order, preserving line breaks and paragraph structure where
sensible. If multiple images are provided, separate each page's transcription with a line
"--- Page N ---" (N starting at 1, in the order the images were given).

Do not summarize, translate, correct spelling, or add commentary — output only the
transcribed text."""

QA_PROMPT_TEMPLATE = """You are a document question-answering assistant. Look at the provided
document image(s) — this may be one or more pages of the same document — and answer the
question using only information visible in the document. If the answer cannot be found in
the document, say so clearly instead of guessing.

Question: {question}

Answer concisely."""

# Used by "QA with OCR": the document is OCR'd to plain text first, then this
# text-only prompt answers the question from that text (no images sent this time).
QA_WITH_OCR_PROMPT_TEMPLATE = """You are a document question-answering assistant. Below is
OCR-extracted text from one or more pages of a document. Answer the question using only
this text as context. If the answer cannot be found in the text, say so clearly instead of
guessing.

--- OCR TEXT ---
{ocr_text}
--- END OCR TEXT ---

Question: {question}

Answer concisely."""

def build_classification_prompt(attachment_types: list[dict]) -> str:
    """Build the doc-type classification prompt from policy_loader.list_classifiable_types().

    Each entry includes the type's TABLE.md description plus title indicators mined from
    its schema's "เป็น..." keys — the one-line descriptions alone are not discriminative
    enough (verified: a certification letter BY an engineer got classified as the
    engineer's license copy until the real document titles were included).
    """
    lines = []
    for t in attachment_types:
        entry = f"- {t['field']} — {t['description_th']}"
        indicators = t.get("title_indicators") or []
        if indicators:
            entry += "\n  หัวเรื่องเอกสารที่พบได้ในประเภทนี้: " + " / ".join(indicators)
        lines.append(entry)
    type_list = "\n".join(lines)
    return f"""You are classifying a Thai factory-permit attachment document.
Look at the provided document image — pay close attention to the document's printed
title/heading — and decide which ONE of the following attachment types it is.
Each entry is: field_name — Thai description, then typical document titles for that type.
A document whose printed title matches an entry's หัวเรื่อง list belongs to that type.

{type_list}

Respond with ONLY the field_name of the best match (exactly as written above,
e.g. factory_safety_certificate). If the document matches none of these types,
respond with exactly: none

No other text, no explanation."""


def parse_classification(text: str, valid_fields: list[str]) -> tuple[str | None, str | None]:
    """Find which valid field name the model answered with.

    Longest-first matching, since some field names are prefixes of others
    (e.g. power_of_attorney_with_revenue_stamp vs power_of_attorney_competent_authority
    share a prefix). Returns (field, None), ("none", None), or (None, error).
    """
    for field in sorted(valid_fields, key=len, reverse=True):
        if field in text:
            return field, None
    if re.search(r"\bnone\b", text.lower()):
        return "none", None
    return None, "Could not find a valid attachment type (or 'none') in response"


SCHEMA_EXTRACTION_PROMPT_TEMPLATE = """You are a document data extraction assistant.
Look at the provided document image(s) and fill in the JSON schema below with the real
values found in the document.

Rules:
- Keep exactly the same keys and structure as the schema.
- The schema's values are placeholders describing what to put there (e.g. "true/false"
  means answer true or false; Thai placeholder text describes the expected content).
- Where the schema shows an array with an example item plus an empty/comment item,
  output one item per real occurrence found in the document (or an empty array if none).
- Where a value cannot be found in the document, use null — never guess and never leave
  the placeholder text.

--- SCHEMA ---
{schema_json}
--- END SCHEMA ---

Respond with ONLY the filled-in JSON object, no markdown code fences, no commentary."""

SCHEMA_EXTRACTION_WITH_OCR_PROMPT_TEMPLATE = """You are a document data extraction assistant.
Look at the provided document image(s) and fill in the JSON schema below with the real
values found in the document. OCR-transcribed text of the same page(s) is provided as
additional context to help you read the text accurately — but visual questions (signatures,
stamps, layout) must still be answered from the images.

Rules:
- Keep exactly the same keys and structure as the schema.
- The schema's values are placeholders describing what to put there (e.g. "true/false"
  means answer true or false; Thai placeholder text describes the expected content).
- Where the schema shows an array with an example item plus an empty/comment item,
  output one item per real occurrence found in the document (or an empty array if none).
- Where a value cannot be found in the document, use null — never guess and never leave
  the placeholder text.

--- OCR TEXT ---
{ocr_text}
--- END OCR TEXT ---

--- SCHEMA ---
{schema_json}
--- END SCHEMA ---

Respond with ONLY the filled-in JSON object, no markdown code fences, no commentary."""


TASKS = {
    "Document extraction": EXTRACTION_PROMPT,
    "Rotate image": ROTATION_PROMPT,
    "Signature localization": SIGNATURE_PROMPT,
}


def _outermost_json_slice(text: str) -> str | None:
    """Find the substring between the first '{' or '[' and the matching last '}'/']'."""
    start_obj = text.find("{")
    start_arr = text.find("[")
    candidates = [s for s in (start_obj, start_arr) if s != -1]
    if not candidates:
        return None
    start = min(candidates)
    end_obj = text.rfind("}")
    end_arr = text.rfind("]")
    end = max(end_obj, end_arr)
    if end == -1 or end <= start:
        return None
    return text[start : end + 1]


def parse_extraction(text: str) -> tuple[dict | None, str | None]:
    slice_ = _outermost_json_slice(text)
    if slice_ is None:
        return None, "No JSON object found in response"
    try:
        parsed = json.loads(slice_)
    except json.JSONDecodeError as exc:
        return None, f"JSON parse error: {exc}"
    if not isinstance(parsed, dict):
        return None, "Parsed JSON was not an object"
    return parsed, None


def parse_rotation(text: str) -> tuple[int | None, str | None]:
    match = re.search(r"\b(top|bottom|left|right)\b", text.lower())
    if not match:
        return None, "Could not find an edge (top/bottom/left/right) in response"
    return EDGE_TO_CW_CORRECTION[match.group(1)], None


def parse_signature_boxes(text: str) -> tuple[list[dict], str | None]:
    slice_ = _outermost_json_slice(text)
    if slice_ is not None:
        try:
            parsed = json.loads(slice_)
            if isinstance(parsed, dict):
                return [parsed], None
            if isinstance(parsed, list):
                return parsed, None
        except json.JSONDecodeError:
            pass

    tag_matches = re.findall(
        r"<box>\(?(\d+),\s*(\d+)\)?,\s*\(?(\d+),\s*(\d+)\)?</box>", text
    )
    if tag_matches:
        boxes = [
            {"bbox_2d": [int(x1), int(y1), int(x2), int(y2)], "label": "signature"}
            for x1, y1, x2, y2 in tag_matches
        ]
        return boxes, None

    return [], "Could not parse any bounding boxes from response"
