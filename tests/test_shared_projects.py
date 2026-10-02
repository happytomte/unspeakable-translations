from __future__ import annotations

import json

import pytest
from starlette.requests import Request

from card_translator.library import (
    apply_card_image_batch_results,
    apply_card_source_proposal,
    card_source_proposal,
    copy_scenario_to_language,
    get_scenario,
    initialize_scenario,
    prepare_card_image_batch,
    stage_card_source_proposal,
    update_card_source_text,
    update_card_translation,
    update_translation_completion,
)
from card_translator.migration import migrate_project, migration_candidates
from card_translator.project_publication import create_shared_project
from card_translator.shared_projects import (
    read_base_document,
    read_translation_document,
    write_base_document,
    write_translation_document,
)
from card_translator.shoggoth_export import export_shoggoth
from card_translator.status_catalog import build_status_catalog
from card_translator.web.app import create_app


def _legacy_scenario(root):
    cards = root / "ah/de/scenario/cards"
    cards.mkdir(parents=True)
    (cards / "001_Card-1.jpg").write_bytes(b"source image")
    initialize_scenario(
        root, game="ah", slug="scenario", title="Scenario", translated_title="Szenario",
        author="", version="", source_url="", target_language="de",
        project_language="de", card_source="cards",
    )
    update_card_source_text(
        root, game="ah", slug="scenario", project_language="de", card_key="001_Card",
        side="front", title="Card", rules="Forced – Test.", flavor="", traits="",
    )
    update_card_translation(
        root, game="ah", slug="scenario", language="de", card_key="001_Card",
        side="front", title="Karte", rules="Erzwungen – Test.", flavor="", traits="",
    )


def test_base_document_stores_each_card_and_its_review_metadata_separately(tmp_path):
    path = tmp_path / "base/extraction.json"
    document = {
        "format": "card-translator-base",
        "format_version": 1,
        "game": "ah",
        "scenario": "scenario",
        "source_language": "en",
        "cards": [
            {"id": "001-First", "key": "001-First", "images": {"front": "source/cards/first.png"}, "text": {"en": {"title": "First"}}},
            {"id": "002-Second", "key": "002-Second", "images": {"front": "source/cards/second.png"}, "text": {"en": {"title": "Second"}}},
        ],
        "_meta": {
            "fields": {
                "001-First.text.en.title": {"source": "manual"},
                "002-Second.text.en.title": {"source": "ocr"},
            },
            "images": {"first.png": {"display_rotation": 90}},
            "cards": {"001-First": {"source_status": {"front": "reviewed"}}},
        },
    }

    write_base_document(path, document)

    manifest = json.loads(path.read_text())
    assert manifest["format_version"] == 2
    assert "cards" not in manifest
    assert manifest["card_files"] == [
        {"key": "001-First", "path": "cards/001-First.json"},
        {"key": "002-Second", "path": "cards/002-Second.json"},
    ]
    first_path = path.parent / "cards/001-First.json"
    second_path = path.parent / "cards/002-Second.json"
    first = json.loads(first_path.read_text())
    assert first["card"]["text"]["en"]["title"] == "First"
    assert first["_meta"]["fields"] == {
        "001-First.text.en.title": {"source": "manual"}
    }
    assert first["_meta"]["images"] == {
        "first.png": {"display_rotation": 90}
    }
    assert first["_meta"]["card"] == {
        "source_status": {"front": "reviewed"}
    }
    manifest_before = path.read_bytes()
    second_before = second_path.read_bytes()

    document["cards"][0]["text"]["en"]["title"] = "Changed"
    write_base_document(path, document)

    assert path.read_bytes() == manifest_before
    assert second_path.read_bytes() == second_before
    restored = read_base_document(path)
    assert restored["cards"][0]["text"]["en"]["title"] == "Changed"
    assert restored["_meta"] == document["_meta"]

    first_path.unlink()
    assert read_base_document(path) is None


