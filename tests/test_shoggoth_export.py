from __future__ import annotations

import pytest

from card_translator.shoggoth_export import build_shoggoth_document


def test_source_workspace_exports_captured_source_text(tmp_path):
    front = tmp_path / "source-front.png"
    front.write_bytes(b"front")
    scenario = {
        "initialized": True,
        "source_only": True,
        "slug": "scenario",
        "title": "Scenario",
        "target_language": "en",
        "project": {"title": "Scenario"},
        "grouped_cards": [{
            "key": "source-card",
            "display_name": "Source Card",
            "record": {"card_type": "story"},
            "card_type": "story",
            "front": {"path": front},
            "source_sides": {
                "front": {"title": "Recovered title", "rules": "Recovered rules."}
            },
            "target_sides": {},
            "game_data": {},
        }],
    }

    card = build_shoggoth_document(scenario)["cards"][0]

    assert card["name"] == "Recovered title"
    assert card["front"]["name"] == "Recovered title"
    assert card["front"]["text"] == "Recovered rules."


def test_exports_complete_card_images_and_translated_fields(tmp_path):
    front = tmp_path / "001-05060-Woods-front.png"
    back = tmp_path / "001-05060-Woods-back.png"
    front.write_bytes(b"front")
    back.write_bytes(b"back")
    scenario = {
        "initialized": True,
        "slug": "witching-hour",
        "title": "The Witching Hour",
        "target_language": "de",
        "project": {
            "title": "The Witching Hour",
            "translated_title": "Hexenstunde",
            "shoggoth_set_icon": "/icons/witching-hour.svg",
            "shoggoth_encounter_icon": "/icons/encounter.svg",
            "shoggoth_illustrator_prefix": "Illus. ",
        },
        "grouped_cards": [
            {
                "key": "001-05060-Woods",
                "display_name": "Woods",
                "record": {"card_type": "Location"},
                "card_type": "Location",
                "front": {"path": front},
                "back": {"path": back},
                "target_sides": {
                    "front": {
                        "title": "Verhexter Wald",
                        "subname": "Dunkler Pfad",
                        "traits": ["Wald", "Geist"],
                        "rules": "{revelation} {action} Lege 1 {clue} Hinweis darauf.",
                        "flavor": "Die Bäume flüstern.",
                    },
                    "back": {"title": "Verhexter Wald", "traits": ["Arkham", "Innenstadt"]},
                },
                "game_data": {
                    "shroud": 3,
                    "clues": 1,
                    "clues_per_investigator": "true",
                    "victory": 1,
                    "connection": "diamond",
                    "artist": "Alice Example",
                    "copyright": "2026 Happy Tomte",
                    "project_number": 12,
                    "encounter_number": 3,
                    "classes": ["guardian"],
                    "connections": ["circle", "square"],
                    "sides": {
                        "back": {
                            "connection": "square",
                            "artist": "Bob Example",
                            "connections": ["circle"],
                            "shroud": 4,
                            "clues": 2,
                            "clues_per_investigator": "true",
                        }
                    },
                    "printed_quantity": 2,
                },
            }
        ],
    }

    document = build_shoggoth_document(scenario)
    card = document["cards"][0]

    assert document["name"] == "Hexenstunde"
    assert document["icon"] == "/icons/witching-hour.svg"
    assert document["encounter_sets"][0]["icon"] == "/icons/encounter.svg"
    assert document["meta"]["language"] == "de"
    assert card["amount"] == 2
    assert card["copyright"] == "© 2026 Happy Tomte"
    assert card["project_number"] == "12"
    assert card["encounter_number"] == "3"
    assert card["front"]["type"] == "location"
    assert card["back"]["type"] == "location_back"
    assert card["front"]["illustration"] == str(front.resolve())
    assert card["back"]["illustration"] == str(back.resolve())
    assert card["front"]["text"] == "<rev> <action> Lege 1 <clue> Hinweis darauf."
    assert card["front"]["traits"] == "Wald. Geist"
    assert card["front"]["subtitle"] == "Dunkler Pfad"
    assert card["front"]["connection"] == "diamond"
    assert card["front"]["illustrator"] == "Illus. Alice Example"
    assert "classes" not in card["front"]
    assert card["front"]["connections"] == ["circle", "square"]
    assert card["front"]["clues"] == "1<per>"
    assert card["back"]["connection"] == "square"
    assert card["back"]["illustrator"] == "Illus. Bob Example"
    assert card["back"]["connections"] == ["circle"]
    assert card["back"]["shroud"] == "4"
    assert card["back"]["clues"] == "2<per>"
    assert card["back"]["traits"] == "Arkham. Innenstadt"
    assert "illustration_scale" not in card["front"]
    assert "illustration_pan_x" not in card["front"]


