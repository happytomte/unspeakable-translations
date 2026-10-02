from __future__ import annotations

import json

from starlette.requests import Request

from card_translator.config import card_image_provider, ollama_enabled
from card_translator.library import initialize_scenario
from card_translator.web.app import create_app
from card_translator.web.i18n import normalize_ui_language


def test_ui_language_defaults_to_english_and_keeps_explicit_selection():
    assert normalize_ui_language(None) == "en"
    assert normalize_ui_language("unknown") == "en"
    assert normalize_ui_language("de") == "de"


def test_format_rules_endpoint_formats_card_json_without_gemini(tmp_path, monkeypatch):
    monkeypatch.setenv("CARD_TRANSLATOR_LIBRARY", str(tmp_path))
    app = create_app()
    endpoint = next(route.endpoint for route in app.routes if route.name == "format_rules")
    result = {
        "source": {"rules": "Forced – Arkham changes."},
        "translation": {"rules": "Erzwungen – Arkham verändert sich."},
        "metadata": {"card_type": "act"},
    }
    response = endpoint(
        game="ah", project_language="de", slug="scenario", result_json=json.dumps(result),
    )
    formatted = response["result"]
    assert formatted["source"]["rules"] == "<b>Forced</b> – <bi>Arkham</bi> changes."
    assert formatted["translation"]["rules"] == (
        "<b>Erzwungen</b> – <bi>Arkham</bi> verändert sich."
    )
    assert formatted["metadata"] == result["metadata"]