def test_translation_document_is_sparse_and_keeps_card_reviews_separate(tmp_path):
    base_path = tmp_path / "base/extraction.json"
    write_base_document(base_path, {
        "game": "ah",
        "scenario": "scenario",
        "source_language": "en",
        "cards": [
            {"id": "001-First", "key": "001-First"},
            {"id": "002-Second", "key": "002-Second"},
            {"id": "003-Untranslated", "key": "003-Untranslated"},
        ],
        "_meta": {},
    })
    path = tmp_path / "translations/de/translation.json"
    document = {
        "game": "ah",
        "scenario": "scenario",
        "source_language": "en",
        "target_language": "de",
        "cards": [
            {
                "id": "001-First",
                "key": "001-First",
                "text": {"de": {"sides": {"front": {"title": "Erste"}}}},
            },
            {
                "id": "002-Second",
                "key": "002-Second",
                "text": {"de": {"sides": {"front": {"title": "Zweite"}}}},
            },
        ],
        "_meta": {
            "fields": {
                "002-Second.text.de.sides.front.title": {
                    "source": "manual",
                    "review_required": False,
                }
            }
        },
    }

    write_translation_document(path, document, base_path=base_path)

    manifest = json.loads(path.read_text())
    assert manifest["format_version"] == 3
    assert "cards" not in manifest
    assert manifest["_meta"] == {}
    first_path = path.parent / "cards/001-First.json"
    card_path = path.parent / "cards/002-Second.json"
    payload = json.loads(card_path.read_text())
    assert payload["card"]["text"]["de"]["sides"]["front"]["title"] == "Zweite"
    assert payload["_meta"] == document["_meta"]
    restored = read_translation_document(path)
    assert restored["cards"] == document["cards"]
    assert restored["_meta"] == document["_meta"]
    assert not (path.parent / "cards/003-Untranslated.json").exists()

    manifest_before = path.read_bytes()
    second_before = card_path.read_bytes()
    document["cards"][0]["text"]["de"]["sides"]["front"]["title"] = "Geändert"
    write_translation_document(path, document, base_path=base_path)

    assert path.read_bytes() == manifest_before
    assert card_path.read_bytes() == second_before
    assert json.loads(first_path.read_text())["card"]["text"]["de"]["sides"]["front"][
        "title"
    ] == "Geändert"


def test_migration_keeps_legacy_data_and_splits_shared_extraction(tmp_path):
    _legacy_scenario(tmp_path)
    original = tmp_path / "ah/de/scenario/translation_de.json"
    original_bytes = original.read_bytes()

    assert migration_candidates(tmp_path) == [
        {"game": "ah", "slug": "scenario", "languages": ["de"]}
    ]
    migrated = migrate_project(tmp_path, game="ah", slug="scenario")
    project = migrated["path"]
    manifest = json.loads((project / "base/extraction.json").read_text())
    assert "cards" not in manifest
    assert manifest["card_files"] == [
        {"key": "001_Card", "path": "cards/001_Card.json"}
    ]
    base = read_base_document(project / "base/extraction.json")
    german = read_translation_document(project / "translations/de/translation.json")

    assert original.read_bytes() == original_bytes
    assert (project / "source/cards/001_Card-1.jpg").read_bytes() == b"source image"
    assert base["cards"][0]["text"]["en"]["sides"]["front"]["title"] == "Card"
    assert "de" not in base["cards"][0]["text"]
    assert german["cards"][0]["text"]["de"]["sides"]["front"]["title"] == "Karte"
    assert "game_data" not in german["cards"][0]

    scenario = get_scenario(tmp_path, "ah", "scenario", language="de")
    assert scenario["shared_layout"] is True
    assert scenario["grouped_cards"][0]["source_sides"]["front"]["title"] == "Card"
    assert scenario["grouped_cards"][0]["target_sides"]["front"]["title"] == "Karte"


def test_migration_combines_existing_languages_without_copying_source_images(tmp_path):
    _legacy_scenario(tmp_path)
    copy_scenario_to_language(
        tmp_path, game="ah", slug="scenario", project_language="de", target_language="fr",
    )

    migrated = migrate_project(tmp_path, game="ah", slug="scenario")

    assert migrated["languages"] == ["de", "fr"]
    assert len(list((migrated["path"] / "source/cards").iterdir())) == 1
    assert get_scenario(tmp_path, "ah", "scenario", language="fr")["shared_layout"] is True


def test_shared_source_edits_do_not_duplicate_source_in_language_files(tmp_path):
    _legacy_scenario(tmp_path)
    migrate_project(tmp_path, game="ah", slug="scenario")
    copy_scenario_to_language(
        tmp_path, game="ah", slug="scenario", project_language="de", target_language="fr",
    )
    project = tmp_path / "projects/ah/scenario"
    update_card_source_text(
        tmp_path, game="ah", slug="scenario", project_language="de", card_key="001_Card",
        side="front", title="New Card", rules="Forced – Test.", flavor="", traits="",
    )
    base = read_base_document(project / "base/extraction.json")
    german = read_translation_document(project / "translations/de/translation.json")
    french = read_translation_document(project / "translations/fr/translation.json")

    assert base["cards"][0]["text"]["en"]["sides"]["front"]["title"] == "New Card"
    assert "en" not in german["cards"][0]["text"]
    assert french["cards"] == []
    assert get_scenario(tmp_path, "ah", "scenario", language="fr")["grouped_cards"][0]["source_sides"]["front"]["title"] == "New Card"
    assert german["_meta"]["fields"]["001_Card.text.de.sides.front.title"]["review_required"] is True