def test_exports_front_and_back_index_for_act_cards(tmp_path):
    front = tmp_path / "act-front.png"
    back = tmp_path / "act-back.png"
    front.write_bytes(b"front")
    back.write_bytes(b"back")
    scenario = {
        "initialized": True,
        "slug": "scenario",
        "title": "Scenario",
        "project": {},
        "grouped_cards": [
            {
                "key": "act",
                "display_name": "Act",
                "record": {"card_type": "Act"},
                "card_type": "Act",
                "front": {"path": front},
                "back": {"path": back},
                "target_sides": {
                    "front": {"title": "Das Ziel"},
                    "back": {"title": "Die Wahrheit"},
                },
                "game_data": {
                    "index": "2a",
                    "sides": {"back": {"index": "2b"}},
                },
            }
        ],
    }

    card = build_shoggoth_document(scenario)["cards"][0]

    assert card["front"]["type"] == "act"
    assert card["front"]["index"] == "2a"
    assert card["back"]["type"] == "act_back"
    assert card["back"]["index"] == "2b"


def test_explicit_imported_back_type_overrides_the_default(tmp_path):
    front = tmp_path / "story-front.png"
    back = tmp_path / "story-back.png"
    front.write_bytes(b"front")
    back.write_bytes(b"back")
    scenario = {
        "initialized": True,
        "slug": "scenario",
        "title": "Scenario",
        "project": {},
        "grouped_cards": [{
            "key": "story",
            "display_name": "Story",
            "record": {"card_type": "story"},
            "card_type": "story",
            "front": {"path": front},
            "back": {"path": back},
            "target_sides": {"front": {"title": "Story"}, "back": {}},
            "game_data": {"sides": {"back": {"card_type": "enemy"}}},
        }],
    }

    card = build_shoggoth_document(scenario)["cards"][0]

    assert card["front"]["type"] == "story"
    assert card["back"]["type"] == "enemy"


def test_exports_chaos_rules_as_shoggoth_chaos_entries(tmp_path):
    image = tmp_path / "scenario-front.png"
    image.write_bytes(b"front")
    scenario = {
        "initialized": True,
        "slug": "scenario",
        "title": "Scenario",
        "project": {},
        "grouped_cards": [
            {
                "key": "scenario",
                "display_name": "Scenario",
                "record": {"card_type": "Chaos"},
                "card_type": "Chaos",
                "front": {"path": image},
                "back": None,
                "target_sides": {
                    "front": {
                        "title": "Szenario",
                        "rules": "{skull}: -2. Lege 1 Karte ab.\n{cultist}{tablet}: -4.",
                    }
                },
                "game_data": {},
            }
        ],
    }

    card = build_shoggoth_document(scenario)["cards"][0]

    assert card["front"]["type"] == "chaos"
    assert card["front"]["entries"] == [
        {"token": ["skull"], "text": "-2. Lege 1 Karte ab."},
        {"token": ["cultist", "tablet"], "text": "-4."},
    ]