def test_settings_save_persists_ollama_toggle_and_card_image_provider(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("GEMINI_API_KEY=kept\n", encoding="utf-8")
    monkeypatch.setenv("CARD_TRANSLATOR_ROOT", str(tmp_path))
    monkeypatch.setenv("CARD_TRANSLATOR_LIBRARY", str(tmp_path))
    monkeypatch.setenv("CARD_TRANSLATOR_ENABLE_OLLAMA", "true")
    monkeypatch.setenv("CARD_TRANSLATOR_CARD_IMAGE_PROVIDER", "gemini")
    app = create_app()
    endpoint = next(
        route.endpoint for route in app.routes if route.name == "save_translation_providers"
    )
    response = endpoint(ollama_enabled="false", image_provider="anthropic")
    assert response.status_code == 303
    saved = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "GEMINI_API_KEY=kept" in saved
    assert "UNSPEAKABLE_TRANSLATIONS_ENABLE_OLLAMA=false" in saved
    assert "UNSPEAKABLE_TRANSLATIONS_CARD_IMAGE_PROVIDER=anthropic" in saved
    assert ollama_enabled() is False
    assert card_image_provider() == "anthropic"
    settings_endpoint = next(route.endpoint for route in app.routes if route.name == "settings")
    request = Request({
        "type": "http", "method": "GET", "path": "/settings", "root_path": "",
        "scheme": "http", "server": ("test", 80),
        "headers": [(b"cookie", b"card_translator_ui_language=de")],
        "query_string": b"", "router": app.router,
    })
    body = settings_endpoint(request).body.decode()
    assert 'name="image_provider" value="anthropic" checked' in body
    assert 'name="ollama_enabled" value="true" checked' not in body

    response = endpoint(ollama_enabled="false", image_provider="none")
    assert response.status_code == 303
    assert card_image_provider() == "none"
    body = settings_endpoint(request).body.decode()
    assert 'name="image_provider" value="none" checked' in body


def test_card_image_preview_uses_provider_selected_in_settings(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(
        "CARD_TRANSLATOR_CARD_IMAGE_PROVIDER=openai\n", encoding="utf-8",
    )
    monkeypatch.setenv("CARD_TRANSLATOR_ROOT", str(tmp_path))
    monkeypatch.setenv("CARD_TRANSLATOR_LIBRARY", str(tmp_path))
    captured = {}

    def preview(root, **kwargs):
        captured.update(root=root, **kwargs)
        return {"model": "gpt-5", "source": {}, "translation": {}, "metadata": {}}

    monkeypatch.setattr("card_translator.web.app.preview_card_image", preview)
    app = create_app()
    endpoint = next(route.endpoint for route in app.routes if route.name == "card_image_preview")
    response = endpoint(
        game="ah", project_language="de", slug="scenario", card_key="001", side="front",
    )
    assert response["ok"] is True
    assert captured["provider"] == "openai"
    assert captured["root"] == tmp_path


def test_card_image_batch_apply_uses_deterministic_importer(tmp_path, monkeypatch):
    cards = tmp_path / "ah" / "de" / "scenario" / "cards"
    cards.mkdir(parents=True)
    (cards / "001-front.png").write_bytes(b"image")
    initialize_scenario(
        tmp_path,
        game="ah",
        slug="scenario",
        title="Scenario",
        translated_title="",
        author="",
        version="",
        source_url="",
        target_language="de",
        project_language="de",
        card_source="cards",
    )
    monkeypatch.setenv("CARD_TRANSLATOR_LIBRARY", str(tmp_path))
    captured = {}

    def apply(root, batch_directory):
        captured.update(root=root, batch_directory=batch_directory)
        return {
            "applied": 1,
            "results": ["001-front.result.json"],
            "game": "ah",
            "scenario": "scenario",
            "language": "de",
        }

    monkeypatch.setattr("card_translator.web.app.apply_card_image_batch_results", apply)
    app = create_app()
    endpoint = next(route.endpoint for route in app.routes if route.name == "card_image_batch_apply")

    result = endpoint(game="ah", project_language="de", slug="scenario")

    assert result["ok"] is True
    assert result["applied"] == 1
    assert captured == {
        "root": tmp_path,
        "batch_directory": tmp_path / "ah" / "de" / "scenario" / "llm-batch" / "de",
    }


def test_settings_page_renders_empty_json_source_form(tmp_path, monkeypatch):
    monkeypatch.setenv("CARD_TRANSLATOR_LIBRARY", str(tmp_path))
    app = create_app()
    endpoint = next(route.endpoint for route in app.routes if route.name == "settings")
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/settings",
            "root_path": "",
            "scheme": "http",
            "server": ("test", 80),
            "headers": [(b"cookie", b"card_translator_ui_language=de")],
            "query_string": b"",
            "router": app.router,
        }
    )
    response = endpoint(request)
    body = response.body.decode()
    assert response.status_code == 200
    assert "Arkham Horror: The Card Game" in body
    assert "The Lord of the Rings" not in body
    assert "Spielsystem-Konfiguration" in body
    assert 'name="image_provider" value="gemini"' in body
    assert 'name="image_provider" value="openai"' in body
    assert 'name="image_provider" value="anthropic"' in body
    assert 'name="image_provider" value="none"' in body
    assert 'onchange="this.requestSubmit()"' in body
    assert "Keys bleiben lokal" in body
    assert "Repository-URL" not in body
    assert "Felder aus Dateinamen" not in body
    assert "ID-Namensraum" not in body
    assert "Ollama / TranslateGemma aktivieren" in body
    assert "Diese Anweisung fließt in die LLM-Prompts" in body
    assert "Einstellungen speichern" not in body
    assert ">Daten laden</button>" in body
    assert "ArkhamDB-Daten und Übersetzungsreferenzen" in body

    data_directory = tmp_path / "ah" / "data"
    data_directory.mkdir(parents=True)
    (data_directory / "arkham.en.json").write_text("[]\n", encoding="utf-8")
    updated_body = endpoint(request).body.decode()
    assert ">Daten aktualisieren</button>" in updated_body

    error_response = endpoint(request, error="Duplicate field ID: title")
    error_body = error_response.body.decode()
    assert "Duplicate field ID: title" in error_body
    assert 'role="alert"' in error_body


