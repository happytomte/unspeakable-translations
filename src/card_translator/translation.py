from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from card_translator.arkham_reference import ArkhamReference
from card_translator.config import (
    ollama_api_url,
    ollama_enabled,
    translategemma_context_length,
    translategemma_model,
)

PROTECTED_TOKEN_PATTERN = r"\{[^{}\r\n]+\}"
SQUARE_TOKEN_PATTERN = re.compile(r"(?<!\[)\[[a-z][a-z0-9_]*\](?!\])", re.IGNORECASE)

LANGUAGE_NAMES = {
    "ar": "Arabic",
    "az": "Azerbaijani",
    "bg": "Bulgarian",
    "ca": "Catalan",
    "cs": "Czech",
    "da": "Danish",
    "de": "German",
    "el": "Greek",
    "en": "English",
    "eo": "Esperanto",
    "es": "Spanish",
    "eu": "Basque",
    "fi": "Finnish",
    "fr": "French",
    "ga": "Irish",
    "gl": "Galician",
    "he": "Hebrew",
    "hi": "Hindi",
    "hu": "Hungarian",
    "id": "Indonesian",
    "it": "Italian",
    "ja": "Japanese",
    "ko": "Korean",
    "ms": "Malay",
    "nl": "Dutch",
    "pl": "Polish",
    "pt": "Portuguese",
    "pt-br": "Brazilian Portuguese",
    "ru": "Russian",
    "sk": "Slovak",
    "sv": "Swedish",
    "sw": "Swahili",
    "tr": "Turkish",
    "uk": "Ukrainian",
    "ur": "Urdu",
    "zh": "Chinese",
}

FIELD_CONTEXTS = {
    "title": "This is a card title. Return only the translated title.",
    "traits": (
        "This is a comma-separated list of card traits or keywords. Translate each item "
        "concisely and keep the list structure."
    ),
    "rules": (
        "This is rules text. Preserve paragraph structure, numbers, punctuation, timing "
        "labels and all game symbols exactly. Use clear imperative card-game wording."
    ),
    "flavor": (
        "This is narrative flavor text. Preserve its literary tone, quotations and attribution."
    ),
}


class TranslationUnavailable(RuntimeError):
    pass


class TranslationFailed(RuntimeError):
    pass


class TranslationReviewRequired(TranslationFailed):
    def __init__(
        self,
        *,
        original: str,
        draft: str,
        issues: list[dict[str, Any]],
        text_index: int,
    ) -> None:
        super().__init__("TranslateGemma changed an internal placeholder")
        self.original = original
        self.draft = draft
        self.issues = issues
        self.text_index = text_index
        self.side = ""
        self.field = ""
        self.trace: dict[str, Any] = {}
        self.traces: list[dict[str, Any]] = []


@dataclass(frozen=True)
class TranslationRule:
    source: str
    target: str