def test_translation_source_edits_are_staged_and_require_explicit_review(tmp_path):
    _legacy_scenario(tmp_path)
    migrate_project(tmp_path, game="ah", slug="scenario")
    project = tmp_path / "projects/ah/scenario"
    base_path = project / "base/extraction.json"
    before = base_path.read_bytes()

    stage_card_source_proposal(
        tmp_path,
        game="ah",
        slug="scenario",
        project_language="de",
        card_key="001_Card",
        side="front",
        text_values={"title": "Corrected Card"},
        source="manual",
    )

    assert base_path.read_bytes() == before
    proposal_path = project / "translations/de/source-proposals.json"
    assert proposal_path.is_file()
    proposal = card_source_proposal(
        tmp_path,
        game="ah",
        slug="scenario",
        project_language="de",
        card_key="001_Card",
    )
    title = next(field for side in proposal["sides"] for field in side["fields"])
    assert title["current"] == "Card"
    assert title["proposed"] == "Corrected Card"
    assert title["stale"] is False

    result = apply_card_source_proposal(
        tmp_path,
        game="ah",
        slug="scenario",
        project_language="de",
        card_key="001_Card",
        updates=[{
            "side": "front",
            "kind": "text",
            "field": "title",
            "value": "Corrected Card",
            "reviewed_hash": title["reviewed_hash"],
        }],
    )

    assert result["applied"] == 1
    assert not proposal_path.exists()
    base = read_base_document(base_path)
    assert base["cards"][0]["text"]["en"]["sides"]["front"]["title"] == "Corrected Card"


def test_source_proposal_rejects_a_concurrent_canonical_change(tmp_path):
    _legacy_scenario(tmp_path)
    migrate_project(tmp_path, game="ah", slug="scenario")
    stage_card_source_proposal(
        tmp_path,
        game="ah",
        slug="scenario",
        project_language="de",
        card_key="001_Card",
        side="front",
        text_values={"title": "Translator correction"},
    )
    proposal = card_source_proposal(
        tmp_path, game="ah", slug="scenario", project_language="de", card_key="001_Card"
    )
    title = proposal["sides"][0]["fields"][0]
    update_card_source_text(
        tmp_path,
        game="ah",
        slug="scenario",
        project_language="source",
        card_key="001_Card",
        side="front",
        title="Canonical correction",
        rules="Forced – Test.",
        flavor="",
        traits="",
    )

    with pytest.raises(ValueError, match="changed after the diff was opened"):
        apply_card_source_proposal(
            tmp_path,
            game="ah",
            slug="scenario",
            project_language="de",
            card_key="001_Card",
            updates=[{
                "side": "front",
                "kind": "text",
                "field": "title",
                "value": "Translator correction",
                "reviewed_hash": title["reviewed_hash"],
            }],
        )
    refreshed = card_source_proposal(
        tmp_path, game="ah", slug="scenario", project_language="de", card_key="001_Card"
    )
    assert refreshed["stale"] is True


