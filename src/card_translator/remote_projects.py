"""Git-backed remote project catalog and sparse downloads."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from hashlib import sha256
from pathlib import Path
from typing import Any

import httpx

from card_translator.shared_projects import read_json, write_json

REMOTE_MARKER = ".card-translator-remote.json"


def _git(*args: str, cwd: Path | None = None, timeout: int = 90) -> str:
    try:
        result = subprocess.run(
            ["git", "-c", "credential.helper=", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={
                **os.environ,
                "GIT_TERMINAL_PROMPT": "0",
                "GCM_INTERACTIVE": "Never",
            },
        )
    except FileNotFoundError as exc:
        raise RuntimeError("Git is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Git operation timed out") from exc
    except subprocess.CalledProcessError as exc:
        message = (exc.stderr or exc.stdout or "Git operation failed").strip()
        raise RuntimeError(message.splitlines()[-1]) from exc
    return result.stdout.strip()


def _cache_path(root: Path, url: str, branch: str) -> Path:
    identifier = sha256(f"{url}\0{branch}".encode()).hexdigest()[:16]
    return root / ".remote" / "repositories" / identifier


def _safe_component(value: str, label: str) -> str:
    if not value or value in {".", ".."} or "/" in value or "\\" in value:
        raise ValueError(f"Invalid {label}")
    return value


def _head(checkout: Path, branch: str) -> str:
    return _git("rev-parse", f"origin/{branch}", cwd=checkout)


def github_pages_catalog_url(repository_url: str) -> str:
    match = re.fullmatch(
        r"https://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?", repository_url,
    )
    if not match:
        return ""
    owner, repository = match.groups()
    prefix = "" if repository.casefold() == f"{owner}.github.io".casefold() else f"/{repository}"
    return f"https://{owner}.github.io{prefix}/data/projects.json"


def _catalog(checkout: Path, catalog_url: str) -> dict[str, Any]:
    if catalog_url:
        try:
            response = httpx.get(catalog_url, timeout=30, follow_redirects=True)
            response.raise_for_status()
            value = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise RuntimeError(f"Could not load project catalog: {exc}") from exc
        if not isinstance(value, dict):
            raise RuntimeError("Project catalog is not a JSON object")
        return value
    return read_json(checkout / "site/data/projects.json") or {}


def refresh_repository(
    root: Path, *, url: str, branch: str, catalog_url: str = "",
) -> dict[str, Any]:
    if not url.strip():
        return {"configured": False, "available": False, "projects": []}
    if url.startswith("-") or any(character in url for character in "\r\n\0"):
        return {
            "configured": True,
            "available": False,
            "url": url,
            "branch": branch,
            "projects": [],
            "error": "Invalid repository URL",
        }
    checkout = _cache_path(root, url, branch)
    try:
        if not (checkout / ".git").is_dir():
            checkout.parent.mkdir(parents=True, exist_ok=True)
            if checkout.exists():
                shutil.rmtree(checkout)
            _git(
                "clone", "--filter=blob:none", "--no-checkout", "--branch", branch,
                "--single-branch", url, str(checkout), timeout=180,
            )
            _git("sparse-checkout", "init", "--cone", cwd=checkout)
            _git("sparse-checkout", "set", "site/data", cwd=checkout)
        else:
            _git("fetch", "--prune", "origin", branch, cwd=checkout, timeout=120)
        _git("checkout", "--detach", f"origin/{branch}", cwd=checkout)
        resolved_catalog_url = catalog_url or github_pages_catalog_url(url)
        catalog = _catalog(checkout, resolved_catalog_url)
        projects = catalog.get("projects") if isinstance(catalog.get("projects"), list) else []
        return {
            "configured": True,
            "available": True,
            "url": url,
            "branch": branch,
            "catalog_url": resolved_catalog_url,
            "checkout": checkout,
            "revision": _head(checkout, branch),
            "projects": [item for item in projects if isinstance(item, dict)],
        }
    except (OSError, RuntimeError) as exc:
        return {
            "configured": True,
            "available": False,
            "url": url,
            "branch": branch,
            "catalog_url": catalog_url or github_pages_catalog_url(url),
            "checkout": checkout,
            "projects": [],
            "error": str(exc),
        }


def _files(path: Path) -> list[Path]:
    entries = sorted(path.rglob("*"))
    if path.is_symlink() or any(item.is_symlink() for item in entries):
        raise ValueError("Remote projects may not contain symbolic links")
    return [item for item in entries if item.is_file()]


def _digest(path: Path) -> str:
    checksum = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def _copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Keep working files independent from the Git cache. A later checkout may
    # replace cache contents and must never mutate a user's local edits.
    shutil.copy2(source, destination)


def _copy_paths(source_project: Path, destination: Path, relatives: list[Path]) -> dict[str, str]:
    planned: list[tuple[Path, Path]] = []
    for relative in relatives:
        source = source_project / relative
        if not source.exists():
            raise ValueError(f"Remote project is missing {relative.as_posix()}")
        paths = _files(source) if source.is_dir() else [source]
        if source.is_symlink():
            raise ValueError("Remote projects may not contain symbolic links")
        planned.extend((path, path.relative_to(source_project)) for path in paths)
    existing = [relative for _, relative in planned if (destination / relative).exists()]
    if existing:
        raise ValueError(f"Local file already exists: {existing[0].as_posix()}")

    manifest: dict[str, str] = {}
    for path, file_relative in planned:
        target = destination / file_relative
        _copy_file(path, target)
        manifest[file_relative.as_posix()] = _digest(target)
    return manifest


def download_project(
    root: Path,
    *,
    repository: dict[str, Any],
    game: str,
    slug: str,
    languages: list[str],
    campaign: str | None = None,
) -> dict[str, Any]:
    if not repository.get("available"):
        raise ValueError("Remote repository is not available")
    game = _safe_component(game, "game")
    slug = _safe_component(slug, "project")
    campaign = _safe_component(campaign, "campaign") if campaign else None
    languages = sorted({_safe_component(language, "language") for language in languages})
    if not languages:
        raise ValueError("Choose at least one language")
    project_id = f"{game}/{campaign}/{slug}" if campaign else f"{game}/{slug}"
    project = next(
        (item for item in repository["projects"] if item.get("id") == project_id), None,
    )
    if project is None:
        raise ValueError("Unknown remote project")
    available_languages = set((project.get("translations") or {}).keys())
    if not set(languages) <= available_languages:
        raise ValueError("The selected language is not available remotely")

    checkout = Path(repository["checkout"])
    remote_path = (
        Path("projects") / game / campaign / "scenarios" / slug
        if campaign else Path("projects") / game / slug
    )
    _git("sparse-checkout", "add", remote_path.as_posix(), cwd=checkout)
    _git("checkout", "--detach", f"origin/{repository['branch']}", cwd=checkout)
    source_project = checkout / remote_path
    destination = (
        root / "projects" / game / campaign / "scenarios" / slug
        if campaign else root / "projects" / game / slug
    )
    marker_path = destination / REMOTE_MARKER
    marker = read_json(marker_path)
    if destination.exists() and marker is None:
        raise ValueError("A local project with this ID already exists and is not linked to the remote repository")
    marker = marker or {
        "format": "card-translator-remote",
        "format_version": 1,
        "repository": repository["url"],
        "branch": repository["branch"],
        "manifest": {},
        "languages": [],
        "remote_path": remote_path.as_posix(),
    }
    if marker.get("repository") != repository["url"] or marker.get("branch") != repository["branch"]:
        raise ValueError("The local project belongs to a different remote repository")

    relatives: list[Path] = []
    campaign_source: Path | None = None
    campaign_target: Path | None = None
    if not destination.exists():
        relatives.extend([Path("project.json"), Path("base"), Path("source")])
        if campaign:
            campaign_source = checkout / "projects" / game / campaign / "campaign.json"
            campaign_target = root / "projects" / game / campaign / "campaign.json"
            if not campaign_source.is_file():
                raise ValueError("Remote campaign is missing campaign.json")
            if campaign_source.is_file() and not campaign_target.exists():
                _copy_file(campaign_source, campaign_target)
    installed = set(marker.get("languages") or [])
    for language in languages:
        if language not in installed:
            relatives.append(Path("translations") / language)
    if not relatives:
        return {"path": destination, "languages": sorted(installed), "downloaded": False}
    copied = _copy_paths(source_project, destination, relatives)
    marker["manifest"].update(copied)
    marker["languages"] = sorted(installed | set(languages))
    marker["base_revision"] = repository["revision"]
    if campaign_source and campaign_target:
        marker["campaign_digest"] = _digest(campaign_source)
    write_json(marker_path, marker)
    return {"path": destination, "languages": marker["languages"], "downloaded": True}


def project_remote_status(project_path: Path, repository: dict[str, Any]) -> dict[str, Any] | None:
    marker = read_json(project_path / REMOTE_MARKER)
    if marker is None or not repository.get("available"):
        return None
    checkout = Path(repository["checkout"])
    base_revision = str(marker.get("base_revision") or "")
    current_revision = str(repository.get("revision") or "")
    remote_relative = Path(str(marker.get("remote_path") or ""))
    if not remote_relative.parts:
        remote_relative = Path("projects") / project_path.parent.name / project_path.name
    try:
        _git("sparse-checkout", "add", remote_relative.as_posix(), cwd=checkout)
        _git("checkout", "--detach", f"origin/{repository['branch']}", cwd=checkout)
    except RuntimeError as exc:
        return {
            "base_revision": base_revision,
            "remote_revision": current_revision,
            "remote_changed": [],
            "local_changed": [],
            "conflicts": [],
            "update_available": False,
            "error": str(exc),
        }
    remote_project = checkout / remote_relative
    manifest = marker.get("manifest") or {}
    tracked_remote_files: set[str] = set()
    scopes = [Path("project.json"), Path("base"), Path("source")]
    scopes.extend(Path("translations") / language for language in marker.get("languages") or [])
    try:
        for scope in scopes:
            path = remote_project / scope
            if path.is_file():
                tracked_remote_files.add(scope.as_posix())
            elif path.is_dir():
                tracked_remote_files.update(
                    item.relative_to(remote_project).as_posix() for item in _files(path)
                )
    except ValueError as exc:
        return {
            "base_revision": base_revision,
            "remote_revision": current_revision,
            "remote_changed": [],
            "local_changed": [],
            "conflicts": [],
            "update_available": False,
            "error": str(exc),
        }
    remote_changed = sorted(
        relative for relative in set(manifest) | tracked_remote_files
        if relative not in manifest
        or not (remote_project / relative).is_file()
        or _digest(remote_project / relative) != manifest[relative]
    )
    local_changed = [
        relative for relative, digest in manifest.items()
        if not (project_path / relative).is_file() or _digest(project_path / relative) != digest
    ]
    local_changed.extend(
        relative for relative in remote_changed
        if relative not in manifest and (project_path / relative).exists()
    )
    campaign_digest = str(marker.get("campaign_digest") or "")
    if campaign_digest and remote_relative.parent.name == "scenarios":
        remote_campaign = remote_project.parent.parent / "campaign.json"
        local_campaign = project_path.parent.parent / "campaign.json"
        remote_campaign_changed = (
            not remote_campaign.is_file() or _digest(remote_campaign) != campaign_digest
        )
        local_campaign_changed = (
            not local_campaign.is_file() or _digest(local_campaign) != campaign_digest
        )
        if remote_campaign_changed:
            remote_changed.append("campaign.json")
        if local_campaign_changed:
            local_changed.append("campaign.json")
    conflicts = sorted(set(remote_changed) & set(local_changed))
    return {
        "base_revision": base_revision,
        "remote_revision": current_revision,
        "remote_changed": sorted(remote_changed),
        "local_changed": sorted(local_changed),
        "conflicts": conflicts,
        "update_available": bool(remote_changed),
    }


def update_project(project_path: Path, repository: dict[str, Any]) -> dict[str, Any]:
    _safe_component(project_path.name, "project")
    marker_path = project_path / REMOTE_MARKER
    marker = read_json(marker_path)
    if marker is None:
        raise ValueError("The local project is not linked to a remote repository")
    status = project_remote_status(project_path, repository)
    if status is None:
        raise ValueError("Remote repository is not available")
    if status.get("error"):
        raise ValueError(str(status["error"]))
    if status["conflicts"]:
        raise ValueError(
            "Remote update conflicts with local changes: " + ", ".join(status["conflicts"])
        )
    if not status["update_available"]:
        return {"updated": False, "files": []}

    remote_relative = Path(str(marker.get("remote_path") or ""))
    if not remote_relative.parts:
        if project_path.parent.parent.name != "projects":
            raise ValueError("Invalid local project path")
        remote_relative = Path("projects") / project_path.parent.name / project_path.name
    remote_project = Path(repository["checkout"]) / remote_relative
    manifest = marker.get("manifest") or {}
    changed = status["remote_changed"]
    with tempfile.TemporaryDirectory(
        prefix="unspeakable-translations-update-", dir=project_path.parent
    ) as tmp:
        staging = Path(tmp)
        staged: dict[str, str] = {}
        for relative in changed:
            source = (
                remote_project.parent.parent / "campaign.json"
                if relative == "campaign.json" and marker.get("campaign_digest")
                else remote_project / relative
            )
            if relative == "campaign.json" and not source.is_file():
                raise ValueError("Remote campaign is missing campaign.json")
            if source.is_file():
                if source.is_symlink():
                    raise ValueError("Remote projects may not contain symbolic links")
                target = staging / relative
                _copy_file(source, target)
                staged[relative] = _digest(target)
        for relative in changed:
            target = (
                project_path.parent.parent / "campaign.json"
                if relative == "campaign.json" and marker.get("campaign_digest")
                else project_path / relative
            )
            staged_file = staging / relative
            if staged_file.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                staged_file.replace(target)
                if relative != "campaign.json":
                    manifest[relative] = staged[relative]
            elif target.is_file():
                target.unlink()
                if relative != "campaign.json":
                    manifest.pop(relative, None)
    marker["manifest"] = manifest
    if "campaign.json" in changed:
        campaign_file = project_path.parent.parent / "campaign.json"
        marker["campaign_digest"] = _digest(campaign_file) if campaign_file.is_file() else ""
    marker["base_revision"] = repository["revision"]
    write_json(marker_path, marker)
    return {"updated": True, "files": changed}
