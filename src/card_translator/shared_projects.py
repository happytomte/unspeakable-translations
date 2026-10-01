"""Shared scenario storage with one extraction and separate language documents."""

from __future__ import annotations

import copy
import json
import re
from hashlib import sha256
from pathlib import Path
from typing import Any

PROJECTS_DIR = "projects"
CAMPAIGN_FILENAME = "campaign.json"
SCENARIOS_DIR = "scenarios"
BASE_FILENAME = "base/extraction.json"
BASE_CARDS_DIR = "cards"
LANGUAGE_FILENAME = "translation.json"
LANGUAGE_CARDS_DIR = "cards"

_CARD_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")


def shared_project_path(root: Path, game: str, slug: str) -> Path:
    return root / PROJECTS_DIR / game / slug


def campaign_path(root: Path, game: str, campaign: str) -> Path:
    return root / PROJECTS_DIR / game / campaign


def campaign_scenario_path(
    root: Path, game: str, campaign: str, scenario: str
) -> Path:
    return campaign_path(root, game, campaign) / SCENARIOS_DIR / scenario


def shared_scenario_directories(root: Path, game: str | None = None) -> list[Path]:
    """Return both campaign-nested scenarios and legacy top-level projects."""
    projects_root = root / PROJECTS_DIR
    game_directories = (
        [projects_root / game]
        if game
        else sorted(path for path in projects_root.glob("*") if path.is_dir())
    )
    result: list[Path] = []
    for game_dir in game_directories:
        if not game_dir.is_dir():
            continue
        for child in sorted(path for path in game_dir.iterdir() if path.is_dir()):
            scenarios_dir = child / SCENARIOS_DIR
            if (child / CAMPAIGN_FILENAME).is_file() and scenarios_dir.is_dir():
                result.extend(
                    sorted(path for path in scenarios_dir.iterdir() if path.is_dir())
                )
            elif (child / "project.json").is_file():
                result.append(child)
    return result


def campaign_for_scenario(path: Path) -> tuple[str | None, Path | None]:
    """Return the campaign ID/path for a nested scenario, or ``(None, None)``."""
    if path.parent.name != SCENARIOS_DIR:
        return None, None
    campaign = path.parent.parent
    if not (campaign / CAMPAIGN_FILENAME).is_file():
        return None, None
    return campaign.name, campaign


def language_path(project_path: Path, language: str) -> Path:
    return project_path / "translations" / language


def read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    if not path.is_file() or path.read_text(encoding="utf-8") != rendered:
        path.write_text(rendered, encoding="utf-8")


def _base_card_entries(manifest: dict[str, Any]) -> list[dict[str, str]]:
    entries = manifest.get("card_files")
    if not isinstance(entries, list):
        return []
    return [
        {"key": str(entry["key"]), "path": str(entry["path"])}
        for entry in entries
        if isinstance(entry, dict) and entry.get("key") and entry.get("path")
    ]


def _safe_card_path(base_path: Path, relative: str) -> Path | None:
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        return None
    resolved = base_path.parent / candidate
    try:
        resolved.relative_to(base_path.parent / BASE_CARDS_DIR)
    except ValueError:
        return None
    return resolved


def _card_filename(key: str, used: set[str]) -> str:
    stem = _CARD_FILENAME.sub("_", key).strip("._") or "card"
    stem = stem[:160]
    candidate = f"{stem}.json"
    if candidate.casefold() in used:
        digest = sha256(key.encode("utf-8")).hexdigest()[:10]
        candidate = f"{stem[:149]}-{digest}.json"
    used.add(candidate.casefold())
    return candidate


def read_base_document(path: Path) -> dict[str, Any] | None:
    """Read a shared extraction, including legacy embedded-card documents."""
    manifest = read_json(path)
    if manifest is None:
        return None
    if isinstance(manifest.get("cards"), list):
        return manifest

    entries = _base_card_entries(manifest)
    if not entries and manifest.get("format_version") != 2:
        return manifest
    result = copy.deepcopy(manifest)
    result.pop("card_files", None)
    result["cards"] = []
    combined_meta = result.setdefault("_meta", {})
    if not isinstance(combined_meta, dict):
        combined_meta = {}
        result["_meta"] = combined_meta
    fields = combined_meta.setdefault("fields", {})
    images = combined_meta.setdefault("images", {})
    cards_meta = combined_meta.setdefault("cards", {})
    seen_keys: set[str] = set()
    seen_paths: set[str] = set()
    for entry in entries:
        if entry["key"] in seen_keys or entry["path"] in seen_paths:
            return None
        card_path = _safe_card_path(path, entry["path"])
        payload = read_json(card_path) if card_path else None
        if payload is None or not isinstance(payload.get("card"), dict):
            return None
        card = copy.deepcopy(payload["card"])
        key = str(card.get("key") or card.get("id") or entry["key"])
        if key != entry["key"]:
            return None
        seen_keys.add(key)
        seen_paths.add(entry["path"])
        result["cards"].append(card)
        card_meta = payload.get("_meta") if isinstance(payload.get("_meta"), dict) else {}
        if isinstance(card_meta.get("card"), dict):
            cards_meta[key] = copy.deepcopy(card_meta["card"])
        if isinstance(card_meta.get("fields"), dict):
            fields.update(copy.deepcopy(card_meta["fields"]))
        if isinstance(card_meta.get("images"), dict):
            images.update(copy.deepcopy(card_meta["images"]))
    return result


