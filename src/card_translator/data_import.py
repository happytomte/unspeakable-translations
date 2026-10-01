from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from uuid import uuid4

from card_translator.arkham_reference import arkham_data_files
from card_translator.games import get_game
from card_translator.library import get_scenario
from card_translator.shared_projects import (
    read_json,
    read_scenario_data,
    write_json,
    write_scenario_data,
)


def _value_at(value: Any, path: str) -> Any:
    current = value
    if not path or path == "$":
        return current
    for part in path.removeprefix("$.").split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return current


def _normalized_title(value: str) -> str:
    value = Path(value).stem.casefold()
    value = re.sub(r"(?:[-_]?[12])$", "", value)
    value = re.sub(r"^\s*\d+\s*[-_. ]+\s*", "", value)
    return "".join(character for character in value if character.isalnum())


def _as_traits(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in re.split(r"[,;.]", value) if item.strip()]
    return []


def _rows_from_payload(payload: Any, records_path: str) -> list[Any]:
    """Resolve configured records while accepting an array as the document root."""
    if isinstance(payload, list):
        return payload
    rows = _value_at(payload, records_path or "$")
    if not isinstance(rows, list):
        raise ValueError("Configured records path does not point to a JSON array")  # noqa: TRY004
    return rows


def _source_definition(game: dict[str, Any], source_id: str) -> dict[str, Any]:
    sources = game.get("imports")
    if not isinstance(sources, list):
        raise ValueError("Game defines no JSON data sources")  # noqa: TRY004
    source = next(
        (
            item
            for item in sources
            if isinstance(item, dict) and item.get("id") == source_id and not item.get("adapter")
        ),
        None,
    )
    if source is None:
        raise ValueError("Unknown or non-JSON data source")
    return source


def preview_filename_metadata(
    root: Path,
    *,
    game_id: str,
    scenario_slug: str,
    project_language: str,
    parser_pattern: str | None = None,
) -> dict[str, Any]:
    scenario = get_scenario(root, game_id, scenario_slug, language=project_language)
    if scenario is None or not scenario["initialized"]:
        raise ValueError("Scenario not found or not initialized")
    game = get_game(root, game_id)
    parser = game.get("filename_parser") if game else None
    if not isinstance(parser, dict):
        if parser_pattern is None:
            raise ValueError("No filename parser is configured for this game")
        parser = {}
    try:
        pattern_text = (
            str(parser.get("pattern") or "")
            if parser_pattern is None
            else parser_pattern.strip()
        )
        if not pattern_text:
            raise ValueError("Filename parser pattern is empty")
        pattern = re.compile(pattern_text)
    except re.error as exc:
        raise ValueError(f"Invalid filename parser: {exc}") from exc

    translation = read_scenario_data(scenario) or {}
    records = translation.get("cards") if isinstance(translation.get("cards"), list) else []
    namespace = str(parser.get("identifier_namespace") or "")
    source_language = str((scenario.get("project") or {}).get("source_language") or "en")
    id_owners = {
        str((record.get("identifiers") or {}).get(namespace)): str(record.get("key") or "")
        for record in records
        if isinstance(record, dict) and (record.get("identifiers") or {}).get(namespace)
    }
    records_by_key = {
        str(record.get("key") or ""): record for record in records if isinstance(record, dict)
    }
    rows: list[dict[str, Any]] = []
    for card in scenario["grouped_cards"]:
        matches = []
        for side in ("front", "back"):
            image = card.get(side)
            if not isinstance(image, dict):
                continue
            match = pattern.fullmatch(Path(str(image.get("name") or "")).stem)
            if match:
                matches.append((side, image, match.groupdict()))
        chosen = next(
            (item for item in matches if item[0] == "front"), matches[0] if matches else None
        )
        values = chosen[2] if chosen else {}
        card_key = str(card["key"])
        external_id = str(values.get("id") or "").strip()
        title = " ".join(str(values.get("title") or "").replace("_", " ").split())
        titles = {
            side: " ".join(str(groups.get("title") or "").replace("_", " ").split())
            for side, _, groups in matches
            if groups.get("title")
        }
        card_number = str(
            values.get("card_number") or values.get("number") or ""
        ).strip()
        conflicts: list[str] = []
        if matches:
            parsed_ids = {
                str(item[2].get("id") or "").strip() for item in matches if item[2].get("id")
            }
            if len(parsed_ids) > 1:
                conflicts.append("front/back IDs differ")
            parsed_numbers = {
                str(item[2].get("card_number") or item[2].get("number") or "").strip()
                for item in matches
                if item[2].get("card_number") or item[2].get("number")
            }
            if len(parsed_numbers) > 1:
                conflicts.append("front/back card numbers differ")
            for image_side, _, groups in matches:
                parsed_side = str(groups.get("side") or "").strip().casefold()
                expected = {"front": {"1", "front"}, "back": {"2", "back"}}[image_side]
                if parsed_side and parsed_side not in expected:
                    conflicts.append(f"{image_side} image declares side {parsed_side}")
        owner = id_owners.get(external_id)
        if external_id and owner and owner != card_key:
            conflicts.append(f"ID already belongs to {owner}")
        current_record = records_by_key.get(card_key, {})
        current_sides = (
            ((current_record.get("text") or {}).get(source_language) or {}).get("sides") or {}
        )
        current_titles = {
            side: str((current_sides.get(side) or {}).get("title") or "")
            for side in ("front", "back")
        }
        overwrites = [
            side
            for side, parsed_title in titles.items()
            if current_titles.get(side) and current_titles[side] != parsed_title
        ]
        rows.append(
            {
                "card_key": card_key,
                "filename": str(chosen[1].get("name") or "") if chosen else "",
                "matched": chosen is not None,
                "external_id": external_id,
                "title": title,
                "titles": titles,
                "side_rows": [
                    {
                        "side": side,
                        "filename": str(image.get("name") or ""),
                        "title": " ".join(
                            str(groups.get("title") or "").replace("_", " ").split()
                        ),
                        "external_id": str(groups.get("id") or "").strip(),
                        "card_number": str(
                            groups.get("card_number") or groups.get("number") or ""
                        ).strip(),
                    }
                    for side, image, groups in matches
                ],
                "card_number": card_number,
                "side": str(values.get("side") or "").strip(),
                "conflicts": conflicts,
                "current_title": current_titles.get("front") or current_titles.get("back") or "",
                "will_overwrite_title": bool(overwrites),
            }
        )
    ids = [row["external_id"] for row in rows if row["external_id"]]
    duplicate_ids = sorted({value for value in ids if ids.count(value) > 1})
    return {
        "namespace": namespace,
        "pattern": pattern_text,
        "total": len(rows),
        "matched": sum(bool(row["matched"]) for row in rows),
        "duplicate_ids": duplicate_ids,
        "conflicts": sum(bool(row["conflicts"]) for row in rows),
        "overwrites": sum(bool(row["will_overwrite_title"]) for row in rows),
        "rows": rows,
    }


