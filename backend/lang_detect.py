"""
Language auto-detection: distinguishes English vs. Chinese source text.

The app only needs to tell EN and ZH apart (per the product spec), so a
lightweight Unicode-range heuristic is both simpler and more reliable here
than a general-purpose statistical language detector (which tends to
misfire on short, jargon-heavy technical strings, numbers, and mixed
Latin/CJK snippets that are common in specialized PDFs).
"""
from __future__ import annotations

import re

# CJK Unified Ideographs + common extensions actually used in Simplified/
# Traditional Chinese technical documents.
_CJK_RANGES = (
    (0x4E00, 0x9FFF),   # CJK Unified Ideographs
    (0x3400, 0x4DBF),   # CJK Extension A
    (0xF900, 0xFAFF),   # CJK Compatibility Ideographs
    (0x2E80, 0x2EFF),   # CJK Radicals Supplement
    (0x3000, 0x303F),   # CJK punctuation (、。「」etc.)
    (0xFF00, 0xFFEF),   # Fullwidth forms
)

_LATIN_RE = re.compile(r"[A-Za-z]")


def _is_cjk(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _CJK_RANGES)


def detect_language(text: str) -> str:
    """Return 'zh' or 'en' for a chunk of text.

    Heuristic: count CJK ideographs vs. Latin letters. If CJK characters
    make up a meaningful share of the "meaningful" characters (ideographs +
    Latin letters, ignoring digits/punctuation/whitespace), classify as
    Chinese; otherwise English. Empty/ambiguous text defaults to 'en'.
    """
    if not text:
        return "en"

    cjk_count = sum(1 for ch in text if _is_cjk(ch))
    latin_count = len(_LATIN_RE.findall(text))

    meaningful = cjk_count + latin_count
    if meaningful == 0:
        return "en"

    # Even a modest fraction of CJK ideographs is a strong signal, since
    # Chinese technical docs often mix in Latin abbreviations, model
    # numbers, units, etc.
    if cjk_count / meaningful >= 0.15:
        return "zh"
    return "en"


def detect_document_language(page_texts: list[str], sample_pages: int = 5) -> str:
    """Detect the dominant source language across the first few pages of a
    document, by pooling text and voting rather than trusting a single
    page (title pages / cover pages can be misleading).
    """
    sample = page_texts[:sample_pages] if page_texts else []
    pooled = "\n".join(sample)
    return detect_language(pooled)
