from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlencode, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, Tag

BASE_URL = "https://hallofbeorn.com"
SCENARIOS_URL = f"{BASE_URL}/LotR/Scenarios/"
SEARCH_URL = f"{BASE_URL}/LotR"
USER_AGENT = "unspeakable-translations/0.1 (local personal translation workbench)"


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _slug_from_href(href: str, prefix: str) -> str:
    path = unquote(urlparse(urljoin(BASE_URL, href)).path)
    return path.split(prefix, 1)[1].strip("/") if prefix in path else ""



def _nearest_product_title(anchor: Tag) -> str:
    """Best-effort product title for a scenario link in HoB's scenario browser."""
    node: Tag | None = anchor
    for _ in range(6):
        if node is None:
            break
        product = node.select_one('a[href*="/LotR/Products/"]')
        if product is not None and product is not anchor:
            title = _clean(product.get_text(" ", strip=True))
            if title:
                return title
        parent = node.parent
        node = parent if isinstance(parent, Tag) else None
    previous = anchor.find_previous('a', href=re.compile(r'/LotR/Products/'))
    if previous is not None:
        return _clean(previous.get_text(" ", strip=True))
    return ""

def parse_scenario_list_html(html: str) -> list[dict[str, Any]]:
    """Parse Hall of Beorn's scenario browser.

    A scenario can be listed more than once when the same playable scenario was
    released in both an original product and a revised/repackaged product. Keep
    one logical scenario entry and aggregate every surrounding HoB heading under
    ``products`` instead of dropping later occurrences.
    """
    soup = _soup(html)
    scenarios: list[dict[str, Any]] = []
    by_slug: dict[str, dict[str, Any]] = {}

    for anchor in soup.select('a[href*="/LotR/Scenarios/"]'):
        href = str(anchor.get("href") or "")
        slug = _slug_from_href(href, "/LotR/Scenarios/")
        if not slug:
            continue
        title = _clean(anchor.get_text(" ", strip=True))
        if not title or title.lower() in {"scenarios", "back to scenario list", "map view"}:
            continue

        heading = anchor.find_previous(["h2", "h3", "h4"])
        group = _clean(heading.get_text(" ", strip=True)) if heading else ""
        if group == title:
            previous = heading.find_previous(["h2", "h3", "h4"]) if heading else None
            group = _clean(previous.get_text(" ", strip=True)) if previous else ""
        product_title = _nearest_product_title(anchor)

        existing = by_slug.get(slug)
        if existing is None:
            existing = {
                "slug": slug,
                "title": title,
                "group": group,
                "products": [],
                "url": urljoin(BASE_URL, href),
            }
            by_slug[slug] = existing
            scenarios.append(existing)

        product = product_title or group
        if product and product not in existing["products"]:
            existing["products"].append(product)
        if not existing.get("group") and group:
            existing["group"] = group

    return scenarios



def parse_set_list_html(html: str) -> list[dict[str, str]]:
    """Parse Hall of Beorn's CardSet select into products/sets with group headings.

    Hall of Beorn mixes real selectable sets with uppercase section labels and visual
    separator options. We keep the section label as ``group`` but do not expose it
    as an importable set.
    """
    soup = _soup(html)
    select = soup.select_one('select#CardSet')
    if select is None:
        return []

    sets: list[dict[str, str]] = []
    group = ""
    seen: set[str] = set()
    for option in select.find_all("option"):
        value = _clean(str(option.get("value") or ""))
        label = _clean(option.get_text(" ", strip=True))
        if not value or value == "Any" or not label:
            continue
        if set(label) <= {"—", "-", "–"}:
            continue

        letters = "".join(ch for ch in label if ch.isalpha())
        is_group = bool(letters) and label == label.upper() and len(label.split()) <= 5
        if is_group:
            group = label
            continue

        if value in seen:
            continue
        seen.add(value)
        sets.append({
            "id": value,
            "title": label,
            "group": group,
            "url": f"{BASE_URL}/LotR?CardSet={value.replace(' ', '+')}",
        })
    return sets



def _printing_token(value: str) -> tuple[str, int, int | None] | None:
    """Parse HoB result labels such as ``The Black Riders (x3/x1)``."""
    match = re.match(r"^(.+?)\s*\(x(\d+)(?:/x(\d+))?\)\s*$", _clean(value))
    if not match:
        return None
    return (match.group(1).strip(), int(match.group(2)), int(match.group(3)) if match.group(3) else None)