def test_exports_numeric_and_image_only_chaos_tokens(tmp_path):
    image = tmp_path / "scenario-front.png"
    image.write_bytes(b"front")
    scenario = {
        "initialized": True,
        "slug": "scenario",
        "title": "Scenario",
        "project": {},
        "grouped_cards": [{
            "key": "scenario",
            "display_name": "Scenario",
            "record": {"card_type": "Chaos"},
            "card_type": "Chaos",
            "front": {"path": image},
            "back": None,
            "target_sides": {"front": {"rules": "{-4}: Text.\n{blood}{fail}: More."}},
            "game_data": {},
        }],
    }

    entries = build_shoggoth_document(scenario)["cards"][0]["front"]["entries"]

    assert entries == [
        {"token": ["-4"], "text": "Text."},
        {"token": ["blood", "fail"], "text": "More."},
    ]


def test_exports_minicard_with_investigator_front_and_encounter_back(tmp_path):
    front = tmp_path / "alice-front.png"
    back = tmp_path / "alice-back.png"
    front.write_bytes(b"front")
    back.write_bytes(b"back")
    scenario = {
        "initialized": True,
        "slug": "alice",
        "title": "Alice",
        "project": {},
        "grouped_cards": [
            {
                "key": "alice3",
                "display_name": "alice3",
                "record": {"card_type": "minicard"},
                "card_type": "minicard",
                "front": {"path": front},
                "back": {"path": back},
                "target_sides": {"front": {}, "back": {}},
                "game_data": {},
            }
        ],
    }

    card = build_shoggoth_document(scenario)["cards"][0]

    assert card["front"]["type"] == "mini_investigator"
    assert card["back"]["type"] == "mini_investigator_back"
    assert card["front"]["illustration"] == str(front.resolve())
    assert card["back"]["illustration"] == str(back.resolve())


def test_replaces_ignored_standard_back_with_type_only_fallback(tmp_path):
    front = tmp_path / "card-front.png"
    back = tmp_path / "standard-back.png"
    front.write_bytes(b"front")
    back.write_bytes(b"back")
    scenario = {
        "initialized": True,
        "slug": "alice",
        "title": "Alice",
        "project": {},
        "grouped_cards": [
            {
                "key": "card",
                "display_name": "Card",
                "record": {"card_type": "treachery"},
                "card_type": "treachery",
                "front": {"path": front, "hidden": False},
                "back": {"path": back, "hidden": True},
                "target_sides": {"front": {"title": "Karte"}, "back": {}},
                "game_data": {},
            }
        ],
    }

    card = build_shoggoth_document(scenario)["cards"][0]

    assert "front" in card
    assert card["back"] == {"type": "encounter"}


@pytest.mark.parametrize(
    ("card_type", "front_type", "back_type"),
    [
        ("act", "act", "act_back"),
        ("agenda", "agenda", "agenda_back"),
        ("asset", "asset", "player"),
        ("chaos", "chaos", "chaos"),
        ("enemy", "enemy", "encounter"),
        ("enemy_location", "enemy_location", "location_back"),
        ("event", "event", "player"),
        ("investigator", "investigator", "investigator_back"),
        ("key", "key", "key_back"),
        ("location", "location", "location_back"),
        ("mini_investigator", "mini_investigator", "mini_investigator_back"),
        ("scenario", "scenario", "scenario_back"),
        ("skill", "skill", "player"),
        ("story", "story", "story"),
        ("treachery", "treachery", "encounter"),
        ("ultimatum", "ultimatum", "ultimatum_back"),
        ("weakness", "weakness_treachery", "player"),
    ],
)
def test_adds_shoggoth_fallback_back_for_each_arkham_card_type(
    tmp_path, card_type, front_type, back_type
):
    front = tmp_path / f"{card_type}-front.png"
    front.write_bytes(b"front")
    scenario = {
        "initialized": True,
        "slug": "scenario",
        "title": "Scenario",
        "project": {},
        "grouped_cards": [
            {
                "key": card_type,
                "display_name": card_type,
                "record": {"card_type": card_type},
                "card_type": card_type,
                "front": {"path": front},
                "back": None,
                "target_sides": {"front": {"title": card_type}},
                "game_data": {},
            }
        ],
    }

    card = build_shoggoth_document(scenario)["cards"][0]

    assert card["front"]["type"] == front_type
    assert card["back"] == {"type": back_type}