def test_dashboard_renders_remote_project_language_downloads(tmp_path, monkeypatch):
    monkeypatch.setenv("CARD_TRANSLATOR_LIBRARY", str(tmp_path))
    monkeypatch.setattr(
        "card_translator.web.app.refresh_repository",
        lambda *args, **kwargs: {
            "configured": True,
            "available": True,
            "url": "https://github.com/example/cards.git",
            "branch": "main",
            "catalog_url": "https://example.github.io/cards/data/projects.json",
            "revision": "0123456789abcdef",
            "projects": [{
                "id": "ah/demo",
                "game": "ah",
                "slug": "demo",
                "title": "Demo Project",
                "base": {"extraction_percent": 75},
                "translations": {"de": {"translation_percent": 40}},
            }, {
                "id": "lotr/hidden",
                "game": "lotr",
                "slug": "hidden",
                "title": "Hidden LotR Project",
                "base": {"extraction_percent": 100},
                "translations": {"de": {"translation_percent": 100}},
            }],
        },
    )
    app = create_app()
    endpoint = next(route.endpoint for route in app.routes if route.name == "dashboard")
    request = Request({
        "type": "http", "method": "GET", "path": "/", "root_path": "",
        "scheme": "http", "server": ("test", 80),
        "headers": [(b"cookie", b"card_translator_ui_language=de")],
        "query_string": b"", "router": app.router,
    })

    body = endpoint(request).body.decode()

    assert "Demo Project" in body
    assert "Hidden LotR Project" not in body
    assert "lokale Übersetzungswerkbank" not in body
    assert "Projekte auf diesem Rechner" in body
    assert "Gespiegelte Projekte" not in body
    assert "Verfügbare Projekte" in body
    assert 'action="/remote-projects/download"' in body
    assert 'name="languages" value="de"' in body
    assert "75%" in body


def test_settings_warns_once_when_arkham_target_reference_is_missing(tmp_path, monkeypatch):
    data_directory = tmp_path / "data"
    data_directory.mkdir()
    (data_directory / "arkham.en.json").write_text(json.dumps([]), encoding="utf-8")
    monkeypatch.setenv("CARD_TRANSLATOR_ROOT", str(tmp_path))
    monkeypatch.setenv("CARD_TRANSLATOR_LIBRARY", str(tmp_path / "library"))
    app = create_app()
    endpoint = next(route.endpoint for route in app.routes if route.name == "settings")
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/settings",
            "root_path": "",
            "scheme": "http",
            "server": ("test", 80),
            "headers": [(b"cookie", b"card_translator_ui_language=de")],
            "query_string": b"game=ah&language=fr",
            "router": app.router,
        }
    )

    body = endpoint(request, language="fr").body.decode()

    assert "Keine offizielle ArkhamDB-Referenzdatei für: FR" in body
    assert body.count("Keine offizielle ArkhamDB-Referenzdatei") == 1


