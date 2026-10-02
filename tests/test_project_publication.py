from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from card_translator.library import (
    get_scenario,
    save_separate_card_art,
    update_card_source_text,
    update_separate_card_art_setting,
)
from card_translator.project_publication import (
    campaign_summaries,
    changed_base_cards,
    changed_translation_cards,
    contribution_preview,
    create_campaign,
    create_campaign_scenario,
    create_project_translation,
    create_shared_project,
    publication_status,
    publish_extraction,
    publish_translation,
    save_project_image,
    shared_project_summaries,
    update_campaign_private_only,
    update_scenario_private_only,
)
from card_translator.shared_projects import (
    read_base_document,
    read_translation_document,
    write_base_document,
    write_translation_document,
)
from card_translator.shoggoth_export import build_shoggoth_document
from card_translator.web.app import create_app


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def create_demo(library):
    result = create_shared_project(
        library,
        game="ah",
        slug="demo-scenario",
        title="Demo Scenario",
        author="Example",
        source_url="https://example.test/demo",
        source_language="en",
    )
    create_project_translation(
        library, game="ah", slug="demo-scenario", target_language="de",
    )
    (result["cards_path"] / "001-Card-1.png").write_bytes(b"front")
    translation_path = result["path"] / "translations/de/translation.json"
    translation = read_translation_document(translation_path)
    translation["cards"] = [{
        "id": "001-Card",
        "key": "001-Card",
        "text": {"de": {"sides": {"front": {"title": "Karte"}}}},
    }]
    write_translation_document(
        translation_path,
        translation,
        base_path=result["path"] / "base/extraction.json",
    )
    base_path = result["path"] / "base/extraction.json"
    base = read_base_document(base_path)
    base["cards"] = [{
        "id": "001-Card",
        "key": "001-Card",
        "card_type": "story",
        "images": {"front": "source/cards/001-Card-1.png"},
        "text": {"en": {"sides": {"front": {"title": "Card"}}}},
    }]
    write_base_document(base_path, base)
    return result


def test_scenario_and_translation_are_created_independently(tmp_path):
    result = create_shared_project(
        tmp_path,
        game="ah",
        slug="new-scenario",
        title="New Scenario",
        author="Author",
        source_url="https://example.test",
        source_language="fr",
    )

    project = read_json(result["path"] / "project.json")
    assert project["format_version"] == 2
    assert project["source_language"] == "fr"
    assert project["card_source"] == "source/cards"
    assert project["private_only"] is False
    assert project["private_only_override"] == "inherit"
    assert result["cards_path"].is_dir()
    assert not (result["path"] / "translations").exists()
    assert shared_project_summaries(tmp_path)[0]["languages"] == []

    created = create_project_translation(
        tmp_path, game="ah", slug="new-scenario", target_language="de",
    )
    translation = read_translation_document(
        result["path"] / "translations/de/translation.json"
    )
    assert created["target_language"] == "de"
    assert translation["target_language"] == "de"
    assert shared_project_summaries(tmp_path)[0]["languages"] == ["de"]

    with pytest.raises(ValueError, match="different"):
        create_project_translation(
            tmp_path, game="ah", slug="new-scenario", target_language="fr",
        )


def test_project_images_are_shared_source_files_and_update_metadata(tmp_path):
    result = create_shared_project(
        tmp_path,
        game="ah",
        slug="new-scenario",
        title="New Scenario",
        author="Author",
        source_url="",
        source_language="en",
    )

    mood = save_project_image(
        tmp_path,
        game="ah",
        slug="new-scenario",
        kind="mood",
        filename="atmosphere.webp",
        content=b"mood-image",
    )
    cover = save_project_image(
        tmp_path,
        game="ah",
        slug="new-scenario",
        kind="cover",
        filename="cover.png",
        content=b"cover-image",
    )

    assert mood == result["path"] / "source/media/mood.webp"
    assert cover == result["path"] / "source/media/cover.png"
    project = read_json(result["path"] / "project.json")
    assert project["mood_image"] == "source/media/mood.webp"
    assert project["cover_image"] == "source/media/cover.png"

    with pytest.raises(ValueError, match="JPG, PNG, or WebP"):
        save_project_image(
            tmp_path,
            game="ah",
            slug="new-scenario",
            kind="cover",
            filename="cover.svg",
            content=b"<svg/>",
        )