def _result_printing(anchor: Tag) -> tuple[str, int, int | None] | None:
    """Read both historic combined and current split product/count labels."""
    result = anchor.find_parent(id=lambda value: isinstance(value, str) and value.startswith("search-result-"))
    if isinstance(result, Tag):
        labels = [_clean(label.get_text(" ", strip=True)) for label in result.select(".detail-label")]
        for label in labels:
            parsed = _printing_token(label)
            if parsed is not None:
                return parsed
        for index, label in enumerate(labels):
            count = re.fullmatch(r"\(x(\d+)(?:/x(\d+))?\)", label)
            if count and index:
                product = labels[index - 1]
                if product and not product.startswith("("):
                    return (
                        product,
                        int(count.group(1)),
                        int(count.group(2)) if count.group(2) else None,
                    )

    node: Tag | None = anchor
    for _ in range(7):
        if node is None:
            break
        for text in node.stripped_strings:
            parsed = _printing_token(str(text))
            if parsed is not None:
                return parsed
        parent = node.parent
        node = parent if isinstance(parent, Tag) else None
    return None


def parse_scenario_printings_html(html: str) -> list[str]:
    """Return concrete CardSet/product names present in a HoB scenario search.

    HoB renders the set name next to each result as plain text (for example
    ``The Black Riders (x3/x1)``), not necessarily as a ``CardSet=`` link.
    """
    soup = _soup(html)
    products: list[str] = []

    # Preferred path when HoB happens to render a set link.
    for link in soup.select('a[href*="CardSet="]'):
        title = _clean(link.get_text(" ", strip=True))
        if title and title not in products:
            products.append(title)

    # Current HoB result markup exposes the set/printing as a text node.
    for text in soup.stripped_strings:
        parsed = _printing_token(str(text))
        if parsed is None:
            continue
        title, _, _ = parsed
        if title and title not in products and "Nightmare" not in title:
            products.append(title)
    return products


def parse_scenario_usage_html(html: str) -> dict[str, dict[str, Any]]:
    """Extract per-card normal/easy quantities and concrete printings from a search page."""
    soup = _soup(html)
    usage: dict[str, dict[str, Any]] = {}
    for anchor in soup.select('a[href*="/LotR/Details/"]'):
        href = str(anchor.get("href") or "")
        slug = _slug_from_href(href, "/LotR/Details/")
        if not slug:
            continue

        parsed = _result_printing(anchor)
        if parsed is None:
            continue
        product, normal, easy = parsed
        if "Nightmare" in product:
            continue
        entry = usage.setdefault(slug, {"normal": normal, "easy": easy, "products": []})
        # Search results can contain the same logical card from old and revised
        # products. Quantities describe the scenario, so keep one value and
        # aggregate all concrete printings.
        if product not in entry["products"]:
            entry["products"].append(product)
    return usage



def parse_scenario_related_html(html: str) -> list[dict[str, str]]:
    """Return card detail links exposed by HoB's Scenario search.

    This search can contain campaign components referenced by setup/resolution
    text that the dedicated scenario table omits (for example Mr. Underhill).
    We only discover links here; the importer later fetches card details and
    decides whether an extra result is a campaign component worth importing.
    """
    soup = _soup(html)
    cards: list[dict[str, str]] = []
    seen: set[str] = set()
    for anchor in soup.select('a[href*="/LotR/Details/"]'):
        href = str(anchor.get("href") or "")
        slug = _slug_from_href(href, "/LotR/Details/").split("?", 1)[0]
        title = _clean(anchor.get_text(" ", strip=True))
        if not slug or not title or slug in seen:
            continue
        seen.add(slug)
        cards.append({
            "slug": slug,
            "title": title,
            "detail_url": urljoin(BASE_URL, href.split("?", 1)[0]),
        })
    return cards