def apply_filename_metadata(
    root: Path,
    *,
    game_id: str,
    scenario_slug: str,
    project_language: str,
    parser_pattern: str | None = None,
) -> dict[str, Any]:
    preview = preview_filename_metadata(
        root,
        game_id=game_id,
        scenario_slug=scenario_slug,
        project_language=project_language,
        parser_pattern=parser_pattern,
    )
    if preview["duplicate_ids"] or preview["conflicts"]:
        raise ValueError("Filename parser preview contains conflicts; resolve them before applying")
    scenario = get_scenario(root, game_id, scenario_slug, language=project_language)
    if scenario is None:
        raise ValueError("Scenario not found")
    data = read_scenario_data(scenario) or {}
    records = data.setdefault("cards", [])
    if not isinstance(records, list):
        raise ValueError("Translation contains invalid card records")  # noqa: TRY004
    source_language = str((scenario.get("project") or {}).get("source_language") or "en")
    cards_by_key = {str(card["key"]): card for card in scenario["grouped_cards"]}
    applied = 0
    for row in preview["rows"]:
        if not row["matched"]:
            continue
        card_key = row["card_key"]
        logical = cards_by_key[card_key]
        record = next(
            (
                item
                for item in records
                if isinstance(item, dict) and str(item.get("key") or "") == card_key
            ),
            None,
        )
        if record is None:
            record = {
                "id": card_key,
                "key": card_key,
                "images": {
                    side: f"{scenario['card_source']}/{logical[side]['name']}"
                    for side in ("front", "back")
                    if isinstance(logical.get(side), dict)
                },
                "identifiers": {},
                "text": {},
                "game_data": {},
            }
            records.append(record)
        if row["external_id"]:
            record.setdefault("identifiers", {})[preview["namespace"]] = row["external_id"]
        for side, parsed_title in row["titles"].items():
            record.setdefault("text", {}).setdefault(source_language, {}).setdefault(
                "sides", {}
            ).setdefault(side, {})["title"] = parsed_title
            if side == "front":
                record["text"][source_language]["title"] = parsed_title
        if row["card_number"]:
            value: int | str = row["card_number"]
            if str(value).isdigit():
                value = int(value)
            record.setdefault("game_data", {})["card_number"] = value
        applied += 1
    data.setdefault("_meta", {})["filename_parser"] = {
        "applied_at": datetime.now(UTC).isoformat(),
        "matched": applied,
    }
    write_scenario_data(scenario, data)
    return {**preview, "applied": applied}


def _record_from_source(
    raw: dict[str, Any],
    *,
    source: dict[str, Any],
    source_language: str,
    field_types: dict[str, str],
    index: int,
) -> dict[str, Any]:
    namespace = str(source.get("identifier_namespace") or source["id"])
    identifier_path = str(source.get("identifier") or "id")
    external_id = str(_value_at(raw, identifier_path) or "").strip()
    fields = source.get("fields") if isinstance(source.get("fields"), dict) else {}

    mapped = {internal: _value_at(raw, str(external)) for internal, external in fields.items()}
    sides: dict[str, dict[str, Any]] = {"front": {}}
    for internal, value in mapped.items():
        if internal.startswith("game_data.") or internal == "card_type":
            continue
        side_name = "front"
        field_name = internal
        if internal.startswith("sides.front."):
            field_name = internal.removeprefix("sides.front.")
        elif internal.startswith("sides.back."):
            side_name = "back"
            field_name = internal.removeprefix("sides.back.")
        side = sides.setdefault(side_name, {})
        if field_types.get(field_name) == "list":
            side[field_name] = _as_traits(value)
        else:
            side[field_name] = str(value or "").strip()
    if "back" in sides and not any(sides["back"].values()):
        del sides["back"]
    game_data: dict[str, Any] = {}
    for internal, value in mapped.items():
        if internal.startswith("game_data."):
            game_data[internal.removeprefix("game_data.")] = value
    if source.get("id") == "arkham-json" and not game_data.get("classes"):
        classes = []
        for field in ("faction_code", "faction2_code", "faction3_code"):
            value = str(raw.get(field) or "").strip().casefold()
            if value and value not in classes:
                classes.append(value)
        if classes:
            game_data["classes"] = classes

    identity = external_id or f"row-{index}"
    return {
        "id": f"{namespace}:{identity}",
        "key": f"import:{namespace}:{identity}",
        "card_type": str(mapped.get("card_type") or "").strip(),
        "images": {"front": None, "back": None},
        "identifiers": {namespace: external_id} if external_id else {},
        "text": {source_language: {"sides": sides}},
        "game_data": game_data,
        "source": {"provider": source["id"], "import_row": index},
    }


