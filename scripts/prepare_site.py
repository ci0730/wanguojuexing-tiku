# -*- coding: utf-8 -*-
"""Prepare landing site assets + portable ZIP for release."""
from __future__ import annotations

import os
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = "1.0.0"
APP_DIR = ROOT / "dist" / "万国觉醒题库"
OUT_DIR = ROOT / "dist" / "installer"
ZIP_NAME = f"万国觉醒题库_便携版_v{VERSION}.zip"
ZIP_PATH = OUT_DIR / ZIP_NAME
SITE_DIR = ROOT / "site"
SITE_ASSETS = SITE_DIR / "assets"


def main() -> int:
    print("==> Syncing site assets...")
    SITE_ASSETS.mkdir(parents=True, exist_ok=True)
    for name in ("app.png", "mascot.png", "app.ico"):
        src = ROOT / "assets" / name
        if not src.is_file():
            print(f"FAIL: missing {src}", file=sys.stderr)
            return 1
        shutil.copy2(src, SITE_ASSETS / name)

    if not APP_DIR.is_dir():
        print(f"FAIL: packaged app folder missing: {APP_DIR}", file=sys.stderr)
        print("Run scripts/build_installer.ps1 first.", file=sys.stderr)
        return 1

    print("==> Creating portable ZIP...")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if ZIP_PATH.exists():
        ZIP_PATH.unlink()

    with zipfile.ZipFile(ZIP_PATH, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in APP_DIR.rglob("*"):
            if path.is_file():
                arc = Path("万国觉醒题库") / path.relative_to(APP_DIR)
                zf.write(path, arcname=str(arc))

    mb = ZIP_PATH.stat().st_size / 1024 / 1024
    print(f"Portable ZIP ready: {ZIP_PATH} ({mb:.1f} MB)")

    release_url = os.environ.get("SITE_RELEASE_ZIP_URL", "").strip()
    if release_url:
        index = SITE_DIR / "index.html"
        html = index.read_text(encoding="utf-8")
        html = html.replace("RELEASE_ZIP_URL_PLACEHOLDER", release_url)
        html = html.replace(
            'href="downloads/万国觉醒题库_便携版_v1.0.0.zip"',
            f'href="{release_url}"',
        )
        index.write_text(html, encoding="utf-8")
        print("Updated download URL in site/index.html")

    print(f'Done. Upload ZIP via: gh release create v{VERSION} "{ZIP_PATH}"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
