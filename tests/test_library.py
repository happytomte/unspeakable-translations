from __future__ import annotations

import json

import pytest

from card_translator.library import (
    _validate_card_image_batch_result,
    apply_card_image_batch_results,
    apply_card_ocr_text,
    apply_card_source_proposal,
    apply_gemini_card_image,
    card_source_proposal,
    collapse_duplicate_cards,
    copy_scenario_to_language,
    delete_scenario,
    discover_library,
    format_arkham_rules_result,
    format_arkham_rules_text,
    game_global_translation_rules,
    game_translation_rules,
    gemini_card_image_prompt,
    get_scenario,
    initialize_scenario,
    prepare_card_image_batch,
    preview_card_image,
    update_card_game_fields,
    update_card_translation,
    update_game_system_config,
    update_game_translation_rules,
    update_image_metadata,
)


def _accept_source_proposal(root, *, game="ah", slug="scenario", language="de", card_key):
    proposal = card_source_proposal(
        root,
        game=game,
        slug=slug,
        project_language=language,
        card_key=card_key,
    )
    updates = [
        {
            "side": side["side"],
            "kind": field["kind"],
            "field": field["field"],
            "value": field["proposed"],
            "reviewed_hash": field["reviewed_hash"],
        }
        for side in proposal["sides"]
        for field in side["fields"]
    ]
    return apply_card_source_proposal(
        root,
        game=game,
        slug=slug,
        project_language=language,
        card_key=card_key,
        updates=updates,
    )


def test_discovering_empty_library_does_not_create_game_directories(tmp_path):
    games = discover_library(tmp_path)

    assert [game["slug"] for game in games] == ["ah", "lotr"]
    assert not (tmp_path / "ah").exists()
    assert not (tmp_path / "lotr").exists()


def test_rule_formatting_marks_labels_and_arkham_without_touching_prose():
    source = "Forced – Place a clue in Arkham.\nObjective – Advance.\nForced appears here."
    target = "Erzwungen – Lege einen Hinweis in Arkham.\nZiel – Rücke vor."
    pasted = {
        "source": {"rules": source, "flavor": "Arkham in flavor"},
        "translation": {"rules": target},
        "metadata": {"card_type": "act"},
    }
    formatted = format_arkham_rules_result(pasted)
    assert formatted["source"]["rules"] == (
        "<b>Forced</b> – Place a clue in <bi>Arkham</bi>.\n"
        "<b>Objective</b> – Advance.\nForced appears here."
    )
    assert formatted["translation"]["rules"] == (
        "<b>Erzwungen</b> – Lege einen Hinweis in <bi>Arkham</bi>.\n"
        "<b>Ziel</b> – Rücke vor."
    )
    assert formatted["source"]["flavor"] == "Arkham in flavor"
    assert formatted["metadata"] == pasted["metadata"]
    assert pasted["source"]["rules"] == source
    assert format_arkham_rules_result(formatted) == formatted


def test_rule_formatting_preserves_existing_markup_and_requires_a_label_separator():
    rules = "<b>Forced</b> – Visit <bi>Arkham</bi>.\nThe Objective is clear.\nRevelation: Go to Arkham."
    assert format_arkham_rules_text(rules) == (
        "<b>Forced</b> – Visit <bi>Arkham</bi>.\nThe Objective is clear.\n"
        "<b>Revelation</b>: Go to <bi>Arkham</bi>."
    )


def test_rule_formatting_bolds_only_unformatted_resolution_ids():
    rules = "({resolution}R2) ({resolution}R10) ({resolution}<b>R1</b>) (R3)"
    formatted = format_arkham_rules_text(rules)
    assert formatted == (
        "({resolution}<b>R2</b>) ({resolution}<b>R10</b>) "
        "({resolution}<b>R1</b>) (R3)"
    )
    assert format_arkham_rules_text(formatted) == formatted


def test_rule_formatting_normalizes_diamond_bullets():
    rules = "◆ Place 1 doom.\n  ◇ Take 1 damage.\n◊Move."

    assert format_arkham_rules_text(rules) == (
        "{bullet} Place 1 doom.\n"
        "  {bullet} Take 1 damage.\n"
        "{bullet} Move."
    )


def test_card_image_preview_records_selected_provider(tmp_path, monkeypatch):
    cards = tmp_path / "ah" / "de" / "scenario" / "cards"
    cards.mkdir(parents=True)
    (cards / "001_Card-1.jpg").write_bytes(b"image")
    initialize_scenario(
        tmp_path, game="ah", slug="scenario", title="Scenario", translated_title="",
        author="", version="", source_url="", target_language="de",
        project_language="de", card_source="cards",
    )
    captured = {}

    def analyze(path, *, prompt, schema, provider):
        captured.update(path=path, prompt=prompt, schema=schema, provider=provider)
        return {
            "source": {"title": "Card"},
            "translation": {"title": "Karte"},
            "metadata": {},
            "_card_image_trace": {"raw_response": "{}", "usage": {}},
        }

    monkeypatch.setattr("card_translator.library.analyze_card_image", analyze)
    monkeypatch.setattr("card_translator.library.provider_model", lambda provider: "model-x")
    preview = preview_card_image(
        tmp_path, game="ah", slug="scenario", project_language="de",
        card_key="001_Card", side="front", provider="anthropic",
    )
    assert captured["provider"] == "anthropic"
    assert captured["path"].name == "001_Card-1.jpg"
    assert preview["model"] == "model-x"
    assert preview["provider"] == "anthropic"
    log = json.loads((tmp_path / "ah" / "de" / "scenario" / "logs" / "translation.jsonl").read_text().splitlines()[-1])
    assert log["provider"] == "anthropic"