def test_fallback_back_keeps_imported_shoggoth_overrides(tmp_path):
    front = tmp_path / "asset-front.png"
    front.write_bytes(b"front")
    scenario = {
        "initialized": True,
        "slug": "scenario",
        "title": "Scenario",
        "project": {},
        "grouped_cards": [
            {
                "key": "asset",
                "display_name": "Asset",
                "record": {
                    "card_type": "asset",
                    "shoggoth": {"sides": {"back": {"custom": "value"}}},
                },
                "card_type": "asset",
                "front": {"path": front},
                "back": None,
                "target_sides": {"front": {"title": "Asset"}},
                "game_data": {},
            }
        ],
    }

    card = build_shoggoth_document(scenario)["cards"][0]

    assert card["back"] == {"custom": "value", "type": "player"}


def test_exports_all_player_card_fields_without_an_encounter_set(tmp_path):
    front = tmp_path / "asset-front.png"
    front.write_bytes(b"front")
    scenario = {
        "initialized": True,
        "slug": "scenario",
        "title": "Scenario",
        "project": {},
        "grouped_cards": [
            {
                "key": "charles",
                "display_name": "Charles Dexter Card",
                "record": {"card_type": "asset"},
                "card_type": "asset",
                "front": {"path": front},
                "back": None,
                "target_sides": {"front": {"title": "Charles Dexter Card"}},
                "game_data": {
                    "classes": ["neutral"],
                    "cost": 3,
                    "xp": 0,
                    "slot": "Ally, Arcane",
                    "willpower": 1,
                    "intellect": 2,
                    "combat": 1,
                    "agility": 0,
                    "wild": 1,
                    "health": 2,
                    "sanity": 1,
                    "printed_quantity": 2,
                },
            }
        ],
    }

    card = build_shoggoth_document(scenario)["cards"][0]

    assert "encounter_set" not in card
    assert card["amount"] == 2
    assert card["front"] == {
        "type": "asset",
        "illustration": str(front.resolve()),
        "name": "Charles Dexter Card",
        "health": "2",
        "sanity": "1",
        "classes": ["neutral"],
        "cost": "3",
        "level": "0",
        "slots": ["ally", "arcane"],
        "icons": "WIICQ",
    }
    assert card["back"] == {"type": "player"}


def test_exports_enemy_fight_as_attack_and_vengeance_as_victory_text(tmp_path):
    front = tmp_path / "enemy-front.png"
    front.write_bytes(b"front")
    scenario = {
        "initialized": True,
        "slug": "scenario",
        "title": "Scenario",
        "project": {},
        "grouped_cards": [
            {
                "key": "enemy",
                "display_name": "Enemy",
                "record": {"card_type": "enemy"},
                "card_type": "enemy",
                "front": {"path": front},
                "back": None,
                "target_sides": {"front": {"title": "{unique}Enemy"}},
                "game_data": {
                    "health": "X<per>",
                    "fight": "5<per>",
                    "evade": "X<per>",
                    "vengeance": 2,
                },
            }
        ],
    }

    card = build_shoggoth_document(scenario)["cards"][0]

    assert card["front"]["name"] == "<unique>Enemy"
    assert card["front"]["health"] == "X<per>"
    assert card["front"]["attack"] == "5<per>"
    assert card["front"]["evade"] == "X<per>"
    assert "fight" not in card["front"]
    assert card["front"]["victory"] == "Vengeance 2."
    assert card["encounter_set"] == "scenario-encounter"
    assert card["amount"] == 3