def parse_campaign_referenced_html(
    html: str,
    *,
    campaign_card: dict[str, Any],
) -> list[dict[str, Any]]:
    """Resolve card names explicitly mentioned by a campaign card's rules text.

    Candidate identities come from the campaign card's own product search. This
    avoids guessing card names from prose while still finding setup components
    that HoB does not associate with the scenario filter.
    """
    sides = campaign_card.get("sides") if isinstance(campaign_card.get("sides"), dict) else {}
    rule_fields = {
        f"sides.{side}.rules": _clean(str((sides.get(side) or {}).get("rules") or ""))
        for side in ("front", "back")
        if isinstance(sides.get(side), dict)
    }
    if not any(rule_fields.values()):
        rules = _clean(str(campaign_card.get("rules") or ""))
        if rules:
            rule_fields["rules"] = rules

    source_title = _clean(str(campaign_card.get("title") or ""))
    candidates = parse_scenario_related_html(html)
    referenced: list[dict[str, Any]] = []
    for candidate in candidates:
        title = _clean(candidate["title"])
        if not title or title.casefold() == source_title.casefold():
            continue
        pattern = re.compile(rf"(?<!\w){re.escape(title)}(?!\w)", re.IGNORECASE)
        matched_fields = [field for field, text in rule_fields.items() if pattern.search(text)]
        if not matched_fields:
            continue
        referenced.append({
            **candidate,
            "reference": {
                "source_title": source_title,
                "source_url": campaign_card.get("url"),
                "matched_fields": matched_fields,
                "match": "exact_card_title_in_rules",
            },
        })
    return referenced

def _deck_role(card_type: str, section: str) -> str:
    normalized = card_type.strip().lower()
    if normalized == "quest":
        return "quest_deck"
    if normalized == "campaign":
        return "campaign"
    if normalized in {"boon", "burden"}:
        return "campaign_component"
    if section.strip().lower() == "quest cards":
        return "quest_deck"
    return "encounter_deck"

def _row_counts(anchor: Tag) -> tuple[int | None, int | None, int | None]:
    row = anchor.find_parent("tr")
    if not row:
        return (None, None, None)
    cells = [_clean(cell.get_text(" ", strip=True)) for cell in row.find_all(["td", "th"])]
    if len(cells) < 4:
        return (None, None, None)
    result: list[int | None] = []
    for token in cells[-3:]:
        result.append(int(token) if token.isdigit() else 0 if token in {"-", "–", "—"} else None)
    return tuple(result)  # type: ignore[return-value]


def parse_scenario_html(html: str, *, slug: str, url: str) -> dict[str, Any]:
    soup = _soup(html)
    heading = soup.find("h2")
    title = _clean(heading.get_text(" ", strip=True)) if heading else slug.replace("-", " ")
    cards: list[dict[str, Any]] = []
    seen: set[str] = set()

    for anchor in soup.select('a[href*="/LotR/Details/"]'):
        href = str(anchor.get("href") or "")
        card_slug = _slug_from_href(href, "/LotR/Details/")
        if not card_slug or card_slug in seen:
            continue
        card_title = _clean(anchor.get_text(" ", strip=True))
        if not card_title:
            continue
        normal, easy, nightmare = _row_counts(anchor)
        section = anchor.find_previous(["h3", "h4"])
        section_title = _clean(section.get_text(" ", strip=True)) if section else ""
        seen.add(card_slug)
        cards.append(
            {
                "slug": card_slug,
                "title": re.sub(r"^\d+\s*-\s*", "", card_title),
                "detail_url": urljoin(BASE_URL, href),
                "section": section_title,
                "normal_count": normal,
                "easy_count": easy,
                "nightmare_count": nightmare,
            }
        )

    # Hall of Beorn scenario pages can include the matching Nightmare encounter
    # set underneath the normal scenario.  The exact markup around the count
    # columns varies, so relying on Normal/Easy/Nightmare counts alone is not
    # sufficient.  For a normal scenario import, explicitly discard cards from
    # sections whose heading contains "Nightmare".  A deliberately requested
    # Nightmare scenario keeps those sections.
    importing_nightmare = "nightmare" in slug.lower()
    if not importing_nightmare:
        cards = [
            card
            for card in cards
            if "nightmare" not in str(card.get("section") or "").lower()
        ]

    # Counts are still useful as a second guard when Hall of Beorn exposes them.
    if not importing_nightmare and any(card["normal_count"] is not None for card in cards):
        cards = [card for card in cards if card["normal_count"] is None or card["normal_count"] > 0]

    return {"slug": slug, "title": title, "url": url, "cards": cards}


def _image_urls(soup: BeautifulSoup) -> list[str]:
    urls: list[str] = []
    for image in soup.select("img.card-image"):
        raw = image.get("data-src") or image.get("src")
        if not raw:
            continue
        absolute = urljoin(BASE_URL, str(raw))
        if absolute not in urls:
            urls.append(absolute)
    return urls