def test_applies_ocr_to_side_specific_artist_metadata(tmp_path):
    cards = tmp_path / "ah" / "de" / "scenario" / "cards"
    cards.mkdir(parents=True)
    (cards / "001 - Card-1.jpg").write_bytes(b"front")
    (cards / "001 - Card-2.jpg").write_bytes(b"back")
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

    result = apply_card_ocr_text(
        tmp_path,
        game="ah",
        slug="scenario",
        project_language="de",
        card_key="001 - Card",
        image_name="001 - Card-2.jpg",
        language="en",
        side="back",
        field="artist",
        value="Erik Johansson",
        raw_text="Illus. Erik Johansson",
        confidence=0.91,
        x=0.1,
        y=0.8,
        width=0.5,
        height=0.1,
        rotation=0,
    )

    assert result["value"] == "Erik Johansson"
    scenario = get_scenario(tmp_path, "ah", "scenario", language="de")
    assert scenario["grouped_cards"][0]["game_data"] == {}
    proposal = card_source_proposal(
        tmp_path, game="ah", slug="scenario", project_language="de", card_key="001 - Card"
    )
    artist = proposal["sides"][0]["fields"][0]
    assert artist["field"] == "artist"
    assert artist["proposed"] == "Erik Johansson"
    assert artist["origin"] == "ocr"


def test_discovers_uninitialized_scenario(tmp_path):
    cards = tmp_path / "ah" / "my-scenario" / "cards"
    cards.mkdir(parents=True)
    (cards / "001.jpg").write_bytes(b"not-a-real-jpeg")

    games = discover_library(tmp_path)
    ah = next(game for game in games if game["slug"] == "ah")
    scenario = ah["scenarios"][0]

    assert scenario["slug"] == "my-scenario"
    assert scenario["initialized"] is False
    assert scenario["card_count"] == 1
    assert scenario["card_source"] == "cards"


def test_game_data_directory_is_not_discovered_as_scenario(tmp_path):
    data_directory = tmp_path / "ah" / "data"
    data_directory.mkdir(parents=True)
    (data_directory / "arkham.en.json").write_text("[]", encoding="utf-8")

    ah = next(game for game in discover_library(tmp_path) if game["slug"] == "ah")

    assert ah["scenarios"] == []


def test_copies_project_to_new_language_with_source_data_but_without_translations(tmp_path):
    cards = tmp_path / "ah" / "de" / "scenario" / "cards"
    cards.mkdir(parents=True)
    (cards / "001-front.png").write_bytes(b"image")
    initialize_scenario(
        tmp_path,
        game="ah",
        slug="scenario",
        title="Scenario",
        translated_title="Szenario",
        author="",
        version="",
        source_url="",
        target_language="de",
        project_language="de",
        card_source="cards",
    )
    source_path = tmp_path / "ah" / "de" / "scenario" / "translation_de.json"
    data = json.loads(source_path.read_text())
    data["cards"] = [
        {
            "key": "001",
            "card_type": "act",
            "game_data": {"card_number": 1},
            "identifiers": {"arkham-json": "12345"},
            "text": {
                "en": {"sides": {"front": {"title": "Source title"}}},
                "de": {"sides": {"front": {"title": "Deutscher Titel"}}},
            },
        }
    ]
    source_path.write_text(json.dumps(data))

    copy_scenario_to_language(
        tmp_path,
        game="ah",
        slug="scenario",
        project_language="de",
        target_language="fr",
    )

    copied = json.loads(
        (tmp_path / "ah" / "fr" / "scenario" / "translation_fr.json").read_text()
    )
    assert copied["target_language"] == "fr"
    assert copied["cards"][0]["text"] == {
        "en": {"sides": {"front": {"title": "Source title"}}}
    }
    assert copied["cards"][0]["game_data"] == {"card_number": 1}
    assert copied["cards"][0]["identifiers"] == {"arkham-json": "12345"}
    assert json.loads(source_path.read_text())["cards"][0]["text"]["de"]


def test_legacy_language_glossary_overrides_global_rules(tmp_path):
    update_game_translation_rules(
        tmp_path,
        game="ah",
        language="de",
        rules_text="Doom => Verderben\nAction => Aktion",
    )
    update_game_translation_rules(
        tmp_path,
        game="ah",
        language="fr",
        rules_text="Doom => Fatalité",
    )

    de_text = game_translation_rules(tmp_path, game="ah", language="de")["text"]
    fr_text = game_translation_rules(tmp_path, game="ah", language="fr")["text"]
    assert "Doom => Verderben" in de_text
    assert "Action => Aktion" in de_text
    assert "Doom => Fatalité" in fr_text
    assert "[clue] => {clue}" in de_text


def test_arkham_default_glossary_is_available_until_language_is_saved(tmp_path):
    rules = game_translation_rules(tmp_path, game="ah", language="fr")

    assert "[clue] => {clue}" in rules["text"]
    assert "[willpower] => {willpower}" in rules["text"]

    update_game_translation_rules(
        tmp_path,
        game="ah",
        language="fr",
        rules_text="[clue] => [clue]",
    )
    translated = game_translation_rules(tmp_path, game="ah", language="fr")["text"]
    assert "[clue] => [clue]" in translated
    assert "[willpower] => {willpower}" in translated


def test_game_system_config_is_global_and_persisted(tmp_path):
    result = update_game_system_config(
        tmp_path,
        game="ah",
        translation_context="Use concise rules language.",
        glossary_text="[clue] => {clue}",
        arkhamdb_urls_text="en | https://arkhamdb.com/api/public/cards/?encounter=1",
    )

    stored = json.loads((tmp_path / "ah" / "game.json").read_text(encoding="utf-8"))
    assert stored["translation_context"] == "Use concise rules language."
    assert stored["arkhamdb_urls"]["en"].startswith("https://arkhamdb.com/")
    assert result["rules"][0].source == "[clue]"
    assert game_global_translation_rules(tmp_path, game="ah")["text"] == "[clue] => {clue}"
    assert "[clue] => {clue}" in game_translation_rules(
        tmp_path, game="ah", language="it"
    )["text"]


