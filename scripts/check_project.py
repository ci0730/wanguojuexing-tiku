# -*- coding: utf-8 -*-
"""Local project health check before packaging."""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    errors: list[str] = []
    sys.path.insert(0, str(ROOT))

    for name in ("app.py", "bank_io.py", "desktop.py", "ocr_config.py"):
        path = ROOT / name
        try:
            ast.parse(path.read_text(encoding="utf-8"))
            print("syntax OK", name)
        except Exception as exc:
            errors.append(f"syntax {name}: {exc}")

    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    checks = {
        "answer_before_preview": html.find('id="result"') < html.find('id="preview"'),
        "hide_scrollbar": "scrollbar-width: none" in html,
        "paste_replace": "solveToken" in html,
        "more_btn": "moreBtn" in html,
        "credit_ok": ("博学智识" in html) and ("74896887065" not in html),
        "bank_ui": "onlineBtn" in html and "addBtn" in html,
        "compact_preview": "max-height: 96px" in html,
    }
    for key, ok in checks.items():
        print(("OK" if ok else "BAD"), key)
        if not ok:
            errors.append(key)

    from app import app, reload_bank

    reload_bank()
    client = app.test_client()
    health = client.get("/api/health").get_json()
    if not health.get("ok"):
        errors.append(f"health failed: {health}")
    else:
        print("health OK", health.get("total"))

    stats = client.get("/api/stats").get_json()
    print("stats", stats.get("total"), "zh", stats.get("zh"), "user", stats.get("user_added"))

    add = client.post(
        "/api/questions/add",
        json={"question": "健康检查补题唯一题干ABCDEF", "answer": "健康答案"},
    ).get_json()
    if add.get("status") not in {"added", "updated"}:
        errors.append(f"add failed: {add}")
    else:
        print("add OK")

    search = client.get("/api/search?q=健康检查补题唯一题干").get_json()
    ans = (search.get("results") or [{}])[0].get("answer")
    if ans != "健康答案":
        errors.append(f"search failed: {ans}")
    else:
        print("search OK")

    sample = ROOT / "data" / "sample-q1.png"
    if sample.is_file():
        with sample.open("rb") as fh:
            resp = client.post(
                "/api/solve",
                data={"image": (fh, "q.png")},
                content_type="multipart/form-data",
            )
        data = resp.get_json()
        print("solve", data.get("answer"), data.get("confidence"), data.get("timing_ms"))
        if data.get("answer") != "伦敦国家美术馆":
            errors.append(f"solve unexpected: {data.get('answer')}")
    else:
        print("WARN no sample image")

    for p in (
        ROOT / "web" / "assets" / "app.png",
        ROOT / "assets" / "app.ico",
        ROOT / "data" / "questions.json",
    ):
        if not p.exists():
            errors.append(f"missing {p}")
        else:
            print("file OK", p.name)

    bank = json.loads((ROOT / "data" / "questions.json").read_text(encoding="utf-8"))
    print("bank count", bank.get("count"))

    if errors:
        print("=== FAILURES ===")
        for e in errors:
            print("-", e)
        return 1
    print("=== ALL LOCAL CHECKS PASS ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
