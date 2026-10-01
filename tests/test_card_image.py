from __future__ import annotations

import json

import httpx

from card_translator.providers import card_image

SCHEMA = {
    "type": "object",
    "properties": {
        "source": {"type": "object", "properties": {"rules": {"type": "string"}}},
        "translation": {"type": "object", "properties": {"rules": {"type": "string"}}},
    },
    "required": ["source", "translation"],
}
CARD = {"source": {"rules": "Forced – Test."}, "translation": {"rules": "Erzwungen – Test."}}


def test_openai_card_image_uses_selected_key_image_and_strict_schema(tmp_path, monkeypatch):
    image = tmp_path / "card.png"
    image.write_bytes(b"image")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    captured = {}

    def post(url, **kwargs):
        captured.update(url=url, **kwargs)
        return httpx.Response(
            200, request=httpx.Request("POST", url),
            json={"output": [{"content": [{"type": "output_text", "text": json.dumps(CARD)}]}],
                  "usage": {"input_tokens": 1}},
        )

    monkeypatch.setattr(card_image.httpx, "post", post)
    result = card_image.analyze_card_image(
        image, prompt="Read card", schema=SCHEMA, provider="openai",
    )
    assert captured["url"].endswith("/responses")
    assert captured["headers"]["Authorization"] == "Bearer openai-secret"
    assert captured["json"]["store"] is False
    content = captured["json"]["input"][0]["content"]
    assert content[0]["image_url"].startswith("data:image/png;base64,")
    assert content[1] == {"type": "input_text", "text": "Read card"}
    strict = captured["json"]["text"]["format"]["schema"]
    assert strict["additionalProperties"] is False
    assert strict["properties"]["source"]["required"] == ["rules"]
    assert strict["properties"]["source"]["additionalProperties"] is False
    assert SCHEMA["properties"]["source"].get("required") is None
    assert result["source"] == CARD["source"]
    assert result["_card_image_trace"]["provider"] == "openai"


def test_anthropic_card_image_uses_selected_key_image_and_json_schema(tmp_path, monkeypatch):
    image = tmp_path / "card.jpg"
    image.write_bytes(b"image")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret")
    captured = {}

    def post(url, **kwargs):
        captured.update(url=url, **kwargs)
        return httpx.Response(
            200, request=httpx.Request("POST", url),
            json={"content": [{"type": "text", "text": json.dumps(CARD)}],
                  "stop_reason": "end_turn", "usage": {"input_tokens": 1}},
        )

    monkeypatch.setattr(card_image.httpx, "post", post)
    result = card_image.analyze_card_image(
        image, prompt="Read card", schema=SCHEMA, provider="anthropic",
    )
    assert captured["url"].endswith("/messages")
    assert captured["headers"]["x-api-key"] == "anthropic-secret"
    assert captured["headers"]["anthropic-version"] == "2023-06-01"
    content = captured["json"]["messages"][0]["content"]
    assert content[0]["source"]["media_type"] == "image/jpeg"
    assert content[0]["source"]["type"] == "base64"
    assert content[1] == {"type": "text", "text": "Read card"}
    assert captured["json"]["output_config"]["format"]["type"] == "json_schema"
    assert result["translation"] == CARD["translation"]
    assert result["_card_image_trace"]["provider"] == "anthropic"


def test_gemini_card_image_keeps_existing_provider_path(tmp_path, monkeypatch):
    image = tmp_path / "card.png"
    image.write_bytes(b"image")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-secret")
    captured = {}

    def analyze(path, *, prompt, schema):
        captured.update(path=path, prompt=prompt, schema=schema)
        return {**CARD, "_gemini_trace": {"raw_response": json.dumps(CARD)}}

    monkeypatch.setattr(card_image.gemini, "analyze_card_image", analyze)
    result = card_image.analyze_card_image(
        image, prompt="Read card", schema=SCHEMA, provider="gemini",
    )
    assert captured == {"path": image, "prompt": "Read card", "schema": SCHEMA}
    assert result["_card_image_trace"]["provider"] == "gemini"
