"""Build a single Windows GUI executable with its Python runtime included."""
from pathlib import Path
import os
import sys

root = Path(__file__).resolve().parent
os.chdir(root)
for extra in (root / ".build-tools", root / ".vendor"):
    if extra.is_dir():
        sys.path.insert(0, str(extra))

import PyInstaller.__main__

PyInstaller.__main__.run([
    "--noconfirm", "--onefile", "--windowed", "--noupx",
    "--name", "Newsletter Studio",
    "--distpath", str(root / "dist"),
    "--workpath", str(root / "build"),
    "--specpath", str(root),
    "--paths", str(root / ".vendor"),
    "--collect-all", "pymupdf",
    "--collect-all", "zxingcpp",
    "--add-data", f"{root / 'index.html'};.",
    "--add-data", f"{root / 'app.js'};.",
    "--add-data", f"{root / 'styles.css'};.",
    str(root / "desktop.py"),
])
