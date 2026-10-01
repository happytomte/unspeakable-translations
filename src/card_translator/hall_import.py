from __future__ import annotations

import json
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from card_translator.library import initialize_scenario
from card_translator.ocr import extract_quest_flavor
from card_translator.providers.hall_of_beorn import HallOfBeornProvider


def _safe_name(value: str) -> str:
    value = re.sub(r"[\\/:*?\"<>|]+", "-", value).strip().strip(".")
    return re.sub(r"\s+", " ", value) or "card"


def _extension(url: str) -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    return suffix if suffix in {".jpg", ".jpeg", ".png", ".webp"} else ".jpg"


def _hall_card_identity_keys(card: dict[str, Any]) -> set[str]:
    """Return provider-stable identities available on a scenario/search entry."""
    identities: set[str] = set()
    for field in ("id", "card_id", "hall_of_beorn_id", "slug"):
        value = unquote(str(card.get(field) or "")).strip()
        if value:
            identities.add(f"id:{value.casefold()}")

    nested = card.get("identifiers")
    if isinstance(nested, dict):
        value = unquote(str(nested.get("hall_of_beorn") or "")).strip()
        if value:
            identities.add(f"id:{value.casefold()}")

    detail_url = str(card.get("detail_url") or "").strip()
    if detail_url:
        parsed = urlparse(detail_url)
        path = unquote(parsed.path).rstrip("/").casefold()
        if path:
            identities.add(f"url:{path}")
            marker = "/lotr/details/"
            if marker in path:
                identities.add(f"id:{path.split(marker, 1)[1]}")
    return identities


def _localized_reference_available(card: dict[str, Any], target_language: str) -> bool:
    needle = f"/Cards/{target_language.upper()}/"
    return any(needle in url for url in card.get("images", []))


def _card_type_has(card_type: str, kind: str) -> bool:
    return re.search(rf"\b{re.escape(kind)}\b", (card_type or "").casefold()) is not None


def _is_campaign_component_type(card_type: str) -> bool:
    return any(_card_type_has(card_type, kind) for kind in ("boon", "burden", "campaign"))



def _deck_role(card_type: str, section: str) -> str:
    normalized = (card_type or "").strip().lower()
    if _card_type_has(normalized, "quest") or section.strip().lower() == "quest cards":
        return "quest_deck"
    if _card_type_has(normalized, "campaign"):
        return "campaign"
    if _card_type_has(normalized, "boon") or _card_type_has(normalized, "burden"):
        return "campaign_component"
    return "encounter_deck"


def _scenario_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    def count_for(difficulty: str, *, quest: bool | None = None) -> int | None:
        values: list[int | None] = []
        for record in records:
            game_data = record.get("game_data", {})
            role = game_data.get("deck_role")
            is_quest = role == "quest_deck"
            if quest is True and not is_quest:
                continue
            if quest is False and role != "encounter_deck":
                continue
            value = game_data.get("quantity", {}).get(difficulty)
            values.append(value if isinstance(value, int) else None)
        if not values:
            return 0
        if any(value is None for value in values):
            return None
        return sum(value for value in values if value is not None)

    return {
        "unique_card_count": len(records),
        "image_count": sum(1 for record in records for side in ("front", "back") if record.get("images", {}).get(side)),
        "quest_cards": {difficulty: count_for(difficulty, quest=True) for difficulty in ("normal", "easy", "nightmare")},
        "encounter_pool": {difficulty: count_for(difficulty, quest=False) for difficulty in ("normal", "easy", "nightmare")},
        "campaign_component_count": sum(
            1
            for record in records
            if record.get("game_data", {}).get("deck_role") == "campaign_component"
        ),
    }


