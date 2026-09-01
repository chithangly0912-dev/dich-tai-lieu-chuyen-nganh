"""
DeepSeek translation client.

DeepSeek's API is OpenAI-compatible (POST {base_url}/chat/completions), so
this talks to it with plain `requests` rather than pulling in the `openai`
SDK — one less fast-moving dependency, and the REST surface we need is
tiny.

The prompt is written for *specialized / technical* source documents
(engineering specs, datasheets, manuals, contracts, academic papers, ...):
it asks the model to preserve terminology consistency, numbers, units,
codes/model numbers, and not to "helpfully" expand or explain anything —
just translate.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field

import requests

DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

_SRC_LANG_NAMES = {"en": "English", "zh": "Chinese"}

_SYSTEM_PROMPT = """You are a professional technical translator working for a \
specialized-document translation service. You translate {src_lang} text into \
natural, precise Vietnamese for professional/technical documents (engineering, \
manufacturing, scientific, legal, or business specifications).

Rules:
- Translate the given text fragment into Vietnamese. Output ONLY the \
translation, nothing else (no notes, no quotes, no explanations).
- Preserve the original meaning precisely; this is technical/specialized \
content, so prioritize terminological accuracy over fluency embellishment.
- Keep numbers, units, physical quantities, model/part numbers, codes, \
formulas, and proper nouns unchanged unless a standard Vietnamese \
technical term/unit exists.
- Keep a consistent glossary of technical terms across fragments (e.g. \
always translate the same source term the same way).
- Preserve line breaks inside the fragment where they separate distinct \
items (e.g. list items, table cells) — do not merge them into one sentence.
- If the fragment is not real sentence content (e.g. a lone page number, a \
figure/table label like "Fig. 3", a standalone unit symbol, or gibberish \
from OCR), translate what is translatable and leave the rest as-is; never \
invent content that is not in the source.
- Do not translate proper brand names or standard international codes \
(ISO, IEC, ASTM, part numbers, chemical formulas, etc.).
"""

_BATCH_SYSTEM_PROMPT = """You are a professional technical translator working for a \
specialized-document translation service. You translate {src_lang} text into \
natural, precise Vietnamese for professional/technical documents (engineering, \
manufacturing, scientific, legal, or business specifications).

You will receive a JSON array of independent text fragments, extracted from \
different regions of a PDF page (paragraphs, table cells, labels, captions). \
Translate EACH fragment into Vietnamese independently.

