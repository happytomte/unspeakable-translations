from __future__ import annotations

import json
import re
import shutil
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from card_translator.arkham_reference import INDEX_FILENAME, find_arkham_references
from card_translator.config import gemini_model, translategemma_model
from card_translator.games import discover_games, get_game
from card_translator.providers.card_image import analyze_card_image, provider_model
from card_translator.providers.gemini import translate_prompt as gemini_translate_prompt
from card_translator.providers.google_translate import translate_text as google_translate_text
from card_translator.shared_projects import (
    BASE_FILENAME,
    CAMPAIGN_FILENAME,
    LANGUAGE_FILENAME,
    campaign_for_scenario,
    language_path,
    read_json,
    read_scenario_data,
    read_translation_document,
    shared_scenario_directories,
    write_json,
    write_scenario_data,
    write_translation_document,
)
from card_translator.translation import (
    TranslationReviewRequired,
    TranslationRule,
    finalize_external_translation,
    format_rules,
    merge_rules,
    parse_rules,
    prepare_external_translation,
    translate_texts,
)

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
IGNORED_SOURCE_DIRS = {"builds", "snapshots", "translations", ".git", ".venv", "__pycache__"}
GAME_SYSTEM_DIRS = {"data"}
LANGUAGE_CODE = re.compile(r"^[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*$")
LANGUAGE_DIR_CODE = re.compile(r"^[a-z]{2,3}(?:[-_][a-z0-9]{2,3})?$")
SOURCE_PROPOSALS_FILENAME = "source-proposals.json"


def _source_value_hash(value: Any) -> str:
    rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(rendered.encode("utf-8")).hexdigest()


def _source_proposals_path(scenario: dict[str, Any]) -> Path:
    translation_path = scenario.get("translation_path")
    if isinstance(translation_path, Path):
        return translation_path.parent / SOURCE_PROPOSALS_FILENAME
    return Path(scenario["path"]) / SOURCE_PROPOSALS_FILENAME


def _read_source_proposals(scenario: dict[str, Any]) -> dict[str, Any]:
    if scenario.get("source_only"):
        return {}
    return read_json(_source_proposals_path(scenario)) or {
        "format": "card-translator-source-proposals",
        "format_version": 1,
        "source_language": str((scenario.get("project") or {}).get("source_language") or "en"),
        "target_language": str(scenario.get("target_language") or ""),
        "cards": {},
    }


def _write_source_proposals(scenario: dict[str, Any], proposals: dict[str, Any]) -> None:
    cards = proposals.get("cards")
    path = _source_proposals_path(scenario)
    if not isinstance(cards, dict) or not cards:
        path.unlink(missing_ok=True)
        return
    write_json(path, proposals)


def _proposal_current_value(
    card: dict[str, Any], *, side: str, kind: str, field: str
) -> Any:
    if kind == "text":
        source_side = (card.get("source_sides") or {}).get(side) or {}
        value = source_side.get(field)
        if side == "front" and value is None:
            value = (card.get("source_text") or {}).get(field)
        return value if value is not None else ([] if field == "traits" else "")
    if field == "card_type":
        return str(card.get("card_type") or "")
    side_data = card.get("game_data") or {}
    if side == "back":
        side_data = ((side_data.get("sides") or {}).get("back") or {})
    return side_data.get(field)


def stage_card_source_proposal(
    root: Path,
    *,
    game: str,
    slug: str,
    project_language: str,
    card_key: str,
    side: str,
    text_values: dict[str, Any] | None = None,
    metadata_values: dict[str, Any] | None = None,
    source: str = "manual",
) -> dict[str, Any]:
    """Store translation-local source edits without changing the shared extraction."""
    scenario = get_scenario(root, game, slug, language=project_language)
    if scenario is None or not scenario.get("initialized"):
        raise ValueError("Scenario is not initialized")
    if scenario.get("source_only"):
        raise ValueError("Source proposals are only used in translation workspaces")
    if side not in {"front", "back"}:
        raise ValueError("side must be front or back")
    card = next(
        (item for item in scenario["grouped_cards"] if item["key"] == card_key),
        None,
    )
    if card is None:
        raise ValueError("Unknown logical card")

    proposals = _read_source_proposals(scenario)
    cards = proposals.setdefault("cards", {})
    card_proposal = cards.setdefault(card_key, {"sides": {}})
    side_proposal = card_proposal.setdefault("sides", {}).setdefault(side, {})
    changed: list[str] = []
    for kind, incoming in (("text", text_values or {}), ("metadata", metadata_values or {})):
        entries = side_proposal.setdefault(kind, {})
        for field, proposed_value in incoming.items():
            field = str(field)
            if kind == "text":
                definition = next(
                    (
                        item for item in scenario["translatable_fields"]
                        if str(item["id"]) == field
                    ),
                    None,
                )
                if definition is None:
                    continue
                if definition.get("type") == "list":
                    proposed_value = (
                        proposed_value
                        if isinstance(proposed_value, list)
                        else [
                            item.strip()
                            for item in re.split(r"[,;\n]", str(proposed_value))
                            if item.strip()
                        ]
                    )
                else:
                    proposed_value = str(proposed_value or "").strip()
                    if field == "title":
                        proposed_value = _normalize_unique_title(proposed_value)
            else:
                definition = next(
                    (
                        item for item in scenario["metadata_fields"]
                        if str(item["id"]) == field
                    ),
                    None,
                )
                if definition is None:
                    continue
                raw = str(proposed_value or "").strip()
                if (
                    definition.get("type") == "integer"
                    or definition.get("value_format") == "scaled_number"
                ) and raw in _PRINTED_EMPTY_NUMBER_MARKERS:
                    raw = ""
                if definition.get("value_format") == "scaled_number":
                    proposed_value = _normalize_scaled_number(raw, field_id=field)
                elif definition.get("type") == "integer":
                    try:
                        proposed_value = int(raw) if raw else None
                    except ValueError as exc:
                        raise ValueError(f"{field} must be a whole number") from exc
                elif definition.get("type") == "list":
                    proposed_value = [
                        item.strip() for item in re.split(r"[,;\n]", raw) if item.strip()
                    ]
                else:
                    proposed_value = raw
            current = _proposal_current_value(card, side=side, kind=kind, field=field)
            existing = entries.get(field)
            if proposed_value == current:
                entries.pop(field, None)
                continue
            entries[field] = {
                "base_value": (
                    existing.get("base_value")
                    if isinstance(existing, dict)
                    else current
                ),
                "base_hash": (
                    existing.get("base_hash")
                    if isinstance(existing, dict)
                    else _source_value_hash(current)
                ),
                "proposed_value": proposed_value,
                "source": source,
            }
            changed.append(f"{kind}.{field}")
        if not entries:
            side_proposal.pop(kind, None)
    if not side_proposal:
        card_proposal.get("sides", {}).pop(side, None)
    if not card_proposal.get("sides"):
        cards.pop(card_key, None)
    pending_fields = [
        f"{kind}.{field}"
        for kind, entries in side_proposal.items()
        if isinstance(entries, dict)
        for field in entries
    ]
    proposal_count = sum(
        len(entries)
        for proposed_side in card_proposal.get("sides", {}).values()
        if isinstance(proposed_side, dict)
        for entries in proposed_side.values()
        if isinstance(entries, dict)
    )
    _write_source_proposals(scenario, proposals)
    return {
        "changed_fields": changed,
        "pending_fields": pending_fields,
        "proposal_count": proposal_count,
    }


def _mark_source_status(
    root: Path,
    *,
    game: str,
    slug: str,
    project_language: str,
    card_key: str,
    side: str,
    status: str,
) -> None:
    scenario = get_scenario(root, game, slug, language=project_language)
    if scenario is None:
        return
    data = read_scenario_data(scenario)
    if data is None:
        return
    cards = data.setdefault("_meta", {}).setdefault("cards", {})
    cards.setdefault(card_key, {}).setdefault("source_status", {})[side] = status
    write_scenario_data(scenario, data)


def _append_translation_log(scenario_path: Path, event: dict[str, Any]) -> Path:
    path = scenario_path / "logs" / "translation.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {"at": datetime.now(UTC).isoformat(), **event},
                ensure_ascii=False,
            )
            + "\n"
        )
    return path


def normalize_language_code(value: str) -> str:
    code = value.strip().lower()
    if not code or len(code) > 24 or not LANGUAGE_CODE.fullmatch(code):
        raise ValueError("Language must be a short ISO-style code, for example de or pt-br")
    return code


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _humanize_slug(slug: str) -> str:
    return re.sub(r"\s+", " ", slug.replace("_", " ").replace("-", " ")).strip().title()


def _card_images(cards_dir: Path) -> list[Path]:
    if not cards_dir.exists() or not cards_dir.is_dir():
        return []
    return sorted(
        p
        for p in cards_dir.iterdir()
        if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
    )


def _is_ignored_source_path(path: Path, scenario_dir: Path) -> bool:
    try:
        relative = path.relative_to(scenario_dir)
    except ValueError:
        return True
    return any(part in IGNORED_SOURCE_DIRS or part.startswith(".") for part in relative.parts)


def _find_card_sources(scenario_dir: Path) -> list[dict[str, Any]]:
    """Find directories below a project root that directly contain card images.

    We intentionally keep the downloaded/source directory structure untouched. A source
    candidate is any non-generated directory that directly contains supported images.
    """
    candidates: list[dict[str, Any]] = []

    for directory in [scenario_dir, *sorted(p for p in scenario_dir.rglob("*") if p.is_dir())]:
        if _is_ignored_source_path(directory, scenario_dir):
            continue
        images = _card_images(directory)
        if not images:
            continue

        relative = directory.relative_to(scenario_dir)
        relative_text = relative.as_posix() if relative.parts else "."
        candidates.append(
            {
                "path": directory,
                "relative_path": relative_text,
                "label": "Projektordner" if relative_text == "." else relative_text,
                "image_count": len(images),
                "images": images,
            }
        )

    return sorted(candidates, key=lambda item: (-item["image_count"], item["relative_path"].lower()))


def _select_card_source(
    scenario_dir: Path,
    project: dict[str, Any] | None,
    candidates: list[dict[str, Any]],
) -> dict[str, Any] | None:
    # Once a project has an explicit source, never silently switch to another directory.
    if project and isinstance(project.get("card_source"), str):
        configured = project["card_source"].strip() or "."
        for candidate in candidates:
            if candidate["relative_path"] == configured:
                return candidate
        return None

    # Backwards-compatible convention from v0.1.
    for candidate in candidates:
        if candidate["relative_path"] == "cards":
            return candidate

    if not candidates:
        return None

    # Auto-select only if one candidate clearly has the largest direct image collection.
    largest = candidates[0]["image_count"]
    leaders = [candidate for candidate in candidates if candidate["image_count"] == largest]
    return leaders[0] if len(leaders) == 1 else None


def _translation_progress(path: Path | None) -> tuple[int, int]:
    if path is None:
        return (0, 0)
    data = _read_json(path)
    if not data:
        return (0, 0)
    cards = data.get("cards")
    if not isinstance(cards, list):
        return (0, 0)

    translated = 0
    total = len(cards)
    target_language = str(data.get("target_language") or data.get("language") or "de")
    for card in cards:
        if not isinstance(card, dict):
            continue
        text = card.get("text")
        if not isinstance(text, dict):
            continue
        localized = text.get(target_language)
        if isinstance(localized, dict) and any(
            isinstance(value, str) and value.strip() for value in localized.values()
        ):
            translated += 1
    return translated, total


def _scenario_summary(translation: dict[str, Any] | None) -> dict[str, Any] | None:
    if not translation or not isinstance(translation.get("cards"), list):
        return None
    records = [record for record in translation["cards"] if isinstance(record, dict)]
    if not records:
        return None

    def count_for(difficulty: str, role: str) -> int | None:
        relevant = [
            record
            for record in records
            if (record.get("game_data") or {}).get("deck_role") == role
        ]
        values = [
            (record.get("game_data") or {}).get("quantity", {}).get(difficulty)
            for record in relevant
        ]
        if not values or any(not isinstance(value, int) for value in values):
            return None
        return sum(values)

    return {
        "unique_card_count": len(records),
        "image_count": sum(
            1
            for record in records
            for side in ("front", "back")
            if (record.get("images") or {}).get(side)
        ),
        "quest_cards": {
            difficulty: count_for(difficulty, "quest_deck")
            for difficulty in ("normal", "easy", "nightmare")
        },
        "encounter_pool": {
            difficulty: count_for(difficulty, "encounter_deck")
            for difficulty in ("normal", "easy", "nightmare")
        },
        "campaign_component_count": sum(
            1
            for record in records
            if (record.get("game_data") or {}).get("deck_role") == "campaign_component"
        ),
    }



def _split_card_side(image: Path) -> tuple[str, str]:
    """Return a logical card key and side label for common fan-card filenames.

    Shoggoth exports ``_<front|back>_<version>``. A final ``-1`` / ``-2`` or
    ``-front`` / ``-back`` is also interpreted as front/back. Any earlier
    side-like suffix remains part of the card name, so filenames such as
    ``005 - Act 3-1-1.jpg`` and ``005 - Act 3-1-2.jpg`` still pair correctly.
    """
    shoggoth_match = re.match(
        r"^(?P<base>.+)_(?P<side>front|back)_\d+$",
        image.stem,
        flags=re.IGNORECASE,
    )
    if shoggoth_match:
        return shoggoth_match.group("base"), shoggoth_match.group("side").casefold()
    match = re.match(
        r"^(?P<base>.+)-(?P<side>1|2|front|back)$",
        image.stem,
        flags=re.IGNORECASE,
    )
    if not match:
        return image.stem, "front"
    side = match.group("side").casefold()
    return match.group("base"), "front" if side in {"1", "front"} else "back"


def _image_meta(translation: dict[str, Any] | None) -> dict[str, Any]:
    if not translation:
        return {}
    meta = translation.get("_meta")
    if not isinstance(meta, dict):
        return {}
    images = meta.get("images")
    return images if isinstance(images, dict) else {}


def _card_meta(translation: dict[str, Any] | None) -> dict[str, Any]:
    if not translation:
        return {}
    meta = translation.get("_meta")
    if not isinstance(meta, dict):
        return {}
    cards = meta.get("cards")
    return cards if isinstance(cards, dict) else {}


def _field_meta(translation: dict[str, Any] | None) -> dict[str, Any]:
    if not translation:
        return {}
    meta = translation.get("_meta")
    if not isinstance(meta, dict):
        return {}
    fields = meta.get("fields")
    return fields if isinstance(fields, dict) else {}


def _translatable_fields(game_definition: dict[str, Any]) -> list[dict[str, Any]]:
    schema = game_definition.get("card_schema")
    fields = schema.get("translatable_fields") if isinstance(schema, dict) else None
    if not isinstance(fields, list):
        return []
    return [
        field
        for field in fields
        if isinstance(field, dict)
        and isinstance(field.get("id"), str)
        and field.get("type") in {"text", "multiline", "list"}
    ]


def _metadata_fields(game_definition: dict[str, Any]) -> list[dict[str, Any]]:
    schema = game_definition.get("card_schema")
    fields = schema.get("metadata_fields") if isinstance(schema, dict) else None
    if not isinstance(fields, list):
        return []
    return [
        field
        for field in fields
        if isinstance(field, dict)
        and isinstance(field.get("id"), str)
        and field.get("type") in {"text", "integer", "list"}
    ]


def _card_types(game_definition: dict[str, Any]) -> list[dict[str, Any]]:
    schema = game_definition.get("card_schema")
    values = schema.get("card_types") if isinstance(schema, dict) else None
    return [
        value
        for value in (values if isinstance(values, list) else [])
        if isinstance(value, dict)
        and value.get("id")
        and isinstance(value.get("fields"), list)
    ]