def write_base_document(path: Path, value: dict[str, Any]) -> None:
    """Write one independently reviewable file for every extracted card."""
    document = copy.deepcopy(value)
    cards = [card for card in document.pop("cards", []) if isinstance(card, dict)]
    meta = document.get("_meta") if isinstance(document.get("_meta"), dict) else {}
    fields = meta.pop("fields", {}) if isinstance(meta.get("fields"), dict) else {}
    images = meta.pop("images", {}) if isinstance(meta.get("images"), dict) else {}
    cards_meta = meta.pop("cards", {}) if isinstance(meta.get("cards"), dict) else {}

    previous = read_json(path) or {}
    previous_entries = {
        entry["key"]: entry["path"] for entry in _base_card_entries(previous)
    }
    used = {Path(relative).name.casefold() for relative in previous_entries.values()}
    entries: list[dict[str, str]] = []
    written: set[str] = set()
    assigned_fields: set[str] = set()
    assigned_images: set[str] = set()
    assigned_cards: set[str] = set()
    for card in cards:
        key = str(card.get("key") or card.get("id") or "")
        if not key:
            continue
        relative = previous_entries.get(key)
        card_path = _safe_card_path(path, relative) if relative else None
        if card_path is None:
            filename = _card_filename(key, used)
            relative = f"{BASE_CARDS_DIR}/{filename}"
            card_path = path.parent / relative
        record_id = str(card.get("id") or key)
        prefixes = {f"{key}.", f"{record_id}."}
        card_fields = {
            field: copy.deepcopy(state)
            for field, state in fields.items()
            if any(str(field).startswith(prefix) for prefix in prefixes)
        }
        image_names = {
            Path(image).name
            for image in (card.get("images") or {}).values()
            if isinstance(image, str) and image
        }
        card_images = {
            image: copy.deepcopy(state)
            for image, state in images.items()
            if Path(str(image)).name in image_names
        }
        payload_meta: dict[str, Any] = {
            "fields": card_fields,
            "images": card_images,
        }
        state = cards_meta.get(key, cards_meta.get(record_id))
        if isinstance(state, dict):
            payload_meta["card"] = copy.deepcopy(state)
        payload_meta = {name: item for name, item in payload_meta.items() if item}
        write_json(card_path, {
            "format": "card-translator-card-extraction",
            "format_version": 1,
            "card": card,
            **({"_meta": payload_meta} if payload_meta else {}),
        })
        entries.append({"key": key, "path": relative})
        written.add(relative)
        assigned_fields.update(card_fields)
        assigned_images.update(card_images)
        assigned_cards.update({key, record_id})

    if remaining := {key: value for key, value in fields.items() if key not in assigned_fields}:
        meta["fields"] = remaining
    if remaining := {key: value for key, value in images.items() if key not in assigned_images}:
        meta["images"] = remaining
    if remaining := {key: value for key, value in cards_meta.items() if key not in assigned_cards}:
        meta["cards"] = remaining

    document["format"] = "card-translator-base"
    document["format_version"] = 2
    document["card_files"] = entries
    document["_meta"] = meta
    write_json(path, document)

    for entry in _base_card_entries(previous):
        if entry["path"] in written:
            continue
        stale = _safe_card_path(path, entry["path"])
        if stale and stale.is_file():
            stale.unlink()


def _translation_card_payloads(path: Path) -> list[tuple[Path, dict[str, Any]]] | None:
    cards_dir = path.parent / LANGUAGE_CARDS_DIR
    if not cards_dir.is_dir():
        return []
    payloads: list[tuple[Path, dict[str, Any]]] = []
    for card_path in sorted(cards_dir.glob("*.json")):
        payload = read_json(card_path)
        if (
            payload is None
            or payload.get("format") != "card-translator-card-translation"
            or not isinstance(payload.get("card"), dict)
        ):
            return None
        payloads.append((card_path, payload))
    return payloads


