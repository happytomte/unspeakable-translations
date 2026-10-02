"""Create local shared projects and publish reviewed working files."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any
from uuid import uuid4

from card_translator.games import get_game
from card_translator.library import get_scenario, normalize_language_code
from card_translator.project_documents import missing_document_translations
from card_translator.shared_projects import (
    BASE_FILENAME,
    CAMPAIGN_FILENAME,
    LANGUAGE_FILENAME,
    campaign_for_scenario,
    campaign_path,
    campaign_scenario_path,
    language_path,
    read_base_document,
    read_json,
    read_translation_document,
    shared_scenario_directories,
    write_base_document,
    write_json,
    write_translation_document,
)

PROJECT_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
PUBLISH_IGNORED_NAMES = {"logs", "snapshots", "__pycache__", ".DS_Store"}
PROJECT_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
MAX_PROJECT_IMAGE_SIZE = 15 * 1024 * 1024


def _normalize_slug(value: str, label: str) -> str:
    value = value.strip().casefold()
    if not PROJECT_SLUG.fullmatch(value):
        raise ValueError(f"{label} ID must contain lowercase letters, numbers and hyphens")
    return value


def create_campaign(
    root: Path,
    *,
    game: str,
    slug: str,
    title: str,
    author: str,
    source_url: str,
    source_language: str,
    private_only: bool = False,
) -> dict[str, Any]:
    """Create the metadata container that owns one or more scenarios."""
    if get_game(root, game) is None:
        raise ValueError("Unknown game")
    slug = _normalize_slug(slug, "Campaign")
    source_language = normalize_language_code(source_language)
    destination = campaign_path(root, game, slug)
    if destination.exists():
        raise ValueError("A campaign with this game and ID already exists")
    campaign = {
        "format": "card-translator-campaign",
        "format_version": 1,
        "game": game,
        "slug": slug,
        "title": title.strip() or slug.replace("-", " ").title(),
        "author": author.strip(),
        "source_url": source_url.strip(),
        "source_language": source_language,
        "private_only": private_only,
    }
    write_json(destination / CAMPAIGN_FILENAME, campaign)
    (destination / "scenarios").mkdir(parents=True, exist_ok=True)
    return {"path": destination, **campaign}


def save_project_image(
    root: Path,
    *,
    game: str,
    slug: str,
    kind: str,
    filename: str,
    content: bytes,
) -> Path:
    """Store shared project artwork and reference it from the base project."""
    if kind not in {"mood", "cover"}:
        raise ValueError("Unknown project image kind")
    suffix = Path(filename).suffix.lower()
    if suffix not in PROJECT_IMAGE_SUFFIXES:
        raise ValueError("Project images must be JPG, PNG, or WebP files")
    if not content:
        raise ValueError("Project image is empty")
    if len(content) > MAX_PROJECT_IMAGE_SIZE:
        raise ValueError("Project image exceeds the 15 MB limit")
    scenario = get_scenario(root, game, slug, language="source")
    if scenario is None or not scenario.get("shared_layout"):
        raise ValueError("Shared project does not exist")
    project_path = Path(scenario["path"])
    media = project_path / "source" / "media"
    media.mkdir(parents=True, exist_ok=True)
    for old_suffix in PROJECT_IMAGE_SUFFIXES:
        (media / f"{kind}{old_suffix}").unlink(missing_ok=True)
    destination = media / f"{kind}{suffix}"
    destination.write_bytes(content)
    project = read_json(project_path / "project.json") or {}
    project[f"{kind}_image"] = destination.relative_to(project_path).as_posix()
    write_json(project_path / "project.json", project)
    return destination


def create_campaign_scenario(
    root: Path,
    *,
    game: str,
    campaign: str,
    slug: str,
    title: str,
    private_only: bool = False,
) -> dict[str, Any]:
    """Create a scenario and all of its working-file directories in a campaign."""
    campaign = _normalize_slug(campaign, "Campaign")
    slug = _normalize_slug(slug, "Scenario")
    campaign_dir = campaign_path(root, game, campaign)
    campaign_data = read_json(campaign_dir / CAMPAIGN_FILENAME)
    if campaign_data is None or campaign_data.get("game") != game:
        raise ValueError("Campaign does not exist")
    destination = campaign_scenario_path(root, game, campaign, slug)
    if any(path.name == slug for path in shared_scenario_directories(root, game)):
        raise ValueError("Scenario IDs must be unique within a game")
    if destination.exists():
        raise ValueError("A scenario with this ID already exists in the campaign")
    source_language = normalize_language_code(
        str(campaign_data.get("source_language") or "en")
    )
    project = {
        "format": "card-translator-project",
        "format_version": 3,
        "game": game,
        "campaign": campaign,
        "slug": slug,
        "title": title.strip() or slug.replace("-", " ").title(),
        "source_language": source_language,
        "card_source": "source/cards",
        "private_only": private_only,
        "private_only_override": "private" if private_only else "inherit",
    }
    base = {
        "format": "card-translator-base",
        "format_version": 1,
        "game": game,
        "campaign": campaign,
        "scenario": slug,
        "source_language": source_language,
        "cards": [],
        "_meta": {"fields": {}, "images": {}, "cards": {}},
    }
    (destination / "source" / "cards").mkdir(parents=True)
    write_json(destination / "project.json", project)
    write_base_document(destination / BASE_FILENAME, base)
    return {
        "path": destination,
        "game": game,
        "campaign": campaign,
        "slug": slug,
        "cards_path": destination / "source" / "cards",
    }


def campaign_summaries(root: Path) -> list[dict[str, Any]]:
    """List campaign metadata, including campaigns that do not have scenarios yet."""
    campaigns: list[dict[str, Any]] = []
    projects_root = root / "projects"
    if not projects_root.is_dir():
        return campaigns
    for path in sorted(projects_root.glob("*/*")):
        data = read_json(path / CAMPAIGN_FILENAME)
        if not path.is_dir() or data is None:
            continue
        scenarios_dir = path / "scenarios"
        scenarios = sorted(
            item.name for item in scenarios_dir.iterdir()
            if item.is_dir() and (item / "project.json").is_file()
        ) if scenarios_dir.is_dir() else []
        campaigns.append({
            **data,
            "game": str(data.get("game") or path.parent.name),
            "slug": str(data.get("slug") or path.name),
            "title": str(data.get("title") or path.name.replace("-", " ").title()),
            "path": path,
            "scenario_count": len(scenarios),
            "scenarios": scenarios,
        })
    return campaigns


def create_shared_project(
    root: Path,
    *,
    game: str,
    slug: str,
    title: str,
    author: str,
    source_url: str,
    source_language: str,
    private_only: bool = False,
) -> dict[str, Any]:
    """Create shared scenario data without implicitly starting a translation."""
    if get_game(root, game) is None:
        raise ValueError("Unknown game")
    slug = slug.strip().casefold()
    if not PROJECT_SLUG.fullmatch(slug):
        raise ValueError("Project ID must contain lowercase letters, numbers and hyphens")
    source_language = normalize_language_code(source_language)
    destination = root / "projects" / game / slug
    if destination.exists():
        raise ValueError("A project with this game and ID already exists")

    project = {
        "format": "card-translator-project",
        "format_version": 2,
        "game": game,
        "slug": slug,
        "title": title.strip() or slug.replace("-", " ").title(),
        "author": author.strip(),
        "source_url": source_url.strip(),
        "source_language": source_language,
        "card_source": "source/cards",
        "private_only": private_only,
        "private_only_override": "private" if private_only else "inherit",
    }
    base = {
        "format": "card-translator-base",
        "format_version": 1,
        "game": game,
        "scenario": slug,
        "source_language": source_language,
        "cards": [],
        "_meta": {"fields": {}, "images": {}, "cards": {}},
    }
    (destination / "source" / "cards").mkdir(parents=True)
    write_json(destination / "project.json", project)
    write_base_document(destination / BASE_FILENAME, base)
    return {
        "path": destination,
        "game": game,
        "slug": slug,
        "cards_path": destination / "source" / "cards",
    }


def update_campaign_private_only(
    root: Path, *, game: str, slug: str, private_only: bool
) -> dict[str, Any]:
    path = campaign_path(root, game, _normalize_slug(slug, "Campaign"))
    campaign = read_json(path / CAMPAIGN_FILENAME)
    if campaign is None or campaign.get("game") != game:
        raise ValueError("Campaign does not exist")
    campaign["private_only"] = private_only
    write_json(path / CAMPAIGN_FILENAME, campaign)
    return campaign


def update_scenario_private_only(
    root: Path,
    *,
    game: str,
    slug: str,
    private_only: bool | None = None,
    mode: str | None = None,
) -> dict[str, Any]:
    scenario = get_scenario(root, game, slug, language="source")
    if scenario is None or not scenario.get("shared_layout"):
        raise ValueError("Scenario does not exist")
    path = Path(scenario["path"]) / "project.json"
    project = read_json(path)
    if project is None:
        raise ValueError("Scenario project metadata does not exist")
    resolved_mode = mode or ("private" if private_only else "inherit")
    if resolved_mode not in {"inherit", "private", "public"}:
        raise ValueError("Unknown private-only mode")
    project["private_only_override"] = resolved_mode
    project["private_only"] = resolved_mode == "private"
    write_json(path, project)
    return project


def create_project_translation(
    root: Path, *, game: str, slug: str, target_language: str,
    campaign: str | None = None,
) -> dict[str, Any]:
    """Add a blank target-language document to an existing shared project."""
    game = game.strip()
    slug = slug.strip().casefold()
    if get_game(root, game) is None:
        raise ValueError("Unknown game")
    if not PROJECT_SLUG.fullmatch(slug):
        raise ValueError("Invalid project ID")
    target_language = normalize_language_code(target_language)
    if campaign:
        project_path = campaign_scenario_path(
            root, game, _normalize_slug(campaign, "Campaign"), slug
        )
    else:
        matches = [
            path for path in shared_scenario_directories(root, game)
            if path.name == slug
        ]
        if len(matches) > 1:
            raise ValueError("Scenario ID is ambiguous; choose a campaign")
        project_path = matches[0] if matches else root / "projects" / game / slug
    project = read_json(project_path / "project.json")
    if project is None or project.get("format_version") not in {2, 3}:
        raise ValueError("Shared project does not exist")
    if project.get("game") != game or project.get("slug") != slug:
        raise ValueError("Project metadata does not match its path")
    source_language = normalize_language_code(str(project.get("source_language") or "en"))
    if source_language == target_language:
        raise ValueError("Source and target language must be different")
    destination = language_path(project_path, target_language)
    if destination.exists():
        raise ValueError("A translation for this language already exists")
    write_json(destination / "project.json", {
        "target_language": target_language,
        "translated_title": "",
        "final_reviewed": False,
    })
    write_translation_document(destination / LANGUAGE_FILENAME, {
        "format": "card-translator-translation",
        "format_version": 3,
        "game": game,
        **({"campaign": project.get("campaign")} if project.get("campaign") else {}),
        "scenario": slug,
        "source_language": source_language,
        "target_language": target_language,
        "_meta": {"fields": {}},
    }, base_path=project_path / BASE_FILENAME)
    return {
        "path": project_path,
        "game": game,
        "campaign": project.get("campaign"),
        "slug": slug,
        "target_language": target_language,
    }


def shared_project_summaries(root: Path) -> list[dict[str, Any]]:
    """List shared projects even when they do not contain a translation yet."""
    projects_root = root / "projects"
    if not projects_root.is_dir():
        return []
    image_suffixes = {".jpg", ".jpeg", ".png", ".webp"}
    summaries: list[dict[str, Any]] = []
    for path in shared_scenario_directories(root):
        project = read_json(path / "project.json")
        if project is None or project.get("format_version") not in {2, 3}:
            continue
        campaign, campaign_dir = campaign_for_scenario(path)
        campaign_data = read_json(campaign_dir / CAMPAIGN_FILENAME) if campaign_dir else None
        game = str(project.get("game") or (campaign_dir.parent.name if campaign_dir else path.parent.name))
        slug = str(project.get("slug") or path.name)
        translations_dir = path / "translations"
        languages = sorted(
            item.name
            for item in translations_dir.iterdir()
            if item.is_dir() and (item / LANGUAGE_FILENAME).is_file()
        ) if translations_dir.is_dir() else []
        card_source = str(project.get("card_source") or "source/cards")
        cards_path = path / card_source
        card_count = sum(
            1 for item in cards_path.iterdir()
            if item.is_file() and item.suffix.casefold() in image_suffixes
        ) if cards_path.is_dir() else 0
        summaries.append({
            "game": game,
            "campaign": campaign,
            "campaign_title": str((campaign_data or {}).get("title") or ""),
            "slug": slug,
            "title": str(project.get("title") or slug.replace("-", " ").title()),
            "source_language": str(project.get("source_language") or "en"),
            "languages": languages,
            "card_count": card_count,
            "cards_path": cards_path,
            "linked": (path / ".card-translator-remote.json").is_file(),
        })
    return summaries


def _card_states(
    document: dict[str, Any] | None, *, include_base_meta: bool = False
) -> dict[str, str]:
    if not document:
        return {}
    fields = ((document.get("_meta") or {}).get("fields") or {})
    fields = fields if isinstance(fields, dict) else {}
    states: dict[str, str] = {}
    for card in document.get("cards") or []:
        if not isinstance(card, dict):
            continue
        key = str(card.get("key") or card.get("id") or "")
        if not key:
            continue
        record_id = str(card.get("id") or key)
        prefixes = {f"{key}.", f"{record_id}."}
        card_fields = {
            path: value
            for path, value in fields.items()
            if any(str(path).startswith(prefix) for prefix in prefixes)
        }
        state: dict[str, Any] = {"card": card, "fields": card_fields}
        if include_base_meta:
            meta = document.get("_meta") or {}
            cards_meta = meta.get("cards") if isinstance(meta, dict) else {}
            images_meta = meta.get("images") if isinstance(meta, dict) else {}
            cards_meta = cards_meta if isinstance(cards_meta, dict) else {}
            images_meta = images_meta if isinstance(images_meta, dict) else {}
            image_names = {
                Path(image).name
                for image in (card.get("images") or {}).values()
                if isinstance(image, str) and image
            }
            state["card_meta"] = cards_meta.get(key, cards_meta.get(record_id))
            state["images"] = {
                image: value
                for image, value in images_meta.items()
                if Path(str(image)).name in image_names
            }
        states[key] = json.dumps(
            state,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    return states


def _published_scenario_path(
    scenario: dict[str, Any], repository_root: Path
) -> Path:
    campaign = scenario.get("campaign")
    if campaign:
        return (
            repository_root / "projects" / str(scenario["game"]) / str(campaign)
            / "scenarios" / str(scenario["slug"])
        )
    return repository_root / "projects" / str(scenario["game"]) / str(scenario["slug"])


def changed_translation_cards(
    scenario: dict[str, Any], repository_root: Path
) -> set[str]:
    """Return local card keys that differ from the tracked published translation."""
    local_path = Path(scenario["translation_path"])
    published_path = (
        _published_scenario_path(scenario, repository_root)
        / "translations" / str(scenario["storage_language"]) / LANGUAGE_FILENAME
    )

    local = _card_states(read_translation_document(local_path))
    published = _card_states(read_translation_document(published_path))
    return {
        key for key in local.keys() | published.keys()
        if local.get(key) != published.get(key)
    }


def changed_base_cards(
    scenario: dict[str, Any], repository_root: Path
) -> set[str]:
    """Return card keys whose shared extraction differs from the publication."""
    local_path = Path(scenario["path"]) / BASE_FILENAME
    published_path = _published_scenario_path(scenario, repository_root) / BASE_FILENAME
    local = _card_states(read_base_document(local_path), include_base_meta=True)
    published = _card_states(read_base_document(published_path), include_base_meta=True)
    return {
        key for key in local.keys() | published.keys()
        if local.get(key) != published.get(key)
    }


def _repository_scope_paths(
    scenario: dict[str, Any], repository_root: Path
) -> list[str]:
    destination = _published_scenario_path(scenario, repository_root)
    paths = [
        destination / "project.json",
        destination / "source",
        destination / "base",
    ]
    if not scenario.get("source_only"):
        paths.append(
            destination / "translations" / str(scenario["storage_language"])
        )
    if scenario.get("campaign"):
        campaign_destination = destination.parent.parent
        paths.extend([
            campaign_destination / CAMPAIGN_FILENAME,
            campaign_destination / "documents" / "source",
        ])
        if not scenario.get("source_only"):
            paths.append(
                campaign_destination / "documents" / "translations"
                / str(scenario["storage_language"])
            )
    return [path.relative_to(repository_root).as_posix() for path in paths]


def _repository_changed_files(
    scenario: dict[str, Any], repository_root: Path
) -> tuple[bool, set[str]]:
    """Return uncommitted files in this publication scope."""
    if not (repository_root / ".git").exists():
        return False, set()
    status = subprocess.run(
        [
            "git",
            "-c",
            "core.quotepath=false",
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--",
            *_repository_scope_paths(scenario, repository_root),
        ],
        cwd=repository_root,
        check=False,
        capture_output=True,
        text=True,
    )
    if status.returncode != 0:
        return False, set()
    records = status.stdout.split("\0")
    changed: set[str] = set()
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if len(record) < 4:
            continue
        state = record[:2]
        changed.add(record[3:])
        if "R" in state or "C" in state:
            if index < len(records) and records[index]:
                changed.add(records[index])
            index += 1
    return True, changed


def _repository_changed_card_keys(
    repository_root: Path, changed_files: set[str]
) -> set[str]:
    keys: set[str] = set()
    for relative in changed_files:
        if not re.search(
            r"/(?:base/cards|translations/[^/]+/cards)/[^/]+\.json$",
            f"/{relative}",
        ):
            continue
        payload = read_json(repository_root / relative)
        card = payload.get("card") if isinstance(payload, dict) else None
        key = (
            str(card.get("key") or card.get("id") or "")
            if isinstance(card, dict)
            else ""
        )
        keys.add(key or Path(relative).stem)
    return keys


def _completed_card_keys(scenario: dict[str, Any]) -> set[str]:
    side_group = "source_sides" if scenario.get("source_only") else "target_sides"
    return {
        str(card["key"])
        for card in scenario.get("grouped_cards") or []
        if isinstance(card, dict)
        and card.get("key")
        and any(
            isinstance(value, str) and value.strip()
            for side in (card.get(side_group) or {}).values()
            if isinstance(side, dict)
            for value in side.values()
        )
    }


def publication_status(
    scenario: dict[str, Any], repository_root: Path
) -> dict[str, Any]:
    destination = _published_scenario_path(scenario, repository_root)
    base_changed = changed_base_cards(scenario, repository_root)
    source_only = bool(scenario.get("source_only"))
    translation_changed = (
        set() if source_only else changed_translation_cards(scenario, repository_root)
    )
    changed = base_changed | translation_changed
    publication_path = (
        destination / BASE_FILENAME
        if source_only
        else destination / "translations" / str(scenario["storage_language"])
    )
    git_available, repository_changed_files = _repository_changed_files(
        scenario, repository_root
    )
    repository_changed_cards = _repository_changed_card_keys(
        repository_root, repository_changed_files
    )
    completed_cards = _completed_card_keys(scenario)
    local_completed_cards = changed & completed_cards
    ready_completed_cards = (
        repository_changed_cards - local_completed_cards
    ) & completed_cards
    contributed_cards = completed_cards - local_completed_cards - ready_completed_cards
    total_cards = int(scenario.get("translation_total") or 0)
    missing_documents = missing_document_translations(scenario)

    def percent(count: int) -> float:
        return round((count / total_cards) * 100, 2) if total_cards else 0

    return {
        "private_only": bool(scenario.get("private_only")),
        "scenario_private_only": bool(scenario.get("scenario_private_only")),
        "campaign_private_only": bool(scenario.get("campaign_private_only")),
        "published": publication_path.exists(),
        "changed_cards": changed,
        "changed_count": len(changed),
        "base_changed_cards": base_changed,
        "base_changed_count": len(base_changed),
        "translation_changed_cards": translation_changed,
        "translation_changed_count": len(translation_changed),
        "git_available": git_available,
        "repository_changed_files": repository_changed_files,
        "repository_changed_file_count": len(repository_changed_files),
        "repository_changed_cards": repository_changed_cards,
        "repository_changed_card_count": len(repository_changed_cards),
        "ready_for_pull_request": bool(repository_changed_files),
        "missing_document_translations": missing_documents,
        "missing_document_count": len(missing_documents),
        "completed_count": len(completed_cards),
        "local_completed_count": len(local_completed_cards),
        "ready_completed_count": len(ready_completed_cards),
        "contributed_count": len(contributed_cards),
        "local_completed_percent": percent(len(local_completed_cards)),
        "ready_completed_percent": percent(len(ready_completed_cards)),
        "contributed_percent": percent(len(contributed_cards)),
        "contribution_type": "base" if source_only else "translation",
        "destination": destination,
    }


def _published_file_map(root: Path) -> dict[Path, Path]:
    if not root.is_dir():
        return {}
    return {
        path.relative_to(root): path
        for path in root.rglob("*")
        if path.is_file()
        and not any(part in PUBLISH_IGNORED_NAMES for part in path.relative_to(root).parts)
    }


def _files_equal(left: Path, right: Path) -> bool:
    try:
        if left.stat().st_size != right.stat().st_size:
            return False
        with left.open("rb") as left_file, right.open("rb") as right_file:
            while True:
                left_chunk = left_file.read(1024 * 1024)
                right_chunk = right_file.read(1024 * 1024)
                if left_chunk != right_chunk:
                    return False
                if not left_chunk:
                    return True
    except OSError:
        return False


def _scope_file_changes(
    source: Path,
    destination: Path,
    *,
    repository_root: Path,
) -> list[dict[str, Any]]:
    if source.is_file():
        local_files = {Path(): source}
        published_files = {Path(): destination} if destination.is_file() else {}
    elif source.is_dir():
        local_files = _published_file_map(source)
        published_files = _published_file_map(destination)
    else:
        return []

    changes: list[dict[str, Any]] = []
    for relative in sorted(local_files.keys() | published_files.keys()):
        local = local_files.get(relative)
        published = published_files.get(relative)
        if local is None:
            status = "deleted"
            display_path = destination / relative
        elif published is None:
            status = "added"
            display_path = destination / relative
        elif _files_equal(local, published):
            continue
        else:
            status = "modified"
            display_path = destination / relative
        changes.append({
            "path": display_path.relative_to(repository_root).as_posix(),
            "status": status,
            "size": local.stat().st_size if local is not None else None,
        })
    return changes


def _cards_by_key(document: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    return {
        str(card.get("key") or card.get("id")): card
        for card in (document or {}).get("cards") or []
        if isinstance(card, dict) and (card.get("key") or card.get("id"))
    }


def _card_value_labels(
    scenario: dict[str, Any],
) -> tuple[dict[str, str], dict[str, str], dict[str, str], dict[str, str]]:
    schema = (scenario.get("game_definition") or {}).get("card_schema") or {}
    metadata = {
        str(field.get("id")): str(field.get("label") or field.get("id"))
        for field in schema.get("metadata_fields") or []
        if isinstance(field, dict) and field.get("id")
    }
    translatable = {
        str(field.get("id")): str(field.get("label") or field.get("id"))
        for field in schema.get("translatable_fields") or []
        if isinstance(field, dict) and field.get("id")
    }
    metadata_types = {
        str(field.get("id")): str(field.get("type") or "text")
        for field in schema.get("metadata_fields") or []
        if isinstance(field, dict) and field.get("id")
    }
    translatable_types = {
        str(field.get("id")): str(field.get("type") or "text")
        for field in schema.get("translatable_fields") or []
        if isinstance(field, dict) and field.get("id")
    }
    return metadata, translatable, metadata_types, translatable_types


def _canonical_card_values(
    card: dict[str, Any] | None,
    *,
    language: str,
) -> dict[str, Any]:
    if card is None:
        return {}
    values: dict[str, Any] = {}
    if "card_type" in card:
        values["metadata.card_type"] = card.get("card_type")
    for field, value in (card.get("game_data") or {}).items():
        if field == "sides" and isinstance(value, dict):
            for side, side_values in value.items():
                if not isinstance(side_values, dict):
                    continue
                for side_field, side_value in side_values.items():
                    values[f"metadata.{side}.{side_field}"] = side_value
        else:
            values[f"metadata.{field}"] = value
    for side, value in (card.get("images") or {}).items():
        values[f"image.{side}"] = value
    shoggoth = card.get("shoggoth")
    if isinstance(shoggoth, dict):
        pending = [("shoggoth", shoggoth)]
        while pending:
            prefix, value = pending.pop()
            if isinstance(value, dict):
                pending.extend(
                    (f"{prefix}.{field}", nested)
                    for field, nested in value.items()
                )
            else:
                values[prefix] = value

    language_text = (card.get("text") or {}).get(language) or {}
    sides = language_text.get("sides") if isinstance(language_text, dict) else {}
    if isinstance(sides, dict) and sides:
        for side, side_values in sides.items():
            if not isinstance(side_values, dict):
                continue
            for field, value in side_values.items():
                values[f"text.{side}.{field}"] = value
    elif isinstance(language_text, dict):
        for field, value in language_text.items():
            if field != "sides":
                values[f"text.front.{field}"] = value
    return values


def _diff_value(value: Any) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _diff_field_label(
    path: str,
    metadata_labels: dict[str, str],
    translatable_labels: dict[str, str],
) -> str:
    group, _, remainder = path.partition(".")
    if group == "metadata":
        side, separator, field = remainder.partition(".")
        if separator:
            field_label = metadata_labels.get(
                field, field.replace("_", " ").title()
            )
            return f"{side.replace('_', ' ').title()} · {field_label}"
        return metadata_labels.get(remainder, remainder.replace("_", " ").title())
    side, _, field = remainder.partition(".")
    side_label = side.replace("_", " ").title()
    if group == "image":
        return f"{side_label} · Image"
    if group == "shoggoth":
        return f"Shoggoth · {remainder.replace('.', ' · ').replace('_', ' ').title()}"
    field_label = translatable_labels.get(field, field.replace("_", " ").title())
    return f"{side_label} · {field_label}"


def _card_image_urls(
    card: dict[str, Any] | None, scenario: dict[str, Any]
) -> list[dict[str, Any]]:
    if not card:
        return []
    urls: list[dict[str, Any]] = []
    project_path = Path(scenario["path"])
    card_key = str(card.get("key") or card.get("id") or "")
    logical_card = next(
        (
            item for item in scenario.get("grouped_cards") or []
            if isinstance(item, dict) and str(item.get("key") or "") == card_key
        ),
        {},
    )
    for side in ("front", "back"):
        relative = (card.get("images") or {}).get(side)
        if not isinstance(relative, str) or not relative:
            continue
        if not (project_path / relative).is_file():
            continue
        urls.append({
            "side": side,
            "url": f"/library/{scenario['library_relative_path']}/{relative}",
            "rotation": int((logical_card.get(side) or {}).get("display_rotation") or 0),
        })
    return urls


def _card_content_diffs(
    scenario: dict[str, Any],
    repository_root: Path,
    status: dict[str, Any],
) -> list[dict[str, Any]]:
    source_path = Path(scenario["path"])
    published_path = _published_scenario_path(scenario, repository_root)
    local_base_document = read_base_document(source_path / BASE_FILENAME)
    published_base_document = read_base_document(published_path / BASE_FILENAME)
    local_base = _cards_by_key(local_base_document)
    published_base = _cards_by_key(published_base_document)
    source_language = str(
        (scenario.get("project") or {}).get("source_language") or "en"
    )
    target_language = str(scenario.get("storage_language") or "")
    local_translation: dict[str, dict[str, Any]] = {}
    published_translation: dict[str, dict[str, Any]] = {}
    if not scenario.get("source_only"):
        local_translation = _cards_by_key(read_translation_document(
            source_path / "translations" / target_language / LANGUAGE_FILENAME
        ))
        published_translation = _cards_by_key(read_translation_document(
            published_path / "translations" / target_language / LANGUAGE_FILENAME
        ))

    (
        metadata_labels,
        translatable_labels,
        metadata_types,
        translatable_types,
    ) = _card_value_labels(scenario)
    changed_keys = sorted(
        status["base_changed_cards"] | status["translation_changed_cards"]
    )
    result: list[dict[str, Any]] = []
    missing = object()
    logical_cards = {
        str(card.get("key")): card
        for card in scenario.get("grouped_cards") or []
        if isinstance(card, dict) and card.get("key")
    }
    card_positions = {key: index for index, key in enumerate(logical_cards, start=1)}
    for key in changed_keys:
        local_base_card = local_base.get(key)
        published_base_card = published_base.get(key)
        local_translation_card = local_translation.get(key)
        published_translation_card = published_translation.get(key)
        published_source_values = _canonical_card_values(
            published_base_card, language=source_language
        )
        local_source_values = _canonical_card_values(
            local_base_card, language=source_language
        )
        published_translation_values = _canonical_card_values(
            published_translation_card, language=target_language
        )
        local_translation_values = _canonical_card_values(
            local_translation_card, language=target_language
        )
        if not scenario.get("source_only"):
            for path, value in published_source_values.items():
                if not path.startswith("text."):
                    published_translation_values.setdefault(path, value)
            for path, value in local_source_values.items():
                if not path.startswith("text."):
                    local_translation_values.setdefault(path, value)
        paths = set(
            published_source_values.keys()
            | local_source_values.keys()
            | published_translation_values.keys()
            | local_translation_values.keys()
        )
        logical_card = logical_cards.get(key) or {}
        visible_fields = set(logical_card.get("visible_field_ids") or [])
        paths.update(
            f"metadata.{field}"
            for field in metadata_labels
            if not visible_fields or field in visible_fields
        )
        present_sides = {"front"}
        for values in (
            published_source_values,
            local_source_values,
            published_translation_values,
            local_translation_values,
        ):
            present_sides.update(
                path.split(".", 2)[1]
                for path in values
                if path.startswith("text.") and path.count(".") >= 2
            )
        paths.update(
            f"text.{side}.{field}"
            for side in present_sides
            for field in translatable_labels
            if not visible_fields or field in visible_fields
        )
        path_order = {
            **{
                f"metadata.{field}": index
                for index, field in enumerate(metadata_labels)
            },
            **{
                f"text.{side}.{field}": 1000 + side_index * 100 + field_index
                for side_index, side in enumerate(("front", "back"))
                for field_index, field in enumerate(translatable_labels)
            },
        }
        fields: list[dict[str, Any]] = []
        for path in sorted(paths, key=lambda item: (path_order.get(item, 9000), item)):
            published_source = published_source_values.get(path, missing)
            local_source = local_source_values.get(path, missing)
            published_target = published_translation_values.get(path, missing)
            local_target = local_translation_values.get(path, missing)
            def comparable(value: Any) -> Any:
                if value is missing or value is None or value == "" or value == [] or value == {}:
                    return None
                return value

            source_changed = comparable(published_source) != comparable(local_source)
            translation_changed = comparable(published_target) != comparable(local_target)
            group, _, remainder = path.partition(".")
            _, _, translatable_field = remainder.partition(".")
            metadata_field = remainder.rsplit(".", 1)[-1] if group == "metadata" else ""
            raw_values = (
                published_source,
                local_source,
                published_target,
                local_target,
            )
            has_value = any(
                value is not missing and value not in (None, "", [], {})
                for value in raw_values
            )
            schema_relevant = (
                group == "metadata" and metadata_field in visible_fields
            ) or (
                group == "text" and translatable_field in visible_fields
            )
            if group == "metadata" and "." in remainder:
                # Side-specific metadata is useful only when that side actually
                # carries a value (or a published value is being removed). Empty
                # defaults from Shoggoth must not grow the review table.
                if not has_value:
                    continue
                front_path = f"metadata.{metadata_field}"
                if all(
                    comparable(value) == comparable(front_values.get(front_path, missing))
                    for value, front_values in (
                        (published_source, published_source_values),
                        (local_source, local_source_values),
                        (published_target, published_translation_values),
                        (local_target, local_translation_values),
                    )
                ):
                    continue
            if not source_changed and not translation_changed and not (
                schema_relevant or has_value
            ):
                continue
            fields.append({
                "path": path,
                "label": _diff_field_label(
                    path, metadata_labels, translatable_labels
                ),
                "published_source": _diff_value(
                    None if published_source is missing else published_source
                ),
                "local_source": _diff_value(
                    None if local_source is missing else local_source
                ),
                "published_translation": _diff_value(
                    None if published_target is missing else published_target
                ),
                "local_translation": _diff_value(
                    None if local_target is missing else local_target
                ),
                "source_changed": source_changed,
                "translation_changed": translation_changed,
                "source_editable": (
                    metadata_field in metadata_types
                    or translatable_field in translatable_types
                ),
                "translation_editable": translatable_field in translatable_types,
                "editor_multiline": (
                    translatable_types.get(translatable_field) == "multiline"
                ),
                "local_source_edit_value": _diff_value(
                    None if local_source is missing else local_source
                ),
                "local_translation_edit_value": _diff_value(
                    None if local_target is missing else local_target
                ),
            })

        title_card = local_base_card or published_base_card
        title_values = _canonical_card_values(
            title_card, language=source_language
        )
        display_name = str(
            title_values.get("text.front.title")
            or _canonical_card_values(
                local_translation_card or published_translation_card,
                language=target_language,
            ).get("text.front.title")
            or key
        )
        result.append({
            "key": key,
            "editor_anchor": f"card-{card_positions.get(key, 1)}",
            "display_name": display_name,
            "change_type": (
                "added" if published_base_card is None and local_base_card is not None
                else "deleted" if local_base_card is None and published_base_card is not None
                else "modified"
            ),
            "base_changed": key in status["base_changed_cards"],
            "translation_changed": key in status["translation_changed_cards"],
            "images": _card_image_urls(local_base_card, scenario),
            "fields": fields,
        })
    return result


def contribution_preview(
    scenario: dict[str, Any], repository_root: Path
) -> dict[str, Any]:
    """Build an exact, read-only preview of the files a publish would replace."""
    source = Path(scenario["path"])
    destination = _published_scenario_path(scenario, repository_root)
    scopes = [
        (source / "project.json", destination / "project.json"),
        (source / "source", destination / "source"),
        (source / "base", destination / "base"),
    ]
    if not scenario.get("source_only"):
        language = str(scenario["storage_language"])
        scopes.append((
            source / "translations" / language,
            destination / "translations" / language,
        ))
    if scenario.get("campaign"):
        campaign_source = source.parent.parent
        campaign_destination = destination.parent.parent
        scopes.append((
            campaign_source / CAMPAIGN_FILENAME,
            campaign_destination / CAMPAIGN_FILENAME,
        ))
        scopes.append((
            campaign_source / "documents" / "source",
            campaign_destination / "documents" / "source",
        ))
        if not scenario.get("source_only"):
            language = str(scenario["storage_language"])
            scopes.append((
                campaign_source / "documents" / "translations" / language,
                campaign_destination / "documents" / "translations" / language,
            ))

    files = sorted(
        (
            change
            for local_scope, published_scope in scopes
            for change in _scope_file_changes(
                local_scope,
                published_scope,
                repository_root=repository_root,
            )
        ),
        key=lambda change: (change["status"], change["path"]),
    )
    status = publication_status(scenario, repository_root)
    cards = _card_content_diffs(scenario, repository_root, status)
    return {
        **status,
        "files": [
            {
                **item,
                "kind": (
                    "shoggoth"
                    if item["path"].endswith("/builds/shoggoth.json")
                    else "document" if item["path"].casefold().endswith(".pdf")
                    else "project"
                ),
            }
            for item in files
        ],
        "file_count": len(files),
        "added_count": sum(item["status"] == "added" for item in files),
        "modified_count": sum(item["status"] == "modified" for item in files),
        "deleted_count": sum(item["status"] == "deleted" for item in files),
        "cards": cards,
    }


def _replace_from(source: Path, destination: Path) -> None:
    if not source.exists():
        return
    if destination.is_dir():
        shutil.rmtree(destination)
    elif destination.exists():
        destination.unlink()
    if source.is_dir():
        shutil.copytree(
            source,
            destination,
            ignore=shutil.ignore_patterns("logs", "snapshots", "__pycache__", ".DS_Store"),
        )
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def _publish_shared_project(
    root: Path,
    repository_root: Path,
    *,
    game: str,
    slug: str,
    language: str | None,
) -> dict[str, Any]:
    """Copy the shared base and optionally one translation into tracked projects/."""
    scenario = get_scenario(root, game, slug, language=language or "source")
    if scenario is None or not scenario.get("initialized"):
        raise ValueError("Scenario is not initialized")
    if not scenario.get("shared_layout"):
        raise ValueError("Migrate this project to the shared layout before publishing")
    if scenario.get("private_only"):
        raise ValueError("Private-only scenarios cannot be published")
    source = Path(scenario["path"])
    destination = _published_scenario_path(scenario, repository_root)
    source_campaign: Path | None = None
    target_campaign: Path | None = None
    if scenario.get("campaign"):
        source_campaign = source.parent.parent / CAMPAIGN_FILENAME
        target_campaign = destination.parent.parent / CAMPAIGN_FILENAME
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source_campaign and target_campaign:
        _replace_from(source_campaign, target_campaign)
        source_campaign_root = source_campaign.parent
        target_campaign_root = target_campaign.parent
        _replace_from(
            source_campaign_root / "documents" / "source",
            target_campaign_root / "documents" / "source",
        )
        if language:
            _replace_from(
                source_campaign_root / "documents" / "translations" / language,
                target_campaign_root / "documents" / "translations" / language,
            )
    staging = destination.parent / f".{slug}.publishing-{uuid4().hex}"
    backup = destination.parent / f".{slug}.previous-{uuid4().hex}"
    try:
        if destination.is_dir():
            shutil.copytree(destination, staging)
        else:
            staging.mkdir()
        _replace_from(source / "project.json", staging / "project.json")
        _replace_from(source / "source", staging / "source")
        _replace_from(source / "base", staging / "base")
        if language:
            _replace_from(
                source / "translations" / language,
                staging / "translations" / language,
            )
        (staging / ".card-translator-remote.json").unlink(missing_ok=True)
        if destination.exists():
            os.replace(destination, backup)
        os.replace(staging, destination)
        if backup.exists():
            shutil.rmtree(backup)
    except Exception:
        if not destination.exists() and backup.exists():
            os.replace(backup, destination)
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {
        "destination": destination,
        "game": game,
        "campaign": scenario.get("campaign"),
        "slug": slug,
        "language": language,
        "contribution_type": "translation" if language else "base",
    }


def publish_extraction(
    root: Path,
    repository_root: Path,
    *,
    game: str,
    slug: str,
) -> dict[str, Any]:
    """Copy a source workspace into tracked projects/ without adding a translation."""
    return _publish_shared_project(
        root, repository_root, game=game, slug=slug, language=None
    )


def publish_translation(
    root: Path,
    repository_root: Path,
    *,
    game: str,
    slug: str,
    language: str,
) -> dict[str, Any]:
    """Copy one working translation plus its shared base into tracked projects/."""
    return _publish_shared_project(
        root, repository_root, game=game, slug=slug, language=language
    )