def test_campaign_owns_metadata_and_contains_multiple_scenarios(tmp_path):
    campaign = create_campaign(
        tmp_path,
        game="ah",
        slug="dark-matter",
        title="Dark Matter",
        author="Axolotl",
        source_url="https://example.test/dark-matter",
        source_language="en",
    )
    first = create_campaign_scenario(
        tmp_path,
        game="ah",
        campaign="dark-matter",
        slug="the-tatterdemalion",
        title="The Tatterdemalion",
    )
    second = create_campaign_scenario(
        tmp_path,
        game="ah",
        campaign="dark-matter",
        slug="lost-quantum",
        title="Lost Quantum",
    )

    assert campaign["path"] == tmp_path / "projects/ah/dark-matter"
    assert first["path"] == campaign["path"] / "scenarios/the-tatterdemalion"
    assert second["path"] == campaign["path"] / "scenarios/lost-quantum"
    assert read_json(campaign["path"] / "campaign.json")["author"] == "Axolotl"
    assert read_json(campaign["path"] / "campaign.json")["private_only"] is False
    assert "author" not in read_json(first["path"] / "project.json")
    assert campaign_summaries(tmp_path)[0]["scenario_count"] == 2

    create_project_translation(
        tmp_path,
        game="ah",
        campaign="dark-matter",
        slug="the-tatterdemalion",
        target_language="de",
    )
    scenario = get_scenario(
        tmp_path, "ah", "the-tatterdemalion", language="de", campaign="dark-matter"
    )
    assert scenario["campaign"] == "dark-matter"
    assert scenario["campaign_title"] == "Dark Matter"
    assert scenario["project"]["author"] == "Axolotl"
    assert scenario["path"] == first["path"]


def test_private_only_is_inherited_toggleable_and_blocks_publishing(tmp_path):
    library = tmp_path / "library"
    repository = tmp_path / "repository"
    campaign = create_campaign(
        library,
        game="ah",
        slug="private-campaign",
        title="Private Campaign",
        author="",
        source_url="",
        source_language="en",
        private_only=True,
    )
    created = create_campaign_scenario(
        library,
        game="ah",
        campaign="private-campaign",
        slug="private-scenario",
        title="Private Scenario",
    )
    create_project_translation(
        library,
        game="ah",
        campaign="private-campaign",
        slug="private-scenario",
        target_language="de",
    )

    scenario = get_scenario(
        library, "ah", "private-scenario", language="de", campaign="private-campaign"
    )
    assert scenario["private_only"] is True
    assert scenario["campaign_private_only"] is True
    assert scenario["scenario_private_only"] is False
    assert publication_status(scenario, repository)["private_only"] is True
    with pytest.raises(ValueError, match="Private-only"):
        publish_translation(
            library,
            repository,
            game="ah",
            slug="private-scenario",
            language="de",
        )

    update_scenario_private_only(
        library, game="ah", slug="private-scenario", mode="public"
    )
    scenario = get_scenario(
        library, "ah", "private-scenario", language="de", campaign="private-campaign"
    )
    assert scenario["private_only"] is False
    assert scenario["private_only_override"] == "public"

    update_scenario_private_only(
        library, game="ah", slug="private-scenario", mode="inherit"
    )

    update_campaign_private_only(
        library, game="ah", slug="private-campaign", private_only=False
    )
    update_scenario_private_only(
        library, game="ah", slug="private-scenario", private_only=True
    )
    scenario = get_scenario(
        library, "ah", "private-scenario", language="de", campaign="private-campaign"
    )
    assert scenario["private_only"] is True
    assert scenario["campaign_private_only"] is False
    assert scenario["scenario_private_only"] is True
    with pytest.raises(ValueError, match="Private-only"):
        publish_extraction(
            library, repository, game="ah", slug="private-scenario"
        )

    update_scenario_private_only(
        library, game="ah", slug="private-scenario", private_only=False
    )
    scenario = get_scenario(
        library, "ah", "private-scenario", language="de", campaign="private-campaign"
    )
    assert scenario["private_only"] is False
    assert read_json(campaign["path"] / "campaign.json")["private_only"] is False
    assert read_json(created["path"] / "project.json")["private_only"] is False


