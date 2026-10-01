from __future__ import annotations

import base64
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import httpx

from card_translator.config import (
    CARD_IMAGE_PROVIDERS,
    anthropic_api_base_url,
    anthropic_api_key,
    anthropic_model,
    gemini_api_key,
    gemini_model,
    openai_api_base_url,
    openai_api_key,
    openai_model,
)
from card_translator.providers import gemini

_IMAGE_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


def provider_key(provider: str) -> str:
    return {
        "gemini": gemini_api_key,
        "openai": openai_api_key,
        "anthropic": anthropic_api_key,
    }[provider]()


def provider_model(provider: str) -> str:
    return {
        "gemini": gemini_model,
        "openai": openai_model,
        "anthropic": anthropic_model,
    }[provider]()


def provider_available(provider: str) -> bool:
    return provider in CARD_IMAGE_PROVIDERS and bool(provider_key(provider))


def _strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Require the exact fields already requested by the card prompt."""
    result = deepcopy(schema)

    def close_objects(node: dict[str, Any]) -> None:
        if node.get("type") == "object":
            properties = node.get("properties") or {}
            node["required"] = list(properties)
            node["additionalProperties"] = False
            for child in properties.values():
                if isinstance(child, dict):
                    close_objects(child)
        elif node.get("type") == "array" and isinstance(node.get("items"), dict):
            close_objects(node["items"])

    close_objects(result)
    return result


def _post(url: str, *, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
    try:
        response = httpx.post(url, headers=headers, json=body, timeout=120)
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(
            f"Card image provider request failed (HTTP {exc.response.status_code})"
        ) from exc
    except httpx.RequestError as exc:
        raise RuntimeError("Card image provider could not be reached") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError("Card image provider returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise TypeError("Card image provider returned an invalid response")
    return payload


def _openai_image(prompt: str, encoded: str, mime: str, schema: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    payload = _post(
        f"{openai_api_base_url()}/responses",
        headers={"Authorization": f"Bearer {openai_api_key()}"},
        body={
            "model": openai_model(),
            "store": False,
            "input": [{
                "role": "user",
                "content": [
                    {"type": "input_image", "image_url": f"data:{mime};base64,{encoded}"},
                    {"type": "input_text", "text": prompt},
                ],
            }],
            "text": {"format": {
                "type": "json_schema", "name": "card_image_translation",
                "schema": _strict_schema(schema), "strict": True,
            }},
        },
    )
    if payload.get("status") == "incomplete":
        raise RuntimeError("OpenAI card response was incomplete")
    text = "".join(
        str(part.get("text") or "")
        for item in payload.get("output") or []
        for part in item.get("content") or []
        if part.get("type") == "output_text"
    ).strip()
    if not text:
        raise RuntimeError("OpenAI returned no card JSON")
    usage = payload.get("usage")
    return text, usage if isinstance(usage, dict) else {}


def _anthropic_image(
    prompt: str, encoded: str, mime: str, schema: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    payload = _post(
        f"{anthropic_api_base_url()}/messages",
        headers={
            "x-api-key": anthropic_api_key(),
            "anthropic-version": "2023-06-01",
        },
        body={
            "model": anthropic_model(),
            "max_tokens": 8192,
            "messages": [{"role": "user", "content": [
                {"type": "image", "source": {
                    "type": "base64", "media_type": mime, "data": encoded,
                }},
                {"type": "text", "text": prompt},
            ]}],
            "output_config": {"format": {
                "type": "json_schema", "schema": _strict_schema(schema),
            }},
        },
    )
    if payload.get("stop_reason") in {"max_tokens", "refusal"}:
        raise RuntimeError(f"Anthropic card response stopped: {payload['stop_reason']}")
    text = "".join(
        str(part.get("text") or "")
        for part in payload.get("content") or []
        if part.get("type") == "text"
    ).strip()
    if not text:
        raise RuntimeError("Anthropic returned no card JSON")
    usage = payload.get("usage")
    return text, usage if isinstance(usage, dict) else {}


def analyze_card_image(
    image_path: Path, *, prompt: str, schema: dict[str, Any], provider: str
) -> dict[str, Any]:
    if provider not in CARD_IMAGE_PROVIDERS:
        raise ValueError("Unknown card image provider")
    if not provider_available(provider):
        raise RuntimeError(f"{provider.upper()}_API_KEY is not configured")
    if provider == "gemini":
        result = gemini.analyze_card_image(image_path, prompt=prompt, schema=schema)
        trace = result.pop("_gemini_trace", {})
        result["_card_image_trace"] = {**trace, "provider": provider}
        return result
    mime = _IMAGE_MIME.get(image_path.suffix.casefold())
    if not mime:
        raise ValueError("Card image format is not supported")
    encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
    raw, usage = (
        _openai_image(prompt, encoded, mime, schema)
        if provider == "openai"
        else _anthropic_image(prompt, encoded, mime, schema)
    )
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Card image provider returned invalid card JSON") from exc
    if not isinstance(result, dict):
        raise TypeError("Card image provider returned an invalid card object")
    result["_card_image_trace"] = {
        "raw_response": raw, "usage": usage, "model": provider_model(provider),
        "provider": provider,
    }
    return result
