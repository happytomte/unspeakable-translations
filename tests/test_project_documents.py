from __future__ import annotations

import pytest
from starlette.requests import Request

from card_translator.library import get_scenario
from card_translator.project_documents import (
    delete_pdf_document,
    document_overview,
    save_pdf_document,
)
from card_translator.project_publication import (
    contribution_preview,
    create_campaign,
    create_campaign_scenario,
    create_project_translation,
    publish_translation,
)
from card_translator.web.app import create_app

PDF = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n%%EOF\n"


def create_campaign_workspaces(root):
    create_campaign(
        root,
        game="ah",
        slug="campaign",
        title="Campaign",
        author="",
        source_url="",
        source_language="en",
    )
    create_campaign_scenario(
        root,
        game="ah",
        campaign="campaign",
        slug="scenario",
        title="Scenario",
    )
    create_project_translation(
        root,
        game="ah",
        campaign="campaign",
        slug="scenario",
        target_language="de",
    )
    source = get_scenario(
        root, "ah", "scenario", language="source", campaign="campaign"
    )
    translation = get_scenario(
        root, "ah", "scenario", language="de", campaign="campaign"
    )
    return source, translation


def test_pdf_documents_are_matched_by_scope_and_name(tmp_path):
    source, translation = create_campaign_workspaces(tmp_path)
    save_pdf_document(
        source,
        level="campaign",
        filename="Campaign Guide.pdf",
        content=PDF,
    )
    saved = save_pdf_document(
        source,
        level="scenario",
        filename="../Scenario Sheet.pdf",
        content=PDF,
    )
    assert saved["name"] == "Scenario Sheet.pdf"

    overview = document_overview(tmp_path, translation)
    assert overview["campaign"]["missing_count"] == 1
    assert overview["scenario"]["missing_count"] == 1
    assert overview["campaign"]["entries"][0]["source_url"].endswith(
        "Campaign%20Guide.pdf"
    )
    preview = contribution_preview(translation, tmp_path / "repository")
    assert preview["missing_document_count"] == 2
    publish_translation(
        tmp_path,
        tmp_path / "repository",
        game="ah",
        slug="scenario",
        language="de",
    )
    assert (
        tmp_path
        / "repository/projects/ah/campaign/documents/source/Campaign Guide.pdf"
    ).is_file()
    assert not (
        tmp_path
        / "repository/projects/ah/campaign/documents/translations/de/Campaign Guide.pdf"
    ).exists()

    save_pdf_document(
        translation,
        level="campaign",
        filename="German filename is ignored.pdf",
        source_name="Campaign Guide.pdf",
        content=PDF,
    )
    overview = document_overview(tmp_path, translation)
    assert overview["campaign"]["missing_count"] == 0
    assert overview["campaign"]["entries"][0]["translation_exists"] is True
    assert overview["scenario"]["missing_count"] == 1

    with pytest.raises(ValueError, match="not a PDF"):
        save_pdf_document(
            source,
            level="scenario",
            filename="not-really.pdf",
            content=b"plain text",
        )
    with pytest.raises(ValueError, match="does not exist"):
        save_pdf_document(
            translation,
            level="scenario",
            filename="translation.pdf",
            source_name="unknown.pdf",
            content=PDF,
        )


def test_documents_are_published_and_source_deletion_removes_equivalents(tmp_path):
    library = tmp_path / "library"
    repository = tmp_path / "repository"
    source, translation = create_campaign_workspaces(library)
    for scenario, level, name in (
        (source, "campaign", "Campaign.pdf"),
        (source, "scenario", "Scenario.pdf"),
        (translation, "campaign", "Campaign.pdf"),
        (translation, "scenario", "Scenario.pdf"),
    ):
        save_pdf_document(
            scenario,
            level=level,
            filename=name,
            source_name=name if not scenario["source_only"] else "",
            content=PDF,
        )

    preview = contribution_preview(translation, repository)
    document_paths = {
        item["path"] for item in preview["files"] if item["kind"] == "document"
    }
    assert document_paths == {
        "projects/ah/campaign/documents/source/Campaign.pdf",
        "projects/ah/campaign/documents/translations/de/Campaign.pdf",
        "projects/ah/campaign/scenarios/scenario/source/documents/Scenario.pdf",
        "projects/ah/campaign/scenarios/scenario/translations/de/documents/Scenario.pdf",
    }

    publish_translation(
        library,
        repository,
        game="ah",
        slug="scenario",
        language="de",
    )
    for path in document_paths:
        assert (repository / path).read_bytes() == PDF

    deleted = delete_pdf_document(source, level="campaign", name="Campaign.pdf")
    assert len(deleted["deleted"]) == 2
    overview = document_overview(library, translation)
    assert overview["campaign"]["entries"] == []


def test_dashboard_calls_out_missing_document_translations(tmp_path, monkeypatch):
    library = tmp_path / "library"
    source, _ = create_campaign_workspaces(library)
    save_pdf_document(
        source, level="campaign", filename="Campaign.pdf", content=PDF
    )
    save_pdf_document(
        source, level="scenario", filename="Scenario.pdf", content=PDF
    )
    monkeypatch.setenv("CARD_TRANSLATOR_ROOT", str(tmp_path))
    monkeypatch.setenv("CARD_TRANSLATOR_LIBRARY", str(library))
    app = create_app()
    request = Request({
        "type": "http",
        "method": "GET",
        "path": "/",
        "root_path": "",
        "scheme": "http",
        "server": ("test", 80),
        "headers": [(b"cookie", b"card_translator_ui_language=de")],
        "query_string": b"",
        "router": app.router,
    })
    dashboard = next(route.endpoint for route in app.routes if route.name == "dashboard")

    body = dashboard(request).body.decode()

    assert "2 Dokumente fehlen" in body
    detail = next(route.endpoint for route in app.routes if route.name == "scenario_detail")
    detail_body = detail(
        request, game="ah", project_language="de", slug="scenario"
    ).body.decode()
    assert (
        '<details class="panel project-documents-panel '
        'project-documents-collapsible" id="documents">'
    ) in detail_body
    assert '<summary class="project-documents-summary">' in detail_body
    assert "Noch fehlende PDF-Übersetzungen" not in detail_body
    assert "Es fehlen noch 2 PDF-Übersetzungen." in detail_body
    assert 'href="/ah/de/scenario/contribute"' in detail_body
    source_detail_body = detail(
        request, game="ah", project_language="source", slug="scenario"
    ).body.decode()
    assert '<section class="panel project-documents-panel" id="documents">' in source_detail_body
    assert "project-documents-collapsible" not in source_detail_body

    preview_page = next(
        route.endpoint for route in app.routes
        if route.name == "contribution_preview_page"
    )
    preview_body = preview_page(
        request, game="ah", project_language="de", slug="scenario"
    ).body.decode()
    assert "Noch fehlende PDF-Übersetzungen" in preview_body
    assert "Kampagne · Campaign.pdf" in preview_body
    assert "Szenario · Scenario.pdf" in preview_body
    assert 'action="/ah/de/scenario/publish"' in preview_body
