from __future__ import annotations

import json
from pathlib import Path

import pytest

from card_translator.games import (
    delete_json_import_source,
    discover_games,
    game_definition_issues,
    get_game,
    save_filename_parser,
    save_game,
    save_json_import_source,
    save_translatable_fields,
)


def test_builtin_games_are_available_for_existing_libraries(tmp_path):
    games = discover_games(tmp_path)
    assert [game["id"] for game in games] == ["ah", "lotr"]


def test_arkham_definition_models_database_id_as_identifier():
    game = get_game(Path("library"), "ah")

    assert game is not None
    assert "id" not in {
        field["id"] for field in game["card_schema"]["translatable_fields"]
    }
    source = next(item for item in game["imports"] if item["id"] == "arkham-json")
    assert source["identifier"] == "code"
    assert source["identifier_namespace"] == "arkham-json"
    assert source["fields"]["game_data.artist"] == "illustrator"
    assert source["fields"]["game_data.project_number"] == "position"
    assert source["fields"]["game_data.encounter_number"] == "encounter_position"


def test_arkham_card_types_share_one_field_namespace():
    game = get_game(Path("library"), "ah")

    assert game is not None
    types = {item["id"]: set(item["fields"]) for item in game["card_schema"]["card_types"]}
    assert "title" in types["location"]
    assert "title" in types["weakness"]
    assert {"title", "subname", "rules"} <= types["chaos"]
    assert {"title", "encounter_set", "printed_quantity"} <= types["mini_investigator"]
    assert {"investigator", "mini_investigator"} <= types.keys()
    assert {
        "act_agenda_full", "concealed", "customizable", "enemy_deck",
        "fullart_location", "fullart_scanning", "chapter_2/enemy", "ultimatum",
    } <= types.keys()
    assert "shroud" in types["location"]
    assert "index" in types["act"]
    assert "index" in types["agenda"]
    assert all(
        "index" not in fields
        for type_id, fields in types.items()
        if type_id not in {"act", "agenda", "act_agenda_full"}
    )
    assert {"connection", "connections"} <= types["location"]
    assert all(
        "clues_per_investigator" not in fields
        for fields in types.values()
        if "clues" in fields
    )
    metadata = {
        field["id"]: field for field in game["card_schema"]["metadata_fields"]
    }
    assert metadata["clues"]["value_format"] == "scaled_number"
    assert "clues_per_investigator" not in metadata
    assert "classes" not in types["location"]
    assert all("artist" in fields for fields in types.values())
    assert "shroud" not in types["weakness"]
    field_ids = [field["id"] for field in game["card_schema"]["translatable_fields"]]
    assert field_ids.count("title") == 1


def test_arkham_location_symbols_match_bundled_colored_icons():
    game = get_game(Path("library"), "ah")
    fields = {
        field["id"]: field
        for field in game["card_schema"]["metadata_fields"]
    }
    symbols = set(fields["connection"]["options"])

    assert {"quote", "slash", "spade"} <= symbols
    assert fields["connections"]["options"] == fields["connection"]["options"]
    icon_dir = Path(__file__).resolve().parents[1] / "src/card_translator/web/static/location_symbols"
    assert {path.stem.removeprefix("connection_") for path in icon_dir.glob("*.svg")} == symbols


def test_saved_game_definition_is_discovered_and_overrides_builtin(tmp_path):
    saved = save_game(
        tmp_path,
        game_id="ah",
        name="Arkham Horror LCG",
        description="Investigators versus the mythos.",
        default_source_language="EN",
    )

    assert saved["default_source_language"] == "en"
    assert get_game(tmp_path, "ah")["name"] == "Arkham Horror LCG"
    raw = json.loads((tmp_path / "ah" / "game.json").read_text())
    assert raw["format"] == "card-translator-game"
    assert raw["card_schema"]["translatable_fields"][0]["id"] == "title"


def test_new_game_becomes_a_library_game(tmp_path):
    save_game(tmp_path, game_id="earthborne", name="Earthborne Rangers")
    assert [game["id"] for game in discover_games(tmp_path)] == ["ah", "earthborne", "lotr"]