def test_publish_translation_and_detect_changed_cards(tmp_path):
    library = tmp_path / "library"
    repository = tmp_path / "repository"
    create_demo(library)
    scenario = get_scenario(library, "ah", "demo-scenario", language="de")
    shoggoth_path = (
        library
        / "projects/ah/demo-scenario/translations/de/builds/shoggoth.json"
    )
    write_json(shoggoth_path, {"cards": []})
    render_path = (
        library
        / "projects/ah/demo-scenario/translations/de/renders/001-Card.png"
    )
    render_path.parent.mkdir(parents=True)
    render_path.write_bytes(b"rendered card")
    assert changed_translation_cards(scenario, repository) == {"001-Card"}

    result = publish_translation(
        library,
        repository,
        game="ah",
        slug="demo-scenario",
        language="de",
    )

    assert result["destination"] == repository / "projects/ah/demo-scenario"
    assert changed_translation_cards(scenario, repository) == set()
    assert (result["destination"] / "translations/de/builds/shoggoth.json").is_file()
    assert (
        result["destination"] / "translations/de/renders/001-Card.png"
    ).read_bytes() == b"rendered card"
    tracked = result["destination"] / "translations/de/translation.json"
    document = read_translation_document(tracked)
    document["cards"][0]["text"]["de"]["sides"]["front"]["title"] = "Published"
    write_translation_document(
        tracked,
        document,
        base_path=result["destination"] / "base/extraction.json",
    )
    assert changed_translation_cards(scenario, repository) == {"001-Card"}
    preview = contribution_preview(scenario, repository)
    card = preview["cards"][0]
    assert card["display_name"] == "Card"
    title = next(field for field in card["fields"] if field["path"] == "text.front.title")
    assert title["published_source"] == title["local_source"] == "Card"
    assert title["published_translation"] == "Published"
    assert title["local_translation"] == "Karte"
    assert not title["source_changed"]
    assert title["translation_changed"]
    unchanged_rules = next(
        field for field in card["fields"] if field["path"] == "text.front.rules"
    )
    assert not unchanged_rules["source_changed"]
    assert not unchanged_rules["translation_changed"]
    assert not any(field["path"] == "metadata.doom" for field in card["fields"])
    local_base_path = library / "projects/ah/demo-scenario/base/extraction.json"
    local_base = read_base_document(local_base_path)
    local_base["cards"][0].setdefault("game_data", {})["intellect"] = 1
    local_base["cards"][0]["game_data"]["sides"] = {
        "back": {"artist": "", "index": "1b"}
    }
    local_base["cards"][0]["shoggoth"] = {
        "sides": {"front": {"pan_x": 0.25}}
    }
    write_base_document(local_base_path, local_base)
    write_json(shoggoth_path, {"cards": [{"changed": True}]})
    preview = contribution_preview(scenario, repository)
    shoggoth_field = next(
        field
        for field in preview["cards"][0]["fields"]
        if field["path"] == "shoggoth.sides.front.pan_x"
    )
    assert shoggoth_field["published_source"] == ""
    assert shoggoth_field["local_source"] == "0.25"
    intellect = next(
        field
        for field in preview["cards"][0]["fields"]
        if field["path"] == "metadata.intellect"
    )
    assert intellect["published_source"] == intellect["published_translation"] == ""
    assert intellect["local_source"] == intellect["local_translation"] == "1"
    assert intellect["source_changed"] and intellect["translation_changed"]
    assert not any(
        field["path"] == "metadata.back.artist"
        for field in preview["cards"][0]["fields"]
    )
    assert any(
        field["path"] == "metadata.back.index"
        for field in preview["cards"][0]["fields"]
    )
    assert any(
        item["kind"] == "shoggoth" and item["status"] == "modified"
        for item in preview["files"]
    )


