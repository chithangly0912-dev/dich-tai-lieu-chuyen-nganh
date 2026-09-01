"""
Core layout-preserving PDF translation pipeline.

Strategy (best-effort layout preservation, not perfect DTP-grade reflow):

1. Extract text *blocks* per page via PyMuPDF's structured text dict, each
   with its bounding box, dominant font size/color/weight, and a guessed
   text alignment.
2. Auto-detect the source language (English vs. Chinese) from the first
   few pages.
3. Translate each block's text as one fragment via DeepSeek (skipping
   blocks that contain no letters at all, e.g. lone page numbers).
4. For each page: sample background colors under each text block (so
   redacting doesn't leave odd colored boxes on shaded table cells),
   redact (blank out) only the original text - images and vector
   graphics/table borders outside the tight text boxes are left alone -
   then re-insert the Vietnamese translation into the same box, shrinking
   the font size as needed so it fits.

Known limitations (documented in README):
- Complex multi-column reflow, rotated/vertical text, and text embedded
  inside images (e.g. scanned figures) are not handled.
- Heavily shaded/patterned cell backgrounds may not be perfectly matched.
- Vietnamese text is often ~20-40% longer than English/Chinese source
  text; the auto-shrink keeps it inside the original box but very dense
  source pages may end up with noticeably smaller translated text.
"""
from __future__ import annotations

import os
import statistics
from dataclasses import dataclass, field
from typing import Callable, Optional

import pymupdf as fitz

from lang_detect import detect_document_language, detect_language
from deepseek_client import DeepSeekClient

_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))

FONT_REGULAR = "NotoSans-Regular"
FONT_BOLD = "NotoSans-Bold"
FONT_REGULAR_FILE = os.path.join(_BACKEND_DIR, "fonts", "NotoSans-Regular.ttf")
FONT_BOLD_FILE = os.path.join(_BACKEND_DIR, "fonts", "NotoSans-Bold.ttf")

MIN_FONT_SIZE = 5.0
MAX_FONT_SIZE = 28.0
FONT_SHRINK_STEP = 0.5

ProgressCB = Optional[Callable[[str, int, int, str], None]]


@dataclass
class TextBlock:
    page_index: int
    bbox: tuple
    text: str
    font_size: float
    color: tuple
    bold: bool
    align: int
    translatable: bool
    translated_text: str = ""


def _int_to_rgb(color_int: int) -> tuple:
    r = ((color_int >> 16) & 0xFF) / 255.0
    g = ((color_int >> 8) & 0xFF) / 255.0
    b = (color_int & 0xFF) / 255.0
    return (r, g, b)


def _has_letters(text: str) -> bool:
    return any(ch.isalpha() for ch in text)


def _guess_align(lines: list[dict], bbox: tuple) -> int:
    x0b, _, x1b, _ = bbox
    width = x1b - x0b
    if width <= 0 or not lines:
        return fitz.TEXT_ALIGN_LEFT

    left_margins = []
    right_margins = []
    for line in lines:
        spans = line.get("spans", [])
        if not spans:
            continue
        lx0 = min(s["bbox"][0] for s in spans)
        lx1 = max(s["bbox"][2] for s in spans)
        left_margins.append((lx0 - x0b) / width)
        right_margins.append((x1b - lx1) / width)

    if not left_margins:
        return fitz.TEXT_ALIGN_LEFT

    avg_left = statistics.mean(left_margins)
    avg_right = statistics.mean(right_margins)

    if avg_left < 0.04:
        return fitz.TEXT_ALIGN_LEFT
    if avg_right < 0.04:
        return fitz.TEXT_ALIGN_RIGHT
    if abs(avg_left - avg_right) < 0.06:
        return fitz.TEXT_ALIGN_CENTER
    return fitz.TEXT_ALIGN_LEFT


