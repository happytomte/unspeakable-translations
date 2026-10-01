from __future__ import annotations

import json

import pytest

from card_translator.data_import import (
    apply_filename_metadata,
    import_arkhamdb_scenario_data,
    import_card_data,
    import_shoggoth_data,
    latest_import_status,
    match_import_record,
    preview_card_data,
    preview_filename_metadata,
    set_card_identifier,
    unmatched_import_records,
)
from card_translator.games import save_filename_parser, save_game
from card_translator.library import get_scenario, initialize_scenario
from card_translator.project_publication import create_shared_project
from card_translator.shared_projects import read_base_document
from card_translator.shoggoth_export import build_shoggoth_document


def test_imports_local_arkhamdb_source_and_translation_by_assigned_id(tmp_path):
    cards = tmp_path / "ah" / "de" / "scenario" / "cards"
    cards.mkdir(parents=True)
    (cards / "001 - Witch-Haunted Woods-1.png").write_bytes(b"front")
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
    data = tmp_path / "ah" / "data"
    data.mkdir()
    (data / "arkham.en.json").write_text(json.dumps([{
        "code": "05060", "name": "Witch-Haunted Woods", "text": "Source rules",
        "traits": "Woods.", "type_code": "location", "shroud": 2,
        "clues": 1, "illustrator": "Lukas Banas", "faction_code": "mythos",
    }]))
    (data / "arkham.de.json").write_text(json.dumps([{
        "code": "05060", "name": "Von Hexen heimgesuchte Wälder",
        "text": "Deutsche Regeln", "traits": "Wälder.", "type_code": "location",
    }]))
    result = import_arkhamdb_scenario_data(
        tmp_path, scenario_slug="scenario", project_language="de"
    )

    translation = json.loads(
        (tmp_path / "ah" / "de" / "scenario" / "translation_de.json").read_text()
    )
    card = translation["cards"][0]
    assert result["source_imported"] == 1
    assert result["target_imported"] == 1
    assert card["text"]["en"]["sides"]["front"]["title"] == "Witch-Haunted Woods"
    assert card["text"]["de"]["sides"]["front"]["rules"] == "Deutsche Regeln"
    assert card["card_type"] == "location"
    assert card["game_data"]["shroud"] == 2
    assert card["game_data"]["artist"] == "Lukas Banas"
    assert card["game_data"]["classes"] == ["mythos"]
    assert card["identifiers"]["arkham-json"] == "05060"


def test_imports_shoggoth_source_data_and_preserves_layout_fields(tmp_path):
    result = create_shared_project(
        tmp_path,
        game="ah",
        slug="shoggoth-source",
        title="Shoggoth Source",
        author="",
        source_url="",
        source_language="en",
    )
    image = result["cards_path"] / "001 - The Door-front.jpg"
    image.write_bytes(b"front")
    payload = {
        "name": "Shoggoth Source",
        "cards": [{
            "id": "door-1",
            "name": "The Door",
            "amount": 2,
            "front": {
                "type": "location",
                "name": "The Door",
                "traits": "Door. Mysterious.",
                "text": "<b>Forced</b> – Open it. <action>",
                "illustration": "./images/001 - The Door-front.jpg",
                "shroud": "3",
                "clues": "1<per>",
                "illustration_scale": 1.25,
                "illustration_x": -14,
            },
        }],
    }

    imported = import_shoggoth_data(
        tmp_path,
        game_id="ah",
        scenario_slug="shoggoth-source",
        project_language="source",
        filename="project.shoggoth",
        content=json.dumps(payload).encode(),
    )

    assert imported["matched_by_image"] == 1
    base = read_base_document(result["path"] / "base/extraction.json")
    card = base["cards"][0]
    assert card["text"]["en"]["sides"]["front"]["rules"] == (
        "<b>Forced</b> – Open it. {action}"
    )
    assert card["game_data"]["clues"] == "1<per>"
    assert "clues_per_investigator" not in card["game_data"]
    assert card["game_data"]["printed_quantity"] == 2
    assert card["shoggoth"]["sides"]["front"] == {
        "illustration": "./images/001 - The Door-front.jpg",
        "illustration_scale": 1.25,
        "illustration_x": -14,
    }
    scenario = get_scenario(tmp_path, "ah", "shoggoth-source", language="source")
    exported = build_shoggoth_document(scenario)["cards"][0]["front"]
    assert exported["illustration_scale"] == 1.25
    assert exported["illustration_x"] == -14
    assert exported["illustration"] == "./images/001 - The Door-front.jpg"
    assert not (result["path"] / "translations").exists()