def _text_parts(scope: Tag | BeautifulSoup) -> dict[str, Any]:
    rules = [_clean(node.get_text(" ", strip=True)) for node in scope.select(".main-text")]
    shadow = [_clean(node.get_text(" ", strip=True)) for node in scope.select(".shadow-text")]
    flavor = [_clean(node.get_text(" ", strip=True)) for node in scope.select(".flavor-text")]
    traits = []
    for node in scope.select(".trait, .statTextBox a[title*='Trait'] i"):
        value = _clean(node.get_text(" ", strip=True)).rstrip(".")
        if value and value not in traits:
            traits.append(value)
    return {
        "rules": "\n\n".join([*rules, *shadow]),
        "flavor": "\n\n".join(flavor),
        "traits": traits,
    }


def _side_text_parts(soup: BeautifulSoup) -> dict[str, dict[str, Any]]:
    """Extract front/back text independently for double-sided HoB cards.

    Quest cards use ``card-text-wrapper`` containers rather than the
    ``statTextBox`` structure used by most vertical cards.  Flavor-only quest
    fronts are especially easy to lose if we only inspect statTextBox.
    """
    wrappers = [
        node for node in soup.select(".card-text-wrapper")
        if node.select_one(".main-text, .shadow-text, .flavor-text") is not None
    ]
    if wrappers:
        back = next((node for node in wrappers if "back-side" in (node.get("class") or [])), None)
        front = next((node for node in wrappers if node is not back), wrappers[0])
        return {
            "front": _text_parts(front),
            "back": _text_parts(back) if back is not None else {"rules": "", "flavor": "", "traits": []},
        }

    boxes = soup.select(".statTextBox")
    if len(boxes) >= 2:
        return {
            "front": _text_parts(boxes[0]),
            "back": _text_parts(boxes[1]),
        }
    front = _text_parts(boxes[0]) if boxes else _text_parts(soup)
    return {"front": front, "back": {"rules": "", "flavor": "", "traits": []}}


def _integer(value: str) -> int | None:
    match = re.search(r"-?\d+", value)
    return int(match.group()) if match else None


def _card_structure(soup: BeautifulSoup, card_type: str) -> dict[str, Any]:
    """Extract renderer-neutral LOTR fields from HoB's type-specific stat header."""
    type_box = soup.select_one(".statTypeBox")
    type_labels = []
    if type_box:
        for node in type_box.select("div[style*='font-weight:bold']"):
            label = _clean(node.get_text(" ", strip=True))
            if label and label not in type_labels:
                type_labels.append(label)

    primary_type = type_labels[0] if type_labels else card_type
    subtype = " ".join(type_labels[1:])
    encounter_link = soup.select_one('.statTypeBox a[href*="EncounterSet="]')
    encounter_set = _clean(str(encounter_link.get("title") or "")) if encounter_link else ""

    stats: dict[str, int] = {}
    values = soup.select_one(".statValueBox")
    if values:
        icon_names = {
            "threat": "threat",
            "attack": "attack",
            "defense": "defense",
            "heart": "hit_points",
            "hit points": "hit_points",
            "willpower": "willpower",
        }
        for image in values.select("img"):
            raw_name = _clean(str(image.get("title") or "")).casefold()
            if not raw_name:
                raw_name = Path(urlparse(str(image.get("src") or "")).path).stem.casefold()
                raw_name = raw_name.removesuffix("-med")
            field = icon_names.get(raw_name)
            if field is None:
                continue
            previous = image.find_previous("span")
            number = _integer(previous.get_text(" ", strip=True)) if previous else None
            if number is not None:
                stats[field] = number

        value_text = _clean(values.get_text(" ", strip=True))
        parenthesized = re.search(r"\(\s*(-?\d+)\s*\)", value_text)
        bracketed = re.search(r"\[\s*(-?\d+)\s*\]", value_text)
        normalized_type = primary_type.casefold()
        if "quest" in normalized_type:
            stage = re.search(r"(\d+)\s*A(?:\s*-\s*B)?", value_text, re.IGNORECASE)
            if stage:
                stats["quest_stage"] = int(stage.group(1))
            if parenthesized:
                stats["quest_points"] = int(parenthesized.group(1))
        elif "enemy" in normalized_type:
            if parenthesized:
                stats["engagement_cost"] = int(parenthesized.group(1))
        elif "location" in normalized_type:
            if parenthesized:
                stats["quest_points"] = int(parenthesized.group(1))
        elif bracketed:
            stats["cost"] = int(bracketed.group(1))

    return {
        "card_type": primary_type,
        "card_subtype": subtype,
        "encounter_set": encounter_set,
        "stats": stats,
    }