def test_game_id_is_safe_for_use_as_a_directory(tmp_path):
    with pytest.raises(ValueError):
        save_game(tmp_path, game_id="../oops", name="Oops")


def test_invalid_game_definition_is_reported_instead_of_loaded(tmp_path):
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "game.json").write_text('{"format": "card-translator-game", bad json')

    assert "broken" not in [game["id"] for game in discover_games(tmp_path)]
    assert game_definition_issues(tmp_path) == [
        {
            "path": "broken/game.json",
            "error": "Invalid JSON at line 1, column 36",
        }
    ]


def test_invalid_nested_game_schema_is_reported(tmp_path):
    broken = tmp_path / "broken"
    broken.mkdir()
    definition = {
        "format": "card-translator-game",
        "format_version": 1,
        "id": "broken",
        "name": "Broken",
        "default_source_language": "en",
        "card_schema": {
            "translatable_fields": [
                {"id": "rules", "label": "Rules", "type": "unknown"}
            ],
            "metadata_fields": [],
        },
        "imports": [],
    }
    (broken / "game.json").write_text(json.dumps(definition))

    assert "broken" not in [game["id"] for game in discover_games(tmp_path)]
    assert game_definition_issues(tmp_path)[0]["error"] == (
        "translatable_fields[0].type is invalid"
    )


def test_save_game_rejects_invalid_source_language_before_writing(tmp_path):
    with pytest.raises(ValueError, match="default_source_language"):
        save_game(
            tmp_path,
            game_id="test",
            name="Test",
            default_source_language="English",
        )
    assert not (tmp_path / "test" / "game.json").exists()


def test_saves_declarative_json_field_mapping(tmp_path):
    save_game(tmp_path, game_id="test", name="Test")
    save_json_import_source(
        tmp_path,
        game_id="test",
        source_id="cards-json",
        label="Cards JSON",
        records_path="data.cards",
        identifier_namespace="example",
        identifier="IDENTIFIKATOR",
        fields={"title": "Name", "rules": "Text", "flavor": ""},
    )

    source = get_game(tmp_path, "test")["imports"][0]
    assert source["records_path"] == "data.cards"
    assert source["identifier"] == "IDENTIFIKATOR"
    assert source["fields"] == {"title": "Name", "rules": "Text"}

    delete_json_import_source(tmp_path, game_id="test", source_id="cards-json")
    assert get_game(tmp_path, "test")["imports"] == []


def test_saves_translatable_card_schema(tmp_path):
    save_game(tmp_path, game_id="test", name="Test")
    fields = save_translatable_fields(
        tmp_path,
        game_id="test",
        fields_text="title | Card name | text\nability | Ability | multiline\ntags | Tags | list",
        metadata_fields_text="card_type | Card type | text\ncard_number | Number | integer",
    )

    assert [field["id"] for field in fields] == ["title", "ability", "tags"]
    assert get_game(tmp_path, "test")["card_schema"]["translatable_fields"] == fields
    assert get_game(tmp_path, "test")["card_schema"]["metadata_fields"] == [
        {"id": "card_type", "label": "Card type", "type": "text"},
        {"id": "card_number", "label": "Number", "type": "integer"},
    ]


def test_saves_and_validates_filename_parser(tmp_path):
    save_game(tmp_path, game_id="test", name="Test")
    save_json_import_source(
        tmp_path,
        game_id="test",
        source_id="cards-json",
        label="Cards",
        records_path="cards",
        identifier_namespace="testdb",
        identifier="id",
        fields={"title": "name"},
    )

    parser = save_filename_parser(
        tmp_path,
        game_id="test",
        pattern=r"^(?P<id>\d+)_(?P<title>.+)-(?P<side>[12])$",
        identifier_namespace="testdb",
    )

    assert parser["identifier_namespace"] == "testdb"
    with pytest.raises(ValueError, match="named id or title"):
        save_filename_parser(
            tmp_path,
            game_id="test",
            pattern=r"^(\d+)$",
            identifier_namespace="testdb",
        )