def _merge_imported_record(
    incoming: dict[str, Any], existing: dict[str, Any], *, source_language: str
) -> dict[str, Any]:
    incoming["id"] = existing.get("id", incoming["id"])
    incoming["key"] = existing.get("key", incoming["key"])
    incoming["images"] = existing.get("images", incoming["images"])
    incoming_identifiers = incoming.setdefault("identifiers", {})
    for namespace, value in (existing.get("identifiers") or {}).items():
        incoming_identifiers.setdefault(namespace, value)

    incoming_text = incoming.setdefault("text", {})
    existing_text = existing.get("text") if isinstance(existing.get("text"), dict) else {}
    for language, localized in existing_text.items():
        if language != source_language:
            incoming_text[language] = localized
            continue
        if not isinstance(localized, dict):
            continue
        imported_localized = incoming_text.setdefault(language, {})
        for field, value in localized.items():
            if field == "sides" or field in imported_localized:
                continue
            imported_localized[field] = value
        existing_sides = localized.get("sides") if isinstance(localized.get("sides"), dict) else {}
        imported_sides = imported_localized.setdefault("sides", {})
        for side, side_values in existing_sides.items():
            if not isinstance(side_values, dict):
                continue
            imported_side = imported_sides.setdefault(side, {})
            for field, value in side_values.items():
                imported_side.setdefault(field, value)
    return incoming


