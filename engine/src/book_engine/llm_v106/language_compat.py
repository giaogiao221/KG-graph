from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

LanguageCode = Literal["zh", "en", "mixed"]

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_LATIN_RE = re.compile(r"[A-Za-z]")
_EN_WORD_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9'/-]*\b")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_MD_LINK_RE = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_URL_RE = re.compile(r"https?://\S+", re.I)
_CODE_RE = re.compile(r"`[^`]*`")


@dataclass(frozen=True)
class LanguageProfile:
    language: LanguageCode
    cjk_chars: int
    latin_letters: int
    english_words: int
    cjk_ratio: float
    latin_ratio: float


def _visible_text(text: str) -> str:
    value = str(text or "")
    value = _HTML_TAG_RE.sub(" ", value)
    value = _MD_LINK_RE.sub(r" \1 ", value)
    value = _URL_RE.sub(" ", value)
    value = _CODE_RE.sub(" ", value)
    return value


def detect_language(text: str) -> LanguageProfile:
    """Classify one evidence unit as Chinese, English, or mixed.

    The classifier intentionally ignores HTML/Markdown markup so English table
    tags do not cause Chinese tables to be misclassified. Scientific symbols,
    numbers, abbreviations, and formula characters are neutral.
    """

    visible = _visible_text(text)
    cjk = len(_CJK_RE.findall(visible))
    latin = len(_LATIN_RE.findall(visible))
    words = len(_EN_WORD_RE.findall(visible))
    total = cjk + latin
    if total <= 0:
        # Existing Chinese behavior is the safest default for symbol-only units.
        return LanguageProfile("zh", 0, 0, 0, 0.0, 0.0)

    cjk_ratio = cjk / total
    latin_ratio = latin / total

    # Mixed must have meaningful evidence from both scripts. This avoids
    # classifying Chinese text containing AP/HTPB/RDX as mixed merely because of
    # a few domain abbreviations.
    if cjk >= 2 and latin >= 12 and words >= 3 and cjk_ratio >= 0.04 and latin_ratio >= 0.12:
        language: LanguageCode = "mixed"
    elif latin >= 12 and words >= 3 and latin_ratio >= 0.72:
        language = "en"
    else:
        language = "zh"
    return LanguageProfile(language, cjk, latin, words, cjk_ratio, latin_ratio)


def is_english_like(language: str) -> bool:
    return str(language or "").lower() in {"en", "mixed"}


__all__ = ["LanguageCode", "LanguageProfile", "detect_language", "is_english_like"]