def _line_y_range(line: dict) -> tuple:
    spans = line.get("spans", [])
    if spans:
        y0 = min(s["bbox"][1] for s in spans)
        y1 = max(s["bbox"][3] for s in spans)
    else:
        bbox = line.get("bbox", (0, 0, 0, 0))
        y0, y1 = bbox[1], bbox[3]
    return y0, y1


def _y_overlap_ratio(r1: tuple, r2: tuple) -> float:
    inter = min(r1[1], r2[1]) - max(r1[0], r2[0])
    min_h = min(r1[1] - r1[0], r2[1] - r2[0])
    if min_h <= 0:
        return 0.0
    return max(inter, 0.0) / min_h


def _cluster_rows(lines: list[dict]) -> list[list[dict]]:
    """Group a MuPDF block's lines into visual "rows": lines that sit at
    the same vertical position (i.e. side-by-side, like table columns
    MuPDF lumped into one block) end up in the same row. A row with more
    than one line is treated later as separate cells rather than merged
    paragraph text - this is what keeps table columns from being
    concatenated into one garbled string.
    """
    rows: list[list[dict]] = []
    cur_range: tuple | None = None
    for line in lines:
        y_range = _line_y_range(line)
        if rows and cur_range is not None and _y_overlap_ratio(cur_range, y_range) > 0.4:
            rows[-1].append(line)
            cur_range = (min(cur_range[0], y_range[0]), max(cur_range[1], y_range[1]))
        else:
            rows.append([line])
            cur_range = y_range
    return rows


def _make_block(lines: list[dict], page_index: int) -> TextBlock | None:
    line_texts = []
    sizes, colors, bold_flags = [], [], []
    x0s, y0s, x1s, y1s = [], [], [], []
    for line in lines:
        spans = line.get("spans", [])
        line_text = "".join(s.get("text", "") for s in spans)
        if not line_text.strip():
            continue
        line_texts.append(line_text)
        for s in spans:
            sizes.append(s.get("size", 10.0))
            colors.append(s.get("color", 0))
            bold_flags.append(bool(s.get("flags", 0) & 2**4))
            sx0, sy0, sx1, sy1 = s["bbox"]
            x0s.append(sx0); y0s.append(sy0); x1s.append(sx1); y1s.append(sy1)

    if not line_texts:
        return None

    bbox = (min(x0s), min(y0s), max(x1s), max(y1s))
    # Join with a space rather than a newline: within one visual block the
    # line breaks are just word-wrap artifacts of the ORIGINAL layout
    # (e.g. "...flow rate is 45 / L/min..." wrapped mid-value). Sending a
    # single flowing string to translation avoids baking those arbitrary
    # break points into the Vietnamese text, and insert_textbox re-wraps
    # the result to fit the box on its own.
    text = " ".join(line_texts)
    font_size = statistics.mean(sizes) if sizes else 10.0
    dominant_color = max(set(colors), key=colors.count) if colors else 0
    bold = (sum(bold_flags) / len(bold_flags)) > 0.5 if bold_flags else False
    align = _guess_align(lines, bbox)

    return TextBlock(
        page_index=page_index,
        bbox=bbox,
        text=text,
        font_size=min(max(font_size, MIN_FONT_SIZE), MAX_FONT_SIZE),
        color=_int_to_rgb(dominant_color),
        bold=bold,
        align=align,
        translatable=_has_letters(text),
    )


def extract_page_blocks(page: "fitz.Page", page_index: int) -> list[TextBlock]:
    text_dict = page.get_text("dict", sort=True)
    units: list[TextBlock] = []

    for b in text_dict.get("blocks", []):
        if b.get("type") != 0:  # 0 = text, 1 = image
            continue
        raw_lines = b.get("lines", [])
        lines = [
            l for l in raw_lines
            if "".join(s.get("text", "") for s in l.get("spans", [])).strip()
        ]
        if not lines:
            continue

        rows = _cluster_rows(lines)

        pending: list[dict] = []

        def flush_paragraph() -> None:
            if pending:
                blk = _make_block(list(pending), page_index)
                if blk:
                    units.append(blk)
                pending.clear()

        for row in rows:
            if len(row) == 1:
                # Could be a genuine paragraph-wrap line, or a lone cell -
                # accumulate consecutive single-line rows and merge them
                # into one translation unit for better sentence context.
                pending.append(row[0])
            else:
                # Multiple lines at the same vertical position: MuPDF
                # lumped separate columns/cells into one block. Flush any
                # pending paragraph first, then emit each cell as its own
                # independent unit so columns don't get concatenated.
                flush_paragraph()
                for line in row:
                    blk = _make_block([line], page_index)
                    if blk:
                        units.append(blk)
        flush_paragraph()

    return units


