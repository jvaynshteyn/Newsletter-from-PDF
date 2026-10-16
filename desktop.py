"""Windowed entry point for the standalone Newsletter Studio application."""
from __future__ import annotations

import argparse
import errno
import os
from pathlib import Path
import sys
import threading
import webbrowser

# Windows GUI executables have no console streams. Keep dependency diagnostics
# and the HTTP handler's ordinary print calls from failing on a missing stream.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

import tkinter as tk
from tkinter import messagebox

import server


def sample_location(app_location: Path) -> Path | None:
    """Find the optional example beside Code, even from a frozen temp folder."""
    filename = "PSIS 499 PTA Newsletter_ October.pdf"
    for parent in (app_location, app_location.parent, app_location.parent.parent):
        candidate = parent / "October" / filename
        if candidate.is_file():
            return candidate
    return None


def start_service(port: int):
    """Start one local instance, or return None if this app already owns it."""
    try:
        instance = server.NewsletterServer(("127.0.0.1", port), server.Handler)
    except OSError as error:
        in_use = error.errno in {errno.EADDRINUSE, 10048} or getattr(error, "winerror", None) == 10048
        if in_use and server.existing_app_running(port, expected_version=server.APP_VERSION):
            return None
        if not in_use or not server.existing_app_running(port):
            raise
        # Keep an older instance and any email edits intact while opening the
        # updated application at the next available local address.
        for candidate in range(port + 1, min(port + 6, 65536)):
            try:
                instance = server.NewsletterServer(("127.0.0.1", candidate), server.Handler)
                break
            except OSError as next_error:
                if next_error.errno not in {errno.EADDRINUSE, 10048} and getattr(next_error, "winerror", None) != 10048:
                    raise
        else:
            raise error
    worker = threading.Thread(target=instance.serve_forever, kwargs={"poll_interval": .2}, daemon=True)
    worker.start()
    return instance


def stop_service(instance):
    if instance is not None:
        instance.shutdown()
        instance.server_close()
        with server.DOCUMENT_LOCK:
            server.DOCUMENTS.clear()


def main():
    parser = argparse.ArgumentParser(description="Open Newsletter Studio without a terminal.")
    parser.add_argument("--port", type=int, default=8799)
    parser.add_argument("--no-browser", action="store_true", help="Do not open the browser automatically")
    # Used by the packaged smoke test; this stays off in ordinary double-click use.
    parser.add_argument("--smoke-test", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        return 1
    app_location = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    if "NEWSLETTER_SAMPLE" not in os.environ:
        sample = sample_location(app_location)
        if sample:
            os.environ["NEWSLETTER_SAMPLE"] = str(sample)
    selected_port = next((candidate for candidate in range(args.port, min(args.port + 6, 65536))
                          if server.existing_app_running(candidate, expected_version=server.APP_VERSION)), args.port)
    url = f"http://127.0.0.1:{selected_port}"
    try:
        instance = start_service(selected_port)
    except OSError:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("Newsletter Studio", f"The local address {url} is in use by another app.\n\nClose that app or restart Newsletter Studio with a different port.", parent=root)
        root.destroy()
        return 1
    if instance is None:
        if not args.no_browser:
            webbrowser.open(url)
        return 0
    url = f"http://127.0.0.1:{instance.server_port}"
    if args.smoke_test:
        # Allow a real frozen executable to be exercised through HTTP, then
        # release the process and its temporary bundled files automatically.
        threading.Event().wait(90)
        stop_service(instance)
        return 0
    root = None
    try:
        root = tk.Tk()
        root.title("Newsletter Studio")
        root.geometry("440x290")
        root.resizable(False, False)
        root.configure(background="#f5f5f0")
        frame = tk.Frame(root, bg="#f5f5f0", padx=28, pady=26)
        frame.pack(fill="both", expand=True)
        tk.Label(frame, text="Newsletter Studio", font=("Segoe UI", 20, "bold"), fg="#19574a", bg="#f5f5f0").pack(anchor="w")
        tk.Label(frame, text="Your newsletter workspace is ready.", font=("Segoe UI", 11), fg="#53665b", bg="#f5f5f0").pack(anchor="w", pady=(12, 5))
        tk.Label(frame, text="Keep this window open while editing your email.\nSave or copy your email before quitting.", font=("Segoe UI", 10), justify="left", fg="#78837c", bg="#f5f5f0").pack(anchor="w")
        tk.Label(frame, text="Runs on your computer. No cloud uploads.", font=("Segoe UI", 9), fg="#78837c", bg="#f5f5f0").pack(anchor="w", pady=(14, 18))
        buttons = tk.Frame(frame, bg="#f5f5f0")
        buttons.pack(fill="x")
        tk.Button(buttons, text="Open workspace", command=lambda: webbrowser.open(url), font=("Segoe UI", 10, "bold"), bg="#19574a", fg="white", activebackground="#104538", activeforeground="white", borderwidth=0, padx=18, pady=9, cursor="hand2").pack(side="left")

        def quit_app():
            if messagebox.askokcancel("Quit Newsletter Studio?", "Save or copy any email edits before quitting.\n\nClose Newsletter Studio now?", parent=root):
                root.destroy()

        tk.Button(buttons, text="Quit", command=quit_app, font=("Segoe UI", 10), bg="#f5f5f0", fg="#53665b", borderwidth=0, padx=18, pady=9, cursor="hand2").pack(side="right")
        root.protocol("WM_DELETE_WINDOW", quit_app)
        if not args.no_browser:
            root.after(250, lambda: webbrowser.open(url))
        root.mainloop()
    finally:
        stop_service(instance)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