def parse_rules(value: str) -> list[TranslationRule]:
    """Parse the compact UI format: one ``source => target`` rule per line."""
    rules: list[TranslationRule] = []
    for line_number, raw_line in enumerate(value.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=>" not in line:
            raise ValueError(f"Rule on line {line_number} needs '=>'")
        source, target = (part.strip() for part in line.split("=>", 1))
        if not source or not target:
            raise ValueError(f"Rule on line {line_number} needs source and target text")
        rules.append(TranslationRule(source=source, target=target))
    return rules


def format_rules(rules: Iterable[TranslationRule]) -> str:
    return "\n".join(f"{rule.source} => {rule.target}" for rule in rules)


def merge_rules(
    game_rules: Iterable[TranslationRule], scenario_rules: Iterable[TranslationRule]
) -> list[TranslationRule]:
    """Merge rules while retaining intentional source-casing variants.

    A scenario/language group replaces the game-wide group with the same
    case-insensitive source. Within one group, however, exact spellings such as
    ``The Name`` and ``the Name`` remain separate rules.
    """
    game_rules = list(game_rules)
    scenario_rules = list(scenario_rules)
    overridden_groups = {rule.source.casefold() for rule in scenario_rules}
    merged: dict[str, TranslationRule] = {}
    for rule in game_rules:
        if rule.source.casefold() not in overridden_groups:
            merged[rule.source] = rule
    for rule in scenario_rules:
        merged[rule.source] = rule
    return sorted(merged.values(), key=lambda rule: len(rule.source), reverse=True)


def _rules_by_casefold(
    rules: Iterable[TranslationRule],
) -> dict[str, list[TranslationRule]]:
    grouped: dict[str, list[TranslationRule]] = {}
    for rule in rules:
        grouped.setdefault(rule.source.casefold(), []).append(rule)
    return grouped


def _applicable_rules(
    value: str, rules: Iterable[TranslationRule]
) -> list[TranslationRule]:
    """Select exact casing variants, with case-insensitive matching for singletons."""
    applicable: list[TranslationRule] = []
    for group in _rules_by_casefold(rules).values():
        if len(group) == 1:
            if re.search(re.escape(group[0].source), value, re.IGNORECASE):
                applicable.extend(group)
            continue
        applicable.extend(rule for rule in group if rule.source in value)
    return sorted(applicable, key=lambda rule: len(rule.source), reverse=True)


def _protected_parts(
    value: str, rules: Iterable[TranslationRule]
) -> list[tuple[bool, str]]:
    """Split text into translatable and protected canonical fragments."""
    rules = list(rules)
    exact_rules = {rule.source: rule.target for rule in rules}
    grouped_rules = _rules_by_casefold(rules)
    alternatives = [re.escape(rule.source) for rule in rules if rule.source]
    patterns = [PROTECTED_TOKEN_PATTERN]
    if alternatives:
        patterns.append("|".join(alternatives))
    pattern = re.compile("(" + "|".join(patterns) + ")", re.IGNORECASE)

    result: list[tuple[bool, str]] = []
    position = 0
    for match in pattern.finditer(value):
        if match.start() > position:
            result.append((False, value[position : match.start()]))
        found = match.group(0)
        replacement = exact_rules.get(found)
        if replacement is None and re.fullmatch(PROTECTED_TOKEN_PATTERN, found):
            replacement = found
        if replacement is None:
            matching_rules = grouped_rules.get(found.casefold(), [])
            if len(matching_rules) == 1:
                replacement = matching_rules[0].target
        result.append((replacement is not None, replacement or found))
        position = match.end()
    if position < len(value):
        result.append((False, value[position:]))
    return result


def _with_placeholders(
    value: str, rules: Iterable[TranslationRule]
) -> tuple[str, dict[str, str]]:
    placeholders: dict[str, str] = {}
    fragments: list[str] = []
    for protected, fragment in _protected_parts(value, rules):
        if not protected:
            fragments.append(fragment)
            continue
        marker = f"[CT_PROTECTED_{len(placeholders):04d}]"
        placeholders[marker] = fragment
        fragments.append(marker)
    return "".join(fragments), placeholders


def _normalize_glossary_outputs(
    value: str, rules: Sequence[TranslationRule]
) -> str:
    """Replace glossary source forms that the model copied into its translation."""
    for group in _rules_by_casefold(rules).values():
        for rule in sorted(group, key=lambda item: len(item.source), reverse=True):
            value = re.sub(
                re.escape(rule.source),
                lambda _match, target=rule.target: target,
                value,
                flags=0 if len(group) > 1 else re.IGNORECASE,
            )
    return value


def _recover_rendered_placeholders(value: str, placeholders: dict[str, str]) -> str:
    """Accept a protected value when the model rendered its final form directly."""
    for marker, expected in placeholders.items():
        if marker not in value and expected in value:
            value = value.replace(expected, marker, 1)
    return value


def _unconfigured_square_tokens(
    value: str, rules: Sequence[TranslationRule]
) -> list[str]:
    configured = {rule.source.casefold() for rule in rules}
    return sorted(
        {
            token
            for token in SQUARE_TOKEN_PATTERN.findall(value)
            if token.casefold() not in configured
        },
        key=str.casefold,
    )


def _language_name(code: str) -> str:
    return LANGUAGE_NAMES.get(code.lower(), code)


def _short_reference(value: str, limit: int = 700) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def _translation_prompt(
    text: str,
    *,
    source_language: str,
    target_language: str,
    rules: Sequence[TranslationRule],
    game: str,
    game_context: str,
    scenario_title: str,
    card_title: str,
    field: str,
    protected_markers: Sequence[str],
    references: Sequence[ArkhamReference] = (),
) -> str:
    source_name = _language_name(source_language)
    target_name = _language_name(target_language)
    instructions = [
        (
            f"You are a professional {source_name} ({source_language}) to "
            f"{target_name} ({target_language}) translator. Your goal is to accurately convey "
            f"the meaning and nuances of the original {source_name} text while adhering to "
            f"{target_name} grammar, vocabulary, and cultural sensitivities."
        ),
    ]
    if game_context.strip():
        instructions.append(game_context.strip())
    if scenario_title:
        instructions.append(f'The scenario or set is "{scenario_title}".')
    if card_title:
        instructions.append(f'The source card is titled "{card_title}".')
    if field in FIELD_CONTEXTS:
        instructions.append(FIELD_CONTEXTS[field])
    if rules:
        glossary = "\n".join(f"- {rule.source} => {rule.target}" for rule in rules)
        instructions.append(
            "The following project glossary is mandatory. Its approved target strings are "
            f"restored exactly after translation:\n{glossary}"
        )
    if protected_markers:
        marker_list = ", ".join(protected_markers)
        instructions.append(
            f"The source contains these protected markers: {marker_list}. Copy each one exactly "
            "once, including brackets and digits. Do not translate, remove, duplicate, renumber "
            "or add protected markers."
        )
    if references:
        examples = []
        for reference in references:
            examples.append(
                f"Official Arkham translation example ({reference.card_name}):\n"
                f"{source_name}:\n{_short_reference(reference.source)}\n"
                f"{target_name}:\n{_short_reference(reference.target)}"
            )
        instructions.append(
            "Use the following official translations only as style and terminology "
            "references. Translate the actual source text; do not copy unrelated card "
            "content.\n\n" + "\n\n".join(examples)
        )
    instructions.append(
        f"Produce only the {target_name} translation, without any additional explanations "
        f"or commentary. Please translate the following {source_name} text into {target_name}:"
    )
    return "\n".join(instructions) + "\n\n" + text


def ollama_status() -> dict[str, Any]:
    model = translategemma_model()
    if not ollama_enabled():
        return {
            "enabled": False, "available": False, "installed": False,
            "model": model, "error": "disabled",
        }
    try:
        with httpx.Client(base_url=ollama_api_url(), trust_env=False, timeout=3.0) as client:
            response = client.get("/api/tags")
        response.raise_for_status()
        payload = response.json()
        models = payload.get("models", []) if isinstance(payload, dict) else []
        names = {
            str(item.get("name") or item.get("model"))
            for item in models
            if isinstance(item, dict)
        }
    except (httpx.HTTPError, ValueError) as exc:
        return {
            "enabled": True, "available": False, "installed": False,
            "model": model, "error": str(exc),
        }
    return {
        "enabled": True,
        "available": True,
        "installed": model in names,
        "model": model,
        "error": "",
    }


def _translate_with_ollama(prompt: str) -> str:
    if not ollama_enabled():
        raise TranslationUnavailable("Ollama / TranslateGemma is disabled in settings")
    model = translategemma_model()
    try:
        with httpx.Client(base_url=ollama_api_url(), trust_env=False, timeout=300.0) as client:
            response = client.post(
                "/api/chat",
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                    "think": False,
                    "keep_alive": "10m",
                    "options": {
                        "temperature": 0,
                        "num_ctx": translategemma_context_length(),
                    },
                },
            )
        if response.status_code == 404:
            raise TranslationUnavailable(
                f"Ollama model '{model}' is not installed. Run: ollama pull {model}"
            )
        response.raise_for_status()
        payload = response.json()
    except TranslationUnavailable:
        raise
    except httpx.ConnectError as exc:
        raise TranslationUnavailable(
            f"Ollama is not reachable at {ollama_api_url()}. Start Ollama and try again."
        ) from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise TranslationFailed(f"Local Ollama translation failed: {exc}") from exc
    message = payload.get("message") if isinstance(payload, dict) else None
    translated = message.get("content") if isinstance(message, dict) else None
    if not isinstance(translated, str) or not translated.strip():
        raise TranslationFailed("TranslateGemma returned no translation.")
    return translated.strip()


