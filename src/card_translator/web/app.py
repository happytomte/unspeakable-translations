from __future__ import annotations

import json
import subprocess
from math import ceil
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from card_translator.arkham_reference import (
    arkham_reference_status,
    build_arkham_reference_index,
    download_arkhamdb_cards,
)
from card_translator.config import (
    CARD_IMAGE_PROVIDERS,
    arkhamdb_scenario_import_enabled,
    card_image_provider,
    library_root,
    project_root,
    projects_catalog_url,
    projects_repository_branch,
    projects_repository_url,
    update_local_env,
)
from card_translator.data_import import (
    apply_filename_metadata,
    import_arkhamdb_scenario_data,
    import_card_data,
    import_shoggoth_data,
    latest_import_status,
    match_import_record,
    preview_card_data,
    preview_filename_metadata,
    set_card_identifier,
    unmatched_import_records,
)
from card_translator.games import (
    delete_json_import_source,
    discover_games,
    game_definition_issues,
    save_filename_parser,
    save_game,
    save_json_import_source,
    save_translatable_fields,
)
from card_translator.github_contribution import (
    create_pull_request,
    pull_request_status,
)
from card_translator.hall_import import import_hall_of_beorn_scenario
from card_translator.library import (
    apply_card_image_batch_results,
    apply_card_ocr_text,
    apply_card_source_proposal,
    apply_external_card_translation,
    apply_gemini_card_image,
    card_source_proposal,
    collapse_duplicate_cards,
    copy_scenario_to_language,
    delete_scenario,
    discover_library,
    format_arkham_rules_result,
    game_global_translation_rules,
    gemini_card_image_prompt,
    get_scenario,
    initialize_scenario,
    normalize_language_code,
    prepare_card_image_batch,
    prepare_external_card_translation,
    preview_card_image,
    save_separate_card_art,
    stage_card_source_proposal,
    translate_missing_card_fields,
    translation_rules,
    update_card_game_fields,
    update_card_source_text,
    update_card_translation,
    update_game_system_config,
    update_game_translation_rules,
    update_image_metadata,
    update_separate_card_art_setting,
    update_shoggoth_settings,
    update_translation_completion,
    update_translation_rules,
)
from card_translator.ocr import dependencies_available, extract_region
from card_translator.project_documents import (
    MAX_DOCUMENT_SIZE,
    delete_pdf_document,
    document_overview,
    save_pdf_document,
)
from card_translator.project_publication import (
    campaign_summaries,
    contribution_preview,
    create_campaign,
    create_campaign_scenario,
    create_project_translation,
    create_shared_project,
    publication_status,
    publish_extraction,
    publish_translation,
    shared_project_summaries,
    update_campaign_private_only,
    update_scenario_private_only,
)
from card_translator.providers.card_image import provider_available, provider_model
from card_translator.providers.gemini import available as gemini_available
from card_translator.providers.google_translate import available as google_translate_available
from card_translator.providers.hall_of_beorn import HallOfBeornProvider
from card_translator.remote_projects import (
    REMOTE_MARKER,
    download_project,
    project_remote_status,
    refresh_repository,
    update_project,
)
from card_translator.shoggoth_export import export_shoggoth
from card_translator.translation import (
    TranslationFailed,
    TranslationReviewRequired,
    TranslationUnavailable,
    ollama_status,
)
from card_translator.web.i18n import (
    TARGET_LANGUAGES,
    UI_LANGUAGES,
    normalize_ui_language,
    translator,
)

HERE = Path(__file__).parent
ASSET_DIR = HERE.parents[2] / "assets"
TEMPLATES = Jinja2Templates(directory=str(HERE / "templates"))
UI_GAME_IDS = {"ah"}


def _ui_context(request: Request) -> dict[str, object]:
    language = normalize_ui_language(
        request.cookies.get("unspeakable_translations_ui_language")
        or request.cookies.get("card_translator_ui_language")
    )
    return {
        "ui_language": language,
        "ui_languages": UI_LANGUAGES,
        "target_languages": TARGET_LANGUAGES,
        "t": translator(language),
    }


def _safe_next(value: str) -> str:
    return value if value.startswith("/") and not value.startswith("//") else "/"


def _settings_redirect(
    *, error: str = "", saved: str = "", game: str = "", language: str = ""
) -> RedirectResponse:
    query = {
        key: value
        for key, value in {
            "error": error,
            "saved": saved,
            "game": game,
            "language": language,
        }.items()
        if value
    }
    url = f"/settings?{urlencode(query)}" if query else "/settings"
    return RedirectResponse(url=url, status_code=303)