def read_translation_document(path: Path) -> dict[str, Any] | None:
    """Read a sparse per-card translation or a legacy embedded-card document."""
    manifest = read_json(path)
    if manifest is None:
        return None
    if isinstance(manifest.get("cards"), list):
        return manifest
    payloads = _translation_card_payloads(path)
    if payloads is None:
        return None
    result = copy.deepcopy(manifest)
    result["cards"] = []
    combined_meta = result.setdefault("_meta", {})
    if not isinstance(combined_meta, dict):
        combined_meta = {}
        result["_meta"] = combined_meta
    fields = combined_meta.setdefault("fields", {})
    seen_keys: set[str] = set()
    for _, payload in payloads:
        card = copy.deepcopy(payload["card"])
        key = str(card.get("key") or card.get("id") or "")
        if not key or key in seen_keys:
            return None
        seen_keys.add(key)
        result["cards"].append(card)
        card_meta = payload.get("_meta") if isinstance(payload.get("_meta"), dict) else {}
        if isinstance(card_meta.get("fields"), dict):
            fields.update(copy.deepcopy(card_meta["fields"]))
    return result


def write_translation_document(
    path: Path, value: dict[str, Any], *, base_path: Path | None = None
) -> None:
    """Write only translated cards, with each card isolated in its own file."""
    document = copy.deepcopy(value)
    cards = [card for card in document.pop("cards", []) if isinstance(card, dict)]
    meta = document.get("_meta") if isinstance(document.get("_meta"), dict) else {}
    fields = meta.pop("fields", {}) if isinstance(meta.get("fields"), dict) else {}

    previous_payloads = _translation_card_payloads(path)
    if previous_payloads is None:
        raise ValueError("Translation contains an invalid per-card file")
    previous_files = {
        str(payload["card"].get("key") or payload["card"].get("id")): card_path
        for card_path, payload in previous_payloads
    }
    base_files = {
        entry["key"]: Path(entry["path"]).name
        for entry in _base_card_entries(read_json(base_path) or {})
    } if base_path else {}
    cards_dir = path.parent / LANGUAGE_CARDS_DIR
    used = {card_path.name.casefold() for card_path in previous_files.values()}
    written: set[Path] = set()
    assigned_fields: set[str] = set()
    for card in cards:
        key = str(card.get("key") or card.get("id") or "")
        if not key:
            continue
        card_path = previous_files.get(key)
        if card_path is None:
            filename = base_files.get(key) or _card_filename(key, used)
            if filename.casefold() in used:
                filename = _card_filename(key, used)
            else:
                used.add(filename.casefold())
            card_path = cards_dir / filename
        record_id = str(card.get("id") or key)
        prefixes = {f"{key}.", f"{record_id}."}
        card_fields = {
            field: copy.deepcopy(state)
            for field, state in fields.items()
            if any(str(field).startswith(prefix) for prefix in prefixes)
        }
        write_json(card_path, {
            "format": "card-translator-card-translation",
            "format_version": 1,
            "card": card,
            **({"_meta": {"fields": card_fields}} if card_fields else {}),
        })
        written.add(card_path)
        assigned_fields.update(card_fields)

    if remaining := {key: value for key, value in fields.items() if key not in assigned_fields}:
        meta["fields"] = remaining
    document["format"] = "card-translator-translation"
    document["format_version"] = 3
    document["_meta"] = meta
    write_json(path, document)

    for card_path in previous_files.values():
        if card_path not in written and card_path.is_file():
            card_path.unlink()


def is_shared_scenario(scenario: dict[str, Any]) -> bool:
    return bool(scenario.get("shared_layout"))


def compose_documents(base: dict[str, Any], translation: dict[str, Any]) -> dict[str, Any]:
    """Present shared and language records in the old workbench shape."""
    result = copy.deepcopy(translation)
    language = str(translation.get("target_language") or "")
    result["source_language"] = base.get("source_language", "en")
    translated_by_key = {
        str(item.get("key") or item.get("id")): item
        for item in translation.get("cards", [])
        if isinstance(item, dict) and (item.get("key") or item.get("id"))
    }
    cards: list[dict[str, Any]] = []
    consumed: set[str] = set()
    for base_item in base.get("cards", []):
        if not isinstance(base_item, dict):
            continue
        item = copy.deepcopy(base_item)
        key = str(item.get("key") or item.get("id") or "")
        translated = translated_by_key.get(key)
        if translated:
            consumed.add(key)
            localized = (translated.get("text") or {}).get(language)
            if isinstance(localized, dict):
                item.setdefault("text", {})[language] = copy.deepcopy(localized)
            translated_images = translated.get("images") or {}
            references = translated_images.get("reference") if isinstance(translated_images, dict) else None
            if isinstance(references, dict):
                item.setdefault("images", {}).setdefault("reference", {}).update(copy.deepcopy(references))
        cards.append(item)
    for key, translated in translated_by_key.items():
        if key not in consumed:
            cards.append(copy.deepcopy(translated))
    result["cards"] = cards
    base_meta = base.get("_meta") if isinstance(base.get("_meta"), dict) else {}
    language_meta = translation.get("_meta") if isinstance(translation.get("_meta"), dict) else {}
    result["_meta"] = {**copy.deepcopy(base_meta), **copy.deepcopy(language_meta)}
    result["_meta"]["fields"] = {
        **copy.deepcopy(base_meta.get("fields") or {}),
        **copy.deepcopy(language_meta.get("fields") or {}),
    }
    return result


