from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

GAME_ID = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
FIELD_ID = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

DEFAULT_CARD_SCHEMA = {
    "sides": ["front", "back"],
    "translatable_fields": [
        {"id": "title", "type": "text", "required": True},
        {"id": "traits", "type": "list"},
        {"id": "rules", "type": "multiline"},
        {"id": "flavor", "type": "multiline"},
    ],
    "metadata_fields": [],
}

ARKHAM_TRANSLATABLE_FIELDS = [
    {"id": "title", "label": "Name", "type": "text", "required": True},
    {"id": "subname", "label": "Subtitle", "type": "text"},
    {"id": "traits", "label": "Traits", "type": "list"},
    {"id": "rules", "label": "Rules", "type": "multiline"},
    {"id": "flavor", "label": "Flavor", "type": "multiline"},
]
ARKHAM_LOCATION_SYMBOLS = [
    "circle", "clover", "cross", "diamond", "double_slash", "heart",
    "hourglass", "crescent", "moon", "quote", "slash", "spade",
    "square", "star", "sun", "t", "triangle", "ying",
]
ARKHAM_CLASSES = [
    "guardian", "survivor", "seeker", "mystic", "rogue", "neutral",
    "specialist", "weakness", "basic weakness",
]
ARKHAM_METADATA_FIELDS = [
    {"id": "card_type", "label": "Card type", "type": "text"},
    {"id": "encounter_set", "label": "Encounter set", "type": "text"},
    {"id": "card_number", "label": "Card number", "type": "text"},
    {"id": "printed_quantity", "label": "Printed quantity", "type": "integer"},
    {"id": "pack_name", "label": "Pack", "type": "text"},
    {"id": "artist", "label": "Artist", "type": "text"},
    {"id": "classes", "label": "Classes", "type": "list", "options": ARKHAM_CLASSES},
    {"id": "copyright", "label": "Copyright", "type": "text", "common": True, "scope": "card"},
    {"id": "project_number", "label": "Collection number", "type": "text", "common": True, "scope": "card"},
    {"id": "encounter_number", "label": "Encounter number", "type": "text", "common": True, "scope": "card"},
    {"id": "stage", "label": "Stage", "type": "integer"},
    {"id": "index", "label": "Index", "type": "text"},
    {"id": "doom", "label": "Doom", "type": "text", "value_format": "scaled_number"},
    {"id": "shroud", "label": "Shroud", "type": "text", "value_format": "scaled_number"},
    {"id": "clues", "label": "Clues", "type": "text", "value_format": "scaled_number"},
    {"id": "health", "label": "Health", "type": "text", "value_format": "scaled_number"},
    {"id": "sanity", "label": "Sanity", "type": "text", "value_format": "scaled_number"},
    {"id": "fight", "label": "Fight", "type": "text", "value_format": "scaled_number"},
    {"id": "damage", "label": "Damage", "type": "text", "value_format": "scaled_number"},
    {"id": "horror", "label": "Horror", "type": "text", "value_format": "scaled_number"},
    {"id": "evade", "label": "Evade", "type": "text", "value_format": "scaled_number"},
    {"id": "cost", "label": "Cost", "type": "text", "value_format": "scaled_number"},
    {"id": "xp", "label": "Experience", "type": "integer"},
    {"id": "slot", "label": "Slot", "type": "text"},
    {"id": "willpower", "label": "Willpower icon", "type": "integer"},
    {"id": "intellect", "label": "Intellect icon", "type": "integer"},
    {"id": "combat", "label": "Combat icon", "type": "integer"},
    {"id": "agility", "label": "Agility icon", "type": "integer"},
    {"id": "wild", "label": "Wild icon", "type": "integer"},
    {"id": "connection", "label": "Location symbol", "type": "text", "options": ARKHAM_LOCATION_SYMBOLS},
    {"id": "connections", "label": "Connected location symbols", "type": "list", "options": ARKHAM_LOCATION_SYMBOLS, "max_items": 6},
    {"id": "victory", "label": "Victory", "type": "text", "value_format": "scaled_number"},
    {"id": "vengeance", "label": "Vengeance", "type": "text", "value_format": "scaled_number"},
]


