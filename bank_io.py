# -*- coding: utf-8 -*-
"""Question bank load/save + online public-source import."""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from html import unescape
from pathlib import Path

ONLINE_SOURCES = [
    {
        "id": "rbtips",
        "name": "rbtips 繁简题库",
        "url": "https://www.rbtips.com/2020/05/lyceum-of-wisdom.html",
        "kind": "rbtips",
    },
    {
        "id": "lyceum-json",
        "name": "GitHub Lyceum JSON",
        "url": "https://cdn.jsdelivr.net/gh/AndreVarandas/lyceum-of-wisdom-questions@main/data.json",
        "kind": "json-list",
    },
    {
        "id": "gamerempire",
        "name": "GamerEmpire EN",
        "url": "https://gamerempire.net/rise-of-kingdoms-all-peerless-scholar-questions-answers/",
        "kind": "html-table",
    },
]


def resource_root() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return Path(__file__).resolve().parent


def user_data_dir() -> Path:
    """Writable dir for user-added questions (works even if installed to Program Files)."""
    if getattr(sys, "frozen", False):
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "万国觉醒题库"
    else:
        base = resource_root() / "data" / "user"
    base.mkdir(parents=True, exist_ok=True)
    return base


def user_questions_path() -> Path:
    return user_data_dir() / "user_questions.json"


def clean_answer(answer: str) -> str:
    answer = (answer or "").strip()
    answer = re.sub(r"答案查看详情>?|答案详情查看>?", "", answer).strip()
    for left, right in (("『", "』"), ("「", "」"), ("“", "”"), ("'", "'"), ('"', '"')):
        if answer.startswith(left) and answer.endswith(right):
            answer = answer[len(left) : -len(right)].strip()
    return answer.strip(" \t\"'")


def normalize_key(question: str) -> str:
    q = re.sub(r"\s+", "", question or "")
    q = q.replace("？", "?").replace("，", ",").replace("。", ".")
    return q.casefold()


