"""Backend regression checks. Run: python -m unittest test_server.py"""

import base64
from contextlib import redirect_stdout
from email import policy
from email.parser import BytesParser
import errno
import http.client
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.parse
import urllib.request

import server


PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
)


class EmailExportTests(unittest.TestCase):
    def message(self, html, subject="PTA October"):
        return BytesParser(policy=policy.default).parsebytes(server.build_eml(html, subject))

    def test_outlook_zero_table_gutters_survive_export(self):
        message = self.message('<table style="mso-table-lspace:0pt;mso-table-rspace:0pt;table-layout:fixed"><tr><td>Header</td></tr></table>')
        html = message.get_body(preferencelist=("html",)).get_content()
        self.assertIn("mso-table-lspace:0pt", html)
        self.assertIn("mso-table-rspace:0pt", html)

    def test_embedded_images_become_deduplicated_cid_parts(self):
        source = "data:image/png;base64," + base64.b64encode(PIXEL_PNG).decode()
        message = self.message(
            f'<p>Welcome</p><a href="https://example.org/pta"><img src="{source}" alt="PTA"></a>'
            f'<img src="{source}" alt="Same picture">'
        )
        html = message.get_body(preferencelist=("html",)).get_content()
        images = [part for part in message.walk() if part.get_content_maintype() == "image"]
        self.assertEqual(message["X-Unsent"], "1")
        self.assertEqual(message.get_content_type(), "multipart/alternative")
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0].get_payload(decode=True), PIXEL_PNG)
        self.assertEqual(images[0]["Content-ID"], "<newsletter-image-1@local>")
        self.assertEqual(images[0].get_content_disposition(), "inline")
        self.assertEqual(html.count('src="cid:newsletter-image-1@local"'), 2)
        self.assertIn('href="https://example.org/pta"', html)
        self.assertNotIn("data:image/", html)

    def test_sanitizes_active_html_without_losing_message_or_safe_styles(self):
        message = self.message(
            '<h2 style="color:#174f46;position:fixed">Meeting details</h2>'
            '<p onclick="alert(1)">Everyone welcome</p>'
            '<a href="javascript:alert(1)">Unsafe destination</a>'
            '<a href="https://example.org/join">Join us</a>'
            '<img src="https://tracker.example/one.png" onerror="alert(2)">'
            '<script>secretScript()</script><iframe src="https://evil.example">secretFrame</iframe>'
            '<span style="background-color:url(https://tracker.example/two)">Visible</span>'
        )
        html = message.get_body(preferencelist=("html",)).get_content()
        for unsafe in ("onclick", "onerror", "javascript:", "<script", "secretScript", "<iframe", "secretFrame", "tracker.example", "position:"):
            with self.subTest(unsafe=unsafe):
                self.assertNotIn(unsafe, html)
        self.assertIn("Meeting details", html)
        self.assertIn("Everyone welcome", html)
        self.assertIn("color:#174f46", html)
        self.assertIn("https://example.org/join", html)

    def test_plain_text_keeps_link_destinations_and_subject_cannot_inject_headers(self):
        message = self.message(
            '<p><a href="https://example.org/register">Register</a> and '
            '<a href="mailto:pta@example.org">email the PTA</a>.</p>',
            "October\r\nBcc: attacker@example.org",
        )
        plain = message.get_body(preferencelist=("plain",)).get_content()
        self.assertIn("Register (https://example.org/register)", plain)
        self.assertIn("mailto:pta@example.org", plain)
        self.assertIsNone(message["Bcc"])
        self.assertNotIn("\n", str(message["Subject"]))

    def test_faithful_table_geometry_survives_export_without_editor_controls(self):
        source = "data:image/png;base64," + base64.b64encode(PIXEL_PNG).decode()
        message = self.message(f'<table data-pdf-page="1" contenteditable="false" draggable="true" role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="table-layout:fixed;border-collapse:collapse;line-height:0;font-size:0"><tr><td width="25%" style="padding:0;vertical-align:top"><a href="https://example.org/header"><img src="{source}" width="100%" style="display:block;width:100%;height:auto" alt="Header"></a></td></tr></table>')
        html = message.get_body(preferencelist=("html",)).get_content()
        for preserved in ('width="100%"', 'width="25%"', 'cellpadding="0"', 'cellspacing="0"', 'table-layout:fixed', 'line-height:0', 'role="presentation"', 'https://example.org/header'):
            self.assertIn(preserved, html)
        for editor_attribute in ('data-pdf-page', 'contenteditable', 'draggable'):
            self.assertNotIn(editor_attribute, html)


class QuietHandler(server.Handler):
    def log_message(self, format, *args):
        pass


class LocalAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with server.pymupdf.open() as document:
            page = document.new_page()
            page.insert_text((72, 72), "PTA newsletter: October meeting", fontsize=18)
            page.insert_text((72, 110), "Register for the meeting")
            page.insert_link({
                "kind": server.pymupdf.LINK_URI,
                "from": server.pymupdf.Rect(72, 96, 240, 114),
                "uri": "https://example.org/pta",
            })
            cls.pdf = document.tobytes()
        cls.httpd = server.NewsletterServer(("127.0.0.1", 0), QuietHandler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.httpd.server_port}"
        cls.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=2)
        with server.DOCUMENT_LOCK:
            server.DOCUMENTS.clear()

    def setUp(self):
        with server.DOCUMENT_LOCK:
            server.DOCUMENTS.clear()

    def request(self, path, data=None, content_type=None, headers=None):
        headers = dict(headers or {})
        if content_type:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(self.base + path, data=data, headers=headers)
        with self.opener.open(request, timeout=10) as response:
            return response.status, response.headers, response.read()

    def upload(self):
        return json.loads(self.request("/api/convert?images=0", self.pdf, "application/pdf")[2])

    def test_faithful_conversion_mode_and_invalid_mode(self):
        status, _, body = self.request("/api/convert?layout=faithful", self.pdf, "application/pdf")
        converted = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(converted["layout"], "faithful")
        self.assertIn('data-pdf-page="1"', converted["html"])
        self.assertIn("https://example.org/pta", converted["html"])
        self.assertGreater(converted["imageCount"], 0)
        self.assert_error(400, "/api/convert?layout=unknown", self.pdf, "application/pdf")

    def test_compression_download_preserves_pdf_and_reports_real_sizes(self):
        status, headers, body = self.request("/api/compress", self.pdf, "application/pdf")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/pdf")
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertEqual(int(headers["X-Original-Bytes"]), len(self.pdf))
        self.assertEqual(int(headers["X-Compressed-Bytes"]), len(body))
        self.assertLessEqual(len(body), len(self.pdf))
        self.assertAlmostEqual(float(headers["X-Compression-Ratio"]), len(body) / len(self.pdf))
        self.assertEqual(headers["X-Page-Count"], "1")
        self.assertEqual(headers["X-Link-Count"], "1")
        self.assertIsInstance(json.loads(urllib.parse.unquote(headers["X-Compression-Warnings"])), list)
        with server.pymupdf.open(stream=body, filetype="pdf") as document:
            self.assertIn("October meeting", document[0].get_text())
            self.assertEqual(document[0].get_links()[0]["uri"], "https://example.org/pta")
        self.assertEqual(len(server.DOCUMENTS), 0)

    def test_compression_rejects_bad_input_and_cross_site_requests(self):
        self.assert_error(415, "/api/compress", b"{}", "application/json")
        self.assert_error(422, "/api/compress", b"not a PDF", "application/pdf")
        for target in ("nan", "inf", "0", "1", "1.1", "wrong"):
            self.assert_error(400, "/api/compress?target=" + target, self.pdf, "application/pdf")
        self.assert_error(403, "/api/compress", self.pdf, "application/pdf", {"Origin": "https://evil.example"})

    def test_compression_serializes_work_and_releases_lock_on_failure(self):
        with server.COMPRESSION_LOCK:
            self.assert_error(429, "/api/compress", self.pdf, "application/pdf")
        with patch.object(server, "compress_pdf", side_effect=ValueError("Cannot compress this PDF.")):
            self.assert_error(422, "/api/compress", self.pdf, "application/pdf")
        self.assertFalse(server.COMPRESSION_LOCK.locked())

    def assert_error(self, code, path, data=None, content_type=None, headers=None):
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request(path, data, content_type, headers)
        try:
            self.assertEqual(raised.exception.code, code)
            self.assertIsInstance(json.loads(raised.exception.read())["error"], str)
        finally:
            raised.exception.close()

    def test_health_and_static_page(self):
        status, headers, body = self.request("/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["app"], "newsletter-studio")
        self.assertTrue(json.loads(body)["local"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
        self.assertIn(b"<!doctype html>", self.request("/")[2].lower())
        self.assert_error(404, "/missing-route")

    def test_pdf_upload_and_lazy_page_render_preserve_links(self):
        converted = self.upload()
        self.assertEqual(converted["pageCount"], 1)
        self.assertRegex(converted["id"], r"^[0-9a-f]{32}$")
        self.assertIn("https://example.org/pta", converted["html"])
        status, headers, jpeg = self.request(f'/api/documents/{converted["id"]}/pages/1.jpg')
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "image/jpeg")
        self.assertTrue(jpeg.startswith(b"\xff\xd8"))
        rendered = server.pymupdf.Pixmap(jpeg)
        self.assertLessEqual(rendered.width, 1000)
        self.assertLessEqual(rendered.height, 1600)
        self.assert_error(404, f'/api/documents/{converted["id"]}/pages/2.jpg')

    def test_upload_validation_and_cross_site_requests(self):
        self.assert_error(415, "/api/convert", b"{}", "application/json")
        self.assert_error(422, "/api/convert", b"not a PDF", "application/pdf")
        self.assert_error(403, "/api/convert", self.pdf, "application/pdf", {"Origin": "https://evil.example"})
        self.assert_error(403, "/api/health", headers={"Host": "evil.example"})
        self.assert_error(403, "/api/health", headers={"Sec-Fetch-Site": "cross-site"})
        connection = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=10)
        try:
            connection.request("POST", "/api/convert", body=b"", headers={
                "Content-Type": "application/pdf", "Content-Length": str(server.MAX_UPLOAD + 1),
            })
            response = connection.getresponse()
            self.assertEqual(response.status, 413)
            self.assertIn("30 MB", json.loads(response.read())["error"])
        finally:
            connection.close()

    def test_only_latest_three_pdfs_remain_available(self):
        identifiers = [self.upload()["id"] for _ in range(4)]
        with server.DOCUMENT_LOCK:
            self.assertEqual(list(server.DOCUMENTS), identifiers[1:])
        self.assert_error(404, f"/api/documents/{identifiers[0]}/pages/1.jpg")
        self.assertEqual(self.request(f"/api/documents/{identifiers[-1]}/pages/1.jpg")[0], 200)

    def test_sample_is_read_from_configured_local_path(self):
        with tempfile.TemporaryDirectory() as directory:
            sample = Path(directory) / "example.pdf"
            sample.write_bytes(self.pdf)
            with patch.dict(server.os.environ, {"NEWSLETTER_SAMPLE": str(sample)}):
                self.assertEqual(self.request("/api/sample")[2], self.pdf)
            with patch.dict(server.os.environ, {"NEWSLETTER_SAMPLE": str(sample) + ".missing"}):
                self.assert_error(404, "/api/sample")

    def test_email_export_and_invalid_payload(self):
        payload = {"html": '<p><a href="https://example.org">PTA</a></p>', "subject": "October PTA"}
        status, headers, body = self.request("/api/export-eml", json.dumps(payload).encode(), "application/json")
        message = BytesParser(policy=policy.default).parsebytes(body)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "message/rfc822")
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertEqual(message["Subject"], "October PTA")
        self.assertEqual(message["X-Unsent"], "1")
        self.assertIn("https://example.org", message.get_body(preferencelist=("html",)).get_content())
        self.assert_error(400, "/api/export-eml", b'{"html":123}', "application/json")

    def test_second_launch_reuses_verified_app_without_opening_browser_when_disabled(self):
        self.assertTrue(server.existing_app_running(self.httpd.server_port))
        args = ["server.py", "--port", str(self.httpd.server_port), "--no-browser"]
        with patch("sys.argv", args), patch.object(server.webbrowser, "open") as browser, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(server.main(), 0)
        browser.assert_not_called()
        self.assertIn(self.base, output.getvalue())
        self.assertTrue(json.loads(self.request("/api/health")[2])["ok"])

    def test_second_launch_opens_existing_browser_by_default(self):
        args = ["server.py", "--port", str(self.httpd.server_port)]
        with patch("sys.argv", args), patch.object(server.webbrowser, "open") as browser, redirect_stdout(io.StringIO()):
            self.assertEqual(server.main(), 0)
        browser.assert_called_once_with(self.base)

    def test_busy_unrelated_port_keeps_failure_and_does_not_open_browser(self):
        with patch("sys.argv", ["server.py"]), patch.object(
            server, "NewsletterServer", side_effect=OSError(errno.EADDRINUSE, "Port in use")
        ), patch.object(server, "existing_app_running", return_value=False), patch.object(
            server.webbrowser, "open"
        ) as browser, redirect_stdout(io.StringIO()):
            self.assertEqual(server.main(), 1)
        browser.assert_not_called()

    def test_health_probe_rejects_another_local_app(self):
        class OtherApp(QuietHandler):
            def do_GET(self):
                self._json(200, {"ok": True, "local": True, "app": "another-app"})

        other = server.NewsletterServer(("127.0.0.1", 0), OtherApp)
        worker = threading.Thread(target=other.serve_forever, daemon=True)
        worker.start()
        try:
            self.assertFalse(server.existing_app_running(other.server_port))
        finally:
            other.shutdown()
            other.server_close()
            worker.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