def _sample_bg_color(pix: "fitz.Pixmap", bbox: tuple, zoom: float) -> tuple:
    """Best-effort background color sample from just outside the block's
    corners, falling back to white if anything goes wrong."""
    try:
        x0, y0, x1, y1 = bbox
        pad = 2
        pts = [
            (x0 * zoom - pad, y0 * zoom - pad),
            (x1 * zoom + pad, y0 * zoom - pad),
            (x0 * zoom - pad, y1 * zoom + pad),
            (x1 * zoom + pad, y1 * zoom + pad),
        ]
        samples = []
        for px, py in pts:
            ix = min(max(int(px), 0), pix.width - 1)
            iy = min(max(int(py), 0), pix.height - 1)
            pixel = pix.pixel(ix, iy)
            samples.append(tuple(pixel[:3]))
        # Prefer the lightest sample (less likely to be glyph ink that
        # leaked into the sampling point).
        best = max(samples, key=lambda c: sum(c))
        return (best[0] / 255.0, best[1] / 255.0, best[2] / 255.0)
    except Exception:
        return (1.0, 1.0, 1.0)


def _insert_fitted_text(page: "fitz.Page", bbox: tuple, text: str, block: TextBlock) -> None:
    fontfile = FONT_BOLD_FILE if block.bold else FONT_REGULAR_FILE
    fontname = FONT_BOLD if block.bold else FONT_REGULAR

    size = block.font_size
    rect = fitz.Rect(bbox)
    # Give a little breathing room since translated Vietnamese text tends
    # to run longer than the source.
    rect = fitz.Rect(rect.x0, rect.y0, rect.x1, rect.y1 + 2)

    while size >= MIN_FONT_SIZE:
        overflow = page.insert_textbox(
            rect,
            text,
            fontsize=size,
            fontname=fontname,
            fontfile=fontfile,
            color=block.color,
            align=block.align,
            lineheight=1.15,
        )
        if overflow >= 0:
            return
        size -= FONT_SHRINK_STEP

    # Last resort: insert at the minimum size even if it slightly overflows,
    # rather than silently dropping the translated text.
    page.insert_textbox(
        rect,
        text,
        fontsize=MIN_FONT_SIZE,
        fontname=fontname,
        fontfile=fontfile,
        color=block.color,
        align=block.align,
        lineheight=1.15,
    )