def test_updates_schema_defined_card_metadata(tmp_path):
    cards = tmp_path / "ah" / "de" / "scenario" / "cards"
    cards.mkdir(parents=True)
    (cards / "001_Card-1.jpg").write_bytes(b"x")
    (cards / "001_Card-2.jpg").write_bytes(b"x")
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

    update_card_game_fields(
        tmp_path,
        game="ah",
        slug="scenario",
        project_language="de",
        card_key="001_Card",
        field_values={"card_type": "Location", "card_number": "17"},
    )
    update_card_game_fields(
        tmp_path,
        game="ah",
        slug="scenario",
        project_language="de",
        card_key="001_Card",
        side="back",
        field_values={"connection": "square", "connections": "circle, diamond"},
    )
    scenario = next(
        scenario
        for game in discover_library(tmp_path)
        if game["slug"] == "ah"
        for scenario in game["scenarios"]
    )
    fields = {field["id"]: field["value"] for field in scenario["grouped_cards"][0]["metadata_fields"]}
    assert fields["card_type"] == "Location"
    assert fields["encounter_set"] is None
    assert fields["card_number"] == "17"
    assert "shroud" in fields
    back_fields = {
        field["id"]: field["value"]
        for field in scenario["grouped_cards"][0]["metadata_fields_by_side"]["back"]
    }
    assert back_fields["connection"] == "square"
    assert back_fields["connections"] == ["circle", "diamond"]
    assert back_fields["card_number"] is None

    update_card_game_fields(
        tmp_path,
        game="ah",
        slug="scenario",
        project_language="de",
        card_key="001_Card",
        field_values={"clues": "1{per_investigator}"},
    )
    updated = get_scenario(tmp_path, "ah", "scenario", language="de")
    updated_fields = {
        field["id"]: field["value"]
        for field in updated["grouped_cards"][0]["metadata_fields_by_side"]["front"]
    }
    assert updated_fields["clues"] == "1<per>"
    assert "clues_per_investigator" not in updated_fields

    # Projects saved by older versions keep the multiplier in a second field.
    # The editor still presents those values in the new compact form.
    translation_path = tmp_path / "ah" / "de" / "scenario" / "translation_de.json"
    legacy_data = json.loads(translation_path.read_text(encoding="utf-8"))
    legacy_data["cards"][0]["game_data"]["clues"] = 2
    legacy_data["cards"][0]["game_data"]["clues_per_investigator"] = "true"
    translation_path.write_text(
        json.dumps(legacy_data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    legacy = get_scenario(tmp_path, "ah", "scenario", language="de")
    legacy_fields = {
        field["id"]: field["value"]
        for field in legacy["grouped_cards"][0]["metadata_fields_by_side"]["front"]
    }
    assert legacy_fields["clues"] == "2<per>"

    update_card_game_fields(
        tmp_path,
        game="ah",
        slug="scenario",
        project_language="de",
        card_key="001_Card",
        field_values={
            "card_number": "105a",
            "health": "x{per_investigator}",
            "fight": "5<per>",
            "evade": "X",
        },
    )
    scaled = get_scenario(tmp_path, "ah", "scenario", language="de")
    scaled_fields = {
        field["id"]: field["value"]
        for field in scaled["grouped_cards"][0]["metadata_fields_by_side"]["front"]
    }
    assert scaled_fields["card_number"] == "105a"
    assert scaled_fields["health"] == "X<per>"
    assert scaled_fields["fight"] == "5<per>"
    assert scaled_fields["evade"] == "X"

    with pytest.raises(ValueError, match=r"health must be X or a whole number"):
        update_card_game_fields(
            tmp_path,
            game="ah",
            slug="scenario",
            project_language="de",
            card_key="001_Card",
            field_values={"health": "five<per>"},
        )


def test_card_image_import_normalizes_unique_title_markup(tmp_path):
    cards = tmp_path / "ah" / "de" / "scenario" / "cards"
    cards.mkdir(parents=True)
    (cards / "001_Unique-1.png").write_bytes(b"front")
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

    apply_gemini_card_image(
        tmp_path,
        game="ah",
        slug="scenario",
        project_language="de",
        card_key="001_Unique",
        side="front",
        result={
            "source": {"title": "<unique> Dr. Henry Armitage"},
            "translation": {"title": "{unique} Dr. Henry Armitage"},
            "metadata": {},
        },
    )

    scenario = get_scenario(tmp_path, "ah", "scenario", language="de")
    card = scenario["grouped_cards"][0]
    assert "front" not in card["source_sides"]
    assert card["target_sides"]["front"]["title"] == "{unique}Dr. Henry Armitage"
    proposal = card_source_proposal(
        tmp_path, game="ah", slug="scenario", project_language="de", card_key="001_Unique"
    )
    assert proposal["sides"][0]["fields"][0]["proposed"] == "{unique}Dr. Henry Armitage"
    _accept_source_proposal(tmp_path, card_key="001_Unique")
    reviewed = get_scenario(tmp_path, "ah", "scenario", language="de")["grouped_cards"][0]
    assert reviewed["source_sides"]["front"]["title"] == "{unique}Dr. Henry Armitage"
    assert reviewed["target_side_review"]["front"]["title"].get("review_required") is not True


def test_gemini_prompt_detects_missing_card_type_and_requests_type_fields(tmp_path):
    cards = tmp_path / "ah" / "de" / "scenario" / "cards"
    cards.mkdir(parents=True)
    (cards / "001_Card-1.jpg").write_bytes(b"x")
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

    result = gemini_card_image_prompt(
        tmp_path,
        game="ah",
        slug="scenario",
        project_language="de",
        card_key="001_Card",
        side="front",
    )

    assert '"card_type": ""' in result["prompt"]
    assert "metadata.card_type to exactly one of these canonical IDs" in result["prompt"]
    assert "act, agenda, act_agenda_full, asset" in result["prompt"]
    assert '"shroud": ""' in result["prompt"]
    assert '"clues": ""' in result["prompt"]
    assert "metadata.connection is the location's own single glyph" in result["prompt"]
    assert "circle, clover, cross, diamond" in result["prompt"]
    assert "not chaos tokens" in result["prompt"]
    assert "Escape every straight double quote inside a JSON string" in result["prompt"]
    assert result["prompt"].startswith("Before transcribing, inspect each narrative paragraph")
    assert "Title, subname, and traits must be plain text" in result["prompt"]
    assert "metadata.clues uses a numeric value optionally followed by <per>" in result["prompt"]
    assert "a lightning bolt is {fast}" in result["prompt"]
    assert "not '1 {damage} damage'" in result["prompt"]
    assert "diamond-shaped list marker before a rules effect is the {bullet} icon" in result["prompt"]
    assert "never as a Unicode diamond such as ◆, ◇, ◊, or ◈" in result["prompt"]
    assert "Location connections belong in metadata.connection" in result["prompt"]
    assert "Following the rabbit" not in result["prompt"]
    assert "Source language: en." in result["prompt"]
    assert "Target language: de." in result["prompt"]
    assert (
        "Write every value in the translation object exclusively in that target language."
        in result["prompt"]
    )
    assert "If a standalone dash is printed instead of a clue number" in result["prompt"]
    assert "For every leading structural rules label" in result["prompt"]


def test_prepares_llm_batch_for_visible_card_sides(tmp_path):
    cards = tmp_path / "ah" / "de" / "scenario" / "cards"
    cards.mkdir(parents=True)
    (cards / "location_library-front.png").write_bytes(b"front")
    (cards / "location_library-back.png").write_bytes(b"back")
    (cards / "hidden_card-front.png").write_bytes(b"hidden")
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
    update_image_metadata(
        tmp_path,
        game="ah",
        slug="scenario",
        project_language="de",
        image_name="hidden_card-front.png",
        hidden=True,
    )
    batch = tmp_path / "ah" / "de" / "scenario" / "llm-batch" / "de"
    batch.mkdir(parents=True)
    (batch / "removed-card.job.json").write_text("{}", encoding="utf-8")
    (batch / "legacy-card.txt").write_text("stale", encoding="utf-8")

    result = prepare_card_image_batch(
        tmp_path,
        game="ah",
        slug="scenario",
        project_language="de",
    )

    assert result == {
        "count": 2,
        "text_only": 0,
        "image_jobs": 2,
        "skipped_hidden": 1,
        "path": "llm-batch/de",
        "manifest": "llm-batch/de/manifest.json",
    }
    assert (batch / "location_library-front.job.json").is_file()
    assert (batch / "location_library-back.job.json").is_file()
    assert not (batch / "hidden_card-front.job.json").exists()
    assert not (batch / "removed-card.job.json").exists()
    assert not (batch / "legacy-card.txt").exists()
    front_job = json.loads((batch / "location_library-front.job.json").read_text())
    back_job = json.loads((batch / "location_library-back.job.json").read_text())
    assert front_job["format"] == "card-translator-llm-job"
    assert front_job["format_version"] == 2
    assert front_job["card_key"] == "location_library"
    assert front_job["side"] == "front"
    assert front_job["image"] == "cards/location_library-front.png"
    assert front_job["context"] == "context.json"
    assert front_job["result"] == "location_library-front.result.json"
    assert front_job["references"] == {}
    assert front_job["output_schema"]["required"] == ["source", "translation", "metadata"]
    source_schema = front_job["output_schema"]["properties"]["source"]["properties"]
    metadata_schema = front_job["output_schema"]["properties"]["metadata"]["properties"]
    assert front_job["output_schema"]["properties"]["source"]["required"] == list(source_schema)
    assert front_job["output_schema"]["properties"]["metadata"]["required"] == list(metadata_schema)
    assert front_job["output_schema"]["additionalProperties"] is False
    assert "no angle-bracket tags" in source_schema["title"]["description"]
    assert "May use only" in source_schema["rules"]["description"]
    assert "1<per>" in metadata_schema["clues"]["description"]
    assert "clues_per_investigator" not in metadata_schema
    assert "Mandatory glossary" not in front_job["prompt"]
    assert "Return only JSON" in front_job["prompt"]
    assert "Inspect the actual card image carefully" in front_job["prompt"]
    assert "small-print footer separately" in front_job["prompt"]
    assert "rules" in back_job["output_schema"]["properties"]["source"]["properties"]
    assert "flavor" in back_job["output_schema"]["properties"]["source"]["properties"]
    assert "stage" in back_job["output_schema"]["properties"]["metadata"]["properties"]
    assert "card_type" not in back_job["output_schema"]["properties"]["metadata"]["properties"]
    context = json.loads((batch / "context.json").read_text())
    assert context["format"] == "card-translator-llm-context"
    assert "Mandatory glossary" in context["prompt"]
    assert "small-print footer" in context["prompt"]
    manifest = json.loads((batch / "manifest.json").read_text())
    assert manifest["format_version"] == 2
    assert manifest["target_language"] == "de"
    assert manifest["context"] == "context.json"
    assert manifest["jobs"] == [
        {
            "card_key": "location_library",
            "side": "front",
            "image": "cards/location_library-front.png",
            "job": "location_library-front.job.json",
        },
        {
            "card_key": "location_library",
            "side": "back",
            "image": "cards/location_library-back.png",
            "job": "location_library-back.job.json",
        },
    ]


def test_batch_validator_honors_nested_required_fields():
    legacy_schema = {
        "type": "object",
        "properties": {
            "metadata": {
                "type": "object",
                "properties": {
                    "artist": {"type": "string"},
                    "card_number": {"type": "string"},
                },
            }
        },
        "required": ["metadata"],
    }

    _validate_card_image_batch_result(
        {"metadata": {}}, legacy_schema, "legacy.result.json"
    )

    strict_schema = json.loads(json.dumps(legacy_schema))
    strict_schema["properties"]["metadata"]["required"] = [
        "artist",
        "card_number",
    ]
    with pytest.raises(ValueError, match="missing artist, card_number"):
        _validate_card_image_batch_result(
            {"metadata": {}}, strict_schema, "strict.result.json"
        )


def test_llm_batch_selects_small_official_reference_set_locally(tmp_path):
    cards = tmp_path / "ah" / "de" / "scenario" / "cards"
    cards.mkdir(parents=True)
    (cards / "001_Card-1.png").write_bytes(b"front")
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
    apply_gemini_card_image(
        tmp_path,
        game="ah",
        slug="scenario",
        project_language="de",
        card_key="001_Card",
        side="front",
        result={
            "source": {
                "title": "A Test",
                "traits": "Hazard.",
                "rules": "<b>Revelation</b> — Test {willpower} (3). For each point you fail by, take 1 horror.",
            },
            "translation": {},
            "metadata": {"card_type": "treachery"},
        },
    )
    _accept_source_proposal(tmp_path, card_key="001_Card")
    data = tmp_path / "ah" / "data"
    data.mkdir(parents=True)
    (data / "arkham.references.json").write_text(
        json.dumps(
            {
                "format": "card-translator-arkham-references",
                "format_version": 1,
                "source_language": "en",
                "languages": {
                    "de": [
                        {
                            "source": "<b>Revelation</b> - Test [willpower] (3). For each point you fail by, take 1 horror.",
                            "target": "<b>Enthüllung</b> - Lege eine [willpower]-Probe (3) ab. Nimm 1 Horror.",
                            "card_name": "Official Example",
                            "card_type": "Treachery",
                            "field": "rules",
                        },
                        {
                            "source": "Unrelated text without matching words.",
                            "target": "Nicht verwandter Text.",
                            "card_name": "Unrelated Example",
                            "card_type": "Asset",
                            "field": "rules",
                        },
                        {
                            "source": "Hazard",
                            "target": "Gefah",
                            "card_name": "Inexact Trait",
                            "card_type": "Treachery",
                            "field": "traits",
                        },
                        {
                            "source": "Hazard.",
                            "target": "Gefahr.",
                            "card_name": "Exact Trait",
                            "card_type": "Treachery",
                            "field": "traits",
                        },
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    prepare_card_image_batch(
        tmp_path,
        game="ah",
        slug="scenario",
        project_language="de",
    )

    job_path = (
        tmp_path
        / "ah"
        / "de"
        / "scenario"
        / "llm-batch"
        / "de"
        / "001_Card-1.job.json"
    )
    job = json.loads(job_path.read_text())
    assert [reference["card_name"] for reference in job["references"]["rules"]] == [
        "Official Example"
    ]
    assert "{willpower}" in job["references"]["rules"][0]["source"]
    assert "[willpower]" not in job["references"]["rules"][0]["target"]
    assert job["references"]["traits"] == [
        {
            "card_name": "Exact Trait",
            "card_type": "Treachery",
            "source": "Hazard.",
            "target": "Gefahr.",
        }
    ]


def test_applies_prepared_llm_batch_results_to_project(tmp_path):
    cards = tmp_path / "ah" / "de" / "scenario" / "cards"
    cards.mkdir(parents=True)
    (cards / "001_Card-1.png").write_bytes(b"front")
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
    prepare_card_image_batch(
        tmp_path,
        game="ah",
        slug="scenario",
        project_language="de",
    )
    batch = tmp_path / "ah" / "de" / "scenario" / "llm-batch" / "de"
    job_path = batch / "001_Card-1.job.json"
    job = json.loads(job_path.read_text())
    schema = job["output_schema"]
    result = {
        section: {field: "" for field in definition["properties"]}
        for section, definition in schema["properties"].items()
    }
    result["source"]["title"] = "Source Card"
    result["translation"]["title"] = "Übersetzte Karte"
    (batch / job["result"]).write_text(json.dumps(result), encoding="utf-8")

    imported = apply_card_image_batch_results(tmp_path, batch)

    assert imported["applied"] == 1
    assert imported["source_proposal_cards"] == ["001_Card"]
    scenario = get_scenario(tmp_path, "ah", "scenario", language="de")
    card = scenario["grouped_cards"][0]
    assert "front" not in card["source_sides"]
    assert card["target_sides"]["front"]["title"] == "Übersetzte Karte"
    proposal = card_source_proposal(
        tmp_path, game="ah", slug="scenario", project_language="de", card_key="001_Card"
    )
    assert next(
        field for side in proposal["sides"] for field in side["fields"]
        if field["field"] == "title"
    )["proposed"] == "Source Card"


def test_prepared_llm_batch_apply_error_identifies_card_and_side(tmp_path):
    cards = tmp_path / "ah" / "de" / "scenario" / "cards"
    cards.mkdir(parents=True)
    (cards / "001_Good-1.png").write_bytes(b"front")
    (cards / "002_Bad-1.png").write_bytes(b"front")
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
    prepare_card_image_batch(
        tmp_path,
        game="ah",
        slug="scenario",
        project_language="de",
    )
    batch = tmp_path / "ah" / "de" / "scenario" / "llm-batch" / "de"
    for stem in ("001_Good-1", "002_Bad-1"):
        job = json.loads((batch / f"{stem}.job.json").read_text())
        result = {
            section: {field: "" for field in definition["properties"]}
            for section, definition in job["output_schema"]["properties"].items()
        }
        result["source"]["title"] = "Good" if stem.startswith("001") else "Bad"
        result["metadata"]["card_type"] = "act"
        if stem.startswith("002"):
            result["metadata"]["stage"] = "not-a-number"
        (batch / job["result"]).write_text(json.dumps(result), encoding="utf-8")

    imported = apply_card_image_batch_results(tmp_path, batch)

    assert imported["applied"] == 1
    assert imported["failed"] == 1
    assert imported["errors"][0]["card_key"] == "002_Bad"
    assert imported["errors"][0]["side"] == "front"
    assert imported["errors"][0]["result_name"] == "002_Bad-1.result.json"
    assert imported["errors"][0]["error"] == "stage must be a whole number"
    assert '"stage": "not-a-number"' in imported["errors"][0]["result_json"]
    scenario = get_scenario(tmp_path, "ah", "scenario", language="de")
    good = next(card for card in scenario["grouped_cards"] if card["key"] == "001_Good")
    assert "front" not in good["source_sides"]
    proposal = card_source_proposal(
        tmp_path, game="ah", slug="scenario", project_language="de", card_key="001_Good"
    )
    assert next(
        field for side in proposal["sides"] for field in side["fields"]
        if field["field"] == "title"
    )["proposed"] == "Good"


def test_gemini_import_treats_printed_clue_dash_as_empty(tmp_path):
    cards = tmp_path / "ah" / "de" / "scenario" / "cards"
    cards.mkdir(parents=True)
    (cards / "001_Act-1.jpg").write_bytes(b"front")
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
    translation_path = tmp_path / "ah" / "de" / "scenario" / "translation_de.json"
    original = translation_path.read_bytes()
    result = {
        "source": {"title": "Through the Looking-Glass"},
        "translation": {"title": "Hinter den Spiegeln"},
        "metadata": {
            "card_type": "act",
            "stage": "3",
            "index": "3a",
            "clues": "-",
            "doom": "–",
            "printed_quantity": "—",
        },
    }

    invalid = {**result, "metadata": {**result["metadata"], "stage": "three"}}
    with pytest.raises(ValueError, match="stage must be a whole number"):
        apply_gemini_card_image(
            tmp_path, game="ah", slug="scenario", project_language="de",
            card_key="001_Act", side="front", result=invalid,
        )
    assert translation_path.read_bytes() == original

    invalid_markup = {
        **result,
        "source": {**result["source"], "title": "<b>Through the Looking-Glass</b>"},
    }
    with pytest.raises(ValueError, match="source.title must not contain"):
        apply_gemini_card_image(
            tmp_path,
            game="ah",
            slug="scenario",
            project_language="de",
            card_key="001_Act",
            side="front",
            result=invalid_markup,
        )
    assert translation_path.read_bytes() == original

    apply_gemini_card_image(
        tmp_path, game="ah", slug="scenario", project_language="de",
        card_key="001_Act", side="front", result=result,
    )
    _accept_source_proposal(tmp_path, card_key="001_Act")
    card = get_scenario(tmp_path, "ah", "scenario", language="de")["grouped_cards"][0]
    fields = {
        field["id"]: field["value"]
        for field in card["metadata_fields_by_side"]["front"]
    }
    assert fields["clues"] == ""
    assert fields["doom"] == ""
    assert fields["printed_quantity"] is None
    assert fields["stage"] == 3
    assert fields["index"] == "3a"
    assert card["target_sides"]["front"]["title"] == "Hinter den Spiegeln"


def test_gemini_markup_scope_allows_structural_per_only_for_clues(tmp_path):
    cards = tmp_path / "ah" / "de" / "scenario" / "cards"
    cards.mkdir(parents=True)
    (cards / "001_Location-1.jpg").write_bytes(b"front")
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
    apply_gemini_card_image(
        tmp_path,
        game="ah",
        slug="scenario",
        project_language="de",
        card_key="001_Location",
        side="front",
        result={
            "source": {
                "title": "A Location",
                "rules": "◆ <b>Forced</b> — Move.",
                "flavor": "<i>A quiet place.</i>",
            },
            "translation": {
                "title": "Ein Ort",
                "rules": "◇ <b>Erzwungen</b> — Bewege dich.",
                "flavor": "<i>Ein ruhiger Ort.</i>",
            },
            "metadata": {"card_type": "location", "clues": "1<per>"},
        },
    )
    _accept_source_proposal(tmp_path, card_key="001_Location")
    card = get_scenario(tmp_path, "ah", "scenario", language="de")["grouped_cards"][0]
    fields = {
        field["id"]: field["value"]
        for field in card["metadata_fields_by_side"]["front"]
    }
    assert fields["clues"] == "1<per>"
    assert "clues_per_investigator" not in fields
    assert card["source_sides"]["front"]["rules"].startswith("{bullet} ")
    assert card["target_sides"]["front"]["rules"].startswith("{bullet} ")

    with pytest.raises(ValueError, match="unsupported tag <per>"):
        apply_gemini_card_image(
            tmp_path,
            game="ah",
            slug="scenario",
            project_language="de",
            card_key="001_Location",
            side="front",
            result={"source": {"rules": "Move <per>."}, "translation": {}, "metadata": {}},
        )
    with pytest.raises(ValueError, match="metadata.connection must not contain"):
        apply_gemini_card_image(
            tmp_path,
            game="ah",
            slug="scenario",
            project_language="de",
            card_key="001_Location",
            side="front",
            result={"source": {}, "translation": {}, "metadata": {"connection": "<circle>"}},
        )


def test_gemini_can_mark_a_standard_back_hidden(tmp_path):
    cards = tmp_path / "ah" / "de" / "scenario" / "cards"
    cards.mkdir(parents=True)
    (cards / "001_Card-1.jpg").write_bytes(b"front")
    (cards / "001_Card-2.jpg").write_bytes(b"back")
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

    prompt = gemini_card_image_prompt(
        tmp_path,
        game="ah",
        slug="scenario",
        project_language="de",
        card_key="001_Card",
        side="back",
    )["prompt"]
    assert '"standard_back": false' in prompt
    assert "generic reusable card back" in prompt

    apply_gemini_card_image(
        tmp_path,
        game="ah",
        slug="scenario",
        project_language="de",
        card_key="001_Card",
        side="back",
        result={"source": {}, "translation": {}, "metadata": {}, "standard_back": True},
    )
    scenario = get_scenario(tmp_path, "ah", "scenario", language="de")
    assert scenario["grouped_cards"][0]["back"]["hidden"] is True


def test_combines_act_stage_and_side_letter_into_shoggoth_index(tmp_path):
    cards = tmp_path / "ah" / "de" / "scenario" / "cards"
    cards.mkdir(parents=True)
    (cards / "001_Act-1.jpg").write_bytes(b"front")
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

    update_card_game_fields(
        tmp_path,
        game="ah",
        slug="scenario",
        project_language="de",
        card_key="001_Act",
        field_values={"card_type": "act", "stage": "1", "index": "a"},
    )

    scenario = get_scenario(tmp_path, "ah", "scenario", language="de")
    fields = {
        field["id"]: field["value"]
        for field in scenario["grouped_cards"][0]["metadata_fields_by_side"]["front"]
    }
    assert fields["stage"] == 1
    assert fields["index"] == "1a"


def test_discovers_nested_source_automatically_when_unambiguous(tmp_path):
    cards = tmp_path / "ah" / "alice" / "Alice in Wonderland"
    cards.mkdir(parents=True)
    (cards / "001 - Card-1.jpg").write_bytes(b"x")
    (cards / "001 - Card-2.jpg").write_bytes(b"x")

    games = discover_library(tmp_path)
    ah = next(game for game in games if game["slug"] == "ah")
    scenario = ah["scenarios"][0]

    assert scenario["card_source"] == "Alice in Wonderland"
    assert scenario["card_count"] == 2
    assert scenario["card_source_needs_selection"] is False


def test_multiple_equal_sources_require_selection(tmp_path):
    root = tmp_path / "ah" / "ward" / "The Case of Charles Dexter Ward"
    with_margins = root / "Individual Card Images (with margins)"
    without_margins = root / "Individual Card Images (without margins)"
    with_margins.mkdir(parents=True)
    without_margins.mkdir(parents=True)

    for directory in (with_margins, without_margins):
        (directory / "01 - Card-1.png").write_bytes(b"x")
        (directory / "01 - Card-2.png").write_bytes(b"x")

    games = discover_library(tmp_path)
    ah = next(game for game in games if game["slug"] == "ah")
    scenario = ah["scenarios"][0]

    assert scenario["card_count"] == 0
    assert scenario["card_source"] is None
    assert scenario["card_source_needs_selection"] is True
    assert len(scenario["card_source_candidates"]) == 2


def test_initialize_scenario_writes_selected_source(tmp_path):
    cards = tmp_path / "lotr" / "test-scenario" / "download" / "images"
    cards.mkdir(parents=True)
    (cards / "001.jpg").write_bytes(b"x")

    initialize_scenario(
        tmp_path,
        game="lotr",
        slug="test-scenario",
        title="Test Scenario",
        translated_title="Testszenario",
        author="Example",
        version="1.0",
        source_url="",
        target_language="de",
        card_source="download/images",
    )

    project = json.loads((tmp_path / "lotr" / "test-scenario" / "project.json").read_text())
    translation = json.loads(
        (tmp_path / "lotr" / "test-scenario" / "translation_de.json").read_text()
    )

    assert project["title"] == "Test Scenario"
    assert project["card_source"] == "download/images"
    assert translation["cards"] == []
    assert translation["target_language"] == "de"



def test_pairs_front_and_back_by_final_suffix(tmp_path):
    cards = tmp_path / "ah" / "alice" / "cards"
    cards.mkdir(parents=True)
    (cards / "005 - Act 3-1-1.jpg").write_bytes(b"x")
    (cards / "005 - Act 3-1-2.jpg").write_bytes(b"x")

    games = discover_library(tmp_path)
    ah = next(game for game in games if game["slug"] == "ah")
    scenario = ah["scenarios"][0]
    grouped = scenario["grouped_cards"]

    assert len(grouped) == 1
    assert grouped[0]["key"] == "005 - Act 3-1"
    assert grouped[0]["front"]["name"].endswith("-1.jpg")
    assert grouped[0]["back"]["name"].endswith("-2.jpg")


def test_pairs_front_and_back_by_named_suffix(tmp_path):
    cards = tmp_path / "ah" / "witching-hour" / "cards"
    cards.mkdir(parents=True)
    (cards / "001-05056-A_Circle_Unbroken-front.png").write_bytes(b"x")
    (cards / "001-05056-A_Circle_Unbroken-back.png").write_bytes(b"x")

    games = discover_library(tmp_path)
    ah = next(game for game in games if game["slug"] == "ah")
    scenario = ah["scenarios"][0]
    grouped = scenario["grouped_cards"]

    assert len(grouped) == 1
    assert grouped[0]["key"] == "001-05056-A_Circle_Unbroken"
    assert grouped[0]["front"]["name"].endswith("-front.png")
    assert grouped[0]["back"]["name"].endswith("-back.png")


def test_pairs_shoggoth_exported_front_and_back(tmp_path):
    cards = tmp_path / "ah" / "alice" / "cards"
    cards.mkdir(parents=True)
    card_id = "314a931d-9183-4b15-9fe7-d895c6cb1331_act_example"
    (cards / f"{card_id}_front_0.png").write_bytes(b"front")
    (cards / f"{card_id}_back_0.png").write_bytes(b"back")

    scenario = next(
        game for game in discover_library(tmp_path) if game["slug"] == "ah"
    )["scenarios"][0]
    grouped = scenario["grouped_cards"]

    assert len(grouped) == 1
    assert grouped[0]["key"] == card_id
    assert grouped[0]["front"]["name"].endswith("_front_0.png")
    assert grouped[0]["back"]["name"].endswith("_back_0.png")


def test_persists_rotation_and_hidden_state(tmp_path):
    cards = tmp_path / "ah" / "alice" / "cards"
    cards.mkdir(parents=True)
    (cards / "001 - Card-1.jpg").write_bytes(b"x")
    (cards / "001 - Card-2.jpg").write_bytes(b"x")

    initialize_scenario(
        tmp_path,
        game="ah",
        slug="alice",
        title="Alice",
        translated_title="",
        author="",
        version="",
        source_url="",
        target_language="de",
        card_source="cards",
    )

    update_image_metadata(
        tmp_path,
        game="ah",
        slug="alice",
        image_name="001 - Card-2.jpg",
        display_rotation=180,
        hidden=True,
    )
    data = json.loads((tmp_path / "ah" / "alice" / "translation_de.json").read_text())
    assert data["_meta"]["images"]["001 - Card-2.jpg"]["display_rotation"] == 180
    assert data["_meta"]["images"]["001 - Card-2.jpg"]["hidden"] is True
    assert data["_meta"]["settings"]["exclude_hidden_sides_from_output"] is True


def test_collapses_byte_identical_cards_into_last_copy(tmp_path):
    cards = tmp_path / "ah" / "de" / "scenario" / "cards"
    cards.mkdir(parents=True)
    for number in (1, 2, 3):
        (cards / f"00{number} - Same Card-1.jpg").write_bytes(b"same front")
        (cards / f"00{number} - Same Card-2.jpg").write_bytes(b"same back")
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

    result = collapse_duplicate_cards(
        tmp_path, game="ah", slug="scenario", project_language="de"
    )

    data = json.loads(
        (tmp_path / "ah" / "de" / "scenario" / "translation_de.json").read_text()
    )
    assert result == {
        "groups": 1,
        "hidden_cards": 2,
        "collapsed": [{
            "representative_key": "003 - Same Card",
            "representative_name": "003 - Same Card",
            "quantity": 3,
            "hidden_keys": ["001 - Same Card", "002 - Same Card"],
        }],
    }
    assert data["_meta"]["images"]["001 - Same Card-1.jpg"]["hidden"] is True
    assert data["_meta"]["images"]["002 - Same Card-2.jpg"]["hidden"] is True
    representative = next(card for card in data["cards"] if card["key"] == "003 - Same Card")
    assert representative["game_data"]["printed_quantity"] == 3


def test_matches_translation_record_by_explicit_image_path(tmp_path):
    cards = tmp_path / "lotr" / "shadow" / "cards"
    cards.mkdir(parents=True)
    (cards / "001 - Three is Company-1.jpg").write_bytes(b"x")
    (cards / "001 - Three is Company-2.jpg").write_bytes(b"x")

    initialize_scenario(
        tmp_path,
        game="lotr",
        slug="shadow",
        title="A Shadow of the Past",
        translated_title="",
        author="",
        version="",
        source_url="",
        target_language="de",
        card_source="cards",
    )
    translation_path = tmp_path / "lotr" / "shadow" / "translation_de.json"
    data = json.loads(translation_path.read_text())
    data["cards"] = [{
        "key": "some-provider-key-that-does-not-match-the-filename",
        "card_type": "Quest",
        "images": {
            "front": "cards/001 - Three is Company-1.jpg",
            "back": "cards/001 - Three is Company-2.jpg",
        },
        "text": {
            "en": {"title": "Three is Company", "rules": "Setup text"},
            "de": {"title": "", "rules": ""},
        },
        "game_data": {},
    }]
    translation_path.write_text(json.dumps(data))

    scenario = next(
        scenario
        for game in discover_library(tmp_path) if game["slug"] == "lotr"
        for scenario in game["scenarios"] if scenario["slug"] == "shadow"
    )
    card = scenario["grouped_cards"][0]
    assert card["matched"] is True
    assert card["card_type"] == "Quest"
    assert card["source_text"]["title"] == "Three is Company"


def test_updates_one_translated_card_side_and_records_manual_provenance(tmp_path):
    cards = tmp_path / "lotr" / "shadow" / "cards"
    cards.mkdir(parents=True)
    (cards / "001 - Three is Company-1.jpg").write_bytes(b"x")
    initialize_scenario(
        tmp_path,
        game="lotr",
        slug="shadow",
        title="A Shadow of the Past",
        translated_title="",
        author="",
        version="",
        source_url="",
        target_language="de",
        card_source="cards",
    )
    translation_path = tmp_path / "lotr" / "shadow" / "translation_de.json"
    data = json.loads(translation_path.read_text())
    data["cards"] = [{
        "id": "hall-of-beorn:Three-is-Company-TBR",
        "key": "001 - Three is Company",
        "images": {"front": "cards/001 - Three is Company-1.jpg", "back": None},
        "text": {
            "en": {"sides": {"front": {"title": "Three is Company", "rules": "Setup", "flavor": "", "traits": []}}},
            "de": {"sides": {"front": {"title": "", "rules": "", "flavor": "", "traits": []}}},
        },
    }]
    translation_path.write_text(json.dumps(data))

    result = update_card_translation(
        tmp_path,
        game="lotr",
        slug="shadow",
        card_key="001 - Three is Company",
        language="de",
        side="front",
        title="Drei sind eine Gesellschaft",
        rules="Vorbereitung: Mische das Begegnungsdeck.",
        flavor="Eine Reise beginnt.",
        traits="Auenland, Hobbit",
    )
    assert result["traits"] == ["Auenland", "Hobbit"]

    saved = json.loads(translation_path.read_text())
    localized = saved["cards"][0]["text"]["de"]
    assert localized["sides"]["front"]["rules"].startswith("Vorbereitung")
    assert localized["title"] == "Drei sind eine Gesellschaft"
    field = "hall-of-beorn:Three-is-Company-TBR.text.de.sides.front.rules"
    assert saved["_meta"]["fields"][field]["source"] == "manual"
    assert saved["_meta"]["fields"][field]["confidence"] == 1.0
    assert saved["_meta"]["fields"][field]["source_value"] == "Setup"
    assert len(saved["_meta"]["fields"][field]["source_hash"]) == 64


def test_translation_save_creates_record_for_unmatched_local_card(tmp_path):
    cards = tmp_path / "ah" / "de" / "scenario" / "cards"
    cards.mkdir(parents=True)
    (cards / "001_Unknown_front.png").write_bytes(b"x")
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

    result = update_card_translation(
        tmp_path,
        game="ah",
        slug="scenario",
        card_key="001_Unknown_front",
        language="de",
        side="front",
        title="Unbekannt",
        rules="Regeltext",
        flavor="",
        traits="",
    )

    assert result["title"] == "Unbekannt"
    saved = json.loads(
        (tmp_path / "ah" / "de" / "scenario" / "translation_de.json").read_text()
    )
    assert saved["cards"][0]["key"] == "001_Unknown_front"
    assert saved["cards"][0]["images"]["front"] == "cards/001_Unknown_front.png"
    assert saved["cards"][0]["text"]["de"]["sides"]["front"]["rules"] == "Regeltext"


def test_delete_scenario_removes_only_project_folder(tmp_path):
    project = tmp_path / "lotr" / "to-delete"
    project.mkdir(parents=True)
    (project / "project.json").write_text("{}")
    sibling = tmp_path / "lotr" / "keep-me"
    sibling.mkdir()

    delete_scenario(tmp_path, game="lotr", slug="to-delete")

    assert not project.exists()
    assert sibling.exists()
