# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for 万国觉醒题库 (self-contained desktop app)."""
import sys
from pathlib import Path

sys.setrecursionlimit(sys.getrecursionlimit() * 5)

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

block_cipher = None
# SPECPATH = directory that contains this .spec file (= packaging/)
root = Path(SPECPATH).resolve().parent


def gather_dir(src: Path, prefix: str) -> list:
    """Collect (src_file, dest_dir) pairs for PyInstaller datas."""
    items = []
    if not src.is_dir():
        return items
    for f in src.rglob("*"):
        if not f.is_file():
            continue
        rel_parent = f.relative_to(src).parent.as_posix()
        dest = prefix if rel_parent in {".", ""} else f"{prefix}/{rel_parent}"
        items.append((str(f), dest))
    return items


datas = [
    (str(root / "data" / "questions.json"), "data"),
    (str(root / "data" / "questions.zh.json"), "data"),
]
datas += gather_dir(root / "web", "web")
datas += gather_dir(root / "assets", "assets")

datas += collect_data_files("rapidocr_onnxruntime")
try:
    datas += collect_data_files("webview")
except Exception:
    pass
try:
    datas += collect_data_files("clr_loader")
except Exception:
    pass

binaries = []
binaries += collect_dynamic_libs("onnxruntime")
try:
    binaries += collect_dynamic_libs("cv2")
except Exception:
    pass

hiddenimports = [
    "app",
    "bank_io",
    "ocr_config",
    "desktop",
    "yaml",
    "rapidocr_onnxruntime",
    "rapidocr_onnxruntime.main",
    "onnxruntime",
    "PIL",
    "PIL.Image",
    "numpy",
    "cv2",
    "pyclipper",
    "shapely",
    "flask",
    "rapidfuzz",
    "werkzeug",
    "jinja2",
    "webview",
    "webview.platforms.edgechromium",
    "clr_loader",
    "pythonnet",
    "winreg",
    "pkg_resources",
]

excludes = [
    "tensorflow",
    "tensorflow_core",
    "tensorboard",
    "keras",
    "torch",
    "torchvision",
    "pandas",
    "matplotlib",
    "IPython",
    "notebook",
    "pytest",
    "unittest",
    "tkinter",
    "PyQt5",
    "PyQt6",
    "PySide2",
    "PySide6",
]

a = Analysis(
    [str(root / "desktop.py")],
    pathex=[str(root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="万国觉醒题库",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(root / "assets" / "app.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="万国觉醒题库",
)
