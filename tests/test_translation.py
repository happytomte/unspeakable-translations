from __future__ import annotations

import json
import re

import pytest

from card_translator import translation
from card_translator.arkham_reference import (
    INDEX_FILENAME,
    ArkhamReference,
    arkham_reference_status,
    build_arkham_reference_index,
    download_arkhamdb_cards,
    find_arkham_references,
)


def test_download_arkhamdb_cards_validates_and_persists_json(tmp_path, monkeypatch):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return [{"code": "01001", "name": "Roland Banks"}]

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, _url, **_kwargs):
            return Response()

    monkeypatch.setattr("card_translator.arkham_reference.httpx.Client", Client)

    result = download_arkhamdb_cards(
        tmp_path,
        language="DE",
        url="https://de.arkhamdb.com/api/public/cards/?encounter=1",
    )

    assert result["card_count"] == 1
    assert json.loads((tmp_path / "arkham.de.json").read_text(encoding="utf-8"))[0][
        "code"
    ] == "01001"


def test_configured_game_context_is_in_translation_prompt(monkeypatch):
    prompts: list[str] = []

    def fake_translate(prompt: str) -> str:
        prompts.append(prompt)
        return "Übersetzung"

    monkeypatch.setattr(translation, "_translate_with_ollama", fake_translate)
    result = translation.translate_texts(
        ["Source"],
        source_language="en",
        target_language="de",
        game="custom-game",
        game_context="Use the terse terminology of Custom Game.",
        fields=["rules"],
    )

    assert result == ["Übersetzung"]
    assert "Use the terse terminology of Custom Game." in prompts[0]
    assert "Arkham Horror" not in prompts[0]


def test_official_references_are_added_to_translation_prompt(monkeypatch):
    prompts: list[str] = []

    def fake_translate(prompt: str) -> str:
        prompts.append(prompt)
        return "Lege 1 Verderben auf diese Agenda."

    monkeypatch.setattr(translation, "_translate_with_ollama", fake_translate)
    reference = ArkhamReference(
        source="Place 1 doom on this agenda.",
        target="Lege 1 Verderben auf diese Agenda.",
        card_name="The Night Howls",
        card_type="Agenda",
        field="rules",
    )
    translation.translate_texts(
        ["Place 1 doom on this agenda."],
        source_language="en",
        target_language="de",
        game="ah",
        fields=["rules"],
        references=[[reference]],
    )

    assert "Official Arkham translation example (The Night Howls)" in prompts[0]
    assert "Lege 1 Verderben auf diese Agenda." in prompts[0]
    assert "do not copy unrelated card content" in prompts[0]


def test_finds_matching_arkham_reference_by_field_and_card_type(tmp_path):
    english = [
        {
            "code": "05052",
            "name": "The Night Howls",
            "type_name": "Agenda",
            "text": "At the end of the round, place 1 doom on this agenda.",
            "flavor": "The dark forest waits.",
        },
        {
            "code": "01001",
            "name": "Other Card",
            "type_name": "Asset",
            "text": "Place 1 doom on an enemy.",
        },
    ]
    german = [
        {
            "code": "05052",
            "name": "Heulen in der Nacht",
            "text": "Lege am Ende der Runde 1 Verderben auf diese Agenda.",
            "flavor": "Der dunkle Wald wartet.",
        },
        {
            "code": "01001",
            "name": "Andere Karte",
            "text": "Lege 1 Verderben auf einen Gegner.",
        },
    ]
    (tmp_path / "arkham.en.json").write_text(json.dumps(english), encoding="utf-8")
    (tmp_path / "arkham.de.json").write_text(json.dumps(german), encoding="utf-8")
    result = build_arkham_reference_index(tmp_path)

    references = find_arkham_references(
        tmp_path / INDEX_FILENAME,
        text="At the end of the round, place doom on this agenda.",
        field="rules",
        target_language="de",
        card_type="Agenda",
        limit=1,
    )

    assert result["reference_counts"]["de"] == 5
    assert len(references) == 1
    assert references[0].card_name == "The Night Howls"
    assert references[0].target == "Lege am Ende der Runde 1 Verderben auf diese Agenda."


