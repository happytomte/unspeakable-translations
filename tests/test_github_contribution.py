from __future__ import annotations

import subprocess

import pytest
from starlette.requests import Request

from card_translator.github_contribution import (
    create_pull_request,
    pull_request_status,
)
from card_translator.library import get_scenario
from card_translator.project_documents import save_pdf_document
from card_translator.project_publication import (
    create_project_translation,
    create_shared_project,
    publish_translation,
    update_scenario_private_only,
)
from card_translator.web.app import create_app


def git(path, *args):
    return subprocess.run(
        ["git", *args], cwd=path, check=True, capture_output=True, text=True
    ).stdout.strip()


def prepared_contribution(tmp_path):
    library = tmp_path / "library"
    repository = tmp_path / "repository"
    created = create_shared_project(
        library,
        game="ah",
        slug="demo-scenario",
        title="Demo Scenario",
        author="",
        source_url="",
        source_language="en",
    )
    create_project_translation(
        library, game="ah", slug="demo-scenario", target_language="de"
    )
    git(repository.parent, "init", "-q", str(repository))
    git(repository, "config", "user.email", "test@example.test")
    git(repository, "config", "user.name", "Test User")
    (repository / "README.md").write_text("repository\n", encoding="utf-8")
    git(repository, "add", ".")
    git(repository, "commit", "-qm", "initial")
    git(repository, "branch", "-M", "main")
    publish_translation(
        library,
        repository,
        game="ah",
        slug="demo-scenario",
        language="de",
    )
    scenario = get_scenario(library, "ah", "demo-scenario", language="de")
    return library, repository, created, scenario


def test_pull_request_status_limits_submission_to_one_scenario(tmp_path):
    _, repository, _, scenario = prepared_contribution(tmp_path)

    status = pull_request_status(
        scenario,
        repository,
        upstream_url="https://github.com/example/translations.git",
        base_branch="main",
    )

    assert status["can_prepare"] is True
    assert status["can_submit"] is True
    assert status["upstream_repository"] == "example/translations"
    assert status["branch_name"] == "contribution/ah-demo-scenario-de"
    assert status["repository_changed_files"]
    assert status["translation_changed_files"]
    assert status["extraction_changed_files"]
    assert status["translation_changed_files"].isdisjoint(
        status["extraction_changed_files"]
    )
    assert status["repository_changed_files"] == (
        status["translation_changed_files"] | status["extraction_changed_files"]
    )
    assert all(
        "/translations/de/" in f"/{path}"
        for path in status["translation_changed_files"]
    )
    assert not any(
        "/translations/de/" in f"/{path}"
        for path in status["extraction_changed_files"]
    )
    assert not status["unrelated_changes"]

    (repository / "README.md").write_text("unrelated\n", encoding="utf-8")
    with_unrelated_changes = pull_request_status(
        scenario,
        repository,
        upstream_url="https://github.com/example/translations.git",
        base_branch="main",
    )
    assert with_unrelated_changes["can_prepare"] is True
    assert with_unrelated_changes["unrelated_changes"] == ["README.md"]

    git(repository, "add", "README.md")
    with_staged_change = pull_request_status(
        scenario,
        repository,
        upstream_url="https://github.com/example/translations.git",
        base_branch="main",
    )
    assert with_staged_change["can_prepare"] is False
    assert with_staged_change["staged_changes"] == ["README.md"]


def test_private_only_cannot_be_submitted_even_with_ready_files(tmp_path):
    library, repository, _, _ = prepared_contribution(tmp_path)
    update_scenario_private_only(
        library, game="ah", slug="demo-scenario", private_only=True
    )
    scenario = get_scenario(library, "ah", "demo-scenario", language="de")

    status = pull_request_status(
        scenario,
        repository,
        upstream_url="https://github.com/example/translations.git",
        base_branch="main",
    )
    assert status["can_prepare"] is False
    with pytest.raises(ValueError, match="Private-only"):
        create_pull_request(
            scenario,
            repository,
            upstream_url="https://github.com/example/translations.git",
            base_branch="main",
            method="manual",
            fork_url="https://github.com/user/translations.git",
        )


def test_missing_pdf_translation_warns_but_keeps_pull_request_available(tmp_path):
    library, repository, _, scenario = prepared_contribution(tmp_path)
    source = get_scenario(library, "ah", "demo-scenario", language="source")
    save_pdf_document(
        source,
        level="scenario",
        filename="Guide.pdf",
        content=b"%PDF-1.4\n%%EOF\n",
    )
    scenario = get_scenario(library, "ah", "demo-scenario", language="de")

    status = pull_request_status(
        scenario,
        repository,
        upstream_url="https://github.com/example/translations.git",
        base_branch="main",
    )

    assert status["repository_changed_files"]
    assert status["missing_document_count"] == 1
    assert status["can_prepare"] is True