def _arkham_type(type_id: str, label: str, fields: str) -> dict[str, Any]:
    common = (
        "title traits rules flavor encounter_set card_number printed_quantity pack_name "
        "artist copyright project_number encounter_number"
    )
    return {"id": type_id, "label": label, "fields": (common + " " + fields).split()}


ARKHAM_CARD_TYPES = [
    _arkham_type("act", "Act", "stage index clues victory"),
    _arkham_type("agenda", "Agenda", "stage index doom"),
    _arkham_type("act_agenda_full", "Act/Agenda (full card)", "stage index clues doom victory"),
    _arkham_type("asset", "Asset", "subname classes cost xp slot willpower intellect combat agility wild health sanity victory"),
    _arkham_type("chaos", "Chaos", "subname"),
    _arkham_type("concealed", "Concealed", "subname"),
    _arkham_type("customizable", "Customizable", "subname classes cost xp slot willpower intellect combat agility wild health sanity victory"),
    _arkham_type("enemy", "Enemy", "subname classes health fight evade damage horror victory vengeance"),
    _arkham_type("enemy_deck", "Enemy deck", "subname"),
    _arkham_type("enemy_location", "Enemy-Location", "classes health fight evade damage horror shroud clues victory vengeance"),
    _arkham_type("event", "Event", "subname classes cost xp willpower intellect combat agility wild victory"),
    _arkham_type("investigator", "Investigator", "subname classes willpower intellect combat agility health sanity"),
    _arkham_type("key", "Key", "subname"),
    _arkham_type("location", "Location", "subname connection connections shroud clues victory vengeance"),
    _arkham_type("mini_investigator", "Mini investigator", "subname"),
    _arkham_type("scenario", "Scenario", ""),
    _arkham_type("skill", "Skill", "subname classes xp willpower intellect combat agility wild"),
    _arkham_type("story", "Story", "victory"),
    _arkham_type("treachery", "Treachery", "subname classes victory vengeance"),
    _arkham_type("ultimatum", "Ultimatum", "subname"),
    _arkham_type("weakness", "Weakness", "subname classes cost xp slot willpower intellect combat agility wild health sanity"),
    _arkham_type("fullart_asset", "Full-art asset", "subname classes cost xp slot willpower intellect combat agility wild health sanity victory"),
    _arkham_type("fullart_event", "Full-art event", "subname classes cost xp willpower intellect combat agility wild victory"),
    _arkham_type("fullart_skill", "Full-art skill", "subname classes xp willpower intellect combat agility wild"),
    _arkham_type("fullart_investigator", "Full-art investigator", "subname classes willpower intellect combat agility health sanity"),
    _arkham_type("fullart_enemy", "Full-art enemy", "subname classes health fight evade damage horror victory vengeance"),
    _arkham_type("fullart_treachery", "Full-art treachery", "subname classes victory vengeance"),
    _arkham_type("fullart_location", "Full-art location", "subname connection connections shroud clues victory vengeance"),
    _arkham_type("fullart_scanning", "Full-art scanning", "subname"),
    _arkham_type("fullart_encounter_with_connections", "Full-art encounter with connections", "subname connection connections"),
    _arkham_type("chapter_2/enemy", "Chapter 2 enemy", "subname classes health fight evade damage horror victory vengeance"),
]

