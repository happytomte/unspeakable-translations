"""Build deterministic release archives for reviewed translations."""

from __future__ import annotations

import json
import re
import zipfile
from hashlib import sha256
from pathlib import Path
from typing import Any

from card_translator.shared_projects import (
    campaign_for_scenario,
    read_json,
    shared_scenario_directories,
    write_json,
)


def _archive_files(project: Path, language: str) -> list[Path]:
    language_dir = project / "translations" / language
    candidates = [
        project / "project.json",
        language_dir / "project.json",
        language_dir / "translation.json",
    ]
    campaign, campaign_dir = campaign_for_scenario(project)
    if campaign and campaign_dir:
        candidates.append(campaign_dir / "campaign.json")
    for directory_name in ("cards", "builds", "renders"):
        directory = language_dir / directory_name
        if directory.is_dir():
            candidates.extend(
                path for path in directory.rglob("*")
                if path.is_file() and not path.is_symlink()
            )
    return sorted({path for path in candidates if path.is_file()})


def _write_zip(path: Path, project: Path, files: list[Path], metadata: dict[str, Any]) -> None:
    rendered_metadata = json.dumps(metadata, ensure_ascii=False, indent=2).encode() + b"\n"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for source in files:
            relative = (
                "campaign.json"
                if source.name == "campaign.json" and project not in source.parents
                else source.relative_to(project).as_posix()
            )
            info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, source.read_bytes())
        info = zipfile.ZipInfo("release.json", date_time=(1980, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o100644 << 16
        archive.writestr(info, rendered_metadata)


PROJECT_ID = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)?$"
)
LANGUAGE_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
VERSION = re.compile(r"^[0-9]+\.[0-9]+(?:\.[0-9]+)?(?:[-+][A-Za-z0-9.-]+)?$")


def build_release_bundles(
    root: Path,
    output: Path,
    *,
    projects: set[str] | None = None,
    languages: set[str] | None = None,
    version: str = "",
) -> dict[str, Any]:
    projects = {value.strip() for value in projects or set() if value.strip()}
    languages = {value.strip().lower() for value in languages or set() if value.strip()}
    if any(not PROJECT_ID.fullmatch(value) for value in projects):
        raise ValueError("Project filters must use <game>/<scenario> or <game>/<campaign>/<scenario>")
    if any(not LANGUAGE_ID.fullmatch(value) for value in languages):
        raise ValueError("Invalid language filter")
    if version and not VERSION.fullmatch(version):
        raise ValueError("Version must look like 1.0.0")
    output.mkdir(parents=True, exist_ok=True)
    for previous in output.glob("*.zip"):
        previous.unlink()
    artifacts: list[dict[str, Any]] = []
    projects_root = root / "projects"
    if projects_root.is_dir():
        for project in shared_scenario_directories(root):
            campaign, campaign_dir = campaign_for_scenario(project)
            game = campaign_dir.parent.name if campaign_dir else project.parent.name
            slug = project.name
            project_id = f"{game}/{campaign}/{slug}" if campaign else f"{game}/{slug}"
            if projects and project_id not in projects:
                continue
            translations = project / "translations"
            if not translations.is_dir():
                continue
            for language_dir in sorted(path for path in translations.iterdir() if path.is_dir()):
                language = language_dir.name
                if languages and language.lower() not in languages:
                    continue
                settings = read_json(language_dir / "project.json") or {}
                if settings.get("final_reviewed") is not True:
                    continue
                files = _archive_files(project, language)
                if not files:
                    continue
                filename = (
                    f"{game}-{campaign}-{slug}-{language}.zip"
                    if campaign else f"{game}-{slug}-{language}.zip"
                )
                archive_path = output / filename
                metadata = {
                    "format": "card-translator-release",
                    "format_version": 1,
                    "game": game,
                    "campaign": campaign,
                    "slug": slug,
                    "language": language,
                    "title": (read_json(project / "project.json") or {}).get("title", slug),
                }
                _write_zip(archive_path, project, files, metadata)
                artifacts.append({
                    **metadata,
                    "filename": filename,
                    "size": archive_path.stat().st_size,
                    "sha256": sha256(archive_path.read_bytes()).hexdigest(),
                })
    manifest = {
        "format": "card-translator-release-manifest",
        "format_version": 1,
        "artifacts": artifacts,
        "selection": {
            "projects": sorted(projects),
            "languages": sorted(languages),
        },
    }
    if version:
        manifest["version"] = version
    if version and len(projects) == 1:
        manifest["release_tag"] = f"{next(iter(projects)).replace('/', '-')}-v{version}"
    write_json(output / "release-manifest.json", manifest)
    return manifest
