from __future__ import annotations

import os
import re
from pathlib import Path

ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
PROJECTS_REPOSITORY_URL = ""
PROJECTS_REPOSITORY_BRANCH = "main"
PROJECTS_CATALOG_URL = ""


def _env(name: str, default: str = "", *, legacy: str | None = None) -> str:
    """Read a renamed setting while accepting the pre-rename variable."""
    if name in os.environ:
        return os.environ[name]
    if legacy and legacy in os.environ:
        return os.environ[legacy]
    return default


def load_env_file(path: Path | None = None, *, override: bool = False) -> Path:
    """Load the project's small local .env file without overriding shell values."""
    path = path or project_root() / ".env"
    if not path.is_file():
        return path
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        name, value = (part.strip() for part in line.split("=", 1))
        if not ENV_NAME.fullmatch(name):
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if override or name not in os.environ:
            os.environ[name] = value
    return path


def project_root() -> Path:
    configured = _env(
        "UNSPEAKABLE_TRANSLATIONS_ROOT", legacy="CARD_TRANSLATOR_ROOT"
    )
    if configured:
        return Path(configured).expanduser().resolve()
    current = Path.cwd().resolve()
    for candidate in (current, *current.parents):
        pyproject = candidate / "pyproject.toml"
        if not pyproject.is_file():
            continue
        try:
            content = pyproject.read_text(encoding="utf-8")
        except OSError:
            continue
        if re.search(
            r'^name\s*=\s*["\'](?:unspeakable-translations|card-translator)["\']\s*$',
            content,
            re.MULTILINE,
        ):
            return candidate
    return current


load_env_file()


def library_root() -> Path:
    configured = _env(
        "UNSPEAKABLE_TRANSLATIONS_LIBRARY", legacy="CARD_TRANSLATOR_LIBRARY"
    )
    if configured:
        return Path(configured).expanduser().resolve()
    return project_root() / "library"


def projects_repository_url() -> str:
    return _env(
        "UNSPEAKABLE_TRANSLATIONS_PROJECTS_REPOSITORY",
        PROJECTS_REPOSITORY_URL,
        legacy="CARD_TRANSLATOR_PROJECTS_REPOSITORY",
    ).strip()


def projects_repository_branch() -> str:
    return _env(
        "UNSPEAKABLE_TRANSLATIONS_PROJECTS_BRANCH",
        PROJECTS_REPOSITORY_BRANCH,
        legacy="CARD_TRANSLATOR_PROJECTS_BRANCH",
    ).strip() or PROJECTS_REPOSITORY_BRANCH


def projects_catalog_url() -> str:
    return _env(
        "UNSPEAKABLE_TRANSLATIONS_PROJECTS_CATALOG_URL",
        PROJECTS_CATALOG_URL,
        legacy="CARD_TRANSLATOR_PROJECTS_CATALOG_URL",
    ).strip()


def arkhamdb_scenario_import_enabled() -> bool:
    """Return whether the private official-scenario import route is enabled."""
    return _env(
        "UNSPEAKABLE_TRANSLATIONS_ENABLE_ARKHAMDB_SCENARIO_IMPORT",
        "false",
        legacy="CARD_TRANSLATOR_ENABLE_ARKHAMDB_SCENARIO_IMPORT",
    ).strip().casefold() in {"1", "true", "yes", "on"}


def ollama_api_url() -> str:
    base = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").strip()
    if not base.startswith(("http://", "https://")):
        base = f"http://{base}"
    return base.rstrip("/")


def ollama_enabled() -> bool:
    value = saved_local_setting(
        "UNSPEAKABLE_TRANSLATIONS_ENABLE_OLLAMA",
        legacy="CARD_TRANSLATOR_ENABLE_OLLAMA",
    )
    if value is None:
        value = _env(
            "UNSPEAKABLE_TRANSLATIONS_ENABLE_OLLAMA",
            "true",
            legacy="CARD_TRANSLATOR_ENABLE_OLLAMA",
        )
    return value.strip().casefold() not in {
        "0", "false", "no", "off",
    }


def saved_local_setting(name: str, *, legacy: str | None = None) -> str | None:
    """Read a UI setting from .env so a stale parent-process value cannot override it."""
    path = project_root() / ".env"
    if not path.is_file():
        return None
    lines = path.read_text(encoding="utf-8").splitlines()
    for target in (name, legacy):
        if target is None:
            continue
        for raw_line in reversed(lines):
            line = raw_line.strip()
            if line.startswith("export "):
                line = line[7:].lstrip()
            if "=" not in line:
                continue
            key, value = (part.strip() for part in line.split("=", 1))
            if key == target:
                if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
                    return value[1:-1]
                return value
    return None


CARD_IMAGE_PROVIDERS = ("gemini", "openai", "anthropic")


