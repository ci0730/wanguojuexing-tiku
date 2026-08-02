# -*- coding: utf-8 -*-
"""Upload local site hero assets via GitHub Contents API when git push is flaky."""
from __future__ import annotations

import base64
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = "ci0730/wanguojuexing-tiku"
FILES = [
    ROOT / "site" / "assets" / "hero-battle.jpg",
    ROOT / "site" / "index.html",
    ROOT / "scripts" / "clean_hero_bg.py",
]


def gh_api(method: str, path: str, body: dict | None = None) -> dict:
    cmd = ["gh", "api", "-X", method, path]
    if body is not None:
        cmd.extend(["--input", "-"])
    proc = subprocess.run(
        cmd,
        input=json.dumps(body) if body is not None else None,
        text=True,
        capture_output=True,
        encoding="utf-8",
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or proc.stdout or f"gh api failed: {path}")
    return json.loads(proc.stdout) if proc.stdout.strip() else {}


def upsert(path: Path) -> None:
    rel = path.relative_to(ROOT).as_posix()
    api_path = f"repos/{REPO}/contents/{rel}"
    content_b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    body = {
        "message": f"Update {rel} for battle hero background",
        "content": content_b64,
        "branch": "master",
    }
    try:
        existing = gh_api("GET", f"{api_path}?ref=master")
        body["sha"] = existing["sha"]
    except RuntimeError as exc:
        if "404" not in str(exc) and "Not Found" not in str(exc):
            # GET may fail with Not Found for new files — continue create
            if "Not Found" not in str(exc) and '"status":"404"' not in str(exc):
                print("GET warn:", exc, file=sys.stderr)

    # Retry GET properly
    check = subprocess.run(
        ["gh", "api", f"{api_path}?ref=master"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if check.returncode == 0:
        body["sha"] = json.loads(check.stdout)["sha"]

    result = gh_api("PUT", api_path, body)
    print("OK", rel, result.get("content", {}).get("sha", "")[:8])


def main() -> int:
    for path in FILES:
        if not path.is_file():
            print("missing", path, file=sys.stderr)
            return 1
        upsert(path)
    # trigger pages workflow
    subprocess.run(
        ["gh", "workflow", "run", "Deploy landing site", "--repo", REPO],
        check=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