Rules:
- Respond with ONLY a single JSON array of strings, in the exact same order, \
with EXACTLY the same number of elements as the input array. No other text, \
no markdown code fences, no explanations.
- Element i of your output must be the Vietnamese translation of element i of \
the input. Never merge two fragments into one, never split one fragment into \
two, never drop a fragment.
- Preserve the original meaning precisely; this is technical/specialized \
content, so prioritize terminological accuracy over fluency embellishment.
- Keep numbers, units, physical quantities, model/part numbers, codes, \
formulas, and proper nouns unchanged unless a standard Vietnamese technical \
term/unit exists.
- Keep a consistent glossary of technical terms across fragments (translate \
the same source term the same way every time it appears).
- If a fragment is not real sentence content (e.g. a lone page number, a \
figure/table label like "Fig. 3", a standalone unit symbol, or gibberish \
from OCR), translate what is translatable and leave the rest as-is; never \
invent content that is not in the source. An empty-ish fragment can be \
returned unchanged.
- Do not translate proper brand names or standard international codes \
(ISO, IEC, ASTM, part numbers, chemical formulas, etc.).
"""


def _extract_json_array(content: str) -> list | None:
    """Pull a JSON array of strings out of a model response, tolerating
    markdown code fences or stray leading/trailing text."""
    content = content.strip()
    # Strip ```json ... ``` or ``` ... ``` fences if present.
    fence_match = re.match(r"^```(?:json)?\s*(.*?)\s*```$", content, re.DOTALL)
    if fence_match:
        content = fence_match.group(1).strip()

    start = content.find("[")
    end = content.rfind("]")
    if start == -1 or end == -1 or end < start:
        return None
    candidate = content[start : end + 1]
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, list):
        return None
    if not all(isinstance(x, str) for x in parsed):
        return None
    return parsed


@dataclass
class TranslationError(Exception):
    message: str
    status_code: int | None = None

    def __str__(self) -> str:
        return self.message


@dataclass
class DeepSeekClient:
    api_key: str = field(default_factory=lambda: os.environ.get("DEEPSEEK_API_KEY", ""))
    base_url: str = DEEPSEEK_BASE_URL
    model: str = DEEPSEEK_MODEL
    max_retries: int = 4
    timeout: float = 60.0

    def __post_init__(self) -> None:
        if not self.api_key:
            raise TranslationError(
                "Thiếu DEEPSEEK_API_KEY. Hãy đặt biến môi trường DEEPSEEK_API_KEY "
                "(xem file .env.example) trước khi chạy dịch."
            )

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def translate_fragment(self, text: str, source_lang: str, glossary_hint: str = "") -> str:
        """Translate a single text fragment (a block, a table cell group,
        etc.) from `source_lang` ('en'|'zh') into Vietnamese.
        """
        text = text.strip()
        if not text:
            return text

        src_lang_name = _SRC_LANG_NAMES.get(source_lang, "English")
        system_prompt = _SYSTEM_PROMPT.format(src_lang=src_lang_name)
        if glossary_hint:
            system_prompt += f"\n\nGlossary consistency hint (terms already used earlier in this document): {glossary_hint}\n"

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text},
            ],
            "temperature": 0.2,
            "stream": False,
        }

        url = f"{self.base_url.rstrip('/')}/chat/completions"
        last_err: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = requests.post(url, headers=self._headers(), json=payload, timeout=self.timeout)
            except requests.RequestException as exc:
                last_err = exc
                time.sleep(min(2 ** attempt, 10))
                continue

            if resp.status_code == 200:
                data = resp.json()
                try:
                    return data["choices"][0]["message"]["content"].strip()
                except (KeyError, IndexError) as exc:
                    raise TranslationError(f"Phản hồi DeepSeek không đúng định dạng: {exc}") from exc

            if resp.status_code in (429, 500, 502, 503, 504):
                # Transient — back off and retry.
                last_err = TranslationError(
                    f"DeepSeek API lỗi tạm thời (HTTP {resp.status_code}): {resp.text[:300]}",
                    status_code=resp.status_code,
                )
                time.sleep(min(2 ** attempt, 15))
                continue

            # Non-retryable (4xx auth/validation errors, etc.)
            raise TranslationError(
                f"DeepSeek API trả về lỗi (HTTP {resp.status_code}): {resp.text[:500]}",
                status_code=resp.status_code,
            )

        raise TranslationError(f"Không thể gọi DeepSeek API sau {self.max_retries} lần thử: {last_err}")

    def translate_batch(self, texts: list[str], source_lang: str, progress_cb=None) -> list[str]:
        """Translate a list of independent fragments sequentially, keeping
        a small rolling glossary hint (recently-seen source->translated
        term pairs are NOT tracked explicitly here — DeepSeek is simply
        given the previous fragment's translation as light context) so
        terminology stays consistent across a document.
        """
        results: list[str] = []
        recent_hint = ""
        total = len(texts)
        for i, text in enumerate(texts):
            translated = self.translate_fragment(text, source_lang, glossary_hint=recent_hint)
            results.append(translated)
            # Keep a short rolling hint from the last non-trivial fragment.
            if len(text) > 20:
                recent_hint = f"\"{text[:80]}\" -> \"{translated[:80]}\""
            if progress_cb:
                progress_cb(i + 1, total)
        return results

    def _translate_group(self, texts: list[str], source_lang: str) -> list[str] | None:
        """Translate a small group of fragments in ONE API call via a JSON
        array in, JSON array out contract. Returns None (signalling the
        caller to fall back to one-by-one translation) if the model's
        response can't be parsed or doesn't line up 1:1 with the input.
        """
        src_lang_name = _SRC_LANG_NAMES.get(source_lang, "English")
        system_prompt = _BATCH_SYSTEM_PROMPT.format(src_lang=src_lang_name)
        user_content = (
            f"Translate this JSON array of {len(texts)} text fragment(s). "
            f"Respond with ONLY a JSON array of exactly {len(texts)} translated "
            f"string(s), in the same order:\n{json.dumps(texts, ensure_ascii=False)}"
        )

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0.2,
            "stream": False,
        }

        url = f"{self.base_url.rstrip('/')}/chat/completions"
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = requests.post(url, headers=self._headers(), json=payload, timeout=self.timeout)
            except requests.RequestException:
                time.sleep(min(2 ** attempt, 10))
                continue

            if resp.status_code == 200:
                data = resp.json()
                try:
                    content = data["choices"][0]["message"]["content"]
                except (KeyError, IndexError):
                    return None
                parsed = _extract_json_array(content)
                if parsed is not None and len(parsed) == len(texts):
                    return parsed
                return None  # malformed / misaligned -> caller falls back

            if resp.status_code in (429, 500, 502, 503, 504):
                time.sleep(min(2 ** attempt, 15))
                continue

            raise TranslationError(
                f"DeepSeek API trả về lỗi (HTTP {resp.status_code}): {resp.text[:500]}",
                status_code=resp.status_code,
            )

        return None

    def translate_batch_grouped(
        self,
        texts: list[str],
        source_lang: str,
        batch_char_budget: int = 2500,
        batch_max_items: int = 25,
        progress_cb=None,
    ) -> list[str]:
        """Translate many independent fragments efficiently by grouping
        them into a handful of items per API call (bounded by both item
        count and total character budget, since technical fragments vary
        wildly in length - a page full of short table cells and a page of
        dense paragraphs should both produce reasonably-sized requests).

        Falls back to translating a group's fragments one-by-one whenever
        the grouped call fails to parse or return a matching count, so a
        single malformed response never loses or corrupts a fragment.
        """
        n = len(texts)
        results: list[str] = [""] * n
        if n == 0:
            return results

        groups: list[list[int]] = []
        current: list[int] = []
        current_len = 0
        for i, t in enumerate(texts):
            if current and (current_len + len(t) > batch_char_budget or len(current) >= batch_max_items):
                groups.append(current)
                current = []
                current_len = 0
            current.append(i)
            current_len += len(t)
        if current:
            groups.append(current)

        done = 0
        for idx_group in groups:
            group_texts = [texts[i] for i in idx_group]
            translated = None
            if len(group_texts) > 1:
                translated = self._translate_group(group_texts, source_lang)
            # Defensive check (not just trusting _translate_group's own
            # validation): a misaligned or short result must never get
            # zipped against the original indices, or fragments would
            # silently end up empty or mismatched.
            if translated is None or len(translated) != len(group_texts):
                translated = self.translate_batch(group_texts, source_lang)
            for i, tr in zip(idx_group, translated):
                results[i] = tr
            done += len(idx_group)
            if progress_cb:
                progress_cb(done, n)
        return results
