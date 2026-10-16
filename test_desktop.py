"""Launcher regression checks: python -B -m unittest test_desktop.py."""
import errno
import http.client
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

import desktop
import server


SAMPLE_NAME = "PSIS 499 PTA Newsletter_ October.pdf"


class SampleLocationTests(unittest.TestCase):
    def test_finds_sample_with_executable_in_code_or_nested_pta_folder(self):
        with tempfile.TemporaryDirectory() as folder:
            newsletter = Path(folder) / "PTA newsletter"
            sample = newsletter / "October" / SAMPLE_NAME
            sample.parent.mkdir(parents=True)
            sample.write_bytes(b"%PDF-example")
            for app_location in (newsletter / "Code", newsletter / "Code" / "PTA"):
                with self.subTest(app_location=app_location):
                    self.assertEqual(desktop.sample_location(app_location), sample)

    def test_prefers_nearest_example_and_requires_a_file(self):
        with tempfile.TemporaryDirectory() as folder:
            app_location = Path(folder) / "Code"
            nearest = app_location / "October" / SAMPLE_NAME
            farther = app_location.parent / "October" / SAMPLE_NAME
            nearest.parent.mkdir(parents=True)
            farther.parent.mkdir(parents=True)
            nearest.mkdir()
            farther.write_bytes(b"%PDF-example")
            self.assertEqual(desktop.sample_location(app_location), farther)
            nearest.rmdir()
            nearest.write_bytes(b"%PDF-nearest")
            self.assertEqual(desktop.sample_location(app_location), nearest)

    def test_missing_sample_is_optional(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertIsNone(desktop.sample_location(Path(folder) / "Code" / "PTA"))

    def test_frozen_launcher_uses_executable_folder_for_example(self):
        with tempfile.TemporaryDirectory() as folder:
            executable = Path(folder) / "Code" / "Newsletter Studio.exe"
            sample = Path(folder) / "October" / SAMPLE_NAME
            sample.parent.mkdir(parents=True)
            sample.write_bytes(b"%PDF-example")
            with patch.object(sys, "frozen", True, create=True), \
                    patch.object(sys, "executable", str(executable)), \
                    patch.object(sys, "argv", [str(executable), "--no-browser"]), \
                    patch.dict(os.environ, {}, clear=True), \
                    patch.object(desktop, "start_service", return_value=None) as start, \
                    patch.object(desktop.tk, "Tk") as window:
                self.assertEqual(desktop.main(), 0)
                self.assertEqual(os.environ["NEWSLETTER_SAMPLE"], str(sample))
                start.assert_called_once_with(8799)
                window.assert_not_called()


class ServiceLifecycleTests(unittest.TestCase):
    def tearDown(self):
        with server.DOCUMENT_LOCK:
            server.DOCUMENTS.clear()

    def test_reuses_only_recognized_newsletter_server(self):
        occupied = OSError(errno.EADDRINUSE, "Address in use")
        with patch.object(server, "NewsletterServer", side_effect=occupied), \
                patch.object(server, "existing_app_running", return_value=True) as existing, \
                patch.object(desktop.threading, "Thread") as worker:
            self.assertIsNone(desktop.start_service(8799))
            existing.assert_called_once_with(8799, expected_version=server.APP_VERSION)
            worker.assert_not_called()

    def test_rejects_unrelated_listener(self):
        occupied = OSError(errno.EADDRINUSE, "Address in use")
        with patch.object(server, "NewsletterServer", side_effect=occupied), \
                patch.object(server, "existing_app_running", return_value=False), \
                patch.object(desktop.threading, "Thread") as worker:
            with self.assertRaises(OSError) as raised:
                desktop.start_service(8799)
            self.assertIs(raised.exception, occupied)
            worker.assert_not_called()

    def test_other_startup_errors_are_not_mistaken_for_existing_app(self):
        denied = PermissionError(errno.EACCES, "Permission denied")
        with patch.object(server, "NewsletterServer", side_effect=denied), \
                patch.object(server, "existing_app_running") as existing:
            with self.assertRaises(PermissionError):
                desktop.start_service(8799)
            existing.assert_not_called()

    def test_older_app_keeps_its_documents_and_updated_app_uses_next_port(self):
        occupied = OSError(errno.EADDRINUSE, "Address in use")
        replacement = unittest.mock.MagicMock()
        with patch.object(server, "NewsletterServer", side_effect=[occupied, replacement]) as constructor, \
                patch.object(server, "existing_app_running", side_effect=[False, True]), \
                patch.object(desktop.threading, "Thread") as worker:
            self.assertIs(desktop.start_service(8799), replacement)
            self.assertEqual(constructor.call_args_list[1].args[0], ("127.0.0.1", 8800))
            worker.return_value.start.assert_called_once()

    def test_real_service_answers_health_then_stops_and_clears_uploaded_pdf(self):
        instance = desktop.start_service(0)
        self.assertIsNotNone(instance)
        port = instance.server_port
        try:
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            try:
                connection.request("GET", "/api/health")
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(json.loads(response.read())["app"], "newsletter-studio")
            finally:
                connection.close()
            with server.DOCUMENT_LOCK:
                server.DOCUMENTS["example"] = b"private PDF contents"
        finally:
            desktop.stop_service(instance)
        self.assertFalse(server.DOCUMENTS)
        with self.assertRaises(OSError):
            with socket.create_connection(("127.0.0.1", port), timeout=.5):
                pass

    def test_non_owner_stop_preserves_other_service_documents(self):
        with server.DOCUMENT_LOCK:
            server.DOCUMENTS["example"] = b"private PDF contents"
        desktop.stop_service(None)
        self.assertIn("example", server.DOCUMENTS)


if __name__ == "__main__":
    unittest.main()