def test_manual_submission_commits_only_scope_and_pushes_branch(tmp_path, monkeypatch):
    _, repository, _, scenario = prepared_contribution(tmp_path)
    (repository / "README.md").write_text("unrelated local edit\n", encoding="utf-8")
    fork = tmp_path / "fork.git"
    git(tmp_path, "init", "--bare", "-q", str(fork))

    def repository_identity(value):
        if value == str(fork):
            return "translator", "translations"
        return "community", "translations"

    monkeypatch.setattr(
        "card_translator.github_contribution._github_repository",
        repository_identity,
    )
    result = create_pull_request(
        scenario,
        repository,
        upstream_url="https://github.com/community/translations.git",
        base_branch="main",
        method="manual",
        fork_url=str(fork),
    )

    branch = "contribution/ah-demo-scenario-de"
    assert result == {
        "created": False,
        "url": (
            "https://github.com/community/translations/compare/"
            "main...translator:contribution/ah-demo-scenario-de?expand=1"
        ),
        "branch": branch,
    }
    assert git(repository, "branch", "--show-current") == branch
    remaining_paths = {
        line.split(maxsplit=1)[-1]
        for line in git(repository, "status", "--porcelain").splitlines()
    }
    assert "README.md" in remaining_paths
    assert "projects/ah/demo-scenario/base/" in remaining_paths
    assert "projects/ah/demo-scenario/project.json" in remaining_paths
    committed_paths = set(
        git(repository, "diff", "--name-only", f"main..{branch}").splitlines()
    )
    assert committed_paths
    assert "README.md" not in committed_paths
    assert all(
        path.startswith("projects/ah/demo-scenario/translations/de/")
        for path in committed_paths
    )
    assert subprocess.run(
        ["git", "--git-dir", str(fork), "show-ref", "--verify", f"refs/heads/{branch}"],
        check=False,
        capture_output=True,
        text=True,
    ).returncode == 0


def test_manual_submission_can_create_separate_extraction_branch(tmp_path, monkeypatch):
    _, repository, _, scenario = prepared_contribution(tmp_path)
    fork = tmp_path / "fork.git"
    git(tmp_path, "init", "--bare", "-q", str(fork))

    monkeypatch.setattr(
        "card_translator.github_contribution._github_repository",
        lambda value: (
            ("translator", "translations")
            if value == str(fork)
            else ("community", "translations")
        ),
    )
    result = create_pull_request(
        scenario,
        repository,
        upstream_url="https://github.com/community/translations.git",
        base_branch="main",
        method="manual",
        fork_url=str(fork),
        contribution_scope="extraction",
    )

    branch = "contribution/ah-demo-scenario-source"
    assert result["branch"] == branch
    committed_paths = set(
        git(repository, "diff", "--name-only", f"main..{branch}").splitlines()
    )
    assert committed_paths
    assert not any("/translations/" in f"/{path}" for path in committed_paths)
    assert any("/base/" in f"/{path}" for path in committed_paths)


def test_translation_submission_includes_extractions_when_selected(
    tmp_path, monkeypatch
):
    _, repository, _, scenario = prepared_contribution(tmp_path)
    fork = tmp_path / "fork.git"
    git(tmp_path, "init", "--bare", "-q", str(fork))
    monkeypatch.setattr(
        "card_translator.github_contribution._github_repository",
        lambda value: (
            ("translator", "translations")
            if value == str(fork)
            else ("community", "translations")
        ),
    )

    result = create_pull_request(
        scenario,
        repository,
        upstream_url="https://github.com/community/translations.git",
        base_branch="main",
        method="manual",
        fork_url=str(fork),
        contribution_scope="translation",
        include_extractions=True,
    )

    committed_paths = set(
        git(
            repository,
            "diff",
            "--name-only",
            f"main..{result['branch']}",
        ).splitlines()
    )
    assert any("/translations/de/" in f"/{path}" for path in committed_paths)
    assert any("/base/" in f"/{path}" for path in committed_paths)


def test_pull_request_page_shows_scope_and_both_submission_methods(tmp_path, monkeypatch):
    library, repository, _, _ = prepared_contribution(tmp_path)
    (repository / "README.md").write_text("unrelated local edit\n", encoding="utf-8")
    monkeypatch.setenv("CARD_TRANSLATOR_ROOT", str(repository))
    monkeypatch.setenv("CARD_TRANSLATOR_LIBRARY", str(library))
    monkeypatch.setenv(
        "CARD_TRANSLATOR_PROJECTS_REPOSITORY",
        "https://github.com/community/translations.git",
    )
    monkeypatch.setenv("CARD_TRANSLATOR_PROJECTS_BRANCH", "main")
    app = create_app()
    request = Request({
        "type": "http",
        "method": "GET",
        "path": "/ah/de/demo-scenario/pull-request",
        "root_path": "",
        "scheme": "http",
        "server": ("test", 80),
        "headers": [(b"cookie", b"card_translator_ui_language=de")],
        "query_string": b"",
        "router": app.router,
    })
    page = next(route.endpoint for route in app.routes if route.name == "pull_request_page")

    body = page(
        request,
        game="ah",
        project_language="de",
        slug="demo-scenario",
    ).body.decode()

    assert "Übersetzung und Kartendaten getrennt einreichen" in body
    assert "Translations" in body
    assert "Card Data Extractions" in body
    assert 'name="contribution_scope" value="translation"' in body
    assert 'name="contribution_scope" value="extraction"' in body
    assert 'name="include_extractions" value="true" checked' in body
    assert "Weitere lokale Änderungen gefunden" not in body
    assert "Automatisch mit GitHub CLI" in body
    assert "Vorhandenen Fork verwenden" in body
    assert 'name="fork_url"' in body
    assert 'value="https://github.com/community/translations.git"' in body
    assert "projects/ah/demo-scenario/translations/de/translation.json" in body
