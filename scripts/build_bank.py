# -*- coding: utf-8 -*-
"""Parse public RoK quiz sources into a unified question bank.

Note: 初试 / 复试 / 终试 share ONE question pool in-game.
Community dumps rarely tag by stage; we keep a single merged bank.
"""
from __future__ import annotations

import json
import re
from html import unescape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
RAW = DATA / "raw"
AGENT = Path(r"C:\Users\zhou\.cursor\projects\f\agent-tools")


def clean_answer(answer: str) -> str:
    answer = answer.strip()
    answer = re.sub(r"答案查看详情>?|答案详情查看>?", "", answer).strip()
    for left, right in (("『", "』"), ("「", "」"), ("“", "”"), ("'", "'"), ('"', '"')):
        if answer.startswith(left) and answer.endswith(right):
            answer = answer[len(left) : -len(right)].strip()
    return answer.strip(" \t\"'")


def parse_markdown_table(text: str) -> list[dict]:
    rows: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        if ("題目" in line or "题目" in line) and "答案" in line:
            continue
        if re.match(r"^\|\s*-+", line):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 2:
            continue
        question, answer = cells[0], clean_answer(cells[1])
        if not question or not answer or question in {"日子", "題目", "题目"}:
            continue
        rows.append({"question": question, "answer": answer})
    return rows


def parse_numbered_quotes(text: str) -> list[dict]:
    rows: list[dict] = []
    # 1、题干 答：『答案』  or 1、题干 『答案』
    pattern = re.compile(
        r"(?:^|[^\d])(\d+)[、.．]\s*(.+?)(?:答[:：]\s*)?『(.+?)』",
        re.S,
    )
    for match in pattern.finditer(text):
        q = re.sub(r"\s+", " ", match.group(2)).strip(" ：:.-")
        q = re.sub(r"答案查看详情>?|答案详情查看>?", "", q).strip()
        a = clean_answer(match.group(3))
        if len(q) >= 4 and a:
            rows.append({"question": q, "answer": a})
    return rows


def parse_html_two_col_table(text: str) -> list[dict]:
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
        if len(q) < 6 or not a:
            continue
        if q.lower() in {"question", "题目", "題目"}:
            continue
        rows.append({"question": q, "answer": a})
    return rows


def parse_kingrider(text: str) -> list[dict]:
    rows: list[dict] = []
    pattern = re.compile(
        r"Quest[aã]o\s+\d+\s*\n(.+?)\nR:\s*(.+?)(?=\n\n|\nQuest|\Z)",
        re.I | re.S,
    )
    for match in pattern.finditer(text):
        q = re.sub(r"\s+", " ", match.group(1)).strip()
        a = clean_answer(re.sub(r"\s+", " ", match.group(2)))
        if len(q) >= 4 and a:
            rows.append({"question": q, "answer": a})
    return rows


def parse_english_json(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = []
    for item in data:
        q = str(item.get("question", "")).strip()
        a = clean_answer(str(item.get("answer", "")))
        if q and a:
            rows.append({"question": q, "answer": a})
    return rows


def normalize_key(question: str) -> str:
    q = re.sub(r"\s+", "", question)
    q = q.replace("？", "?").replace("，", ",").replace("。", ".")
    return q.casefold()


def add_all(merged: list[dict], seen: set[str], items: list[dict], source: str) -> int:
    added = 0
    for item in items:
        key = normalize_key(item["question"])
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(
            {
                "question": item["question"],
                "answer": item["answer"],
                "source": source,
            }
        )
        added += 1
    return added


def parse_rbtips(path: Path) -> tuple[list[dict], list[dict]]:
    text = path.read_text(encoding="utf-8")
    parts = re.split(r"\n簡體版\n", text, maxsplit=1)
    traditional = parse_markdown_table(parts[0])
    simplified = parse_markdown_table(parts[1]) if len(parts) > 1 else []
    return traditional, simplified


def first_existing(*paths: Path) -> Path | None:
    for path in paths:
        if path and path.exists():
            return path
    return None


def main() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    RAW.mkdir(parents=True, exist_ok=True)
    merged: list[dict] = []
    seen: set[str] = set()
    stats: dict[str, int] = {}

    rbtips = first_existing(
        RAW / "rbtips.txt",
        AGENT / "3780c105-3282-46d6-8ead-d9afff68aec1.txt",
        AGENT / "804352e4-c83e-4942-a687-bb9d54037f15.txt",
    )
    if rbtips:
        traditional, simplified = parse_rbtips(rbtips)
        stats["rbtips-zh-hans"] = add_all(merged, seen, simplified, "rbtips-zh-hans")
        stats["rbtips-zh-hant"] = add_all(merged, seen, traditional, "rbtips-zh-hant")

    for name, label in (
        ("0946b13b-5331-4ea8-b79d-18b6a51d3537.txt", "9game"),
        ("247ca1b2-6bd2-4552-a73e-c13cc2480b6e.txt", "9game"),
        ("ecf694ca-6a35-434c-b071-3f473acfa4f7.txt", "233leyuan"),
    ):
        path = first_existing(RAW / name, AGENT / name)
        if not path:
            continue
        rows = parse_numbered_quotes(path.read_text(encoding="utf-8", errors="ignore"))
        stats[label] = stats.get(label, 0) + add_all(merged, seen, rows, label)

    en_path = DATA / "lyceum-en-multi.json"
    if en_path.exists():
        stats["lyceum-en"] = add_all(merged, seen, parse_english_json(en_path), "lyceum-en")

    ge = RAW / "gamerempire.html"
    if ge.exists():
        stats["gamerempire-en"] = add_all(
            merged, seen, parse_html_two_col_table(ge.read_text(encoding="utf-8", errors="ignore")), "gamerempire-en"
        )

    kr = first_existing(RAW / "kingrider.txt", DATA / "kingrider-readme.txt")
    if kr:
        stats["kingrider-pt"] = add_all(
            merged, seen, parse_kingrider(kr.read_text(encoding="utf-8", errors="ignore")), "kingrider-pt"
        )

    extras = [
        {
            "question": "波提切利的著名画作《维纳斯和战神》色泽纯洁鲜明，人物形态完美。如今这幅名画藏于下列哪处美术馆中？",
            "answer": "伦敦国家美术馆",
        },
        {
            "question": "波提切利的著名画作《维纳斯和战神》如今这幅名画藏于下列哪处美术馆中？",
            "answer": "伦敦国家美术馆",
        },
    ]
    stats["manual"] = add_all(merged, seen, extras, "manual")

    out = DATA / "questions.json"
    payload = {
        "version": "2026-07-19b",
        "note": "初试/复试/终试共用同一题池，公开整理无法保证覆盖全部新题",
        "count": len(merged),
        "sources": stats,
        "questions": merged,
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    zh_only = [q for q in merged if re.search(r"[\u4e00-\u9fff]", q["question"])]
    (DATA / "questions.zh.json").write_text(
        json.dumps(
            {"version": payload["version"], "count": len(zh_only), "questions": zh_only},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(json.dumps({"total": len(merged), "zh": len(zh_only), "stats": stats}, ensure_ascii=False))


if __name__ == "__main__":
    main()