def test_round_trips_shoggoth_player_fields(tmp_path):
    result = create_shared_project(
        tmp_path,
        game="ah",
        slug="player-cards",
        title="Player Cards",
        author="",
        source_url="",
        source_language="en",
    )
    image = result["cards_path"] / "charles_front_0.png"
    image.write_bytes(b"front")
    payload = {
        "name": "Player Cards",
        "cards": [{
            "id": "charles",
            "name": "Charles",
            "amount": 2,
            "front": {
                "type": "asset",
                "name": "Charles",
                "illustration": "charles_front_0.png",
                "classes": ["neutral"],
                "cost": "3",
                "level": "0",
                "icons": "WIQ",
                "health": "2",
                "sanity": "1",
                "slots": ["ally"],
            },
            "back": {"type": "player"},
        }],
    }

    import_shoggoth_data(
        tmp_path,
        game_id="ah",
        scenario_slug="player-cards",
        project_language="source",
        filename="players.json",
        content=json.dumps(payload).encode(),
    )

    base = read_base_document(result["path"] / "base/extraction.json")
    data = base["cards"][0]["game_data"]
    assert data["classes"] == ["neutral"]
    assert data["cost"] == "3"
    assert data["xp"] == 0
    assert data["slot"] == "ally"
    assert data["willpower"] == 1
    assert data["intellect"] == 1
    assert data["wild"] == 1
    assert data["sanity"] == "1"

    scenario = get_scenario(tmp_path, "ah", "player-cards", language="source")
    card = build_shoggoth_document(scenario)["cards"][0]
    assert "encounter_set" not in card
    assert card["amount"] == 2
    assert card["front"]["classes"] == ["neutral"]
    assert card["front"]["cost"] == "3"
    assert card["front"]["level"] == "0"
    assert card["front"]["icons"] == "WIQ"
    assert card["front"]["health"] == "2"
    assert card["front"]["sanity"] == "1"
    assert card["front"]["slots"] == ["ally"]


