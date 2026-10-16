"""Local PDF newsletter conversion and email download server."""

from __future__ import annotations

import argparse
import base64
import binascii
from collections import OrderedDict
from email import policy
from email.message import EmailMessage
import errno
from html import escape
from html.parser import HTMLParser
import http.client
import json
from pathlib import Path
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.parse
import uuid
import webbrowser
import os

APP_DIR = Path(__file__).resolve().parent
if not getattr(sys, "frozen", False) and (APP_DIR / ".vendor").is_dir():
    sys.path.insert(0, str(APP_DIR / ".vendor"))

import pymupdf  # noqa: E402
from converter import convert_pdf  # noqa: E402

MAX_UPLOAD = 30 * 1024 * 1024
APP_VERSION = "1.1"
MAX_DOCUMENTS = 3
DOCUMENTS: OrderedDict[str, bytes] = OrderedDict()
DOCUMENT_LOCK = threading.Lock()
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
}
PAGE_ROUTE = re.compile(r"^/api/documents/([0-9a-f]{32})/pages/([1-9][0-9]*)\.jpg$")


def safe_url(value: str, *, image: bool = False) -> str | None:
    value = value.strip()
    if any(ord(char) < 32 or ord(char) == 127 for char in value) or "\\" in value:
        return None
    if image:
        return value if re.fullmatch(
            r"data:image/(?:png|jpeg|gif|webp);base64,[A-Za-z0-9+/=\s]+", value,
            re.IGNORECASE,
        ) else None
    try:
        parts = urllib.parse.urlsplit(value)
        scheme = parts.scheme.lower()
        if scheme in {"http", "https"}:
            if not parts.hostname:
                return None
            parts.port
        elif not parts.path:
            return None
    except ValueError:
        return None
    return value if scheme in {"http", "https", "mailto", "tel"} else None


SAFE_CSS = {
    "color", "background-color", "font-family", "font-size", "font-weight",
    "font-style", "line-height", "text-align", "text-decoration", "white-space",
    "margin", "margin-top", "margin-right", "margin-bottom", "margin-left",
    "padding", "padding-top", "padding-right", "padding-bottom", "padding-left",
    "border", "border-top", "border-bottom", "border-left", "border-right",
    "border-color", "border-width", "border-style", "border-collapse",
    "border-radius", "width", "max-width", "min-width", "height", "max-height",
    "vertical-align", "display", "list-style-type", "word-break", "word-wrap", "overflow-wrap", "table-layout", "mso-table-lspace", "mso-table-rspace",
}


def clean_style(value: str) -> str:
    parts = []
    for declaration in value.split(";"):
        if ":" not in declaration:
            continue
        name, val = declaration.split(":", 1)
        name, val = name.strip().lower(), val.strip()
        if name not in SAFE_CSS or not val or len(val) > 240:
            continue
        if re.search(r"url\s*\(|expression|javascript|vbscript|behavior|@|\\|[<>]", val, re.I):
            continue
        if not re.fullmatch(r"[\w\s#.,%()'\"+\-!/]+", val):
            continue
        parts.append(f"{name}:{val}")
    return ";".join(parts)


