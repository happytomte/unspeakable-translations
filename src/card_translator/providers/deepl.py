"""Dormant DeepL adapter, kept separate from the active TranslateGemma workflow."""

from __future__ import annotations

import httpx

from card_translator.config import deepl_api_url, deepl_auth_key


def translate_texts(texts: list[str], *, source_language: str, target_language: str) -> list[str]:
    auth_key = deepl_auth_key()
    if not auth_key:
        raise RuntimeError("DEEPL_AUTH_KEY is not configured")
    response = httpx.post(
        deepl_api_url(),
        headers={"Authorization": f"DeepL-Auth-Key {auth_key}"},
        json={
            "text": texts,
            "source_lang": source_language.upper(),
            "target_lang": target_language.upper(),
            "preserve_formatting": True,
        },
        timeout=30,
    )
    response.raise_for_status()
    return [str(item["text"]) for item in response.json()["translations"]]
