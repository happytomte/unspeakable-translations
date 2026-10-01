from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

from card_translator.library import get_scenario

SHOGGOTH_FRONT_TYPES = {
    "act", "agenda", "act_agenda_full", "asset", "chaos", "concealed",
    "customizable", "enemy", "enemy_deck", "enemy_location", "event",
    "investigator", "key", "location", "mini_investigator", "scenario",
    "skill", "story", "treachery", "ultimatum", "fullart_asset", "fullart_event",
    "fullart_skill", "fullart_investigator", "fullart_enemy",
    "fullart_treachery", "fullart_location", "fullart_scanning",
    "fullart_encounter_with_connections", "chapter_2/enemy",
}
TYPE_ALIASES = {
    **{card_type: card_type for card_type in SHOGGOTH_FRONT_TYPES},
    "scene": "act",
    "szene": "act",
    "ort": "location",
    "minicard": "mini_investigator",
    "mini card": "mini_investigator",
    "gegner": "enemy",
    "enemy-location": "enemy_location",
    "verrat": "treachery",
    "szenario": "scenario",
    "ermittler": "investigator",
    "geschichte": "story",
    "schlüssel": "key",
    "weakness": "weakness_treachery",
    "schwäche": "weakness_treachery",
}
BACK_TYPES = {
    "act": "act_back",
    "agenda": "agenda_back",
    "act_agenda_full": "act_agenda_full_back",
    "asset": "player",
    "chaos": "chaos",
    "concealed": "concealed_back",
    "customizable": "customizable_back",
    "enemy": "encounter",
    "enemy_deck": "enemy_deck",
    "enemy_location": "location_back",
    "event": "player",
    "investigator": "investigator_back",
    "key": "key_back",
    "location": "location_back",
    "mini_investigator": "mini_investigator_back",
    "scenario": "scenario_back",
    "skill": "player",
    "story": "story",
    "treachery": "encounter",
    "ultimatum": "ultimatum_back",
    "weakness_treachery": "player",
    "fullart_asset": "player",
    "fullart_event": "player",
    "fullart_skill": "player",
    "fullart_investigator": "investigator_back",
    "fullart_enemy": "encounter",
    "fullart_treachery": "encounter",
    "fullart_location": "fullart_location_back",
    "fullart_scanning": "fullart_scanning_back",
    "fullart_encounter_with_connections": "encounter",
    "chapter_2/enemy": "encounter",
}
PLAYER_FRONT_TYPES = {
    "asset", "event", "skill", "customizable", "fullart_asset",
    "fullart_event", "fullart_skill",
}
INVESTIGATOR_FRONT_TYPES = {"investigator", "fullart_investigator"}
CLASS_FRONT_TYPES = {
    "asset", "enemy", "enemy_location", "event", "investigator", "skill",
    "treachery", "weakness_treachery", "customizable", "fullart_asset",
    "fullart_event", "fullart_skill", "fullart_investigator", "fullart_enemy",
    "fullart_treachery", "chapter_2/enemy",
}
ENCOUNTER_FRONT_TYPES = {
    "act", "agenda", "chaos", "enemy", "enemy_location", "key", "location",
    "story", "treachery", "ultimatum", "scenario", "concealed", "enemy_deck",
    "act_agenda_full", "fullart_enemy", "fullart_treachery", "fullart_location",
    "fullart_scanning", "fullart_encounter_with_connections", "chapter_2/enemy",
}
DEFAULT_AMOUNTS = {"asset": 2, "event": 2, "skill": 2, "enemy": 3, "treachery": 3}
SKILL_ICON_FIELDS = (
    ("willpower", "W"),
    ("intellect", "I"),
    ("combat", "C"),
    ("agility", "A"),
    ("wild", "Q"),
)
TOKEN = re.compile(r"(?:\{([^{}]+)\}|(?<!\[)\[([a-z][a-z0-9_]*)\](?!\]))", re.IGNORECASE)
TOKEN_ALIASES = {
    "per_investigator": "per",
    "revelation": "rev",
    "forced": "for",
    "objective": "obj",
}


