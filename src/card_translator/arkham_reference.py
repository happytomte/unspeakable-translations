from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx

WORD = re.compile(r"[a-z][a-z'-]{2,}", re.IGNORECASE)
ARKHAM_FILE = re.compile(r"^arkham\.(?P<language>[a-z][a-z0-9_-]*)\.json$", re.IGNORECASE)
FIELD_KEYS = {
    "title": ("name", "back_name"),
    "traits": ("traits", "back_traits"),
    "rules": ("text", "back_text"),
    "flavor": ("flavor", "back_flavor"),
}
INDEX_FILENAME = "arkham.references.json"


@dataclass(frozen=True)
class ArkhamReference:
    source: str
    target: str
    card_name: str
    card_type: str
    field: str


def _read_records(path: Path) -> dict[str, dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(value, list):
        return {}
    return {
        str(item["code"]): item
        for item in value
        if isinstance(item, dict) and item.get("code")
    }


def arkham_data_files(data_directory: Path) -> dict[str, Path]:
    result: dict[str, Path] = {}
    if not data_directory.is_dir():
        return result
    for path in data_directory.iterdir():
        if path.name == INDEX_FILENAME:
            continue
        match = ARKHAM_FILE.fullmatch(path.name)
        if match and path.is_file():
            result[match.group("language").casefold().replace("_", "-")] = path
    return result


def download_arkhamdb_cards(
    data_directory: Path, *, language: str, url: str
) -> dict[str, Any]:
    language = language.casefold().replace("_", "-")
    if not url.startswith("https://"):
        raise ValueError("ArkhamDB downloads require an HTTPS URL")
    try:
        with httpx.Client(follow_redirects=True, timeout=120.0) as client:
            response = client.get(url, headers={"Accept": "application/json"})
        response.raise_for_status()
        cards = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise ValueError(f"ArkhamDB download failed: {exc}") from exc
    if not isinstance(cards, list) or not all(isinstance(card, dict) for card in cards):
        raise ValueError("ArkhamDB returned an unexpected response")
    data_directory.mkdir(parents=True, exist_ok=True)
    destination = data_directory / f"arkham.{language}.json"
    destination.write_text(
        json.dumps(cards, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return {"path": destination, "card_count": len(cards), "language": language}


def build_arkham_reference_index(
    data_directory: Path,
    *,
    source_language: str = "en",
) -> dict[str, Any]:
    """Build a compact multilingual index from arkham.XX.json files."""
    source_language = source_language.casefold().replace("_", "-")
    files = arkham_data_files(data_directory)
    source_path = files.get(source_language)
    if source_path is None:
        raise ValueError(f"Missing {data_directory / f'arkham.{source_language}.json'}")
    source_records = _read_records(source_path)
    if not source_records:
        raise ValueError(f"No ArkhamDB cards found in {source_path.name}")

    languages: dict[str, list[dict[str, str]]] = {}
    for language, target_path in sorted(files.items()):
        if language == source_language:
            continue
        references: list[dict[str, str]] = []
        for code, target in _read_records(target_path).items():
            source = source_records.get(code)
            if source is None:
                continue
            for field, keys in FIELD_KEYS.items():
                for key in keys:
                    source_text = str(source.get(key) or "").strip()
                    target_text = str(target.get(key) or "").strip()
                    if not source_text or not target_text:
                        continue
                    if source_text.casefold() == target_text.casefold():
                        continue
                    references.append(
                        asdict(
                            ArkhamReference(
                                source=source_text,
                                target=target_text,
                                card_name=str(source.get("name") or code),
                                card_type=str(
                                    source.get("type_name") or source.get("type_code") or ""
                                ),
                                field=field,
                            )
                        )
                    )
        languages[language] = references

    index = {
        "format": "card-translator-arkham-references",
        "format_version": 1,
        "built_at": datetime.now(UTC).isoformat(),
        "source_language": source_language,
        "source_files": {
            language: {"name": path.name, "mtime_ns": path.stat().st_mtime_ns}
            for language, path in sorted(files.items())
        },
        "languages": languages,
    }
    data_directory.mkdir(parents=True, exist_ok=True)
    destination = data_directory / INDEX_FILENAME
    destination.write_text(
        json.dumps(index, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    _load_index.cache_clear()
    return {
        "path": destination,
        "source_language": source_language,
        "languages": sorted(languages),
        "reference_counts": {
            language: len(references) for language, references in languages.items()
        },
    }


def _read_index(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(value, dict) or value.get("format") != "card-translator-arkham-references":
        return {}
    return value


def arkham_reference_status(
    data_directory: Path,
    *,
    source_language: str = "en",
    requested_languages: set[str] | None = None,
) -> dict[str, Any]:
    source_language = source_language.casefold().replace("_", "-")
    files = arkham_data_files(data_directory)
    target_languages = sorted(language for language in files if language != source_language)
    index = _read_index(data_directory / INDEX_FILENAME)
    indexed_languages = sorted((index.get("languages") or {}).keys()) if index else []
    indexed_source_language = str(index.get("source_language") or "") if index else ""
    requested = {
        language.casefold().replace("_", "-")
        for language in (requested_languages or set())
        if language and language.casefold().replace("_", "-") != source_language
    }
    indexed_files = index.get("source_files") if index else {}
    stale = bool(index) and (
        not isinstance(indexed_files, dict)
        or set(indexed_files) != set(files)
        or any(
            not isinstance(indexed_files.get(language), dict)
            or indexed_files[language].get("mtime_ns") != path.stat().st_mtime_ns
            for language, path in files.items()
        )
    )
    return {
        "source_exists": source_language in files,
        "available_languages": target_languages,
        "indexed_languages": indexed_languages,
        "indexed_source_language": indexed_source_language,
        "source_indexed": bool(index) and indexed_source_language == source_language,
        "missing_index_languages": sorted(requested - set(indexed_languages)),
        "missing_languages": sorted(requested - set(target_languages)),
        "index_exists": bool(index),
        "stale": stale,
    }


@lru_cache(maxsize=16)
def _load_index(path_name: str, mtime_ns: int) -> dict[str, Any]:
    del mtime_ns
    return _read_index(Path(path_name))


def _tokens(value: str) -> set[str]:
    return {word.casefold() for word in WORD.findall(value)}


def find_arkham_references(
    index_path: Path,
    *,
    text: str,
    field: str,
    target_language: str,
    card_type: str = "",
    limit: int = 3,
) -> list[ArkhamReference]:
    """Return the closest official translation pairs from the compact index."""
    if field not in FIELD_KEYS or not index_path.is_file() or limit < 1:
        return []
    index = _load_index(str(index_path.resolve()), index_path.stat().st_mtime_ns)
    language = target_language.casefold().replace("_", "-")
    raw_references = (index.get("languages") or {}).get(language, [])
    query = _tokens(text)
    requested_type = card_type.casefold().strip()
    source_labels = {
        label.casefold() for label in re.findall(r"<b>([^<]+)</b>", text, re.IGNORECASE)
    }

    scored: list[tuple[float, ArkhamReference]] = []
    for raw in raw_references:
        if not isinstance(raw, dict) or raw.get("field") != field:
            continue
        try:
            reference = ArkhamReference(**raw)
        except TypeError:
            continue
        candidate = _tokens(reference.source)
        overlap = len(query & candidate)
        if not overlap:
            continue
        score = overlap / math.sqrt(max(1, len(query) * len(candidate)))
        if reference.source.casefold().strip() == text.casefold().strip():
            score += 1.0
        if requested_type and reference.card_type.casefold() == requested_type:
            score += 0.35
        candidate_labels = {
            label.casefold()
            for label in re.findall(r"<b>([^<]+)</b>", reference.source, re.IGNORECASE)
        }
        if source_labels & candidate_labels:
            score += 0.5
        scored.append((score, reference))

    scored.sort(key=lambda item: (-item[0], len(item[1].source), item[1].card_name))
    return [reference for _, reference in scored[:limit]]
