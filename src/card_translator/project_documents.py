from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

MAX_DOCUMENT_SIZE = 50 * 1024 * 1024
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")


def _document_name(filename: str) -> str:
    name = Path(filename.replace("\\", "/")).name.strip()
    name = _CONTROL_CHARACTERS.sub("", name)
    if not name or name in {".", ".."} or not name.casefold().endswith(".pdf"):
        raise ValueError("Documents must be PDF files")
    return name[:240]


def _scope_root(scenario: dict[str, Any], level: str) -> Path:
    scenario_root = Path(scenario["path"])
    if level == "scenario":
        return scenario_root
    if level == "campaign" and scenario.get("campaign"):
        return scenario_root.parent.parent
    raise ValueError("Unknown document scope")


def _document_directory(
    scenario: dict[str, Any], *, level: str, language: str | None
) -> Path:
    root = _scope_root(scenario, level)
    if level == "scenario":
        return (
            root / "source" / "documents"
            if language is None
            else root / "translations" / language / "documents"
        )
    return (
        root / "documents" / "source"
        if language is None
        else root / "documents" / "translations" / language
    )


def _pdf_files(path: Path) -> dict[str, Path]:
    if not path.is_dir():
        return {}
    return {
        item.name: item
        for item in sorted(path.iterdir(), key=lambda entry: entry.name.casefold())
        if item.is_file() and item.suffix.casefold() == ".pdf"
    }


def _document_url(root: Path, path: Path) -> str:
    return f"/library/{quote(path.relative_to(root).as_posix(), safe='/')}"


def document_overview(root: Path, scenario: dict[str, Any]) -> dict[str, Any]:
    language = (
        None if scenario.get("source_only") else str(scenario["storage_language"])
    )
    result: dict[str, Any] = {}
    for level in ("campaign", "scenario"):
        if level == "campaign" and not scenario.get("campaign"):
            continue
        source_files = _pdf_files(
            _document_directory(scenario, level=level, language=None)
        )
        translated_files = (
            _pdf_files(_document_directory(scenario, level=level, language=language))
            if language
            else {}
        )
        names = sorted(
            source_files.keys() | translated_files.keys(), key=str.casefold
        )
        entries = [
            {
                "name": name,
                "source_exists": name in source_files,
                "source_url": (
                    _document_url(root, source_files[name])
                    if name in source_files
                    else ""
                ),
                "translation_exists": name in translated_files,
                "translation_url": (
                    _document_url(root, translated_files[name])
                    if name in translated_files
                    else ""
                ),
            }
            for name in names
        ]
        result[level] = {
            "entries": entries,
            "source_count": len(source_files),
            "translation_count": len(translated_files),
            "missing_count": sum(
                entry["source_exists"] and not entry["translation_exists"]
                for entry in entries
            ) if language else 0,
        }
    return result


def missing_document_translations(scenario: dict[str, Any]) -> list[dict[str, str]]:
    if scenario.get("source_only"):
        return []
    language = str(scenario["storage_language"])
    missing: list[dict[str, str]] = []
    for level in ("campaign", "scenario"):
        if level == "campaign" and not scenario.get("campaign"):
            continue
        source_files = _pdf_files(
            _document_directory(scenario, level=level, language=None)
        )
        translated_files = _pdf_files(
            _document_directory(scenario, level=level, language=language)
        )
        missing.extend(
            {"level": level, "name": name}
            for name in sorted(source_files, key=str.casefold)
            if name not in translated_files
        )
    return missing


def save_pdf_document(
    scenario: dict[str, Any],
    *,
    level: str,
    filename: str,
    content: bytes,
    source_name: str = "",
) -> dict[str, Any]:
    if len(content) > MAX_DOCUMENT_SIZE:
        raise ValueError("PDF document exceeds the 50 MB limit")
    if not content.startswith(b"%PDF-"):
        raise ValueError("The uploaded file is not a PDF document")
    language = (
        None if scenario.get("source_only") else str(scenario["storage_language"])
    )
    if language:
        name = _document_name(source_name)
        source = _document_directory(scenario, level=level, language=None) / name
        if not source.is_file():
            raise ValueError("The source document does not exist")
    else:
        name = _document_name(filename)
    destination = _document_directory(scenario, level=level, language=language) / name
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(content)
    return {"path": destination, "name": name, "level": level, "language": language}


def delete_pdf_document(
    scenario: dict[str, Any], *, level: str, name: str
) -> dict[str, Any]:
    name = _document_name(name)
    language = (
        None if scenario.get("source_only") else str(scenario["storage_language"])
    )
    deleted: list[Path] = []
    if language:
        candidates = [
            _document_directory(scenario, level=level, language=language) / name
        ]
    else:
        scope_root = _scope_root(scenario, level)
        candidates = [_document_directory(scenario, level=level, language=None) / name]
        translation_root = (
            scope_root / "translations"
            if level == "scenario"
            else scope_root / "documents" / "translations"
        )
        if translation_root.is_dir():
            candidates.extend(
                path / "documents" / name if level == "scenario" else path / name
                for path in translation_root.iterdir()
                if path.is_dir()
            )
    for candidate in candidates:
        if candidate.is_file():
            candidate.unlink()
            deleted.append(candidate)
    if not deleted:
        raise ValueError("Document does not exist")
    return {"deleted": deleted, "name": name, "level": level, "language": language}
