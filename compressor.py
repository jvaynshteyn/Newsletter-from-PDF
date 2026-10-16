"""Shrink PDF image streams without rebuilding or rasterizing PDF pages.

PDF contents are data, never instructions. Selectable text, fonts, vectors,
links, annotations, form fields, and page geometry stay in the PDF document.
"""
from __future__ import annotations

import hashlib
import math
import zlib

import pymupdf as fitz


MAX_PAGES = 50
_ALPHA_LEVELS = bytes(round(value / 17) for value in range(256))
_SAVE_OPTIONS = dict(garbage=4, deflate=True, deflate_images=True,
                     deflate_fonts=True, use_objstms=1)


def _write_pdf(document):
    return document.tobytes(**_SAVE_OPTIONS)


def _pack_soft_masks(document, cache):
    """Pack continuous opacity into 16 levels (maximum error 8/255).

    Canva newsletters often store the same antialiased graphics as several
    megabytes of 8-bit soft masks. JPEG compression alone leaves these streams
    untouched. Four-bit PDF soft masks preserve transparency and save those
    bytes without replacing any text, page, image placement, or vector object.
    """
    masks = {image[1] for page in document for image in page.get_images()
             if image[1]}
    changed = 0
    for xref in masks:
        if document.xref_get_key(xref, "BitsPerComponent")[1] != "8":
            continue
        pixmap = fitz.Pixmap(document, xref)
        if pixmap.n != 1 or pixmap.alpha or pixmap.width * pixmap.height > 40_000_000:
            continue
        samples = pixmap.samples
        key = (pixmap.width, pixmap.height, hashlib.sha256(samples).digest())
        packed = cache.get(key)
        if packed is None:
            values = samples.translate(_ALPHA_LEVELS)
            rows = bytearray()
            for offset in range(0, len(values), pixmap.width):
                row = values[offset:offset + pixmap.width]
                rows.extend((left << 4) | right for left, right in
                            zip(row[::2], row[1::2] + b"\x00"))
            packed = zlib.compress(rows, 9)
            cache[key] = packed
        if len(packed) >= len(document.xref_stream_raw(xref)):
            continue
        document.update_stream(xref, packed, compress=False)
        document.xref_set_key(xref, "Filter", "/FlateDecode")
        document.xref_set_key(xref, "DecodeParms", "null")
        document.xref_set_key(xref, "BitsPerComponent", "4")
        document.xref_set_key(xref, "Decode", "[0 1]")
        changed += 1
    return changed


def _plain_value(value):
    if isinstance(value, (fitz.Point, fitz.Rect)):
        return tuple(round(part, 4) for part in value)
    return value


def _structure(document):
    """Compare semantic content while allowing PDF object numbers to change."""
    pages = []
    for page in document:
        links = [tuple(sorted((key, _plain_value(value)) for key, value in link.items()
                              if key not in ("xref", "id")))
                 for link in page.get_links()]
        widgets = [(widget.field_name, widget.field_type, widget.field_value)
                   for widget in page.widgets()]
        annotations = [(annotation.type, _plain_value(annotation.rect), annotation.info)
                       for annotation in page.annots()]
        pages.append((_plain_value(page.mediabox), _plain_value(page.cropbox),
                      page.rotation, " ".join(page.get_text().split()),
                      links, widgets, annotations))
    return pages