def test_arkham_scenario_page_renders_card_type_schema(tmp_path, monkeypatch):
    cards = tmp_path / "ah" / "de" / "scenario" / "cards"
    cards.mkdir(parents=True)
    (cards / "001-front.png").write_bytes(b"image")
    initialize_scenario(
        tmp_path,
        game="ah",
        slug="scenario",
        title="Scenario",
        translated_title="",
        author="",
        version="",
        source_url="",
        target_language="de",
        project_language="de",
        card_source="cards",
    )
    monkeypatch.setenv("CARD_TRANSLATOR_LIBRARY", str(tmp_path))
    monkeypatch.setenv("CARD_TRANSLATOR_ROOT", str(tmp_path))
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-test-key")
    (tmp_path / ".env").write_text(
        "CARD_TRANSLATOR_CARD_IMAGE_PROVIDER=openai\n", encoding="utf-8",
    )
    monkeypatch.setattr(
        "card_translator.web.app.ollama_status",
        lambda: {"installed": False, "available": False},
    )
    app = create_app()
    endpoint = next(route.endpoint for route in app.routes if route.name == "scenario_detail")
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/ah/de/scenario",
            "root_path": "",
            "scheme": "http",
            "server": ("test", 80),
            "headers": [(b"cookie", b"card_translator_ui_language=de")],
            "query_string": b"",
            "router": app.router,
        }
    )

    response = endpoint(request, game="ah", project_language="de", slug="scenario")

    assert response.status_code == 200
    body = response.body.decode()
    assert '<select name="card_type">' in body
    assert "Metadaten bearbeiten" not in body
    assert body.index('<select name="card_type">') < body.index("EN · Name")
    assert 'const symbolTokens = [' in body
    assert '["willpower", "w"]' in body
    assert 'className = "symbol-autocomplete"' in body
    assert 'chaos_${name}.png' in body
    assert "Chaos-Symbole werden vom OCR nicht erkannt" in body
    assert "let repaired" in body
    assert "Karten 1–1 von 1" in body
    assert '<option value="100" selected>100</option>' in body
    assert 'class="button-secondary llm-json-format-rules"' in body
    assert 'class="multi-select location-symbol-select" data-max-items="1" data-single="true"' in body
    assert 'class="multi-select location-symbol-select" data-max-items="6" data-single="false"' in body
    for symbol in ("quote", "slash", "spade"):
        assert f'/static/location_symbols/connection_{symbol}.svg' in body
    assert 'hiddenInput.dispatchEvent(new Event("input", {bubbles: true}))' in body
    assert 'class="button-secondary card-image-button"' in body
    assert "Karte mit KI erfassen · OpenAI" in body
    assert "Für LLM mit Bildern vorbereiten" in body
    assert "Für LLM nur mit extrahierten Daten vorbereiten" not in body
    assert "Verarbeitungs-Prompt kopieren" in body
    assert (
        "Verarbeite jeden Job des vorbereiteten Karten-Batches unter "
        "library/ah/de/scenario/llm-batch/de" in body
    )
    assert "Öffne für ausnahmslos jeden Job" in body
    assert "ein erkannter Titel allein ist kein abgeschlossenes Ergebnis" in body
    assert "Batch-Ergebnisse einspielen" in body
    assert 'class="batch-card-error" role="alert" hidden' in body
    assert 'row.classList.add("batch-import-error")' in body
    assert 'input.value = failure.result_json || ""' in body
    assert "betroffene Karten wurden markiert" in body
    assert "5, X, 5&lt;per&gt;, X&lt;per&gt;" in body
    assert "&lt;unique&gt;Name" in body
    assert "Noch kein API-Protokoll vorhanden." in body
    assert "/logs/translation.jsonl" not in body
    api_status = body.index('class="translation-model-row project-api-status')
    shoggoth_import = body.index('class="shoggoth-import-panel')
    translation_rules = body.index('class="translation-rules-panel')
    assert api_status < shoggoth_import < translation_rules
    assert (
        '<details class="translation-rules-panel inline-settings-panel">\n'
        '        <summary><strong>Begriffe und Formatierung</strong></summary>'
    ) in body
    options_start = body.index('class="project-menu-content project-options-content"')
    cards_start = body.index('class="cards-section"')
    collapse_button = body.index('class="button-secondary collapse-duplicates-button"')
    assert options_start < collapse_button < cards_start
    assert "Vorder- und Rückseite bilden gemeinsam eine logische Karte." not in body
    assert "führe keinen Importer aus" in body
    assert "translations/de/" in body
    assert str(tmp_path / "ah" / "de" / "scenario") in body

    log_path = tmp_path / "ah" / "de" / "scenario" / "logs" / "translation.jsonl"
    log_path.parent.mkdir()
    log_path.write_text('{"status":"translated"}\n', encoding="utf-8")
    logged_body = endpoint(
        request, game="ah", project_language="de", slug="scenario"
    ).body.decode()
    assert "Noch kein API-Protokoll vorhanden." not in logged_body
    assert (
        '/library/ah/de/scenario/logs/translation.jsonl'
        in logged_body
    )
    assert "const cardImageBatchUrl = `${projectBaseUrl}/card-image-batch`" in body
    assert "const cardImageBatchApplyUrl = `${projectBaseUrl}/card-image-batch-apply`" in body
    assert 'const cardImagePreviewUrl = `${projectBaseUrl}/card-image-preview`' in body
    assert 'const sourceRules = row.querySelector' in body
    assert 'control.dispatchEvent(new Event("input", {bubbles: true}))' in body
    assert 'className = "markup-selection-menu"' in body
    assert '["blockquote", "Blockquote"]' in body
    assert 'control.setRangeText(`<${tag}>${selected}</${tag}>`' in body
    assert 'const textHistories = new WeakMap()' in body
    assert 'stepTextHistory(control, key === "y" || event.shiftKey ? 1 : -1)' in body
    assert '.source-side-form textarea[name="flavor"]' in body
    assert '.translation-side-form textarea[name="rules"]' in body
    assert 'gemini-card-format-rules' not in body
    assert 'gemini-card-bold-review' not in body
    assert "Kartendaten speichern" not in body
    assert 'form.requestSubmit()' in body

    (tmp_path / ".env").write_text(
        "UNSPEAKABLE_TRANSLATIONS_CARD_IMAGE_PROVIDER=none\n", encoding="utf-8"
    )
    body_without_card_ai = endpoint(
        request, game="ah", project_language="de", slug="scenario"
    ).body.decode()
    assert 'class="button-secondary card-image-button"' not in body_without_card_ai
    assert "Karte mit KI erfassen" not in body_without_card_ai