def test_reference_index_supports_each_arkham_language_and_reports_missing(tmp_path):
    english = [{"code": "1", "name": "Doom", "text": "Place one doom."}]
    french = [{"code": "1", "name": "Fatalité", "text": "Placez une fatalité."}]
    (tmp_path / "arkham.en.json").write_text(json.dumps(english), encoding="utf-8")
    (tmp_path / "arkham.fr.json").write_text(json.dumps(french), encoding="utf-8")
    build_arkham_reference_index(tmp_path)
    references = find_arkham_references(
        tmp_path / INDEX_FILENAME,
        text="Place doom on the agenda.",
        field="rules",
        target_language="fr",
    )
    status = arkham_reference_status(
        tmp_path,
        requested_languages={"fr", "es"},
    )

    assert references[0].target == "Placez une fatalité."
    assert status["available_languages"] == ["fr"]
    assert status["indexed_languages"] == ["fr"]
    assert status["indexed_source_language"] == "en"
    assert status["source_indexed"] is True
    assert status["missing_index_languages"] == ["es"]
    assert status["missing_languages"] == ["es"]
    assert status["stale"] is False


def test_changed_placeholder_returns_editable_review_details(monkeypatch):
    monkeypatch.setattr(
        translation,
        "_translate_with_ollama",
        lambda _prompt: "Lege die Karte an einen Ort an.",
    )

    with pytest.raises(translation.TranslationReviewRequired) as raised:
        translation.translate_texts(
            ["Attach {card_name} to a location."],
            source_language="en",
            target_language="de",
            fields=["rules"],
        )

    error = raised.value
    assert error.original == "Attach {card_name} to a location."
    assert error.draft == "Lege die Karte an einen Ort an."
    assert error.issues == [
        {
            "type": "missing",
            "marker": "[CT_PROTECTED_0000]",
            "expected": "{card_name}",
            "count": 0,
        }
    ]


def test_copied_symbol_is_normalized_to_mandatory_glossary_target(monkeypatch):
    monkeypatch.setattr(
        translation,
        "_translate_with_ollama",
        lambda _prompt: "Teste [willpower] und [agility].",
    )

    result = translation.translate_texts(
        ["Test [willpower] and [agility]."],
        source_language="en",
        target_language="de",
        fields=["rules"],
        rules=[
            translation.TranslationRule("[willpower]", "{willpower}"),
            translation.TranslationRule("[agility]", "{agility}"),
        ],
    )

    assert result == ["Teste {willpower} und {agility}."]


def test_directly_rendered_glossary_target_satisfies_placeholder(monkeypatch):
    monkeypatch.setattr(
        translation,
        "_translate_with_ollama",
        lambda _prompt: "Lege 1 Verderben auf die Agenda.",
    )

    result = translation.translate_texts(
        ["Place 1 doom on the agenda."],
        source_language="en",
        target_language="de",
        fields=["rules"],
        rules=[translation.TranslationRule("doom", "Verderben")],
    )

    assert result == ["Lege 1 Verderben auf die Agenda."]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("Prowling Chaos", "Schleichendes Chaos"),
        ("The Prowling Chaos", "Das Schleichende Chaos"),
        ("the Prowling Chaos", "das Schleichende Chaos"),
    ],
)
def test_glossary_prefers_exact_casing_variants(monkeypatch, source, expected):
    monkeypatch.setattr(
        translation,
        "_translate_with_ollama",
        lambda prompt: next(
            marker
            for marker in re.findall(r"\[CT_PROTECTED_\d{4}\]", prompt)
        ),
    )
    rules = translation.parse_rules(
        "Prowling Chaos => Schleichendes Chaos\n"
        "The Prowling Chaos => Das Schleichende Chaos\n"
        "the Prowling Chaos => das Schleichende Chaos"
    )

    result = translation.translate_texts(
        [source],
        source_language="en",
        target_language="de",
        fields=["title"],
        rules=rules,
    )

    assert result == [expected]


def test_single_glossary_spelling_still_matches_case_insensitively(monkeypatch):
    monkeypatch.setattr(
        translation,
        "_translate_with_ollama",
        lambda _prompt: "[CT_PROTECTED_0000]",
    )

    result = translation.translate_texts(
        ["DOOM"],
        source_language="en",
        target_language="de",
        fields=["rules"],
        rules=[translation.TranslationRule("doom", "Verderben")],
    )

    assert result == ["Verderben"]


def test_finalizes_pasted_translation_with_glossary_and_symbol_rules():
    result, issues = translation.finalize_external_translation(
        "The Prowling Chaos gains [clue].",
        "The Prowling Chaos erhält [clue].",
        [
            translation.TranslationRule("The Prowling Chaos", "Das Schleichende Chaos"),
            translation.TranslationRule("[clue]", "{clue}"),
        ],
    )

    assert result == "Das Schleichende Chaos erhält {clue}."
    assert issues == []


