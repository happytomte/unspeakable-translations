from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Any

import httpx

from card_translator.config import gemini_api_base_url, gemini_api_key, gemini_model


_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
_RETRY_DELAYS_SECONDS = (0.75, 2.0)


def available() -> bool:
    return bool(gemini_api_key())


def _generate(
    parts: list[dict[str, Any]], *, schema: dict[str, Any] | None = None
) -> tuple[str, dict[str, Any]]:
    key = gemini_api_key()
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not configured")
    generation: dict[str, Any] = {"temperature": 0.2}
    if schema:
        generation.update({"responseMimeType": "application/json", "responseSchema": schema})
    url = f"{gemini_api_base_url()}/models/{gemini_model()}:generateContent"
    response: httpx.Response | None = None
    last_error: httpx.HTTPError | None = None
    attempts = len(_RETRY_DELAYS_SECONDS) + 1
    for attempt in range(attempts):
        try:
            response = httpx.post(
                url,
                headers={"x-goog-api-key": key},
                json={"contents": [{"role": "user", "parts": parts}], "generationConfig": generation},
                timeout=120,
            )
            response.raise_for_status()
            break
        except httpx.HTTPStatusError as exc:
            last_error = exc
            if exc.response.status_code not in _RETRYABLE_STATUS_CODES or attempt >= attempts - 1:
                break
        except httpx.RequestError as exc:
            last_error = exc
            if attempt >= attempts - 1:
                break
        time.sleep(_RETRY_DELAYS_SECONDS[attempt])
    else:  # pragma: no cover - the loop always exits through break or exhaustion
        response = None

    if last_error is not None and (response is None or response.is_error):
        if isinstance(last_error, httpx.HTTPStatusError):
            status = last_error.response.status_code
            if status in _RETRYABLE_STATUS_CODES:
                raise RuntimeError(
                    f"Gemini ist nach {attempts} Versuchen vorübergehend nicht erreichbar "
                    f"(Google HTTP {status}). Google ist möglicherweise ausgelastet; "
                    "bitte versuche es in einem Moment erneut."
                ) from last_error
        raise RuntimeError(f"Gemini request failed after {attempts} attempts: {last_error}") from last_error

    if response is None:  # defensive; keeps the response type narrow below
        raise RuntimeError("Gemini request failed without a response")
    payload = response.json()
    candidates = payload.get("candidates") or []
    if not candidates:
        raise RuntimeError("Gemini returned no candidate")
    parts_out = ((candidates[0].get("content") or {}).get("parts") or [])
    text = "".join(str(part.get("text") or "") for part in parts_out).strip()
    if not text:
        raise RuntimeError("Gemini returned no text")
    usage = payload.get("usageMetadata")
    return text, usage if isinstance(usage, dict) else {}


def translate_prompt(prompt: str, **_kwargs: str) -> str:
    text, _usage = _generate([{"text": prompt}])
    return text


def analyze_card_image(
    image_path: Path, *, prompt: str, schema: dict[str, Any]
) -> dict[str, Any]:
    mime = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
    }.get(image_path.suffix.casefold())
    if not mime:
        raise ValueError("Gemini does not support this card image format")
    encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
    raw, usage = _generate(
        [{"text": prompt}, {"inlineData": {"mimeType": mime, "data": encoded}}],
        schema=schema,
    )
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise RuntimeError("Gemini returned an invalid card object")
    value["_gemini_trace"] = {"raw_response": raw, "usage": usage, "model": gemini_model()}
    return value