def _audit_card_payload(card: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(card, dict):
        return None
    sides = card.get("sides") if isinstance(card.get("sides"), dict) else {}
    return {
        "title": card.get("title"),
        "card_type": card.get("card_type"),
        "set_name": card.get("set_name"),
        "number": card.get("number"),
        "printed_quantity": card.get("quantity"),
        "image_count": len(card.get("images", [])) if isinstance(card.get("images"), list) else 0,
        "rules_length": len(str(card.get("rules") or "")),
        "flavor_length": len(str(card.get("flavor") or "")),
        "sides": {
            side: {
                "rules_length": len(str((sides.get(side) or {}).get("rules") or "")) if isinstance(sides.get(side), dict) else 0,
                "flavor_length": len(str((sides.get(side) or {}).get("flavor") or "")) if isinstance(sides.get(side), dict) else 0,
                "traits": (sides.get(side) or {}).get("traits", []) if isinstance(sides.get(side), dict) else [],
            }
            for side in ("front", "back")
        },
    }


def _write_audit(handle: Any, event: dict[str, Any]) -> None:
    event = {"at": datetime.now(timezone.utc).isoformat(), **event}
    handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    handle.flush()

def import_hall_of_beorn_scenario(
    root: Path,
    *,
    scenario_slug: str,
    target_language: str,
    provider: HallOfBeornProvider | None = None,
) -> Path:
    target_language = target_language.strip().lower()
    own_provider = provider is None
    provider = provider or HallOfBeornProvider()
    final_project_dir = root / "lotr" / target_language / scenario_slug
    if final_project_dir.exists() and (final_project_dir / "project.json").exists():
        return final_project_dir
    if final_project_dir.exists():
        raise ValueError(
            f"Destination already exists but is not an initialized project: {final_project_dir}"
        )

    # Build remote imports outside the visible library and publish them only when
    # every card, JSON file and project metadata file has been written.  This
    # prevents another browser tab from opening a half-imported project while the
    # POST request is still downloading cards.
    staging_root = root / ".imports" / f"{scenario_slug}-{uuid.uuid4().hex}"
    project_dir = staging_root / "lotr" / target_language / scenario_slug

    try:
        scenario = provider.get_scenario(scenario_slug)
        scenario_title = scenario.get("title") or scenario_slug.replace("-", " ")
        product_printings: list[str] = []
        get_printings = getattr(provider, "get_scenario_printings", None)
        if callable(get_printings):
            try:
                product_printings = list(get_printings(scenario_title))
            except Exception:
                product_printings = []

        scenario_usage: dict[str, dict[str, Any]] = {}
        get_usage = getattr(provider, "get_scenario_usage", None)
        if callable(get_usage):
            try:
                raw_usage = get_usage(scenario_title)
                if isinstance(raw_usage, dict):
                    scenario_usage = raw_usage
            except Exception:
                scenario_usage = {}
        project_dir.mkdir(parents=True, exist_ok=True)
        cards_dir = project_dir / "cards"
        cards_dir.mkdir(exist_ok=True)

        records: list[dict[str, Any]] = []
        fields_meta: dict[str, dict[str, Any]] = {}
        audit_path = project_dir / "import_log.jsonl"
        audit_handle = audit_path.open("w", encoding="utf-8")
        _write_audit(audit_handle, {
            "event": "scenario_import_started",
            "provider": "hall_of_beorn",
            "scenario": scenario_title,
            "scenario_slug": scenario_slug,
            "target_language": target_language.lower(),
            "scenario_table_card_count": len(scenario.get("cards", [])),
        })

        scenario_cards: list[dict[str, Any]] = []
        known_identities: set[str] = set()
        for scenario_card in scenario.get("cards", []):
            identities = _hall_card_identity_keys(scenario_card)
            duplicate_keys = sorted(identities & known_identities)
            if duplicate_keys:
                _write_audit(audit_handle, {
                    "event": "duplicate_card_skipped",
                    "discovered_via": "scenario_table",
                    "title": scenario_card.get("title"),
                    "identity_keys": duplicate_keys,
                })
                continue
            scenario_cards.append(scenario_card)
            known_identities.update(identities)
        known_titles = {str(card.get("title") or "").strip().casefold() for card in scenario_cards}
        related_discoveries: list[tuple[str, list[dict[str, Any]]]] = []
        get_related = getattr(provider, "get_scenario_related_cards", None)
        if callable(get_related):
            try:
                related = get_related(scenario_title)
            except Exception as exc:
                related = []
                _write_audit(audit_handle, {"event": "related_campaign_discovery_failed", "error": str(exc)})
            if isinstance(related, list):
                related_discoveries.append(("scenario_search", related))

        # HoB's Scenario filter omits some setup components. Resolve exact card
        # titles mentioned by campaign rules against cards from the same product.
        get_campaign_related = getattr(provider, "get_campaign_referenced_cards", None)
        if callable(get_campaign_related):
            for scenario_card in scenario_cards:
                label = " ".join(str(scenario_card.get(field) or "") for field in ("slug", "title"))
                if "campaign" not in label.casefold():
                    continue
                try:
                    campaign_card = provider.get_card(scenario_card["detail_url"], language="en")
                    scenario_card["_prefetched_english"] = campaign_card
                    if not _card_type_has(str(campaign_card.get("card_type") or ""), "campaign"):
                        continue
                    referenced = get_campaign_related(campaign_card)
                    if isinstance(referenced, list):
                        related_discoveries.append(("campaign_card_reference", referenced))
                        _write_audit(audit_handle, {
                            "event": "campaign_reference_discovery_completed",
                            "campaign_card": campaign_card.get("title"),
                            "campaign_card_url": campaign_card.get("url"),
                            "referenced_card_count": len(referenced),
                        })
                except Exception as exc:
                    _write_audit(audit_handle, {
                        "event": "campaign_reference_discovery_failed",
                        "campaign_entry": scenario_card.get("title"),
                        "error": str(exc),
                    })

        # Fetch only missing results and retain Boon/Burden/Campaign types.
        for discovery_method, related in related_discoveries:
            for candidate in related:
                candidate_title = str(candidate.get("title") or "").strip()
                candidate_identities = _hall_card_identity_keys(candidate)
                duplicate_keys = sorted(candidate_identities & known_identities)
                duplicate_title = candidate_title.casefold() in known_titles
                if not candidate_title:
                    continue
                if duplicate_keys or duplicate_title:
                    _write_audit(audit_handle, {
                        "event": "duplicate_card_skipped",
                        "discovered_via": discovery_method,
                        "title": candidate_title,
                        "identity_keys": duplicate_keys,
                        "matched_by": "provider_identity" if duplicate_keys else "title_fallback",
                    })
                    continue
                try:
                    candidate_card = provider.get_card(candidate["detail_url"], language="en")
                except Exception as exc:
                    _write_audit(audit_handle, {"event": "related_card_probe_failed", "title": candidate_title, "error": str(exc)})
                    continue
                candidate_type = " ".join(
                    str(candidate_card.get(field) or "")
                    for field in ("card_type", "card_subtype")
                ).strip().lower()
                if not _is_campaign_component_type(candidate_type):
                    continue
                scenario_cards.append({
                    "slug": candidate.get("slug"),
                    "title": candidate_title,
                    "detail_url": candidate.get("detail_url"),
                    "section": "Campaign Components",
                    "normal_count": 1,
                    "easy_count": 1,
                    "nightmare_count": 1,
                    "_prefetched_english": candidate_card,
                    "_discovered_via": discovery_method,
                    "_reference": candidate.get("reference"),
                })
                known_titles.add(candidate_title.casefold())
                known_identities.update(candidate_identities)
                _write_audit(audit_handle, {
                    "event": "related_campaign_component_added",
                    "title": candidate_title,
                    "card_type": candidate_card.get("card_type"),
                    "detail_url": candidate.get("detail_url"),
                    "discovered_via": discovery_method,
                    "reference": candidate.get("reference"),
                })

        for index, scenario_card in enumerate(scenario_cards, start=1):
            detail_url = scenario_card["detail_url"]
            english = scenario_card.pop("_prefetched_english", None) or provider.get_card(detail_url, language="en")
            localized = None
            if target_language.lower() != "en":
                try:
                    localized = provider.get_card(detail_url, language=target_language)
                except Exception:
                    localized = None

            _write_audit(audit_handle, {
                "event": "card_source_received",
                "scenario_entry": {k: v for k, v in scenario_card.items() if not str(k).startswith("_")},
                "discovered_via": scenario_card.get("_discovered_via", "scenario_table"),
                "english": _audit_card_payload(english),
                "localized": _audit_card_payload(localized),
            })

            title = english.get("title") or scenario_card.get("title") or scenario_card["slug"]
            key = f"{index:03d} - {_safe_name(str(title))}"
            local_images: list[str] = []
            for side_index, image_url in enumerate(english.get("images", []), start=1):
                suffix = _extension(image_url)
                filename = f"{key}-{side_index}{suffix}" if len(english.get("images", [])) > 1 else f"{key}{suffix}"
                destination = cards_dir / filename
                provider.download(image_url, destination)
                local_images.append(f"cards/{filename}")

            reference_images: list[str] = []
            reference_image_errors: list[dict[str, str]] = []
            if localized and _localized_reference_available(localized, target_language):
                for side_index, image_url in enumerate(localized.get("images", []), start=1):
                    suffix = _extension(image_url)
                    filename = f"{key}-{side_index}{suffix}" if len(localized.get("images", [])) > 1 else f"{key}{suffix}"
                    destination = project_dir / "reference" / target_language.lower() / filename
                    # Localized scans are a convenience/reference source only. Hall of
                    # Beorn can expose a language-specific image URL even when the S3
                    # object does not actually exist (403/404). That must never abort
                    # the scenario import.
                    try:
                        provider.download(image_url, destination)
                    except Exception as exc:
                        reference_image_errors.append({"url": image_url, "error": str(exc)})
                        continue
                    reference_images.append(str(destination.relative_to(project_dir)).replace("\\", "/"))

            english_sides = english.get("sides") if isinstance(english.get("sides"), dict) else {}
            localized_sides = localized.get("sides") if isinstance(localized, dict) and isinstance(localized.get("sides"), dict) else {}
            ocr_review_fields: dict[str, str] = {}
            if _card_type_has(str(english.get("card_type") or ""), "quest") and local_images:
                front = english_sides.get("front")
                if isinstance(front, dict) and not front.get("flavor") and front.get("rules"):
                    flavor = extract_quest_flavor(
                        project_dir / local_images[0],
                        known_rules=str(front.get("rules") or ""),
                    )
                    if flavor:
                        ocr_review_fields["front.flavor"] = flavor
                        _write_audit(audit_handle, {
                            "event": "card_field_flagged_by_ocr",
                            "card_slug": scenario_card.get("slug"),
                            "side": "front",
                            "field": "flavor",
                            "character_count": len(flavor),
                            "reason": "structured_source_field_empty",
                        })
            text: dict[str, Any] = {
                "en": {
                    "title": english.get("title", ""),
                    "traits": english.get("traits", []),
                    "rules": english.get("rules", ""),
                    "flavor": english.get("flavor", ""),
                    "sides": {
                        "front": {
                            "title": english.get("title", ""),
                            **(english_sides.get("front") if isinstance(english_sides.get("front"), dict) else {}),
                        },
                        "back": {
                            "title": english.get("title", "") if len(local_images) > 1 else "",
                            **(english_sides.get("back") if isinstance(english_sides.get("back"), dict) else {}),
                        },
                    },
                },
                target_language.lower(): {
                    "title": "",
                    "traits": [],
                    "rules": "",
                    "flavor": "",
                    "sides": {
                        "front": {"title": "", "traits": [], "rules": "", "flavor": ""},
                        "back": {"title": "", "traits": [], "rules": "", "flavor": ""},
                    },
                },
            }
            if localized:
                target = text[target_language.lower()]
                # Hall of Beorn sometimes localizes the scan/type while its searchable rules text
                # remains English. Only prefill text fields when they actually differ.
                if localized.get("title") and localized.get("title") != english.get("title"):
                    target["title"] = localized["title"]
                if localized.get("rules") and localized.get("rules") != english.get("rules"):
                    target["rules"] = localized["rules"]
                if localized.get("flavor") and localized.get("flavor") != english.get("flavor"):
                    target["flavor"] = localized["flavor"]
                if localized.get("traits") and localized.get("traits") != english.get("traits"):
                    target["traits"] = localized["traits"]
                for side_name in ("front", "back"):
                    source_side = english_sides.get(side_name) if isinstance(english_sides.get(side_name), dict) else {}
                    localized_side = localized_sides.get(side_name) if isinstance(localized_sides.get(side_name), dict) else {}
                    target_side = target["sides"][side_name]
                    if localized.get("title") and localized.get("title") != english.get("title"):
                        target_side["title"] = localized.get("title", "")
                    for field in ("rules", "flavor", "traits"):
                        localized_value = localized_side.get(field)
                        source_value = source_side.get(field)
                        if localized_value and localized_value != source_value:
                            target_side[field] = localized_value

            card_id = f"hall-of-beorn:{scenario_card['slug']}"
            usage = scenario_usage.get(str(scenario_card.get("slug") or ""), {})
            card_printings = usage.get("products") if isinstance(usage.get("products"), list) else product_printings
            normal_count = usage.get("normal") if isinstance(usage.get("normal"), int) else scenario_card.get("normal_count")
            easy_count = usage.get("easy") if isinstance(usage.get("easy"), int) else scenario_card.get("easy_count")
            record = {
                "id": card_id,
                "key": key,
                "card_type": english.get("card_type") or "",
                "game_data": {
                    "sphere": english.get("sphere") or "",
                    "card_subtype": english.get("card_subtype") or "",
                    "set_name": english.get("set_name") or scenario_card.get("section") or "",
                    "product_printings": card_printings,
                    "card_number": english.get("number"),
                    "printed_quantity": english.get("quantity"),
                    "quantity": {
                        "normal": normal_count,
                        "easy": easy_count,
                        "nightmare": scenario_card.get("nightmare_count"),
                    },
                    "scenario": scenario.get("title"),
                    "scenario_section": scenario_card.get("section") or "",
                    "encounter_set": english.get("encounter_set")
                    or (
                        ""
                        if (scenario_card.get("section") or "").strip().lower() == "quest cards"
                        else (scenario_card.get("section") or "")
                    ),
                    "deck_role": _deck_role(
                        " ".join(
                            str(english.get(field) or "")
                            for field in ("card_type", "card_subtype")
                        ),
                        scenario_card.get("section") or "",
                    ),
                    "traits": english.get("traits") or [],
                    "stats": english.get("stats") if isinstance(english.get("stats"), dict) else {},
                },
                "images": {
                    "front": local_images[0] if local_images else None,
                    "back": local_images[1] if len(local_images) > 1 else None,
                    "reference": {target_language.lower(): reference_images},
                },
                "text": text,
                "identifiers": {
                    "hall_of_beorn": scenario_card["slug"],
                },
                "source": {
                    "provider": "hall_of_beorn",
                    "detail_url": detail_url,
                    "discovered_via": scenario_card.get("_discovered_via", "scenario_table"),
                },
            }
            if scenario_card.get("_discovered_via"):
                record["source"]["discovered_via"] = scenario_card["_discovered_via"]
            if scenario_card.get("_reference"):
                record["source"]["reference"] = scenario_card["_reference"]
            if reference_image_errors:
                record["source"]["localized_reference_image_errors"] = reference_image_errors
            records.append(record)
            _write_audit(audit_handle, {
                "event": "card_canonical_written",
                "card_id": card_id,
                "key": key,
                "card_type": record["card_type"],
                "deck_role": record["game_data"]["deck_role"],
                "quantity": record["game_data"]["quantity"],
                "images": record["images"],
                "text": {
                    lang: {
                        "title": data.get("title", ""),
                        "rules_length": len(str(data.get("rules") or "")),
                        "flavor_length": len(str(data.get("flavor") or "")),
                        "sides": {
                            side: {
                                "rules_length": len(str((data.get("sides", {}).get(side, {}) or {}).get("rules") or "")),
                                "flavor_length": len(str((data.get("sides", {}).get(side, {}) or {}).get("flavor") or "")),
                            } for side in ("front", "back")
                        },
                    } for lang, data in text.items() if isinstance(data, dict)
                },
            })
            for field in ("title", "traits", "rules", "flavor"):
                fields_meta[f"{card_id}.text.en.{field}"] = {
                    "source": "hall_of_beorn",
                    "confidence": 1.0,
                }
            for side_name in ("front", "back"):
                side_data = text["en"]["sides"][side_name]
                for field in ("title", "traits", "rules", "flavor"):
                    fields_meta[f"{card_id}.text.en.sides.{side_name}.{field}"] = {
                        "source": "hall_of_beorn",
                        "confidence": 1.0 if side_data.get(field) else 0.0,
                    }
                    review_candidate = ocr_review_fields.get(f"{side_name}.{field}")
                    if review_candidate:
                        fields_meta[f"{card_id}.text.en.sides.{side_name}.{field}"].update({
                            "review_required": True,
                            "review_reason": "ocr_detected_text_missing_from_structured_source",
                            "ocr_candidate": review_candidate,
                            "ocr_confidence": 0.8,
                        })
            fields_meta[f"{card_id}.card_type"] = {
                "source": "hall_of_beorn",
                "confidence": 1.0 if record["card_type"] else 0.0,
            }
            for field in ("card_subtype", "encounter_set", "traits"):
                value = record["game_data"].get(field)
                fields_meta[f"{card_id}.game_data.{field}"] = {
                    "source": "hall_of_beorn",
                    "confidence": 1.0 if value else 0.0,
                }
            for field, value in record["game_data"]["stats"].items():
                fields_meta[f"{card_id}.game_data.stats.{field}"] = {
                    "source": "hall_of_beorn",
                    "confidence": 1.0 if value is not None else 0.0,
                }

        for record in records:
            for product in record.get("game_data", {}).get("product_printings", []):
                if isinstance(product, str) and product and product not in product_printings:
                    product_printings.append(product)

        _write_audit(audit_handle, {
            "event": "scenario_cards_collected",
            "record_count": len(records),
            "campaign_component_count": sum(1 for record in records if record.get("game_data", {}).get("deck_role") == "campaign_component"),
        })
        audit_handle.close()

        initialize_scenario(
            staging_root,
            game="lotr",
            slug=scenario_slug,
            title=scenario.get("title") or scenario_slug.replace("-", " "),
            translated_title="",
            author="Fantasy Flight Games",
            version="",
            source_url=scenario.get("url") or "",
            target_language=target_language,
            project_language=target_language,
            card_source="cards",
        )

        translation_path = project_dir / f"translation_{target_language.lower()}.json"
        translation = json.loads(translation_path.read_text(encoding="utf-8"))
        translation["cards"] = records
        meta = translation.setdefault("_meta", {})
        meta["fields"] = fields_meta
        set_names = sorted({
            str(record.get("game_data", {}).get("set_name") or "").strip()
            for record in records
            if str(record.get("game_data", {}).get("set_name") or "").strip()
        })
        summary = _scenario_summary(records)
        meta["external"] = {
            "hall_of_beorn": {
                "scenario_id": scenario_slug,
                "scenario_slug": scenario_slug,
                "scenario_url": scenario.get("url"),
                "set_names": set_names,
                "product_printings": product_printings,
                "imported_card_count": len(records),
                "summary": summary,
            }
        }

        project_path = project_dir / "project.json"
        project = json.loads(project_path.read_text(encoding="utf-8"))
        project["scenario_id"] = scenario_slug
        project["sets"] = set_names
        project["product_printings"] = product_printings
        project["scenario_summary"] = summary
        project["source_provider"] = "hall_of_beorn"
        project_path.write_text(
            json.dumps(project, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        translation_path.write_text(
            json.dumps(translation, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        final_project_dir.parent.mkdir(parents=True, exist_ok=True)
        project_dir.replace(final_project_dir)
        return final_project_dir
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)
        if own_provider:
            provider.close()