def test_batch_uses_captured_source_text_without_reopening_card_images(
    tmp_path, monkeypatch
):
    _legacy_scenario(tmp_path)
    (tmp_path / "ah/de/scenario/cards/002_Missing-1.jpg").write_bytes(
        b"unextracted source image"
    )
    migrate_project(tmp_path, game="ah", slug="scenario")
    copy_scenario_to_language(
        tmp_path, game="ah", slug="scenario", project_language="de", target_language="fr"
    )

    prepared = prepare_card_image_batch(
        tmp_path,
        game="ah",
        slug="scenario",
        project_language="fr",
        extracted_data_only=True,
    )

    assert prepared["count"] == 1
    assert prepared["text_only"] == 1
    assert prepared["image_jobs"] == 0
    batch = tmp_path / "projects/ah/scenario/llm-batch/fr"
    assert not (batch / "002_Missing-1.job.json").exists()
    job = json.loads((batch / "001_Card-1.job.json").read_text())
    assert job["mode"] == "text_translation"
    assert "image" not in job
    assert set(job["output_schema"]["properties"]) == {"translation"}
    assert "source" not in job["output_schema"]["properties"]
    assert "metadata" not in job["output_schema"]["properties"]
    manifest = json.loads((batch / "manifest.json").read_text())
    assert "image" not in manifest["jobs"][0]
    translated_fields = job["output_schema"]["properties"]["translation"]["properties"]
    result = {
        "translation": {field: f"FR {field}" for field in translated_fields}
    }
    (batch / job["result"]).write_text(json.dumps(result))

    imported = apply_card_image_batch_results(tmp_path, batch)

    assert imported["applied"] == 1, imported
    assert imported["source_proposal_cards"] == []
    scenario = get_scenario(tmp_path, "ah", "scenario", language="fr")
    assert scenario["grouped_cards"][0]["source_sides"]["front"]["title"] == "Card"
    assert scenario["grouped_cards"][0]["target_sides"]["front"]["title"] == "FR title"

    prepared_with_images = prepare_card_image_batch(
        tmp_path,
        game="ah",
        slug="scenario",
        project_language="fr",
        force_image_analysis=True,
    )
    assert prepared_with_images["text_only"] == 0
    assert prepared_with_images["image_jobs"] == 2
    image_job = json.loads((batch / "001_Card-1.job.json").read_text())
    assert image_job["mode"] == "image_extraction"
    assert image_job["image"] == "source/cards/001_Card-1.jpg"
    assert set(image_job["output_schema"]["properties"]) == {
        "source",
        "translation",
        "metadata",
    }

    monkeypatch.setenv("CARD_TRANSLATOR_LIBRARY", str(tmp_path))
    app = create_app()
    endpoint = next(route.endpoint for route in app.routes if route.name == "scenario_detail")
    request = Request({
        "type": "http",
        "method": "GET",
        "path": "/ah/fr/scenario",
        "root_path": "",
        "scheme": "http",
        "server": ("test", 80),
        "headers": [],
        "query_string": b"",
        "router": app.router,
    })
    body = endpoint(
        request, game="ah", project_language="fr", slug="scenario"
    ).body.decode()
    assert "Prepare for LLM with images" in body
    assert "Prepare for LLM using only extracted data" in body
    assert 'data-force-image-analysis="true"' in body
    assert 'data-extracted-data-only="true"' in body


def test_source_workspace_batch_captures_only_original_data(tmp_path):
    created = create_shared_project(
        tmp_path,
        game="ah",
        slug="scenario",
        title="Scenario",
        author="",
        source_url="",
        source_language="en",
    )
    (created["cards_path"] / "001_Card-1.jpg").write_bytes(b"card")

    prepared = prepare_card_image_batch(
        tmp_path, game="ah", slug="scenario", project_language="source"
    )

    assert prepared["count"] == 1
    assert prepared["text_only"] == 0
    assert prepared["image_jobs"] == 1
    batch = created["path"] / "llm-batch/en"
    job = json.loads((batch / "001_Card-1.job.json").read_text())
    assert job["project_language"] == "source"
    assert set(job["output_schema"]["properties"]) == {"source", "metadata"}
    result = {
        section: {field: "" for field in definition["properties"]}
        for section, definition in job["output_schema"]["properties"].items()
    }
    result["source"]["title"] = "Captured Card"
    result["metadata"]["card_type"] = "treachery"
    (batch / job["result"]).write_text(json.dumps(result))

    imported = apply_card_image_batch_results(tmp_path, batch)

    assert imported["applied"] == 1, imported
    assert imported["source_proposal_cards"] == []
    scenario = get_scenario(tmp_path, "ah", "scenario", language="source")
    card = scenario["grouped_cards"][0]
    assert card["source_sides"]["front"]["title"] == "Captured Card"
    assert card["source_status_by_side"]["front"] == "captured"


def test_status_catalog_uses_logical_cards_and_separate_review_counts(tmp_path):
    _legacy_scenario(tmp_path)
    migrate_project(tmp_path, game="ah", slug="scenario")
    copy_scenario_to_language(
        tmp_path, game="ah", slug="scenario", project_language="de", target_language="fr",
    )

    catalog = build_status_catalog(tmp_path)
    project = catalog["projects"][0]

    assert project["layout"] == "shared"
    assert project["game_name"] == "Arkham Horror: The Card Game"
    assert project["author"] == ""
    assert project["source_url"] == ""
    assert project["source_language"] == "en"
    assert project["base"]["cards_total"] == 1
    assert project["base"]["source_text_present"] == 1
    assert project["base"]["source_reviewed"] == 1
    assert project["base"]["extraction_percent"] == 100
    assert project["translations"]["de"]["cards_translated"] == 1
    assert project["translations"]["de"]["cards_reviewed"] == 0
    assert project["translations"]["fr"]["cards_translated"] == 0
    assert "source" not in project["translations"]