def test_publication_status_separates_local_and_pull_request_ready_cards(
    tmp_path, monkeypatch
):
    library = tmp_path / "library"
    repository = tmp_path / "repository"
    create_demo(library)
    scenario = get_scenario(library, "ah", "demo-scenario", language="de")
    publish_translation(
        library,
        repository,
        game="ah",
        slug="demo-scenario",
        language="de",
    )
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "initial publication",
        ],
        cwd=repository,
        check=True,
    )

    translation_path = (
        library / "projects/ah/demo-scenario/translations/de/translation.json"
    )
    translation = read_translation_document(translation_path)
    translation["cards"][0]["text"]["de"]["sides"]["front"]["title"] = "Neu"
    write_translation_document(
        translation_path,
        translation,
        base_path=library / "projects/ah/demo-scenario/base/extraction.json",
    )
    scenario = get_scenario(library, "ah", "demo-scenario", language="de")
    local_status = publication_status(scenario, repository)
    assert local_status["changed_count"] == 1
    assert local_status["local_completed_count"] == 1
    assert not local_status["ready_for_pull_request"]

    publish_translation(
        library,
        repository,
        game="ah",
        slug="demo-scenario",
        language="de",
    )
    scenario = get_scenario(library, "ah", "demo-scenario", language="de")
    ready_status = publication_status(scenario, repository)
    assert ready_status["changed_count"] == 0
    assert ready_status["repository_changed_card_count"] == 1
    assert ready_status["ready_completed_count"] == 1
    assert ready_status["ready_for_pull_request"]
    assert any(
        "/translations/de/cards/" in f"/{path}" and path.endswith(".json")
        for path in ready_status["repository_changed_files"]
    )

    # A contributor may continue locally while the previous publication is still
    # waiting for a PR. The preview must show both states without conflating them,
    # and publishing again must merge the new local state into the same scope.
    translation = read_translation_document(translation_path)
    translation["cards"][0]["text"]["de"]["sides"]["front"]["title"] = "Noch neuer"
    write_translation_document(
        translation_path,
        translation,
        base_path=library / "projects/ah/demo-scenario/base/extraction.json",
    )
    scenario = get_scenario(library, "ah", "demo-scenario", language="de")
    mixed_preview = contribution_preview(scenario, repository)
    assert mixed_preview["ready_for_pull_request"]
    assert mixed_preview["repository_changed_card_count"] == 1
    assert mixed_preview["file_count"] > 0
    assert len(mixed_preview["cards"]) == 1

    monkeypatch.setenv("CARD_TRANSLATOR_ROOT", str(repository))
    monkeypatch.setenv("CARD_TRANSLATOR_LIBRARY", str(library))
    monkeypatch.setattr(
        "card_translator.web.app.ollama_status",
        lambda: {"enabled": False, "installed": False, "available": False},
    )
    app = create_app()
    request = Request({
        "type": "http",
        "method": "GET",
        "path": "/",
        "root_path": "",
        "scheme": "http",
        "server": ("test", 80),
        "headers": [(b"cookie", b"card_translator_ui_language=de")],
        "query_string": b"",
        "router": app.router,
    })
    dashboard = next(route.endpoint for route in app.routes if route.name == "dashboard")

    preview_page = next(
        route.endpoint
        for route in app.routes
        if route.name == "contribution_preview_page"
    )
    mixed_preview_body = preview_page(
        request,
        game="ah",
        project_language="de",
        slug="demo-scenario",
        published=None,
    ).body.decode()
    assert "Änderungen warten bereits auf einen Pull Request" in mixed_preview_body
    assert "Lokale Änderungen seit dem letzten Publish" in mixed_preview_body
    assert "1 lokal betroffene Karten" in mixed_preview_body
    assert "Veröffentlichung aktualisieren" in mixed_preview_body

    publish_translation(
        library,
        repository,
        game="ah",
        slug="demo-scenario",
        language="de",
    )
    scenario = get_scenario(library, "ah", "demo-scenario", language="de")
    ready_status = publication_status(scenario, repository)
    assert ready_status["changed_count"] == 0
    assert ready_status["repository_changed_card_count"] == 1

    dashboard_body = dashboard(request).body.decode()
    assert 'class="progress contribution-progress"' in dashboard_body
    assert 'class="progress-ready"' in dashboard_body
    assert "1 bereit für PR" in dashboard_body

    detail = next(route.endpoint for route in app.routes if route.name == "scenario_detail")
    detail_body = detail(
        request, game="ah", project_language="de", slug="demo-scenario"
    ).body.decode()
    assert "Der nächste Schritt ist ein Pull Request" in detail_body
    assert "Pull Request erstellen" in detail_body

    preview_body = preview_page(
        request,
        game="ah",
        project_language="de",
        slug="demo-scenario",
        published="1",
    ).body.decode()
    assert 'class="panel publish-success-panel"' in preview_body
    assert "wurden in den versionierten projects/-Ordner übertragen" in preview_body
    assert (
        f"Betroffene Karten: 1 · Dateien: "
        f"{ready_status['repository_changed_file_count']}"
    ) in preview_body