# These definitions keep existing libraries working. A library/<id>/game.json file
# overrides the corresponding default and is the canonical editable representation.
BUILTIN_GAMES: dict[str, dict[str, Any]] = {
    "ah": {
        "format": "card-translator-game",
        "format_version": 1,
        "id": "ah",
        "name": "Arkham Horror: The Card Game",
        "description": "Cooperative campaign card game of mystery and cosmic horror.",
        "default_source_language": "en",
        "translation_context": (
            "The text is from Arkham Horror: The Card Game. Use concise, formal rules "
            "language and established terminology of the target-language card game. Preserve "
            "the distinction between rules text, traits, locations and narrative flavor. Treat "
            "card names as proper names unless the glossary specifies a translation."
        ),
        "default_translation_rules": [
            {"source": f"[{token}]", "target": f"{{{token}}}"}
            for token in (
                "action",
                "agility",
                "auto_fail",
                "bless",
                "clue",
                "codex",
                "combat",
                "cultist",
                "curse",
                "day",
                "elder_sign",
                "elder_thing",
                "fast",
                "free",
                "frost",
                "guardian",
                "intellect",
                "knowledge",
                "mystic",
                "name",
                "night",
                "per_investigator",
                "reaction",
                "rogue",
                "seal_a",
                "seal_b",
                "seal_c",
                "seal_d",
                "seal_e",
                "seeker",
                "skull",
                "survivor",
                "tablet",
                *(f"tdc_rune_{letter}" for letter in "abcdefghijklmnopqrstuvwxyz"),
                "wild",
                "willpower",
            )
        ],
        "arkhamdb_urls": {
            language: (
                "https://arkhamdb.com/api/public/cards/?encounter=1"
                if language == "en"
                else f"https://{language}.arkhamdb.com/api/public/cards/?encounter=1"
            )
            for language in (
                "en",
                "cs",
                "de",
                "es",
                "fr",
                "it",
                "ko",
                "pl",
                "pt",
                "ru",
                "uk",
                "vn",
                "zh-cn",
                "zh",
            )
        },
        "card_schema": {
            "sides": ["front", "back"],
            "translatable_fields": ARKHAM_TRANSLATABLE_FIELDS,
            "metadata_fields": ARKHAM_METADATA_FIELDS,
            "card_types": ARKHAM_CARD_TYPES,
        },
        "imports": [
            {
                "id": "arkham-json",
                "label": "Arkham JSON",
                "records_path": "$",
                "identifier_namespace": "arkham-json",
                "identifier": "code",
                "fields": {
                    "title": "name",
                    "subname": "subname",
                    "traits": "traits",
                    "rules": "text",
                    "flavor": "flavor",
                    "sides.back.title": "back_name",
                    "sides.back.rules": "back_text",
                    "sides.back.flavor": "back_flavor",
                    "card_type": "type_code",
                    "game_data.encounter_set": "encounter_name",
                    "game_data.card_number": "encounter_position",
                    "game_data.printed_quantity": "quantity",
                    "game_data.pack_name": "pack_name",
                    "game_data.artist": "illustrator",
                    "game_data.project_number": "position",
                    "game_data.encounter_number": "encounter_position",
                    "game_data.stage": "stage",
                    "game_data.doom": "doom",
                    "game_data.clues": "clues",
                    "game_data.shroud": "shroud",
                    "game_data.health": "health",
                    "game_data.sanity": "sanity",
                    "game_data.fight": "enemy_fight",
                    "game_data.damage": "enemy_damage",
                    "game_data.horror": "enemy_horror",
                    "game_data.evade": "enemy_evade",
                    "game_data.cost": "cost",
                    "game_data.xp": "xp",
                    "game_data.slot": "slot",
                    "game_data.willpower": "skill_willpower",
                    "game_data.intellect": "skill_intellect",
                    "game_data.combat": "skill_combat",
                    "game_data.agility": "skill_agility",
                    "game_data.wild": "skill_wild",
                    "game_data.victory": "victory",
                    "game_data.vengeance": "vengeance",
                },
            }
        ],
        "filename_parser": {
            "pattern": r"^\d+-(?P<id>\d+(?:-m)?)-(?P<title>.+)-(?P<side>front|back)$",
            "identifier_namespace": "arkham-json",
        },
    },
    "lotr": {
        "format": "card-translator-game",
        "format_version": 1,
        "id": "lotr",
        "name": "The Lord of the Rings: The Card Game",
        "description": "Cooperative adventure card game set in Middle-earth.",
        "default_source_language": "en",
        "translation_context": (
            "The text is from The Lord of the Rings: The Card Game. Use concise, formal rules "
            "language and established terminology of the target-language card game. Treat "
            "Middle-earth names and card names as proper names; localize them only when the "
            "glossary specifies a translation or an established target-language name is certain."
        ),
        "card_schema": {
            **DEFAULT_CARD_SCHEMA,
            "metadata_fields": [
                {"id": "card_type", "label": "Card type", "type": "text"},
                {"id": "deck_role", "label": "Deck role", "type": "text"},
                {"id": "encounter_set", "label": "Encounter set", "type": "text"},
                {"id": "set_name", "label": "Set / product", "type": "text"},
                {"id": "scenario_section", "label": "Scenario section", "type": "text"},
                {"id": "card_number", "label": "Card number", "type": "integer"},
                {"id": "printed_quantity", "label": "Printed quantity", "type": "integer"},
            ],
        },
        "imports": [{"id": "hall-of-beorn", "label": "Hall of Beorn", "adapter": "hall_of_beorn"}],
    },
}