def _visible_card_fields(game_definition: dict[str, Any], card_type: str) -> set[str]:
    normalized = card_type.casefold().replace("-", "_").replace(" ", "_")
    if normalized == "minicard":
        normalized = "mini_investigator"
    card_types = _card_types(game_definition)
    for definition in card_types:
        type_id = str(definition.get("id") or "").casefold()
        label = str(definition.get("label") or "").casefold().replace("-", "_").replace(" ", "_")
        if normalized in {type_id, label}:
            common_metadata = {
                str(field["id"])
                for field in _metadata_fields(game_definition)
                if field.get("common")
            }
            return {str(field) for field in definition["fields"]} | common_metadata | {"card_type"}
    if card_types:
        shared = set.intersection(
            *({str(field) for field in definition["fields"]} for definition in card_types)
        )
        shared.update(
            str(field["id"])
            for field in _metadata_fields(game_definition)
            if field.get("common")
        )
        return shared | {"card_type"}
    return {
        str(field["id"])
        for field in _translatable_fields(game_definition) + _metadata_fields(game_definition)
    }


def _group_cards(
    images: list[Path],
    translation: dict[str, Any] | None,
    game_definition: dict[str, Any],
) -> list[dict[str, Any]]:
    image_meta = _image_meta(translation)
    card_meta = _card_meta(translation)
    field_meta = _field_meta(translation)
    records_by_key: dict[str, dict[str, Any]] = {}
    records_by_image: dict[str, dict[str, Any]] = {}
    target_language = "de"
    source_language = "en"
    if translation:
        target_language = str(translation.get("target_language") or "de")
        source_language = str(translation.get("source_language") or "en")
        records = translation.get("cards")
        if isinstance(records, list):
            for record in records:
                if not isinstance(record, dict):
                    continue
                if isinstance(record.get("key"), str):
                    records_by_key[record["key"]] = record

                # Imported providers can tell us exactly which local files belong
                # to a logical card. Use those paths as a second, stronger matching
                # strategy in case a title/key was normalized differently.
                record_images = record.get("images")
                if isinstance(record_images, dict):
                    for side in ("front", "back"):
                        value = record_images.get(side)
                        if isinstance(value, str) and value.strip():
                            records_by_image[Path(value).name] = record
    grouped: dict[str, dict[str, Any]] = {}

    for image in images:
        card_key, side = _split_card_side(image)
        group = grouped.setdefault(
            card_key,
            {
                "key": card_key,
                "display_name": card_key,
                "front": None,
                "back": None,
            },
        )
        raw_meta = image_meta.get(image.name)
        side_meta = raw_meta if isinstance(raw_meta, dict) else {}
        group[side] = {
            "path": image,
            "name": image.name,
            "side": side,
            "display_rotation": int(side_meta.get("display_rotation", 0)) % 360,
            "hidden": bool(side_meta.get("hidden", False)),
        }

    for card_key, group in grouped.items():
        record = records_by_key.get(card_key)
        if record is None:
            for side in ("front", "back"):
                side_data = group.get(side)
                if isinstance(side_data, dict):
                    candidate = records_by_image.get(str(side_data.get("name") or ""))
                    if candidate is not None:
                        record = candidate
                        break
        if record is None:
            record = {}

        text = record.get("text") if isinstance(record, dict) else {}
        text = text if isinstance(text, dict) else {}
        source_text = (
            text.get(source_language) if isinstance(text.get(source_language), dict) else {}
        )
        target_text = text.get(target_language) if isinstance(text.get(target_language), dict) else {}
        game_data = record.get("game_data") if isinstance(record, dict) else {}
        game_data = game_data if isinstance(game_data, dict) else {}
        group["record"] = record
        stored_card_meta = card_meta.get(
            str(record.get("id") or record.get("key") or card_key), {}
        )
        group["source_status_by_side"] = (
            dict(stored_card_meta.get("source_status") or {})
            if isinstance(stored_card_meta, dict)
            else {}
        )
        group["card_type"] = str(record.get("card_type") or "") if isinstance(record, dict) else ""
        group["visible_field_ids"] = _visible_card_fields(
            game_definition, group["card_type"]
        )
        group["sphere"] = str(game_data.get("sphere") or "")
        group["card_subtype"] = str(game_data.get("card_subtype") or "")
        group["game_data"] = game_data
        side_game_data = game_data.get("sides") if isinstance(game_data.get("sides"), dict) else {}
        group["metadata_fields_by_side"] = {
            side: [
                {
                    **field,
                    "value": (
                        record.get(field["id"])
                        if field["id"] == "card_type"
                        else (
                            _compact_legacy_clues(
                                (side_game_data.get(side) or {})
                                if side == "back" and isinstance(side_game_data.get(side), dict)
                                else game_data
                            )
                            if field["id"] == "clues"
                            else (
                                (side_game_data.get(side) or {}).get(field["id"])
                                if side == "back" and isinstance(side_game_data.get(side), dict)
                                else game_data.get(field["id"])
                            )
                        )
                    ),
                }
                for field in _metadata_fields(game_definition)
            ]
            for side in ("front", "back")
        }
        group["metadata_fields"] = group["metadata_fields_by_side"]["front"]
        group["encounter_set"] = str(game_data.get("encounter_set") or "")
        group["set_name"] = str(game_data.get("set_name") or "")
        group["scenario_section"] = str(game_data.get("scenario_section") or "")
        group["card_number"] = game_data.get("card_number")
        group["printed_quantity"] = game_data.get("printed_quantity")
        group["deck_role"] = str(game_data.get("deck_role") or "")
        group["stats"] = game_data.get("stats") if isinstance(game_data.get("stats"), dict) else {}
        group["traits"] = game_data.get("traits") if isinstance(game_data.get("traits"), list) else []
        quantity = game_data.get("quantity")
        group["quantity"] = quantity if isinstance(quantity, dict) else {}
        group["product_printings"] = game_data.get("product_printings") if isinstance(game_data.get("product_printings"), list) else []
        group["identifiers"] = record.get("identifiers") if isinstance(record.get("identifiers"), dict) else {}
        group["source"] = record.get("source") if isinstance(record.get("source"), dict) else {}
        source_sides = source_text.get("sides") if isinstance(source_text.get("sides"), dict) else {}
        target_sides = target_text.get("sides") if isinstance(target_text.get("sides"), dict) else {}
        group["source_text"] = source_text
        group["target_text"] = target_text
        group["source_sides"] = source_sides
        group["target_sides"] = target_sides
        for side in ("front", "back"):
            if side not in group["source_status_by_side"] and isinstance(
                source_sides.get(side), dict
            ) and source_sides[side]:
                group["source_status_by_side"][side] = "captured"
        record_id = str(record.get("id") or record.get("key") or "")
        field_ids = [str(field["id"]) for field in _translatable_fields(game_definition)]
        group["source_side_review"] = {
            side: {
                field: field_meta.get(
                    f"{record_id}.text.{source_language}.sides.{side}.{field}", {}
                )
                for field in field_ids
            }
            for side in ("front", "back")
        }
        group["target_side_review"] = {
            side: {
                field: dict(
                    field_meta.get(
                        f"{record_id}.text.{target_language}.sides.{side}.{field}", {}
                    )
                )
                for field in field_ids
            }
            for side in ("front", "back")
        }
        for side in ("front", "back"):
            for field in field_ids:
                review = group["target_side_review"][side][field]
                expected_hash = review.get("source_hash")
                if not expected_hash:
                    continue
                source_value = (source_sides.get(side) or {}).get(field)
                if side == "front" and source_value is None:
                    source_value = source_text.get(field)
                if _source_value_hash(source_value) != expected_hash:
                    review["review_required"] = True
                    review["review_reason"] = "source_text_changed"
        group["matched"] = bool(record)

    return list(grouped.values())


def _attach_separate_card_art(
    grouped_cards: list[dict[str, Any]], scenario_dir: Path, project: dict[str, Any] | None
) -> None:
    if not project or project.get("use_separate_card_art") is not True:
        return
    mappings = project.get("card_art")
    if not isinstance(mappings, dict):
        return
    scenario_dir = scenario_dir.resolve()
    for card in grouped_cards:
        by_side = mappings.get(card["key"])
        if not isinstance(by_side, dict):
            continue
        resolved: dict[str, dict[str, Any]] = {}
        for side in ("front", "back"):
            relative = by_side.get(side)
            if not isinstance(relative, str) or not relative:
                continue
            path = (scenario_dir / relative).resolve()
            if scenario_dir not in path.parents or not path.is_file():
                continue
            resolved[side] = {"path": path, "name": path.name, "relative_path": relative}
        if resolved:
            card["separate_art"] = resolved


def _rules_from_data(data: dict[str, Any] | None, language: str) -> list[TranslationRule]:
    if not data:
        return []
    by_language = data.get("rules")
    values = by_language.get(language, []) if isinstance(by_language, dict) else []
    if not isinstance(values, list):
        return []
    return [
        TranslationRule(source=str(item["source"]), target=str(item["target"]))
        for item in values
        if isinstance(item, dict) and item.get("source") and item.get("target")
    ]


def _game_translation_rules(
    root: Path, *, game: str, language: str
) -> list[TranslationRule]:
    definition = get_game(root, game) or {}
    configured = definition.get("translation_rules")
    defaults = definition.get("default_translation_rules")
    global_values = configured if isinstance(configured, list) else defaults
    global_rules = [
        TranslationRule(source=str(item["source"]), target=str(item["target"]))
        for item in (global_values if isinstance(global_values, list) else [])
        if isinstance(item, dict) and item.get("source") and item.get("target")
    ]
    path = root / game / language / "translation_rules.json"
    stored = _read_json(path)
    if stored is not None:
        return merge_rules(global_rules, _rules_from_data(stored, language))
    return global_rules


def game_global_translation_rules(root: Path, *, game: str) -> dict[str, Any]:
    definition = get_game(root, game)
    if definition is None:
        raise ValueError("Unknown game")
    values = definition.get("translation_rules")
    if not isinstance(values, list):
        values = definition.get("default_translation_rules")
    rules = [
        TranslationRule(source=str(item["source"]), target=str(item["target"]))
        for item in (values if isinstance(values, list) else [])
        if isinstance(item, dict) and item.get("source") and item.get("target")
    ]
    return {"rules": rules, "text": format_rules(rules)}


def update_game_system_config(
    root: Path,
    *,
    game: str,
    translation_context: str,
    glossary_text: str,
    arkhamdb_urls_text: str = "",
) -> dict[str, Any]:
    definition = get_game(root, game)
    if definition is None:
        raise ValueError("Unknown game")
    rules = parse_rules(glossary_text)
    urls: dict[str, str] = {}
    for line_number, raw_line in enumerate(arkhamdb_urls_text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "|" not in line:
            raise ValueError(f"ArkhamDB URL on line {line_number} needs 'language | URL'")
        language, url = (part.strip() for part in line.split("|", 1))
        language = normalize_language_code(language)
        if not url.startswith("https://"):
            raise ValueError(f"ArkhamDB URL on line {line_number} must use HTTPS")
        urls[language] = url
    path = root / game / "game.json"
    # Persist the fully resolved definition so built-in defaults (such as Arkham's
    # card types) become visible and portable in the game's own config file.
    stored = dict(definition)
    stored["translation_context"] = translation_context.strip()
    stored["translation_rules"] = [rule.__dict__ for rule in rules]
    if game == "ah":
        stored["arkhamdb_urls"] = urls
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(stored, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"rules": rules, "arkhamdb_urls": urls}


def translation_rules(root: Path, *, game: str, slug: str, language: str) -> dict[str, Any]:
    language = normalize_language_code(language)
    scenario = get_scenario(root, game, slug, language=language)
    if scenario is None or not scenario["initialized"]:
        raise ValueError("Scenario is not initialized")
    if language != scenario["target_language"]:
        raise ValueError("Only the project's target language can be edited")
    game_rules = _game_translation_rules(root, game=game, language=language)
    project = scenario.get("project") if isinstance(scenario.get("project"), dict) else {}
    scenario_rules = _rules_from_data(project.get("translation_rules"), language)
    return {
        "game": game_rules,
        "scenario": scenario_rules,
        "merged": merge_rules(game_rules, scenario_rules),
        "game_text": format_rules(game_rules),
        "scenario_text": format_rules(scenario_rules),
    }


def game_translation_rules(root: Path, *, game: str, language: str) -> dict[str, Any]:
    language = normalize_language_code(language)
    if get_game(root, game) is None:
        raise ValueError("Unknown game")
    rules = _game_translation_rules(root, game=game, language=language)
    return {"rules": rules, "text": format_rules(rules), "language": language}


def update_game_translation_rules(
    root: Path, *, game: str, language: str, rules_text: str
) -> dict[str, Any]:
    language = normalize_language_code(language)
    if get_game(root, game) is None:
        raise ValueError("Unknown game")
    rules = parse_rules(rules_text)
    path = root / game / language / "translation_rules.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _read_json(path) or {
        "format": "card-translator-rules",
        "format_version": 1,
        "rules": {},
    }
    data.setdefault("rules", {})[language] = [rule.__dict__ for rule in rules]
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"rules": rules, "text": format_rules(rules), "language": language}