def test_publish_extraction_does_not_copy_translations(tmp_path):
    library = tmp_path / "library"
    repository = tmp_path / "repository"
    create_demo(library)
    scenario = get_scenario(library, "ah", "demo-scenario", language="source")

    assert changed_base_cards(scenario, repository) == {"001-Card"}
    status = publication_status(scenario, repository)
    assert status["contribution_type"] == "base"
    assert status["changed_count"] == 1
    assert not status["published"]
    preview = contribution_preview(scenario, repository)
    assert preview["added_count"] > 0
    assert not any("/translations/" in item["path"] for item in preview["files"])

    result = publish_extraction(
        library,
        repository,
        game="ah",
        slug="demo-scenario",
    )

    assert result["contribution_type"] == "base"
    assert (result["destination"] / "base/cards/001-Card.json").is_file()
    assert not (result["destination"] / "translations").exists()
    assert publication_status(scenario, repository)["published"]
    assert changed_base_cards(scenario, repository) == set()
    assert contribution_preview(scenario, repository)["file_count"] == 0

    base_path = Path(scenario["path"]) / "base/extraction.json"
    base = read_base_document(base_path)
    base["cards"] = []
    write_base_document(base_path, base)
    assert changed_base_cards(scenario, repository) == {"001-Card"}
    preview = contribution_preview(scenario, repository)
    assert {
        (item["status"], item["path"])
        for item in preview["files"]
    } >= {
        ("modified", "projects/ah/demo-scenario/base/extraction.json"),
        ("deleted", "projects/ah/demo-scenario/base/cards/001-Card.json"),
    }


def test_separate_card_art_is_used_by_shoggoth(tmp_path):
    create_demo(tmp_path)
    update_separate_card_art_setting(
        tmp_path,
        game="ah",
        slug="demo-scenario",
        project_language="de",
        enabled=True,
    )
    saved = save_separate_card_art(
        tmp_path,
        game="ah",
        slug="demo-scenario",
        project_language="de",
        card_key="001-Card",
        side="front",
        filename="large-art.png",
        content=b"high resolution art",
    )
    scenario = get_scenario(tmp_path, "ah", "demo-scenario", language="de")
    card = build_shoggoth_document(scenario)["cards"][0]

    assert saved["path"].startswith("source/art/")
    assert card["front"]["illustration"].endswith(saved["path"])
    assert Path(card["front"]["illustration"]).read_bytes() == b"high resolution art"


