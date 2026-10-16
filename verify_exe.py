"""Exercise the actual frozen binary without source files on its search path."""
from email import policy
from email.parser import BytesParser
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import time
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent / ".vendor"))
import pymupdf

executable = Path(sys.argv[1]).resolve()
with executable.open("rb") as stream:
    stream.seek(0x3c)
    pe_offset = struct.unpack("<I", stream.read(4))[0]
    stream.seek(pe_offset + 24 + 68)
    subsystem = struct.unpack("<H", stream.read(2))[0]
assert subsystem == 2, f"Expected Windows GUI subsystem, found {subsystem}"
print("PASS: executable uses Windows GUI mode (no terminal)", flush=True)
with socket.socket() as candidate:
    candidate.bind(("127.0.0.1", 0))
    port = candidate.getsockname()[1]
base = f"http://127.0.0.1:{port}"
environment = dict(os.environ)
for name in ("PYTHONPATH", "PYTHONHOME", "NEWSLETTER_SAMPLE"):
    environment.pop(name, None)

def request(path, body=None, content_type=None, *, with_headers=False):
    headers = {"Content-Type": content_type} if content_type else {}
    with urlopen(Request(base + path, data=body, headers=headers), timeout=120) as response:
        data = response.read()
        return (data, response.headers) if with_headers else data

with tempfile.TemporaryDirectory(prefix="newsletter-exe-check-") as unrelated_directory:
    process = subprocess.Popen([str(executable), "--smoke-test", "--no-browser", "--port", str(port)], cwd=unrelated_directory, env=environment, creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        deadline = time.monotonic() + 40
        while True:
            try:
                health = json.loads(request("/api/health"))
                break
            except OSError:
                if process.poll() is not None:
                    raise RuntimeError(f"Executable exited early with {process.returncode}")
                if time.monotonic() > deadline:
                    raise TimeoutError("Packaged app did not start within 40 seconds")
                time.sleep(.3)
        assert health["app"] == "newsletter-studio"
        assert b"Newsletter Studio" in request("/")
        assert len(request("/app.js")) > 10000
        assert len(request("/styles.css")) > 10000
        print("PASS: packaged runtime and web assets load from an unrelated folder", flush=True)
        sample = request("/api/sample")
        assert sample.startswith(b"%PDF-")
        assert health["version"] == "1.2"
        compressed, headers = request("/api/compress?target=0.18", sample, "application/pdf", with_headers=True)
        assert len(compressed) <= len(sample) * .18
        assert headers["X-Target-Reached"] == "true"
        assert int(headers["X-Original-Bytes"]) == len(sample)
        assert int(headers["X-Compressed-Bytes"]) == len(compressed)
        with pymupdf.open(stream=sample, filetype="pdf") as original, pymupdf.open(stream=compressed, filetype="pdf") as smaller:
            assert len(smaller) == len(original) == 3
            for page, result_page in zip(original, smaller):
                assert page.rect == result_page.rect
                assert page.get_text() == result_page.get_text()
                before_links = [(link["kind"], tuple(link["from"]), link.get("uri")) for link in page.get_links()]
                after_links = [(link["kind"], tuple(link["from"]), link.get("uri")) for link in result_page.get_links()]
                assert before_links == after_links
        print(f"PASS: packaged compression makes {len(compressed):,}-byte PDF ({len(compressed)/len(sample):.1%}), retaining all text and clickable links", flush=True)
        result = json.loads(request("/api/convert?layout=faithful", sample, "application/pdf"))
        assert result["pageCount"] == 3
        assert result["stats"]["annotationLinkCount"] == 3
        assert sum(link.get("source") == "qr" for link in result["links"]) >= 2
        assert result["layout"] == "faithful"
        assert result["imageCount"] > 3
        assert result["html"].count('data-pdf-page=') == 3
        assert request(f"/api/documents/{result['id']}/pages/1.jpg").startswith(b"\xff\xd8")
        print("PASS: real sample, native PDF rendering, links, pictures, and QR decoder", flush=True)
        eml = request("/api/export-eml", json.dumps({"html": result["html"], "subject": "Packaged app check"}).encode(), "application/json")
        message = BytesParser(policy=policy.default).parsebytes(eml)
        assert message["X-Unsent"] == "1"
        assert sum(part.get_content_maintype() == "image" for part in message.walk()) > 3
        html = message.get_body(preferencelist=("html",)).get_content()
        assert "table-layout:fixed" in html
        assert "mso-table-lspace:0pt" in html
        for link in result["links"]:
            assert link["url"].replace("&", "&amp;") in html
        print("PASS: exported email embeds the matching PDF layout and preserves its links", flush=True)
        # A second double-click must reuse the listener without a second server.
        repeat = subprocess.run([str(executable), "--no-browser", "--port", str(port)], cwd=unrelated_directory, env=environment, creationflags=subprocess.CREATE_NO_WINDOW, timeout=30)
        assert repeat.returncode == 0
        print("PASS: repeat launch safely reuses the running app", flush=True)
        process.wait(timeout=100)
        assert process.returncode == 0
        print("PASS: packaged process shuts down cleanly", flush=True)
    finally:
        if process.poll() is None:
            # Only the test-owned process tree is eligible for forced cleanup.
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