def import_card_data(
    root: Path,
    *,
    game_id: str,
    scenario_slug: str,
    project_language: str,
    source_id: str,
    filename: str,
    content: bytes,
) -> dict[str, Any]:
    scenario = get_scenario(root, game_id, scenario_slug, language=project_language)
    if scenario is None or not scenario["initialized"]:
        raise ValueError("Scenario not found or not initialized")
    game = get_game(root, game_id)
    if game is None:
        raise ValueError("Unknown game")
    source = _source_definition(game, source_id)

    try:
        payload = json.loads(content.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Uploaded file is not valid UTF-8 JSON") from exc
    rows = _rows_from_payload(payload, str(source.get("records_path") or "$"))

    translation = read_scenario_data(scenario) or {}
    records = translation.setdefault("cards", [])
    if not isinstance(records, list):
        raise ValueError("Translation contains invalid card records")  # noqa: TRY004

    source_language = str((scenario.get("project") or {}).get("source_language") or "en")
    namespace = str(source.get("identifier_namespace") or source_id)
    schema = game.get("card_schema") if isinstance(game.get("card_schema"), dict) else {}
    schema_fields = schema.get("translatable_fields") if isinstance(schema, dict) else []
    field_types = {
        str(field.get("id")): str(field.get("type") or "text")
        for field in schema_fields
        if isinstance(field, dict) and field.get("id")
    }
    existing_by_id = {
        str(record.get("identifiers", {}).get(namespace)): record
        for record in records
        if isinstance(record, dict) and record.get("identifiers", {}).get(namespace)
    }
    local_by_title: dict[str, list[dict[str, Any]]] = {}
    for card in scenario["grouped_cards"]:
        names = {str(card["key"]), str(card.get("display_name") or "")}
        for name in names:
            normalized = _normalized_title(name)
            if normalized:
                local_by_title.setdefault(normalized, []).append(card)

    result = {"total": 0, "matched_by_id": 0, "matched_by_name": 0, "unmatched": 0}
    payload_ids: set[str] = set()
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            continue
        incoming = _record_from_source(
            row,
            source=source,
            source_language=source_language,
            field_types=field_types,
            index=index,
        )
        result["total"] += 1
        external_id = str(incoming.get("identifiers", {}).get(namespace) or "")
        if external_id and external_id in payload_ids:
            raise ValueError(f"Duplicate external ID in JSON data: {external_id}")
        payload_ids.add(external_id)
        current = existing_by_id.get(external_id) if external_id else None
        if current is not None:
            _merge_imported_record(incoming, current, source_language=source_language)
            records[records.index(current)] = incoming
            existing_by_id[external_id] = incoming
            result["matched_by_id"] += 1
            continue

        title = incoming["text"][source_language]["sides"]["front"]["title"]
        candidates = local_by_title.get(_normalized_title(title), []) if title else []
        if len(candidates) == 1:
            card = candidates[0]
            incoming["key"] = card["key"]
            incoming["images"] = {
                side: (
                    f"{scenario['card_source']}/{card[side]['name']}" if card.get(side) else None
                )
                for side in ("front", "back")
            }
            existing_local = next(
                (
                    record
                    for record in records
                    if isinstance(record, dict) and record.get("key") == card["key"]
                ),
                None,
            )
            if existing_local is not None:
                _merge_imported_record(incoming, existing_local, source_language=source_language)
                records[records.index(existing_local)] = incoming
                if external_id:
                    existing_by_id[external_id] = incoming
                result["matched_by_name"] += 1
                continue
            result["matched_by_name"] += 1
        else:
            result["unmatched"] += 1
        records.append(incoming)
        if external_id:
            existing_by_id[external_id] = incoming

    snapshots = scenario["path"] / "snapshots"
    snapshots.mkdir(exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", Path(filename).name) or "cards.json"
    snapshot_name = f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}-{safe_name}"
    (snapshots / snapshot_name).write_bytes(content)

    meta = translation.setdefault("_meta", {})
    imports = meta.setdefault("imports", [])
    imports.append(
        {
            "source": source_id,
            "snapshot": f"snapshots/{snapshot_name}",
            "imported_at": datetime.now(UTC).isoformat(),
            **result,
        }
    )
    write_scenario_data(scenario, translation)
    return {**result, "snapshot": f"snapshots/{snapshot_name}"}


def import_arkhamdb_scenario_data(
    root: Path,
    *,
    scenario_slug: str,
    project_language: str,
) -> dict[str, Any]:
    """Fill an Arkham scenario from locally downloaded source/target databases by code."""
    scenario = get_scenario(root, "ah", scenario_slug, language=project_language)
    if scenario is None or not scenario["initialized"]:
        raise ValueError("Scenario not found or not initialized")
    game = get_game(root, "ah")
    if game is None:
        raise ValueError("Arkham game system is not configured")
    source = _source_definition(game, "arkham-json")
    parser = game.get("filename_parser")
    if isinstance(parser, dict) and parser.get("pattern"):
        apply_filename_metadata(
            root,
            game_id="ah",
            scenario_slug=scenario_slug,
            project_language=project_language,
        )
        scenario = get_scenario(root, "ah", scenario_slug, language=project_language)
        if scenario is None:
            raise ValueError("Scenario not found after filename metadata was applied")
    source_language = str((scenario.get("project") or {}).get("source_language") or "en")
    target_language = str(scenario.get("target_language") or project_language)
    files = arkham_data_files(root / "ah" / "data")
    source_path = files.get(source_language)
    if source_path is None:
        raise ValueError(f"Missing ArkhamDB source data for {source_language.upper()}")

    def records_by_code(path: Path | None) -> dict[str, dict[str, Any]]:
        if path is None:
            return {}
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Invalid ArkhamDB data file: {path.name}") from exc
        return {
            str(row.get("code")): row
            for row in rows
            if isinstance(row, dict) and row.get("code")
        } if isinstance(rows, list) else {}

    source_rows = records_by_code(source_path)
    target_rows = records_by_code(files.get(target_language))
    source_codes_by_title: dict[str, set[str]] = {}
    for code, row in source_rows.items():
        for value in (row.get("name"), row.get("back_name")):
            normalized = _normalized_title(str(value or ""))
            if normalized:
                source_codes_by_title.setdefault(normalized, set()).add(code)
    schema = game.get("card_schema") if isinstance(game.get("card_schema"), dict) else {}
    schema_fields = schema.get("translatable_fields") if isinstance(schema, dict) else []
    field_types = {
        str(field.get("id")): str(field.get("type") or "text")
        for field in schema_fields
        if isinstance(field, dict) and field.get("id")
    }
    translation = read_scenario_data(scenario) or {}
    records = translation.setdefault("cards", [])
    namespace = str(source.get("identifier_namespace") or "arkham-json")
    records_by_key = {
        str(record.get("key") or ""): record
        for record in records
        if isinstance(record, dict)
    }
    matched = source_imported = target_imported = 0
    missing_source: list[str] = []
    missing_target: list[str] = []

    def merge_localized(record: dict[str, Any], language: str, incoming: dict[str, Any]) -> bool:
        localized = record.setdefault("text", {}).setdefault(language, {})
        incoming_sides = incoming.get("sides") if isinstance(incoming.get("sides"), dict) else {}
        changed = False
        for side_name, fields in incoming_sides.items():
            if not isinstance(fields, dict):
                continue
            destination = localized.setdefault("sides", {}).setdefault(side_name, {})
            for field, value in fields.items():
                if value is None or value == "" or value == []:
                    continue
                destination[field] = value
                if side_name == "front":
                    localized[field] = value
                changed = True
        return changed

    for card in scenario["grouped_cards"]:
        record = records_by_key.get(str(card["key"]))
        external_id = str(((record or {}).get("identifiers") or {}).get(namespace) or "")
        if not external_id:
            candidate_codes: set[str] = set()
            source_sides = card.get("source_sides") or {}
            front_source = source_sides.get("front") if isinstance(source_sides, dict) else {}
            for value in (
                card.get("key"),
                card.get("display_name"),
                front_source.get("title") if isinstance(front_source, dict) else "",
            ):
                normalized = _normalized_title(str(value or ""))
                candidate_codes.update(source_codes_by_title.get(normalized, set()))
            if len(candidate_codes) == 1:
                external_id = candidate_codes.pop()
                if record is None:
                    record = {
                        "id": str(card["key"]),
                        "key": str(card["key"]),
                        "images": {
                            side: f"{scenario['card_source']}/{card[side]['name']}"
                            for side in ("front", "back")
                            if isinstance(card.get(side), dict)
                        },
                        "identifiers": {},
                        "text": {},
                        "game_data": {},
                    }
                    records.append(record)
                    records_by_key[str(card["key"])] = record
                record.setdefault("identifiers", {})[namespace] = external_id
        if not external_id:
            continue
        matched += 1
        source_raw = source_rows.get(external_id)
        if source_raw is None:
            missing_source.append(external_id)
            continue
        if record is None:
            continue
        source_record = _record_from_source(
            source_raw,
            source=source,
            source_language=source_language,
            field_types=field_types,
            index=matched,
        )
        merge_localized(record, source_language, source_record["text"][source_language])
        record["card_type"] = source_record.get("card_type") or record.get("card_type", "")
        existing_game_data = record.setdefault("game_data", {})
        for key, value in source_record.get("game_data", {}).items():
            if value is not None and value != "":
                existing_game_data[key] = value
        record.setdefault("source", {}).update(
            {"provider": "arkham-json", "source_file": source_path.name}
        )
        source_imported += 1

        if target_language == source_language:
            continue
        target_raw = target_rows.get(external_id)
        if target_raw is None:
            missing_target.append(external_id)
            continue
        target_record = _record_from_source(
            target_raw,
            source=source,
            source_language=target_language,
            field_types=field_types,
            index=matched,
        )
        if merge_localized(record, target_language, target_record["text"][target_language]):
            target_imported += 1

    translation.setdefault("_meta", {})["arkhamdb_import"] = {
        "imported_at": datetime.now(UTC).isoformat(),
        "source_language": source_language,
        "target_language": target_language,
        "source_file": source_path.name,
        "target_file": files.get(target_language).name if files.get(target_language) else None,
        "matched_ids": matched,
        "source_imported": source_imported,
        "target_imported": target_imported,
        "missing_source": sorted(set(missing_source)),
        "missing_target": sorted(set(missing_target)),
    }
    write_scenario_data(scenario, translation)
    return translation["_meta"]["arkhamdb_import"]


_SHOGGOTH_TEXT_TAGS = {"b", "i", "bi", "blockquote"}
_SHOGGOTH_TOKEN_ALIASES = {
    "for": "forced",
    "obj": "objective",
    "per": "per_investigator",
    "rev": "revelation",
}
_SHOGGOTH_FACE_DATA_FIELDS = {
    "type", "name", "subtitle", "traits", "text", "flavor_text",
    "illustrator", "connection", "connections", "shroud", "doom", "stage", "index",
    "health", "fight", "combat", "damage", "horror", "evade", "clues", "victory",
}
_SHOGGOTH_CARD_DATA_FIELDS = {"id", "name", "front", "back", "amount"}


def _from_shoggoth_markup(value: Any) -> str:
    text = str(value or "")

    def replace(match: re.Match[str]) -> str:
        closing, tag = match.groups()
        normalized = tag.casefold()
        if closing or normalized in _SHOGGOTH_TEXT_TAGS:
            return match.group(0)
        return "{" + _SHOGGOTH_TOKEN_ALIASES.get(normalized, normalized) + "}"

    return re.sub(r"<(/?)([a-z][a-z0-9_+-]*)>", replace, text, flags=re.IGNORECASE)


def _shoggoth_rules(face: dict[str, Any]) -> str:
    entries = face.get("entries")
    if isinstance(entries, list):
        lines = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            tokens = entry.get("token")
            if not isinstance(tokens, list):
                tokens = [tokens] if tokens else []
            prefix = "".join("{" + str(token) + "}" for token in tokens)
            text = _from_shoggoth_markup(entry.get("text"))
            lines.append(f"{prefix}: {text}".rstrip())
        if lines:
            return "\n".join(lines)
    return _from_shoggoth_markup(face.get("text"))


def _shoggoth_image_name(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return Path(value.replace("\\", "/")).name


def _shoggoth_face_overrides(face: dict[str, Any]) -> dict[str, Any]:
    overrides = {
        key: value
        for key, value in face.items()
        if key not in _SHOGGOTH_FACE_DATA_FIELDS
    }
    entries = face.get("entries")
    if isinstance(entries, list) and all(isinstance(entry, dict) for entry in entries):
        overrides.pop("entries", None)
    return overrides


def import_shoggoth_data(
    root: Path,
    *,
    game_id: str,
    scenario_slug: str,
    project_language: str,
    filename: str,
    content: bytes,
) -> dict[str, Any]:
    """Import card data and reusable face overrides from a Shoggoth project JSON."""
    if game_id != "ah":
        raise ValueError("Shoggoth import is currently available only for Arkham Horror")
    scenario = get_scenario(root, game_id, scenario_slug, language=project_language)
    if scenario is None or not scenario["initialized"]:
        raise ValueError("Scenario not found or not initialized")
    try:
        payload = json.loads(content.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Uploaded file is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("cards"), list):
        raise ValueError("Shoggoth project must contain a cards array")  # noqa: TRY004

    data = read_scenario_data(scenario) or {}
    records = data.setdefault("cards", [])
    if not isinstance(records, list):
        raise ValueError("Scenario contains invalid card records")  # noqa: TRY004
    source_language = str((scenario.get("project") or {}).get("source_language") or "en")
    text_language = (
        source_language if scenario.get("source_only") else str(scenario["target_language"])
    )
    records_by_key = {
        str(record.get("key") or ""): record
        for record in records
        if isinstance(record, dict) and record.get("key")
    }
    records_by_shoggoth_id = {
        str((record.get("identifiers") or {}).get("shoggoth") or "").casefold(): record
        for record in records
        if isinstance(record, dict)
        and isinstance(record.get("identifiers"), dict)
        and (record.get("identifiers") or {}).get("shoggoth")
    }
    local_by_image: dict[str, dict[str, Any]] = {}
    local_shoggoth_cards: list[dict[str, Any]] = []
    local_by_title: dict[str, list[dict[str, Any]]] = {}
    for card in scenario.get("grouped_cards", []):
        for side in ("front", "back"):
            image = card.get(side)
            if isinstance(image, dict) and image.get("name"):
                image_name = str(image["name"])
                local_by_image[image_name.casefold()] = card
                if re.search(r"_(?:front|back)_\d+\.[^.]+$", image_name, re.IGNORECASE):
                    local_shoggoth_cards.append(card)
        for title in (card.get("key"), card.get("display_name")):
            normalized = _normalized_title(str(title or ""))
            if normalized:
                local_by_title.setdefault(normalized, []).append(card)

    result = {"total": 0, "matched_by_image": 0, "matched_by_name": 0, "unmatched": 0}
    for index, item in enumerate(payload["cards"], start=1):
        if not isinstance(item, dict):
            continue
        result["total"] += 1
        faces = {
            side: value
            for side in ("front", "back")
            if isinstance((value := item.get(side)), dict)
        }
        item_id = str(item.get("id") or "").casefold()
        local_card = next(
            (
                card
                for card in local_shoggoth_cards
                if any(
                    isinstance(image, dict)
                    and str(image.get("name") or "").casefold().startswith(f"{item_id}_")
                    for image in (card.get("front"), card.get("back"))
                )
            ),
            None,
        ) if item_id else None
        if local_card is not None:
            result["matched_by_image"] += 1
        else:
            for face in faces.values():
                image_name = _shoggoth_image_name(face.get("illustration"))
                if image_name and image_name.casefold() in local_by_image:
                    local_card = local_by_image[image_name.casefold()]
                    result["matched_by_image"] += 1
                    break
        if local_card is None:
            title = str(
                ((faces.get("front") or {}).get("name")) or item.get("name") or ""
            )
            candidates = local_by_title.get(_normalized_title(title), []) if title else []
            if len(candidates) == 1:
                local_card = candidates[0]
                result["matched_by_name"] += 1

        fallback = str(item.get("id") or item.get("name") or f"shoggoth-{index}")
        card_key = str(local_card["key"]) if local_card else fallback
        record = records_by_key.get(card_key)
        if record is None and item_id:
            record = records_by_shoggoth_id.get(item_id)
        if record is not None and local_card and record.get("key") != card_key:
            previous_key = str(record.get("key") or "")
            record["id"] = card_key
            record["key"] = card_key
            if previous_key:
                records_by_key.pop(previous_key, None)
            records_by_key[card_key] = record
        if record is None:
            record = {"id": card_key, "key": card_key, "text": {}, "game_data": {}}
            records.append(record)
            records_by_key[card_key] = record
        record.setdefault("identifiers", {})["shoggoth"] = str(item.get("id") or fallback)
        records_by_shoggoth_id[str(item.get("id") or fallback).casefold()] = record
        images = record.setdefault("images", {})
        localized = record.setdefault("text", {}).setdefault(text_language, {})
        localized_sides = localized.setdefault("sides", {})
        game_data = record.setdefault("game_data", {})
        shoggoth = record.setdefault("shoggoth", {})
        shoggoth["card"] = {
            key: value
            for key, value in item.items()
            if key not in _SHOGGOTH_CARD_DATA_FIELDS
        }
        shoggoth_sides = shoggoth.setdefault("sides", {})

        front = faces.get("front") or {}
        raw_type = str(front.get("type") or record.get("card_type") or "story")
        record["card_type"] = {
            "weakness_treachery": "weakness",
            "weakness_enemy": "weakness",
        }.get(raw_type, raw_type.removesuffix("_back"))
        for side, face in faces.items():
            image_name = _shoggoth_image_name(face.get("illustration"))
            local_side = local_card.get(side) if local_card else None
            if isinstance(local_side, dict) and local_side.get("name"):
                images[side] = f"{scenario['card_source']}/{local_side['name']}"
            elif image_name:
                images[side] = f"{scenario['card_source']}/{image_name}"

            text_values = {
                "title": _from_shoggoth_markup(face.get("name")),
                "subname": _from_shoggoth_markup(face.get("subtitle")),
                "traits": _as_traits(_from_shoggoth_markup(face.get("traits"))),
                "rules": _shoggoth_rules(face),
                "flavor": _from_shoggoth_markup(face.get("flavor_text")),
            }
            text_values = {
                key: value
                for key, value in text_values.items()
                if value != "" and value != []
            }
            localized_sides.setdefault(side, {}).update(text_values)
            if side == "front":
                localized.update(text_values)

            side_data = game_data if side == "front" else game_data.setdefault(
                "sides", {}
            ).setdefault("back", {})
            if side == "back" and face.get("type"):
                side_data["card_type"] = str(face["type"])
            for field in (
                "connection", "connections", "shroud", "doom", "stage", "index",
                "health", "sanity", "damage", "horror", "evade", "cost",
            ):
                if field in face:
                    side_data[field] = face[field]
            if "attack" in face:
                side_data["fight"] = face["attack"]
            elif raw_type in {"enemy", "enemy_location", "weakness_enemy"} and "combat" in face:
                side_data["fight"] = face["combat"]
            if raw_type == "investigator":
                for field in ("willpower", "intellect", "combat", "agility"):
                    if field in face:
                        side_data[field] = face[field]
            classes = face.get("classes")
            if isinstance(classes, list):
                side_data["classes"] = [str(item) for item in classes if str(item).strip()]
            slots = face.get("slots")
            if isinstance(slots, list):
                side_data["slot"] = ", ".join(
                    str(item) for item in slots if str(item).strip()
                )
            level = face.get("level")
            if str(level).strip().lstrip("-").isdigit():
                side_data["xp"] = int(level)
            icons = str(face.get("icons") or "").upper()
            for field, positive, negative in (
                ("willpower", "W", "V"),
                ("intellect", "I", "H"),
                ("combat", "C", "B"),
                ("agility", "A", "Z"),
                ("wild", "Q", "P"),
            ):
                count = icons.count(positive) - icons.count(negative)
                if count:
                    side_data[field] = count
            clues = str(face.get("clues") or "")
            if clues:
                side_data["clues"] = clues.strip()
            victory = str(face.get("victory") or "")
            victory_match = re.search(r"(-?\d+)", victory)
            if victory_match:
                field = "vengeance" if "vengeance" in victory.casefold() else "victory"
                side_data[field] = victory_match.group(1)
            illustrator = str(face.get("illustrator") or "").strip()
            if illustrator:
                side_data["artist"] = re.sub(
                    r"^Illus\.\s*", "", illustrator, flags=re.IGNORECASE
                )
            shoggoth_sides[side] = _shoggoth_face_overrides(face)
        if item.get("amount") not in {None, ""}:
            try:
                game_data["printed_quantity"] = int(item["amount"])
            except (TypeError, ValueError):
                pass
        for field in ("copyright", "project_number", "encounter_number"):
            if item.get(field) not in {None, ""}:
                game_data[field] = item[field]
        if local_card is None:
            result["unmatched"] += 1

    snapshots = scenario["path"] / "snapshots"
    snapshots.mkdir(exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", Path(filename).name) or "shoggoth.json"
    snapshot_name = f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}-{safe_name}"
    (snapshots / snapshot_name).write_bytes(content)
    result["snapshot"] = f"snapshots/{snapshot_name}"
    project_path = scenario["path"] / "project.json"
    project = read_json(project_path) or {}
    project["shoggoth_source"] = {
        key: value for key, value in payload.items() if key != "cards"
    }
    write_json(project_path, project)
    data.setdefault("_meta", {})["shoggoth_import"] = {
        "imported_at": datetime.now(UTC).isoformat(),
        **result,
    }
    write_scenario_data(scenario, data)
    return result


def preview_card_data(
    root: Path,
    *,
    game_id: str,
    scenario_slug: str,
    project_language: str,
    source_id: str,
    content: bytes,
) -> dict[str, Any]:
    scenario = get_scenario(root, game_id, scenario_slug, language=project_language)
    if scenario is None or not scenario["initialized"]:
        raise ValueError("Scenario not found or not initialized")
    game = get_game(root, game_id)
    if game is None:
        raise ValueError("Unknown game")
    source = _source_definition(game, source_id)
    try:
        payload = json.loads(content.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Uploaded file is not valid UTF-8 JSON") from exc
    rows = _rows_from_payload(payload, str(source.get("records_path") or "$"))

    translation = read_scenario_data(scenario) or {}
    records = translation.get("cards") if isinstance(translation.get("cards"), list) else []
    namespace = str(source.get("identifier_namespace") or source_id)
    existing_ids = {
        str((record.get("identifiers") or {}).get(namespace))
        for record in records
        if isinstance(record, dict) and (record.get("identifiers") or {}).get(namespace)
    }
    local_by_title: dict[str, set[str]] = {}
    for card in scenario["grouped_cards"]:
        normalized = _normalized_title(str(card["key"]))
        if normalized:
            local_by_title.setdefault(normalized, set()).add(str(card["key"]))

    schema = game.get("card_schema") if isinstance(game.get("card_schema"), dict) else {}
    schema_fields = schema.get("translatable_fields") if isinstance(schema, dict) else []
    field_types = {
        str(field.get("id")): str(field.get("type") or "text")
        for field in schema_fields
        if isinstance(field, dict) and field.get("id")
    }
    result: dict[str, Any] = {
        "total": 0,
        "matched_by_id": 0,
        "matched_by_name": 0,
        "unmatched": 0,
        "examples": [],
    }
    seen_ids: set[str] = set()
    used_card_keys: set[str] = set()
    duplicate_ids: set[str] = set()
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            continue
        incoming = _record_from_source(
            row,
            source=source,
            source_language=str((scenario.get("project") or {}).get("source_language") or "en"),
            field_types=field_types,
            index=index,
        )
        result["total"] += 1
        external_id = str((incoming.get("identifiers") or {}).get(namespace) or "")
        title = str(
            incoming.get("text", {})
            .get(str((scenario.get("project") or {}).get("source_language") or "en"), {})
            .get("sides", {})
            .get("front", {})
            .get("title", "")
        )
        match = "unmatched"
        if external_id and external_id in seen_ids:
            duplicate_ids.add(external_id)
        seen_ids.add(external_id)
        if external_id and external_id in existing_ids:
            result["matched_by_id"] += 1
            match = "id"
        else:
            candidates = local_by_title.get(_normalized_title(title), set()) - used_card_keys
            if len(candidates) == 1:
                result["matched_by_name"] += 1
                used_card_keys.update(candidates)
                match = "name"
            else:
                result["unmatched"] += 1
        if len(result["examples"]) < 8:
            result["examples"].append({"external_id": external_id, "title": title, "match": match})
    result["duplicate_ids"] = sorted(duplicate_ids)
    return result


def unmatched_import_records(scenario: dict[str, Any]) -> list[dict[str, str]]:
    translation_path = scenario.get("translation_path")
    if not isinstance(translation_path, Path) or not translation_path.exists():
        return []
    try:
        data = read_scenario_data(scenario) or {}
    except (OSError, json.JSONDecodeError):
        return []
    source_language = str((scenario.get("project") or {}).get("source_language") or "en")
    result: list[dict[str, str]] = []
    for record in data.get("cards", []):
        if not isinstance(record, dict) or not str(record.get("key") or "").startswith("import:"):
            continue
        images = record.get("images") if isinstance(record.get("images"), dict) else {}
        if any(images.get(side) for side in ("front", "back")):
            continue
        source_text = record.get("text", {}).get(source_language, {})
        sides = source_text.get("sides", {}) if isinstance(source_text, dict) else {}
        front = sides.get("front", {}) if isinstance(sides, dict) else {}
        title = str(front.get("title") or source_text.get("title") or record.get("id") or "")
        identifiers = (
            record.get("identifiers") if isinstance(record.get("identifiers"), dict) else {}
        )
        external_id = next((str(value) for value in identifiers.values() if value), "")
        candidate_names = [
            (str(card["key"]), _normalized_title(str(card["key"])))
            for card in scenario.get("grouped_cards", [])
            if not card.get("matched")
        ]
        normalized_title = _normalized_title(title)
        suggestions = sorted(
            (
                (SequenceMatcher(None, normalized_title, normalized_name).ratio(), card_key)
                for card_key, normalized_name in candidate_names
                if normalized_title and normalized_name
            ),
            reverse=True,
        )
        score, suggestion = suggestions[0] if suggestions else (0.0, "")
        result.append(
            {
                "record_id": str(record.get("id") or ""),
                "title": title,
                "external_id": external_id,
                "provider": str((record.get("source") or {}).get("provider") or ""),
                "suggested_card_key": suggestion if score >= 0.6 else "",
                "suggestion_percent": str(round(score * 100)) if score >= 0.6 else "",
            }
        )
    return result


def latest_import_status(scenario: dict[str, Any]) -> dict[str, Any] | None:
    translation_path = scenario.get("translation_path")
    if not isinstance(translation_path, Path) or not translation_path.exists():
        return None
    try:
        data = read_scenario_data(scenario) or {}
    except (OSError, json.JSONDecodeError):
        return None
    meta = data.get("_meta") if isinstance(data.get("_meta"), dict) else {}
    imports = meta.get("imports") if isinstance(meta.get("imports"), list) else []
    latest = next((item for item in reversed(imports) if isinstance(item, dict)), None)
    if latest is None:
        return None
    return {
        **latest,
        "open_records": unmatched_import_records(scenario),
        "unmatched_local_cards": sum(
            1 for card in scenario.get("grouped_cards", []) if not card.get("matched")
        ),
    }


def match_import_record(
    root: Path,
    *,
    game_id: str,
    scenario_slug: str,
    project_language: str,
    import_record_id: str,
    card_key: str,
) -> dict[str, str]:
    scenario = get_scenario(root, game_id, scenario_slug, language=project_language)
    if scenario is None or not scenario["initialized"]:
        raise ValueError("Scenario not found or not initialized")
    logical_card = next(
        (card for card in scenario["grouped_cards"] if card["key"] == card_key), None
    )
    if logical_card is None:
        raise ValueError("Unknown local card")

    data = read_scenario_data(scenario) or {}
    records = data.get("cards")
    if not isinstance(records, list):
        raise ValueError("Translation contains invalid card records")  # noqa: TRY004
    imported = next(
        (
            record
            for record in records
            if isinstance(record, dict)
            and str(record.get("id") or "") == import_record_id
            and str(record.get("key") or "").startswith("import:")
        ),
        None,
    )
    if imported is None:
        raise ValueError("Unmatched import record not found")

    existing = next(
        (
            record
            for record in records
            if isinstance(record, dict)
            and record is not imported
            and str(record.get("key") or "") == card_key
        ),
        None,
    )
    if existing is not None:
        source_language = str((scenario.get("project") or {}).get("source_language") or "en")
        _merge_imported_record(imported, existing, source_language=source_language)
        records.remove(existing)

    imported["key"] = card_key
    imported["images"] = {
        side: (
            f"{scenario['card_source']}/{logical_card[side]['name']}"
            if logical_card.get(side)
            else None
        )
        for side in ("front", "back")
    }
    assignments = data.setdefault("_meta", {}).setdefault("manual_matches", {})
    assignments[card_key] = {
        "record_id": import_record_id,
        "matched_at": datetime.now(UTC).isoformat(),
    }
    write_scenario_data(scenario, data)
    identifiers = (
        imported.get("identifiers") if isinstance(imported.get("identifiers"), dict) else {}
    )
    external_id = next((str(value) for value in identifiers.values() if value), "")
    return {"card_key": card_key, "record_id": import_record_id, "external_id": external_id}


def set_card_identifier(
    root: Path,
    *,
    game_id: str,
    scenario_slug: str,
    project_language: str,
    card_key: str,
    namespace: str,
    external_id: str,
) -> dict[str, str]:
    scenario = get_scenario(root, game_id, scenario_slug, language=project_language)
    if scenario is None or not scenario["initialized"]:
        raise ValueError("Scenario not found or not initialized")
    logical_card = next(
        (card for card in scenario["grouped_cards"] if card["key"] == card_key), None
    )
    if logical_card is None:
        raise ValueError("Unknown local card")
    game = get_game(root, game_id)
    sources = game.get("imports", []) if game else []
    valid_namespaces = {
        str(source.get("identifier_namespace") or source.get("id"))
        for source in sources
        if isinstance(source, dict) and not source.get("adapter")
    }
    if namespace not in valid_namespaces:
        raise ValueError("Identifier namespace is not configured for this game")

    data = read_scenario_data(scenario) or {}
    records = data.get("cards")
    if not isinstance(records, list):
        raise ValueError("Translation contains invalid card records")  # noqa: TRY004
    external_id = external_id.strip()
    owner = next(
        (
            record
            for record in records
            if isinstance(record, dict)
            and str((record.get("identifiers") or {}).get(namespace) or "") == external_id
            and external_id
        ),
        None,
    )
    local = next(
        (
            record
            for record in records
            if isinstance(record, dict) and str(record.get("key") or "") == card_key
        ),
        None,
    )
    if owner is not None and owner is not local:
        if str(owner.get("key") or "").startswith("import:"):
            return match_import_record(
                root,
                game_id=game_id,
                scenario_slug=scenario_slug,
                project_language=project_language,
                import_record_id=str(owner.get("id") or ""),
                card_key=card_key,
            )
        raise ValueError("This external ID is already assigned to another local card")

    if local is None:
        local = {
            "id": card_key,
            "key": card_key,
            "images": {
                side: (
                    f"{scenario['card_source']}/{logical_card[side]['name']}"
                    if logical_card.get(side)
                    else None
                )
                for side in ("front", "back")
            },
            "identifiers": {},
            "text": {},
            "game_data": {},
        }
        records.append(local)
    identifiers = local.setdefault("identifiers", {})
    if external_id:
        identifiers[namespace] = external_id
    else:
        identifiers.pop(namespace, None)
    data.setdefault("_meta", {}).setdefault("identifier_edits", {})[f"{card_key}.{namespace}"] = {
        "value": external_id,
        "edited_at": datetime.now(UTC).isoformat(),
    }
    write_scenario_data(scenario, data)
    return {"card_key": card_key, "namespace": namespace, "external_id": external_id}