def card_image_provider() -> str:
    value = saved_local_setting(
        "UNSPEAKABLE_TRANSLATIONS_CARD_IMAGE_PROVIDER",
        legacy="CARD_TRANSLATOR_CARD_IMAGE_PROVIDER",
    )
    if value is None:
        value = _env(
            "UNSPEAKABLE_TRANSLATIONS_CARD_IMAGE_PROVIDER",
            "none",
            legacy="CARD_TRANSLATOR_CARD_IMAGE_PROVIDER",
        )
    provider = value.strip().casefold()
    return provider if provider in ("none", *CARD_IMAGE_PROVIDERS) else "none"


def update_local_env(values: dict[str, str]) -> Path:
    """Update selected values in the ignored project-local .env file."""
    path = project_root() / ".env"
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    seen: set[str] = set()
    updated: list[str] = []
    for line in lines:
        stripped = line.strip()
        name = stripped.split("=", 1)[0].removeprefix("export ").strip()
        if name in values and "=" in stripped:
            if name not in seen:
                updated.append(f"{name}={values[name]}")
                seen.add(name)
        else:
            updated.append(line)
    pending = {name: value for name, value in values.items() if name not in seen}
    if pending and updated and updated[-1]:
        updated.append("")
    updated.extend(f"{name}={value}" for name, value in pending.items())
    path.write_text("\n".join(updated).rstrip() + "\n", encoding="utf-8")
    for name, value in values.items():
        os.environ[name] = value
    return path


def translategemma_model() -> str:
    return _env(
        "UNSPEAKABLE_TRANSLATIONS_TRANSLATEGEMMA_MODEL",
        "translategemma:12b-it-q8_0",
        legacy="CARD_TRANSLATOR_TRANSLATEGEMMA_MODEL",
    ).strip()


def translategemma_context_length() -> int:
    value = _env(
        "UNSPEAKABLE_TRANSLATIONS_TRANSLATEGEMMA_CONTEXT_LENGTH",
        "4096",
        legacy="CARD_TRANSLATOR_TRANSLATEGEMMA_CONTEXT_LENGTH",
    )
    try:
        return max(2048, min(int(value), 32768))
    except ValueError:
        return 4096


def deepl_auth_key() -> str:
    return os.environ.get("DEEPL_AUTH_KEY", "").strip()


def deepl_api_url() -> str:
    base = os.environ.get("DEEPL_API_URL", "https://api-free.deepl.com").strip()
    return f"{base.rstrip('/')}/v2/translate"


def google_translation_api_key() -> str:
    return os.environ.get("GOOGLE_CLOUD_TRANSLATION_API_KEY", "").strip()


def google_translation_api_url() -> str:
    return os.environ.get(
        "GOOGLE_CLOUD_TRANSLATION_API_URL",
        "https://translation.googleapis.com/language/translate/v2",
    ).strip()


def gemini_api_key() -> str:
    return os.environ.get("GEMINI_API_KEY", "").strip()


def gemini_model() -> str:
    return _env(
        "UNSPEAKABLE_TRANSLATIONS_GEMINI_MODEL",
        "gemini-3.8-flash",
        legacy="CARD_TRANSLATOR_GEMINI_MODEL",
    ).strip()


def gemini_api_base_url() -> str:
    return _env(
        "UNSPEAKABLE_TRANSLATIONS_GEMINI_API_URL",
        "https://generativelanguage.googleapis.com/v1beta",
        legacy="CARD_TRANSLATOR_GEMINI_API_URL",
    ).strip().rstrip("/")


def openai_api_key() -> str:
    return os.environ.get("OPENAI_API_KEY", "").strip()


def openai_model() -> str:
    return _env(
        "UNSPEAKABLE_TRANSLATIONS_OPENAI_MODEL",
        "gpt-5.4-mini",
        legacy="CARD_TRANSLATOR_OPENAI_MODEL",
    ).strip()


def openai_api_base_url() -> str:
    return _env(
        "UNSPEAKABLE_TRANSLATIONS_OPENAI_API_URL",
        "https://api.openai.com/v1",
        legacy="CARD_TRANSLATOR_OPENAI_API_URL",
    ).strip().rstrip("/")


def anthropic_api_key() -> str:
    return os.environ.get("ANTHROPIC_API_KEY", "").strip()


def anthropic_model() -> str:
    return _env(
        "UNSPEAKABLE_TRANSLATIONS_ANTHROPIC_MODEL",
        "claude-sonnet-4-6",
        legacy="CARD_TRANSLATOR_ANTHROPIC_MODEL",
    ).strip()


def anthropic_api_base_url() -> str:
    return _env(
        "UNSPEAKABLE_TRANSLATIONS_ANTHROPIC_API_URL",
        "https://api.anthropic.com/v1",
        legacy="CARD_TRANSLATOR_ANTHROPIC_API_URL",
    ).strip().rstrip("/")
