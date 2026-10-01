from card_translator.providers import google_translate


def test_google_translation_uses_server_side_api_key(monkeypatch):
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": {"translations": [{"translatedText": "Katzen &amp; Hunde"}]}}

    def post(url, **kwargs):
        captured.update({"url": url, **kwargs})
        return Response()

    monkeypatch.setenv("GOOGLE_CLOUD_TRANSLATION_API_KEY", "secret")
    monkeypatch.setattr(google_translate.httpx, "post", post)

    result = google_translate.translate_text(
        "Cats & dogs", source_language="en", target_language="de"
    )

    assert result == "Katzen & Hunde"
    assert captured["headers"] == {"X-goog-api-key": "secret"}
    assert "secret" not in captured["url"]