def create_app() -> FastAPI:
    app = FastAPI(title="Unspeakable Translations", version="0.1.0")
    lib_root = library_root()
    lib_root.mkdir(parents=True, exist_ok=True)
    remote_repository = refresh_repository(
        lib_root,
        url=projects_repository_url(),
        branch=projects_repository_branch(),
        catalog_url=projects_catalog_url(),
    )

    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
    if ASSET_DIR.is_dir():
        app.mount("/assets", StaticFiles(directory=str(ASSET_DIR)), name="assets")
    app.mount("/library", StaticFiles(directory=str(lib_root)), name="library")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> FileResponse:
        return FileResponse(ASSET_DIR / "app" / "favicon.svg", media_type="image/svg+xml")

    @app.post("/ui-language")
    def set_ui_language(language: str = Form(...), next: str = Form("/")) -> RedirectResponse:
        language = normalize_ui_language(language)
        response = RedirectResponse(url=_safe_next(next), status_code=303)
        response.set_cookie(
            "unspeakable_translations_ui_language",
            language,
            max_age=60 * 60 * 24 * 365,
            samesite="lax",
        )
        return response

    @app.get("/", response_class=HTMLResponse)
    def dashboard(
        request: Request,
        error: str = "",
        updated: str = "",
        created: str = "",
        campaign_created: str = "",
    ) -> HTMLResponse:
        library_games = [
            game for game in discover_library(lib_root)
            if game["slug"] in UI_GAME_IDS
        ]
        shared_projects = [
            project for project in shared_project_summaries(lib_root)
            if project["game"] in UI_GAME_IDS
        ]
        campaigns = [
            campaign for campaign in campaign_summaries(lib_root)
            if campaign["game"] in UI_GAME_IDS
        ]
        for game_entry in library_games:
            for scenario in game_entry["scenarios"]:
                scenario["publication"] = publication_status(
                    scenario, project_root()
                )
                scenario["documents"] = document_overview(lib_root, scenario)
                scenario["workbench"] = {
                    "missing_cards": max(
                        0,
                        int(scenario.get("translation_total") or 0)
                        - int(scenario.get("translated") or 0),
                    ),
                    "missing_documents": sum(
                        int(scope.get("missing_count") or 0)
                        for scope in scenario["documents"].values()
                    ),
                }
        local_projects = {
            (scenario["game"], scenario.get("campaign"), scenario["slug"]): scenario
            for game in library_games for scenario in game["scenarios"]
        }
        for project in shared_projects:
            local_projects.setdefault(
                (project["game"], project.get("campaign"), project["slug"]), project
            )
        remote_projects = []
        for item in remote_repository.get("projects", []):
            project = dict(item)
            key = (
                str(project.get("game") or ""),
                str(project.get("campaign") or "") or None,
                str(project.get("slug") or ""),
            )
            if key[0] not in UI_GAME_IDS:
                continue
            local = local_projects.get(key)
            project_path = (
                lib_root / "projects" / key[0] / key[1] / "scenarios" / key[2]
                if key[1] else lib_root / "projects" / key[0] / key[2]
            )
            project["local"] = local is not None
            project["linked"] = (project_path / REMOTE_MARKER).is_file()
            project["installed_languages"] = sorted({
                scenario["storage_language"]
                for game in library_games for scenario in game["scenarios"]
                if (scenario["game"], scenario.get("campaign"), scenario["slug"]) == key
                and not scenario.get("source_only")
            })
            project["remote_status"] = project_remote_status(project_path, remote_repository)
            remote_projects.append(project)
        mirrored_projects = [
            project for project in remote_projects
            if project["local"] and project["linked"]
        ]
        mirrored_keys = {
            (project["game"], project.get("campaign"), project["slug"])
            for project in mirrored_projects
        }
        project_shells = [
            project for project in shared_projects
            if not any(
                scenario["game"] == project["game"]
                and scenario.get("campaign") == project.get("campaign")
                and scenario["slug"] == project["slug"]
                for game in library_games for scenario in game["scenarios"]
            )
            and (project["game"], project.get("campaign"), project["slug"]) not in mirrored_keys
        ]
        games = [
            {
                **game,
                "scenarios": [
                    scenario for scenario in game["scenarios"]
                    if (scenario["game"], scenario.get("campaign"), scenario["slug"])
                    not in mirrored_keys
                ],
            }
            for game in library_games
        ]
        return TEMPLATES.TemplateResponse(
            request=request,
            name="dashboard.html",
            context={
                **_ui_context(request),
                "games": games,
                "campaigns": campaigns,
                "local_project_count": len(local_projects),
                "shared_projects": shared_projects,
                "project_shells": project_shells,
                "remote_repository": remote_repository,
                "mirrored_projects": mirrored_projects,
                "available_remote_projects": [
                    project for project in remote_projects if not project["local"]
                ],
                "dashboard_error": error,
                "dashboard_updated": updated,
                "dashboard_created": created,
                "dashboard_campaign_created": campaign_created,
            },
        )

    @app.post("/campaigns/create")
    def create_campaign_endpoint(
        game: str = Form(...),
        slug: str = Form(...),
        title: str = Form(""),
        author: str = Form(""),
        source_url: str = Form(""),
        source_language: str = Form("en"),
        private_only: str = Form("false"),
    ) -> RedirectResponse:
        try:
            result = create_campaign(
                lib_root,
                game=game,
                slug=slug,
                title=title,
                author=author,
                source_url=source_url,
                source_language=source_language,
                private_only=private_only == "true",
            )
        except ValueError as exc:
            return RedirectResponse(
                url=f"/?{urlencode({'error': str(exc)})}", status_code=303,
            )
        label = f"{result['game']}/{result['slug']}"
        return RedirectResponse(
            url=f"/?{urlencode({'campaign_created': label})}", status_code=303,
        )

    @app.post("/scenarios/create")
    def create_scenario_endpoint(
        campaign_id: str = Form(...),
        slug: str = Form(...),
        title: str = Form(""),
        private_only: str = Form("false"),
    ) -> RedirectResponse:
        try:
            game, campaign = campaign_id.split("/", 1)
            result = create_campaign_scenario(
                lib_root,
                game=game,
                campaign=campaign,
                slug=slug,
                title=title,
                private_only=private_only == "true",
            )
        except (TypeError, ValueError) as exc:
            return RedirectResponse(
                url=f"/?{urlencode({'error': str(exc)})}", status_code=303,
            )
        label = f"{result['game']}/{result['campaign']}/{result['slug']}"
        return RedirectResponse(
            url=f"/?{urlencode({'created': label})}", status_code=303,
        )

    @app.post("/projects/create")
    def create_project(
        game: str = Form(...),
        slug: str = Form(...),
        title: str = Form(""),
        author: str = Form(""),
        source_url: str = Form(""),
        source_language: str = Form("en"),
        private_only: str = Form("false"),
    ) -> RedirectResponse:
        try:
            result = create_shared_project(
                lib_root,
                game=game,
                slug=slug,
                title=title,
                author=author,
                source_url=source_url,
                source_language=source_language,
                private_only=private_only == "true",
            )
        except ValueError as exc:
            return RedirectResponse(
                url=f"/?{urlencode({'error': str(exc)})}", status_code=303,
            )
        label = f"{result['game']}/{result['slug']}"
        return RedirectResponse(
            url=f"/?{urlencode({'created': label})}",
            status_code=303,
        )

    @app.post("/campaigns/{game}/{slug}/private-only")
    def save_campaign_private_only(
        game: str,
        slug: str,
        private_only: str = Form("false"),
    ) -> RedirectResponse:
        try:
            update_campaign_private_only(
                lib_root,
                game=game,
                slug=slug,
                private_only=private_only == "true",
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(url="/", status_code=303)

    @app.post("/projects/translations/create")
    def create_translation(
        project_id: str = Form(...), target_language: str = Form(...),
    ) -> RedirectResponse:
        try:
            parts = project_id.split("/")
            if len(parts) == 3:
                game, campaign, slug = parts
            elif len(parts) == 2:
                game, slug = parts
                campaign = None
            else:
                raise ValueError("Invalid scenario")
            result = create_project_translation(
                lib_root,
                game=game,
                campaign=campaign,
                slug=slug,
                target_language=target_language,
            )
        except (ValueError, TypeError) as exc:
            return RedirectResponse(
                url=f"/?{urlencode({'error': str(exc)})}#create-translation",
                status_code=303,
            )
        return RedirectResponse(
            url=f"/{result['game']}/{result['target_language']}/{result['slug']}?created=1",
            status_code=303,
        )

    @app.post("/remote-projects/download")
    def download_remote_project(
        game: str = Form(...),
        slug: str = Form(...),
        campaign: str = Form(""),
        languages: list[str] = Form(...),  # noqa: B008
    ) -> RedirectResponse:
        try:
            result = download_project(
                lib_root,
                repository=remote_repository,
                game=game,
                slug=slug,
                campaign=campaign or None,
                languages=languages,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            return RedirectResponse(
                url=f"/?{urlencode({'error': str(exc)})}", status_code=303,
            )
        language = result["languages"][0]
        return RedirectResponse(url=f"/{game}/{language}/{slug}", status_code=303)

    @app.post("/remote-projects/update")
    def update_remote_project(
        game: str = Form(...), slug: str = Form(...), campaign: str = Form("")
    ) -> RedirectResponse:
        try:
            game = game.strip()
            slug = slug.strip()
            project_path = (
                lib_root / "projects" / game / campaign / "scenarios" / slug
                if campaign else lib_root / "projects" / game / slug
            )
            result = update_project(project_path, remote_repository)
        except (OSError, RuntimeError, ValueError) as exc:
            return RedirectResponse(url=f"/?{urlencode({'error': str(exc)})}", status_code=303)
        label = f"{game}/{slug}" if result["updated"] else "none"
        return RedirectResponse(url=f"/?{urlencode({'updated': label})}", status_code=303)

    @app.get("/settings", response_class=HTMLResponse)
    def settings(
        request: Request,
        language: str = "",
        error: str = "",
        saved: str = "",
    ) -> HTMLResponse:
        games = [
            game for game in discover_games(lib_root)
            if game["id"] in UI_GAME_IDS
        ]
        library_games = {
            game["slug"]: game for game in discover_library(lib_root)
            if game["slug"] in UI_GAME_IDS
        }
        for game_definition in games:
            schema = (
                game_definition.get("card_schema")
                if isinstance(game_definition.get("card_schema"), dict)
                else {}
            )
            fields = schema.get("translatable_fields") if isinstance(schema, dict) else []
            game_definition["translatable_fields_text"] = "\n".join(
                " | ".join(
                    (
                        str(field.get("id") or ""),
                        str(field.get("label") or field.get("id") or ""),
                        str(field.get("type") or "text"),
                    )
                )
                for field in fields
                if isinstance(field, dict)
            )
            metadata_fields = schema.get("metadata_fields") if isinstance(schema, dict) else []
            game_definition["metadata_fields_text"] = "\n".join(
                " | ".join(
                    (
                        str(field.get("id") or ""),
                        str(field.get("label") or field.get("id") or ""),
                        str(field.get("type") or "text"),
                    )
                )
                for field in metadata_fields
                if isinstance(field, dict)
            )
            game_definition["glossary_text"] = game_global_translation_rules(
                lib_root, game=game_definition["id"]
            )["text"]
            if game_definition["id"] == "ah":
                project_languages = {
                    str(scenario.get("target_language") or "")
                    for scenario in library_games.get("ah", {}).get("scenarios", [])
                }
                if language:
                    try:
                        project_languages.add(normalize_language_code(language))
                    except ValueError:
                        pass
                urls = game_definition.get("arkhamdb_urls")
                urls = urls if isinstance(urls, dict) else {}
                data_directory = lib_root / game_definition["id"] / "data"
                game_definition["arkhamdb_urls_text"] = "\n".join(
                    f"{code} | {url}" for code, url in sorted(urls.items())
                )
                game_definition["arkhamdb_sources"] = [
                    {
                        "language": code,
                        "url": url,
                        "downloaded": (data_directory / f"arkham.{code}.json").exists(),
                    }
                    for code, url in sorted(urls.items())
                ]
                game_definition["reference_status"] = arkham_reference_status(
                    data_directory,
                    source_language=str(
                        game_definition.get("default_source_language") or "en"
                    ),
                    requested_languages=project_languages,
                )
        return TEMPLATES.TemplateResponse(
            request=request,
            name="settings.html",
            context={
                **_ui_context(request),
                "games": games,
                "definition_issues": [
                    issue for issue in game_definition_issues(lib_root)
                    if str(issue.get("path") or "").split("/", 1)[0] in UI_GAME_IDS
                ],
                "settings_error": error,
                "settings_saved": saved,
                "translation_settings": {
                    "ollama": ollama_status(),
                    "card_image_provider": card_image_provider(),
                    "card_image_providers": [{
                        "id": "none",
                        "available": True,
                        "model": "",
                    }, *[
                        {
                            "id": provider,
                            "available": provider_available(provider),
                            "model": provider_model(provider),
                        }
                        for provider in CARD_IMAGE_PROVIDERS
                    ]],
                    "env_path": str(project_root() / ".env"),
                },
            },
        )

    @app.post("/settings/translation-providers")
    def save_translation_providers(
        ollama_enabled: str = Form("false"),
        image_provider: str = Form("none"),
    ) -> RedirectResponse:
        if image_provider not in ("none", *CARD_IMAGE_PROVIDERS):
            return _settings_redirect(error="Unknown card image provider")
        update_local_env(
            {
                "UNSPEAKABLE_TRANSLATIONS_ENABLE_OLLAMA": (
                    "true" if ollama_enabled == "true" else "false"
                ),
                "UNSPEAKABLE_TRANSLATIONS_CARD_IMAGE_PROVIDER": image_provider,
            }
        )
        return _settings_redirect(saved="translation_providers")

    @app.post("/settings/games")
    def create_or_update_game(
        game_id: str = Form(...),
        name: str = Form(...),
        description: str | None = Form(None),
        default_source_language: str = Form("en"),
        translation_context: str | None = Form(None),
    ) -> RedirectResponse:
        try:
            save_game(
                lib_root,
                game_id=game_id,
                name=name,
                description=description,
                default_source_language=default_source_language,
                translation_context=translation_context,
            )
        except ValueError as exc:
            return _settings_redirect(error=str(exc))
        return _settings_redirect(saved="game")

    @app.post("/settings/games/{game_id}/system-config")
    def save_game_system_config(
        game_id: str,
        translation_context: str = Form(""),
        glossary_text: str = Form(""),
        arkhamdb_urls_text: str = Form(""),
    ) -> RedirectResponse:
        try:
            update_game_system_config(
                lib_root,
                game=game_id,
                translation_context=translation_context,
                glossary_text=glossary_text,
                arkhamdb_urls_text=arkhamdb_urls_text,
            )
        except ValueError as exc:
            return _settings_redirect(error=str(exc), game=game_id)
        return _settings_redirect(saved="system_config", game=game_id)

    @app.post("/settings/games/{game_id}/arkhamdb-download")
    def download_arkhamdb_data(
        game_id: str, language: str = Form(...)
    ) -> RedirectResponse:
        game_definition = next(
            (game for game in discover_games(lib_root) if game.get("id") == game_id), None
        )
        if game_id != "ah" or game_definition is None:
            return _settings_redirect(error="ArkhamDB is available only for Arkham", game=game_id)
        try:
            language = normalize_language_code(language)
            urls = game_definition.get("arkhamdb_urls")
            url = urls.get(language) if isinstance(urls, dict) else None
            if not url:
                raise ValueError(f"No ArkhamDB URL configured for {language.upper()}")
            data_directory = lib_root / game_id / "data"
            download_arkhamdb_cards(data_directory, language=language, url=str(url))
            source_language = str(game_definition.get("default_source_language") or "en")
            if (data_directory / f"arkham.{source_language}.json").exists():
                build_arkham_reference_index(
                    data_directory, source_language=source_language
                )
        except ValueError as exc:
            return _settings_redirect(error=str(exc), game=game_id)
        return _settings_redirect(saved="arkhamdb_data", game=game_id)

    @app.post("/settings/games/{game_id}/json-sources")
    async def create_or_update_json_source(
        request: Request,
        game_id: str,
    ) -> RedirectResponse:
        form = await request.form()
        try:
            save_json_import_source(
                lib_root,
                game_id=game_id,
                source_id=str(form.get("source_id") or ""),
                label=str(form.get("label") or ""),
                records_path=str(form.get("records_path") or "$"),
                identifier_namespace=str(form.get("identifier_namespace") or ""),
                identifier=str(form.get("identifier") or "id"),
                fields={
                    str(key).removeprefix("field."): str(value)
                    for key, value in form.items()
                    if str(key).startswith("field.")
                },
            )
        except ValueError as exc:
            return _settings_redirect(error=str(exc), game=game_id)
        return _settings_redirect(saved="source", game=game_id)

    @app.post("/settings/games/{game_id}/card-schema")
    def update_card_schema(
        game_id: str,
        fields_text: str = Form(...),
        metadata_fields_text: str = Form(""),
    ) -> RedirectResponse:
        try:
            save_translatable_fields(
                lib_root,
                game_id=game_id,
                fields_text=fields_text,
                metadata_fields_text=metadata_fields_text,
            )
        except ValueError as exc:
            return _settings_redirect(error=str(exc), game=game_id)
        return _settings_redirect(saved="schema", game=game_id)

    @app.post("/settings/games/{game_id}/filename-parser")
    def update_filename_parser(
        game_id: str,
        pattern: str = Form(""),
        identifier_namespace: str = Form(""),
    ) -> RedirectResponse:
        try:
            save_filename_parser(
                lib_root,
                game_id=game_id,
                pattern=pattern,
                identifier_namespace=identifier_namespace,
            )
        except ValueError as exc:
            return _settings_redirect(error=str(exc), game=game_id)
        return _settings_redirect(saved="filename_parser", game=game_id)

    @app.post("/settings/games/{game_id}/reference-index")
    def rebuild_reference_index(game_id: str) -> RedirectResponse:
        game_definition = next(
            (game for game in discover_games(lib_root) if game.get("id") == game_id), None
        )
        if game_definition is None:
            return _settings_redirect(error="Unknown game", game=game_id)
        if game_id != "ah":
            return _settings_redirect(
                error="Official reference indexing is currently available only for Arkham",
                game=game_id,
            )
        try:
            build_arkham_reference_index(
                lib_root / game_id / "data",
                source_language=str(game_definition.get("default_source_language") or "en"),
            )
        except ValueError as exc:
            return _settings_redirect(error=str(exc), game=game_id)
        return _settings_redirect(saved="reference_index", game=game_id)

    @app.post("/settings/games/{game_id}/json-sources/{source_id}/delete")
    def delete_json_source(game_id: str, source_id: str) -> RedirectResponse:
        try:
            delete_json_import_source(lib_root, game_id=game_id, source_id=source_id)
        except ValueError as exc:
            return _settings_redirect(error=str(exc), game=game_id)
        return _settings_redirect(saved="source_deleted", game=game_id)

    @app.post("/settings/games/{game_id}/glossary")
    def update_game_glossary(
        game_id: str,
        language: str = Form(...),
        rules_text: str = Form(""),
    ) -> RedirectResponse:
        try:
            result = update_game_translation_rules(
                lib_root, game=game_id, language=language, rules_text=rules_text
            )
        except ValueError as exc:
            return _settings_redirect(error=str(exc), game=game_id, language=language)
        return _settings_redirect(
            saved="glossary", game=game_id, language=result["language"]
        )

    @app.get("/lotr/online", response_class=HTMLResponse)
    def lotr_online(request: Request, view: str = "scenarios") -> HTMLResponse:
        view = view if view in {"scenarios", "sets"} else "scenarios"
        scenarios: list[dict[str, str]] = []
        sets: list[dict[str, str]] = []
        error = ""
        provider = HallOfBeornProvider()
        try:
            if view == "sets":
                sets = provider.list_sets()
            else:
                scenarios = provider.list_scenarios()
        except Exception as exc:  # UI should remain usable if the remote site is unavailable.
            error = str(exc)
        finally:
            provider.close()

        local_projects: dict[str, list[dict[str, object]]] = {}
        for scenario in (
            scenario
            for game in discover_library(lib_root)
            if game["slug"] == "lotr"
            for scenario in game["scenarios"]
            if scenario.get("initialized")
        ):
            local_projects.setdefault(str(scenario["slug"]), []).append(scenario)
        for scenario in scenarios:
            local = (local_projects.get(scenario.get("slug", "")) or [None])[0]
            if local and local.get("product_printings"):
                scenario["products"] = list(local["product_printings"])
        return TEMPLATES.TemplateResponse(
            request=request,
            name="online_lotr.html",
            context={
                **_ui_context(request),
                "view": view,
                "scenarios": scenarios,
                "sets": sets,
                "local_projects": local_projects,
                "error": error,
            },
        )


    @app.get("/lotr/online/{scenario_slug}/products")
    def lotr_online_products(scenario_slug: str) -> dict[str, object]:
        provider = HallOfBeornProvider()
        try:
            scenario = provider.get_scenario(scenario_slug)
            products = provider.get_scenario_printings(
                str(scenario.get("title") or scenario_slug.replace("-", " "))
            )
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail=f"Hall of Beorn lookup failed: {exc}"
            ) from exc
        finally:
            provider.close()
        return {"products": products}

    @app.post("/lotr/online/{scenario_slug}/import")
    def import_lotr_scenario(
        scenario_slug: str,
        target_language: str = Form("de"),
    ) -> RedirectResponse:
        try:
            target_language = normalize_language_code(target_language)
            import_hall_of_beorn_scenario(
                lib_root,
                scenario_slug=scenario_slug,
                target_language=target_language,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Hall of Beorn import failed: {exc}") from exc
        return RedirectResponse(url=f"/lotr/{target_language}/{scenario_slug}", status_code=303)

    @app.get("/{game}/{slug}")
    def legacy_scenario_redirect(game: str, slug: str) -> RedirectResponse:
        matches = [
            scenario
            for game_data in discover_library(lib_root)
            if game_data["slug"] == game
            for scenario in game_data["scenarios"]
            if scenario["slug"] == slug
        ]
        if len(matches) != 1:
            raise HTTPException(status_code=404, detail="Scenario not found or language is ambiguous")
        scenario = matches[0]
        return RedirectResponse(
            url=f"/{game}/{scenario['storage_language']}/{slug}", status_code=307
        )

    @app.post("/{game}/{project_language}/{slug}/copy-language")
    def copy_project_language(
        game: str,
        project_language: str,
        slug: str,
        target_language: str = Form(...),
    ) -> RedirectResponse:
        try:
            result = copy_scenario_to_language(
                lib_root,
                game=game,
                slug=slug,
                project_language=project_language,
                target_language=target_language,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(
            url=f"/{game}/{result['target_language']}/{slug}", status_code=303
        )

    @app.get("/{game}/{project_language}/{slug}", response_class=HTMLResponse)
    def scenario_detail(
        request: Request,
        game: str,
        project_language: str,
        slug: str,
        page: int = 1,
        cards_per_page: int = 100,
        created: str = "",
        published: str = "",
        publish_error: str = "",
        document_updated: str = "",
    ) -> HTMLResponse:
        scenario = get_scenario(lib_root, game, slug, language=project_language)
        if scenario is None:
            raise HTTPException(status_code=404, detail="Scenario not found")
        status = publication_status(scenario, project_root())
        all_cards = scenario.get("grouped_cards", [])
        for card in all_cards:
            card["changed_from_published"] = card["key"] in status["changed_cards"]
        has_extracted_card_data = any(
            bool((card.get("source_sides") or {}).get(side))
            for card in all_cards
            if isinstance(card, dict)
            for side in ("front", "back")
        )
        cards_per_page = cards_per_page if cards_per_page in {50, 100, 200} else 100
        page_count = max(1, ceil(len(all_cards) / cards_per_page))
        page = min(max(1, page), page_count)
        start = (page - 1) * cards_per_page
        translation_log_available = (
            Path(scenario["path"]) / "logs" / "translation.jsonl"
        ).is_file()
        reference_warning_key = ""
        if (
            game.casefold() == "ah"
            and scenario.get("initialized")
            and not scenario.get("source_only")
        ):
            source_language = str(
                (scenario.get("project") or {}).get("source_language") or "en"
            ).casefold().replace("_", "-")
            target_language = str(scenario.get("target_language") or "").casefold().replace(
                "_", "-"
            )
            reference_status = arkham_reference_status(
                lib_root / game / "data",
                source_language=source_language,
                requested_languages={target_language},
            )
            if not reference_status["source_exists"]:
                reference_warning_key = "translation.reference_missing_source"
            elif target_language in reference_status["missing_languages"]:
                reference_warning_key = "translation.reference_missing_target"
            elif not reference_status["index_exists"]:
                reference_warning_key = "translation.reference_missing_index"
            elif not reference_status["source_indexed"]:
                reference_warning_key = "translation.reference_wrong_source"
            elif target_language in reference_status["missing_index_languages"]:
                reference_warning_key = "translation.reference_target_not_indexed"
            elif reference_status["stale"]:
                reference_warning_key = "translation.reference_stale"
        scenario = {
            **scenario,
            "documents": document_overview(lib_root, scenario),
            "translation_log_available": translation_log_available,
            "reference_warning_key": reference_warning_key,
            "has_extracted_card_data": has_extracted_card_data,
            "grouped_cards": all_cards[start : start + cards_per_page],
            "card_pagination": {
                "page": page,
                "page_count": page_count,
                "cards_per_page": cards_per_page,
                "total": len(all_cards),
                "start": start + 1 if all_cards else 0,
                "end": min(start + cards_per_page, len(all_cards)),
            },
        }
        rules = (
            translation_rules(
                lib_root,
                game=game,
                slug=slug,
                language=scenario["target_language"],
            )
            if scenario["initialized"] and not scenario.get("source_only")
            else None
        )
        arkhamdb_import_enabled = arkhamdb_scenario_import_enabled()
        return TEMPLATES.TemplateResponse(
            request=request,
            name="scenario.html",
            context={
                **_ui_context(request),
                "scenario": scenario,
                "json_import_sources": [
                    source
                    for source in scenario.get("game_definition", {}).get("imports", [])
                    if isinstance(source, dict)
                    and not source.get("adapter")
                    and (
                        source.get("id") != "arkham-json"
                        or arkhamdb_import_enabled
                    )
                ],
                "unmatched_imports": unmatched_import_records(scenario),
                "latest_import": latest_import_status(scenario),
                "translation_rules": rules,
                "ollama_status": ollama_status(),
                "google_translate_available": google_translate_available(),
                "gemini_available": gemini_available(),
                "card_image_provider": card_image_provider(),
                "card_image_available": provider_available(card_image_provider()),
                "publication": status,
                "project_created": created == "1",
                "project_published": published == "1",
                "project_publish_error": publish_error,
                "document_updated": document_updated == "1",
                "arkhamdb_scenario_import_enabled": arkhamdb_import_enabled,
            },
        )

    @app.post("/{game}/{project_language}/{slug}/documents/upload")
    async def upload_project_document(
        game: str,
        project_language: str,
        slug: str,
        level: str = Form(...),
        source_name: str = Form(""),
        document_file: UploadFile = File(...),
    ) -> RedirectResponse:
        scenario = get_scenario(lib_root, game, slug, language=project_language)
        if scenario is None or not scenario.get("initialized"):
            raise HTTPException(status_code=404, detail="Scenario not found")
        content = await document_file.read(MAX_DOCUMENT_SIZE + 1)
        try:
            save_pdf_document(
                scenario,
                level=level,
                filename=document_file.filename or "document.pdf",
                content=content,
                source_name=source_name,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(
            url=f"/{game}/{project_language}/{slug}?document_updated=1",
            status_code=303,
        )

    @app.post("/{game}/{project_language}/{slug}/documents/delete")
    def delete_project_document(
        game: str,
        project_language: str,
        slug: str,
        level: str = Form(...),
        name: str = Form(...),
    ) -> RedirectResponse:
        scenario = get_scenario(lib_root, game, slug, language=project_language)
        if scenario is None or not scenario.get("initialized"):
            raise HTTPException(status_code=404, detail="Scenario not found")
        try:
            delete_pdf_document(scenario, level=level, name=name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(
            url=f"/{game}/{project_language}/{slug}?document_updated=1",
            status_code=303,
        )

    @app.get(
        "/{game}/{project_language}/{slug}/contribute",
        response_class=HTMLResponse,
    )
    def contribution_preview_page(
        request: Request,
        game: str,
        project_language: str,
        slug: str,
        published: str = "",
    ) -> HTMLResponse:
        scenario = get_scenario(lib_root, game, slug, language=project_language)
        if scenario is None:
            raise HTTPException(status_code=404, detail="Scenario not found")
        if not scenario.get("initialized") or not scenario.get("shared_layout"):
            raise HTTPException(status_code=400, detail="Scenario cannot be published")
        preview = contribution_preview(scenario, project_root())
        return TEMPLATES.TemplateResponse(
            request=request,
            name="contribution_preview.html",
            context={
                **_ui_context(request),
                "scenario": scenario,
                "preview": preview,
                "project_published": published == "1",
            },
        )

    @app.get(
        "/{game}/{project_language}/{slug}/pull-request",
        response_class=HTMLResponse,
    )
    def pull_request_page(
        request: Request,
        game: str,
        project_language: str,
        slug: str,
        result_url: str = "",
        created: str = "",
        warning: str = "",
        error: str = "",
    ) -> HTMLResponse:
        scenario = get_scenario(lib_root, game, slug, language=project_language)
        if scenario is None or not scenario.get("initialized"):
            raise HTTPException(status_code=404, detail="Scenario not found")
        status = pull_request_status(
            scenario,
            project_root(),
            upstream_url=projects_repository_url(),
            base_branch=projects_repository_branch(),
        )
        return TEMPLATES.TemplateResponse(
            request=request,
            name="pull_request.html",
            context={
                **_ui_context(request),
                "scenario": scenario,
                "pull_request": status,
                "result_url": result_url,
                "pull_request_created": created == "1",
                "pull_request_warning": warning,
                "pull_request_error": error,
            },
        )

    @app.post("/{game}/{project_language}/{slug}/pull-request")
    def submit_pull_request(
        game: str,
        project_language: str,
        slug: str,
        method: str = Form(...),
        upstream_url: str = Form(...),
        fork_url: str = Form(""),
        contribution_scope: str = Form("translation"),
        include_extractions: bool = Form(False),
    ) -> RedirectResponse:
        scenario = get_scenario(lib_root, game, slug, language=project_language)
        if scenario is None or not scenario.get("initialized"):
            raise HTTPException(status_code=404, detail="Scenario not found")
        try:
            result = create_pull_request(
                scenario,
                project_root(),
                upstream_url=upstream_url,
                base_branch=projects_repository_branch(),
                method=method,
                fork_url=fork_url,
                contribution_scope=contribution_scope,
                include_extractions=include_extractions,
            )
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            query = urlencode({"error": str(exc)})
        else:
            query = urlencode({
                "result_url": result["url"],
                "created": "1" if result["created"] else "0",
                "warning": result.get("warning", ""),
            })
        return RedirectResponse(
            url=f"/{game}/{project_language}/{slug}/pull-request?{query}",
            status_code=303,
        )

    @app.post("/{game}/{project_language}/{slug}/contribution-field")
    def update_contribution_field(
        game: str,
        project_language: str,
        slug: str,
        card_key: str = Form(...),
        scope: str = Form(...),
        path: str = Form(...),
        value: str = Form(""),
    ) -> dict[str, object]:
        scenario = get_scenario(lib_root, game, slug, language=project_language)
        if scenario is None or not scenario.get("initialized"):
            raise HTTPException(status_code=404, detail="Scenario not found")
        try:
            group, separator, remainder = path.partition(".")
            if not separator or scope not in {"source", "translation"}:
                raise ValueError("Invalid contribution field")
            if group == "metadata" and scope == "source":
                side, separator, field = remainder.partition(".")
                if not separator:
                    side, field = "front", remainder
                if side not in {"front", "back"} or not field:
                    raise ValueError("Invalid metadata field")
                update_card_game_fields(
                    lib_root,
                    game=game,
                    slug=slug,
                    project_language=project_language,
                    card_key=card_key,
                    field_values={field: value},
                    side=side,
                )
            elif group == "text":
                side, separator, field = remainder.partition(".")
                if not separator or side not in {"front", "back"} or not field:
                    raise ValueError("Invalid text field")
                if scope == "source":
                    update_card_source_text(
                        lib_root,
                        game=game,
                        slug=slug,
                        project_language=project_language,
                        card_key=card_key,
                        side=side,
                        title="",
                        rules="",
                        flavor="",
                        traits="",
                        field_values={field: value},
                    )
                elif not scenario.get("source_only"):
                    update_card_translation(
                        lib_root,
                        game=game,
                        slug=slug,
                        card_key=card_key,
                        language=project_language,
                        side=side,
                        title="",
                        rules="",
                        flavor="",
                        traits="",
                        field_values={field: value},
                    )
                else:
                    raise ValueError("This workspace has no translation")
            else:
                raise ValueError("This field cannot be edited in the preview")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "card_key": card_key, "path": path, "scope": scope}

    @app.post("/{game}/{project_language}/{slug}/publish")
    def publish_project_results(
        game: str, project_language: str, slug: str
    ) -> RedirectResponse:
        try:
            if project_language == "source":
                publish_extraction(
                    lib_root,
                    project_root(),
                    game=game,
                    slug=slug,
                )
            else:
                publish_translation(
                    lib_root,
                    project_root(),
                    game=game,
                    slug=slug,
                    language=project_language,
                )
        except (OSError, ValueError) as exc:
            return RedirectResponse(
                url=(
                    f"/{game}/{project_language}/{slug}?"
                    f"{urlencode({'publish_error': str(exc)})}"
                ),
                status_code=303,
            )
        return RedirectResponse(
            url=f"/{game}/{project_language}/{slug}/contribute?published=1",
            status_code=303,
        )

    @app.post("/{game}/{project_language}/{slug}/import-data")
    async def import_data(
        game: str,
        project_language: str,
        slug: str,
        source_id: str = Form(...),
        data_file: UploadFile = File(...),
    ) -> dict[str, object]:
        if game == "ah" and source_id == "arkham-json" and not arkhamdb_scenario_import_enabled():
            raise HTTPException(status_code=404, detail="Not found")
        content = await data_file.read(10 * 1024 * 1024 + 1)
        if len(content) > 10 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="JSON file exceeds the 10 MB limit")
        try:
            result = import_card_data(
                lib_root,
                game_id=game,
                scenario_slug=slug,
                project_language=project_language,
                source_id=source_id,
                filename=data_file.filename or "cards.json",
                content=content,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, **result}

    @app.post("/ah/{project_language}/{slug}/import-arkhamdb")
    def import_arkhamdb(project_language: str, slug: str) -> dict[str, object]:
        if not arkhamdb_scenario_import_enabled():
            raise HTTPException(status_code=404, detail="Not found")
        try:
            result = import_arkhamdb_scenario_data(
                lib_root,
                scenario_slug=slug,
                project_language=project_language,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, **result}

    @app.post("/ah/{project_language}/{slug}/import-shoggoth")
    async def import_shoggoth(
        project_language: str,
        slug: str,
        data_file: UploadFile = File(...),
    ) -> dict[str, object]:
        content = await data_file.read(10 * 1024 * 1024 + 1)
        if len(content) > 10 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="JSON file exceeds the 10 MB limit")
        try:
            result = import_shoggoth_data(
                lib_root,
                game_id="ah",
                scenario_slug=slug,
                project_language=project_language,
                filename=data_file.filename or "shoggoth.json",
                content=content,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, **result}

    @app.post("/{game}/{project_language}/{slug}/export-shoggoth")
    def download_shoggoth_export(
        game: str, project_language: str, slug: str
    ) -> FileResponse:
        try:
            path = export_shoggoth(
                lib_root,
                game=game,
                project_language=project_language,
                slug=slug,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return FileResponse(
            path,
            media_type="application/json",
            filename=f"{slug}-shoggoth.json",
        )

    @app.post("/{game}/{project_language}/{slug}/shoggoth-settings")
    def save_shoggoth_settings(
        game: str,
        project_language: str,
        slug: str,
        set_icon: str = Form(""),
        encounter_icon: str = Form(""),
        illustrator_prefix: str = Form("Illus. "),
    ) -> dict[str, object]:
        try:
            result = update_shoggoth_settings(
                lib_root,
                game=game,
                slug=slug,
                project_language=project_language,
                set_icon=set_icon,
                encounter_icon=encounter_icon,
                illustrator_prefix=illustrator_prefix,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, **result}

    @app.post("/{game}/{project_language}/{slug}/separate-card-art")
    def save_separate_card_art_setting(
        game: str,
        project_language: str,
        slug: str,
        enabled: str = Form("false"),
    ) -> RedirectResponse:
        try:
            update_separate_card_art_setting(
                lib_root,
                game=game,
                slug=slug,
                project_language=project_language,
                enabled=enabled == "true",
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(url=f"/{game}/{project_language}/{slug}", status_code=303)

    @app.post("/{game}/{project_language}/{slug}/card-art")
    async def upload_separate_card_art(
        game: str,
        project_language: str,
        slug: str,
        card_key: str = Form(...),
        side: str = Form(...),
        art_file: UploadFile = File(...),
    ) -> RedirectResponse:
        content = await art_file.read(50 * 1024 * 1024 + 1)
        if len(content) > 50 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="Card art exceeds the 50 MB limit")
        try:
            save_separate_card_art(
                lib_root,
                game=game,
                slug=slug,
                project_language=project_language,
                card_key=card_key,
                side=side,
                filename=art_file.filename or "card-art",
                content=content,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(url=f"/{game}/{project_language}/{slug}", status_code=303)

    @app.post("/{game}/{project_language}/{slug}/completion")
    def save_translation_completion(
        game: str,
        project_language: str,
        slug: str,
        final_reviewed: str = Form("false"),
    ) -> RedirectResponse:
        try:
            update_translation_completion(
                lib_root,
                game=game,
                slug=slug,
                project_language=project_language,
                final_reviewed=final_reviewed == "true",
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(url=f"/{game}/{project_language}/{slug}", status_code=303)

    @app.post("/{game}/{project_language}/{slug}/private-only")
    def save_scenario_private_only(
        game: str,
        project_language: str,
        slug: str,
        private_mode: str = Form("inherit"),
    ) -> RedirectResponse:
        try:
            update_scenario_private_only(
                lib_root,
                game=game,
                slug=slug,
                mode=private_mode,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(url=f"/{game}/{project_language}/{slug}", status_code=303)

    @app.post("/{game}/{project_language}/{slug}/preview-import")
    async def preview_import(
        game: str,
        project_language: str,
        slug: str,
        source_id: str = Form(...),
        data_file: UploadFile = File(...),
    ) -> dict[str, object]:
        if game == "ah" and source_id == "arkham-json" and not arkhamdb_scenario_import_enabled():
            raise HTTPException(status_code=404, detail="Not found")
        content = await data_file.read(10 * 1024 * 1024 + 1)
        if len(content) > 10 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="JSON file exceeds the 10 MB limit")
        try:
            result = preview_card_data(
                lib_root,
                game_id=game,
                scenario_slug=slug,
                project_language=project_language,
                source_id=source_id,
                content=content,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, **result}

    @app.post("/{game}/{project_language}/{slug}/preview-filenames")
    def preview_filenames(
        game: str,
        project_language: str,
        slug: str,
        parser_pattern: str | None = Form(None),
    ) -> dict[str, object]:
        try:
            result = preview_filename_metadata(
                lib_root,
                game_id=game,
                scenario_slug=slug,
                project_language=project_language,
                parser_pattern=parser_pattern,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, **result}

    @app.post("/{game}/{project_language}/{slug}/apply-filenames")
    def apply_filenames(
        game: str,
        project_language: str,
        slug: str,
        parser_pattern: str | None = Form(None),
    ) -> dict[str, object]:
        try:
            result = apply_filename_metadata(
                lib_root,
                game_id=game,
                scenario_slug=slug,
                project_language=project_language,
                parser_pattern=parser_pattern,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, **result}

    @app.post("/{game}/{project_language}/{slug}/card-identifier")
    def card_identifier(
        game: str,
        project_language: str,
        slug: str,
        card_key: str = Form(...),
        namespace: str = Form(...),
        external_id: str = Form(""),
    ) -> dict[str, object]:
        try:
            result = set_card_identifier(
                lib_root,
                game_id=game,
                scenario_slug=slug,
                project_language=project_language,
                card_key=card_key,
                namespace=namespace,
                external_id=external_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, **result}

    @app.post("/{game}/{project_language}/{slug}/match-import")
    def match_import(
        game: str,
        project_language: str,
        slug: str,
        import_record_id: str = Form(...),
        card_key: str = Form(...),
    ) -> dict[str, object]:
        try:
            result = match_import_record(
                lib_root,
                game_id=game,
                scenario_slug=slug,
                project_language=project_language,
                import_record_id=import_record_id,
                card_key=card_key,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, **result}

    @app.post("/{game}/{project_language}/{slug}/initialize")
    def initialize(
        game: str,
        project_language: str,
        slug: str,
        title: str = Form(""),
        translated_title: str = Form(""),
        author: str = Form(""),
        version: str = Form(""),
        source_url: str = Form(""),
        target_language: str = Form("de"),
        card_source: str = Form(""),
    ) -> RedirectResponse:
        try:
            target_language = normalize_language_code(target_language)
            initialize_scenario(
                lib_root,
                game=game,
                slug=slug,
                title=title,
                translated_title=translated_title,
                author=author,
                version=version,
                source_url=source_url,
                target_language=target_language,
                project_language=project_language,
                card_source=card_source,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(url=f"/{game}/{target_language}/{slug}", status_code=303)

    @app.post("/{game}/{project_language}/{slug}/delete")
    def delete_project(game: str, project_language: str, slug: str) -> RedirectResponse:
        try:
            delete_scenario(lib_root, game=game, slug=slug, language=project_language)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(url="/", status_code=303)

    @app.post("/{game}/{project_language}/{slug}/image-meta")
    def image_meta(
        game: str,
        project_language: str,
        slug: str,
        image_name: str = Form(...),
        display_rotation: int | None = Form(None),
        hidden: str | None = Form(None),
    ) -> dict[str, object]:
        try:
            hidden_value = None if hidden is None else hidden.lower() in {"1", "true", "yes", "on"}
            item = update_image_metadata(
                lib_root,
                game=game,
                slug=slug,
                project_language=project_language,
                image_name=image_name,
                display_rotation=display_rotation,
                hidden=hidden_value,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, **item}

    @app.post("/{game}/{project_language}/{slug}/card-game-fields")
    async def card_game_fields(
        request: Request,
        game: str,
        project_language: str,
        slug: str,
    ) -> dict[str, object]:
        form = await request.form()
        card_key = str(form.get("card_key") or "")
        side = str(form.get("side") or "front")
        field_values = {
            str(key): str(value)
            for key, value in form.items()
            if key not in {"card_key", "side"}
        }
        try:
            scenario = get_scenario(lib_root, game, slug, language=project_language)
            if scenario is None:
                raise ValueError("Scenario is not initialized")
            if scenario.get("source_only"):
                item = update_card_game_fields(
                    lib_root,
                    game=game,
                    slug=slug,
                    project_language=project_language,
                    card_key=card_key,
                    field_values=field_values,
                    side=side,
                )
            else:
                item = stage_card_source_proposal(
                    lib_root,
                    game=game,
                    slug=slug,
                    project_language=project_language,
                    card_key=card_key,
                    side=side,
                    metadata_values=field_values,
                )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, **item}

    @app.post("/{game}/{project_language}/{slug}/collapse-duplicates")
    def collapse_duplicates(game: str, project_language: str, slug: str) -> dict[str, object]:
        try:
            result = collapse_duplicate_cards(
                lib_root,
                game=game,
                slug=slug,
                project_language=project_language,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, **result}

    @app.post("/{game}/{project_language}/{slug}/card-text")
    async def card_text(
        request: Request,
        game: str,
        project_language: str,
        slug: str,
    ) -> dict[str, object]:
        form = await request.form()
        card_key = str(form.get("card_key") or "")
        language = str(form.get("language") or "")
        side = str(form.get("side") or "")
        field_values = {
            str(key): str(value)
            for key, value in form.items()
            if key not in {"card_key", "language", "side"}
        }
        if language.lower() != project_language.lower():
            raise HTTPException(status_code=400, detail="Target language does not match project")
        try:
            item = update_card_translation(
                lib_root,
                game=game,
                slug=slug,
                card_key=card_key,
                language=language,
                side=side,
                title="",
                rules="",
                flavor="",
                traits="",
                field_values=field_values,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, **item}

    @app.post("/{game}/{project_language}/{slug}/source-card-text")
    async def source_card_text(
        request: Request,
        game: str,
        project_language: str,
        slug: str,
    ) -> dict[str, object]:
        form = await request.form()
        card_key = str(form.get("card_key") or "")
        side = str(form.get("side") or "")
        field_values = {
            str(key): str(value)
            for key, value in form.items()
            if key not in {"card_key", "side"}
        }
        try:
            scenario = get_scenario(lib_root, game, slug, language=project_language)
            if scenario is None:
                raise ValueError("Scenario is not initialized")
            if scenario.get("source_only"):
                item = update_card_source_text(
                    lib_root,
                    game=game,
                    slug=slug,
                    project_language=project_language,
                    card_key=card_key,
                    side=side,
                    title="",
                    rules="",
                    flavor="",
                    traits="",
                    field_values=field_values,
                )
            else:
                item = stage_card_source_proposal(
                    lib_root,
                    game=game,
                    slug=slug,
                    project_language=project_language,
                    card_key=card_key,
                    side=side,
                    text_values=field_values,
                )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, **item}

    @app.post("/{game}/{project_language}/{slug}/source-proposal")
    def source_proposal(
        game: str,
        project_language: str,
        slug: str,
        card_key: str = Form(...),
    ) -> dict[str, object]:
        try:
            result = card_source_proposal(
                lib_root,
                game=game,
                slug=slug,
                project_language=project_language,
                card_key=card_key,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, **result}

    @app.post("/{game}/{project_language}/{slug}/source-proposal/apply")
    def source_proposal_apply(
        game: str,
        project_language: str,
        slug: str,
        card_key: str = Form(...),
        updates_json: str = Form(...),
    ) -> dict[str, object]:
        try:
            updates = json.loads(updates_json)
            if not isinstance(updates, list):
                raise ValueError("Proposal updates must be a list")
            result = apply_card_source_proposal(
                lib_root,
                game=game,
                slug=slug,
                project_language=project_language,
                card_key=card_key,
                updates=updates,
            )
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True, **result}

    @app.post("/{game}/{project_language}/{slug}/translation-rules")
    def save_translation_rules(
        game: str,
        project_language: str,
        slug: str,
        language: str = Form(...),
        game_rules: str | None = Form(None),
        scenario_rules: str = Form(""),
    ) -> dict[str, object]:
        if language.lower() != project_language.lower():
            raise HTTPException(status_code=400, detail="Target language does not match project")
        try:
            item = update_translation_rules(
                lib_root,
                game=game,
                slug=slug,
                language=language,
                game_rules_text=game_rules,
                scenario_rules_text=scenario_rules,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "rule_count": len(item["merged"])}

    @app.post("/{game}/{project_language}/{slug}/translate-card")
    def translate_card(
        game: str,
        project_language: str,
        slug: str,
        card_key: str = Form(...),
        language: str = Form(...),
        provider: str = Form("translategemma"),
    ) -> dict[str, object]:
        if language.lower() != project_language.lower():
            raise HTTPException(status_code=400, detail="Target language does not match project")
        try:
            item = translate_missing_card_fields(
                lib_root,
                game=game,
                slug=slug,
                card_key=card_key,
                language=language,
                provider=provider,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except TranslationUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except TranslationReviewRequired as exc:
            return JSONResponse(
                status_code=409,
                content={
                    "review_required": True,
                    "detail": str(exc),
                    "card_key": card_key,
                    "language": language,
                    "side": exc.side,
                    "field": exc.field,
                    "original": exc.original,
                    "draft": exc.draft,
                    "issues": exc.issues,
                    "translation_log": exc.traces or ([exc.trace] if exc.trace else []),
                },
            )
        except TranslationFailed as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"ok": True, **item}

    @app.post("/{game}/{project_language}/{slug}/external-translation")
    def apply_external_translation(
        game: str,
        project_language: str,
        slug: str,
        card_key: str = Form(...),
        language: str = Form(...),
        side: str = Form(...),
        field: str = Form(...),
        draft: str = Form(...),
    ) -> dict[str, object]:
        if language.lower() != project_language.lower():
            raise HTTPException(status_code=400, detail="Target language does not match project")
        try:
            result = apply_external_card_translation(
                lib_root,
                game=game,
                slug=slug,
                card_key=card_key,
                language=language,
                side=side,
                field=field,
                draft=draft,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, **result}

    @app.post("/{game}/{project_language}/{slug}/external-translation-source")
    def external_translation_source(
        game: str,
        project_language: str,
        slug: str,
        card_key: str = Form(...),
        language: str = Form(...),
        side: str = Form(...),
        field: str = Form(...),
    ) -> dict[str, object]:
        if language.lower() != project_language.lower():
            raise HTTPException(status_code=400, detail="Target language does not match project")
        try:
            result = prepare_external_card_translation(
                lib_root,
                game=game,
                slug=slug,
                card_key=card_key,
                language=language,
                side=side,
                field=field,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, **result}

    @app.post("/{game}/{project_language}/{slug}/card-image-preview")
    def card_image_preview(
        game: str,
        project_language: str,
        slug: str,
        card_key: str = Form(...),
        side: str = Form(...),
    ) -> dict[str, object]:
        try:
            result = preview_card_image(
                lib_root,
                game=game,
                slug=slug,
                project_language=project_language,
                card_key=card_key,
                side=side,
                provider=card_image_provider(),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except (RuntimeError, OSError, TypeError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"ok": True, **result}

    @app.post("/{game}/{project_language}/{slug}/card-image-prompt")
    def card_image_prompt(
        game: str,
        project_language: str,
        slug: str,
        card_key: str = Form(...),
        side: str = Form(...),
    ) -> dict[str, object]:
        try:
            result = gemini_card_image_prompt(
                lib_root,
                game=game,
                slug=slug,
                project_language=project_language,
                card_key=card_key,
                side=side,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, **result}

    @app.post("/{game}/{project_language}/{slug}/card-image-batch")
    def card_image_batch(
        game: str,
        project_language: str,
        slug: str,
        force_image_analysis: bool = Form(False),
        extracted_data_only: bool = Form(False),
    ) -> dict[str, object]:
        try:
            result = prepare_card_image_batch(
                lib_root,
                game=game,
                slug=slug,
                project_language=project_language,
                force_image_analysis=force_image_analysis,
                extracted_data_only=extracted_data_only,
            )
        except (OSError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, **result}

    @app.post("/{game}/{project_language}/{slug}/card-image-batch-apply")
    def card_image_batch_apply(
        game: str,
        project_language: str,
        slug: str,
    ) -> dict[str, object]:
        scenario = get_scenario(lib_root, game, slug, language=project_language)
        if scenario is None or not scenario.get("initialized"):
            raise HTTPException(status_code=404, detail="Scenario not found")
        batch_directory = (
            Path(scenario["path"]) / "llm-batch" / str(scenario["target_language"])
        )
        try:
            result = apply_card_image_batch_results(lib_root, batch_directory)
        except (OSError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, **result}

    @app.post("/{game}/{project_language}/{slug}/card-image-apply")
    def card_image_apply(
        game: str,
        project_language: str,
        slug: str,
        card_key: str = Form(...),
        side: str = Form(...),
        result_json: str = Form(...),
    ) -> dict[str, object]:
        try:
            result = json.loads(result_json)
            if not isinstance(result, dict):
                raise ValueError("Gemini result must be an object")
            apply_gemini_card_image(
                lib_root,
                game=game,
                slug=slug,
                project_language=project_language,
                card_key=card_key,
                side=side,
                result=result,
            )
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True}

    @app.post("/{game}/{project_language}/{slug}/format-rules")
    def format_rules(
        game: str,
        project_language: str,
        slug: str,
        result_json: str = Form(...),
    ) -> dict[str, object]:
        try:
            if game.casefold() != "ah":
                raise ValueError("Rule formatting is available only for Arkham Horror")
            result = json.loads(result_json)
            if not isinstance(result, dict):
                raise TypeError("Gemini result must be an object")
            formatted = format_arkham_rules_result(result)
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "result": formatted}

    @app.post("/{game}/{project_language}/{slug}/ocr-region")
    def ocr_region(
        game: str,
        project_language: str,
        slug: str,
        image_name: str = Form(...),
        language: str = Form("en"),
        x: float = Form(...),
        y: float = Form(...),
        width: float = Form(...),
        height: float = Form(...),
        rotation: int = Form(0),
    ) -> dict[str, object]:
        scenario = get_scenario(lib_root, game, slug, language=project_language)
        if scenario is None or not scenario["initialized"]:
            raise HTTPException(status_code=404, detail="Scenario not found")
        image = next((item for item in scenario["cards"] if item.name == image_name), None)
        if image is None:
            raise HTTPException(status_code=400, detail="Image is not part of the configured card source")
        source_language = str((scenario.get("project") or {}).get("source_language") or "en")
        if language.lower() != source_language:
            raise HTTPException(status_code=400, detail="Unsupported OCR source language")
        if not dependencies_available():
            raise HTTPException(status_code=503, detail="OCR requires ImageMagick and Tesseract")
        item = extract_region(
            image,
            x=x,
            y=y,
            width=width,
            height=height,
            rotation=rotation,
            language=language.lower(),
        )
        if item is None:
            raise HTTPException(status_code=422, detail="No text recognized in the selected region")
        return {"ok": True, **item}

    @app.post("/{game}/{project_language}/{slug}/card-ocr-apply")
    def card_ocr_apply(
        game: str,
        project_language: str,
        slug: str,
        card_key: str = Form(...),
        image_name: str = Form(...),
        language: str = Form("en"),
        side: str = Form(...),
        field: str = Form(...),
        value: str = Form(""),
        raw_text: str = Form(""),
        confidence: float | None = Form(None),
        x: float = Form(...),
        y: float = Form(...),
        width: float = Form(...),
        height: float = Form(...),
        rotation: int = Form(0),
    ) -> dict[str, object]:
        try:
            item = apply_card_ocr_text(
                lib_root,
                game=game,
                slug=slug,
                project_language=project_language,
                card_key=card_key,
                image_name=image_name,
                language=language,
                side=side,
                field=field,
                value=value,
                raw_text=raw_text,
                confidence=confidence,
                x=x,
                y=y,
                width=width,
                height=height,
                rotation=rotation,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, **item}

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
