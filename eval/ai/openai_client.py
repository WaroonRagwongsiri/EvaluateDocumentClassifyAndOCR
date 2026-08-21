"""Thin HTTP wrapper for OpenAI-compatible chat completion APIs (e.g. ModelHarbor)."""
import re
import time

import requests

# ModelHarbor's proxy (litellm) returns 429 with a message like "...Try again in
# 15 seconds..." when a specific model's backend deployment is temporarily
# unavailable/cooling down — this is transient and distinct from a hard quota
# block, so it's worth a bounded automatic retry rather than failing immediately.
MAX_429_RETRIES = 2
MAX_RETRY_WAIT_S = 30
DEFAULT_RETRY_WAIT_S = 5


def _parse_retry_after_seconds(resp: requests.Response) -> float | None:
    try:
        message = str(resp.json().get("error", {}).get("message", ""))
    except ValueError:
        return None
    match = re.search(r"[Tt]ry again in (\d+(?:\.\d+)?)\s*seconds?", message)
    return float(match.group(1)) if match else None


def list_models(base_url: str, api_key: str) -> list[dict]:
    """GET {base_url}/models -> [{"name": ..., "capabilities": []}, ...].

    Capabilities are always empty here — the OpenAI /v1/models schema doesn't
    expose vision/tools/thinking flags the way Ollama's /api/tags does.
    """
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    resp = requests.get(f"{base_url.rstrip('/')}/models", headers=headers, timeout=10)
    resp.raise_for_status()
    data = resp.json().get("data", [])
    return [{"name": m["id"], "capabilities": []} for m in data]


def _to_openai_message(msg: dict) -> dict:
    """Convert an internal message ({"role","content","images": [b64,...]}) to
    OpenAI's content-parts format for multimodal messages."""
    images = msg.get("images")
    if not images:
        return {"role": msg["role"], "content": msg["content"]}
    parts = [{"type": "text", "text": msg["content"]}]
    for img_b64 in images:
        parts.append(
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_b64}"}}
        )
    return {"role": msg["role"], "content": parts}


def chat_messages(
    model: str,
    messages: list[dict],
    temperature: float,
    think: bool,
    base_url: str,
    api_key: str,
) -> dict:
    """POST {base_url}/chat/completions with an OpenAI-compatible request.

    Returns {"content", "thinking", "raw", "latency_s", "error"}.
    "think" isn't sent — there's no standard OpenAI field for it — but if the
    provider's response includes a "reasoning_content" field (as DeepSeek's
    API does), it's surfaced as "thinking" anyway.
    """
    payload = {
        "model": model,
        "messages": [_to_openai_message(m) for m in messages],
        "temperature": temperature,
    }
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    url = f"{base_url.rstrip('/')}/chat/completions"

    start = time.perf_counter()
    attempt = 0
    while True:
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=300)
        except requests.RequestException as exc:
            return {
                "content": "",
                "thinking": None,
                "raw": None,
                "latency_s": time.perf_counter() - start,
                "error": f"Could not reach {base_url} ({exc})",
            }

        if resp.status_code == 429 and attempt < MAX_429_RETRIES:
            wait_s = min(_parse_retry_after_seconds(resp) or DEFAULT_RETRY_WAIT_S, MAX_RETRY_WAIT_S)
            time.sleep(wait_s)
            attempt += 1
            continue

        try:
            resp.raise_for_status()
        except requests.RequestException as exc:
            retry_note = f" (after {attempt} retry attempt(s))" if attempt else ""
            return {
                "content": "",
                "thinking": None,
                "raw": None,
                "latency_s": time.perf_counter() - start,
                "error": f"HTTP error from {base_url}{retry_note}: {exc}. Response: {resp.text[:300]}",
            }
        raw = resp.json()
        break

    latency_s = time.perf_counter() - start
    message = (raw.get("choices") or [{}])[0].get("message", {})
    return {
        "content": message.get("content") or "",
        "thinking": message.get("reasoning_content"),
        "raw": raw,
        "latency_s": latency_s,
        "error": None,
    }


def chat(
    model: str,
    prompt: str,
    images_b64: list[str] | None,
    temperature: float,
    think: bool,
    base_url: str,
    api_key: str,
) -> dict:
    """POST {base_url}/chat/completions with a single user message (optionally with images)."""
    message: dict = {"role": "user", "content": prompt}
    if images_b64:
        message["images"] = images_b64
    return chat_messages(model, [message], temperature, think, base_url, api_key)
