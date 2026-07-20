# -*- coding: utf-8 -*-
"""Smoke-test the packaged EXE as if the machine has no Python project deps."""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXE = ROOT / "dist" / "万国觉醒题库" / "万国觉醒题库.exe"
SAMPLE = ROOT / "data" / "sample-q1.png"
HOST = "127.0.0.1"
PORT = 8765


def http_json(url: str, timeout: float = 8):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_bytes(url: str, timeout: float = 8) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.read()


def post_image(url: str, image_path: Path):
    import uuid

    boundary = f"----Boundary{uuid.uuid4().hex}"
    data = image_path.read_bytes()
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="image"; filename="q.png"\r\n'
        f"Content-Type: image/png\r\n\r\n"
    ).encode("utf-8") + data + f"\r\n--{boundary}--\r\n".encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    errors: list[str] = []
    print("=== packaged EXE smoke test ===")
    if not EXE.is_file():
        print("FAIL: EXE missing", EXE)
        return 1

    # Required bundled files
    base = EXE.parent / "_internal"
    required = [
        base / "web" / "index.html",
        base / "web" / "assets" / "app.png",
        base / "assets" / "app.ico",
        base / "data" / "questions.json",
        base / "rapidocr_onnxruntime",
        base / "onnxruntime",
    ]
    for path in required:
        ok = path.exists()
        print(("OK  " if ok else "MISS"), path.relative_to(EXE.parent))
        if not ok:
            errors.append(f"missing {path}")

    # UI features must be in packaged html
    html = (base / "web" / "index.html").read_text(encoding="utf-8", errors="ignore")
    for needle, label in (
        ("solveToken", "paste-replace"),
        ("scrollbar-width: none", "hidden-scrollbar"),
        ("id=\"result\"", "result"),
        ("onlineBtn", "bank-import"),
        ("博学智识", "credit"),
    ):
        ok = needle in html
        print(("OK  " if ok else "MISS"), "ui:" + label)
        if not ok:
            errors.append(f"ui missing {label}")

    # result should appear before preview in markup
    if html.find('id="result"') > html.find('id="preview"'):
        errors.append("result should be before preview")
        print("MISS ui:answer-before-preview")
    else:
        print("OK   ui:answer-before-preview")

    # Kill leftover
    subprocess.run(
        ["taskkill", "/F", "/IM", "万国觉醒题库.exe"],
        capture_output=True,
        text=True,
    )
    time.sleep(1)

    proc = subprocess.Popen([str(EXE)], cwd=str(EXE.parent))
    print("started pid", proc.pid)

    healthy = False
    for i in range(40):
        time.sleep(0.5)
        try:
            health = http_json(f"http://{HOST}:{PORT}/api/health")
            if health.get("ok") and health.get("app") == "rok-quiz":
                healthy = True
                print("health", health)
                break
        except Exception:
            pass
    if not healthy:
        errors.append("health endpoint not ready")
        print("FAIL: server not healthy")
        proc.terminate()
        return 1

    # UI + avatar
    try:
        html = http_bytes(f"http://{HOST}:{PORT}/").decode("utf-8", errors="ignore")
        if "74896887065" in html:
            errors.append("old Douyin credit still in UI")
        if "博学智识" not in html:
            errors.append("credit text missing in UI")
        if "brand-avatar" not in html:
            errors.append("avatar markup missing")
        png = http_bytes(f"http://{HOST}:{PORT}/assets/app.png")
        if len(png) < 1000:
            errors.append("avatar png too small")
        print("ui/avatar OK", len(png))
    except Exception as exc:
        errors.append(f"ui fetch failed: {exc}")

    # Search
    try:
        search = http_json(
            f"http://{HOST}:{PORT}/api/search?q=" + urllib.request.quote("布匿战争")
        )
        ans = (search.get("results") or [{}])[0].get("answer")
        print("search", ans)
        if not ans:
            errors.append("search returned empty")
    except Exception as exc:
        errors.append(f"search failed: {exc}")

    # OCR solve
    if SAMPLE.is_file():
        try:
            solved = post_image(f"http://{HOST}:{PORT}/api/solve", SAMPLE)
            print("solve", solved.get("answer"), solved.get("confidence"))
            if solved.get("answer") != "伦敦国家美术馆":
                errors.append(f"ocr answer unexpected: {solved.get('answer')}")
        except Exception as exc:
            errors.append(f"solve failed: {exc}")
    else:
        print("WARN: sample image missing, skip OCR")

    # Cleanup
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()
    subprocess.run(
        ["taskkill", "/F", "/IM", "万国觉醒题库.exe"],
        capture_output=True,
        text=True,
    )

    if errors:
        print("=== FAILURES ===")
        for e in errors:
            print("-", e)
        return 1
    print("=== ALL PASS (packaged EXE works without project Python deps) ===")
    print("NOTE: clean PCs still need Microsoft Edge WebView2 Runtime (usually preinstalled on Win10/11).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
