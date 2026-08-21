"""Classification wrapper: page-1 image -> (predicted|None, status, error, raw).

Thin layer over the vendored openai_client + tasks. The prompt is content-only
(no declared type) so classification is genuinely per-sha256 — exactly the thing
the eval measures against each declared context.

Status values mirror files.ai_class_status:
  done  — model returned one of the 19; `predicted` set.
  none  — model returned the `none` sentinel; `predicted` is None.
  error — the call failed or the response couldn't be parsed; `error` set.
"""
from __future__ import annotations

from typing import Any

from . import openai_client, policy_loader, tasks
from .. import config

# Cache the 19 classifiable types + their prompt once. policy_loader reads from
# disk (TABLE.md + the per-type .jsonc schemas); these never change at runtime.
_CLASSIFIABLE_TYPES: list[dict] = policy_loader.list_classifiable_types()
_VALID_FIELDS: list[str] = [t["field"] for t in _CLASSIFIABLE_TYPES]
_CLASSIFICATION_PROMPT: str = tasks.build_classification_prompt(_CLASSIFIABLE_TYPES)


def classify_page(image_b64: str) -> tuple[str | None, str, str | None, dict]:
    """Classify a single page image.

    Returns (predicted, status, error, raw):
      predicted — one of the 19 field keys, or None (status='none'/'error').
      status     — 'done' | 'none' | 'error'.
      error      — human-readable error string, or None on success.
      raw        — the full do_chat dict ({content,thinking,raw,latency_s,error}).
    """
    result = openai_client.chat(
        model=config.MODEL_NAME,
        prompt=_CLASSIFICATION_PROMPT,
        images_b64=[image_b64],
        temperature=config.LLM_TEMPERATURE,
        think=config.LLM_THINK,
        base_url=config.LLM_ENDPOINT,
        api_key=config.MODEL_API_KEY,
    )

    # Transport/API-level failure short-circuits before parsing.
    if result.get("error"):
        return None, "error", result["error"], result

    content = result.get("content") or ""
    field, parse_err = tasks.parse_classification(content, _VALID_FIELDS)
    if parse_err is not None:
        return None, "error", parse_err, result
    if field == "none":
        return None, "none", None, result
    return field, "done", None, result


def classifiable_fields() -> list[str]:
    """The 19 valid classification outputs (for OOV derivation in the server)."""
    return list(_VALID_FIELDS)