def normalize_game_id(value: str) -> str:
    game_id = value.strip().lower()
    if not GAME_ID.fullmatch(game_id):
        raise ValueError("Game ID must start with a letter and contain only a-z, 0-9 and hyphens")
    return game_id


def _definition_error(value: Any, directory_name: str) -> str:
    if not isinstance(value, dict):
        return "Definition must be a JSON object"
    if value.get("format") != "card-translator-game":
        return "Missing or invalid format; expected card-translator-game"
    if value.get("format_version") != 1:
        return "Unsupported format_version; expected 1"
    if str(value.get("id") or "") != directory_name:
        return "Game ID must match its directory name"
    if not str(value.get("name") or "").strip():
        return "Game name is required"
    source_language = str(value.get("default_source_language") or "")
    if not re.fullmatch(r"[a-z]{2,3}(?:-[a-z0-9]{2,8})?", source_language):
        return "default_source_language must be a short lowercase language code"

    schema = value.get("card_schema")
    if not isinstance(schema, dict):
        return "card_schema must be an object"
    translatable = schema.get("translatable_fields")
    if not isinstance(translatable, list) or not translatable:
        return "card_schema.translatable_fields must be a non-empty array"
    seen: set[str] = set()
    for index, field in enumerate(translatable):
        if not isinstance(field, dict):
            return f"translatable_fields[{index}] must be an object"
        field_id = str(field.get("id") or "")
        if not FIELD_ID.fullmatch(field_id):
            return f"translatable_fields[{index}].id is invalid"
        if field_id in seen:
            return f"Duplicate translatable field ID: {field_id}"
        if field.get("type") not in {"text", "multiline", "list"}:
            return f"translatable_fields[{index}].type is invalid"
        seen.add(field_id)
    if "title" not in seen:
        return "card_schema.translatable_fields must contain title"

    metadata = schema.get("metadata_fields", [])
    if not isinstance(metadata, list):
        return "card_schema.metadata_fields must be an array"
    metadata_seen: set[str] = set()
    for index, field in enumerate(metadata):
        if not isinstance(field, dict):
            return f"metadata_fields[{index}] must be an object"
        field_id = str(field.get("id") or "")
        if not FIELD_ID.fullmatch(field_id):
            return f"metadata_fields[{index}].id is invalid"
        if field_id in metadata_seen:
            return f"Duplicate metadata field ID: {field_id}"
        if field.get("type") not in {"text", "integer", "list"}:
            return f"metadata_fields[{index}].type is invalid"
        metadata_seen.add(field_id)

    sources = value.get("imports", [])
    if not isinstance(sources, list):
        return "imports must be an array"
    source_ids: set[str] = set()
    for index, source in enumerate(sources):
        if not isinstance(source, dict):
            return f"imports[{index}] must be an object"
        source_id = str(source.get("id") or "")
        if not GAME_ID.fullmatch(source_id):
            return f"imports[{index}].id is invalid"
        if source_id in source_ids:
            return f"Duplicate data source ID: {source_id}"
        source_ids.add(source_id)
        if not str(source.get("label") or "").strip():
            return f"imports[{index}].label is required"
        if source.get("adapter"):
            continue
        if not str(source.get("records_path") or "").strip():
            return f"imports[{index}].records_path is required"
        fields = source.get("fields")
        if not isinstance(fields, dict) or not str(fields.get("title") or "").strip():
            return f"imports[{index}].fields.title is required"

    filename_parser = value.get("filename_parser")
    if filename_parser is not None:
        if not isinstance(filename_parser, dict):
            return "filename_parser must be an object"
        pattern = str(filename_parser.get("pattern") or "")
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            return f"filename_parser.pattern is invalid: {exc}"
        if not ({"id", "title"} & set(compiled.groupindex)):
            return "filename_parser.pattern needs a named id or title group"
        namespace = str(filename_parser.get("identifier_namespace") or "")
        namespaces = {
            str(source.get("identifier_namespace") or source.get("id") or "")
            for source in sources
            if isinstance(source, dict) and not source.get("adapter")
        }
        if "id" in compiled.groupindex and namespace not in namespaces:
            return "filename_parser.identifier_namespace must match a JSON data source"
    return ""


