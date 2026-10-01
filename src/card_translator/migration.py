"""Non-destructive migration from per-language folders to shared projects."""

from __future__ import annotations

import copy
import os
import shutil
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

from card_translator.library import discover_library
from card_translator.shared_projects import (
    BASE_FILENAME,
    LANGUAGE_FILENAME,
    language_path,
    read_json,
    shared_project_path,
    split_documents,
    write_base_document,
    write_json,
    write_translation_document,
)

LANGUAGE_PROJECT_KEYS = {
    "target_language", "translated_title", "translation_rules",
    "shoggoth_set_icon", "shoggoth_encounter_icon", "shoggoth_illustrator_prefix",
}


def _digest(path: Path) -> str:
    checksum = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def _link_or_copy(source: str, destination: str) -> str:
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)
    return destination


def migration_candidates(root: Path) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for game in discover_library(root):
        for scenario in game["scenarios"]:
            if scenario.get("initialized") and not scenario.get("shared_layout"):
                grouped.setdefault((scenario["game"], scenario["slug"]), []).append(scenario)
    return [
        {"game": game, "slug": slug, "languages": sorted(item["storage_language"] for item in rows)}
        for (game, slug), rows in sorted(grouped.items())
    ]


def migrate_project(root: Path, *, game: str, slug: str) -> dict[str, Any]:
    destination = shared_project_path(root, game, slug)
    if destination.exists():
        raise ValueError("Shared project already exists")
    scenarios = [
        scenario
        for game_entry in discover_library(root)
        if game_entry["slug"] == game
        for scenario in game_entry["scenarios"]
        if scenario["slug"] == slug and scenario.get("initialized")
        and not scenario.get("shared_layout")
    ]
    if not scenarios:
        raise ValueError("No initialized legacy project found")
    scenarios.sort(key=lambda item: item["storage_language"])
    primary = scenarios[0]
    primary_project = primary["project"]
    primary_cards = {image.name: image for image in primary["cards"]}
    if not primary_cards:
        raise ValueError("Project has no selected source card images")
    for scenario in scenarios[1:]:
        other_cards = {image.name: image for image in scenario["cards"]}
        if other_cards.keys() != primary_cards.keys():
            raise ValueError(f"Source card images differ for {scenario['storage_language']}")
        for name, path in other_cards.items():
            if _digest(path) != _digest(primary_cards[name]):
                raise ValueError(f"Source card image differs: {name}")

    staging = destination.parent / f".{slug}.migrating-{uuid4().hex}"
    try:
        staging.mkdir(parents=True)
        source_dir = (primary["path"] / primary["card_source"]).resolve()
        shutil.copytree(source_dir, staging / "source" / "cards", copy_function=_link_or_copy)
        snapshots = primary["path"] / "snapshots"
        if snapshots.is_dir():
            shutil.copytree(snapshots, staging / "snapshots", copy_function=_link_or_copy)

        shared_project = {
            key: copy.deepcopy(value)
            for key, value in primary_project.items()
            if key not in LANGUAGE_PROJECT_KEYS
        }
        shared_project["format"] = "card-translator-project"
        shared_project["format_version"] = 2
        shared_project["card_source"] = "source/cards"
        write_json(staging / "project.json", shared_project)

        base_document: dict[str, Any] | None = None
        translations: list[tuple[str, dict[str, Any]]] = []
        for scenario in scenarios:
            data = scenario["translation_path"]
            document = read_json(data)
            if document is None:
                raise ValueError(f"Invalid translation data for {scenario['storage_language']}")
            base, translation = split_documents(document)
            for record in base["cards"]:
                images = record.get("images")
                if isinstance(images, dict):
                    for side in ("front", "back"):
                        value = images.get(side)
                        if isinstance(value, str) and value:
                            images[side] = f"source/cards/{Path(value).name}"
            if base_document is None:
                base_document = base
            else:
                existing = {
                    str(item.get("key") or item.get("id")): item
                    for item in base_document["cards"] if isinstance(item, dict)
                }
                for record in base["cards"]:
                    key = str(record.get("key") or record.get("id"))
                    if key in existing and existing[key] != record:
                        raise ValueError(f"Shared extraction differs across languages: {key}")
                    if key not in existing:
                        base_document["cards"].append(record)
                for meta_key, meta_value in base.get("_meta", {}).items():
                    current = base_document.setdefault("_meta", {}).get(meta_key)
                    if isinstance(current, dict) and isinstance(meta_value, dict):
                        for key, value in meta_value.items():
                            if key in current and current[key] != value:
                                raise ValueError(f"Shared extraction metadata differs: {meta_key}.{key}")
                            current[key] = value
                    elif current is None:
                        base_document["_meta"][meta_key] = meta_value
                    elif current != meta_value:
                        raise ValueError(f"Shared extraction metadata differs: {meta_key}")
            lang = scenario["storage_language"]
            language_project = {
                key: copy.deepcopy(value)
                for key, value in scenario["project"].items()
                if key in LANGUAGE_PROJECT_KEYS
            }
            language_project["target_language"] = lang
            write_json(language_path(staging, lang) / "project.json", language_project)
            translations.append((lang, translation))

        if base_document is None:
            raise ValueError("Project has no extraction data")
        write_base_document(staging / BASE_FILENAME, base_document)
        for lang, translation in translations:
            write_translation_document(
                language_path(staging, lang) / LANGUAGE_FILENAME,
                translation,
                base_path=staging / BASE_FILENAME,
            )
        staging.rename(destination)
        return {"game": game, "slug": slug, "languages": [item["storage_language"] for item in scenarios], "path": destination}
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
