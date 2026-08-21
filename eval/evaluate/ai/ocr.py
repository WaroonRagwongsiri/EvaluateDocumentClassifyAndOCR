"""Per-page OCR wrapper.

Lifts the loop shape from wind's pipeline_steps.get_ocr_text but drops Streamlit
session_state and the in-process cache — the worker OCRs each file_pages row once
and writes straight to DB, so an in-process cache would only mask re-runs. The
client is passed explicitly (no `ctx`), matching how the worker drives it.

Returns the standard do_chat-shaped dict: {content, thinking, raw, latency_s, error}.
"""
from __future__ import annotations

from typing import Any

from . import openai_client, tasks
from .. import config


def ocr_page(image_b64: str) -> dict:
    """OCR a single page image -> the do_chat dict.

    `content` holds the transcribed text (empty string on error);
    `error` is None on success. The worker writes `content` to file_pages.ai_ocr_text
    and the whole dict to ai_ocr_raw.
    """
    return openai_client.chat(
        model=config.MODEL_NAME,
        prompt=tasks.OCR_PROMPT,
        images_b64=[image_b64],
        temperature=config.LLM_TEMPERATURE,
        think=config.LLM_THINK,
        base_url=config.LLM_ENDPOINT,
        api_key=config.MODEL_API_KEY,
    )