def test_imports_a_shoggoth_image_export_by_card_id_and_keeps_project_data(tmp_path):
    result = create_shared_project(
        tmp_path,
        game="ah",
        slug="alice",
        title="Alice",
        author="",
        source_url="",
        source_language="en",
    )
    card_id = "314a931d-9183-4b15-9fe7-d895c6cb1331"
    prefix = f"{card_id}_act_example"
    front = result["cards_path"] / f"{prefix}_front_0.png"
    back = result["cards_path"] / f"{prefix}_back_0.png"
    front.write_bytes(b"front")
    back.write_bytes(b"back")
    location_layouts = [{
        "id": "layout-1",
        "nodes": {f"{card_id}_front": {"x": 200.0, "y": 0.0}},
    }]
    payload = {
        "name": "Alice",
        "code": "ace",
        "icon": "/icons/alice.svg",
        "encounter_sets": [{
            "name": "Encounter",
            "id": "ace-enc-loc",
            "cards": [],
            "meta": {
                "location_layouts": location_layouts,
                "location_active_layout": "layout-1",
            },
        }],
        "export_profiles": [{"id": "profile-1", "name": "Default"}],
        "cards": [{
            "id": card_id,
            "name": "Act Example",
            "encounter_set": "ace-enc-loc",
            "enumerated": "manual",
            "front": {
                "type": "act",
                "name": "Act Example",
                "illustration": "/original/art/act.jpg",
                "illustration_pan_x": 4.0,
                "illustration_pan_y": 28.0,
                "illustration_scale": 3.9,
                "index": "2a",
            },
            "back": {"type": "act_back", "index": "2b"},
        }],
    }

    imported = import_shoggoth_data(
        tmp_path,
        game_id="ah",
        scenario_slug="alice",
        project_language="source",
        filename="d.json",
        content=json.dumps(payload).encode(),
    )

    assert imported["matched_by_image"] == 1
    assert imported["unmatched"] == 0
    base = read_base_document(result["path"] / "base/extraction.json")
    card = base["cards"][0]
    assert card["key"] == prefix
    assert card["images"]["front"].endswith(f"{prefix}_front_0.png")
    assert card["images"]["back"].endswith(f"{prefix}_back_0.png")
    assert card["identifiers"]["shoggoth"] == card_id
    assert card["shoggoth"]["sides"]["front"]["illustration"] == "/original/art/act.jpg"
    assert card["shoggoth"]["sides"]["front"]["illustration_pan_y"] == 28.0
    assert card["game_data"]["sides"]["back"]["card_type"] == "act_back"

    scenario = get_scenario(tmp_path, "ah", "alice", language="source")
    exported = build_shoggoth_document(scenario)
    exported_card = exported["cards"][0]
    assert exported["code"] == "ace"
    assert exported["export_profiles"] == payload["export_profiles"]
    assert exported["encounter_sets"][0]["meta"]["location_layouts"] == location_layouts
    assert exported_card["id"] == card_id
    assert exported_card["encounter_set"] == "ace-enc-loc"
    assert exported_card["front"]["illustration"] == "/original/art/act.jpg"
    assert exported_card["front"]["illustration_pan_x"] == 4.0
    assert exported_card["front"]["illustration_scale"] == 3.9


def test_shoggoth_reimport_reconnects_records_from_an_older_unmatched_import(tmp_path):
    result = create_shared_project(
        tmp_path,
        game="ah",
        slug="alice",
        title="Alice",
        author="",
        source_url="",
        source_language="en",
    )
    card_id = "314a931d-9183-4b15-9fe7-d895c6cb1331"
    local_key = f"{card_id}_act_example"
    (result["cards_path"] / f"{local_key}_front_0.png").write_bytes(b"front")
    stale = {
        "format": "card-translator-base",
        "format_version": 1,
        "game": "ah",
        "scenario": "alice",
        "source_language": "en",
        "cards": [{
            "id": card_id,
            "key": card_id,
            "identifiers": {"shoggoth": card_id},
            "text": {"en": {"title": "Old import"}},
            "game_data": {},
        }],
        "_meta": {},
    }
    (result["path"] / "base/extraction.json").write_text(json.dumps(stale))
    payload = {
        "cards": [{
            "id": card_id,
            "name": "Act Example",
            "front": {"type": "act", "name": "Act Example"},
        }],
    }

    imported = import_shoggoth_data(
        tmp_path,
        game_id="ah",
        scenario_slug="alice",
        project_language="source",
        filename="d.json",
        content=json.dumps(payload).encode(),
    )

    base = read_base_document(result["path"] / "base/extraction.json")
    assert imported["matched_by_image"] == 1
    assert len(base["cards"]) == 1
    assert base["cards"][0]["id"] == local_key
    assert base["cards"][0]["key"] == local_key