def _read_definition(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if _definition_error(value, path.parent.name):
        return None
    return value


def game_definition_issues(root: Path) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    if not root.exists():
        return issues
    for game_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        path = game_dir / "game.json"
        if not path.exists():
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            issues.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "error": f"Invalid JSON at line {exc.lineno}, column {exc.colno}",
                }
            )
            continue
        except OSError as exc:
            issues.append({"path": path.relative_to(root).as_posix(), "error": str(exc)})
            continue
        error = _definition_error(value, game_dir.name)
        if error:
            issues.append({"path": path.relative_to(root).as_posix(), "error": error})
    return issues


def discover_games(root: Path) -> list[dict[str, Any]]:
    definitions = {key: dict(value) for key, value in BUILTIN_GAMES.items()}
    if root.exists():
        for game_dir in sorted(path for path in root.iterdir() if path.is_dir()):
            definition = _read_definition(game_dir / "game.json")
            if definition is None:
                continue
            try:
                game_id = normalize_game_id(str(definition.get("id") or game_dir.name))
            except ValueError:
                continue
            if game_id != game_dir.name:
                continue
            builtin = definitions.get(game_id, {})
            merged = {**builtin, **definition}
            builtin_schema = builtin.get("card_schema")
            local_schema = definition.get("card_schema")
            if isinstance(builtin_schema, dict) and isinstance(local_schema, dict):
                merged_schema = {**builtin_schema, **local_schema}
                for key in ("translatable_fields", "metadata_fields"):
                    defaults = builtin_schema.get(key)
                    configured = local_schema.get(key)
                    if isinstance(defaults, list) and isinstance(configured, list):
                        configured_by_id = {
                            str(item.get("id")): item
                            for item in configured
                            if isinstance(item, dict) and item.get("id")
                        }
                        merged_fields = []
                        for item in defaults:
                            if not isinstance(item, dict):
                                continue
                            field_id = str(item.get("id"))
                            merged_field = {**item, **configured_by_id.get(field_id, {})}
                            if game_id == "ah" and key == "metadata_fields" and field_id in {"connection", "connections"}:
                                default_options = item.get("options") or []
                                configured_options = merged_field.get("options") or []
                                merged_field["options"] = list(dict.fromkeys([*configured_options, *default_options]))
                            merged_fields.append(merged_field)
                        merged_schema[key] = merged_fields + [
                            item
                            for item in configured
                            if isinstance(item, dict)
                            and str(item.get("id"))
                            not in {str(default.get("id")) for default in defaults if isinstance(default, dict)}
                        ]
                default_types = builtin_schema.get("card_types")
                configured_types = local_schema.get("card_types")
                if isinstance(default_types, list) and isinstance(configured_types, list):
                    configured_by_id = {
                        str(item.get("id")): item
                        for item in configured_types
                        if isinstance(item, dict) and item.get("id")
                    }
                    if game_id == "ah" and "mini_investigator" not in configured_by_id:
                        legacy_mini = configured_by_id.pop("minicard", None)
                        if legacy_mini:
                            configured_by_id["mini_investigator"] = {
                                **legacy_mini,
                                "id": "mini_investigator",
                            }
                    default_ids = {
                        str(item.get("id"))
                        for item in default_types
                        if isinstance(item, dict) and item.get("id")
                    }
                    merged_schema["card_types"] = [
                        {**item, **configured_by_id.get(str(item.get("id")), {})}
                        for item in default_types
                        if isinstance(item, dict)
                    ] + [
                        item for type_id, item in configured_by_id.items()
                        if type_id not in default_ids
                    ]
                merged["card_schema"] = merged_schema
            builtin_imports = builtin.get("imports")
            local_imports = definition.get("imports")
            if isinstance(builtin_imports, list) and isinstance(local_imports, list):
                builtin_by_id = {
                    str(item.get("id")): item
                    for item in builtin_imports
                    if isinstance(item, dict) and item.get("id")
                }
                merged_imports: list[dict[str, Any]] = []
                for item in local_imports:
                    if not isinstance(item, dict):
                        continue
                    default = builtin_by_id.get(str(item.get("id") or ""), {})
                    combined = {**default, **item}
                    if isinstance(default.get("fields"), dict) and isinstance(item.get("fields"), dict):
                        combined["fields"] = {**default["fields"], **item["fields"]}
                    merged_imports.append(combined)
                local_ids = {str(item.get("id")) for item in local_imports if isinstance(item, dict)}
                merged_imports.extend(
                    item
                    for item in builtin_imports
                    if isinstance(item, dict) and str(item.get("id")) not in local_ids
                )
                merged["imports"] = merged_imports
            definitions[game_id] = merged
    return [definitions[key] for key in sorted(definitions)]


