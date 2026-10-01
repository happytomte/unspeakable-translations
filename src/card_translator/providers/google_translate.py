from __future__ import annotations

import html

import httpx

from card_translator.config import google_translation_api_key, google_translation_api_url


def available() -> bool:
    return bool(google_translation_api_key())


def translate_text(text: str, *, source_language: str, target_language: str) -> str:
    key = google_translation_api_key()
    if not key:
        raise RuntimeError("GOOGLE_CLOUD_TRANSLATION_API_KEY is not configured")
    response = httpx.post(
        google_translation_api_url(),
        headers={"X-goog-api-key": key},
        json={
            "q": text,
            "source": source_language,
            "target": target_language,
            "format": "text",
        },
        timeout=30,
    )
    response.raise_for_status()
    values = ((response.json().get("data") or {}).get("translations") or [])
    if not values or not values[0].get("translatedText"):
        raise RuntimeError("Google Cloud Translation returned no translation")
    return html.unescape(str(values[0]["translatedText"]))
