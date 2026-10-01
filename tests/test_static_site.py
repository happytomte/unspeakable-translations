import re
from pathlib import Path
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

ROOT = Path(__file__).parents[1]
SITE = ROOT / "site"
PAGES = tuple(SITE / name for name in ("index.html", "guide.html", "projects.html"))
I18N_ATTRIBUTES = (
    "data-i18n",
    "data-i18n-html",
    "data-i18n-aria",
    "data-i18n-placeholder",
    "data-i18n-content",
    "data-i18n-title",
)


def _soup(path: Path) -> BeautifulSoup:
    return BeautifulSoup(path.read_text(encoding="utf-8"), "html.parser")


def test_static_site_defaults_to_english_and_offers_both_languages():
    for page in PAGES:
        document = _soup(page)
        assert document.html["lang"] == "en"
        assert document.title.get_text(strip=True)
        assert document.title.get_text(strip=True) not in {
            "Anleitung · Unspeakable Translations",
            "Projekte · Unspeakable Translations",
        }
        selector = document.select_one("[data-language-select]")
        assert [(option["value"], option.get_text(strip=True)) for option in selector.select("option")] == [
            ("en", "English"),
            ("de", "Deutsch"),
        ]


def test_every_static_translation_key_has_a_german_value():
    javascript = (SITE / "site.js").read_text(encoding="utf-8")
    german_dictionary = javascript.split("const DYNAMIC", 1)[0]
    defined = set(re.findall(r'^\s+"([^"]+)":', german_dictionary, re.MULTILINE))
    referenced = {
        element[attribute]
        for page in PAGES
        for element in _soup(page).find_all()
        for attribute in I18N_ATTRIBUTES
        if element.has_attr(attribute)
    }
    assert referenced <= defined


def test_static_site_internal_links_and_fragments_exist():
    documents = {page.name: _soup(page) for page in PAGES}
    for page in PAGES:
        for link in documents[page.name].select("a[href]"):
            target = urlsplit(link["href"])
            if target.scheme or target.netloc:
                continue
            target_name = target.path or page.name
            if not target_name.endswith(".html"):
                continue
            assert target_name in documents, f"Missing page {target_name} linked from {page.name}"
            if target.fragment:
                assert documents[target_name].find(id=target.fragment) is not None, (
                    f"Missing fragment #{target.fragment} in {target_name}"
                )


def test_site_script_uses_english_when_no_german_preference_is_stored():
    javascript = (SITE / "site.js").read_text(encoding="utf-8")
    assert 'let siteLanguage = storedLanguage === "de" ? "de" : "en";' in javascript
    assert (
        'localStorage.setItem("unspeakable-translations-site-language", siteLanguage);'
        in javascript
    )
