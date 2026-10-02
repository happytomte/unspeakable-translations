"""Deterministic status projection from local scenario facts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from card_translator.library import IMAGE_SUFFIXES, discover_library


def _present(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return bool(value)
    return value is not None and value != ""


def _active_sides(card: dict[str, Any]) -> list[str]:
    return [
        side for side in ("front", "back")
        if isinstance(card.get(side), dict) and not card[side].get("hidden")
    ]


def _card_text_status(card: dict[str, Any], field_ids: list[str]) -> tuple[bool, bool, bool, bool]:
    source_present = True
    source_reviewed = True
    translated = True
    translated_reviewed = True
    sides = _active_sides(card)
    if not sides:
        return False, False, False, False
    for side in sides:
        source = card.get("source_sides", {}).get(side) or {}
        target = card.get("target_sides", {}).get(side) or {}
        values = [field for field in field_ids if _present(source.get(field))]
        if not values:
            source_present = source_reviewed = translated = translated_reviewed = False
            continue
        source_meta = card.get("source_side_review", {}).get(side) or {}
        target_meta = card.get("target_side_review", {}).get(side) or {}
        for field in values:
            if (source_meta.get(field) or {}).get("review_required") is not False:
                source_reviewed = False
            if not _present(target.get(field)):
                translated = translated_reviewed = False
            elif (target_meta.get(field) or {}).get("review_required") is not False:
                translated_reviewed = False
    return source_present, source_reviewed, translated, translated_reviewed


def _rendered_card_count(path: Path) -> int:
    if not path.is_dir():
        return 0
    return sum(1 for item in path.iterdir() if item.is_file() and item.suffix.lower() in IMAGE_SUFFIXES)


def build_status_catalog(root: Path) -> dict[str, Any]:
    projects: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for game in discover_library(root):
        for scenario in game["scenarios"]:
            if scenario["initialized"]:
                projects.setdefault((
                    scenario["game"], str(scenario.get("campaign") or ""), scenario["slug"]
                ), []).append(scenario)

    rows: list[dict[str, Any]] = []
    for (game, campaign, slug), languages in sorted(projects.items()):
        languages.sort(key=lambda item: item["storage_language"])
        primary = languages[0]
        shared = bool(primary.get("shared_layout"))
        cards = [card for card in primary["grouped_cards"] if _active_sides(card)]
        field_ids = [str(item["id"]) for item in primary["translatable_fields"]]
        source_states = [_card_text_status(card, field_ids) for card in cards]
        source_present = sum(state[0] for state in source_states)
        source_reviewed = sum(state[0] and state[1] for state in source_states)
        base_path = primary["path"] / "base" if shared else primary["path"]
        base_project = primary["project"] or {}
        game_definition = primary.get("game_definition") or {}
        base = {
            "cards_total": len(cards),
            "source_text_present": source_present,
            "source_reviewed": source_reviewed,
            "extraction_percent": round(source_present / len(cards) * 100) if cards else 0,
            "shoggoth_base_exists": any(
                (base_path / name).is_file() for name in ("shoggoth.json", "project.shoggoth")
            ),
            "graphics_reviewed": base_project.get("graphics_reviewed") is True,
        }
        translations: dict[str, dict[str, Any]] = {}
        for scenario in languages:
            language = scenario["storage_language"]
            if language == "source":
                continue
            language_cards = [card for card in scenario["grouped_cards"] if _active_sides(card)]
            states = [_card_text_status(card, field_ids) for card in language_cards]
            translation_dir = (
                scenario["language_project_path"].parent if scenario.get("shared_layout")
                else scenario["path"]
            )
            complete = sum(state[0] and state[2] for state in states)
            reviewed = sum(state[0] and state[2] and state[3] for state in states)
            translations[language] = {
                "cards_translated": complete,
                "cards_reviewed": reviewed,
                "cards_total": len(language_cards),
                "translation_percent": round(complete / len(language_cards) * 100)
                if language_cards else 0,
                "shoggoth_project_exists": any(
                    (translation_dir / name).is_file()
                    for name in ("project.shoggoth", "shoggoth.json")
                ),
                "shoggoth_export_generated": (translation_dir / "builds" / "shoggoth.json").is_file(),
                "rendered_images": _rendered_card_count(translation_dir / "renders"),
                "final_reviewed": (scenario.get("project") or {}).get("final_reviewed") is True,
            }
            if shared and translations[language]["final_reviewed"]:
                prefix = f"{game}-{campaign}-{slug}" if campaign else f"{game}-{slug}"
                translations[language]["release_asset"] = f"{prefix}-{language}.zip"
        project_id = f"{game}/{campaign}/{slug}" if campaign else f"{game}/{slug}"
        rows.append({
            "id": project_id,
            "game": game,
            "game_name": str(game_definition.get("name") or game),
            "campaign": campaign or None,
            "campaign_title": primary.get("campaign_title") or "",
            "slug": slug,
            "title": primary["title"],
            "description": str(base_project.get("description") or ""),
            "author": str(base_project.get("author") or ""),
            "version": str(base_project.get("version") or ""),
            "source_url": str(base_project.get("source_url") or ""),
            "source_language": str(base_project.get("source_language") or "en"),
            "mood_image": str(base_project.get("mood_image") or ""),
            "cover_image": str(base_project.get("cover_image") or ""),
            "layout": "shared" if shared else "legacy",
            "base": base,
            "translations": translations,
        })
    return {"format": "card-translator-status", "format_version": 1, "projects": rows}