def _slug(value: str, fallback: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return slug or fallback


def _shoggoth_markup(value: Any) -> str:
    text = str(value or "")

    def replace_token(match: re.Match[str]) -> str:
        token = (match.group(1) or match.group(2) or "").casefold()
        return f"<{TOKEN_ALIASES.get(token, token)}>"

    return TOKEN.sub(replace_token, text)


def _traits(value: Any) -> str:
    if isinstance(value, list):
        return ". ".join(str(item).strip().rstrip(".") for item in value if str(item).strip())
    return str(value or "").strip()


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in re.split(r"[,;\n]", str(value or "")) if item.strip()]


def _skill_icons(data: dict[str, Any]) -> str:
    icons = ""
    for field, letter in SKILL_ICON_FIELDS:
        value = data.get(field)
        try:
            count = int(value) if value not in {None, ""} else 0
        except (TypeError, ValueError):
            count = 0
        if count > 0:
            icons += letter * count
    return icons


def _side_type(
    front_type: str, side: str, game_data: dict[str, Any] | None = None
) -> str:
    if side == "front":
        return front_type
    side_data = ((game_data or {}).get("sides") or {}).get("back") or {}
    if isinstance(side_data, dict) and side_data.get("card_type"):
        return str(side_data["card_type"])
    return BACK_TYPES.get(front_type, f"{front_type}_back")


