from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import card_translator.remote_projects as remote_projects
from card_translator.remote_projects import (
    REMOTE_MARKER,
    download_project,
    github_pages_catalog_url,
    project_remote_status,
    refresh_repository,
    update_project,
)


def test_git_commands_never_request_credentials(monkeypatch):
    captured = {}

    def run(command, **kwargs):
        captured.update(command=command, **kwargs)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(remote_projects.subprocess, "run", run)

    remote_projects._git("fetch", "origin", "main")

    assert captured["command"][:3] == ["git", "-c", "credential.helper="]
    assert captured["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert captured["env"]["GCM_INTERACTIVE"] == "Never"


def git(repository: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repository, check=True, capture_output=True, text=True,
    )
    return result.stdout.strip()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def make_remote(repository: Path) -> None:
    project = repository / "projects" / "ah" / "demo"
    write_json(project / "project.json", {"title": "Demo"})
    write_json(project / "base" / "extraction.json", {"cards": []})
    (project / "source" / "cards").mkdir(parents=True)
    (project / "source" / "cards" / "001.png").write_bytes(b"image")
    write_json(project / "translations" / "de" / "project.json", {"target_language": "de"})
    write_json(project / "translations" / "de" / "translation.json", {"cards": {}})
    write_json(project / "translations" / "fr" / "project.json", {"target_language": "fr"})
    write_json(project / "translations" / "fr" / "translation.json", {"cards": {}})
    write_json(repository / "site" / "data" / "projects.json", {
        "format": "card-translator-status",
        "format_version": 1,
        "projects": [{
            "id": "ah/demo",
            "game": "ah",
            "slug": "demo",
            "title": "Demo",
            "base": {"extraction_percent": 100},
            "translations": {
                "de": {"translation_percent": 50},
                "fr": {"translation_percent": 25},
            },
        }],
    })
    git(repository, "init")
    git(repository, "config", "user.email", "test@example.com")
    git(repository, "config", "user.name", "Test")
    git(repository, "add", ".")
    git(repository, "commit", "-m", "initial")
    git(repository, "branch", "-M", "main")


def test_sparse_repository_download_and_change_detection(tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    make_remote(origin)
    library = tmp_path / "library"

    remote = refresh_repository(library, url=str(origin), branch="main")

    assert remote["available"] is True
    assert [project["id"] for project in remote["projects"]] == ["ah/demo"]
    result = download_project(
        library, repository=remote, game="ah", slug="demo", languages=["de"],
    )
    local = library / "projects" / "ah" / "demo"
    assert result["downloaded"] is True
    assert (local / "source" / "cards" / "001.png").read_bytes() == b"image"
    assert (local / "translations" / "de" / "translation.json").is_file()
    assert not (local / "translations" / "fr").exists()
    assert (local / REMOTE_MARKER).is_file()

    translation = origin / "projects" / "ah" / "demo" / "translations" / "de" / "translation.json"
    write_json(translation, {"cards": {"001": {"title": "Neu"}}})
    git(origin, "add", ".")
    git(origin, "commit", "-m", "translation update")
    refreshed = refresh_repository(library, url=str(origin), branch="main")
    status = project_remote_status(local, refreshed)
    assert status is not None
    assert status["update_available"] is True
    assert status["remote_changed"] == ["translations/de/translation.json"]
    assert status["conflicts"] == []

    write_json(local / "translations" / "de" / "translation.json", {"cards": {"local": {}}})
    status = project_remote_status(local, refreshed)
    assert status is not None
    assert status["conflicts"] == ["translations/de/translation.json"]

    with pytest.raises(ValueError, match="conflicts with local changes"):
        update_project(local, refreshed)

    write_json(local / "translations" / "de" / "translation.json", {"cards": {}})
    result = update_project(local, refreshed)
    assert result == {"updated": True, "files": ["translations/de/translation.json"]}
    assert json.loads(translation.read_text(encoding="utf-8")) == json.loads(
        (local / "translations" / "de" / "translation.json").read_text(encoding="utf-8")
    )
    assert project_remote_status(local, refreshed)["update_available"] is False


def test_download_does_not_overwrite_unlinked_local_project(tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    make_remote(origin)
    library = tmp_path / "library"
    local = library / "projects" / "ah" / "demo"
    local.mkdir(parents=True)
    remote = refresh_repository(library, url=str(origin), branch="main")

    with pytest.raises(ValueError, match="not linked"):
        download_project(
            library, repository=remote, game="ah", slug="demo", languages=["de"],
        )


def test_github_pages_catalog_url_is_derived_from_repository_url():
    assert github_pages_catalog_url("https://github.com/example/cards.git") == (
        "https://example.github.io/cards/data/projects.json"
    )
    assert github_pages_catalog_url("https://github.com/example/example.github.io") == (
        "https://example.github.io/data/projects.json"
    )
    assert github_pages_catalog_url("git@github.com:example/cards.git") == ""