def get_game(root: Path, game_id: str) -> dict[str, Any] | None:
    normalized = normalize_game_id(game_id)
    return next((game for game in discover_games(root) if game.get("id") == normalized), None)


def save_game(
    root: Path,
    *,
    game_id: str,
    name: str,
    description: str | None = None,
    default_source_language: str = "en",
    translation_context: str | None = None,
) -> dict[str, Any]:
    game_id = normalize_game_id(game_id)
    name = name.strip()
    if not name:
        raise ValueError("Game name is required")
    existing = get_game(root, game_id) or {}
    definition = {
        **existing,
        "format": "card-translator-game",
        "format_version": 1,
        "id": game_id,
        "name": name,
        "description": (
            str(existing.get("description") or "")
            if description is None
            else description.strip()
        ),
        "default_source_language": default_source_language.strip().lower() or "en",
        "translation_context": (
            str(existing.get("translation_context") or "")
            if translation_context is None
            else translation_context.strip()
        ),
        "card_schema": existing.get("card_schema") or DEFAULT_CARD_SCHEMA,
        "imports": existing.get("imports") if isinstance(existing.get("imports"), list) else [],
    }
    definition.pop("logo", None)
    if error := _definition_error(definition, game_id):
        raise ValueError(error)
    game_dir = root / game_id
    game_dir.mkdir(parents=True, exist_ok=True)
    (game_dir / "game.json").write_text(
        json.dumps(definition, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return definition


def save_translatable_fields(
    root: Path,
    *,
    game_id: str,
    fields_text: str,
    metadata_fields_text: str | None = None,
) -> list[dict[str, Any]]:
    game = get_game(root, game_id)
    if game is None:
        raise ValueError("Unknown game")
    fields: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line_number, raw_line in enumerate(fields_text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split("|")]
        if len(parts) not in {2, 3}:
            raise ValueError(f"Invalid card field on line {line_number}; use id | label | type")
        field_id, label = parts[:2]
        field_type = parts[2] if len(parts) == 3 else "text"
        if not FIELD_ID.fullmatch(field_id):
            raise ValueError(f"Invalid field ID on line {line_number}")
        if field_id in seen:
            raise ValueError(f"Duplicate field ID: {field_id}")
        if field_type not in {"text", "multiline", "list"}:
            raise ValueError(f"Invalid type on line {line_number}: {field_type}")
        if not label:
            raise ValueError(f"Missing field label on line {line_number}")
        fields.append({"id": field_id, "label": label, "type": field_type})
        seen.add(field_id)
    if not fields:
        raise ValueError("At least one translatable card field is required")
    if "title" not in seen:
        raise ValueError("The card schema must contain a title field")
    schema = game.get("card_schema") if isinstance(game.get("card_schema"), dict) else {}
    schema["translatable_fields"] = fields
    if metadata_fields_text is not None:
        metadata_fields: list[dict[str, Any]] = []
        metadata_seen: set[str] = set()
        for line_number, raw_line in enumerate(metadata_fields_text.splitlines(), start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [part.strip() for part in line.split("|")]
            if len(parts) not in {2, 3}:
                raise ValueError(
                    f"Invalid metadata field on line {line_number}; use id | label | type"
                )
            field_id, label = parts[:2]
            field_type = parts[2] if len(parts) == 3 else "text"
            if not FIELD_ID.fullmatch(field_id):
                raise ValueError(f"Invalid metadata field ID on line {line_number}")
            if field_id in metadata_seen:
                raise ValueError(f"Duplicate metadata field ID: {field_id}")
            if field_type not in {"text", "integer", "list"}:
                raise ValueError(f"Invalid metadata type on line {line_number}: {field_type}")
            if not label:
                raise ValueError(f"Missing metadata field label on line {line_number}")
            metadata_fields.append({"id": field_id, "label": label, "type": field_type})
            metadata_seen.add(field_id)
        schema["metadata_fields"] = metadata_fields
    schema.setdefault("sides", ["front", "back"])
    schema.setdefault("metadata_fields", [])
    game["card_schema"] = schema
    game.pop("logo", None)
    if error := _definition_error(game, game_id):
        raise ValueError(error)
    game_dir = root / game_id
    game_dir.mkdir(parents=True, exist_ok=True)
    (game_dir / "game.json").write_text(
        json.dumps(game, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return fields


def save_json_import_source(
    root: Path,
    *,
    game_id: str,
    source_id: str,
    label: str,
    records_path: str,
    identifier_namespace: str,
    identifier: str,
    fields: dict[str, str],
) -> dict[str, Any]:
    game = get_game(root, game_id)
    if game is None:
        raise ValueError("Unknown game")
    source_id = normalize_game_id(source_id)
    label = label.strip()
    if not label:
        raise ValueError("Data source label is required")
    cleaned_fields = {
        internal: external.strip()
        for internal, external in fields.items()
        if external.strip()
    }
    if not cleaned_fields.get("title"):
        raise ValueError("A JSON path for the card title is required")
    source = {
        "id": source_id,
        "label": label,
        "records_path": records_path.strip() or "$",
        "identifier_namespace": identifier_namespace.strip() or source_id,
        "identifier": identifier.strip() or "id",
        "fields": cleaned_fields,
    }
    sources = game.get("imports") if isinstance(game.get("imports"), list) else []
    sources = [item for item in sources if not (isinstance(item, dict) and item.get("id") == source_id)]
    sources.append(source)
    game["imports"] = sources
    if error := _definition_error(game, game_id):
        raise ValueError(error)
    game_dir = root / game_id
    game_dir.mkdir(parents=True, exist_ok=True)
    (game_dir / "game.json").write_text(
        json.dumps(game, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return source


def delete_json_import_source(root: Path, *, game_id: str, source_id: str) -> None:
    game = get_game(root, game_id)
    if game is None:
        raise ValueError("Unknown game")
    source_id = normalize_game_id(source_id)
    sources = game.get("imports") if isinstance(game.get("imports"), list) else []
    matching = [
        source
        for source in sources
        if isinstance(source, dict) and source.get("id") == source_id and not source.get("adapter")
    ]
    if not matching:
        raise ValueError("JSON data source not found")
    game["imports"] = [
        source
        for source in sources
        if not (isinstance(source, dict) and source.get("id") == source_id)
    ]
    game.pop("logo", None)
    game_dir = root / game_id
    game_dir.mkdir(parents=True, exist_ok=True)
    (game_dir / "game.json").write_text(
        json.dumps(game, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def save_filename_parser(
    root: Path,
    *,
    game_id: str,
    pattern: str,
    identifier_namespace: str,
) -> dict[str, str] | None:
    game = get_game(root, game_id)
    if game is None:
        raise ValueError("Unknown game")
    pattern = pattern.strip()
    if pattern:
        game["filename_parser"] = {
            "pattern": pattern,
            "identifier_namespace": identifier_namespace.strip(),
        }
    else:
        game.pop("filename_parser", None)
    if error := _definition_error(game, game_id):
        raise ValueError(error)
    game_dir = root / game_id
    game_dir.mkdir(parents=True, exist_ok=True)
    (game_dir / "game.json").write_text(
        json.dumps(game, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return game.get("filename_parser")