def _chaos_entries(rules: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for line in rules.splitlines():
        tokens = [left or right for left, right in TOKEN.findall(line)]
        if not tokens:
            continue
        text = TOKEN.sub("", line).lstrip(" :").strip()
        entries.append({"token": tokens, "text": _shoggoth_markup(text)})
    return entries


def _side_payload(
    *,
    side_name: str,
    side_image: dict[str, Any],
    front_type: str,
    text: dict[str, Any],
    game_data: dict[str, Any],
    illustrator_prefix: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": _side_type(front_type, side_name, game_data),
        # Shoggoth uses the illustration field as its source image. Deliberately
        # omit pan/scale so the source is the complete original card template.
        "illustration": str(Path(side_image["path"]).resolve()),
    }
    title = str(text.get("title") or "").strip()
    subname = str(text.get("subname") or "").strip()
    rules = str(text.get("rules") or "").strip()
    flavor = str(text.get("flavor") or "").strip()
    traits = _traits(text.get("traits"))
    if title:
        payload["name"] = _shoggoth_markup(title)
    if subname:
        payload["subtitle"] = _shoggoth_markup(subname)
    if front_type == "chaos" and rules:
        payload["entries"] = _chaos_entries(rules)
    elif rules:
        payload["text"] = _shoggoth_markup(rules)
    if flavor:
        payload["flavor_text"] = _shoggoth_markup(flavor)
    if traits:
        payload["traits"] = _shoggoth_markup(traits)

    side_overrides = (
        ((game_data.get("sides") or {}).get(side_name) or {})
        if isinstance(game_data.get("sides"), dict)
        else {}
    )
    side_data = {**game_data, **side_overrides} if side_name == "front" else side_overrides
    side_data.pop("sides", None)
    artist = str(side_data.get("artist") or "").strip()
    if artist:
        payload["illustrator"] = f"{illustrator_prefix}{artist}"
    if side_name in {"front", "back"}:
        connection = str(side_data.get("connection") or "").strip()
        if connection:
            payload["connection"] = connection
        value = side_data.get("connections")
        if isinstance(value, list):
            connections = [str(item).strip() for item in value if str(item).strip()]
        else:
            connections = [item.strip() for item in str(value or "").split(",") if item.strip()]
        if connections:
            payload["connections"] = connections
        for field in (
            "shroud", "doom", "stage", "index", "health", "sanity", "damage", "horror", "evade"
        ):
            value = side_data.get(field)
            if value is not None and value != "":
                payload[field] = str(value)
        fight = side_data.get("fight")
        if fight is not None and fight != "":
            payload["attack"] = str(fight)
        classes = _string_list(side_data.get("classes"))
        if classes and front_type in CLASS_FRONT_TYPES:
            payload["classes"] = [item.casefold() for item in classes]
        if side_name == "front" and front_type in PLAYER_FRONT_TYPES | {"weakness_treachery"}:
            cost = side_data.get("cost")
            if cost is not None and cost != "":
                payload["cost"] = str(cost)
            level = side_data.get("xp")
            if level is not None and level != "":
                payload["level"] = str(level)
            slots = _string_list(side_data.get("slots") or side_data.get("slot"))
            if slots:
                payload["slots"] = [item.casefold() for item in slots]
            icons = _skill_icons(side_data)
            if icons:
                payload["icons"] = icons
        elif side_name == "front" and front_type in INVESTIGATOR_FRONT_TYPES:
            for field in ("willpower", "intellect", "combat", "agility"):
                value = side_data.get(field)
                if value is not None and value != "":
                    payload[field] = str(value)
        clues = side_data.get("clues")
        if clues is not None and clues != "":
            per_investigator = str(side_data.get("clues_per_investigator") or "").casefold() in {
                "1", "true", "yes", "on",
            }
            clues_text = str(clues)
            already_compact = bool(
                re.search(r"<(?:per|per_investigator)>\s*$", clues_text, re.IGNORECASE)
            )
            payload["clues"] = (
                f"{clues_text}<per>"
                if per_investigator and not already_compact
                else clues_text
            )
        victory = side_data.get("victory")
        vengeance = side_data.get("vengeance")
        if victory is not None and victory != "":
            payload["victory"] = f"Victory {victory}."
        elif vengeance is not None and vengeance != "":
            payload["victory"] = f"Vengeance {vengeance}."
    return payload


def build_shoggoth_document(scenario: dict[str, Any]) -> dict[str, Any]:
    if not scenario.get("initialized"):
        raise ValueError("Scenario is not initialized")
    project = scenario.get("project") if isinstance(scenario.get("project"), dict) else {}
    source = (
        copy.deepcopy(project.get("shoggoth_source"))
        if isinstance(project.get("shoggoth_source"), dict)
        else {}
    )
    title = str(project.get("translated_title") or project.get("title") or scenario["title"])
    code = _slug(
        str(project.get("shoggoth_code") or source.get("code") or scenario["slug"]),
        "scenario",
    )
    set_icon = str(project.get("shoggoth_set_icon") or source.get("icon") or "").strip()
    encounter_icon = str(project.get("shoggoth_encounter_icon") or "").strip()
    illustrator_prefix = str(project.get("shoggoth_illustrator_prefix", "Illus. "))
    source_sets = source.get("encounter_sets")
    first_source_set = (
        source_sets[0]
        if isinstance(source_sets, list) and source_sets and isinstance(source_sets[0], dict)
        else {}
    )
    set_id = str(first_source_set.get("id") or f"{code}-encounter")
    cards: list[dict[str, Any]] = []

    for grouped in scenario.get("grouped_cards", []):
        record = grouped.get("record") if isinstance(grouped.get("record"), dict) else {}
        target_sides = (
            grouped.get("source_sides")
            if scenario.get("source_only")
            else grouped.get("target_sides")
        ) or {}
        game_data = grouped.get("game_data") if isinstance(grouped.get("game_data"), dict) else {}
        raw_type = str(grouped.get("card_type") or record.get("card_type") or "story")
        front_type = TYPE_ALIASES.get(raw_type.casefold(), _slug(raw_type, "story"))
        front_text = target_sides.get("front") if isinstance(target_sides.get("front"), dict) else {}
        card_name = str(front_text.get("title") or grouped.get("display_name") or grouped["key"])
        shoggoth = record.get("shoggoth") if isinstance(record.get("shoggoth"), dict) else {}
        card_overrides = shoggoth.get("card")
        if not isinstance(card_overrides, dict):
            card_overrides = {}
        identifiers = record.get("identifiers") if isinstance(record.get("identifiers"), dict) else {}
        card: dict[str, Any] = {
            **card_overrides,
            "id": str(
                identifiers.get("shoggoth")
                or f"{set_id}-{_slug(str(grouped['key']), 'card')}"
            ),
            "name": card_name,
            "enumerated": str(card_overrides.get("enumerated") or "manual"),
        }
        if front_type in ENCOUNTER_FRONT_TYPES:
            card["encounter_set"] = str(card_overrides.get("encounter_set") or set_id)
        copyright_text = str(game_data.get("copyright") or "").strip()
        if copyright_text:
            card["copyright"] = (
                copyright_text if copyright_text.startswith("©") else f"© {copyright_text}"
            )
        for source_field, output_field in (
            ("project_number", "project_number"),
            ("encounter_number", "encounter_number"),
        ):
            value = game_data.get(source_field)
            if value is not None and value != "":
                card[output_field] = str(value)
        quantity = game_data.get("printed_quantity")
        if isinstance(quantity, int) and quantity > 0:
            card["amount"] = quantity
        elif "amount" not in card and front_type in DEFAULT_AMOUNTS:
            card["amount"] = DEFAULT_AMOUNTS[front_type]
        for side_name in ("front", "back"):
            side_image = grouped.get(side_name)
            if not isinstance(side_image, dict) or side_image.get("hidden"):
                continue
            separate_art = grouped.get("separate_art")
            has_separate_art = (
                isinstance(separate_art, dict)
                and isinstance(separate_art.get(side_name), dict)
            )
            if has_separate_art:
                side_image = separate_art[side_name]
            side_text = (
                target_sides.get(side_name)
                if isinstance(target_sides.get(side_name), dict)
                else {}
            )
            generated_side = _side_payload(
                side_name=side_name,
                side_image=side_image,
                front_type=front_type,
                text=side_text,
                game_data=game_data,
                illustrator_prefix=illustrator_prefix,
            )
            overrides = (shoggoth.get("sides") or {}).get(side_name)
            card[side_name] = {
                **(overrides if isinstance(overrides, dict) else {}),
                **generated_side,
            }
            if side_name == "front" and "classes" not in card[side_name]:
                if front_type in PLAYER_FRONT_TYPES | INVESTIGATOR_FRONT_TYPES:
                    card[side_name]["classes"] = ["neutral"]
                elif front_type == "weakness_treachery":
                    card[side_name]["classes"] = ["weakness"]
            if (
                not has_separate_art
                and isinstance(overrides, dict)
                and str(overrides.get("illustration") or "").strip()
            ):
                card[side_name]["illustration"] = overrides["illustration"]
        if "front" in card and "back" not in card:
            back_overrides = (shoggoth.get("sides") or {}).get("back")
            card["back"] = {
                **(back_overrides if isinstance(back_overrides, dict) else {}),
                "type": _side_type(front_type, "back", game_data),
            }
        if "front" in card or "back" in card:
            cards.append(card)

    source_meta = source.get("meta") if isinstance(source.get("meta"), dict) else {}
    meta = {
        "dirty": [],
        "french_punctuation": False,
        **source_meta,
        "language": str(scenario.get("target_language") or project.get("target_language") or ""),
    }
    encounter_sets = source_sets if isinstance(source_sets, list) and source_sets else [
        {
            "name": str(project.get("translated_title") or title),
            "icon": encounter_icon,
            "cards": [],
            "id": set_id,
            "meta": {"tts": {}, "location_layouts": []},
        }
    ]
    return {
        **source,
        "name": title,
        "code": code,
        "icon": set_icon,
        "encounter_sets": encounter_sets,
        "cards": cards,
        "id": code,
        "meta": meta,
    }


def export_shoggoth(
    root: Path, *, game: str, project_language: str, slug: str
) -> Path:
    if game != "ah":
        raise ValueError("Shoggoth export is currently available only for Arkham Horror")
    scenario = get_scenario(root, game, slug, language=project_language)
    if scenario is None:
        raise ValueError("Scenario not found")
    document = build_shoggoth_document(scenario)
    destination = (
        scenario["language_project_path"].parent if scenario.get("shared_layout")
        else scenario["path"]
    ) / "builds" / "shoggoth.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return destination
