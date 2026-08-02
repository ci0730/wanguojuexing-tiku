# -*- coding: utf-8 -*-
"""Merge community submissions into data/community/approved.json.

Examples:
  python scripts/merge_community.py --file path/to/bank.json
  python scripts/merge_community.py --issue 12
  python scripts/merge_community.py --issue 12 --close
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import bank_io  # noqa: E402

APPROVED_PATH = ROOT / "data" / "community" / "approved.json"
REPO = "ci0730/wanguojuexing-tiku"


def _load_approved() -> list[dict]:
    if not APPROVED_PATH.is_file():
        return []
    try:
        return bank_io.parse_user_bank_payload(json.loads(APPROVED_PATH.read_text(encoding="utf-8")))
    except Exception:
        return []


def _save_approved(items: list[dict]) -> None:
    payload = {
        "type": "rok-quiz-user-bank",
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(items),
        "questions": [
            {
                "question": q["question"],
                "answer": q["answer"],
                "source": q.get("source") or "community",
            }
            for q in items
        ],
    }
    APPROVED_PATH.parent.mkdir(parents=True, exist_ok=True)
    APPROVED_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _extract_json_blob(text: str) -> str:
    text = (text or "").strip()
    if not text:
        raise ValueError("Issue 正文为空")
    # Prefer fenced ```json ... ```
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.I)
    if m:
        return m.group(1).strip()
    # GitHub issue form often prefixes fields; find first { ... }
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return text[start : end + 1]
    raise ValueError("未找到 JSON 对象")


def _gh_issue(number: int) -> dict:
    proc = subprocess.run(
        ["gh", "api", f"repos/{REPO}/issues/{number}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or proc.stdout or f"gh api failed for issue #{number}")
    return json.loads(proc.stdout)


def _close_issue(number: int, comment: str) -> None:
    subprocess.run(
        ["gh", "api", "-X", "POST", f"repos/{REPO}/issues/{number}/comments", "--input", "-"],
        input=json.dumps({"body": comment}),
        text=True,
        check=False,
        capture_output=True,
        encoding="utf-8",
    )
    subprocess.run(
        ["gh", "api", "-X", "PATCH", f"repos/{REPO}/issues/{number}", "--input", "-"],
        input=json.dumps({"state": "closed"}),
        text=True,
        check=False,
        capture_output=True,
        encoding="utf-8",
    )


def merge_items(incoming: list[dict]) -> dict:
    """Merge into approved.json. Keep existing on conflict; report conflicts."""
    builtin_same = {
        bank_io.normalize_key(q["question"]): bank_io.clean_answer(q["answer"])
        for q in bank_io.load_builtin_questions()
    }
    approved = _load_approved()
    by_key = {bank_io.normalize_key(q["question"]): i for i, q in enumerate(approved)}

    added = 0
    skipped_same = 0
    skipped_builtin = 0
    conflicts: list[dict] = []

    for item in incoming:
        q = re.sub(r"\s+", " ", str(item.get("question", "")).strip())
        a = bank_io.clean_answer(str(item.get("answer", "")))
        key = bank_io.normalize_key(q)
        if len(q) < 4 or not a or not key:
            skipped_same += 1
            continue
        if key in builtin_same and builtin_same[key] == a:
            skipped_builtin += 1
            continue
        if key in by_key:
            old = approved[by_key[key]]
            if old["answer"] == a:
                skipped_same += 1
            else:
                conflicts.append(
                    {
                        "question": q,
                        "existing": old["answer"],
                        "incoming": a,
                    }
                )
            continue
        approved.append({"question": q, "answer": a, "source": "community"})
        by_key[key] = len(approved) - 1
        added += 1

    if added:
        _save_approved(approved)

    return {
        "added": added,
        "skipped_same": skipped_same,
        "skipped_builtin": skipped_builtin,
        "conflicts": conflicts,
        "approved_total": len(approved),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Merge community bank into approved.json")
    parser.add_argument("--file", type=Path, help="Local JSON file to merge")
    parser.add_argument("--issue", type=int, help="GitHub issue number to merge")
    parser.add_argument("--close", action="store_true", help="Close issue after successful merge")
    args = parser.parse_args()

    if not args.file and not args.issue:
        parser.error("需要 --file 或 --issue")

    raw_text = ""
    if args.file:
        raw_text = args.file.read_text(encoding="utf-8-sig")
    else:
        issue = _gh_issue(args.issue)
        raw_text = issue.get("body") or ""

    blob = _extract_json_blob(raw_text)
    data = json.loads(blob)
    items = bank_io.parse_user_bank_payload(data)
    if not items:
        print("没有有效题目", file=sys.stderr)
        return 1

    report = merge_items(items)
    print(
        f"新增 {report['added']}，跳过重复 {report['skipped_same']}，"
        f"跳过内置相同 {report['skipped_builtin']}，"
        f"冲突 {len(report['conflicts'])}，"
        f"approved 共 {report['approved_total']} 题"
    )
    for c in report["conflicts"][:20]:
        print(f"  冲突: {c['question'][:40]}… 已有={c['existing']} 新={c['incoming']}")

    if args.issue and args.close and report["added"] >= 0 and not report["conflicts"]:
        _close_issue(
            args.issue,
            f"已合并进 `data/community/approved.json`：新增 {report['added']} 题"
            f"（跳过重复 {report['skipped_same']}，跳过内置相同 {report['skipped_builtin']}）。",
        )
        print(f"已关闭 Issue #{args.issue}")
    elif args.issue and args.close and report["conflicts"]:
        print("存在冲突，未自动关闭 Issue（请人工处理）", file=sys.stderr)

    if report["added"]:
        print(f"已写入 {APPROVED_PATH.relative_to(ROOT)}")
        print("请 git commit && push；用户「联网更新」经 jsDelivr 拉取。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