def _project(tmp_path):
    game = save_game(tmp_path, game_id="test", name="Test Game")
    game["imports"] = [
        {
            "id": "test-json",
            "label": "Test JSON",
            "records_path": "cards",
            "identifier_namespace": "testdb",
            "identifier": "IDENTIFIKATOR",
            "fields": {"title": "Name", "rules": "Text", "game_data.card_number": "Number"},
        }
    ]
    (tmp_path / "test" / "game.json").write_text(json.dumps(game))
    cards = tmp_path / "test" / "de" / "scenario" / "cards"
    cards.mkdir(parents=True)
    (cards / "003_Miskatonic Library-1.jpg").write_bytes(b"image")
    initialize_scenario(
        tmp_path,
        game="test",
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


def test_import_maps_fields_matches_name_and_keeps_snapshot(tmp_path):
    _project(tmp_path)
    payload = json.dumps(
        {
            "cards": [
                {
                    "IDENTIFIKATOR": "001",
                    "Name": "Miskatonic Library",
                    "Text": "Test your knowledge.",
                    "Number": 3,
                },
                {"IDENTIFIKATOR": "002", "Name": "Missing Card"},
            ]
        }
    ).encode()

    result = import_card_data(
        tmp_path,
        game_id="test",
        scenario_slug="scenario",
        project_language="de",
        source_id="test-json",
        filename="source.json",
        content=payload,
    )

    assert result == {
        "total": 2,
        "matched_by_id": 0,
        "matched_by_name": 1,
        "unmatched": 1,
        "snapshot": result["snapshot"],
    }
    assert (tmp_path / "test" / "de" / "scenario" / result["snapshot"]).read_bytes() == payload
    translation = json.loads(
        (tmp_path / "test" / "de" / "scenario" / "translation_de.json").read_text()
    )
    matched = next(card for card in translation["cards"] if card["identifiers"]["testdb"] == "001")
    assert matched["key"] == "003_Miskatonic Library"
    assert matched["images"]["front"] == "cards/003_Miskatonic Library-1.jpg"
    assert matched["text"]["en"]["sides"]["front"]["rules"] == "Test your knowledge."
    assert matched["game_data"]["card_number"] == 3
    scenario = get_scenario(tmp_path, "test", "scenario", language="de")
    status = latest_import_status(scenario)
    assert status["total"] == 2
    assert len(status["open_records"]) == 1
    assert status["unmatched_local_cards"] == 0


def test_import_accepts_top_level_array_and_maps_back_side(tmp_path):
    _project(tmp_path)
    game_path = tmp_path / "test" / "game.json"
    game = json.loads(game_path.read_text())
    game["imports"][0]["fields"].update(
        {
            "sides.back.title": "back_name",
            "sides.back.rules": "back_text",
            "sides.back.flavor": "back_flavor",
        }
    )
    game_path.write_text(json.dumps(game))
    payload = json.dumps(
        [
            {
                "IDENTIFIKATOR": "001",
                "Name": "Miskatonic Library",
                "Text": "Front rules.",
                "back_name": "The Restricted Collection",
                "back_text": "Back rules.",
                "back_flavor": "Back flavor.",
            }
        ]
    ).encode()

    result = import_card_data(
        tmp_path,
        game_id="test",
        scenario_slug="scenario",
        project_language="de",
        source_id="test-json",
        filename="source.json",
        content=payload,
    )

    assert result["matched_by_name"] == 1
    translation = json.loads(
        (tmp_path / "test" / "de" / "scenario" / "translation_de.json").read_text()
    )
    sides = translation["cards"][0]["text"]["en"]["sides"]
    assert sides["front"]["rules"] == "Front rules."
    assert sides["back"] == {
        "title": "The Restricted Collection",
        "rules": "Back rules.",
        "flavor": "Back flavor.",
    }


def test_preview_reports_matches_and_duplicate_ids_without_writing(tmp_path):
    _project(tmp_path)
    translation_path = tmp_path / "test" / "de" / "scenario" / "translation_de.json"
    before = translation_path.read_bytes()
    payload = json.dumps(
        {
            "cards": [
                {"IDENTIFIKATOR": "001", "Name": "Miskatonic Library"},
                {"IDENTIFIKATOR": "001", "Name": "Unknown"},
            ]
        }
    ).encode()

    result = preview_card_data(
        tmp_path,
        game_id="test",
        scenario_slug="scenario",
        project_language="de",
        source_id="test-json",
        content=payload,
    )

    assert result["total"] == 2
    assert result["matched_by_name"] == 1
    assert result["unmatched"] == 1
    assert result["duplicate_ids"] == ["001"]
    assert translation_path.read_bytes() == before
    assert list((translation_path.parent / "snapshots").iterdir()) == []


def test_filename_parser_previews_then_applies_id_and_source_title(tmp_path):
    _project(tmp_path)
    cards = tmp_path / "test" / "de" / "scenario" / "cards"
    (cards / "003_Miskatonic Library-2.jpg").write_bytes(b"back")
    save_filename_parser(
        tmp_path,
        game_id="test",
        pattern=r"^(?P<id>\d+)_(?P<title>.+)-(?P<side>[12])$",
        identifier_namespace="testdb",
    )
    translation_path = tmp_path / "test" / "de" / "scenario" / "translation_de.json"
    before = translation_path.read_bytes()

    preview = preview_filename_metadata(
        tmp_path, game_id="test", scenario_slug="scenario", project_language="de"
    )
    assert [row["side"] for row in preview["rows"][0]["side_rows"]] == ["front", "back"]
    assert [row["filename"] for row in preview["rows"][0]["side_rows"]] == [
        "003_Miskatonic Library-1.jpg",
        "003_Miskatonic Library-2.jpg",
    ]

    assert preview["matched"] == 1
    assert preview["rows"][0]["external_id"] == "003"
    assert preview["rows"][0]["title"] == "Miskatonic Library"
    assert translation_path.read_bytes() == before

    result = apply_filename_metadata(
        tmp_path, game_id="test", scenario_slug="scenario", project_language="de"
    )
    data = json.loads(translation_path.read_text())
    card = next(item for item in data["cards"] if item["key"] == "003_Miskatonic Library")
    assert result["applied"] == 1
    assert card["identifiers"]["testdb"] == "003"
    assert card["text"]["en"]["sides"]["front"]["title"] == "Miskatonic Library"
    assert card["text"]["en"]["sides"]["back"]["title"] == "Miskatonic Library"


def test_filename_parser_turns_title_underscores_into_spaces(tmp_path):
    _project(tmp_path)
    cards = tmp_path / "test" / "de" / "scenario" / "cards"
    (cards / "004_Of_Cats_and_Dogs-1.jpg").write_bytes(b"front")
    save_filename_parser(
        tmp_path,
        game_id="test",
        pattern=r"^(?P<id>\d+)_(?P<title>.+)-(?P<side>[12])$",
        identifier_namespace="testdb",
    )

    preview = preview_filename_metadata(
        tmp_path, game_id="test", scenario_slug="scenario", project_language="de"
    )
    apply_filename_metadata(
        tmp_path, game_id="test", scenario_slug="scenario", project_language="de"
    )

    row = next(item for item in preview["rows"] if item["external_id"] == "004")
    data = json.loads(
        (tmp_path / "test" / "de" / "scenario" / "translation_de.json").read_text()
    )
    card = next(item for item in data["cards"] if item["key"] == "004_Of_Cats_and_Dogs")
    assert row["title"] == "Of Cats and Dogs"
    assert row["side_rows"][0]["title"] == "Of Cats and Dogs"
    assert card["text"]["en"]["sides"]["front"]["title"] == "Of Cats and Dogs"


def test_temporary_filename_pattern_applies_number_and_title_without_external_id(tmp_path):
    _project(tmp_path)
    save_filename_parser(
        tmp_path,
        game_id="test",
        pattern=r"^(?P<id>never)-(?P<title>matches)$",
        identifier_namespace="testdb",
    )
    temporary_pattern = r"^(?P<card_number>\d+)_(?P<title>.+)-(?P<side>[12])$"

    preview = preview_filename_metadata(
        tmp_path,
        game_id="test",
        scenario_slug="scenario",
        project_language="de",
        parser_pattern=temporary_pattern,
    )
    result = apply_filename_metadata(
        tmp_path,
        game_id="test",
        scenario_slug="scenario",
        project_language="de",
        parser_pattern=temporary_pattern,
    )

    data = json.loads(
        (tmp_path / "test" / "de" / "scenario" / "translation_de.json").read_text()
    )
    card = next(item for item in data["cards"] if item["key"] == "003_Miskatonic Library")
    assert preview["rows"][0]["card_number"] == "003"
    assert preview["rows"][0]["external_id"] == ""
    assert card["game_data"]["card_number"] == 3
    assert card["text"]["en"]["sides"]["front"]["title"] == "Miskatonic Library"
    assert result["pattern"] == temporary_pattern
    assert get_scenario(tmp_path, "test", "scenario", language="de")["game_definition"][
        "filename_parser"
    ]["pattern"] == r"^(?P<id>never)-(?P<title>matches)$"


def test_temporary_filename_pattern_saves_back_side_title(tmp_path):
    _project(tmp_path)
    cards = tmp_path / "test" / "de" / "scenario" / "cards"
    (cards / "004 - Back Name-2.jpg").write_bytes(b"back")
    pattern = r"^(?P<card_number>\d+)\s*-\s*(?P<title>.+?)(?:-(?P<side>[12]))?$"

    apply_filename_metadata(
        tmp_path,
        game_id="test",
        scenario_slug="scenario",
        project_language="de",
        parser_pattern=pattern,
    )

    data = json.loads(
        (tmp_path / "test" / "de" / "scenario" / "translation_de.json").read_text()
    )
    card = next(item for item in data["cards"] if item["key"] == "004 - Back Name")
    assert card["text"]["en"]["sides"]["back"]["title"] == "Back Name"
    assert card["game_data"]["card_number"] == 4


def test_filename_parser_blocks_existing_id_conflict_without_writing(tmp_path):
    _project(tmp_path)
    save_filename_parser(
        tmp_path,
        game_id="test",
        pattern=r"^(?P<id>\d+)_(?P<title>.+)-(?P<side>[12])$",
        identifier_namespace="testdb",
    )
    translation_path = tmp_path / "test" / "de" / "scenario" / "translation_de.json"
    data = json.loads(translation_path.read_text())
    data["cards"].append(
        {"id": "import:003", "key": "import:003", "identifiers": {"testdb": "003"}}
    )
    translation_path.write_text(json.dumps(data))
    before = translation_path.read_bytes()

    preview = preview_filename_metadata(
        tmp_path, game_id="test", scenario_slug="scenario", project_language="de"
    )

    assert preview["conflicts"] == 1
    assert preview["rows"][0]["conflicts"] == ["ID already belongs to import:003"]
    with pytest.raises(ValueError, match="contains conflicts"):
        apply_filename_metadata(
            tmp_path, game_id="test", scenario_slug="scenario", project_language="de"
        )
    assert translation_path.read_bytes() == before


def test_reimport_matches_external_id_without_duplicating_record(tmp_path):
    _project(tmp_path)
    first = json.dumps({"cards": [{"IDENTIFIKATOR": "001", "Name": "Miskatonic Library"}]}).encode()
    second = json.dumps(
        {"cards": [{"IDENTIFIKATOR": "001", "Name": "Renamed", "Text": "New"}]}
    ).encode()
    arguments = {
        "root": tmp_path,
        "game_id": "test",
        "scenario_slug": "scenario",
        "project_language": "de",
        "source_id": "test-json",
        "filename": "source.json",
    }
    import_card_data(content=first, **arguments)
    translation_path = tmp_path / "test" / "de" / "scenario" / "translation_de.json"
    translation = json.loads(translation_path.read_text())
    translation["cards"][0]["text"]["de"] = {
        "sides": {"front": {"title": "Miskatonic-Bibliothek", "rules": "Übersetzt"}}
    }
    translation["cards"][0]["text"]["en"]["sides"]["front"]["editor_note"] = "keep"
    translation_path.write_text(json.dumps(translation))
    result = import_card_data(content=second, **arguments)

    assert result["matched_by_id"] == 1
    translation = json.loads(translation_path.read_text())
    assert len(translation["cards"]) == 1
    assert translation["cards"][0]["key"] == "003_Miskatonic Library"
    assert translation["cards"][0]["text"]["en"]["sides"]["front"]["title"] == "Renamed"
    assert translation["cards"][0]["text"]["en"]["sides"]["front"]["editor_note"] == "keep"
    assert translation["cards"][0]["text"]["de"]["sides"]["front"]["rules"] == "Übersetzt"


def test_import_rejects_duplicate_external_ids_without_writing(tmp_path):
    _project(tmp_path)
    translation_path = tmp_path / "test" / "de" / "scenario" / "translation_de.json"
    before = translation_path.read_bytes()
    payload = json.dumps(
        {
            "cards": [
                {"IDENTIFIKATOR": "001", "Name": "One"},
                {"IDENTIFIKATOR": "001", "Name": "Two"},
            ]
        }
    ).encode()

    with pytest.raises(ValueError, match="Duplicate external ID"):
        import_card_data(
            tmp_path,
            game_id="test",
            scenario_slug="scenario",
            project_language="de",
            source_id="test-json",
            filename="source.json",
            content=payload,
        )
    assert translation_path.read_bytes() == before


def test_manually_assigns_unmatched_import_and_persists_id_match(tmp_path):
    _project(tmp_path)
    payload = json.dumps(
        {"cards": [{"IDENTIFIKATOR": "007", "Name": "A Different Database Name"}]}
    ).encode()
    import_card_data(
        tmp_path,
        game_id="test",
        scenario_slug="scenario",
        project_language="de",
        source_id="test-json",
        filename="source.json",
        content=payload,
    )
    scenario = get_scenario(tmp_path, "test", "scenario", language="de")
    candidates = unmatched_import_records(scenario)
    assert candidates == [
        {
            "record_id": "testdb:007",
            "title": "A Different Database Name",
            "external_id": "007",
            "provider": "test-json",
            "suggested_card_key": "",
            "suggestion_percent": "",
        }
    ]

    match_import_record(
        tmp_path,
        game_id="test",
        scenario_slug="scenario",
        project_language="de",
        import_record_id="testdb:007",
        card_key="003_Miskatonic Library",
    )

    scenario = get_scenario(tmp_path, "test", "scenario", language="de")
    assert unmatched_import_records(scenario) == []
    card = scenario["grouped_cards"][0]
    assert card["matched"] is True
    assert card["identifiers"] == {"testdb": "007"}


def test_sets_external_id_before_import_and_reimport_uses_it(tmp_path):
    _project(tmp_path)
    set_card_identifier(
        tmp_path,
        game_id="test",
        scenario_slug="scenario",
        project_language="de",
        card_key="003_Miskatonic Library",
        namespace="testdb",
        external_id="099",
    )

    payload = json.dumps(
        {"cards": [{"IDENTIFIKATOR": "099", "Name": "Completely Different Name"}]}
    ).encode()
    result = import_card_data(
        tmp_path,
        game_id="test",
        scenario_slug="scenario",
        project_language="de",
        source_id="test-json",
        filename="source.json",
        content=payload,
    )

    assert result["matched_by_id"] == 1
    translation = json.loads(
        (tmp_path / "test" / "de" / "scenario" / "translation_de.json").read_text()
    )
    assert len(translation["cards"]) == 1
    assert translation["cards"][0]["key"] == "003_Miskatonic Library"


def test_unmatched_record_gets_non_binding_fuzzy_suggestion(tmp_path):
    _project(tmp_path)
    payload = json.dumps(
        {"cards": [{"IDENTIFIKATOR": "008", "Name": "Miskatonic Librery"}]}
    ).encode()
    import_card_data(
        tmp_path,
        game_id="test",
        scenario_slug="scenario",
        project_language="de",
        source_id="test-json",
        filename="source.json",
        content=payload,
    )

    scenario = get_scenario(tmp_path, "test", "scenario", language="de")
    candidate = unmatched_import_records(scenario)[0]
    assert candidate["suggested_card_key"] == "003_Miskatonic Library"
    assert int(candidate["suggestion_percent"]) >= 90
    assert scenario["grouped_cards"][0]["matched"] is False