def update_translation_rules(
    root: Path,
    *,
    game: str,
    slug: str,
    language: str,
    game_rules_text: str | None,
    scenario_rules_text: str,
) -> dict[str, Any]:
    language = normalize_language_code(language)
    scenario = get_scenario(root, game, slug, language=language)
    if scenario is None or not scenario["initialized"]:
        raise ValueError("Scenario is not initialized")
    if language != scenario["target_language"]:
        raise ValueError("Only the project's target language can be edited")
    game_rules = (
        _game_translation_rules(root, game=game, language=language)
        if game_rules_text is None
        else parse_rules(game_rules_text)
    )
    scenario_rules = parse_rules(scenario_rules_text)

    if game_rules_text is not None:
        game_path = root / game / language / "translation_rules.json"
        game_path.parent.mkdir(parents=True, exist_ok=True)
        game_data = _read_json(game_path) or {
            "format": "card-translator-rules",
            "format_version": 1,
            "rules": {},
        }
        game_data.setdefault("rules", {})[language] = [rule.__dict__ for rule in game_rules]
        game_path.write_text(json.dumps(game_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    project_path = scenario["language_project_path"]
    project = _read_json(project_path) or {}
    scenario_rule_data = project.setdefault("translation_rules", {})
    scenario_rule_data.setdefault("format", "card-translator-rules")
    scenario_rule_data.setdefault("format_version", 1)
    scenario_rule_data.setdefault("rules", {})[language] = [rule.__dict__ for rule in scenario_rules]
    project_path.write_text(json.dumps(project, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "game": game_rules,
        "scenario": scenario_rules,
        "merged": merge_rules(game_rules, scenario_rules),
    }


def translate_missing_card_fields(
    root: Path,
    *,
    game: str,
    slug: str,
    card_key: str,
    language: str,
    provider: str = "translategemma",
) -> dict[str, Any]:
    """Translate and persist drafts for empty fields on both sides of one card."""
    language = normalize_language_code(language)
    scenario = get_scenario(root, game, slug, language=language)
    if scenario is None or not scenario["initialized"]:
        raise ValueError("Scenario is not initialized")
    if language != scenario["target_language"]:
        raise ValueError("Only the project's target language can be edited")

    data = read_scenario_data(scenario) or {}
    records = data.get("cards")
    if not isinstance(records, list):
        raise ValueError("Translation contains no card records")
    record = next(
        (
            item
            for item in records
            if isinstance(item, dict) and str(item.get("key") or "") == card_key
        ),
        None,
    )
    if record is None:
        raise ValueError("Unknown logical card")

    source_language = str((scenario.get("project") or {}).get("source_language") or "en")
    text = record.setdefault("text", {})
    source = text.get(source_language) if isinstance(text.get(source_language), dict) else {}
    target = text.setdefault(language, {})
    source_sides = source.get("sides") if isinstance(source.get("sides"), dict) else {}
    target_sides = target.setdefault("sides", {})

    jobs: list[tuple[str, str, str]] = []
    visible_fields = _visible_card_fields(
        scenario["game_definition"], str(record.get("card_type") or "")
    )
    for side in ("front", "back"):
        source_side = source_sides.get(side) if isinstance(source_sides.get(side), dict) else {}
        target_side = target_sides.setdefault(side, {})
        field_definitions = _translatable_fields(scenario["game_definition"])
        for field_definition in field_definitions:
            field = str(field_definition["id"])
            if field not in visible_fields:
                continue
            source_value = source_side.get(field)
            if side == "front" and not source_value:
                source_value = source.get(field)
            target_value = target_side.get(field)
            if side == "front" and not target_value:
                target_value = target.get(field)
            if isinstance(target_value, list):
                target_present = any(str(item).strip() for item in target_value)
            else:
                target_present = bool(str(target_value or "").strip())
            if target_present:
                continue
            if isinstance(source_value, list):
                source_string = ", ".join(str(item).strip() for item in source_value if str(item).strip())
            else:
                source_string = str(source_value or "").strip()
            if source_string:
                jobs.append((side, field, source_string))

    if not jobs:
        return {"translated_count": 0, "sides": {}}

    configured_rules = translation_rules(root, game=game, slug=slug, language=language)
    translation_log: list[dict[str, Any]] = []
    reference_sets = [
        find_arkham_references(
            root / game / "data" / INDEX_FILENAME,
            text=source_value,
            field=field,
            target_language=language,
            card_type=str(record.get("card_type") or ""),
        )
        if game == "ah" and source_language != language
        else []
        for _, field, source_value in jobs
    ]
    try:
        if provider not in {"translategemma", "google", "gemini"}:
            raise ValueError("Unknown translation provider")
        translated = translate_texts(
            [source_value for _, _, source_value in jobs],
            source_language=source_language,
            target_language=language,
            rules=configured_rules["merged"],
            game=game,
            game_context=str(scenario["game_definition"].get("translation_context") or ""),
            scenario_title=str(
                (scenario.get("project") or {}).get("translated_title")
                or (scenario.get("project") or {}).get("title")
                or scenario.get("title")
                or ""
            ),
            card_title=str(source.get("title") or record.get("name") or ""),
            fields=[field for _, field, _ in jobs],
            references=reference_sets,
            trace_log=translation_log,
            external_translator=google_translate_text if provider == "google" else None,
            prompt_translator=gemini_translate_prompt if provider == "gemini" else None,
            provider_name={
                "google": "google-cloud",
                "gemini": "gemini",
            }.get(provider, "translategemma"),
        )
    except TranslationReviewRequired as exc:
        exc.side, exc.field, _ = jobs[exc.text_index]
        for index, entry in enumerate(translation_log):
            if index < len(jobs):
                entry["side"] = jobs[index][0]
        for side, field, source_value in jobs[len(translation_log) :]:
            translation_log.append(
                {
                    "side": side,
                    "field": field,
                    "original": source_value,
                    "prepared_source": "",
                    "prompt": "",
                    "raw_translation": "",
                    "normalized_translation": "",
                    "final_translation": "",
                    "issues": [
                        {
                            "type": "not_run",
                            "message": "Not run because an earlier field requires review",
                        }
                    ],
                    "status": "not_run",
                }
            )
        exc.traces = translation_log
        _append_translation_log(
            scenario["path"],
            {"kind": "text_translation", "provider": provider, "card_key": card_key,
             "status": "review_required", "entries": translation_log},
        )
        raise

    card_id = str(record.get("id") or record.get("key") or card_key)
    fields_meta = data.setdefault("_meta", {}).setdefault("fields", {})
    result: dict[str, dict[str, str | list[str]]] = {}
    for (side, field, source_value), translated_value in zip(jobs, translated, strict=True):
        stored_value: str | list[str]
        field_definition = next(
            item for item in field_definitions if item["id"] == field
        )
        if field_definition["type"] == "list":
            stored_value = [
                value.strip()
                for value in re.split(r"[,;\n]", translated_value)
                if value.strip()
            ]
        else:
            stored_value = translated_value.strip()
        target_sides.setdefault(side, {})[field] = stored_value
        result.setdefault(side, {})[field] = stored_value
        provenance = {
            "source": "machine_translation",
            "provider": {"google": "google-cloud", "gemini": "gemini"}.get(
                provider, "translategemma"
            ),
            "model": (
                "nmt"
                if provider == "google"
                else gemini_model() if provider == "gemini" else translategemma_model()
            ),
            "review_required": True,
            "rule_scopes": ["game", "scenario"],
            "source_hash": _source_value_hash(source_value),
            "source_value": source_value,
        }
        fields_meta[f"{card_id}.text.{language}.sides.{side}.{field}"] = provenance
        if side == "front":
            target[field] = stored_value
            fields_meta[f"{card_id}.text.{language}.{field}"] = dict(provenance)

    write_scenario_data(scenario, data)
    for index, entry in enumerate(translation_log):
        if index < len(jobs):
            entry["side"] = jobs[index][0]
    _append_translation_log(
        scenario["path"],
        {"kind": "text_translation", "provider": provider, "card_key": card_key,
         "status": "translated", "entries": translation_log},
    )
    return {"translated_count": len(jobs), "sides": result, "translation_log": translation_log}


def apply_external_card_translation(
    root: Path,
    *,
    game: str,
    slug: str,
    card_key: str,
    language: str,
    side: str,
    field: str,
    draft: str,
) -> dict[str, Any]:
    """Normalize a pasted translation with the configured rules and persist it."""
    scenario = get_scenario(root, game, slug, language=language)
    if scenario is None or not scenario["initialized"]:
        raise ValueError("Scenario is not initialized")
    card = next(
        (item for item in scenario["grouped_cards"] if item["key"] == card_key), None
    )
    if card is None:
        raise ValueError("Unknown logical card")
    if side not in {"front", "back"}:
        raise ValueError("side must be front or back")
    if field not in {item["id"] for item in scenario["translatable_fields"]}:
        raise ValueError("Unknown translatable field")
    source_side = (card.get("source_sides") or {}).get(side) or {}
    original = source_side.get(field)
    if side == "front" and not original:
        original = (card.get("source_text") or {}).get(field)
    if isinstance(original, list):
        original = ", ".join(str(item) for item in original)
    original = str(original or "")
    rules = translation_rules(root, game=game, slug=slug, language=language)["merged"]
    normalized, issues = finalize_external_translation(original, draft, rules)
    if issues:
        return {"review_required": True, "original": original, "draft": normalized, "issues": issues}
    update_card_translation(
        root,
        game=game,
        slug=slug,
        card_key=card_key,
        language=language,
        side=side,
        title="",
        rules="",
        flavor="",
        traits="",
        field_values={field: normalized},
    )
    return {"review_required": False, "value": normalized}


def prepare_external_card_translation(
    root: Path,
    *,
    game: str,
    slug: str,
    card_key: str,
    language: str,
    side: str,
    field: str,
) -> dict[str, str]:
    scenario = get_scenario(root, game, slug, language=language)
    if scenario is None or not scenario["initialized"]:
        raise ValueError("Scenario is not initialized")
    card = next(
        (
            item
            for item in scenario["grouped_cards"]
            if item["key"] == card_key
            or str((item.get("record") or {}).get("key") or "") == card_key
        ),
        None,
    )
    if card is None:
        raise ValueError("Unknown logical card")
    if side not in {"front", "back"}:
        raise ValueError("side must be front or back")
    if field not in {item["id"] for item in scenario["translatable_fields"]}:
        raise ValueError("Unknown translatable field")
    source_side = (card.get("source_sides") or {}).get(side) or {}
    original = source_side.get(field)
    if side == "front" and not original:
        original = (card.get("source_text") or {}).get(field)
    if isinstance(original, list):
        original = ", ".join(str(item) for item in original)
    original = str(original or "")
    rules = translation_rules(root, game=game, slug=slug, language=language)["merged"]
    return {
        "original": original,
        "prepared": prepare_external_translation(original, rules),
    }


_ARKHAM_IMAGE_ICON_TOKENS = (
    "action", "agility", "auto_fail", "bless", "blessing", "blood", "bullet",
    "clue", "codex", "combat", "cultist", "curse", "damage", "day", "elder_sign",
    "elder_thing", "entry", "fail", "fast", "fleur", "free", "frost", "guardian",
    "horror", "intellect", "investigator", "knowledge", "mystic", "name", "night",
    "open", "per", "per_investigator", "reaction", "resolution", "resource", "rogue",
    "seal_a", "seal_b", "seal_c", "seal_d", "seal_e", "seeker", "skull", "star",
    "survivor", "tablet", *(f"tdc_rune_{letter}" for letter in "abcdefghijklmnopqrstuvwxyz"),
    "unique", "wild", "willpower",
)


def _compact_legacy_clues(side_data: dict[str, Any]) -> Any:
    """Expose legacy split clue values through the compact editor format."""
    clues = side_data.get("clues")
    if clues is None or clues == "":
        return clues
    has_per = str(side_data.get("clues_per_investigator") or "").casefold() in {
        "1", "true", "yes", "on",
    }
    clues_text = str(clues)
    if has_per and not re.search(
        r"<(?:per|per_investigator)>\s*$", clues_text, re.IGNORECASE
    ):
        return f"{clues_text}<per>"
    return clues


def _arkham_image_markup_guidance() -> str:
    tokens = ", ".join(f"{{{token}}}" for token in _ARKHAM_IMAGE_ICON_TOKENS)
    return (
        "Shoggoth markup rules for both source and translation values:\n"
        "- Styling tags are field-specific. Only rules and flavor may contain angle-bracket "
        "styling tags, and the only permitted ones there are <b>, <bi>, <i>, and "
        "<blockquote> with their matching closing tags. Title, subname, and traits must be "
        "plain text without any angle-bracket tags. Metadata must also be tag-free except that "
        "metadata.clues uses a numeric value optionally followed by <per>, such as 1<per>. "
        "Keep game "
        "icons as canonical {token} values. Location connections belong in metadata.connection "
        "and metadata.connections as canonical IDs without angle-bracket tags.\n"
        "- Preserve printed typography. Wrap bold text "
        "in <b>...</b> and text that is simultaneously bold and italic in <bi>...</bi>. "
        "Do not use Markdown asterisks and do not nest <b> and <bi>.\n"
        "- <blockquote>...</blockquote> is a visual transcription requirement, not an "
        "optional style choice. "
        "Look in the narrow gutter immediately LEFT of the narrative paragraph inside the "
        "text box. When TWO closely spaced vertical black strokes run beside that paragraph, "
        "put exactly one <blockquote> at the start of its first word and one </blockquote> "
        "after its final punctuation in BOTH source.flavor and translation.flavor. The strokes "
        "themselves are not text. Do not include a separate line below the paragraph, such as "
        "a resolution reference, inside the tags. A single card-frame border or italics alone "
        "does not qualify. If the two strokes are not visible, leave <blockquote> out of both "
        "flavor values.\n"
        "- A standalone paragraph printed entirely in italics is normally flavor text: put it "
        "in the flavor field, not rules. Keep it in rules only when the layout clearly makes it "
        "part of a game instruction, labeled effect, or quotation embedded in rules text.\n"
        "- Convert every visible printed game icon to its canonical braced token. Use only a "
        f"matching token from this vocabulary: {tokens}. Never spell out an icon and never "
        "invent an icon that is not visible.\n"
        "- Distinguish the two ability icons by their printed shape: a lightning bolt is "
        "{fast}; use {action} only for the action-arrow glyph. Never turn a lightning bolt "
        "into {action}.\n"
        "- A small investigator/person silhouette immediately after a number is "
        "{per_investigator}. This remains true before words such as clues or damage: for "
        "example, printed '1 [investigator silhouette] damage' is "
        "'1{per_investigator} damage', not '1 {damage} damage'. Use {damage} only when an "
        "actual damage icon is visibly printed; do not infer it from the word 'damage'.\n"
        "- A small diamond-shaped list marker before a rules effect is the {bullet} icon. "
        "Transcribe it as {bullet}, never as a Unicode diamond such as ◆, ◇, ◊, or ◈. "
        "Keep one {bullet} at the start of each printed bulleted effect.\n"
        "- A visibly printed resolution arrow before an R-number is the {resolution} icon: "
        "preserve the surrounding parentheses and the printed number, for example "
        "'(→R1)' becomes '({resolution}R1)' in source.rules and translation.rules. "
        "Keep this separate from a preceding narrative <blockquote>.\n"
        "- Preserve meaningful paragraph breaks, line breaks, quotation marks, dashes, bullets, "
        "numbers, and punctuation. Keep markup balanced and place punctuation inside or outside "
        "a tag exactly as it is formatted in print."
    )


def _card_image_shared_prompt(
    scenario: dict[str, Any], rules: list[TranslationRule], game: str
) -> str:
    source_language = str(
        (scenario.get("project") or {}).get("source_language") or "en"
    )
    language_guidance = f"Source language: {source_language}."
    if not scenario.get("source_only"):
        language_guidance += (
            f" Target language: {scenario['target_language']}. Write every value in the "
            "translation object exclusively in that target language."
        )
    markup_guidance = (
        f"\n\n{_arkham_image_markup_guidance()}" if game.casefold() == "ah" else ""
    )
    return (
        f"Languages:\n{language_guidance}\n\n"
        "Game translation guidance:\n"
        f"{scenario['game_definition'].get('translation_context', '')}\n\n"
        f"Mandatory glossary:\n{format_rules(rules)}"
        f"{markup_guidance}"
        "\n\nGeneral extraction and output rules:\n"
        "- Extract only visibly printed values. Use an empty string for absent or unreadable "
        "fields and never invent values. Traits are a comma-separated string.\n"
        "- Return only a JSON object matching the current job's output_schema, without a "
        "Markdown fence or additional keys. Encode line breaks inside JSON strings as \\n.\n"
        "- Inspect the small-print footer separately. Put the illustrator in metadata.artist "
        "without an 'Illus.' prefix and preserve a visible copyright line including ©. Extract "
        "printed card, collection, encounter, and quantity numbers only into matching fields; "
        "do not infer them from artwork or unidentified icons.\n"
        "- For every leading structural rules label followed by a dash or colon, preserve its "
        "printed weight with <b>...</b> in both languages; the separator stays outside the tag."
    )


def _gemini_card_image_request(
    root: Path,
    *,
    game: str,
    slug: str,
    project_language: str,
    card_key: str,
    side: str,
    include_shared_context: bool = True,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], str]:
    scenario = get_scenario(root, game, slug, language=project_language)
    if scenario is None or not scenario["initialized"]:
        raise ValueError("Scenario is not initialized")
    card = next((item for item in scenario["grouped_cards"] if item["key"] == card_key), None)
    if card is None or not isinstance(card.get(side), dict):
        raise ValueError("Unknown card side")
    visible = set(card.get("visible_field_ids") or [])
    unknown_card_type = not str(card.get("card_type") or "").strip()
    detect_card_type = side == "front" and unknown_card_type
    if unknown_card_type:
        visible.update(
            str(field_id)
            for card_type in scenario.get("card_types") or []
            for field_id in card_type.get("fields") or []
        )
    text_fields = [
        field for field in scenario["translatable_fields"] if field["id"] in visible
    ]
    metadata_fields = [
        field
        for field in scenario["metadata_fields"]
        if field["id"] in visible and (field["id"] != "card_type" or detect_card_type)
    ]
    def string_properties(
        fields: list[dict[str, Any]], *, translatable: bool
    ) -> dict[str, dict[str, str]]:
        properties: dict[str, dict[str, str]] = {}
        for field in fields:
            field_id = str(field["id"])
            if translatable and field_id in {"rules", "flavor"}:
                description = (
                    "May use only the balanced styling tags <b>, <bi>, <i>, and "
                    "<blockquote>; game icons use canonical {token} notation."
                )
            elif translatable:
                description = (
                    "Plain text only: no angle-bracket tags. Game icons, if printed, use "
                    "canonical {token} notation."
                )
            else:
                description = (
                    "Structured metadata value without angle-bracket tags."
                    if field_id != "clues"
                    else "Whole-number clue value, optionally followed by <per>, for example 1<per>."
                )
            properties[field_id] = {"type": "string", "description": description}
        return properties

    metadata_properties = string_properties(metadata_fields, translatable=False)
    card_type_ids = [
        str(card_type["id"])
        for card_type in scenario.get("card_types") or []
        if card_type.get("id")
    ]
    if detect_card_type and card_type_ids:
        metadata_properties["card_type"] = {"type": "string", "enum": card_type_ids}
    metadata_by_id = {str(field["id"]): field for field in metadata_fields}
    location_symbols = [
        str(option)
        for option in (metadata_by_id.get("connection") or {}).get("options") or []
    ]
    if location_symbols and "connection" in metadata_properties:
        metadata_properties["connection"] = {
            "type": "string",
            "enum": ["", *location_symbols],
        }
    source_only = bool(scenario.get("source_only"))
    source_properties = string_properties(text_fields, translatable=True)

    def object_schema(properties: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        }

    schema = {
        "type": "object",
        "properties": {
            "source": object_schema(source_properties),
            "metadata": object_schema(metadata_properties),
        },
        "required": ["source", "metadata"],
        "additionalProperties": False,
    }
    if not source_only:
        schema["properties"]["translation"] = object_schema(source_properties)
        schema["required"].insert(1, "translation")
    if side == "back":
        schema["properties"]["standard_back"] = {"type": "boolean"}
        schema["required"].append("standard_back")
    project = scenario.get("project") or {}
    rules = (
        []
        if scenario.get("source_only")
        else translation_rules(
            root, game=game, slug=slug, language=scenario["target_language"]
        )["merged"]
    )
    shared_prompt = _card_image_shared_prompt(scenario, rules, game)
    output_shape: dict[str, Any] = {
        "source": {field["id"]: "" for field in text_fields},
        "metadata": {field["id"]: "" for field in metadata_fields},
    }
    if not source_only:
        output_shape["translation"] = {field["id"]: "" for field in text_fields}
    if side == "back":
        output_shape["standard_back"] = False
    blockquote_priority = (
        "Before transcribing, inspect each narrative paragraph for two closely spaced "
        "vertical strokes immediately to its left inside the text box. If and only if they "
        "are visibly printed, wrap that entire paragraph in <blockquote>...</blockquote> "
        "in both source.flavor and translation.flavor. Keep any separate rules paragraph "
        "outside the tags. If the strokes are absent, omit the tags.\n\n"
        if include_shared_context
        and game.casefold() == "ah"
        and "flavor" in {str(field["id"]) for field in text_fields}
        else ""
    )
    task = (
        f"transcribe the printed {project.get('source_language', 'en')} text"
        if source_only
        else (
            f"extract visible printed values and translate from "
            f"{project.get('source_language', 'en')} to {scenario['target_language']}"
        )
    )
    prompt = (
        f"{blockquote_priority}"
        f"Read this {card.get('card_type') or 'unknown-type'} card side for "
        f"{scenario['game_definition'].get('name')}. Extract only visible printed values and "
        f"{task}. "
        "Preserve paragraph breaks and represent printed game icons as {token_name}.\n\n"
        f"{shared_prompt if include_shared_context else 'Apply the shared instructions from context.json.'}"
    )
    prompt += (
        "\n\nInspect the actual card image carefully and rotate it mentally or with an image "
        "viewer until every printed region is readable. Do not infer content from the filename, "
        "job name, or an existing translation. Inspect the title area, text box, numerical and "
        "icon values, and the small-print footer separately. Transcribe every visible field "
        "represented in the schema, including all rules and flavor text and the illustrator; "
        "use an empty string only when that field is genuinely absent or unreadable. Do not "
        "finish the job after recognizing only its title or card type."
    )
    if include_shared_context:
        prompt += (
            "\n\nReturn only JSON, without a Markdown code fence, using exactly this shape and "
            "only the listed keys. Escape every straight double quote inside a JSON string as "
            "\\\" and encode line breaks inside strings as \\n:\n"
            f"{json.dumps(output_shape, ensure_ascii=False)}"
        )
    else:
        prompt += "\n\nReturn only JSON matching job.output_schema exactly."
    if detect_card_type:
        prompt += (
            "\nThe card type has not been set yet. Identify it from the printed card design and "
            "content. Set metadata.card_type to exactly one of these canonical IDs: "
            f"{', '.join(card_type_ids)}. Fill fields relevant to that type and leave fields for "
            "other types empty."
        )
    if "clues" in {str(field["id"]) for field in metadata_fields}:
        prompt += (
            "\nFor clue values, metadata.clues must contain the whole number, optionally "
            "followed immediately by <per>. "
            "If a standalone dash is printed instead of a clue number, use an empty string. If the "
            "per-investigator symbol is visibly printed beside it, use a compact value such as "
            "1<per>; otherwise use only the number."
        )
    prompt += (
        "\nFor every numeric metadata field, a printed standalone dash means no value: return "
        "an empty string rather than the dash."
    )
    if {"stage", "index"}.issubset(metadata_by_id):
        prompt += (
            "\nFor Act and Agenda cards, metadata.index is the complete printed designation, "
            "including its number and side letter: use '1a', '1b', '2a', etc. Never split '1a' "
            "into metadata.stage='1' and metadata.index='a'. metadata.stage may contain the "
            "numeric sequence value, but metadata.index must still contain the complete '1a'."
        )
    if location_symbols and {"connection", "connections"}.issubset(metadata_by_id):
        prompt += (
            "\nFor a Location, recognize its printed location connection glyphs. "
            "metadata.connection is the location's own single glyph; metadata.connections "
            "contains the glyphs reachable from it as comma-separated canonical IDs. Use only: "
            f"{', '.join(location_symbols)}. These are location glyphs, not chaos tokens: do not "
            "wrap them in braces, do not return names such as skull, cultist, tablet, or bless, "
            "and omit blank/empty icon positions."
        )
    numeric_icon_fields = [
        field
        for field in ("willpower", "intellect", "combat", "agility", "wild")
        if field in metadata_by_id
    ]
    if numeric_icon_fields:
        prompt += (
            "\nCount the printed skill icons individually and return only their whole-number "
            f"counts in these fields: {', '.join(f'metadata.{field}' for field in numeric_icon_fields)}. "
            "Use an empty string when that icon is absent, not a guessed zero."
        )
    if detect_card_type or str(card.get("card_type") or "").casefold() == "chaos":
        prompt += (
            "\nIf this is a Chaos card, write every chaos-table entry on its own line in both "
            "source.rules and translation.rules. Begin each line with all visibly printed chaos "
            "tokens in braces followed by a colon and its text, for example "
            "'{cultist}{tablet}: -4 text'. Preserve the same tokens in the translation. Do not "
            "merge entries or omit their modifiers."
        )
    if side == "back":
        prompt += (
            "\nSet standard_back to true only when this image is a generic reusable card back "
            "with no card-specific title, rules, values, or narrative content. A distinct reverse "
            "side of an Act, Agenda, Location, Investigator, Story, or other double-sided card is "
            "not a standard back."
        )
    return scenario, card, schema, prompt


_ARKHAM_REFERENCE_TOKEN = re.compile(r"\[([a-z0-9_]+)\]", re.IGNORECASE)


def _normalize_arkham_reference_tokens(value: str) -> str:
    allowed = {str(token).casefold() for token in _ARKHAM_IMAGE_ICON_TOKENS}

    def replace(match: re.Match[str]) -> str:
        token = match.group(1).casefold()
        return f"{{{token}}}" if token in allowed else match.group(0)

    return _ARKHAM_REFERENCE_TOKEN.sub(replace, value)


def _card_image_references(
    root: Path,
    *,
    game: str,
    scenario: dict[str, Any],
    card: dict[str, Any],
    side: str,
) -> dict[str, list[dict[str, str]]]:
    if game.casefold() != "ah":
        return {}
    index_path = root / game / "data" / INDEX_FILENAME
    source_side = ((card.get("source_sides") or {}).get(side) or {})
    fallback = card.get("source_text") or {} if side == "front" else {}
    references: dict[str, list[dict[str, str]]] = {}
    for field in scenario["translatable_fields"]:
        field_id = str(field["id"])
        if field_id not in {"rules", "traits"}:
            continue
        value = source_side.get(field_id)
        if not value:
            value = fallback.get(field_id)
        if isinstance(value, list):
            text = ", ".join(str(item) for item in value if str(item).strip())
        else:
            text = str(value or "").strip()
        if not text:
            continue
        matches = find_arkham_references(
            index_path,
            text=text,
            field=field_id,
            target_language=scenario["target_language"],
            card_type=str(card.get("card_type") or ""),
            limit=2 if field_id == "rules" else 1,
        )
        if matches:
            references[field_id] = [
                {
                    "card_name": match.card_name,
                    "card_type": match.card_type,
                    "source": _normalize_arkham_reference_tokens(match.source),
                    "target": _normalize_arkham_reference_tokens(match.target),
                }
                for match in matches
            ]
    return references


def gemini_card_image_prompt(
    root: Path,
    *,
    game: str,
    slug: str,
    project_language: str,
    card_key: str,
    side: str,
) -> dict[str, str]:
    _scenario, card, _schema, prompt = _gemini_card_image_request(
        root,
        game=game,
        slug=slug,
        project_language=project_language,
        card_key=card_key,
        side=side,
    )
    return {
        "prompt": prompt,
        "image_name": str(card[side].get("name") or Path(card[side]["path"]).name),
    }


def prepare_card_image_batch(
    root: Path,
    *,
    game: str,
    slug: str,
    project_language: str,
    force_image_analysis: bool = False,
    extracted_data_only: bool = False,
) -> dict[str, Any]:
    """Write one structured LLM job per visible card side.

    Prompts live outside ``source/`` so they remain local working files when a
    shared project is published. The manifest preserves the unambiguous mapping
    from the prompt filename to its logical card and side. Jobs whose source
    fields were already captured intentionally omit the source image entirely,
    unless image analysis was explicitly requested.
    """
    scenario = get_scenario(root, game, slug, language=project_language)
    if scenario is None or not scenario["initialized"]:
        raise ValueError("Scenario is not initialized")
    if force_image_analysis and extracted_data_only:
        raise ValueError("Batch cannot require images and extracted data only")
    batch_directory = Path(scenario["path"]) / "llm-batch" / scenario["target_language"]
    batch_directory.mkdir(parents=True, exist_ok=True)

    # This directory is owned by the generator. Remove jobs from an earlier run,
    # including the legacy plain-text format, so stale card sides cannot remain.
    for pattern in ("*.job.json", "*.txt"):
        for old_job in batch_directory.glob(pattern):
            old_job.unlink()

    rules = (
        []
        if scenario.get("source_only")
        else translation_rules(
            root, game=game, slug=slug, language=scenario["target_language"]
        )["merged"]
    )
    context_name = "context.json"
    write_json(
        batch_directory / context_name,
        {
            "format": "card-translator-llm-context",
            "format_version": 1,
            "game": game,
            "scenario": slug,
            "project_language": scenario["storage_language"],
            "source_language": str(
                (scenario.get("project") or {}).get("source_language") or "en"
            ),
            "target_language": scenario["target_language"],
            "prompt": _card_image_shared_prompt(scenario, rules, game),
        },
    )

    jobs: list[dict[str, str]] = []
    skipped_hidden = 0
    text_only = 0
    image_jobs = 0
    project_path = Path(scenario["path"])
    for card in scenario["grouped_cards"]:
        for side in ("front", "back"):
            image = card.get(side)
            if not isinstance(image, dict):
                continue
            if image.get("hidden") is True:
                skipped_hidden += 1
                continue
            source_side = (card.get("source_sides") or {}).get(side) or {}
            target_side = (card.get("target_sides") or {}).get(side) or {}
            source_status = (card.get("source_status_by_side") or {}).get(side)
            has_captured_source = (
                source_status in {"captured", "reviewed"} and bool(source_side)
            )
            if extracted_data_only and not has_captured_source:
                continue
            if (
                scenario.get("source_only")
                and source_status in {"captured", "reviewed"}
                and not force_image_analysis
            ):
                continue
            use_text_only = (
                not scenario.get("source_only")
                and not force_image_analysis
                and has_captured_source
            )
            mode = "text_translation" if use_text_only else "image_extraction"
            if use_text_only:
                visible = set(card.get("visible_field_ids") or [])
                field_ids = [
                    str(field["id"])
                    for field in scenario["translatable_fields"]
                    if field["id"] in visible
                    and source_side.get(field["id"]) not in (None, "", [])
                    and target_side.get(field["id"]) in (None, "", [])
                ]
                if not field_ids:
                    continue
                schema = {
                    "type": "object",
                    "properties": {
                        "translation": {
                            "type": "object",
                            "properties": {
                                field: {
                                    "type": "string",
                                    "description": (
                                        "Translated field. Preserve canonical {token} values "
                                        "and balanced Shoggoth markup."
                                    ),
                                }
                                for field in field_ids
                            },
                            "required": field_ids,
                            "additionalProperties": False,
                        }
                    },
                    "required": ["translation"],
                    "additionalProperties": False,
                }
                source_payload = {
                    field: (
                        ", ".join(str(item) for item in source_side[field])
                        if isinstance(source_side[field], list)
                        else str(source_side[field])
                    )
                    for field in field_ids
                }
                prompt = (
                    "This is a text-only translation job. Do not open or inspect the card image. "
                    f"Translate these extracted {scenario['project'].get('source_language', 'en')} "
                    f"fields to {scenario['target_language']}: "
                    f"{json.dumps(source_payload, ensure_ascii=False)}. "
                    "Use context.json for terminology and formatting. Preserve every canonical "
                    "{token}, HTML-style formatting tag, paragraph break, number, and game term. "
                    "Return only JSON matching job.output_schema exactly."
                )
                request_card = card
                text_only += 1
            else:
                _scenario, request_card, schema, prompt = _gemini_card_image_request(
                    root,
                    game=game,
                    slug=slug,
                    project_language=project_language,
                    card_key=str(card["key"]),
                    side=side,
                    include_shared_context=False,
                )
                image_jobs += 1
            image_path = Path(image["path"])
            job_name = f"{image_path.stem}.job.json"
            try:
                relative_image = image_path.relative_to(project_path).as_posix()
            except ValueError:
                relative_image = str(image_path)
            references = _card_image_references(
                root,
                game=game,
                scenario=scenario,
                card=request_card,
                side=side,
            )
            if references:
                prompt += (
                    "\nUse the small set of official Arkham translation references in "
                    "this job for terminology and phrasing. They are examples only: translate "
                    "the actual printed card and do not copy unrelated content."
                )
            job = {
                "format": "card-translator-llm-job",
                "format_version": 2,
                "game": game,
                "scenario": slug,
                "project_language": scenario["storage_language"],
                "card_key": str(card["key"]),
                "side": side,
                "mode": mode,
                "source_language": str(
                    (scenario.get("project") or {}).get("source_language") or "en"
                ),
                "target_language": scenario["target_language"],
                "context": context_name,
                "prompt": prompt,
                "output_schema": schema,
                "references": references,
                "result": f"{image_path.stem}.result.json",
            }
            if not use_text_only:
                job["image"] = relative_image
            write_json(batch_directory / job_name, job)
            manifest_job = {
                "card_key": str(card["key"]),
                "side": side,
                "job": job_name,
            }
            if not use_text_only:
                manifest_job["image"] = relative_image
            jobs.append(manifest_job)

    manifest_path = batch_directory / "manifest.json"
    write_json(
        manifest_path,
        {
            "format": "card-translator-llm-batch",
            "format_version": 2,
            "game": game,
            "scenario": slug,
            "project_language": scenario["storage_language"],
            "source_language": str(
                (scenario.get("project") or {}).get("source_language") or "en"
            ),
            "target_language": scenario["target_language"],
            "context": context_name,
            "jobs": jobs,
        },
    )
    return {
        "count": len(jobs),
        "text_only": text_only,
        "image_jobs": image_jobs,
        "skipped_hidden": skipped_hidden,
        "path": manifest_path.parent.relative_to(project_path).as_posix(),
        "manifest": manifest_path.relative_to(project_path).as_posix(),
    }


def _validate_card_image_batch_result(
    result: dict[str, Any], schema: dict[str, Any], result_name: str
) -> None:
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        raise ValueError(f"{result_name}: job contains no usable output schema")
    expected = set(properties)
    required = set(schema.get("required") or [])
    actual = set(result)
    if not required.issubset(actual) or not actual.issubset(expected):
        missing = sorted(required - actual)
        unknown = sorted(actual - expected)
        detail = []
        if missing:
            detail.append(f"missing {', '.join(missing)}")
        if unknown:
            detail.append(f"unknown {', '.join(unknown)}")
        raise ValueError(f"{result_name}: invalid result keys ({'; '.join(detail)})")

    for section in ("source", "translation", "metadata"):
        if section not in properties:
            continue
        value = result.get(section)
        definition = properties.get(section)
        fields = definition.get("properties") if isinstance(definition, dict) else None
        if not isinstance(value, dict) or not isinstance(fields, dict):
            raise ValueError(f"{result_name}: {section} must be an object")
        expected_fields = set(fields)
        required_fields = set(definition.get("required") or [])
        actual_fields = set(value)
        if not required_fields.issubset(actual_fields) or not actual_fields.issubset(expected_fields):
            missing = sorted(required_fields - actual_fields)
            unknown = sorted(actual_fields - expected_fields)
            detail = []
            if missing:
                detail.append(f"missing {', '.join(missing)}")
            if unknown:
                detail.append(f"unknown {', '.join(unknown)}")
            raise ValueError(
                f"{result_name}: invalid {section} keys ({'; '.join(detail)})"
            )
        if any(not isinstance(item, str) for item in value.values()):
            raise ValueError(f"{result_name}: every {section} value must be a string")

    if "standard_back" in properties and not isinstance(result.get("standard_back"), bool):
        raise ValueError(f"{result_name}: standard_back must be true or false")
    _validate_card_image_markup(result, result_name=result_name)


_ANGLE_TAG = re.compile(r"</?[A-Za-z][^<>]*>")
_ALLOWED_TEXT_TAGS = {
    "<b>",
    "</b>",
    "<bi>",
    "</bi>",
    "<i>",
    "</i>",
    "<blockquote>",
    "</blockquote>",
}
_UNIQUE_TITLE_PREFIX = re.compile(
    r"^\s*(?:<unique>|\{unique\})\s*", re.IGNORECASE
)


def _normalize_unique_title(value: str) -> str:
    return _UNIQUE_TITLE_PREFIX.sub("{unique}", value.strip(), count=1)


def _validate_card_image_markup(
    result: dict[str, Any], *, result_name: str = "LLM result"
) -> None:
    for section in ("source", "translation"):
        values = result.get(section)
        if not isinstance(values, dict):
            continue
        for field, value in values.items():
            if not isinstance(value, str):
                continue
            tags = _ANGLE_TAG.findall(value)
            if not tags:
                continue
            if field == "title" and all(tag.casefold() == "<unique>" for tag in tags):
                if _UNIQUE_TITLE_PREFIX.match(value):
                    continue
            if field not in {"rules", "flavor"}:
                raise ValueError(
                    f"{result_name}: {section}.{field} must not contain angle-bracket tags"
                )
            unsupported = [tag for tag in tags if tag.casefold() not in _ALLOWED_TEXT_TAGS]
            if unsupported:
                raise ValueError(
                    f"{result_name}: {section}.{field} contains unsupported tag "
                    f"{unsupported[0]}"
                )
    metadata = result.get("metadata")
    if isinstance(metadata, dict):
        for field, value in metadata.items():
            if isinstance(value, str) and _ANGLE_TAG.search(value):
                if re.fullmatch(
                    r"\s*(?:[+-]?\d+|x)\s*<(?:per|per_investigator)>\s*",
                    value,
                    re.IGNORECASE,
                ):
                    continue
                raise ValueError(
                    f"{result_name}: metadata.{field} must not contain angle-bracket tags"
                )


def apply_card_image_batch_results(
    root: Path,
    batch_directory: Path,
) -> dict[str, Any]:
    """Apply valid LLM batch results independently and report per-card failures."""
    root = root.resolve()
    batch_directory = batch_directory.resolve()
    if root != batch_directory and root not in batch_directory.parents:
        raise ValueError("Batch directory must be inside the configured library")
    manifest = read_json(batch_directory / "manifest.json")
    if manifest is None or manifest.get("format") != "card-translator-llm-batch":
        raise ValueError("Batch manifest is missing or invalid")
    game = str(manifest.get("game") or "")
    slug = str(manifest.get("scenario") or "")
    language = normalize_language_code(str(manifest.get("target_language") or ""))
    project_language = normalize_language_code(
        str(manifest.get("project_language") or language)
    )
    scenario = get_scenario(root, game, slug, language=project_language)
    if scenario is None or not scenario.get("initialized"):
        raise ValueError("Batch project could not be found in the configured library")
    expected_directory = Path(scenario["path"]) / "llm-batch" / language
    if batch_directory != expected_directory.resolve():
        raise ValueError("Batch directory does not match its project manifest")

    raw_jobs = manifest.get("jobs")
    if not isinstance(raw_jobs, list):
        raise ValueError("Batch manifest contains no jobs")
    prepared: list[tuple[str, str, str, str, dict[str, Any]]] = []
    errors: list[dict[str, Any]] = []
    for entry in raw_jobs:
        if not isinstance(entry, dict):
            raise ValueError("Batch manifest contains an invalid job entry")
        card_key = str(entry.get("card_key") or "")
        side = str(entry.get("side") or "")
        job_name = str(entry.get("job") or "")
        if not job_name or Path(job_name).name != job_name:
            raise ValueError("Batch manifest contains an invalid job filename")
        job = read_json(batch_directory / job_name)
        if job is None or job.get("format") != "card-translator-llm-job":
            errors.append({
                "card_key": card_key,
                "side": side,
                "result_name": "",
                "error": f"{job_name}: job is missing or invalid",
                "result_json": "",
            })
            continue
        job_card_key = str(job.get("card_key") or "")
        job_side = str(job.get("side") or "")
        if (
            job.get("game") != game
            or job.get("scenario") != slug
            or str(job.get("project_language") or language) != project_language
            or job.get("target_language") != language
            or card_key != job_card_key
            or side != job_side
            or job_side not in {"front", "back"}
        ):
            errors.append({
                "card_key": card_key or job_card_key,
                "side": side or job_side,
                "result_name": "",
                "error": f"{job_name}: job identity does not match the manifest",
                "result_json": "",
            })
            continue
        card_key = job_card_key
        side = job_side
        result_name = str(job.get("result") or "")
        if not result_name or Path(result_name).name != result_name:
            errors.append({
                "card_key": card_key,
                "side": side,
                "result_name": result_name,
                "error": f"{job_name}: invalid result filename",
                "result_json": "",
            })
            continue
        result_path = batch_directory / result_name
        result = read_json(batch_directory / result_name)
        if result is None:
            raw_result = ""
            try:
                raw_result = result_path.read_text(encoding="utf-8")
            except OSError:
                pass
            errors.append({
                "card_key": card_key,
                "side": side,
                "result_name": result_name,
                "error": f"{result_name}: result is missing or invalid JSON",
                "result_json": raw_result,
            })
            continue
        schema = job.get("output_schema")
        if not isinstance(schema, dict):
            errors.append({
                "card_key": card_key,
                "side": side,
                "result_name": result_name,
                "error": f"{job_name}: output schema is missing",
                "result_json": json.dumps(result, ensure_ascii=False, indent=2),
            })
            continue
        try:
            _validate_card_image_batch_result(result, schema, result_name)
        except (TypeError, ValueError) as exc:
            errors.append({
                "card_key": card_key,
                "side": side,
                "result_name": result_name,
                "error": str(exc),
                "result_json": json.dumps(result, ensure_ascii=False, indent=2),
            })
            continue
        prepared.append((
            card_key,
            side,
            str(job.get("mode") or "image_extraction"),
            result_name,
            result,
        ))

    paths = {
        Path(path)
        for path in (
            scenario.get("base_path"),
            scenario.get("translation_path"),
            _source_proposals_path(scenario),
        )
        if path is not None
    }
    applied_items: list[dict[str, Any]] = []
    for card_key, side, mode, result_name, result in prepared:
        backups = {
            path: path.read_bytes() if path.is_file() else None
            for path in paths
        }
        try:
            if mode == "text_translation":
                translation = result.get("translation")
                if not isinstance(translation, dict):
                    raise ValueError(f"{result_name}: translation must be an object")
                update_card_translation(
                    root,
                    game=game,
                    slug=slug,
                    card_key=card_key,
                    language=language,
                    side=side,
                    title="",
                    rules="",
                    flavor="",
                    traits="",
                    field_values={
                        str(field): str(value or "")
                        for field, value in translation.items()
                    },
                )
            else:
                apply_gemini_card_image(
                    root,
                    game=game,
                    slug=slug,
                    project_language=project_language,
                    card_key=card_key,
                    side=side,
                    result=result,
                )
        except (OSError, TypeError, ValueError) as exc:
            for path, content in backups.items():
                if content is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(content)
            errors.append({
                "card_key": card_key,
                "side": side,
                "result_name": result_name,
                "error": str(exc),
                "result_json": json.dumps(result, ensure_ascii=False, indent=2),
            })
            continue
        applied_items.append({
            "card_key": card_key,
            "side": side,
            "mode": mode,
            "result_name": result_name,
            "result": result,
        })
    proposal_cards = sorted({
        item["card_key"]
        for item in applied_items
        if not scenario.get("source_only")
        and item.get("mode") == "image_extraction"
        and (
            (item.get("result") or {}).get("source")
            or (item.get("result") or {}).get("metadata")
        )
    })
    return {
        "applied": len(applied_items),
        "failed": len(errors),
        "results": [item["result_name"] for item in applied_items],
        "items": applied_items,
        "source_proposal_cards": proposal_cards,
        "source_proposal_count": len(proposal_cards),
        "errors": errors,
        "game": game,
        "scenario": slug,
        "language": language,
    }


_ARKHAM_RULE_LABEL = re.compile(
    r"^(\s*)(Forced Effect|Forced|Objective|Revelation|Prey|Spawn|Action|Reaction|"
    r"Erzwungen|Ziel|Enthüllung|Beute|Erscheinen|Aktion|Reaktion|Spielende)"
    r"(?=\s*[–—:-])",
    re.IGNORECASE,
)
_ARKHAM_WORD = re.compile(r"\bArkham\b", re.IGNORECASE)
_ARKHAM_RESOLUTION = re.compile(r"(\(\{resolution\})(R\d+)(\))")
_ARKHAM_BULLET_PREFIX = re.compile(r"^([ \t]*)[◆◇◊◈]\s*", re.MULTILINE)
_MARKUP_TAG = re.compile(r"(<[^>]+>)")


def format_arkham_rules_text(rules: str) -> str:
    """Format rule labels, resolution IDs, and Arkham without changing other prose."""
    rules = _ARKHAM_BULLET_PREFIX.sub(r"\1{bullet} ", rules)
    lines = []
    for line in rules.splitlines(keepends=True):
        line = _ARKHAM_RULE_LABEL.sub(r"\1<b>\2</b>", line, count=1)
        lines.append(line)
    tagged = _ARKHAM_RESOLUTION.sub(r"\1<b>\2</b>\3", "".join(lines))
    parts = _MARKUP_TAG.split(tagged)
    styled_depth = 0
    for index, part in enumerate(parts):
        if part.startswith("<") and part.endswith(">"):
            if re.match(r"<(b|bi)(?:\s|>)", part, re.IGNORECASE):
                styled_depth += 1
            elif re.match(r"</(b|bi)\s*>", part, re.IGNORECASE):
                styled_depth = max(0, styled_depth - 1)
        elif not styled_depth:
            parts[index] = _ARKHAM_WORD.sub(lambda match: f"<bi>{match.group()}</bi>", part)
    return "".join(parts)


def format_arkham_rules_result(result: dict[str, Any]) -> dict[str, Any]:
    """Format only the two rules fields in a pasted card JSON result."""
    formatted = {**result}
    for side in ("source", "translation"):
        fields = result.get(side)
        if isinstance(fields, dict) and isinstance(fields.get("rules"), str):
            formatted[side] = {**fields, "rules": format_arkham_rules_text(fields["rules"])}
    return formatted


def preview_card_image(
    root: Path,
    *,
    game: str,
    slug: str,
    project_language: str,
    card_key: str,
    side: str,
    provider: str,
) -> dict[str, Any]:
    scenario, card, schema, prompt = _gemini_card_image_request(
        root,
        game=game,
        slug=slug,
        project_language=project_language,
        card_key=card_key,
        side=side,
    )
    result = analyze_card_image(
        Path(card[side]["path"]), prompt=prompt, schema=schema, provider=provider,
    )
    trace = result.pop("_card_image_trace", {})
    source = result.get("source") if isinstance(result.get("source"), dict) else {}
    target = result.get("translation") if isinstance(result.get("translation"), dict) else {}
    rules = translation_rules(
        root, game=game, slug=slug, language=scenario["target_language"]
    )["merged"]
    warnings: list[dict[str, Any]] = []
    for field in set(source) & set(target):
        normalized, issues = finalize_external_translation(
            str(source.get(field) or ""), str(target.get(field) or ""), rules
        )
        target[field] = normalized
        warnings.extend({"field": field, **issue} for issue in issues)
    preview = {
        "source": {str(key): str(value or "") for key, value in source.items()},
        "translation": {str(key): str(value or "") for key, value in target.items()},
        "metadata": {
            str(key): str(value or "")
            for key, value in (result.get("metadata") or {}).items()
        },
        "warnings": warnings,
        "model": provider_model(provider),
        "provider": provider,
    }
    if side == "back":
        preview["standard_back"] = result.get("standard_back") is True
    log_path = _append_translation_log(
        scenario["path"],
        {
            "kind": "image_translation",
            "provider": provider,
            "card_key": card_key,
            "side": side,
            "status": "preview",
            "prompt": prompt,
            "raw_response": trace.get("raw_response", ""),
            "usage": trace.get("usage", {}),
            "result": preview,
        },
    )
    preview["log_path"] = str(log_path)
    return preview


def apply_gemini_card_image(
    root: Path,
    *,
    game: str,
    slug: str,
    project_language: str,
    card_key: str,
    side: str,
    result: dict[str, Any],
) -> None:
    _validate_card_image_markup(result)
    scenario = get_scenario(root, game, slug, language=project_language)
    if scenario is None or not scenario.get("initialized"):
        raise ValueError("Scenario is not initialized")
    source = result.get("source") if isinstance(result.get("source"), dict) else {}
    target = result.get("translation") if isinstance(result.get("translation"), dict) else {}
    metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
    if game.casefold() == "ah":
        source = {
            **source,
            "rules": format_arkham_rules_text(source["rules"]),
        } if isinstance(source.get("rules"), str) else source
        target = {
            **target,
            "rules": format_arkham_rules_text(target["rules"]),
        } if isinstance(target.get("rules"), str) else target
    source_values = {str(key): str(value or "") for key, value in source.items()}
    if "title" in source_values:
        source_values["title"] = _normalize_unique_title(source_values["title"])
    text_definitions = {
        str(field["id"]): field for field in scenario["translatable_fields"]
    }
    source_snapshot_values: dict[str, Any] = {
        field: (
            [item.strip() for item in re.split(r"[,;\n]", value) if item.strip()]
            if text_definitions.get(field, {}).get("type") == "list"
            else value
        )
        for field, value in source_values.items()
    }
    metadata_values = {str(key): str(value or "") for key, value in metadata.items()}
    if scenario.get("source_only"):
        update_card_game_fields(
            root, game=game, slug=slug, project_language=project_language,
            card_key=card_key, side=side, field_values=metadata_values,
        )
        update_card_source_text(
            root, game=game, slug=slug, project_language=project_language,
            card_key=card_key, side=side, title="", rules="", flavor="", traits="",
            field_values=source_values,
        )
        _mark_source_status(
            root,
            game=game,
            slug=slug,
            project_language=project_language,
            card_key=card_key,
            side=side,
            status="captured",
        )
    else:
        stage_card_source_proposal(
            root,
            game=game,
            slug=slug,
            project_language=project_language,
            card_key=card_key,
            side=side,
            text_values=source_snapshot_values,
            metadata_values=metadata,
            source="llm",
        )
    if scenario is not None and not scenario.get("source_only"):
        update_card_translation(
            root, game=game, slug=slug, card_key=card_key, language=project_language,
            side=side, title="", rules="", flavor="", traits="",
            field_values={str(key): str(value or "") for key, value in target.items()},
            source_values=source_snapshot_values,
        )
    if side == "back" and result.get("standard_back") is True:
        scenario = get_scenario(root, game, slug, language=project_language)
        card = next(
            (item for item in (scenario or {}).get("grouped_cards", []) if item["key"] == card_key),
            None,
        )
        if card and isinstance(card.get("back"), dict):
            update_image_metadata(
                root,
                game=game,
                slug=slug,
                project_language=project_language,
                image_name=card["back"]["name"],
                hidden=True,
            )


def update_image_metadata(
    root: Path,
    *,
    game: str,
    slug: str,
    project_language: str | None = None,
    image_name: str,
    display_rotation: int | None = None,
    hidden: bool | None = None,
) -> dict[str, Any]:
    scenario = get_scenario(root, game, slug, language=project_language)
    if scenario is None or not scenario["initialized"]:
        raise ValueError("Scenario is not initialized")
    if image_name not in {image.name for image in scenario["cards"]}:
        raise ValueError("Image is not part of the configured card source")

    data = read_scenario_data(scenario) or {}
    meta = data.setdefault("_meta", {})
    images = meta.setdefault("images", {})
    item = images.setdefault(image_name, {})

    if display_rotation is not None:
        normalized = display_rotation % 360
        if normalized not in {0, 90, 180, 270}:
            raise ValueError("display_rotation must be 0, 90, 180 or 270")
        item["display_rotation"] = normalized
    if hidden is not None:
        item["hidden"] = bool(hidden)
        if hidden:
            item["hidden_reason"] = "source_side_not_needed"
        else:
            item.pop("hidden_reason", None)

    write_scenario_data(scenario, data)
    return item


def collapse_duplicate_cards(
    root: Path,
    *,
    game: str,
    slug: str,
    project_language: str,
) -> dict[str, Any]:
    """Hide byte-identical card copies and set their representative quantity."""
    scenario = get_scenario(root, game, slug, language=project_language)
    if scenario is None or not scenario["initialized"]:
        raise ValueError("Scenario is not initialized")

    def digest(path: Path) -> str:
        checksum = sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                checksum.update(chunk)
        return checksum.hexdigest()

    by_content: dict[tuple[str, str | None], list[dict[str, Any]]] = {}
    for card in scenario["grouped_cards"]:
        front = card.get("front")
        if not isinstance(front, dict):
            continue
        back = card.get("back")
        signature = (
            digest(Path(front["path"])),
            digest(Path(back["path"])) if isinstance(back, dict) else None,
        )
        by_content.setdefault(signature, []).append(card)

    duplicate_groups = [cards for cards in by_content.values() if len(cards) > 1]
    if not duplicate_groups:
        return {"groups": 0, "hidden_cards": 0, "collapsed": []}

    data = read_scenario_data(scenario) or {}
    records = data.setdefault("cards", [])
    image_meta = data.setdefault("_meta", {}).setdefault("images", {})
    hidden_cards = 0
    collapsed: list[dict[str, Any]] = []
    for cards in duplicate_groups:
        cards.sort(
            key=lambda card: [
                int(part) if part.isdigit() else part.casefold()
                for part in re.split(r"(\d+)", str(card["key"]))
            ]
        )
        representative = cards[-1]
        hidden = cards[:-1]
        for duplicate in cards[:-1]:
            hidden_cards += 1
            for side in ("front", "back"):
                image = duplicate.get(side)
                if isinstance(image, dict):
                    image_meta.setdefault(image["name"], {}).update(
                        {"hidden": True, "hidden_reason": "duplicate_copy"}
                    )
        record = next(
            (
                item for item in records
                if isinstance(item, dict) and str(item.get("key") or "") == representative["key"]
            ),
            None,
        )
        if record is None:
            record = {
                "id": representative["key"],
                "key": representative["key"],
                "images": {
                    side: f"{scenario['card_source']}/{representative[side]['name']}"
                    for side in ("front", "back")
                    if isinstance(representative.get(side), dict)
                },
                "text": {},
                "game_data": {},
            }
            records.append(record)
        record.setdefault("game_data", {})["printed_quantity"] = len(cards)
        collapsed.append(
            {
                "representative_key": representative["key"],
                "representative_name": str(
                    representative.get("source_text", {}).get("title")
                    or representative.get("display_name")
                    or representative["key"]
                ),
                "quantity": len(cards),
                "hidden_keys": [card["key"] for card in hidden],
            }
        )

    write_scenario_data(scenario, data)
    return {
        "groups": len(duplicate_groups),
        "hidden_cards": hidden_cards,
        "collapsed": collapsed,
    }


_SCALED_NUMBER = re.compile(
    r"^(?P<value>[+-]?\d+|x)\s*(?P<per><(?:per|per_investigator)>|"
    r"\{(?:per|per_investigator)\}|\[(?:per|per_investigator)\])?$",
    re.IGNORECASE,
)
_PRINTED_EMPTY_NUMBER_MARKERS = {"-", "–", "—", "−"}


def _normalize_scaled_number(value: str, *, field_id: str) -> str:
    if not value:
        return ""
    normalized_minus = value.replace("−", "-").strip()
    match = _SCALED_NUMBER.fullmatch(normalized_minus)
    if match is None:
        raise ValueError(
            f"{field_id} must be X or a whole number, optionally followed by <per>"
        )
    base = match.group("value")
    if base.casefold() == "x":
        base = "X"
    return f"{base}<per>" if match.group("per") else base


def update_card_game_fields(
    root: Path,
    *,
    game: str,
    slug: str,
    project_language: str,
    card_key: str,
    field_values: dict[str, str],
    side: str = "front",
) -> dict[str, Any]:
    scenario = get_scenario(root, game, slug, language=project_language)
    if scenario is None or not scenario["initialized"]:
        raise ValueError("Scenario is not initialized")
    logical_card = next(
        (card for card in scenario["grouped_cards"] if card["key"] == card_key), None
    )
    if logical_card is None:
        raise ValueError("Unknown logical card")
    if side not in {"front", "back"}:
        raise ValueError("side must be front or back")
    field_values = dict(field_values)
    if "clues" in field_values and "clues_per_investigator" in field_values:
        legacy_per = str(field_values.pop("clues_per_investigator") or "").casefold() in {
            "1", "true", "yes", "on",
        }
        if legacy_per and str(field_values["clues"]).strip():
            field_values["clues"] = f"{field_values['clues']}<per>"

    incoming_type = str(field_values.get("card_type") or logical_card.get("card_type") or "")
    if incoming_type.casefold().replace("-", "_").replace(" ", "_") in {"act", "agenda"}:
        stage_value = str(field_values.get("stage") or "").strip()
        index_value = str(field_values.get("index") or "").strip()
        if re.fullmatch(r"\d+", stage_value) and re.fullmatch(r"[a-z]", index_value, re.I):
            field_values["index"] = f"{stage_value}{index_value.casefold()}"

    definitions = {str(field["id"]): field for field in scenario["metadata_fields"]}
    unknown = set(field_values) - set(definitions)
    if unknown:
        raise ValueError(f"Unknown metadata field: {sorted(unknown)[0]}")

    values: dict[str, str | int | list[str] | None] = {}
    for field_id, raw_value in field_values.items():
        field_type = definitions[field_id]["type"]
        value = raw_value.strip()
        if definition := definitions.get(field_id):
            value_format = definition.get("value_format")
        else:
            value_format = None
        if (
            field_type == "integer" or value_format == "scaled_number"
        ) and value in _PRINTED_EMPTY_NUMBER_MARKERS:
            value = ""
        if value_format == "scaled_number":
            values[field_id] = _normalize_scaled_number(value, field_id=field_id)
        elif field_type == "integer":
            try:
                values[field_id] = int(value) if value else None
            except ValueError as exc:
                raise ValueError(f"{field_id} must be a whole number") from exc
        elif field_type == "list":
            values[field_id] = [
                item.strip() for item in re.split(r"[,;\n]", value) if item.strip()
            ]
        else:
            values[field_id] = value

    data = read_scenario_data(scenario) or {}
    records = data.setdefault("cards", [])
    if not isinstance(records, list):
        raise ValueError("Translation contains invalid card records")
    record = next(
        (
            item
            for item in records
            if isinstance(item, dict) and str(item.get("key") or "") == card_key
        ),
        None,
    )
    if record is None:
        record = {
            "id": card_key,
            "key": card_key,
            "images": {
                side: (
                    f"{scenario['card_source']}/{logical_card[side]['name']}"
                    if logical_card.get(side)
                    else None
                )
                for side in ("front", "back")
            },
            "text": {},
            "game_data": {},
        }
        records.append(record)
    game_data = record.setdefault("game_data", {})
    side_values = (
        game_data
        if side == "front"
        else game_data.setdefault("sides", {}).setdefault("back", {})
    )
    card_id = str(record.get("id") or card_key)
    field_meta = data.setdefault("_meta", {}).setdefault("fields", {})
    for field_id, value in values.items():
        if field_id == "card_type":
            if side != "front":
                continue
            record[field_id] = value
            path = f"{card_id}.{field_id}"
        else:
            side_values[field_id] = value
            if field_id == "clues":
                side_values.pop("clues_per_investigator", None)
            path = (
                f"{card_id}.game_data.{field_id}"
                if side == "front"
                else f"{card_id}.game_data.sides.back.{field_id}"
            )
        field_meta[path] = {
            "source": "manual_metadata_edit",
            "confidence": 1.0,
            "review_required": False,
        }
    write_scenario_data(scenario, data)
    return {"values": values}


def update_card_translation(
    root: Path,
    *,
    game: str,
    slug: str,
    card_key: str,
    language: str,
    side: str,
    title: str,
    rules: str,
    flavor: str,
    traits: str,
    field_values: dict[str, str] | None = None,
    source_values: dict[str, Any] | None = None,
) -> dict[str, Any]:
    language = normalize_language_code(language)
    scenario = get_scenario(root, game, slug, language=language)
    if scenario is None or not scenario["initialized"]:
        raise ValueError("Scenario is not initialized")
    if language != scenario["target_language"]:
        raise ValueError("Only the project's target language can be edited")
    if side not in {"front", "back"}:
        raise ValueError("side must be front or back")

    logical_card = next(
        (card for card in scenario["grouped_cards"] if card["key"] == card_key), None
    )
    if logical_card is None:
        raise ValueError("Unknown logical card")

    data = read_scenario_data(scenario) or {}
    records = data.setdefault("cards", [])
    if not isinstance(records, list):
        raise ValueError("Translation contains no card records")
    record = next(
        (
            item
            for item in records
            if isinstance(item, dict) and str(item.get("key") or "") == card_key
        ),
        None,
    )
    if record is None:
        record = {
            "id": card_key,
            "key": card_key,
            "images": {
                image_side: (
                    f"{scenario['card_source']}/{logical_card[image_side]['name']}"
                    if logical_card.get(image_side)
                    else None
                )
                for image_side in ("front", "back")
            },
            "identifiers": {},
            "text": {},
            "game_data": {},
        }
        records.append(record)

    text = record.setdefault("text", {})
    localized = text.setdefault(language, {})
    sides = localized.setdefault("sides", {})
    side_data = sides.setdefault(side, {})
    raw_values = field_values or {
        "title": title,
        "rules": rules,
        "flavor": flavor,
        "traits": traits,
    }
    definitions = {str(field["id"]): field for field in scenario["translatable_fields"]}
    values: dict[str, str | list[str]] = {}
    for field, value in raw_values.items():
        if field not in definitions:
            continue
        if definitions[field]["type"] == "list":
            values[field] = [item.strip() for item in re.split(r"[,;\n]", value) if item.strip()]
        else:
            values[field] = (
                _normalize_unique_title(value) if field == "title" else value.strip()
            )
    side_data.update(values)
    if side == "front":
        localized.update(side_data)

    card_id = str(record.get("id") or record.get("key") or card_key)
    fields = data.setdefault("_meta", {}).setdefault("fields", {})
    for field in values:
        if source_values is not None and field in source_values:
            source_value = source_values[field]
        else:
            source_side = (logical_card.get("source_sides") or {}).get(side) or {}
            source_value = source_side.get(field)
            if side == "front" and source_value is None:
                source_value = (logical_card.get("source_text") or {}).get(field)
        fields[f"{card_id}.text.{language}.sides.{side}.{field}"] = {
            "source": "manual",
            "confidence": 1.0,
            "source_hash": _source_value_hash(source_value),
            "source_value": source_value,
        }

    write_scenario_data(scenario, data)
    return side_data


def update_card_source_text(
    root: Path,
    *,
    game: str,
    slug: str,
    project_language: str,
    card_key: str,
    side: str,
    title: str,
    rules: str,
    flavor: str,
    traits: str,
    field_values: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Save a reviewed manual correction to a card's extracted source text."""
    scenario = get_scenario(root, game, slug, language=project_language)
    if scenario is None or not scenario["initialized"]:
        raise ValueError("Scenario is not initialized")
    if side not in {"front", "back"}:
        raise ValueError("side must be front or back")

    logical_card = next(
        (card for card in scenario["grouped_cards"] if card["key"] == card_key),
        None,
    )
    if logical_card is None:
        raise ValueError("Unknown logical card")
    image_side = logical_card.get(side)
    if not isinstance(image_side, dict):
        raise ValueError("The selected card side has no source image")

    data = read_scenario_data(scenario) or {}
    records = data.setdefault("cards", [])
    if not isinstance(records, list):
        raise ValueError("Translation contains invalid card records")
    record = next(
        (
            item
            for item in records
            if isinstance(item, dict) and str(item.get("key") or "") == card_key
        ),
        None,
    )
    if record is None:
        record = {
            "key": card_key,
            "id": card_key,
            "images": {
                name: value["name"]
                for name in ("front", "back")
                if isinstance((value := logical_card.get(name)), dict) and value.get("name")
            },
            "text": {},
        }
        records.append(record)

    source_language = str((scenario.get("project") or {}).get("source_language") or "en")
    target_language = scenario["target_language"]
    text = record.setdefault("text", {})
    localized = text.setdefault(source_language, {})
    sides = localized.setdefault("sides", {})
    side_data = sides.setdefault(side, {})
    raw_values = field_values or {
        "title": title,
        "traits": traits,
        "rules": rules,
        "flavor": flavor,
    }
    definitions = {str(field["id"]): field for field in scenario["translatable_fields"]}
    values: dict[str, str | list[str]] = {}
    for field, value in raw_values.items():
        if field not in definitions:
            continue
        if definitions[field]["type"] == "list":
            values[field] = [item.strip() for item in re.split(r"[,;\n]", value) if item.strip()]
        elif definitions[field]["type"] == "text":
            collapsed = " ".join(value.strip().splitlines())
            values[field] = (
                _normalize_unique_title(collapsed) if field == "title" else collapsed
            )
        else:
            values[field] = value.strip()
    changed_fields = [field for field, value in values.items() if side_data.get(field) != value]
    side_data.update(values)
    if side == "front":
        localized.update(values)

    card_id = str(record.get("id") or record.get("key") or card_key)
    fields = data.setdefault("_meta", {}).setdefault("fields", {})
    for field in changed_fields:
        field_key = f"{card_id}.text.{source_language}.sides.{side}.{field}"
        provenance: dict[str, Any] = {
            "source": "manual_source_edit",
            "confidence": 1.0,
            "review_required": False,
        }
        previous = fields.get(field_key)
        if isinstance(previous, dict):
            provenance["previous"] = previous
        fields[field_key] = provenance
        if side == "front":
            fields[f"{card_id}.text.{source_language}.{field}"] = dict(provenance)

        if not scenario.get("source_only"):
            target_key = f"{card_id}.text.{target_language}.sides.{side}.{field}"
            target_side = (
                (text.get(target_language) or {}).get("sides", {}).get(side, {})
                if isinstance(text.get(target_language), dict)
                else {}
            )
            if isinstance(target_side, dict) and target_side.get(field):
                target_meta = fields.setdefault(target_key, {"source": "unknown"})
                source_matches = target_meta.get("source_hash") == _source_value_hash(
                    values[field]
                )
                if not source_matches:
                    target_meta["review_required"] = True
                    target_meta["review_reason"] = "source_text_changed"
                if side == "front" and not source_matches:
                    flat_target_meta = fields.setdefault(
                        f"{card_id}.text.{target_language}.{field}", {"source": "unknown"}
                    )
                    flat_target_meta["review_required"] = True
                    flat_target_meta["review_reason"] = "source_text_changed"

    write_scenario_data(scenario, data)
    if scenario.get("shared_layout") and changed_fields:
        translations_dir = scenario["path"] / "translations"
        for translation_dir in (
            translations_dir.iterdir() if translations_dir.is_dir() else []
        ):
            if not translation_dir.is_dir() or translation_dir.name == target_language:
                continue
            other = read_translation_document(translation_dir / LANGUAGE_FILENAME)
            if other is None:
                continue
            other_language = translation_dir.name
            other_record = next(
                (
                    item for item in other.get("cards", [])
                    if isinstance(item, dict)
                    and str(item.get("key") or item.get("id") or "") == card_key
                ), None,
            )
            other_sides = (
                ((other_record.get("text") or {}).get(other_language) or {}).get("sides")
                if isinstance(other_record, dict) else {}
            )
            other_side = (other_sides or {}).get(side) or {}
            other_fields = other.setdefault("_meta", {}).setdefault("fields", {})
            touched = False
            for field in changed_fields:
                if not other_side.get(field):
                    continue
                field_key = f"{card_id}.text.{other_language}.sides.{side}.{field}"
                other_meta = other_fields.setdefault(field_key, {"source": "unknown"})
                if other_meta.get("source_hash") == _source_value_hash(values[field]):
                    continue
                other_meta.update({
                    "review_required": True,
                    "review_reason": "source_text_changed",
                })
                if side == "front":
                    flat_key = f"{card_id}.text.{other_language}.{field}"
                    other_fields.setdefault(flat_key, {"source": "unknown"}).update({
                        "review_required": True,
                        "review_reason": "source_text_changed",
                    })
                touched = True
            if touched:
                write_translation_document(
                    translation_dir / LANGUAGE_FILENAME,
                    other,
                    base_path=scenario["base_path"],
                )
                other_project_path = translation_dir / "project.json"
                other_project = read_json(other_project_path)
                if other_project is not None and other_project.get("final_reviewed") is True:
                    other_project["final_reviewed"] = False
                    write_json(other_project_path, other_project)
    if changed_fields and scenario.get("source_only"):
        _mark_source_status(
            root,
            game=game,
            slug=slug,
            project_language=project_language,
            card_key=card_key,
            side=side,
            status="reviewed",
        )
    return {"values": values, "changed_fields": changed_fields}


def card_source_proposal(
    root: Path,
    *,
    game: str,
    slug: str,
    project_language: str,
    card_key: str,
) -> dict[str, Any]:
    """Return one readable, conflict-aware source proposal."""
    scenario = get_scenario(root, game, slug, language=project_language)
    if scenario is None or not scenario.get("initialized"):
        raise ValueError("Scenario is not initialized")
    card = next(
        (item for item in scenario["grouped_cards"] if item["key"] == card_key),
        None,
    )
    if card is None:
        raise ValueError("Unknown logical card")
    raw = (
        (_read_source_proposals(scenario).get("cards") or {}).get(card_key)
        if not scenario.get("source_only")
        else None
    )
    if not isinstance(raw, dict):
        return {"card_key": card_key, "sides": [], "field_count": 0, "stale": False}
    text_definitions = {
        str(field["id"]): field for field in scenario["translatable_fields"]
    }
    metadata_definitions = {
        str(field["id"]): field for field in scenario["metadata_fields"]
    }
    sides: list[dict[str, Any]] = []
    stale = False
    field_count = 0
    for side in ("front", "back"):
        raw_side = (raw.get("sides") or {}).get(side)
        if not isinstance(raw_side, dict):
            continue
        fields: list[dict[str, Any]] = []
        for kind, definitions in (
            ("text", text_definitions),
            ("metadata", metadata_definitions),
        ):
            entries = raw_side.get(kind)
            if not isinstance(entries, dict):
                continue
            for field, entry in entries.items():
                if not isinstance(entry, dict):
                    continue
                current = _proposal_current_value(
                    card, side=side, kind=kind, field=str(field)
                )
                current_hash = _source_value_hash(current)
                entry_stale = current_hash != str(entry.get("base_hash") or "")
                stale = stale or entry_stale
                definition = definitions.get(str(field), {})
                fields.append({
                    "kind": kind,
                    "field": str(field),
                    "label": str(definition.get("label") or field).replace("_", " ").title(),
                    "current": current,
                    "proposed": entry.get("proposed_value"),
                    "origin": str(entry.get("source") or ""),
                    "stale": entry_stale,
                    "reviewed_hash": current_hash,
                })
                field_count += 1
        image = card.get(side)
        sides.append({
            "side": side,
            "image": (
                f"/library/{scenario['library_relative_path']}/"
                f"{scenario['card_source']}/{image['name']}"
                if isinstance(image, dict) and scenario.get("card_source")
                else ""
            ),
            "fields": fields,
        })
    return {
        "card_key": card_key,
        "title": str((card.get("source_text") or {}).get("title") or card["display_name"]),
        "sides": sides,
        "field_count": field_count,
        "stale": stale,
    }


def apply_card_source_proposal(
    root: Path,
    *,
    game: str,
    slug: str,
    project_language: str,
    card_key: str,
    updates: list[dict[str, Any]],
) -> dict[str, Any]:
    """Apply reviewed proposal values to the canonical extraction."""
    scenario = get_scenario(root, game, slug, language=project_language)
    if scenario is None or not scenario.get("initialized"):
        raise ValueError("Scenario is not initialized")
    if scenario.get("source_only"):
        raise ValueError("Source workspace does not use proposals")
    proposal = card_source_proposal(
        root,
        game=game,
        slug=slug,
        project_language=project_language,
        card_key=card_key,
    )
    known = {
        (side["side"], field["kind"], field["field"]): field
        for side in proposal["sides"]
        for field in side["fields"]
    }
    by_side: dict[str, dict[str, dict[str, Any]]] = {}
    for update in updates:
        key = (
            str(update.get("side") or ""),
            str(update.get("kind") or ""),
            str(update.get("field") or ""),
        )
        field = known.get(key)
        if field is None:
            raise ValueError("Proposal contains an unknown field")
        if str(update.get("reviewed_hash") or "") != field["reviewed_hash"]:
            raise ValueError("Original data changed after the diff was opened")
        by_side.setdefault(key[0], {}).setdefault(key[1], {})[key[2]] = update.get("value")

    for side, kinds in by_side.items():
        text_values = {
            field: (
                ", ".join(str(item) for item in value)
                if isinstance(value, list)
                else str(value or "")
            )
            for field, value in kinds.get("text", {}).items()
        }
        if text_values:
            update_card_source_text(
                root,
                game=game,
                slug=slug,
                project_language=project_language,
                card_key=card_key,
                side=side,
                title="",
                rules="",
                flavor="",
                traits="",
                field_values=text_values,
            )
        metadata_values = {
            field: (
                ", ".join(str(item) for item in value)
                if isinstance(value, list)
                else str(value or "")
            )
            for field, value in kinds.get("metadata", {}).items()
        }
        if metadata_values:
            update_card_game_fields(
                root,
                game=game,
                slug=slug,
                project_language=project_language,
                card_key=card_key,
                side=side,
                field_values=metadata_values,
            )
        _mark_source_status(
            root,
            game=game,
            slug=slug,
            project_language=project_language,
            card_key=card_key,
            side=side,
            status="reviewed",
        )

    proposals = _read_source_proposals(scenario)
    (proposals.get("cards") or {}).pop(card_key, None)
    _write_source_proposals(scenario, proposals)
    return {"applied": len(updates), "card_key": card_key}


def apply_card_ocr_text(
    root: Path,
    *,
    game: str,
    slug: str,
    project_language: str | None = None,
    card_key: str,
    image_name: str,
    language: str,
    side: str,
    field: str,
    value: str,
    raw_text: str,
    confidence: float | None,
    x: float,
    y: float,
    width: float,
    height: float,
    rotation: int,
) -> dict[str, Any]:
    """Apply a reviewed OCR suggestion to one canonical source field."""
    scenario = get_scenario(root, game, slug, language=project_language)
    if scenario is None or not scenario["initialized"]:
        raise ValueError("Scenario is not initialized")
    if image_name not in {image.name for image in scenario["cards"]}:
        raise ValueError("Image is not part of the configured card source")
    logical_card = next(
        (card for card in scenario["grouped_cards"] if card["key"] == card_key),
        None,
    )
    if logical_card is None:
        raise ValueError("Unknown logical card")
    source_language = str((scenario.get("project") or {}).get("source_language") or "en")
    if language.strip().lower() != source_language:
        raise ValueError("OCR can only edit the project's source language")
    if side not in {"front", "back"}:
        raise ValueError("side must be front or back")
    text_fields = {"title", "traits", "rules", "flavor"}
    metadata_definitions = {
        str(item["id"]): item for item in scenario["metadata_fields"]
    }
    is_metadata = field in metadata_definitions and field != "card_type"
    if field not in text_fields and not is_metadata:
        raise ValueError("Unsupported OCR target field")
    image_side = logical_card.get(side)
    if not isinstance(image_side, dict) or image_side.get("name") != image_name:
        raise ValueError("Image does not belong to the selected card side")
    if confidence is not None and not 0 <= confidence <= 1:
        raise ValueError("confidence must be between 0 and 1")
    if rotation % 360 not in {0, 90, 180, 270}:
        raise ValueError("rotation must be 0, 90, 180 or 270")
    if not all(0 <= number <= 1 for number in (x, y, width, height)):
        raise ValueError("OCR region must use normalized coordinates")
    if width < 0.01 or height < 0.01 or x + width > 1.001 or y + height > 1.001:
        raise ValueError("Invalid OCR region")
    if not scenario.get("source_only"):
        proposal = stage_card_source_proposal(
            root,
            game=game,
            slug=slug,
            project_language=str(project_language or scenario["storage_language"]),
            card_key=card_key,
            side=side,
            text_values=None if is_metadata else {field: value},
            metadata_values={field: value} if is_metadata else None,
            source="ocr",
        )
        return {
            **proposal,
            "field": field,
            "value": value.strip(),
            "review_required": True,
        }

    data = read_scenario_data(scenario) or {}
    records = data.setdefault("cards", [])
    if not isinstance(records, list):
        raise ValueError("Translation contains invalid card records")
    record = next(
        (item for item in records if isinstance(item, dict) and str(item.get("key") or "") == card_key),
        None,
    )
    if record is None:
        record = {
            "key": card_key,
            "id": card_key,
            "images": {side: image_name},
            "text": {},
        }
        records.append(record)

    clean_value = value.strip()
    stored_value: str | int | list[str] | None
    if is_metadata:
        definition = metadata_definitions[field]
        if definition.get("value_format") == "scaled_number":
            stored_value = _normalize_scaled_number(clean_value, field_id=field)
        elif definition.get("type") == "integer":
            try:
                stored_value = int(clean_value) if clean_value else None
            except ValueError as exc:
                raise ValueError(f"{field} must be a whole number") from exc
        else:
            stored_value = " ".join(clean_value.splitlines())
        game_data = record.setdefault("game_data", {})
        side_data = (
            game_data
            if side == "front"
            else game_data.setdefault("sides", {}).setdefault("back", {})
        )
        side_data[field] = stored_value
    else:
        text = record.setdefault("text", {})
        localized = text.setdefault(source_language, {})
        sides = localized.setdefault("sides", {})
        side_data = sides.setdefault(side, {})
        if field == "title":
            clean_value = _normalize_unique_title(" ".join(clean_value.splitlines()))
        if field == "traits":
            stored_value = [item.strip() for item in re.split(r"[,;\n]", clean_value) if item.strip()]
        else:
            stored_value = clean_value
        side_data[field] = stored_value
        if side == "front":
            localized[field] = stored_value

    card_id = str(record.get("id") or record.get("key") or card_key)
    provenance = {
        "source": "ocr_manual_region",
        "confidence": confidence,
        "review_required": False,
        "image": image_name,
        "side": side,
        "region": {
            "x": round(x, 6),
            "y": round(y, 6),
            "width": round(width, 6),
            "height": round(height, 6),
            "rotation": rotation % 360,
        },
        "ocr_language": source_language,
        "ocr_raw_text": raw_text,
    }
    fields = data.setdefault("_meta", {}).setdefault("fields", {})
    if is_metadata:
        metadata_path = (
            f"{card_id}.game_data.{field}"
            if side == "front"
            else f"{card_id}.game_data.sides.back.{field}"
        )
        fields[metadata_path] = provenance
    else:
        fields[f"{card_id}.text.{source_language}.sides.{side}.{field}"] = provenance
        if side == "front":
            fields[f"{card_id}.text.{source_language}.{field}"] = dict(provenance)

    write_scenario_data(scenario, data)
    return {"field": field, "value": stored_value, "provenance": provenance}


def _scenario_locations(game_dir: Path) -> list[tuple[str, Path, bool]]:
    """Return (language, scenario directory, legacy-layout) tuples."""
    locations: list[tuple[str, Path, bool]] = []
    for child in sorted(path for path in game_dir.iterdir() if path.is_dir()):
        if child.name.casefold() in GAME_SYSTEM_DIRS or child.name.startswith("."):
            continue
        is_language_layer = bool(LANGUAGE_DIR_CODE.fullmatch(child.name.lower())) and not (
            child / "project.json"
        ).exists()
        if is_language_layer:
            for scenario_dir in sorted(path for path in child.iterdir() if path.is_dir()):
                locations.append((child.name.lower(), scenario_dir, False))
            continue
        project = _read_json(child / "project.json")
        language = str((project or {}).get("target_language") or "de").lower()
        locations.append((language, child, True))
    return locations


def discover_library(root: Path) -> list[dict[str, Any]]:
    root.mkdir(parents=True, exist_ok=True)
    games: list[dict[str, Any]] = []

    for game_definition in discover_games(root):
        game_slug = str(game_definition["id"])
        game_name = str(game_definition["name"])
        game_dir = root / game_slug
        scenarios: list[dict[str, Any]] = []

        shared_scenarios = [
            path for path in shared_scenario_directories(root, game_slug)
            if (_read_json(path / "project.json") or {}).get("format_version") in {2, 3}
        ]
        shared_slugs = {path.name for path in shared_scenarios}
        locations: list[tuple[str, Path, bool, bool, bool]] = [
            (language, path, legacy, False, False)
            for language, path, legacy in (
                _scenario_locations(game_dir) if game_dir.is_dir() else []
            )
            if path.name not in shared_slugs
        ]
        for scenario_dir in shared_scenarios:
            locations.append(("source", scenario_dir, False, True, True))
            translation_dir = scenario_dir / "translations"
            if not translation_dir.is_dir():
                continue
            locations.extend(
                (path.name, scenario_dir, False, True, False)
                for path in sorted(translation_dir.iterdir())
                if path.is_dir() and (path / LANGUAGE_FILENAME).is_file()
            )

        for (
            storage_language,
            scenario_dir,
            legacy_layout,
            shared_layout,
            source_only,
        ) in locations:
            base_project = _read_json(scenario_dir / "project.json")
            campaign_slug, campaign_dir = campaign_for_scenario(scenario_dir)
            campaign = (
                _read_json(campaign_dir / CAMPAIGN_FILENAME)
                if campaign_dir is not None
                else None
            )
            language_project = (
                _read_json(language_path(scenario_dir, storage_language) / "project.json")
                if shared_layout and not source_only else None
            )
            raw_private_override = str(
                (base_project or {}).get("private_only_override") or ""
            ).casefold()
            if raw_private_override not in {"inherit", "private", "public"}:
                raw_private_override = (
                    "private" if (base_project or {}).get("private_only") else "inherit"
                )
            scenario_private_only = raw_private_override == "private"
            campaign_private_only = bool((campaign or {}).get("private_only"))
            private_only = (
                False
                if raw_private_override == "public"
                else scenario_private_only or campaign_private_only
            )
            project = (
                {**(campaign or {}), **base_project, **(language_project or {})}
                if base_project
                else None
            )
            source_candidates = _find_card_sources(scenario_dir)
            selected_source = _select_card_source(scenario_dir, project, source_candidates)
            cards = selected_source["images"] if selected_source else []

            language = storage_language
            base_path = scenario_dir / BASE_FILENAME if shared_layout else None
            translation_path = (
                (
                    base_path
                    if source_only
                    else language_path(scenario_dir, language) / LANGUAGE_FILENAME
                )
                if shared_layout else scenario_dir / f"translation_{language}.json"
            )
            descriptor = {
                "shared_layout": shared_layout,
                "source_only": source_only,
                "base_path": base_path,
                "translation_path": translation_path,
            }
            translation = read_scenario_data(descriptor)
            grouped_cards = _group_cards(cards, translation, game_definition)
            proposal_cards: dict[str, Any] = {}
            if not source_only and isinstance(translation_path, Path):
                proposal_document = read_json(
                    translation_path.parent / SOURCE_PROPOSALS_FILENAME
                )
                if isinstance(proposal_document, dict) and isinstance(
                    proposal_document.get("cards"), dict
                ):
                    proposal_cards = proposal_document["cards"]
            for grouped_card in grouped_cards:
                proposal = proposal_cards.get(str(grouped_card["key"]))
                grouped_card["source_proposal"] = (
                    proposal if isinstance(proposal, dict) else {}
                )
                grouped_card["source_proposal_count"] = sum(
                    len(fields)
                    for side_proposal in (
                        (proposal or {}).get("sides", {}).values()
                        if isinstance(proposal, dict)
                        else []
                    )
                    if isinstance(side_proposal, dict)
                    for fields in side_proposal.values()
                    if isinstance(fields, dict)
                )
                proposal_sides = (
                    proposal.get("sides", {}) if isinstance(proposal, dict) else {}
                )
                grouped_card["source_draft_sides"] = {
                    side: dict((grouped_card.get("source_sides") or {}).get(side) or {})
                    for side in ("front", "back")
                }
                grouped_card["metadata_draft_fields_by_side"] = {}
                for side in ("front", "back"):
                    proposed_side = proposal_sides.get(side) or {}
                    for field, entry in (proposed_side.get("text") or {}).items():
                        if isinstance(entry, dict):
                            grouped_card["source_draft_sides"][side][field] = entry.get(
                                "proposed_value"
                            )
                    proposed_metadata = proposed_side.get("metadata") or {}
                    grouped_card["metadata_draft_fields_by_side"][side] = [
                        {
                            **field,
                            "value": (
                                proposed_metadata[field["id"]].get("proposed_value")
                                if isinstance(proposed_metadata.get(field["id"]), dict)
                                else field.get("value")
                            ),
                        }
                        for field in grouped_card["metadata_fields_by_side"][side]
                    ]
            _attach_separate_card_art(grouped_cards, scenario_dir, base_project)
            completed = sum(
                any(
                    isinstance(value, str) and value.strip()
                    for side in card.get(
                        "source_sides" if source_only else "target_sides", {}
                    ).values()
                    if isinstance(side, dict)
                    for value in side.values()
                )
                for card in grouped_cards
            )
            translation_total = len(grouped_cards)

            title = (
                str(project.get("title"))
                if project and project.get("title")
                else _humanize_slug(scenario_dir.name)
            )

            card_source = selected_source["relative_path"] if selected_source else None
            scenarios.append(
                {
                    "game": game_slug,
                    "game_definition": game_definition,
                    "translatable_fields": _translatable_fields(game_definition),
                    "metadata_fields": _metadata_fields(game_definition),
                    "card_types": _card_types(game_definition),
                    "slug": scenario_dir.name,
                    "campaign": campaign_slug,
                    "campaign_title": str((campaign or {}).get("title") or ""),
                    "campaign_metadata": campaign,
                    "private_only": private_only,
                    "scenario_private_only": scenario_private_only,
                    "private_only_override": raw_private_override,
                    "campaign_private_only": campaign_private_only,
                    "storage_language": storage_language,
                    "legacy_layout": legacy_layout,
                    "shared_layout": shared_layout,
                    "source_only": source_only,
                    "base_path": base_path,
                    "language_project_path": (
                        (
                            scenario_dir / "project.json"
                            if source_only
                            else language_path(scenario_dir, language) / "project.json"
                        )
                        if shared_layout
                        else scenario_dir / "project.json"
                    ),
                    "title": title,
                    "path": scenario_dir,
                    "library_relative_path": scenario_dir.relative_to(root).as_posix(),
                    "project": project,
                    "initialized": project is not None,
                    "product_printings": project.get("product_printings", []) if project else [],
                    "scenario_summary": _scenario_summary(translation)
                    or (project.get("scenario_summary", {}) if project else {}),
                    "card_count": len(cards),
                    "cards": cards,
                    "grouped_cards": grouped_cards,
                    "translation_path": translation_path,
                    "card_source": card_source,
                    "card_source_candidates": source_candidates,
                    "card_source_needs_selection": bool(source_candidates) and selected_source is None,
                    "target_language": (
                        str((base_project or {}).get("source_language") or "en")
                        if source_only else language
                    ),
                    "translated": completed,
                    "translation_total": translation_total,
                    "translation_percent": (
                        round((completed / translation_total) * 100)
                        if translation_total
                        else 0
                    ),
                }
            )

        games.append(
            {
                "slug": game_slug,
                "name": game_name,
                "description": str(game_definition.get("description") or ""),
                "definition": game_definition,
                "scenarios": scenarios,
            }
        )

    return games


def get_scenario(
    root: Path,
    game: str,
    slug: str,
    *,
    language: str | None = None,
    campaign: str | None = None,
) -> dict[str, Any] | None:
    normalized_language = normalize_language_code(language) if language else None
    for game_data in discover_library(root):
        if game_data["slug"] != game:
            continue
        for scenario in game_data["scenarios"]:
            if scenario["slug"] == slug and (
                campaign is None or scenario.get("campaign") == campaign
            ) and (
                normalized_language is None
                or scenario["storage_language"] == normalized_language
            ):
                return scenario
    return None


def delete_scenario(
    root: Path, *, game: str, slug: str, language: str | None = None
) -> None:
    if get_game(root, game) is None:
        raise ValueError(f"Unknown game: {game}")

    scenario = get_scenario(root, game, slug, language=language)
    if scenario is None:
        raise ValueError("Scenario folder does not exist")
    game_dir = (
        root / "projects" / game if scenario.get("shared_layout") else root / game
    ).resolve()
    scenario_dir = scenario["path"].resolve()
    if game_dir not in scenario_dir.parents:
        raise ValueError("Invalid scenario path")
    if not scenario_dir.exists() or not scenario_dir.is_dir():
        raise ValueError("Scenario folder does not exist")

    if scenario.get("shared_layout"):
        shutil.rmtree(language_path(scenario_dir, scenario["storage_language"]))
        return
    shutil.rmtree(scenario_dir)


def copy_scenario_to_language(
    root: Path,
    *,
    game: str,
    slug: str,
    project_language: str,
    target_language: str,
) -> dict[str, Any]:
    """Copy a project while retaining source data and clearing translated text."""
    project_language = normalize_language_code(project_language)
    target_language = normalize_language_code(target_language)
    if target_language == project_language:
        raise ValueError("Choose a different target language")
    scenario = get_scenario(root, game, slug, language=project_language)
    if scenario is None or not scenario["initialized"]:
        raise ValueError("Scenario is not initialized")
    if scenario.get("shared_layout"):
        destination = language_path(scenario["path"], target_language)
        if destination.exists():
            raise ValueError("A translation for this language already exists")
        source_language = str((scenario.get("project") or {}).get("source_language") or "en")
        if target_language == source_language:
            raise ValueError("Source and target language must be different")
        write_json(destination / "project.json", {
            "target_language": target_language,
            "translated_title": "",
        })
        write_translation_document(destination / LANGUAGE_FILENAME, {
            "format": "card-translator-translation",
            "format_version": 3,
            "game": game,
            "scenario": slug,
            "source_language": source_language,
            "target_language": target_language,
            "_meta": {"fields": {}},
        }, base_path=scenario["base_path"])
        return {"path": destination, "target_language": target_language}
    source_directory = scenario["path"].resolve()
    destination = (root / game / target_language / slug).resolve()
    if destination.exists():
        raise ValueError("A project for this scenario and target language already exists")
    game_directory = (root / game).resolve()
    if game_directory not in source_directory.parents or game_directory not in destination.parents:
        raise ValueError("Invalid scenario path")

    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source_directory, destination, ignore=shutil.ignore_patterns("builds"))
    (destination / "builds").mkdir(exist_ok=True)

    project_path = destination / "project.json"
    project = _read_json(project_path) or {}
    source_language = str(project.get("source_language") or "en")
    project["target_language"] = target_language
    project["translated_title"] = ""
    project.pop("translation_rules", None)
    project_path.write_text(
        json.dumps(project, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    old_translation_path = destination / f"translation_{project_language}.json"
    data = _read_json(old_translation_path) or {}
    data["target_language"] = target_language
    for record in data.get("cards", []):
        if not isinstance(record, dict):
            continue
        text = record.get("text")
        if isinstance(text, dict):
            source_text = text.get(source_language)
            record["text"] = (
                {source_language: source_text} if isinstance(source_text, dict) else {}
            )
    fields = ((data.get("_meta") or {}).get("fields"))
    if isinstance(fields, dict):
        fields_to_keep = {
            key: value
            for key, value in fields.items()
            if ".text." not in key or f".text.{source_language}." in key
        }
        data.setdefault("_meta", {})["fields"] = fields_to_keep
    new_translation_path = destination / f"translation_{target_language}.json"
    new_translation_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    if old_translation_path != new_translation_path:
        old_translation_path.unlink(missing_ok=True)
    return {"path": destination, "target_language": target_language}


def update_shoggoth_settings(
    root: Path,
    *,
    game: str,
    slug: str,
    project_language: str,
    set_icon: str,
    encounter_icon: str = "",
    illustrator_prefix: str = "Illus. ",
) -> dict[str, str]:
    scenario = get_scenario(root, game, slug, language=project_language)
    if scenario is None or not scenario["initialized"]:
        raise ValueError("Scenario is not initialized")
    project_path = scenario["language_project_path"]
    project = _read_json(project_path) or {}
    project["shoggoth_set_icon"] = set_icon.strip()
    project["shoggoth_encounter_icon"] = encounter_icon.strip()
    project["shoggoth_illustrator_prefix"] = illustrator_prefix
    project_path.write_text(
        json.dumps(project, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return {
        "set_icon": project["shoggoth_set_icon"],
        "encounter_icon": project["shoggoth_encounter_icon"],
        "illustrator_prefix": project["shoggoth_illustrator_prefix"],
    }


def update_separate_card_art_setting(
    root: Path,
    *,
    game: str,
    slug: str,
    project_language: str,
    enabled: bool,
) -> dict[str, bool]:
    scenario = get_scenario(root, game, slug, language=project_language)
    if scenario is None or not scenario["initialized"]:
        raise ValueError("Scenario is not initialized")
    if not scenario.get("shared_layout"):
        raise ValueError("Separate card art requires the shared project layout")
    project_path = scenario["path"] / "project.json"
    project = _read_json(project_path) or {}
    project["use_separate_card_art"] = enabled
    write_json(project_path, project)
    return {"enabled": enabled}


def save_separate_card_art(
    root: Path,
    *,
    game: str,
    slug: str,
    project_language: str,
    card_key: str,
    side: str,
    filename: str,
    content: bytes,
) -> dict[str, str]:
    scenario = get_scenario(root, game, slug, language=project_language)
    if scenario is None or not scenario["initialized"]:
        raise ValueError("Scenario is not initialized")
    if not scenario.get("shared_layout"):
        raise ValueError("Separate card art requires the shared project layout")
    if side not in {"front", "back"}:
        raise ValueError("Unknown card side")
    if not any(card["key"] == card_key for card in scenario["grouped_cards"]):
        raise ValueError("Unknown card")
    suffix = Path(filename).suffix.casefold()
    if suffix not in IMAGE_SUFFIXES:
        raise ValueError("Card art must be a JPG, PNG or WebP image")
    project_path = scenario["path"] / "project.json"
    project = _read_json(project_path) or {}
    if project.get("use_separate_card_art") is not True:
        raise ValueError("Enable separate card art before uploading images")
    safe_key = re.sub(r"[^a-zA-Z0-9._-]+", "-", card_key).strip("-._") or "card"
    digest = sha256(card_key.encode("utf-8")).hexdigest()[:8]
    relative = Path("source") / "art" / f"{safe_key[:80]}-{digest}-{side}{suffix}"
    destination = scenario["path"] / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(content)
    art = project.setdefault("card_art", {})
    card_art = art.setdefault(card_key, {})
    card_art[side] = relative.as_posix()
    write_json(project_path, project)
    return {"path": relative.as_posix(), "card_key": card_key, "side": side}


def update_translation_completion(
    root: Path,
    *,
    game: str,
    slug: str,
    project_language: str,
    final_reviewed: bool,
) -> dict[str, Any]:
    scenario = get_scenario(root, game, slug, language=project_language)
    if scenario is None or not scenario["initialized"]:
        raise ValueError("Scenario is not initialized")
    project_path = scenario["language_project_path"]
    project = _read_json(project_path) or {}
    project["final_reviewed"] = final_reviewed
    write_json(project_path, project)
    return project


def initialize_scenario(
    root: Path,
    *,
    game: str,
    slug: str,
    title: str,
    translated_title: str,
    author: str,
    version: str,
    source_url: str,
    target_language: str,
    project_language: str | None = None,
    card_source: str = "",
) -> None:
    game_definition = get_game(root, game)
    if game_definition is None:
        raise ValueError(f"Unknown game: {game}")

    target_language = normalize_language_code(target_language)
    if project_language is not None:
        project_language = normalize_language_code(project_language)
    scenario_parent = root / game / project_language if project_language else root / game
    scenario_dir = (scenario_parent / slug).resolve()
    legacy_dir = (root / game / slug).resolve()
    if not scenario_dir.exists() and project_language and legacy_dir.exists():
        destination = (root / game / target_language / slug).resolve()
        if destination.exists():
            raise ValueError("A project with this game, language and scenario already exists")
        destination.parent.mkdir(parents=True, exist_ok=True)
        legacy_dir.rename(destination)
        scenario_dir = destination
    elif project_language is not None and project_language != target_language:
        raise ValueError("Folder language and target language must match")
    if not scenario_dir.exists() or (root / game).resolve() not in scenario_dir.parents:
        raise ValueError("Scenario folder does not exist")

    candidates = _find_card_sources(scenario_dir)
    chosen_source = card_source.strip()

    if chosen_source:
        valid_sources = {candidate["relative_path"] for candidate in candidates}
        if chosen_source not in valid_sources:
            raise ValueError("Selected card source does not exist or contains no supported images")
    else:
        inferred = _select_card_source(scenario_dir, None, candidates)
        if inferred:
            chosen_source = inferred["relative_path"]
        elif candidates:
            raise ValueError("Multiple card image sources found; please choose one")

    project = {
        "format": "card-translator-project",
        "format_version": 1,
        "game": game,
        "slug": slug,
        "title": title.strip() or _humanize_slug(slug),
        "translated_title": translated_title.strip(),
        "author": author.strip(),
        "version": version.strip(),
        "source_url": source_url.strip(),
        "source_language": str(game_definition.get("default_source_language") or "en"),
        "target_language": target_language,
        "card_source": chosen_source or None,
    }

    project_path = scenario_dir / "project.json"
    project_path.write_text(
        json.dumps(project, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    language = project["target_language"]
    translation_path = scenario_dir / f"translation_{language}.json"
    if not translation_path.exists():
        translation = {
            "format": "card-translator-translation",
            "format_version": 1,
            "game": game,
            "scenario": slug,
            "source_language": str(game_definition.get("default_source_language") or "en"),
            "target_language": language,
            "cards": [],
            "_meta": {
                "note": "Field-level provenance/confidence metadata will live here.",
                "images": {},
                "cards": {},
                "settings": {
                    "exclude_hidden_sides_from_output": True,
                },
            },
        }
        translation_path.write_text(
            json.dumps(translation, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    (scenario_dir / "snapshots").mkdir(exist_ok=True)
    (scenario_dir / "builds").mkdir(exist_ok=True)