def test_shared_shoggoth_export_is_written_below_selected_language(tmp_path):
    _legacy_scenario(tmp_path)
    migrate_project(tmp_path, game="ah", slug="scenario")

    output = export_shoggoth(tmp_path, game="ah", project_language="de", slug="scenario")

    assert output == tmp_path / "projects/ah/scenario/translations/de/builds/shoggoth.json"
    assert json.loads(output.read_text())["meta"]["language"] == "de"


def test_translation_can_be_marked_as_finally_reviewed(tmp_path):
    _legacy_scenario(tmp_path)
    migrate_project(tmp_path, game="ah", slug="scenario")

    update_translation_completion(
        tmp_path,
        game="ah",
        slug="scenario",
        project_language="de",
        final_reviewed=True,
    )

    project = json.loads(
        (tmp_path / "projects/ah/scenario/translations/de/project.json").read_text()
    )
    assert project["final_reviewed"] is True
    assert build_status_catalog(tmp_path)["projects"][0]["translations"]["de"][
        "release_asset"
    ] == "ah-scenario-de.zip"

    update_card_translation(
        tmp_path,
        game="ah",
        slug="scenario",
        language="de",
        card_key="001_Card",
        side="front",
        title="Überarbeitete Karte",
        rules="Erzwungen – Test.",
        flavor="",
        traits="",
    )
    project = json.loads(
        (tmp_path / "projects/ah/scenario/translations/de/project.json").read_text()
    )
    assert project["final_reviewed"] is False


def test_shared_project_is_editable_on_existing_scenario_route(tmp_path, monkeypatch):
    _legacy_scenario(tmp_path)
    migrate_project(tmp_path, game="ah", slug="scenario")
    stage_card_source_proposal(
        tmp_path,
        game="ah",
        slug="scenario",
        project_language="de",
        card_key="001_Card",
        side="front",
        text_values={"title": "Corrected Card"},
        metadata_values={"artist": "Draft Artist"},
    )
    monkeypatch.setenv("CARD_TRANSLATOR_ROOT", str(tmp_path))
    monkeypatch.setenv("CARD_TRANSLATOR_LIBRARY", str(tmp_path))
    monkeypatch.setattr(
        "card_translator.web.app.ollama_status",
        lambda: {"installed": False, "available": False},
    )

    app = create_app()
    endpoint = next(route.endpoint for route in app.routes if route.name == "scenario_detail")
    request = Request({
        "type": "http", "method": "GET", "path": "/ah/de/scenario",
        "root_path": "", "scheme": "http", "server": ("test", 80),
        "headers": [(b"cookie", b"unspeakable_translations_ui_language=de")],
        "query_string": b"", "router": app.router,
    })
    response = endpoint(request, game="ah", project_language="de", slug="scenario")

    assert response.status_code == 200
    body = response.body.decode()
    assert '/library/projects/ah/scenario/source/cards/001_Card-1.jpg' in body
    assert 'name="rules"' in body
    assert "source-edit-locked" in body
    assert "Originaldaten bearbeiten" in body
    assert "Originaldaten überschreiben" in body
    assert "Show diff" in body
    assert 'id="source-proposal-dialog"' in body
    assert "Originaldaten · gelten für alle Sprachen" in body
    assert "Übersetzung direkt unter Originalfeld anzeigen" in body
    assert "translation-field-layout-toggle" in body
    assert "ui-interleave-translation-fields" in body
    assert "let interleaveTranslationFields = true" in body
    assert "storedFieldLayout === null" in body
    assert 'class="text-field-layout"' in body
    assert "--translation-field-order: 0" in body
    assert "--translation-field-order: 1" in body
    assert "Als erledigt markieren" in body
    assert "unspeakable-translations.card-progress" in body
    assert "unspeakable-translations.card-notes" in body
    assert "is-manually-complete" in body
    assert 'class="manual-card-notes-input"' in body
    assert "Nur lokal in diesem Browser gespeichert" not in body
    assert "(nicht exportiert)" in body
    assert 'class="manual-card-type-suffix"' in body
    assert 'class="manual-card-notes-alert"' in body
    assert "source-proposal-pending" in body
    assert "Änderung vorgemerkt" in body
    assert 'value="Corrected Card"' in body
    assert 'value="Draft Artist"' in body
    assert "Vorder- und Rückseite bilden gemeinsam eine logische Karte." not in body
    assert "ArkhamDB-Referenzen sind für EN → DE nicht bereit" in body
    assert "LLM trotzdem starten?" in body
    assert "Der Batch übersetzt von en nach de." in body