def _read_json_list(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if isinstance(data, dict):
        items = data.get("questions", [])
    elif isinstance(data, list):
        items = data
    else:
        items = []
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        q = str(item.get("question", "")).strip()
        a = clean_answer(str(item.get("answer", "")))
        if q and a:
            out.append(
                {
                    "question": q,
                    "answer": a,
                    "source": str(item.get("source", "user")),
                }
            )
    return out


def load_builtin_questions() -> list[dict]:
    path = resource_root() / "data" / "questions.json"
    if not path.is_file():
        raise FileNotFoundError(f"题库文件缺失: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    questions = payload.get("questions", [])
    if not isinstance(questions, list) or not questions:
        raise ValueError("题库为空或格式错误")
    return [
        {
            "question": str(q.get("question", "")).strip(),
            "answer": clean_answer(str(q.get("answer", ""))),
            "source": str(q.get("source", "builtin")),
        }
        for q in questions
        if isinstance(q, dict) and str(q.get("question", "")).strip() and str(q.get("answer", "")).strip()
    ]


def load_user_questions() -> list[dict]:
    return _read_json_list(user_questions_path())


def merge_questions(*groups: list[dict]) -> list[dict]:
    seen: set[str] = set()
    merged: list[dict] = []
    for group in groups:
        for item in group:
            key = normalize_key(item["question"])
            if not key or key in seen:
                continue
            seen.add(key)
            merged.append(item)
    return merged


def save_user_questions(items: list[dict]) -> None:
    path = user_questions_path()
    payload = {
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(items),
        "questions": items,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def add_user_question(question: str, answer: str, source: str = "manual") -> dict:
    q = re.sub(r"\s+", " ", (question or "").strip())
    a = clean_answer(answer or "")
    if len(q) < 4:
        raise ValueError("题目太短")
    if not a:
        raise ValueError("答案不能为空")

    users = load_user_questions()
    key = normalize_key(q)
    for item in users:
        if normalize_key(item["question"]) == key:
            item["answer"] = a
            item["source"] = source
            save_user_questions(users)
            return {"status": "updated", "question": q, "answer": a}

    users.append({"question": q, "answer": a, "source": source})
    save_user_questions(users)
    return {"status": "added", "question": q, "answer": a}


def append_imported(items: list[dict], source: str) -> tuple[int, int]:
    """Append new items into user store. Returns (added, skipped)."""
    users = load_user_questions()
    seen = {normalize_key(x["question"]) for x in users}
    # also skip if already in builtin later by caller; here only user file
    added = 0
    skipped = 0
    for item in items:
        q = str(item.get("question", "")).strip()
        a = clean_answer(str(item.get("answer", "")))
        key = normalize_key(q)
        if not key or not a:
            skipped += 1
            continue
        if key in seen:
            skipped += 1
            continue
        seen.add(key)
        users.append({"question": q, "answer": a, "source": source})
        added += 1
    if added:
        save_user_questions(users)
    return added, skipped


def _http_get(url: str, timeout: float = 45) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "RoKQuizBank/1.0 (local desktop; import public quiz lists)",
            "Accept": "text/html,application/json,*/*",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    for enc in ("utf-8", "gb18030", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="ignore")


def parse_rbtips_html(text: str) -> list[dict]:
    # Prefer simplified section if present
    parts = re.split(r"簡體版|简体版", text, maxsplit=1)
    target = parts[1] if len(parts) > 1 else text
    rows: list[dict] = []

    # Markdown-style tables (from some scrapers / mirrors)
    for line in target.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        if "題目" in line or "题目" in line:
            continue
        if re.match(r"^\|\s*-+", line):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 2:
            continue
        q, a = cells[0], clean_answer(cells[1])
        if len(q) >= 4 and a:
            rows.append({"question": q, "answer": a})

    # Real blogspot HTML tables
    if len(rows) < 20:
        rows = parse_html_table(target) or parse_html_table(text)

    # Numbered Q/A fallback
    if len(rows) < 20:
        for match in re.finditer(
            r"(\d+)[、.．]\s*(.+?)(?:答[:：]\s*)?『(.+?)』",
            text,
            re.S,
        ):
            q = re.sub(r"\s+", " ", match.group(2)).strip(" ：:.-")
            a = clean_answer(match.group(3))
            if len(q) >= 4 and a:
                rows.append({"question": q, "answer": a})
    return rows


def parse_html_table(text: str) -> list[dict]:
    text = re.sub(r"<script[\s\S]*?</script>", " ", text, flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
    rows: list[dict] = []
    for match in re.finditer(
        r"<tr[^>]*>\s*<td[^>]*>(.*?)</td>\s*<td[^>]*>(.*?)</td>",
        text,
        re.I | re.S,
    ):
        q = unescape(re.sub(r"<[^>]+>", "", match.group(1)))
        a = unescape(re.sub(r"<[^>]+>", "", match.group(2)))
        q = re.sub(r"\s+", " ", q).strip()
        a = clean_answer(re.sub(r"\s+", " ", a))
        if len(q) >= 6 and a and q.lower() not in {"question", "题目", "題目"}:
            rows.append({"question": q, "answer": a})
    return rows


def parse_json_list(text: str) -> list[dict]:
    data = json.loads(text)
    if isinstance(data, dict):
        data = data.get("questions", data.get("data", []))
    rows = []
    if not isinstance(data, list):
        return rows
    for item in data:
        if not isinstance(item, dict):
            continue
        q = str(item.get("question", "")).strip()
        a = clean_answer(str(item.get("answer", "")))
        if q and a:
            rows.append({"question": q, "answer": a})
    return rows


def import_from_online() -> dict:
    """Fetch known public sources and merge new questions into user store."""
    builtin_keys = {normalize_key(q["question"]) for q in load_builtin_questions()}
    user_keys = {normalize_key(q["question"]) for q in load_user_questions()}
    report = {"sources": [], "added": 0, "skipped": 0, "failed": []}

    for src in ONLINE_SOURCES:
        entry = {"id": src["id"], "name": src["name"], "fetched": 0, "added": 0}
        try:
            text = _http_get(src["url"])
            if src["kind"] == "rbtips":
                items = parse_rbtips_html(text)
            elif src["kind"] == "json-list":
                items = parse_json_list(text)
            else:
                items = parse_html_table(text)
            entry["fetched"] = len(items)

            fresh = []
            for item in items:
                key = normalize_key(item["question"])
                if key in builtin_keys or key in user_keys:
                    report["skipped"] += 1
                    continue
                user_keys.add(key)
                fresh.append(item)
            added, skipped = append_imported(fresh, source=f"online:{src['id']}")
            entry["added"] = added
            report["added"] += added
            report["skipped"] += skipped
        except Exception as exc:
            report["failed"].append({"id": src["id"], "error": str(exc)})
        report["sources"].append(entry)

    return report
