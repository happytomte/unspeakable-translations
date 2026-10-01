from __future__ import annotations

import csv
import io
import re
import shutil
import subprocess
import tempfile
from difflib import SequenceMatcher
from pathlib import Path


OCR_LANGUAGES = {
    "de": "deu",
    "en": "eng",
    "es": "spa",
    "fr": "fra",
    "it": "ita",
    "pl": "pol",
}


def dependencies_available() -> bool:
    return bool(shutil.which("magick") and shutil.which("tesseract"))


def _normalized(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def extract_quest_flavor(image: Path, *, known_rules: str) -> str | None:
    """Read a missing landscape quest-card flavor block conservatively.

    OCR is accepted only when its leading lines closely match rules already
    supplied by the structured source. This prevents artwork or footer text
    from being mistaken for canonical flavor text.
    """
    magick = shutil.which("magick")
    tesseract = shutil.which("tesseract")
    if not magick or not tesseract or not known_rules.strip():
        return None

    try:
        dimensions = subprocess.run(
            [magick, "identify", "-format", "%w %h", str(image)],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        ).stdout.split()
        width, height = (int(value) for value in dimensions)
        if width <= height:
            return None

        crop = (
            f"{round(width * 0.94)}x{round(height * 0.345)}"
            f"+{round(width * 0.03)}+{round(height * 0.615)}"
        )
        with tempfile.TemporaryDirectory(prefix="unspeakable-translations-ocr-") as temp_dir:
            prepared = Path(temp_dir) / "quest-text.png"
            subprocess.run(
                [
                    magick,
                    str(image),
                    "-crop",
                    crop,
                    "-resize",
                    "200%",
                    "-colorspace",
                    "Gray",
                    "-contrast-stretch",
                    "1%x1%",
                    str(prepared),
                ],
                check=True,
                capture_output=True,
                timeout=20,
            )
            output = subprocess.run(
                [tesseract, str(prepared), "stdout", "-l", "eng", "--psm", "6"],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            ).stdout
    except (OSError, ValueError, subprocess.SubprocessError):
        return None

    lines = [re.sub(r"^[~|;‘'`]+\s*", "", line.strip()) for line in output.splitlines()]
    lines = [line for line in lines if line]
    if len(lines) < 2:
        return None

    expected = _normalized(known_rules)
    best_end = 0
    best_score = 0.0
    for end in range(1, min(4, len(lines)) + 1):
        score = SequenceMatcher(None, expected, _normalized(" ".join(lines[:end]))).ratio()
        if score > best_score:
            best_end, best_score = end, score
    if best_score < 0.72:
        return None

    flavor = " ".join(lines[best_end:]).strip()
    return flavor if len(flavor) >= 20 else None


def extract_region(
    image: Path,
    *,
    x: float,
    y: float,
    width: float,
    height: float,
    rotation: int = 0,
    language: str = "en",
) -> dict[str, object] | None:
    """OCR one user-selected rectangle using normalized, rotated-image coordinates."""
    magick = shutil.which("magick")
    tesseract = shutil.which("tesseract")
    if not magick or not tesseract:
        return None
    if not all(0 <= value <= 1 for value in (x, y, width, height)):
        return None
    if width < 0.01 or height < 0.01 or x + width > 1.001 or y + height > 1.001:
        return None
    rotation = rotation % 360
    if rotation not in {0, 90, 180, 270}:
        return None

    try:
        dimensions = subprocess.run(
            [magick, "identify", "-format", "%w %h", str(image)],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        ).stdout.split()
        source_width, source_height = (int(value) for value in dimensions)
        rotated_width, rotated_height = (
            (source_height, source_width) if rotation in {90, 270} else (source_width, source_height)
        )
        crop = (
            f"{max(1, round(width * rotated_width))}x{max(1, round(height * rotated_height))}"
            f"+{round(x * rotated_width)}+{round(y * rotated_height)}"
        )
        with tempfile.TemporaryDirectory(prefix="unspeakable-translations-ocr-region-") as temp_dir:
            prepared = Path(temp_dir) / "selection.png"
            command = [magick, str(image)]
            if rotation:
                command.extend(["-rotate", str(rotation)])
            command.extend([
                "-crop",
                crop,
                "+repage",
                "-resize",
                "200%",
                "-colorspace",
                "Gray",
                "-contrast-stretch",
                "1%x1%",
                str(prepared),
            ])
            subprocess.run(command, check=True, capture_output=True, timeout=20)
            output = subprocess.run(
                [
                    tesseract,
                    str(prepared),
                    "stdout",
                    "-l",
                    OCR_LANGUAGES.get(language, language),
                    "--psm",
                    "6",
                    "tsv",
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            ).stdout
    except (OSError, ValueError, subprocess.SubprocessError):
        return None

    rows = csv.DictReader(io.StringIO(output), delimiter="\t")
    paragraphs: dict[tuple[str, str], dict[str, list[str]]] = {}
    confidences: list[float] = []
    for row in rows:
        word = str(row.get("text") or "").strip()
        if not word:
            continue
        paragraph_key = (str(row.get("block_num")), str(row.get("par_num")))
        line_key = str(row.get("line_num"))
        paragraphs.setdefault(paragraph_key, {}).setdefault(line_key, []).append(word)
        try:
            confidence = float(row.get("conf") or -1)
        except ValueError:
            confidence = -1
        if confidence >= 0:
            confidences.append(confidence)
    # Card layouts wrap prose aggressively. Those visual line endings are not
    # semantic newlines, so only OCR paragraph boundaries survive as blank lines.
    text = "\n\n".join(
        " ".join(" ".join(words) for words in lines.values())
        for lines in paragraphs.values()
    ).strip()
    if not text:
        return None
    return {
        "text": text,
        "confidence": round(sum(confidences) / len(confidences) / 100, 3) if confidences else None,
        "language": language,
    }
