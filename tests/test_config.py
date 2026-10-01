import os

from card_translator.config import (
    arkhamdb_scenario_import_enabled,
    card_image_provider,
    load_env_file,
    ollama_enabled,
    project_root,
    projects_catalog_url,
    projects_repository_branch,
    projects_repository_url,
    update_local_env,
)


def test_load_env_file_does_not_override_shell_values(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("EXISTING=saved\nNEW_VALUE='from file'\n", encoding="utf-8")
    monkeypatch.setenv("EXISTING", "from shell")
    monkeypatch.delenv("NEW_VALUE", raising=False)

    load_env_file(path)

    assert os.environ["EXISTING"] == "from shell"
    assert os.environ["NEW_VALUE"] == "from file"


def test_default_env_path_follows_configured_project_root(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("UNSPEAKABLE_TRANSLATIONS_TEST_VALUE=from-root\n", encoding="utf-8")
    monkeypatch.setenv("UNSPEAKABLE_TRANSLATIONS_ROOT", str(tmp_path))
    monkeypatch.delenv("UNSPEAKABLE_TRANSLATIONS_TEST_VALUE", raising=False)
    assert load_env_file() == path
    assert os.environ["UNSPEAKABLE_TRANSLATIONS_TEST_VALUE"] == "from-root"


def test_project_root_finds_repository_when_started_from_a_subdirectory(
    tmp_path, monkeypatch
):
    repository = tmp_path / "unspeakable-translations"
    working_directory = repository / "projects"
    working_directory.mkdir(parents=True)
    (repository / "pyproject.toml").write_text(
        '[project]\nname = "unspeakable-translations"\n', encoding="utf-8"
    )
    monkeypatch.delenv("UNSPEAKABLE_TRANSLATIONS_ROOT", raising=False)
    monkeypatch.chdir(working_directory)

    assert project_root() == repository


def test_update_local_env_preserves_secrets_and_changes_toggle(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text(
        "GEMINI_API_KEY=secret\nUNSPEAKABLE_TRANSLATIONS_ENABLE_OLLAMA=true\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("UNSPEAKABLE_TRANSLATIONS_ROOT", str(tmp_path))

    result = update_local_env({"UNSPEAKABLE_TRANSLATIONS_ENABLE_OLLAMA": "false"})

    assert result == path
    assert "GEMINI_API_KEY=secret" in path.read_text(encoding="utf-8")
    assert "UNSPEAKABLE_TRANSLATIONS_ENABLE_OLLAMA=false" in path.read_text(encoding="utf-8")


def test_saved_provider_settings_win_over_stale_process_values(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(
        "UNSPEAKABLE_TRANSLATIONS_ENABLE_OLLAMA=false\n"
        "UNSPEAKABLE_TRANSLATIONS_CARD_IMAGE_PROVIDER=anthropic\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("UNSPEAKABLE_TRANSLATIONS_ROOT", str(tmp_path))
    monkeypatch.setenv("UNSPEAKABLE_TRANSLATIONS_ENABLE_OLLAMA", "true")
    monkeypatch.setenv("UNSPEAKABLE_TRANSLATIONS_CARD_IMAGE_PROVIDER", "gemini")
    assert ollama_enabled() is False
    assert card_image_provider() == "anthropic"

    update_local_env({
        "UNSPEAKABLE_TRANSLATIONS_ENABLE_OLLAMA": "true",
        "UNSPEAKABLE_TRANSLATIONS_CARD_IMAGE_PROVIDER": "openai",
    })
    assert ollama_enabled() is True
    assert card_image_provider() == "openai"


def test_card_image_provider_defaults_to_none(tmp_path, monkeypatch):
    monkeypatch.setenv("UNSPEAKABLE_TRANSLATIONS_ROOT", str(tmp_path))
    monkeypatch.delenv("UNSPEAKABLE_TRANSLATIONS_CARD_IMAGE_PROVIDER", raising=False)
    monkeypatch.delenv("CARD_TRANSLATOR_CARD_IMAGE_PROVIDER", raising=False)

    assert card_image_provider() == "none"


def test_project_repository_settings_are_optional_and_configurable(monkeypatch):
    monkeypatch.delenv("UNSPEAKABLE_TRANSLATIONS_PROJECTS_REPOSITORY", raising=False)
    monkeypatch.delenv("UNSPEAKABLE_TRANSLATIONS_PROJECTS_BRANCH", raising=False)
    monkeypatch.delenv("UNSPEAKABLE_TRANSLATIONS_PROJECTS_CATALOG_URL", raising=False)

    assert projects_repository_url() == ""
    assert projects_repository_branch() == "main"
    assert projects_catalog_url() == ""

    monkeypatch.setenv(
        "UNSPEAKABLE_TRANSLATIONS_PROJECTS_REPOSITORY",
        "https://github.com/example/cards",
    )
    monkeypatch.setenv("UNSPEAKABLE_TRANSLATIONS_PROJECTS_BRANCH", "published")
    monkeypatch.setenv(
        "UNSPEAKABLE_TRANSLATIONS_PROJECTS_CATALOG_URL", "https://example.test/catalog"
    )

    assert projects_repository_url() == "https://github.com/example/cards"
    assert projects_repository_branch() == "published"
    assert projects_catalog_url() == "https://example.test/catalog"


def test_arkhamdb_scenario_import_is_an_opt_in_startup_flag(monkeypatch):
    monkeypatch.delenv(
        "UNSPEAKABLE_TRANSLATIONS_ENABLE_ARKHAMDB_SCENARIO_IMPORT", raising=False
    )
    assert arkhamdb_scenario_import_enabled() is False

    monkeypatch.setenv("UNSPEAKABLE_TRANSLATIONS_ENABLE_ARKHAMDB_SCENARIO_IMPORT", "true")
    assert arkhamdb_scenario_import_enabled() is True
