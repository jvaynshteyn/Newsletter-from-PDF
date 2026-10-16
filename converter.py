"""Convert a PDF into small, editable, email-friendly HTML.

PDF objects are treated as data: only text, rendered image regions, and safe
external link annotations are used. No document action or embedded code runs.
"""
from __future__ import annotations

import base64
import html
import re
import statistics
from collections import defaultdict
from urllib.parse import urlsplit

import pymupdf as fitz

try:
    import zxingcpp
    if not hasattr(zxingcpp, "read_barcodes"):
        zxingcpp = None
except ImportError:
    zxingcpp = None


LINK_STYLE = "color:#125fa2;text-decoration:underline;"
BODY_STYLE = "margin:0 0 14px;font-size:16px;line-height:1.6;color:#273449;"
_URL = re.compile(r"https?://[^\s<>]+|www\.[^\s<>]+|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|\b(?:[A-Za-z0-9-]+\.)+(?:org|com|edu|net|gov)(?:/[^\s<>]*)?", re.I)
_BULLET = re.compile(r"^\s*(?:[•●▪◦‣\u2022-]|\d+[.)])\s+")


def safe_url(value):
    """Return a safe, absolute external URL, or None for unsupported actions."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or any(ord(c) < 32 or ord(c) == 127 for c in value) or "\\" in value:
        return None
    try:
        parts = urlsplit(value)
        if parts.scheme.lower() in ("http", "https") and parts.hostname:
            # Reading port also rejects malformed authorities.
            parts.port
            return value
        if parts.scheme.lower() in ("mailto", "tel") and parts.path:
            return value
    except (ValueError, TypeError):
        pass
    return None


def _clean(text):
    return text.replace("\x00", "").replace("\u00ad", "").replace("\ufb01", "fi").replace("\ufb02", "fl")


def _key(text):
    return re.sub(r"\s+", " ", _clean(text)).strip()


def _union(rects):
    rects = list(rects)
    return fitz.Rect(min(r.x0 for r in rects), min(r.y0 for r in rects), max(r.x1 for r in rects), max(r.y1 for r in rects))


def _intersection(a, b):
    return max(0, min(a.x1, b.x1) - max(a.x0, b.x0)) * max(0, min(a.y1, b.y1) - max(a.y0, b.y0))


def _line_data(page):
    raw = page.get_text("rawdict", flags=fitz.TEXT_PRESERVE_LIGATURES | fitz.TEXT_PRESERVE_WHITESPACE)
    lines, duplicates = [], 0
    by_text = defaultdict(list)
    for bi, block in enumerate(raw["blocks"]):
        for line in block.get("lines", []):
            chars = []
            for span in line["spans"]:
                for char in span["chars"]:
                    chars.append({"text": _clean(char["c"]), "rect": fitz.Rect(char["bbox"]), "size": span["size"], "bold": bool(span["flags"] & 16), "italic": bool(span["flags"] & 2)})
            text = "".join(c["text"] for c in chars)
            norm, rect = _key(text), fitz.Rect(line["bbox"])
            if not norm:
                continue
            if any(abs(rect.x0-r.x0) < 2.2 and abs(rect.y0-r.y0) < 2.2 and abs(rect.x1-r.x1) < 3 for r in by_text[norm]):
                duplicates += 1
                continue
            by_text[norm].append(rect)
            lines.append({"text": text, "chars": chars, "rect": rect, "block": bi, "size": statistics.median(c["size"] for c in chars), "bold": sum(c["bold"] for c in chars) > len(chars)/2})
    return lines, duplicates


def _page_links(page, lines):
    result, unsafe = [], 0
    for link in page.get_links():
        uri = link.get("uri")
        # Internal page jumps, file launch actions, and JS never become HTML.
        if not uri:
            if link.get("kind") not in (fitz.LINK_GOTO, fitz.LINK_NONE):
                unsafe += 1
            continue
        url = safe_url(uri)
        if not url:
            unsafe += 1
            continue
        rect = fitz.Rect(link["from"])
        if any(l["url"] == url and all(abs(a-b) < .5 for a,b in zip(l["rect"], rect)) for l in result):
            continue
        labels = [l["text"].strip() for l in lines if _intersection(rect, l["rect"]) / max(l["rect"].get_area(), 1) > .45]
        actions = [label for label in labels if re.search(r"learn more|rsvp|support|volunteer|register|donate|click|visit", label, re.I)]
        label = min(actions, key=len) if actions else (" ".join(labels)[:120] or url)
        result.append({"url": url, "label": label, "rect": rect, "mapped": False})
    return result, unsafe


def _char_link(char, links, mark=False):
    r = char["rect"]
    if not char["text"].strip():
        return None
    candidates = [link for link in links if _intersection(r, link["rect"]) / max(r.get_area(), .1) > .35]
    if candidates:
        match = min(candidates, key=lambda x: x["rect"].get_area())
        if mark:
            match["mapped"] = True
        return match["url"]
    return None


def _render_runs(chars, links):
    urls = [_char_link(char, links, mark=True) for char in chars]
    # Annotation rectangles apply to words, but spaces do not have useful
    # geometry. A space between words in the same link belongs to that link.
    index = 0
    while index < len(chars):
        if chars[index]["text"].strip():
            index += 1
            continue
        start = index
        while index < len(chars) and not chars[index]["text"].strip():
            index += 1
        left = urls[start-1] if start else None
        right = urls[index] if index < len(chars) else None
        if left and left == right:
            urls[start:index] = [left]*(index-start)
    links_groups = []
    for char, url in zip(chars, urls):
        if links_groups and links_groups[-1][0] == url:
            links_groups[-1][1].append(char)
        else:
            links_groups.append([url, [char]])
    output = []
    for url, link_chars in links_groups:
        styles = []
        for char in link_chars:
            style = (char["bold"], char["italic"])
            if styles and styles[-1][0] == style:
                styles[-1][1] += char["text"]
            else:
                styles.append([style, char["text"]])
        content = []
        for (bold, italic), text in styles:
            escaped = html.escape(text)
            if bold:
                escaped = "<strong>"+escaped+"</strong>"
            if italic:
                escaped = "<em>"+escaped+"</em>"
            content.append(escaped)
        markup = "".join(content)
        if url:
            markup = '<a href="'+html.escape(url,quote=True)+'" style="'+LINK_STYLE+'">'+markup+'</a>'
        output.append(markup)
    return "".join(output)


def _autolink_chars(lines, links, page_number, detected):
    for line in lines:
        text = "".join(c["text"] for c in line["chars"])
        for match in _URL.finditer(text):
            label = match.group().rstrip(".,;:!?)")
            if not label:
                continue
            subset = line["chars"][match.start():match.start()+len(label)]
            if not subset or any(_char_link(c, links) for c in subset):
                continue
            url = safe_url(label if label.lower().startswith(("http://", "https://")) else ("mailto:"+label if "@" in label else "https://"+label))
            if url:
                link = {"url": url, "label": label, "rect": _union(c["rect"] for c in subset), "mapped": False, "source": "text"}
                links.append(link)
                detected.append({"url": url, "label": label, "page": page_number, "source": "text"})


def _paragraphs(lines, body_size):
    # Preserve PDF paragraph boundaries, splitting blocks that contain columns.
    by_block = defaultdict(list)
    for line in lines:
        by_block[line["block"]].append(line)
    items = []
    for block_lines in by_block.values():
        block_lines.sort(key=lambda l: (l["rect"].y0, l["rect"].x0))
        groups = []
        for line in block_lines:
            if groups:
                prev = groups[-1][-1]
                gap = line["rect"].y0-prev["rect"].y1
                same_column = abs(line["rect"].x0-prev["rect"].x0) < max(14, body_size)
                same_size = abs(line["size"]-prev["size"]) < 2
                # Widely spaced rows and bullets stay individually editable.
                continuous = gap < max(7, body_size*.65) and gap > -prev["rect"].height*.55
                if same_column and same_size and continuous and not _BULLET.match(line["text"]):
                    groups[-1].append(line)
                    continue
            groups.append([line])
        for group in groups:
            rect = _union(l["rect"] for l in group)
            items.append({"kind":"text", "rect": rect, "lines": group, "size": max(l["size"] for l in group)})
    # A drop-shadow exporter may put each line of one large title in a
    # different PDF block. Rejoin only matching, vertically adjacent titles.
    items.sort(key=lambda i:(i["rect"].y0,i["rect"].x0))
    merged = []
    for item in items:
        previous = next((p for p in reversed(merged) if p["size"] >= 27 and item["size"] >= 27 and abs(p["size"]-item["size"])<2 and abs(p["rect"].x0-item["rect"].x0)<4 and -20 < item["rect"].y0-p["rect"].y1 < 12), None)
        if previous:
            previous["lines"] += item["lines"]
            previous["rect"] = _union([previous["rect"], item["rect"]])
        else:
            merged.append(item)
    return merged


def _gap(items, axis):
    intervals = sorted((item["rect"][axis], item["rect"][axis+2]) for item in items)
    end, best = intervals[0][1], (0, None)
    for lo, hi in intervals[1:]:
        if lo-end > best[0]:
            best = (lo-end, (lo+end)/2)
        end = max(end, hi)
    return best


def _reading_order(items, depth=0):
    """Whitespace cuts read full-width bands, then complete each column."""
    if len(items) <= 1:
        return items
    if depth > 18:
        return sorted(items, key=lambda i:(i["rect"].y0, i["rect"].x0))
    gx, gy = _gap(items, 0), _gap(items, 1)
    if gx[0] >= 13 and (gy[0] < 16 or gx[0] > gy[0]*1.4):
        axis, cut = 0, gx[1]
    elif gy[0] >= 8:
        axis, cut = 1, gy[1]
    else:
        return sorted(items, key=lambda i:(round(i["rect"].y0/5)*5, i["rect"].x0))
    before = [i for i in items if i["rect"][axis+2] <= cut]
    after = [i for i in items if i["rect"][axis] >= cut]
    if not before or not after or len(before)+len(after) != len(items):
        return sorted(items, key=lambda i:(i["rect"].y0, i["rect"].x0))
    return _reading_order(before, depth+1)+_reading_order(after, depth+1)


def _jpeg_region(page, rect, max_size=900):
    scale = min(1.65, max_size / max(rect.width, rect.height))
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=rect, alpha=False, colorspace=fitz.csRGB)
    return pix.tobytes("jpeg", jpg_quality=70), pix.width, pix.height


def _header_region(page, lines):
    """Keep connected full-width artwork at the top as one complete header.

    Exporters often build a masthead from several background strips, a small
    logo, and text (sometimes with raster twins). Rebuilding those separately
    loses the logo and lets the title wrap. Their original composite is stable.
    """
    bands = [fitz.Rect(image["bbox"]) & page.rect for image in page.get_image_info()]
    bands = [rect for rect in bands if rect.width >= page.rect.width*.85
             and 8 < rect.height < page.rect.height*.4
             and rect.get_area() < page.rect.get_area()*.6]
    edge = 0
    for rect in sorted(bands, key=lambda rect: rect.y0):
        if rect.y0 <= edge+8 and rect.y1 <= page.rect.height*.45:
            edge = max(edge, rect.y1)
    if edge < page.rect.height*.07:
        return None
    # Include a crossing date/heading in full, rather than crop its last pixels.
    for line in sorted(lines, key=lambda line: line["rect"].y0):
        if line["rect"].y0 < edge < line["rect"].y1:
            edge = line["rect"].y1+1
    return fitz.Rect(page.rect.x0, page.rect.y0, page.rect.x1, min(edge, page.rect.height*.45))


def _tile_rows(width, height, linked_rects):
    """Partition every page pixel exactly once, with real linked image cells.

    Each horizontal band has its own nested table so its x cuts do not alter
    other rows. Adjacent areas with the same target merge to limit email size.
    Smaller overlapping annotations win; fully covered targets get a fallback.
    """
    bounds = sorted({0, height} | {y for rect, _ in linked_rects for y in (rect.y0, rect.y1)})
    rows = []
    for top, bottom in zip(bounds, bounds[1:]):
        if bottom <= top:
            continue
        active = [(rect, link) for rect, link in linked_rects if rect.y0 <= top and rect.y1 >= bottom]
        cuts = sorted({0, width} | {x for rect, _ in active for x in (rect.x0, rect.x1)})
        cells = []
        for left, right in zip(cuts, cuts[1:]):
            if right <= left:
                continue
            covering = [(rect, link) for rect, link in active if rect.x0 <= left and rect.x1 >= right]
            link = min(covering, key=lambda pair: pair[0].width*pair[0].height)[1] if covering else None
            if cells and cells[-1][1] is link:
                cells[-1] = (fitz.IRect(cells[-1][0].x0, top, right, bottom), link)
            else:
                cells.append((fitz.IRect(left, top, right, bottom), link))
        if len(cells) == 1 and rows and len(rows[-1]) == 1 and cells[0][1] is rows[-1][0][1]:
            previous = rows[-1][0][0]
            rows[-1] = [(fitz.IRect(0, previous.y0, width, bottom), cells[0][1])]
        else:
            rows.append(cells)
    return rows


def _faithful_page(page, lines, links, page_number, render_width=1200):
    """Render once, then copy exact integer pixel regions into email tables."""
    scale = render_width/page.rect.width
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False, colorspace=fitz.csRGB)
    linked_rects = []
    for link in links:
        rect = link["rect"] & page.rect
        if rect.is_empty:
            continue
        # Round each boundary once; all tile crops share these same boundaries.
        px = fitz.IRect(max(0, round(rect.x0*scale)), max(0, round(rect.y0*scale)),
                        min(pix.width, round(rect.x1*scale)), min(pix.height, round(rect.y1*scale)))
        if not px.is_empty:
            linked_rects.append((px, link))
    rows = _tile_rows(pix.width, pix.height, linked_rects)
    row_markup, tiles, byte_count = [], [], 0
    full_text = "\n".join(line["text"].strip() for line in _reading_order(lines))
    cell_style = "padding:0;margin:0;border:0;font-size:0;line-height:0;vertical-align:top;"
    table_style = "width:100%;border:0;border-collapse:collapse;border-spacing:0;table-layout:fixed;mso-table-lspace:0pt;mso-table-rspace:0pt;"
    first = True
    for cells in rows:
        cells_markup = []
        for rect, link in cells:
            # This is a pixel copy, not another PDF render. Text edges remain
            # aligned across both linked and unlinked crops without white gaps.
            crop = fitz.Pixmap(fitz.csRGB, rect, False)
            crop.copy(pix, rect)
            data = crop.tobytes("jpeg", jpg_quality=78)
            byte_count += len(data)
            src = "data:image/jpeg;base64,"+base64.b64encode(data).decode("ascii")
            alt = f"Newsletter page {page_number}. {full_text}" if first else (link["label"] if link else "")
            first = False
            percent = rect.width/pix.width*100
            display_width = max(1, round(rect.width/pix.width*640))
            tag = f'<img src="{src}" alt="{html.escape(alt, quote=True)}" width="{display_width}" style="display:block;width:100%;max-width:100%;height:auto;border:0;margin:0;padding:0;" />'
            if link:
                link["mapped"] = True
                tag = '<a href="'+html.escape(link["url"],quote=True)+'" style="display:block;text-decoration:none;line-height:0;">'+tag+'</a>'
            cells_markup.append(f'<td width="{percent:.8f}%" style="{cell_style}width:{percent:.8f}%;">{tag}</td>')
            tiles.append({"x":rect.x0,"y":rect.y0,"width":rect.width,"height":rect.height,"url":link["url"] if link else None})
        row_markup.append(f'<tr><td style="{cell_style}"><table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="{table_style}"><tr>'+"".join(cells_markup)+'</tr></table></td></tr>')
    table = f'<table role="presentation" data-pdf-page="{page_number}" aria-label="Newsletter page {page_number}" width="640" cellpadding="0" cellspacing="0" border="0" style="{table_style}max-width:640px;margin:0 auto;">'+"".join(row_markup)+'</table>'
    # Annotations outside the visible page or fully hidden by another annotation
    # still have a reachable destination below the page.
    missing = {link["url"]:link for link in links if not link["mapped"]}
    if missing:
        table += '<p style="'+BODY_STYLE+'">Additional links: '+" &middot; ".join('<a href="'+html.escape(url,quote=True)+'" style="'+LINK_STYLE+'">'+html.escape(link["label"] or url)+'</a>' for url,link in missing.items())+'</p>'
    return table, full_text, tiles, byte_count, pix.width, pix.height


def _images(page, lines, include_images):
    if not include_images:
        return []
    images = []
    seen = set()
    for image in page.get_image_info():
        rect = fitz.Rect(image["bbox"]) & page.rect
        if rect.is_empty or rect.width < 90 or rect.height < 65 or rect.get_area() < 6500:
            continue
        position = tuple(round(n) for n in rect)
        if position in seen:
            continue
        seen.add(position)
        # Discard decorative full-width strips and full-page backgrounds.
        if rect.width > page.rect.width*.85 and (rect.height < 125 or rect.get_area() > page.rect.get_area()*.72):
            continue
        overlap = sum(_intersection(rect, line["rect"]) for line in lines)
        # Newsletter exporters frequently store live text AND a raster twin.
        if overlap / max(rect.get_area(), 1) > .085:
            continue
        data, width, height = _jpeg_region(page, rect)
        images.append({"kind":"image", "rect":rect, "data":data, "width":width, "height":height})
    return images


def _qr_links(page, lines):
    """Read raster QR destinations when the optional decoder is installed."""
    found, seen, likely_without_decoder = [], set(), False
    for image in page.get_image_info():
        rect = fitz.Rect(image["bbox"]) & page.rect
        if rect.is_empty or min(rect.width,rect.height)<70 or not .45 < rect.width/rect.height < 2.2 or rect.get_area()>page.rect.get_area()*.72:
            continue
        position = tuple(round(n) for n in rect)
        if position in seen:
            continue
        seen.add(position)
        if sum(_intersection(rect,line["rect"]) for line in lines)/max(rect.get_area(),1)>.085:
            continue
        scale = min(3,1200/max(rect.width,rect.height))
        pix = page.get_pixmap(matrix=fitz.Matrix(scale,scale),clip=rect,alpha=False,colorspace=fitz.csGRAY)
        if zxingcpp is None:
            samples = pix.samples[::max(1,len(pix.samples)//12000)]
            dark = sum(c<45 for c in samples)/max(1,len(samples))
            extreme = sum(c<45 or c>225 for c in samples)/max(1,len(samples))
            if .65<rect.width/rect.height<1.5 and .12<dark<.7 and extreme>.9:
                likely_without_decoder = True
            continue
        try:
            view = memoryview(pix.samples).cast("B", shape=(pix.height,pix.width))
            barcodes = zxingcpp.read_barcodes(view)
        except Exception:
            # A decoder failure never discards an image or the original PDF.
            likely_without_decoder = True
            continue
        for barcode in barcodes:
            url = safe_url(barcode.text)
            if not url or any(link["url"]==url for link in found):
                continue
            p = barcode.position
            points = [p.top_left,p.top_right,p.bottom_left,p.bottom_right]
            qr_rect = fitz.Rect(rect.x0+min(pt.x for pt in points)/scale,rect.y0+min(pt.y for pt in points)/scale,rect.x0+max(pt.x for pt in points)/scale,rect.y0+max(pt.y for pt in points)/scale)
            label = "QR code: "+(urlsplit(url).hostname or url)
            found.append({"url":url,"label":label,"rect":qr_rect,"mapped":False,"source":"qr"})
    return found, likely_without_decoder


def _paragraph_html(item, links, body_size):
    lines = item["lines"]
    pieces, plain = [], []
    list_like = (len(lines) >= 3
                 and statistics.mean(len(l["text"].strip()) for l in lines) < 42
                 and sum(bool(re.match(r"^[A-Z0-9$]",l["text"].lstrip())) for l in lines) >= len(lines)*.8)
    for index, line in enumerate(lines):
        chars = list(line["chars"])
        text = line["text"].strip()
        suffix = "<br />" if list_like and index < len(lines)-1 else " "
        if index < len(lines)-1 and text.endswith("-") and re.match(r"^[a-z]", lines[index+1]["text"].lstrip()):
            # Retain explicit compound-word hyphens without an inserted space.
            # Removing visible hyphens can silently turn tax-deductible into
            # taxdeductible; only a soft hyphen is safe to remove generically.
            if len(chars)>1 and chars[-2]["text"].isalpha():
                suffix = ""
        if index < len(lines)-1 and chars and chars[-1]["text"] == "":
            suffix = ""
        pieces.append(_render_runs(chars, links).strip()+suffix)
        plain.append(text+("\n" if "<br" in suffix else suffix))
    content, text = "".join(pieces).strip(), "".join(plain).strip()
    # The PDF may wrap one link onto several lines. Keep one editable anchor
    # while retaining its internal bold/italic formatting and line breaks.
    boundary = re.compile(r'(<a href="([^\"]+)" style="'+re.escape(LINK_STYLE)+r'">(?:(?!</a>).)*?)</a>(\s*(?:<br />\s*)?)<a href="\2" style="'+re.escape(LINK_STYLE)+r'">',re.S)
    while True:
        joined, count = boundary.subn(lambda match:match.group(1)+match.group(3),content)
        if not count:
            break
        content = joined
    if item["size"] >= max(27, body_size*1.9):
        tag, style = "h1", "margin:24px 0 14px;font-size:30px;line-height:1.2;color:#19372e;"
    elif item["size"] >= max(18, body_size*1.3):
        tag, style = "h2", "margin:22px 0 10px;font-size:22px;line-height:1.3;color:#19372e;"
    elif (all(l["bold"] for l in lines) and len(text)<85 and (text.isupper() or item["size"] > body_size*1.12)):
        tag, style = "h3", "margin:20px 0 8px;font-size:18px;line-height:1.4;color:#19372e;"
    elif _BULLET.match(text):
        # Keep the original bullet glyph; its text remains directly editable.
        tag, style = "p", BODY_STYLE+"padding-left:18px;"
    else:
        tag, style = "p", BODY_STYLE
    return f'<{tag} style="{style}">{content}</{tag}>', text


def convert_pdf(pdf_bytes: bytes, include_images: bool=True, layout: str="editable") -> dict:
    """Return editable email HTML, plain text, link positions, and diagnostics."""
    if not isinstance(pdf_bytes, (bytes, bytearray)) or not pdf_bytes:
        raise ValueError("Choose a non-empty PDF file.")
    if layout not in ("editable", "faithful"):
        raise ValueError("Choose either the original PDF layout or editable text layout.")
    requested_layout = layout
    if layout == "faithful" and not include_images:
        layout = "editable"
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:
        raise ValueError("This file could not be read as a PDF.") from exc
    try:
        if doc.needs_pass:
            raise ValueError("This PDF is password protected. Export an unlocked copy first.")
        if not doc.page_count:
            raise ValueError("This PDF has no pages.")
        if doc.page_count > 50:
            raise ValueError("Please use a newsletter of 50 pages or fewer.")
        output, pages, all_links, warnings, plain_sections = [], [], [], [], []
        duplicate_count, image_count, image_bytes, skipped_links = 0, 0, 0, 0
        repeated_headers, annotation_urls = set(), set()
        for page in doc:
            page_number = page.number+1
            lines, duplicates = _line_data(page)
            duplicate_count += duplicates
            original_lines = list(lines)
            links, unsafe = _page_links(page, lines)
            skipped_links += unsafe
            for link in links:
                annotation_urls.add(link["url"])
                all_links.append({"url":link["url"],"label":link["label"],"page":page_number,"source":"annotation"})
            qr_links, qr_unavailable = _qr_links(page, lines)
            for link in qr_links:
                if not any(l["url"]==link["url"] and _intersection(l["rect"],link["rect"])>0 for l in links):
                    links.append(link)
                    all_links.append({"url":link["url"],"label":link["label"],"page":page_number,"source":"qr"})
            if qr_unavailable:
                warnings.append(f"Page {page_number} may contain an unlinked QR code. Its image is preserved when images are enabled; check that its destination has an email link.")
            _autolink_chars(lines, links, page_number, all_links)
            page_links = [{"x":l["rect"].x0,"y":l["rect"].y0,"width":l["rect"].width,"height":l["rect"].height,"url":l["url"],"label":l["label"]} for l in links]
            pages.append({"width":page.rect.width,"height":page.rect.height,"links":page_links})
            if layout == "faithful":
                markup, text, tiles, byte_count, pixel_width, pixel_height = _faithful_page(page, lines, links, page_number)
                output.append(markup)
                plain_sections.append(text)
                image_count += len(tiles)
                image_bytes += byte_count
                pages[-1].update({"tiles":tiles,"pixelWidth":pixel_width,"pixelHeight":pixel_height})
                continue
            header = _header_region(page, original_lines) if include_images else None
            header_lines = [line for line in original_lines if header and line["rect"].y0 < header.y1 and line["rect"].y1 <= header.y1]
            retained = []
            for line in lines:
                if line in header_lines:
                    continue
                key = re.sub(r"\s", "", line["text"]).lower()
                if re.fullmatch(r"page\d+of\d+", key):
                    continue
                if line["rect"].y1 < page.rect.height*.11:
                    if key in repeated_headers:
                        continue
                    repeated_headers.add(key)
                retained.append(line)
            body_sizes = [l["size"] for l in retained for _ in range(min(80,len(l["text"])))]
            body_size = statistics.median(body_sizes) if body_sizes else 12
            items = _paragraphs(retained, body_size)
            if sum(len(_key(l["text"])) for l in lines) < 20:
                warnings.append(f"Page {page_number} has little or no selectable text. Its image is preserved; text in that image cannot be edited without OCR.")
                items = []
                if include_images:
                    data,width,height = _jpeg_region(page,page.rect)
                    items.append({"kind":"image","rect":page.rect,"data":data,"width":width,"height":height,"scan":True})
                else:
                    output.append('<p style="'+BODY_STYLE+'">[Page '+str(page_number)+': image-only content. Enable images to include it.]</p>')
            else:
                items += _images(page, original_lines, include_images)
                if header:
                    # Small logos and raster title twins are already part of
                    # this composite. Do not display either a second time.
                    items = [item for item in items if item["rect"].y0 >= header.y1]
                    data,width,height = _jpeg_region(page,header,max_size=1200)
                    items.append({"kind":"image","rect":header,"data":data,"width":width,"height":height,"header":True,"text":" ".join(line["text"].strip() for line in _reading_order(header_lines))})
            page_output, page_plain = [], []
            for item in _reading_order(items):
                if item["kind"] == "text":
                    markup, text = _paragraph_html(item,links,body_size)
                    page_output.append(markup)
                    page_plain.append(text)
                else:
                    image_count += 1
                    image_bytes += len(item["data"])
                    src = "data:image/jpeg;base64,"+base64.b64encode(item["data"]).decode("ascii")
                    width = min(592, item["width"], round(item["rect"].width*1.1))
                    description = item.get("text") or f"Newsletter image from page {page_number}"
                    image_style = "display:block;width:100%;max-width:100%;height:auto;border:0;" if item.get("header") else "display:block;max-width:100%;height:auto;border:0;border-radius:6px;"
                    if item.get("header"):
                        width = 592
                    tag = f'<img src="{src}" alt="{html.escape(description,quote=True)}" width="{width}" style="{image_style}" />'
                    matches = [link for link in links if _intersection(item["rect"],link["rect"])/max(item["rect"].get_area(),1)>.35 or (link.get("source")=="qr" and _intersection(item["rect"],link["rect"])/max(link["rect"].get_area(),1)>.7)]
                    if matches:
                        match = min(matches,key=lambda l:l["rect"].get_area())
                        match["mapped"] = True
                        tag = '<a href="'+html.escape(match["url"],quote=True)+'" style="'+LINK_STYLE+'">'+tag+'</a>'
                    page_output.append('<p style="margin:14px 0 22px;">'+tag+'</p>')
                    page_plain.append(item.get("text") or f'[Newsletter image from page {page_number}]')
            missing = {}
            mapped_urls = {link["url"] for link in links if link["mapped"]}
            for link in links:
                if link["url"] not in mapped_urls:
                    missing.setdefault(link["url"],link)
            if missing:
                page_output.append('<p style="'+BODY_STYLE+'"><strong>Links from page '+str(page_number)+'</strong></p>')
                for link in missing.values():
                    page_output.append('<p style="'+BODY_STYLE+'"><a href="'+html.escape(link["url"],quote=True)+'" style="'+LINK_STYLE+'">'+html.escape(link["label"] or link["url"])+'</a></p>')
                    page_plain.append((link["label"] or "Link")+": "+link["url"])
            if page.number and page_output:
                output.append('<hr style="border:0;border-top:1px solid #dde4df;margin:28px 0;" />')
            output.extend(page_output)
            plain_sections.extend(page_plain)
        unique = {}
        for link in all_links:
            unique.setdefault(link["url"],link)
            if link["source"] == "annotation" and re.search(r"learn more|support|volunteer|rsvp",link["label"],re.I):
                unique[link["url"]] = link
        # Always include URL text in the plain-text alternative, even when the
        # rich HTML uses a concise button or article label.
        if unique:
            plain_sections += ["", "Links:"]+[link["label"]+": "+link["url"] for link in unique.values()]
        if skipped_links:
            warnings.append(f"{skipped_links} unsupported or unsafe link action(s) were omitted. Only web, email, and telephone links are supported.")
        if not include_images:
            warnings.append("Images are excluded. Any dates, flyer details, or QR codes stored only in images will be absent from the email.")
        if requested_layout == "faithful" and layout != "faithful":
            warnings.append("The original PDF layout requires images. Editable text layout was used because images are excluded.")
        if layout == "faithful":
            warnings.append("Original PDF layout preserves the page appearance and clickable links. Page artwork moves as one block. Use editable text layout to change wording; inserted text and images remain editable in either layout.")
        if any("\ufffd" in line for line in plain_sections):
            warnings.append("Some PDF characters could not be decoded. Review dates, punctuation, and names in the editable email.")
        warnings.append("Copying images into email depends on your email app. Send a test email to yourself and check images and links before sending the newsletter.")
        padding = "0" if layout == "faithful" else "24px"
        body = '<div style="max-width:640px;margin:0 auto;padding:'+padding+';font-family:Arial,Helvetica,sans-serif;background:#ffffff;color:#273449;word-wrap:break-word;">'+"\n".join(output)+'</div>'
        return {"html":body,"layout":layout,"pages":pages,"links":list(unique.values()),"pageCount":doc.page_count,"imageCount":image_count,"warnings":warnings,"plainText":"\n\n".join(plain_sections),"stats":{"sourceBytes":len(pdf_bytes),"htmlBytes":len(body.encode("utf-8")),"imageBytes":image_bytes,"annotationLinkCount":len(annotation_urls),"linkCount":len(unique),"duplicatesRemoved":duplicate_count}}
    finally:
        doc.close()
