import json

import httpx

from card_translator.library import _arkham_image_markup_guidance
from card_translator.providers import gemini


def test_gemini_image_request_uses_inline_data_and_structured_schema(tmp_path, monkeypatch):
    image = tmp_path / "card.png"
    image.write_bytes(b"image")
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "candidates": [
                    {"content": {"parts": [{"text": json.dumps({"source": {"title": "Cat"}})}]}}
                ]
            }

    def post(url, **kwargs):
        captured.update({"url": url, **kwargs})
        return Response()

    monkeypatch.setenv("GEMINI_API_KEY", "secret")
    monkeypatch.setenv("UNSPEAKABLE_TRANSLATIONS_GEMINI_MODEL", "test-flash")
    monkeypatch.setattr(gemini.httpx, "post", post)
    schema = {"type": "object", "properties": {"source": {"type": "object"}}}

    result = gemini.analyze_card_image(image, prompt="Read card", schema=schema)

    assert result["source"] == {"title": "Cat"}
    assert result["_gemini_trace"] == {
        "raw_response": '{"source": {"title": "Cat"}}',
        "usage": {},
        "model": "test-flash",
    }
    assert captured["url"].endswith("/models/test-flash:generateContent")
    assert captured["headers"] == {"x-goog-api-key": "secret"}
    parts = captured["json"]["contents"][0]["parts"]
    assert parts[0] == {"text": "Read card"}
    assert parts[1]["inlineData"]["mimeType"] == "image/png"
    assert captured["json"]["generationConfig"]["responseSchema"] == schema


def test_gemini_retries_temporary_service_error(monkeypatch):
    request = httpx.Request("POST", "https://example.test/generateContent")
    responses = [
        httpx.Response(503, request=request),
        httpx.Response(
            200,
            request=request,
            json={"candidates": [{"content": {"parts": [{"text": "translated"}]}}]},
        ),
    ]
    delays = []

    monkeypatch.setenv("GEMINI_API_KEY", "secret")
    monkeypatch.setattr(gemini.httpx, "post", lambda *_args, **_kwargs: responses.pop(0))
    monkeypatch.setattr(gemini.time, "sleep", delays.append)

    result = gemini.translate_prompt("Translate this")

    assert result == "translated"
    assert delays == [0.75]


def test_gemini_explains_persistent_service_error(monkeypatch):
    request = httpx.Request("POST", "https://example.test/generateContent")

    monkeypatch.setenv("GEMINI_API_KEY", "secret")
    monkeypatch.setattr(
        gemini.httpx,
        "post",
        lambda *_args, **_kwargs: httpx.Response(503, request=request),
    )
    monkeypatch.setattr(gemini.time, "sleep", lambda _delay: None)

    try:
        gemini.translate_prompt("Translate this")
    except RuntimeError as exc:
        assert "nach 3 Versuchen vorübergehend nicht erreichbar" in str(exc)
        assert "Google HTTP 503" in str(exc)
    else:
        raise AssertionError("Expected a RuntimeError")


def test_arkham_image_prompt_describes_shoggoth_markup():
    guidance = _arkham_image_markup_guidance()

    assert "<b>...</b>" in guidance
    assert "<bi>...</bi>" in guidance
    assert "<blockquote>...</blockquote>" in guidance
    assert "TWO closely spaced vertical black strokes" in guidance
    assert "If the two strokes are not visible" in guidance
    assert "Following the rabbit" not in guidance
    assert "'({resolution}R1)'" in guidance
    assert "{willpower}" in guidance
    assert "{action}" in guidance
    assert "{clue}" in guidance
    assert "Do not use Markdown asterisks" in guidance
    assert "Never spell out an icon" in guidance
    assert "standalone paragraph printed entirely in italics is normally flavor text" in guidance