def test_downloaded_project_is_grouped_with_projects_on_this_device(tmp_path, monkeypatch):
    library = tmp_path / "library"
    result = create_demo(library)
    write_json(result["path"] / ".card-translator-remote.json", {"format_version": 1})
    monkeypatch.setenv("CARD_TRANSLATOR_ROOT", str(tmp_path))
    monkeypatch.setenv("CARD_TRANSLATOR_LIBRARY", str(library))
    monkeypatch.setattr(
        "card_translator.web.app.refresh_repository",
        lambda *args, **kwargs: {
            "configured": True,
            "available": True,
            "projects": [{
                "id": "ah/demo-scenario",
                "game": "ah",
                "slug": "demo-scenario",
                "title": "Demo Scenario",
                "base": {"extraction_percent": 100},
                "translations": {"de": {"translation_percent": 50}},
            }],
        },
    )
    monkeypatch.setattr(
        "card_translator.web.app.project_remote_status",
        lambda *args, **kwargs: {"update_available": False},
    )
    app = create_app()
    request = Request({
        "type": "http", "method": "GET", "path": "/", "root_path": "",
        "scheme": "http", "server": ("test", 80),
        "headers": [(b"cookie", b"card_translator_ui_language=de")],
        "query_string": b"", "router": app.router,
    })

    dashboard = next(route.endpoint for route in app.routes if route.name == "dashboard")
    body = dashboard(request).body.decode()

    assert "Projekte auf diesem Rechner" in body
    assert "heruntergeladen" in body
    assert "Gespiegelte Projekte" not in body
    assert body.index("heruntergeladen") < body.index("Verfügbare Projekte")


def test_dashboard_shows_scenario_without_translation(tmp_path, monkeypatch):
    library = tmp_path / "library"
    result = create_shared_project(
        library,
        game="ah",
        slug="source-only",
        title="Source Only",
        author="Example",
        source_url="",
        source_language="en",
    )
    (result["cards_path"] / "001 - The Door-1.jpg").write_bytes(b"front")
    monkeypatch.setenv("CARD_TRANSLATOR_ROOT", str(tmp_path))
    monkeypatch.setenv("CARD_TRANSLATOR_LIBRARY", str(library))
    app = create_app()
    request = Request({
        "type": "http", "method": "GET", "path": "/", "root_path": "",
        "scheme": "http", "server": ("test", 80),
        "headers": [(b"cookie", b"card_translator_ui_language=de")],
        "query_string": b"", "router": app.router,
    })

    dashboard = next(route.endpoint for route in app.routes if route.name == "dashboard")
    body = dashboard(request).body.decode()

    assert "Source Only" in body
    assert "Originaldaten erfassen · EN" in body
    assert 'href="/ah/source/source-only"' in body
    assert 'action="/projects/translations/create"' in body
    assert '<option value="ah/source-only">Source Only · AH</option>' in body

    detail = next(route.endpoint for route in app.routes if route.name == "scenario_detail")
    detail_body = detail(
        request, game="ah", project_language="source", slug="source-only",
    ).body.decode()
    assert "Originaldaten · EN" in detail_body
    assert str(result["cards_path"]) in detail_body
    assert "001 - Kartenname-1.jpg" in detail_body
    assert "Normale Kartenrückseiten können weggelassen werden." in detail_body
    assert "nur <i>ein</i> Bildpaar" in detail_body
    assert "Anzahl <i>kann</i> später" in detail_body
    assert 'class="shoggoth-import-form"' in detail_body
    assert "arkhamdb-import-panel" not in detail_body
    assert "Aus ArkhamDB befüllen" not in detail_body
    assert "data-import-panel" not in detail_body
    assert "filename-parser-panel" not in detail_body
    assert "preview-filenames" not in detail_body
    assert 'class="source-fields source-side-form"' in detail_body
    assert 'class="translation-side-form"' not in detail_body
    assert "Übersetzung direkt unter Originalfeld anzeigen" not in detail_body
    assert 'href="/ah/source/source-only/contribute"' in detail_body
    assert "Änderungen prüfen" in detail_body
    assert "Ergebnisse mit der Community teilen" in detail_body

    import_arkhamdb = next(
        route.endpoint for route in app.routes if route.name == "import_arkhamdb"
    )
    with pytest.raises(HTTPException) as hidden:
        import_arkhamdb(project_language="source", slug="source-only")
    assert hidden.value.status_code == 404

    update_card_source_text(
        library,
        game="ah",
        slug="source-only",
        project_language="source",
        card_key="001 - The Door",
        side="front",
        title="",
        rules="",
        flavor="",
        traits="",
        field_values={"title": "The Door", "rules": "Open it."},
    )
    base = read_base_document(result["path"] / "base/extraction.json")
    assert base["cards"][0]["text"]["en"]["sides"]["front"]["title"] == "The Door"
    assert not (result["path"] / "translations").exists()

    preview_page = next(
        route.endpoint
        for route in app.routes
        if route.name == "contribution_preview_page"
    )
    preview_body = preview_page(
        request, game="ah", project_language="source", slug="source-only"
    ).body.decode()
    assert "Contribution-Vorschau" in preview_body
    assert 'action="/ah/source/source-only/publish"' in preview_body
    assert "Originaldaten veröffentlichen" in preview_body
    assert "projects/ah/source-only/base/cards/001_-_The_Door.json" in preview_body
    assert "The Door" in preview_body
    assert "Original (veröffentlicht)" in preview_body
    assert "Meine Änderung am Original" in preview_body
    assert "Lokale Werte bearbeiten" in preview_body
    assert "Karte im Editor öffnen" in preview_body
    assert "text.front.title" in preview_body

    update_preview_field = next(
        route.endpoint
        for route in app.routes
        if route.name == "update_contribution_field"
    )
    saved = update_preview_field(
        game="ah",
        project_language="source",
        slug="source-only",
        card_key="001 - The Door",
        scope="source",
        path="metadata.card_number",
        value="7",
    )
    assert saved["ok"]
    base = read_base_document(result["path"] / "base/extraction.json")
    assert base["cards"][0]["game_data"]["card_number"] == "7"

    publish = next(
        route.endpoint for route in app.routes if route.name == "publish_project_results"
    )
    response = publish(game="ah", project_language="source", slug="source-only")
    assert response.status_code == 303
    assert response.headers["location"] == "/ah/source/source-only/contribute?published=1"
    published = tmp_path / "projects/ah/source-only"
    assert read_base_document(published / "base/extraction.json")["cards"][0][
        "key"
    ] == "001 - The Door"
    assert not (published / "translations").exists()

    monkeypatch.setenv("CARD_TRANSLATOR_ENABLE_ARKHAMDB_SCENARIO_IMPORT", "true")
    enabled_body = detail(
        request, game="ah", project_language="source", slug="source-only",
    ).body.decode()
    assert 'class="panel arkhamdb-import-panel"' in enabled_body
    assert "Aus ArkhamDB befüllen" in enabled_body
    assert 'class="panel data-import-panel"' in enabled_body