def translate_pdf(
    input_path: str,
    output_path: str,
    api_key: str,
    on_progress: ProgressCB = None,
    deepseek_base_url: str | None = None,
    deepseek_model: str | None = None,
) -> dict:
    def report(stage: str, current: int, total: int, message: str = "") -> None:
        if on_progress:
            on_progress(stage, current, total, message)

    report("opening", 0, 1, "Đang mở file PDF...")
    doc = fitz.open(input_path)
    page_count = doc.page_count

    # --- 1. Detect source language from the first few pages ---
    report("detecting_language", 0, 1, "Đang phát hiện ngôn ngữ nguồn...")
    sample_texts = [doc[i].get_text("text") for i in range(min(5, page_count))]
    source_lang = detect_document_language(sample_texts)

    # --- 2. Extract blocks for every page ---
    report("extracting", 0, page_count, "Đang trích xuất bố cục PDF...")
    all_blocks: list[TextBlock] = []
    for i in range(page_count):
        page = doc[i]
        # Re-check language per page for mixed documents; only used to
        # pick the translation prompt language per block.
        page_blocks = extract_page_blocks(page, i)
        all_blocks.extend(page_blocks)
        report("extracting", i + 1, page_count, f"Đã trích xuất trang {i + 1}/{page_count}")

    translatable_blocks = [b for b in all_blocks if b.translatable and b.text.strip()]
    for b in all_blocks:
        if not b.translatable:
            b.translated_text = b.text  # leave numbers/symbols as-is

    # --- 3. Translate ---
    client = DeepSeekClient(api_key=api_key, base_url=deepseek_base_url or "https://api.deepseek.com",
                             model=deepseek_model or "deepseek-chat")

    total_blocks = len(translatable_blocks)
    report("translating", 0, total_blocks, "Đang dịch nội dung...")

    texts = [b.text for b in translatable_blocks]
    # Per-block source language: use the block's own text to decide when
    # the document mixes English and Chinese; fall back to document-level
    # detection for very short fragments where the heuristic is unreliable.
    per_block_lang = [detect_language(t) if len(t) >= 8 else source_lang for t in texts]

    # Bucket by language so each grouped API call only ever asks the model
    # to translate one source language at a time, then translate each
    # bucket with batching (several fragments per call) for speed, while
    # keeping every fragment's result mapped back to its original index.
    translated_texts: list[str] = [""] * total_blocks
    overall_done = 0

    def _progress_cb(done_in_bucket: int, total_in_bucket: int) -> None:
        # `done_in_bucket` is cumulative within the current bucket; report
        # progress against the whole document's block count.
        report(
            "translating",
            overall_done + done_in_bucket,
            total_blocks,
            f"Đã dịch {overall_done + done_in_bucket}/{total_blocks} đoạn văn bản",
        )

    for lang in ("en", "zh"):
        bucket_indices = [i for i, l in enumerate(per_block_lang) if l == lang]
        if not bucket_indices:
            continue
        bucket_texts = [texts[i] for i in bucket_indices]
        bucket_translated = client.translate_batch_grouped(
            bucket_texts, lang, progress_cb=_progress_cb
        )
        for i, tr in zip(bucket_indices, bucket_translated):
            translated_texts[i] = tr
        overall_done += len(bucket_indices)

    for blk, translated in zip(translatable_blocks, translated_texts):
        blk.translated_text = translated

    # --- 4. Rebuild pages: redact original text, insert translation ---
    report("rebuilding", 0, page_count, "Đang dựng lại file PDF...")
    blocks_by_page: dict[int, list[TextBlock]] = {}
    for b in all_blocks:
        blocks_by_page.setdefault(b.page_index, []).append(b)

    if not doc.is_form_pdf:
        pass  # no-op, placeholder for future form-field handling

    zoom = 2.0
    for i in range(page_count):
        page = doc[i]
        page_blocks = blocks_by_page.get(i, [])
        if not page_blocks:
            report("rebuilding", i + 1, page_count, f"Trang {i + 1}/{page_count} (không có văn bản)")
            continue

        # Render once before any redactions for background color sampling.
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))

        for b in page_blocks:
            fill = _sample_bg_color(pix, b.bbox, zoom)
            page.add_redact_annot(fitz.Rect(b.bbox), fill=fill, cross_out=False)

        page.apply_redactions(images=0, graphics=1, text=0)

        for b in page_blocks:
            content = b.translated_text if b.translated_text else b.text
            if not content.strip():
                continue
            _insert_fitted_text(page, b.bbox, content, b)

        report("rebuilding", i + 1, page_count, f"Đã dựng lại trang {i + 1}/{page_count}")

    report("saving", 0, 1, "Đang lưu file PDF kết quả...")
    doc.save(output_path, garbage=4, deflate=True)
    doc.close()
    report("done", 1, 1, "Hoàn tất.")

    return {
        "source_lang": source_lang,
        "page_count": page_count,
        "block_count": len(all_blocks),
        "translated_block_count": total_blocks,
    }