def test_finalizing_pasted_translation_reports_missing_protected_value():
    result, issues = translation.finalize_external_translation(
        "Gain [clue].",
        "Erhalte einen Hinweis.",
        [translation.TranslationRule("[clue]", "{clue}")],
    )

    assert result == "Erhalte einen Hinweis."
    assert issues[0]["expected"] == "{clue}"
    assert issues[0]["type"] == "missing"


def test_external_translation_provider_receives_and_restores_placeholders():
    received = []

    def external(text, **_kwargs):
        received.append(text)
        return "Erhalte [CT_PROTECTED_0000]."

    result = translation.translate_texts(
        ["Gain [clue]."],
        source_language="en",
        target_language="de",
        rules=[translation.TranslationRule("[clue]", "{clue}")],
        external_translator=external,
        provider_name="google-cloud",
    )

    assert received == ["Gain [CT_PROTECTED_0000]."]
    assert result == ["Erhalte {clue}."]


def test_prepares_web_translation_with_glossary_placeholders():
    result = translation.prepare_external_translation(
        "The Prowling Chaos gains [clue].",
        [
            translation.TranslationRule("The Prowling Chaos", "Das Schleichende Chaos"),
            translation.TranslationRule("[clue]", "{clue}"),
        ],
    )

    assert result == "[CT_PROTECTED_0000] gains [CT_PROTECTED_0001]."


def test_prompt_translation_provider_gets_full_game_prompt():
    prompts = []

    def provider(prompt, **_kwargs):
        prompts.append(prompt)
        return "[CT_PROTECTED_0000]"

    result = translation.translate_texts(
        ["Prowling Chaos"],
        source_language="en",
        target_language="de",
        game="ah",
        game_context="Use Barkham wordplay.",
        rules=[translation.TranslationRule("Prowling Chaos", "Schleichendes Chaos")],
        prompt_translator=provider,
        provider_name="gemini",
    )

    assert result == ["Schleichendes Chaos"]
    assert "Use Barkham wordplay." in prompts[0]
    assert "CT_PROTECTED_0000" in prompts[0]


def test_merge_rules_retains_casing_variants_from_overriding_group():
    result = translation.merge_rules(
        [translation.TranslationRule("The Prowling Chaos", "old")],
        [
            translation.TranslationRule("The Prowling Chaos", "Das Schleichende Chaos"),
            translation.TranslationRule("the Prowling Chaos", "das Schleichende Chaos"),
        ],
    )

    assert [(rule.source, rule.target) for rule in result] == [
        ("The Prowling Chaos", "Das Schleichende Chaos"),
        ("the Prowling Chaos", "das Schleichende Chaos"),
    ]


def test_unconfigured_square_token_is_only_reported_in_trace(monkeypatch):
    prompts: list[str] = []

    def fake_translate(prompt: str) -> str:
        prompts.append(prompt)
        return "und lege 1 Hinweis darauf."

    monkeypatch.setattr(translation, "_translate_with_ollama", fake_translate)

    trace: list[dict[str, object]] = []
    result = translation.translate_texts(
        ["and place 1[clue] clues on it."],
        source_language="en",
        target_language="de",
        fields=["rules"],
        trace_log=trace,
    )

    assert result == ["und lege 1 Hinweis darauf."]
    assert "1[clue] clues" in prompts[0]
    assert trace[0]["warnings"] == [
        {
            "type": "unconfigured_token",
            "value": "[clue]",
            "message": "No glossary rule is configured for this square-bracket token",
        }
    ]


def test_translation_trace_contains_prompt_raw_normalized_and_final(monkeypatch):
    monkeypatch.setattr(
        translation,
        "_translate_with_ollama",
        lambda _prompt: "Teste [willpower].",
    )
    trace: list[dict[str, object]] = []

    translation.translate_texts(
        ["Test [willpower]."],
        source_language="en",
        target_language="de",
        fields=["rules"],
        rules=[translation.TranslationRule("[willpower]", "{willpower}")],
        trace_log=trace,
    )

    assert trace[0]["original"] == "Test [willpower]."
    assert trace[0]["raw_translation"] == "Teste [willpower]."
    assert trace[0]["final_translation"] == "Teste {willpower}."
    assert "CT_PROTECTED_0000" in str(trace[0]["prompt"])
    assert trace[0]["status"] == "translated"