def split_documents(data: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Separate shared card facts from one language's text and review metadata."""
    language = str(data.get("target_language") or "")
    source_language = str(data.get("source_language") or "en")
    base_cards: list[dict[str, Any]] = []
    language_cards: list[dict[str, Any]] = []
    for item in data.get("cards", []):
        if not isinstance(item, dict):
            continue
        base_item = copy.deepcopy(item)
        text = base_item.get("text")
        localized = text.pop(language, None) if isinstance(text, dict) and language != source_language else None
        if not text:
            base_item.pop("text", None)
        image_references = None
        images = base_item.get("images")
        references = images.get("reference") if isinstance(images, dict) else None
        if isinstance(references, dict) and language in references:
            image_references = references.pop(language)
            if not references:
                images.pop("reference", None)
        base_cards.append(base_item)
        if (isinstance(localized, dict) and localized) or image_references is not None:
            language_item = {
                "id": item.get("id") or item.get("key"),
                "key": item.get("key") or item.get("id"),
            }
            if isinstance(localized, dict) and localized:
                language_item["text"] = {language: copy.deepcopy(localized)}
            if image_references is not None:
                language_item["images"] = {"reference": {language: copy.deepcopy(image_references)}}
            language_cards.append(language_item)

    meta = data.get("_meta") if isinstance(data.get("_meta"), dict) else {}
    fields = meta.get("fields") if isinstance(meta.get("fields"), dict) else {}
    base_meta = {key: copy.deepcopy(value) for key, value in meta.items() if key != "fields"}
    base_meta["fields"] = {
        key: copy.deepcopy(value)
        for key, value in fields.items()
        if f".text.{language}." not in key
    }
    language_meta = {"fields": {
        key: copy.deepcopy(value)
        for key, value in fields.items()
        if f".text.{language}." in key
    }}
    base = {
        "format": "card-translator-base",
        "format_version": 1,
        "game": data.get("game"),
        "scenario": data.get("scenario"),
        "source_language": source_language,
        "cards": base_cards,
        "_meta": base_meta,
    }
    translation = {
        **{key: copy.deepcopy(value) for key, value in data.items() if key not in {"cards", "_meta"}},
        "format": "card-translator-translation",
        "format_version": 2,
        "source_language": source_language,
        "target_language": language,
        "cards": language_cards,
        "_meta": language_meta,
    }
    return base, translation


def read_scenario_data(scenario: dict[str, Any]) -> dict[str, Any] | None:
    if scenario.get("source_only"):
        return read_base_document(scenario["base_path"])
    translation = read_translation_document(scenario["translation_path"])
    if not is_shared_scenario(scenario):
        return translation
    base = read_base_document(scenario["base_path"])
    if base is None or translation is None:
        return None
    return compose_documents(base, translation)


def write_scenario_data(scenario: dict[str, Any], data: dict[str, Any]) -> None:
    if scenario.get("source_only"):
        source_language = str(data.get("source_language") or "en")
        data = copy.deepcopy(data)
        data.pop("target_language", None)
        data.update({
            "format": "card-translator-base",
            "format_version": 1,
            "source_language": source_language,
        })
        write_base_document(scenario["base_path"], data)
        return
    project_path = scenario.get("language_project_path")
    if isinstance(project_path, Path):
        project = read_json(project_path)
        if project is not None and project.get("final_reviewed") is True:
            project["final_reviewed"] = False
            write_json(project_path, project)
    if not is_shared_scenario(scenario):
        write_json(scenario["translation_path"], data)
        return
    base, translation = split_documents(data)
    write_base_document(scenario["base_path"], base)
    write_translation_document(
        scenario["translation_path"], translation, base_path=scenario["base_path"]
    )