def test_workbench_renders_creation_publication_changes_and_art_controls(tmp_path, monkeypatch):
    library = tmp_path / "library"
    create_demo(library)
    update_separate_card_art_setting(
        library,
        game="ah",
        slug="demo-scenario",
        project_language="de",
        enabled=True,
    )
    monkeypatch.setenv("CARD_TRANSLATOR_ROOT", str(tmp_path))
    monkeypatch.setenv("CARD_TRANSLATOR_LIBRARY", str(library))
    monkeypatch.setattr(
        "card_translator.web.app.ollama_status",
        lambda: {"enabled": False, "installed": False, "available": False},
    )
    app = create_app()
    request = Request({
        "type": "http",
        "method": "GET",
        "path": "/ah/de/demo-scenario",
        "root_path": "",
        "scheme": "http",
        "server": ("test", 80),
        "headers": [(b"cookie", b"card_translator_ui_language=de")],
        "query_string": b"",
        "router": app.router,
    })

    dashboard = next(route.endpoint for route in app.routes if route.name == "dashboard")
    dashboard_body = dashboard(request).body.decode()
    assert 'action="/projects/create"' in dashboard_body
    assert 'action="/projects/translations/create"' in dashboard_body
    assert "Szenario anlegen" in dashboard_body
    assert "Szenariodaten</legend>" in dashboard_body
    assert "Übersetzung beginnen" in dashboard_body
    assert "lokales Projekt" in dashboard_body
    assert 'class="contribution-legend"' in dashboard_body
    assert "Im Repository, bereit für PR" in dashboard_body
    assert "Nur lokal bearbeitet" in dashboard_body
    assert "Noch nicht übersetzt" in dashboard_body
    assert 'name="source_language"' in dashboard_body
    assert 'name="target_language"' in dashboard_body
    assert 'name="private_only" value="true"' in dashboard_body

    detail = next(route.endpoint for route in app.routes if route.name == "scenario_detail")
    body = detail(
        request, game="ah", project_language="de", slug="demo-scenario",
    ).body.decode()
    assert 'href="/ah/de/demo-scenario/contribute"' in body
    assert "Änderungen prüfen" in body
    assert 'href="/ah/de/demo-scenario/contribute#diff-card-1"' in body
    assert "Kartendiff öffnen" in body
    assert "1</strong>" in body
    assert "lokal geändert" in body
    assert 'name="enabled" value="true" checked' in body
    assert 'action="/ah/de/demo-scenario/private-only"' in body
    assert 'name="private_mode"' in body
    assert 'value="inherit" selected' in body
    assert 'enctype="multipart/form-data"' in body
    assert 'name="art_file"' in body
    image_stage = body.index('class="card-image-stage"')
    resize_handle = body.index('class="card-width-resizer"', image_stage)
    image_actions = body.index('class="image-actions"', resize_handle)
    llm_json = body.index('class="llm-json-import"', image_actions)
    assert image_stage < resize_handle < image_actions < llm_json

    preview_page = next(
        route.endpoint
        for route in app.routes
        if route.name == "contribution_preview_page"
    )
    preview_body = preview_page(
        request, game="ah", project_language="de", slug="demo-scenario"
    ).body.decode()
    assert "Card" in preview_body
    assert "Karte" in preview_body
    assert "Übersetzung (veröffentlicht)" in preview_body
    assert "Meine Änderung an der Übersetzung" in preview_body
    assert "/library/projects/ah/demo-scenario/source/cards/001-Card-1.png" in preview_body
    assert 'class="card-width-resizer"' in preview_body
    assert 'class="contribution-review-layout' in preview_body
    assert 'class="contribution-review-image-stage"' in preview_body
    assert preview_body.count('class="button-secondary contribution-rotate-image"') == 2
    assert "translate(-50%, -50%) rotate(${rotation}deg)" in preview_body
    assert 'class="contribution-card-note" hidden' in preview_body
    assert 'class="panel contribution-preview-notes" hidden' in preview_body
    assert 'class="badge badge-warn contribution-note-badge" hidden' in preview_body
    assert "Notiz zu dieser Karte" in preview_body
    assert "Offene Kartennotizen" in preview_body
    assert "unspeakable-translations.card-notes:${projectBaseUrl}" in preview_body

    update_scenario_private_only(
        library, game="ah", slug="demo-scenario", private_only=True
    )
    private_body = detail(
        request, game="ah", project_language="de", slug="demo-scenario",
    ).body.decode()
    assert "Private Veröffentlichungssperre aktiv" in private_body
    assert "Veröffentlichung gesperrt" in private_body
    private_preview = preview_page(
        request, game="ah", project_language="de", slug="demo-scenario"
    ).body.decode()
    assert "Private Veröffentlichungssperre aktiv" in private_preview
    assert 'action="/ah/de/demo-scenario/publish"' not in private_preview

    update_preview_field = next(
        route.endpoint
        for route in app.routes
        if route.name == "update_contribution_field"
    )
    saved = update_preview_field(
        game="ah",
        project_language="de",
        slug="demo-scenario",
        card_key="001-Card",
        scope="translation",
        path="text.front.title",
        value="Neue Karte",
    )
    assert saved["ok"]
    translation = read_translation_document(
        library / "projects/ah/demo-scenario/translations/de/translation.json"
    )
    assert translation["cards"][0]["text"]["de"]["sides"]["front"]["title"] == (
        "Neue Karte"
    )
    update_preview_field(
        game="ah",
        project_language="de",
        slug="demo-scenario",
        card_key="001-Card",
        scope="source",
        path="metadata.intellect",
        value="1",
    )
    update_preview_field(
        game="ah",
        project_language="de",
        slug="demo-scenario",
        card_key="001-Card",
        scope="source",
        path="metadata.combat",
        value="",
    )
    base = read_base_document(library / "projects/ah/demo-scenario/base/extraction.json")
    assert base["cards"][0]["game_data"]["intellect"] == 1
    assert base["cards"][0]["game_data"]["combat"] is None