def translate_texts(
    texts: list[str],
    *,
    source_language: str,
    target_language: str,
    rules: Iterable[TranslationRule] = (),
    game: str = "",
    game_context: str = "",
    scenario_title: str = "",
    card_title: str = "",
    fields: Sequence[str] | None = None,
    references: Sequence[Sequence[ArkhamReference]] | None = None,
    trace_log: list[dict[str, Any]] | None = None,
    external_translator: Callable[..., str] | None = None,
    prompt_translator: Callable[..., str] | None = None,
    provider_name: str = "translategemma",
) -> list[str]:
    """Translate with local TranslateGemma while protecting glossary terms and tokens."""
    if not texts:
        return []
    source_language = source_language.lower()
    target_language = target_language.lower()
    rules = list(rules)
    field_names = list(fields or [""] * len(texts))
    if len(field_names) != len(texts):
        raise ValueError("fields must have the same length as texts")
    reference_sets = list(references or [[] for _ in texts])
    if len(reference_sets) != len(texts):
        raise ValueError("references must have the same length as texts")

    translated_texts: list[str] = []
    for text_index, (text, field, text_references) in enumerate(
        zip(texts, field_names, reference_sets, strict=True)
    ):
        applicable_rules = _applicable_rules(text, rules)
        prepared, placeholders = _with_placeholders(text, applicable_rules)
        prompt = _translation_prompt(
            prepared,
            source_language=source_language,
            target_language=target_language,
            rules=applicable_rules,
            game=game,
            game_context=game_context,
            scenario_title=scenario_title,
            card_title=card_title,
            field=field,
            protected_markers=list(placeholders),
            references=text_references,
        )
        if external_translator:
            prompt = f"{provider_name} input:\n{prepared}"
        trace: dict[str, Any] = {
            "field": field,
            "original": text,
            "prepared_source": prepared,
            "prompt": prompt,
            "raw_translation": "",
            "normalized_translation": "",
            "final_translation": "",
            "issues": [],
            "warnings": [
                {
                    "type": "unconfigured_token",
                    "value": token,
                    "message": "No glossary rule is configured for this square-bracket token",
                }
                for token in _unconfigured_square_tokens(text, applicable_rules)
            ],
            "status": "started",
        }
        try:
            if prompt_translator:
                translated = prompt_translator(
                    prompt,
                    source_language=source_language,
                    target_language=target_language,
                )
            elif external_translator:
                translated = external_translator(
                    prepared,
                    source_language=source_language,
                    target_language=target_language,
                )
            else:
                translated = _translate_with_ollama(prompt)
        except TranslationFailed as exc:
            trace.update({"status": "failed", "issues": [{"type": "error", "message": str(exc)}]})
            if trace_log is not None:
                trace_log.append(trace)
            raise
        except (httpx.HTTPError, RuntimeError, ValueError, KeyError) as exc:
            wrapped = TranslationFailed(f"{provider_name} translation failed: {exc}")
            trace.update(
                {"status": "failed", "issues": [{"type": "error", "message": str(wrapped)}]}
            )
            if trace_log is not None:
                trace_log.append(trace)
            raise wrapped from exc
        trace["raw_translation"] = translated
        translated = _normalize_glossary_outputs(translated, applicable_rules)
        translated = _recover_rendered_placeholders(translated, placeholders)
        trace["normalized_translation"] = translated
        changed = [marker for marker in placeholders if translated.count(marker) != 1]
        unexpected = re.findall(r"\[CT_PROTECTED_\d{4}\]", translated)
        if changed or any(marker not in placeholders for marker in unexpected):
            issues = [
                {
                    "type": "missing" if translated.count(marker) == 0 else "duplicate",
                    "marker": marker,
                    "expected": placeholders[marker],
                    "count": translated.count(marker),
                }
                for marker in changed
            ]
            issues.extend(
                {
                    "type": "unexpected",
                    "marker": marker,
                    "expected": "",
                    "count": translated.count(marker),
                }
                for marker in sorted(set(unexpected) - set(placeholders))
            )
            draft = translated
            for marker, protected_text in placeholders.items():
                draft = draft.replace(marker, protected_text)
            trace.update({"status": "review_required", "final_translation": draft.strip(), "issues": issues})
            if trace_log is not None:
                trace_log.append(trace)
            error = TranslationReviewRequired(
                original=text,
                draft=draft.strip(),
                issues=issues,
                text_index=text_index,
            )
            error.trace = trace
            raise error
        for marker, protected_text in placeholders.items():
            translated = translated.replace(marker, protected_text)
        translated = translated.strip()
        trace.update({"status": "translated", "final_translation": translated})
        if trace_log is not None:
            trace_log.append(trace)
        translated_texts.append(translated)
    return translated_texts


def finalize_external_translation(
    original: str, draft: str, rules: Iterable[TranslationRule]
) -> tuple[str, list[dict[str, Any]]]:
    """Apply glossary rules to a pasted translation and validate protected values."""
    applicable = _applicable_rules(original, list(rules))
    _, placeholders = _with_placeholders(original, applicable)
    normalized = _normalize_glossary_outputs(draft, applicable)
    normalized = _recover_rendered_placeholders(normalized, placeholders)
    issues = [
        {
            "type": "missing" if normalized.count(marker) == 0 else "duplicate",
            "marker": marker,
            "expected": expected,
            "count": normalized.count(marker),
        }
        for marker, expected in placeholders.items()
        if normalized.count(marker) != 1
    ]
    if issues:
        for marker, expected in placeholders.items():
            normalized = normalized.replace(marker, expected)
        return normalized.strip(), issues
    for marker, expected in placeholders.items():
        normalized = normalized.replace(marker, expected)
    return normalized.strip(), []


def prepare_external_translation(
    original: str, rules: Iterable[TranslationRule]
) -> str:
    """Protect configured terms before opening an external translation website."""
    applicable = _applicable_rules(original, list(rules))
    prepared, _ = _with_placeholders(original, applicable)
    return prepared