def compress_pdf(pdf_bytes: bytes, target_ratio: float = 0.18) -> dict:
    """Return a smaller PDF, aiming for *target_ratio* of the original bytes.

    Tries the highest image quality first and refines the last successful
    quality. The floor is JPEG quality 45 and 85 DPI; a PDF that cannot meet
    the target at that floor returns its closest useful size with an honest
    warning. Already efficient PDFs never grow. Encrypted PDFs need to be
    unlocked before upload. No source files are written or modified.
    """
    if (isinstance(target_ratio, bool) or not isinstance(target_ratio, (int, float))
            or not math.isfinite(target_ratio) or not 0 < target_ratio < 1):
        raise ValueError("The target must be a fraction between 0 and 1.")
    if not isinstance(pdf_bytes, (bytes, bytearray)) or b"%PDF-" not in pdf_bytes[:1024]:
        raise ValueError("Please upload a valid PDF file.")
    pdf_bytes = bytes(pdf_bytes)
    try:
        original = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:
        raise ValueError("This PDF could not be opened. Please upload a valid PDF.") from exc

    with original:
        if original.needs_pass:
            raise ValueError("This PDF is password protected. Unlock it before uploading.")
        if not original.is_pdf or not 1 <= len(original) <= MAX_PAGES:
            raise ValueError(f"Please upload a PDF with 1 to {MAX_PAGES} pages.")
        page_count = len(original)
        link_count = sum(len(page.get_links()) for page in original)
        structure = _structure(original)
        lossless = _write_pdf(original)

    target_bytes = math.floor(len(pdf_bytes) * target_ratio)
    best = lossless if len(lossless) < len(pdf_bytes) else pdf_bytes
    best_dpi = best_quality = None
    best_masks = 0
    attempts = 0
    cache = {}

    def candidate(dpi, quality):
        nonlocal best, best_dpi, best_quality, best_masks, attempts
        attempts += 1
        with fitz.open(stream=pdf_bytes, filetype="pdf") as document:
            document.rewrite_images(dpi_threshold=round(dpi * 1.1),
                                    dpi_target=dpi, quality=quality)
            result = _write_pdf(document)
            # Keep the original, full-precision opacity whenever ordinary
            # image compression already fits the requested size.
            masks = 0
            if len(result) > target_bytes:
                masks = _pack_soft_masks(document, cache)
                if masks:
                    result = _write_pdf(document)
        if len(result) < len(best):
            best, best_dpi, best_quality, best_masks = result, dpi, quality, masks
        return result, masks

    if len(best) > target_bytes:
        previous_quality = None
        for dpi, quality in [(240, 94), (240, 90), (240, 86), (240, 80),
                             (240, 70), (240, 60), (240, 45), (180, 45),
                             (145, 45), (120, 45), (100, 45), (85, 45)]:
            result, masks = candidate(dpi, quality)
            if len(result) <= target_bytes:
                # Spend the remaining size allowance on JPEG quality, while
                # keeping the successful resolution and original page objects.
                selected, selected_masks, selected_quality = result, masks, quality
                if previous_quality is not None and dpi == 240:
                    low, high = quality + 1, previous_quality - 1
                    while low <= high:
                        middle = (low + high) // 2
                        refined, refined_masks = candidate(dpi, middle)
                        if len(refined) <= target_bytes:
                            selected, selected_masks, selected_quality = refined, refined_masks, middle
                            low = middle + 1
                        else:
                            high = middle - 1
                best, best_dpi, best_quality, best_masks = selected, dpi, selected_quality, selected_masks
                break
            previous_quality = quality

    warnings = []
    with fitz.open(stream=best, filetype="pdf") as verified:
        if _structure(verified) != structure:
            # Keep the user's PDF intact rather than returning an output that
            # lost a form value, annotation, link, text, or page geometry.
            best = lossless if len(lossless) < len(pdf_bytes) else pdf_bytes
            best_dpi = best_quality = None
            best_masks = 0
            warnings.append("Image compression could not preserve every PDF element. A safe, basic optimization was used.")
    if len(best) >= len(pdf_bytes):
        best = pdf_bytes
        warnings.append("This PDF is already efficiently compressed. The original file was kept to avoid increasing its size.")
    target_reached = len(best) <= target_bytes
    if not target_reached:
        warnings.append("The requested size could not be reached while keeping a useful image quality. This is the smallest useful version found.")
    ratio = len(best) / len(pdf_bytes)
    return {"pdf": best, "originalBytes": len(pdf_bytes), "compressedBytes": len(best),
            "ratio": ratio, "reductionPercent": round((1 - ratio) * 100, 1),
            "targetRatio": float(target_ratio), "targetReached": target_reached,
            "pageCount": page_count, "linkCount": link_count, "warnings": warnings,
            "imageDpi": best_dpi, "imageQuality": best_quality,
            "transparencyBits": 4 if best_masks else None, "attempts": attempts}