class EmailHTMLSanitizer(HTMLParser):
    """Keep ordinary email formatting and remove executable HTML."""

    TAGS = {
        "a", "abbr", "b", "blockquote", "br", "caption", "center", "code",
        "col", "colgroup", "dd", "del", "div", "dl", "dt", "em", "h1", "h2",
        "h3", "h4", "h5", "h6", "hr", "i", "img", "li", "ol", "p", "pre",
        "s", "small", "span", "strike", "strong", "sub", "sup", "table",
        "tbody", "td", "th", "thead", "tr", "u", "ul",
    }
    VOID = {"br", "col", "hr", "img"}
    DROP_CONTENT = {"script", "style", "iframe", "object", "svg", "math", "template"}
    COMMON_ATTRS = {"title", "align", "valign", "width", "height", "bgcolor", "role"}
    TABLE_ATTRS = {"cellpadding", "cellspacing", "border", "colspan", "rowspan"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.output: list[str] = []
        self.drop_stack: list[str] = []
        self.tag_stack: list[str] = []
        self.links: list[str | None] = []
        self.plain: list[str] = []

    def handle_starttag(self, tag, attrs):
        if self.drop_stack:
            if tag in self.DROP_CONTENT:
                self.drop_stack.append(tag)
            return
        if tag in self.DROP_CONTENT:
            self.drop_stack.append(tag)
            return
        if tag not in self.TAGS:
            return
        clean = []
        link = None
        for name, value in attrs:
            if value is None or len(value) > MAX_UPLOAD:
                continue
            if name == "style":
                value = clean_style(value)
                if value:
                    clean.append((name, value))
            elif tag == "a" and name == "href":
                value = safe_url(value)
                if value:
                    clean.append((name, value))
                    link = value
            elif tag == "img" and name == "src":
                value = safe_url(value, image=True)
                if value:
                    clean.append((name, value))
            elif tag == "img" and name == "alt":
                clean.append((name, value[:1000]))
            elif name in self.COMMON_ATTRS or (
                tag in {"table", "td", "th"} and name in self.TABLE_ATTRS
            ) or (tag == "ol" and name == "start"):
                clean.append((name, value[:200]))
        if tag == "a" and link:
            clean.append(("rel", "noopener noreferrer"))
        attr_html = "".join(f' {name}="{escape(value, quote=True)}"' for name, value in clean)
        self.output.append(f"<{tag}{attr_html}>")
        if tag not in self.VOID:
            self.tag_stack.append(tag)
        if tag == "a":
            self.links.append(link)
        if tag in {"p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "br", "hr"}:
            self.plain.append("\n")
        if tag == "img":
            alt = dict(clean).get("alt", "")
            if alt:
                self.plain.append(f"[{alt}]")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if self.drop_stack:
            if tag == self.drop_stack[-1]:
                self.drop_stack.pop()
            return
        if tag not in self.TAGS or tag in self.VOID or tag not in self.tag_stack:
            return
        while self.tag_stack:
            closing = self.tag_stack.pop()
            self.output.append(f"</{closing}>")
            if closing == "a" and self.links:
                link = self.links.pop()
                if link:
                    self.plain.append(f" ({link})")
            if closing == tag:
                break
        if tag in {"p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr"}:
            self.plain.append("\n")

    def handle_data(self, data):
        if not self.drop_stack:
            self.output.append(escape(data))
            self.plain.append(data)

    def result(self):
        self.close()
        while self.tag_stack:
            closing = self.tag_stack.pop()
            self.output.append(f"</{closing}>")
            if closing == "a" and self.links:
                link = self.links.pop()
                if link:
                    self.plain.append(f" ({link})")
        return "".join(self.output), re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", "".join(self.plain)).strip()


def build_eml(html: str, subject: str) -> bytes:
    sanitizer = EmailHTMLSanitizer()
    sanitizer.feed(html)
    clean_html, plain = sanitizer.result()
    pictures: list[tuple[str, bytes, str]] = []
    known_images: dict[str, str] = {}

    def inline_image(match):
        image_uri = match.group(1)
        if image_uri in known_images:
            return f'src="cid:{known_images[image_uri]}"'
        header, encoded = image_uri.split(",", 1)
        subtype = header.split("/", 1)[1].split(";", 1)[0].lower()
        try:
            data = base64.b64decode(re.sub(r"\s+", "", encoded), validate=True)
        except (ValueError, binascii.Error):
            return 'alt="Image could not be embedded"'
        if len(data) > 10 * 1024 * 1024:
            raise ValueError("One image is too large to export (maximum 10 MB per image).")
        if not data:
            return 'alt="Empty image"'
        cid = f"newsletter-image-{len(pictures) + 1}@local"
        known_images[image_uri] = cid
        pictures.append((subtype, data, cid))
        return f'src="cid:{cid}"'

    clean_html = re.sub(
        r'src="(data:image/(?:png|jpeg|gif|webp);base64,[A-Za-z0-9+/=\s]+)"',
        inline_image, clean_html, flags=re.I,
    )
    message = EmailMessage(policy=policy.SMTP)
    subject = re.sub(r"[\r\n\x00-\x1f\x7f]+", " ", subject).strip()[:250]
    message["Subject"] = subject or "PTA Newsletter"
    message["X-Unsent"] = "1"
    message.set_content(plain or "Open this message in an HTML-capable email application.")
    message.add_alternative(
        '<!doctype html><html><head><meta charset="utf-8"></head>'
        '<body style="margin:0;padding:24px;background:#ffffff">'
        + clean_html + "</body></html>", subtype="html",
    )
    html_part = message.get_payload()[-1]
    for index, (subtype, data, cid) in enumerate(pictures, 1):
        extension = "jpg" if subtype == "jpeg" else subtype
        html_part.add_related(
            data, maintype="image", subtype=subtype, cid=f"<{cid}>",
            disposition="inline", filename=f"newsletter-image-{index}.{extension}",
        )
    return message.as_bytes()


class NewsletterServer(ThreadingHTTPServer):
    daemon_threads = True
    # Windows SO_REUSEADDR can let another process bind an active listener.
    allow_reuse_address = os.name != "nt"


class Handler(BaseHTTPRequestHandler):
    server_version = "NewsletterLocal/1.0"

    def log_message(self, format, *args):
        # Avoid logging PDF content, subjects, links, or document identifiers.
        print(f"[{self.log_date_time_string()}] {self.command} {self.path.split('?', 1)[0].split('/api/documents/')[0]}")

    def _send(self, status: int, body: bytes, content_type: str, **headers):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; font-src 'self'; connect-src 'self'; "
            "frame-src 'self' blob:; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
        )
        for key, value in headers.items():
            self.send_header(key.replace("_", "-"), value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, value: dict):
        self._send(status, json.dumps(value, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _error(self, status: int, message: str):
        self._json(status, {"error": message})

    def _local_request(self) -> bool:
        allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        host = self.headers.get("Host", "").lower()
        if host not in allowed:
            self.close_connection = True
            self._error(403, "This app only accepts requests from its local browser address.")
            return False
        origin = self.headers.get("Origin")
        if origin and origin.lower() not in {"http://" + item for item in allowed}:
            self.close_connection = True
            self._error(403, "Cross-site requests are not allowed.")
            return False
        if self.headers.get("Sec-Fetch-Site", "").lower() in {"cross-site", "same-site"}:
            self.close_connection = True
            self._error(403, "Open the app directly at its local address.")
            return False
        return True

    def _body(self, content_type: str) -> bytes | None:
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != content_type:
            self.close_connection = True
            self._error(415, f"This request requires {content_type} content.")
            return None
        try:
            size = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            size = -1
        if size < 0 or self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            self._error(411, "A Content-Length header is required.")
            return None
        if size > MAX_UPLOAD:
            self.close_connection = True
            self._error(413, "This file is too large. Choose a PDF smaller than 30 MB.")
            return None
        self.connection.settimeout(30)
        try:
            data = self.rfile.read(size)
        except TimeoutError:
            self.close_connection = True
            self._error(408, "The upload timed out. Please try again.")
            return None
        if len(data) != size:
            self.close_connection = True
            self._error(400, "The upload was incomplete. Please try again.")
            return None
        return data

    def do_GET(self):
        if not self._local_request():
            return
        path = urllib.parse.urlsplit(self.path).path
        if path in STATIC_FILES:
            name, content_type = STATIC_FILES[path]
            try:
                data = (APP_DIR / name).read_bytes()
            except OSError:
                self._error(404, "The app files could not be found. Keep the application folder together.")
                return
            self._send(200, data, content_type)
        elif path == "/api/health":
            self._json(200, {"ok": True, "local": True, "app": "newsletter-studio", "version": APP_VERSION})
        elif path == "/api/sample":
            sample = Path(os.environ.get("NEWSLETTER_SAMPLE", str(
                APP_DIR.parent.parent / "October" / "PSIS 499 PTA Newsletter_ October.pdf"
            )))
            try:
                if sample.stat().st_size > MAX_UPLOAD:
                    self._error(413, "The example PDF is larger than 30 MB.")
                    return
                data = sample.read_bytes()
            except OSError:
                self._error(404, "The October example was not found. Use Upload PDF to choose your newsletter.")
                return
            self._send(200, data, "application/pdf")
        elif match := PAGE_ROUTE.fullmatch(path):
            identifier, page_num = match.group(1), int(match.group(2))
            with DOCUMENT_LOCK:
                data = DOCUMENTS.get(identifier)
            if data is None:
                self._error(404, "This PDF is no longer in memory. Upload it again to see the pages.")
                return
            try:
                with pymupdf.open(stream=data, filetype="pdf") as document:
                    if page_num > len(document):
                        self._error(404, "That PDF page does not exist.")
                        return
                    page = document[page_num - 1]
                    scale = min(1000 / max(page.rect.width, 1), 1600 / max(page.rect.height, 1), 2.0)
                    pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
                    jpeg = pixmap.tobytes("jpeg", jpg_quality=78)
                self._send(200, jpeg, "image/jpeg")
            except (ValueError, RuntimeError):
                self._error(422, "This PDF page could not be rendered.")
        elif path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
        else:
            self._error(404, "This page could not be found.")

    def do_POST(self):
        if not self._local_request():
            return
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/api/convert":
            data = self._body("application/pdf")
            if data is None:
                return
            if b"%PDF-" not in data[:1024]:
                self._error(422, "Choose a valid PDF file.")
                return
            include_images = urllib.parse.parse_qs(parsed.query).get("images", ["1"])[0] != "0"
            layout = urllib.parse.parse_qs(parsed.query).get("layout", ["editable"])[0]
            if layout not in {"editable", "faithful"}:
                self._error(400, "Choose Match PDF layout or Editable text.")
                return
            try:
                converted = convert_pdf(data, include_images=include_images, layout=layout)
            except (ValueError, RuntimeError) as error:
                self._error(422, str(error) or "This PDF could not be converted.")
                return
            except Exception:
                self._error(500, "This PDF could not be converted. Try another file or restart the app.")
                return
            identifier = uuid.uuid4().hex
            with DOCUMENT_LOCK:
                DOCUMENTS[identifier] = data
                while len(DOCUMENTS) > MAX_DOCUMENTS:
                    DOCUMENTS.popitem(last=False)
            self._json(200, {**converted, "id": identifier})
        elif parsed.path == "/api/export-eml":
            data = self._body("application/json")
            if data is None:
                return
            try:
                payload = json.loads(data)
                if not isinstance(payload, dict) or not isinstance(payload.get("html"), str):
                    raise ValueError("Email HTML is required.")
                subject = payload.get("subject", "PTA Newsletter")
                if not isinstance(subject, str):
                    raise ValueError("The subject must be text.")
                eml = build_eml(payload["html"], subject)
            except (ValueError, TypeError, UnicodeError) as error:
                self._error(400, str(error) or "The email could not be exported.")
                return
            self._send(
                200, eml, "message/rfc822",
                Content_Disposition='attachment; filename="PTA-newsletter.eml"',
            )
        else:
            self.close_connection = True
            self._error(404, "This action could not be found.")


def existing_app_running(port: int, expected_version: str | None = None) -> bool:
    """Recognize only this app, without changing another listener on the port."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        connection.request("GET", "/api/health")
        response = connection.getresponse()
        if response.status != 200:
            return False
        result = json.loads(response.read(16 * 1024))
        return (
            isinstance(result, dict) and result.get("ok") is True
            and result.get("local") is True and result.get("app") == "newsletter-studio"
            and (expected_version is None or result.get("version") == expected_version)
        )
    except (OSError, ValueError, http.client.HTTPException):
        return False
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser(description="Convert PDF newsletters into editable emails on your computer.")
    parser.add_argument("--port", type=int, default=8799, help="Local server port (default: 8799)")
    parser.add_argument("--no-browser", action="store_true", help="Do not open the web browser automatically")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    url = f"http://127.0.0.1:{args.port}"
    try:
        server = NewsletterServer(("127.0.0.1", args.port), Handler)
    except OSError as error:
        in_use = error.errno in {errno.EADDRINUSE, 10048} or getattr(error, "winerror", None) == 10048
        if in_use and existing_app_running(args.port):
            print(f"\nNewsletter Studio is already ready at {url}")
            if not args.no_browser:
                webbrowser.open(url)
            return 0
        print(f"Could not start the newsletter app: {error}")
        print("If the app is already running, open its browser window. Otherwise try --port 8800.")
        return 1
    print(f"\nNewsletter to Email is ready at {url}")
    print("Keep this window open while using the app. Press Ctrl+C to stop.\n")
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nNewsletter app stopped. Uploaded PDFs have been cleared from memory.")
    finally:
        server.server_close()
        with DOCUMENT_LOCK:
            DOCUMENTS.clear()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
