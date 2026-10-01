from __future__ import annotations

import json
import zipfile

from card_translator.release_bundles import build_release_bundles


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def test_release_bundles_include_only_reviewed_languages_and_are_reproducible(tmp_path):
    project = tmp_path / "projects" / "ah" / "demo"
    write_json(project / "project.json", {"title": "Demo"})
    write_json(
        project / "translations" / "de" / "project.json",
        {"target_language": "de", "final_reviewed": True},
    )
    write_json(project / "translations" / "de" / "translation.json", {"cards": []})
    write_json(
        project / "translations" / "de" / "cards" / "001.json",
        {"format": "card-translator-card-translation", "card": {"key": "001"}},
    )
    write_json(project / "translations" / "de" / "builds" / "shoggoth.json", {"cards": []})
    (project / "translations" / "de" / "renders").mkdir()
    (project / "translations" / "de" / "renders" / "001.png").write_bytes(b"render")
    write_json(
        project / "translations" / "fr" / "project.json",
        {"target_language": "fr", "final_reviewed": False},
    )
    write_json(project / "translations" / "fr" / "translation.json", {"cards": []})
    output = tmp_path / "release"

    first = build_release_bundles(
        tmp_path,
        output,
        projects={"ah/demo"},
        languages={"de"},
        version="1.2.0",
    )
    first_bytes = (output / "ah-demo-de.zip").read_bytes()
    second = build_release_bundles(
        tmp_path,
        output,
        projects={"ah/demo"},
        languages={"de"},
        version="1.2.0",
    )

    assert first == second
    assert [item["filename"] for item in first["artifacts"]] == ["ah-demo-de.zip"]
    assert first["selection"] == {"projects": ["ah/demo"], "languages": ["de"]}
    assert first["release_tag"] == "ah-demo-v1.2.0"
    assert (output / "ah-demo-de.zip").read_bytes() == first_bytes
    assert not (output / "ah-demo-fr.zip").exists()
    with zipfile.ZipFile(output / "ah-demo-de.zip") as archive:
        assert archive.namelist() == [
            "project.json",
            "translations/de/builds/shoggoth.json",
            "translations/de/cards/001.json",
            "translations/de/project.json",
            "translations/de/renders/001.png",
            "translations/de/translation.json",
            "release.json",
        ]
        assert json.loads(archive.read("release.json"))["language"] == "de"


def test_release_bundle_preserves_campaign_context(tmp_path):
    campaign = tmp_path / "projects/ah/dark-matter"
    project = campaign / "scenarios/the-tatterdemalion"
    write_json(campaign / "campaign.json", {"title": "Dark Matter", "author": "A"})
    write_json(project / "project.json", {"title": "The Tatterdemalion"})
    write_json(
        project / "translations/de/project.json",
        {"target_language": "de", "final_reviewed": True},
    )
    write_json(project / "translations/de/translation.json", {"cards": []})

    manifest = build_release_bundles(
        tmp_path,
        tmp_path / "release",
        projects={"ah/dark-matter/the-tatterdemalion"},
    )

    artifact = manifest["artifacts"][0]
    assert artifact["campaign"] == "dark-matter"
    assert artifact["filename"] == "ah-dark-matter-the-tatterdemalion-de.zip"
    with zipfile.ZipFile(tmp_path / "release" / artifact["filename"]) as archive:
        assert "campaign.json" in archive.namelist()