def parse_card_detail_html(html: str, *, url: str, language: str) -> dict[str, Any]:
    soup = _soup(html)
    title_box = soup.select_one(".titleNameBox")
    title = ""
    if title_box:
        for div in title_box.find_all("div", recursive=False):
            candidate = _clean(div.get_text(" ", strip=True))
            if candidate and not candidate.startswith("#"):
                title = candidate
                break
    if not title:
        heading = soup.find(["h1", "h2"])
        title = _clean(heading.get_text(" ", strip=True)) if heading else ""

    number = None
    quantity = None
    if title_box:
        match = re.search(r"#(\d+)\s*\(x(\d+)\)", title_box.get_text(" ", strip=True))
        if match:
            number, quantity = int(match.group(1)), int(match.group(2))

    card_type = ""
    type_box = soup.select_one(".statTypeBox")
    if type_box:
        # HoB uses slightly different nesting for different LotR card types.
        # Quest cards in particular do not always have the inner <div> shape
        # used by player cards. The statTypeBox itself contains only the type
        # label (plus an optional sphere image), so its text is the robust source.
        card_type = _clean(type_box.get_text(" ", strip=True))

    sphere = ""
    sphere_img = soup.select_one(".statTypeBox img")
    if sphere_img and sphere_img.get("src"):
        sphere = Path(urlparse(str(sphere_img["src"])).path).stem.replace("-med", "")

    set_name = ""
    set_link = soup.select_one('.titleNameBox a[href*="CardSet="]')
    if set_link:
        set_name = _clean(set_link.get_text(" ", strip=True))

    structure = _card_structure(soup, card_type)
    sides = _side_text_parts(soup)
    # Keep the historic flat fields as the front-side view for compatibility,
    # while exposing both sides explicitly for the translation workbench.
    parts = sides["front"]
    return {
        "url": url,
        "language": language.lower(),
        "title": title,
        "set_name": set_name,
        "number": number,
        "quantity": quantity,
        **structure,
        "sphere": sphere,
        "images": _image_urls(soup),
        "sides": sides,
        **parts,
    }


class HallOfBeornProvider:
    def __init__(self, *, timeout: float = 20.0) -> None:
        self.client = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        )

    def close(self) -> None:
        self.client.close()

    def _get_text(self, url: str) -> str:
        response = self.client.get(url)
        response.raise_for_status()
        return response.text

    def list_scenarios(self) -> list[dict[str, str]]:
        return parse_scenario_list_html(self._get_text(SCENARIOS_URL))

    def list_sets(self) -> list[dict[str, str]]:
        return parse_set_list_html(self._get_text(SEARCH_URL))

    def get_scenario(self, slug: str) -> dict[str, Any]:
        url = f"{BASE_URL}/LotR/Scenarios/{slug}"
        return parse_scenario_html(self._get_text(url), slug=slug, url=url)

    def get_scenario_printings(self, scenario_title: str) -> list[str]:
        query = urlencode({"Scenario": scenario_title, "Sort": "Set_Number"})
        return parse_scenario_printings_html(self._get_text(f"{SEARCH_URL}?{query}"))

    def get_scenario_usage(self, scenario_title: str) -> dict[str, dict[str, Any]]:
        query = urlencode({"Scenario": scenario_title, "Sort": "Set_Number"})
        return parse_scenario_usage_html(self._get_text(f"{SEARCH_URL}?{query}"))

    def get_scenario_related_cards(self, scenario_title: str) -> list[dict[str, str]]:
        query = urlencode({"Scenario": scenario_title, "Sort": "Set_Number"})
        return parse_scenario_related_html(self._get_text(f"{SEARCH_URL}?{query}"))

    def get_campaign_referenced_cards(self, campaign_card: dict[str, Any]) -> list[dict[str, Any]]:
        set_name = str(campaign_card.get("set_name") or "").strip()
        if not set_name:
            return []
        query = urlencode({"CardSet": set_name, "Sort": "Set_Number"})
        return parse_campaign_referenced_html(
            self._get_text(f"{SEARCH_URL}?{query}"),
            campaign_card=campaign_card,
        )

    def get_card(self, detail_url: str, *, language: str = "en") -> dict[str, Any]:
        separator = "&" if "?" in detail_url else "?"
        url = f"{detail_url}{separator}Lang={language.upper()}"
        return parse_card_detail_html(self._get_text(url), url=url, language=language)

    def download(self, url: str, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.client.stream("GET", url) as response:
            response.raise_for_status()
            with path.open("wb") as handle:
                for chunk in response.iter_bytes():
                    handle.write(chunk)
