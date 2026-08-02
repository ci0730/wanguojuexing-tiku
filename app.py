# -*- coding: utf-8 -*-
"""Rise of Kingdoms quiz helper: screenshot OCR + fuzzy match."""
from __future__ import annotations

import io
import json
import logging
import re
import sys
import threading
import time
import traceback
import webbrowser
from functools import lru_cache
from pathlib import Path

import numpy as np
from flask import Flask, Response, jsonify, request, send_from_directory
from PIL import Image
from rapidfuzz import fuzz, process

import bank_io

HOST = "127.0.0.1"
PORT = 8765
APP_ID = "rok-quiz"


def resource_root() -> Path:
    return bank_io.resource_root()


ROOT = resource_root()
DATA = ROOT / "data"
WEB = ROOT / "web"

app = Flask(__name__, static_folder=str(WEB), static_url_path="")
log = logging.getLogger("rok-quiz")

_OCR_LOCK = threading.Lock()
_OCR_READY = False


def normalize_text(text: str) -> str:
    text = text.replace("\u3000", " ")
    text = re.sub(r"\s+", "", text)
    text = text.replace("？", "?").replace("，", ",").replace("。", ".")
    text = text.replace("：", ":").replace("（", "(").replace("）", ")")
    # Strip question-number prefixes only (Q5. / 14、), keep answers like 14根
    text = re.sub(r"^[QqＱｑ]\d+[.、:：]?", "", text)
    text = re.sub(r"^\d+[.、:：]", "", text)
    return text.casefold()


def reload_bank() -> list[dict]:
    load_bank.cache_clear()
    bank_index.cache_clear()
    return load_bank()


@lru_cache(maxsize=1)
def load_bank() -> list[dict]:
    builtin = bank_io.load_builtin_questions()
    users = bank_io.load_user_questions()
    merged = bank_io.merge_questions(builtin, users)
    if not merged:
        raise ValueError("题库为空或格式错误")
    return merged


@lru_cache(maxsize=1)
def bank_index() -> dict:
    """Precomputed structures for fast search."""
    bank = load_bank()
    q_map = {i: item["question"] for i, item in enumerate(bank)}
    a_map = {i: item["answer"] for i, item in enumerate(bank)}
    q_norm = [normalize_text(item["question"]) for item in bank]
    a_norm = [normalize_text(item["answer"]) for item in bank]
    return {
        "bank": bank,
        "q_map": q_map,
        "a_map": a_map,
        "q_norm": q_norm,
        "a_norm": a_norm,
    }


@lru_cache(maxsize=1)
def get_ocr():
    from rapidocr_onnxruntime import RapidOCR

    import ocr_config

    # Always rebuild at runtime so model paths point to THIS machine/package
    cfg = bank_io.user_data_dir() / "ocr_fast.yaml"
    try:
        ocr_config.write_fast_ocr_config(cfg)
        return RapidOCR(config_path=str(cfg))
    except Exception:
        log.exception("fast OCR config failed, fallback default")
        return RapidOCR()


def warm_up() -> dict:
    """Preload bank + OCR so first screenshot is fast."""
    global _OCR_READY
    t0 = time.perf_counter()
    bank = load_bank()
    bank_index()
    t_bank = time.perf_counter() - t0

    t1 = time.perf_counter()
    with _OCR_LOCK:
        ocr = get_ocr()
        # Warm common crop shapes to avoid first-call ONNX compile stall
        for shape in ((120, 480, 3), (160, 640, 3), (220, 640, 3)):
            dummy = np.full(shape, 255, dtype=np.uint8)
            try:
                ocr(dummy)
            except Exception:
                pass
        _OCR_READY = True
    t_ocr = time.perf_counter() - t1
    return {
        "ok": True,
        "total": len(bank),
        "bank_ms": round(t_bank * 1000),
        "ocr_ms": round(t_ocr * 1000),
        "ready": _OCR_READY,
    }


_OPTION_RE = re.compile(r"^([ABCDＡＢＣＤ])[.、:：\s]*(.+)$")
_OPTION_INLINE_RE = re.compile(r"([ABCDＡＢＣＤ])[.、:：\s]*([^ABCDＡＢＣＤ]+)")
_ANSWER_HINT_RE = re.compile(r"(?:正确答案|正确选项|答案)\s*[:：]?\s*(.+)$")
_LETTER_MAP = {"Ａ": "A", "Ｂ": "B", "Ｃ": "C", "Ｄ": "D"}
_OPTION_START_RE = re.compile(
    r"(?=(?:进攻|进行|击败|摧毁|采集|完成|建造|训练|研究|使用|派遣|消灭|占领|"
    r"解锁|选择|提高|降低|增加|减少|购买|招募|集结|驻防|治疗|修复)一次)"
)
_PCT_RE = re.compile(r"(\d{1,3})\s*%")
_REWARD_NOISE_RE = re.compile(r"^\d+\s*小时$")
_UNIT_WORD_RE = re.compile(r"^[根个次支条人点级座艘辆]$")
_UI_NOISE_RE = re.compile(
    r"^(?:19点|答题|剩余|分数|正确|已选|终试|悬浮|识题|题库|导入|不导入|"
    r"搜索|侧有|继续|暂停|纠错|保存|取消|"
    r"\d{1,3}\s*%|万国觉醒|Home|Editing|Agent|Terminal|Output|Problems|"
    r"Explorer|Search|Source|Debug|Extensions)$",
    re.IGNORECASE,
)
_DEV_NOISE_RE = re.compile(
    r"(?:\.py\b|\.tsx?\b|\.jsx?\b|\.json\b|\.md\b|\.bat\b|"
    r"restart_overlay|overlay\.py|cursor|vscode|RapidOCR|"
    r"[\\/][\w.-]+\+\d+|^\w+\+\d+$)",
    re.IGNORECASE,
)
# Cursor 聊天 / 助手说明被框进去时的特征（绝不能当题干）
_CHAT_META_RE = re.compile(
    r"(?:RapidOCR|绿底|白字|修好了|查清|识题框|题库未收录|导入题库|不导入|"
    r"自检|建议答案|匹配度|悬浮识题|错色高亮|对勾|并修好)",
    re.IGNORECASE,
)
# Prefer real quiz answers: 14根 / 拜占庭 / England — not IDE chrome words.
_OPTION_BODY_RE = re.compile(
    r"^(?:\d{1,4}[\u4e00-\u9fff]{1,4}|[\u4e00-\u9fff]{2,12}|[A-Z][A-Za-z '\-]{1,20})$"
)


def _is_ui_noise(line: str) -> bool:
    s = (line or "").strip()
    if not s:
        return True
    if _REWARD_NOISE_RE.match(s) or _UI_NOISE_RE.match(s) or _DEV_NOISE_RE.search(s):
        return True
    if _CHAT_META_RE.search(s):
        return True
    if re.fullmatch(r"\d+%\.*", s) or re.fullmatch(r"\d+%", s):
        return True
    # lowercase latin junk from IDE/chat OCR (efly) — real EN answers are Capitalized
    if re.fullmatch(r"[a-z]{2,16}", s):
        return True
    return False


def question_looks_like_quiz(question: str) -> bool:
    """True only for real quiz stems — reject Cursor chat / assistant notes."""
    q = (question or "").strip()
    if not q:
        return False
    if _CHAT_META_RE.search(q) or _DEV_NOISE_RE.search(q):
        return False
    zh = len(re.findall(r"[\u4e00-\u9fff]", q))
    if zh < 8:
        return False
    # Typical Peerless Scholar stems
    if re.search(r"[？?]|[QｑＱ]\s*\d+|下列|哪[个项一]|什么|多少|几[个根支条次]|哪位|哪座", q):
        return True
    # Long Chinese without quiz markers still OK if not meta chatter
    if zh >= 14 and not re.search(r"(?:修好|查清|识别|OCR|点击|按钮|面板)", q):
        return True
    return False


def _looks_like_option_body(text: str) -> bool:
    t = _clean_option_text(text)
    if not t or _is_ui_noise(t):
        return False
    if not _OPTION_BODY_RE.match(t):
        return False
    # Reject plain IDE/menu English unless it looks like a country/name answer.
    if not re.search(r"[\u4e00-\u9fff\d]", t):
        if t.lower() in {"home", "editing", "agent", "terminal", "output", "search"}:
            return False
    return True


def _assign_next_option(options: list[dict], text: str) -> bool:
    text = _clean_option_text(text)
    if not text or not _looks_like_option_body(text):
        return False
    have = {o["key"] for o in options}
    have_text = {str(o.get("text") or "") for o in options}
    if text in have_text:
        return False
    for k in "ABCD":
        if k not in have:
            options.append({"key": k, "text": text})
            return True
    return False


def _clean_question_line(line: str) -> str:
    """Strip Q6 / 6. prefixes only — never eat digits from options like 14根."""
    line = (line or "").strip()
    line = re.sub(r"^[QqＱｑ]\d+[.、:：\s]*", "", line)
    # Require a separator so "14根" stays intact
    line = re.sub(r"^\d{1,2}[.、:：]\s*", "", line)
    return line.strip()


def _clean_option_text(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r"\s*\d{1,3}\s*%.*$", "", text).strip()
    text = re.sub(r"(正确|已选).*$", "", text).strip()
    return text.strip(" .、:：")


def _options_from_lettered(text: str) -> list[dict]:
    options: list[dict] = []
    for m in _OPTION_INLINE_RE.finditer(text or ""):
        letter = _LETTER_MAP.get(m.group(1), m.group(1).upper())
        body = _clean_option_text(m.group(2))
        if body and not _REWARD_NOISE_RE.match(body) and letter not in {o["key"] for o in options}:
            options.append({"key": letter, "text": body})
    return options[:4]


def _options_from_glued(blob: str) -> list[dict]:
    """Split option text glued together after the question mark."""
    blob = (blob or "").strip()
    if not blob:
        return []
    lettered = _options_from_lettered(blob)
    if len(lettered) >= 2:
        return lettered
    parts = [p.strip(" .、:：") for p in _OPTION_START_RE.split(blob) if p and p.strip(" .、:：")]
    # If split failed, keep whole blob as one option only when short
    if len(parts) < 2:
        return [{"key": "A", "text": blob}] if 2 <= len(blob) <= 24 else []
    out: list[dict] = []
    for i, text in enumerate(parts[:4]):
        out.append({"key": "ABCD"[i], "text": _clean_option_text(text)})
    return out


def _repair_numeric_options(options: list[dict], lines: list[str]) -> list[dict]:
    """Reattach orphan digits (14) to unit-only options (根) when OCR splits them."""
    if not options:
        return options
    digits = [ln.strip() for ln in lines if re.fullmatch(r"\d{1,4}", (ln or "").strip())]
    if not digits:
        return options
    need = [
        o
        for o in options
        if o.get("text") and not re.search(r"\d", o["text"]) and _UNIT_WORD_RE.match(o["text"])
    ]
    if not need:
        return options
    # Zip orphan digits onto unit-only slots in ABCD order (works for 1+ orphans)
    repaired: list[dict] = []
    di = 0
    for o in options:
        text = str(o.get("text") or "")
        if di < len(digits) and text and not re.search(r"\d", text) and _UNIT_WORD_RE.match(text):
            repaired.append({"key": o.get("key", ""), "text": f"{digits[di]}{text}"})
            di += 1
        else:
            repaired.append(o)
    return repaired


def _join_question_parts(parts: list[str]) -> str:
    """Join stem fragments and remove OCR overlap/doubled characters (历史上+上的 → 历史上的)."""
    out = ""
    for part in parts:
        p = re.sub(r"\s+", "", (part or "").strip())
        if not p:
            continue
        if not out:
            out = p
            continue
        max_o = 0
        for n in range(1, min(len(out), len(p)) + 1):
            if out.endswith(p[:n]):
                max_o = n
        out += p[max_o:]
    # Collapse accidental doubled CJK (历史上上的 → 历史上的)
    out = re.sub(r"([\u4e00-\u9fff])\1+", r"\1", out)
    return out


def _fill_missing_letter_options(options: list[dict], lines: list[str]) -> list[dict]:
    """Assign leftover answer lines to missing A/B/C/D slots in order."""
    options = [dict(o) for o in (options or []) if o.get("key") and o.get("text")]
    used = {re.sub(r"\s+", "", str(o.get("text") or "")) for o in options}
    orphans: list[str] = []
    for ln in lines:
        # Strip "C冰雹" so we compare body text and never reassign a duplicate of B
        text = _strip_option_letter(ln or "")
        if not text or not _looks_like_option_body(text):
            continue
        key = re.sub(r"\s+", "", text)
        if key in used:
            continue
        orphans.append(text)
        used.add(key)
    have = {str(o.get("key") or "") for o in options}
    missing = [k for k in "ABCD" if k not in have]
    for letter, text in zip(missing, orphans):
        options.append({"key": letter, "text": text})
    order = {k: i for i, k in enumerate("ABCD")}
    options.sort(key=lambda o: order.get(str(o.get("key") or ""), 99))
    return options[:4]


def _best_voted_option(options: list[dict], lines: list[str]) -> str:
    """Pick option text with the highest on-screen percentage (e.g. 98%)."""
    best_vote = -1
    best_text = ""
    for line in lines:
        m = _OPTION_RE.match(line.strip())
        if not m:
            continue
        vote_m = _PCT_RE.search(line)
        if not vote_m:
            continue
        vote = int(vote_m.group(1))
        text = _clean_option_text(m.group(2))
        if vote > best_vote and text and not _REWARD_NOISE_RE.match(text):
            best_vote = vote
            best_text = text
    if best_text:
        # Map onto repaired option wording when possible
        mapped = resolve_option_text(best_text, options)
        return mapped or best_text
    return ""


def extract_quiz_fields(ocr_text: str) -> dict:
    """Parse OCR into a question + ABCD options for optional bank import."""
    lines = [ln.strip() for ln in (ocr_text or "").splitlines() if ln.strip()]
    options: list[dict] = []
    question_parts: list[str] = []
    trailing_parts: list[str] = []
    hinted_answer = ""
    seen_question_end = False
    pending_digit = ""
    pending_letter = ""

    for line in lines:
        hint = _ANSWER_HINT_RE.search(line)
        if hint:
            hinted_answer = _clean_option_text(hint.group(1))
            m = _OPTION_RE.match(hinted_answer)
            if m:
                hinted_answer = _clean_option_text(m.group(2))
            continue
        if re.fullmatch(r"\d+%\.*", line) or re.fullmatch(r"\d+%", line):
            continue
        if _is_ui_noise(line):
            continue
        # Keep bare digits for merging with following unit (14 + 根)
        if re.fullmatch(r"\d{1,4}", line):
            pending_digit = line
            continue
        if re.fullmatch(r"[QqＱｑ]\d+", line):
            continue
        if line in {"A", "B", "C", "D", "Ａ", "Ｂ", "Ｃ", "Ｄ"}:
            pending_letter = _LETTER_MAP.get(line, line.upper())
            continue
        if line in {"Q", "正确", "错误"}:
            continue
        if _REWARD_NOISE_RE.match(line):
            continue

        m = _OPTION_RE.match(line)
        if m:
            letter = _LETTER_MAP.get(m.group(1), m.group(1).upper())
            raw = m.group(2).strip()
            vote_m = _PCT_RE.search(raw)
            text = _clean_option_text(raw)
            if pending_digit and text and not re.search(r"\d", text) and _UNIT_WORD_RE.match(text):
                text = pending_digit + text
                pending_digit = ""
            elif pending_digit and (not text or _REWARD_NOISE_RE.match(text)):
                text = pending_digit
                pending_digit = ""
            have_text = {re.sub(r"\s+", "", str(o.get("text") or "")) for o in options}
            body_key = re.sub(r"\s+", "", text or "")
            # Skip C=冰雹 when B already took 冰雹 (green OCR miss copying neighbor)
            if (
                text
                and not _REWARD_NOISE_RE.match(text)
                and letter not in {o["key"] for o in options}
                and body_key not in have_text
            ):
                options.append({"key": letter, "text": text})
                if vote_m and int(vote_m.group(1)) >= 50:
                    hinted_answer = text
            seen_question_end = True
            pending_letter = ""
            continue

        # Unit-only line after a buffered digit
        if pending_digit and _UNIT_WORD_RE.match(line):
            text = pending_digit + line
            pending_digit = ""
            if pending_letter and pending_letter not in {o["key"] for o in options}:
                options.append({"key": pending_letter, "text": text})
                pending_letter = ""
            else:
                trailing_parts.append(text)
            seen_question_end = True
            continue

        # "A" on its own line, then "14根" on the next
        if pending_letter and (
            re.fullmatch(r"\d{1,4}[\u4e00-\u9fff]{1,4}", line)
            or (_UNIT_WORD_RE.match(line) and not re.search(r"\d", line))
        ):
            text = line
            if pending_digit and not re.search(r"\d", text):
                text = pending_digit + text
                pending_digit = ""
            if pending_letter not in {o["key"] for o in options}:
                options.append({"key": pending_letter, "text": text})
            pending_letter = ""
            seen_question_end = True
            continue

        # Quantity / short answer without letter (15根 / 英格兰 / 拜占庭)
        if seen_question_end and _looks_like_option_body(line):
            if _assign_next_option(options, line):
                pending_digit = ""
                pending_letter = ""
                continue
            trailing_parts.append(line)
            pending_digit = ""
            pending_letter = ""
            continue

        # Inline "A.xx B.yy" on one line
        inline = _options_from_lettered(line)
        if len(inline) >= 2:
            for opt in inline:
                if opt["key"] not in {o["key"] for o in options}:
                    options.append(opt)
            m0 = _OPTION_INLINE_RE.search(line)
            head = _clean_question_line(line[: m0.start()] if m0 else "")
            if head and not seen_question_end:
                question_parts.append(head)
            seen_question_end = True
            pending_digit = ""
            pending_letter = ""
            continue

        # After the stem, keep raw option text (avoid stripping "14根" → "根")
        if seen_question_end:
            if not _is_ui_noise(line):
                trailing_parts.append(line)
            continue

        line = _clean_question_line(line)
        if not line or _is_ui_noise(line):
            continue

        if ("？" in line) or ("?" in line):
            # Split "题干？选项粘连"
            for sep in ("？", "?"):
                if sep in line:
                    left, right = line.split(sep, 1)
                    q = left.strip()
                    if q and not _is_ui_noise(q):
                        question_parts.append(q + "？")
                    right = right.strip()
                    if right and not _is_ui_noise(right):
                        trailing_parts.append(right)
                    break
            seen_question_end = True
            continue

        question_parts.append(line)

    if not options and trailing_parts:
        # Lines after question mark without A/B/C/D prefixes
        clean_trail = [
            _clean_option_text(t)
            for t in trailing_parts
            if t and not _is_ui_noise(t) and not _REWARD_NOISE_RE.match(t.strip())
        ]
        clean_trail = [t for t in clean_trail if t and _looks_like_option_body(t)]
        if len(clean_trail) >= 2:
            options = [{"key": "ABCD"[i], "text": t} for i, t in enumerate(clean_trail[:4])]
        else:
            options = _options_from_glued("".join(clean_trail or trailing_parts))
    elif options and trailing_parts:
        # Letter-prefixed options already found, but some answers lacked letters
        for t in trailing_parts:
            _assign_next_option(options, t)

    options = _repair_numeric_options(options[:4], lines)
    options = _fill_missing_letter_options(options, lines)
    options = sorted(options, key=lambda o: str(o.get("key") or "Z"))
    voted = _best_voted_from_lines(options, lines) or _best_voted_option(options, lines)
    if voted:
        hinted_answer = voted

    question = _join_question_parts(question_parts)
    if _is_ui_noise(question) or _DEV_NOISE_RE.search(question or ""):
        question = ""
    # Cut question at first ？ and drop anything after (options may still be glued)
    for sep in ("？", "?"):
        if sep in question:
            question = question.split(sep, 1)[0].rstrip() + "？"
            break

    if not question:
        raw = re.sub(r"\s+", "", ocr_text or "")
        for sep in ("？", "?"):
            if sep in raw:
                q = raw.split(sep, 1)[0].strip() + "？"
                q = _join_question_parts([q])
                if not _is_ui_noise(q) and not _DEV_NOISE_RE.search(q):
                    question = q
                if not options:
                    options = _options_from_glued(raw.split(sep, 1)[1])
                break

    return {
        "question": question,
        "options": options[:4],
        "hinted_answer": hinted_answer,
    }


def extract_question_candidates(ocr_text: str) -> list[str]:
    parsed = extract_quiz_fields(ocr_text)
    question = (parsed.get("question") or "").strip()
    candidates: list[str] = []
    if question:
        candidates.append(question)
        # Also try without trailing punctuation
        trimmed = question.rstrip("？?。.")
        if trimmed and trimmed != question:
            candidates.append(trimmed)

    # Keep a couple of raw long lines as backup (excluding known options)
    opt_norms = {normalize_text(o["text"]) for o in (parsed.get("options") or []) if o.get("text")}
    lines = [ln.strip() for ln in (ocr_text or "").splitlines() if ln.strip()]
    for line in lines:
        line = _clean_question_line(line)
        if not line or normalize_text(line) in opt_norms:
            continue
        if _OPTION_RE.match(line):
            continue
        if len(normalize_text(line)) >= 8:
            candidates.append(line)

    seen: set[str] = set()
    unique: list[str] = []
    for c in candidates:
        key = normalize_text(c)
        if len(key) < 6 or key in seen:
            continue
        seen.add(key)
        unique.append(c)
    return unique[:4]


def answer_in_options(answer: str, options: list[dict] | None) -> bool:
    """True if answer matches one OCR option, or options are unavailable."""
    if not answer or not options:
        return True
    return resolve_option_text(answer, options) is not None


def resolve_option_text(answer: str, options: list[dict] | None) -> str | None:
    """Map a bank/OCR answer onto the best matching on-screen option text."""
    if not answer:
        return None
    text_answer = str(answer).strip()
    if not options:
        return text_answer or None
    na = normalize_text(text_answer)
    if not na:
        return None
    best: tuple[float, str] | None = None
    for opt in options:
        text = str(opt.get("text") or "").strip()
        if not text:
            continue
        nt = normalize_text(text)
        if not nt:
            continue
        if na == nt:
            score = 100.0
        elif na in nt or nt in na:
            score = 92.0 + min(len(na), len(nt)) / max(len(na), len(nt)) * 6.0
        else:
            score = float(fuzz.ratio(na, nt))
            if score < 86:
                continue
        if best is None or score > best[0]:
            best = (score, text)
    return best[1] if best else None


def filter_results_by_options(results: list[dict], options: list[dict] | None) -> list[dict]:
    if not options:
        return results
    return [r for r in results if answer_in_options(r.get("answer") or "", options)]


def search_bank(query: str, limit: int = 5, *, include_answers: bool = True) -> list[dict]:
    if not query.strip():
        return []

    idx = bank_index()
    bank = idx["bank"]
    nq = normalize_text(query)
    scored_map: dict[int, float] = {}

    # Fast path: normalized substring containment (very common after OCR)
    if len(nq) >= 6:
        for i, nitem in enumerate(idx["q_norm"]):
            if not nitem:
                continue
            if nq in nitem:
                scored_map[i] = 96.0
            elif len(nitem) >= 8 and nitem in nq:
                scored_map[i] = 94.0
        if include_answers:
            for i, nans in enumerate(idx["a_norm"]):
                if nq and nq == nans:
                    scored_map[i] = max(scored_map.get(i, 0.0), 93.0)
                elif nq and len(nq) >= 4 and nq in nans:
                    scored_map[i] = max(scored_map.get(i, 0.0), 88.0)

    # If we already have strong hits, skip expensive fuzzy scan
    strong = [i for i, s in scored_map.items() if s >= 94]
    if len(strong) >= limit:
        ranked = sorted(scored_map.items(), key=lambda x: x[1], reverse=True)
    else:
        for _match, score, i in process.extract(
            query,
            idx["q_map"],
            scorer=fuzz.WRatio,
            limit=max(limit * 3, 12),
            score_cutoff=72,
        ):
            scored_map[i] = max(scored_map.get(i, 0.0), float(score))

        if include_answers:
            for _match, score, i in process.extract(
                query,
                idx["a_map"],
                scorer=fuzz.WRatio,
                limit=max(limit * 2, 8),
                score_cutoff=78,
            ):
                scored_map[i] = max(scored_map.get(i, 0.0), float(score) * 0.92)

        ranked = sorted(scored_map.items(), key=lambda x: x[1], reverse=True)

    results: list[dict] = []
    seen_q: set[str] = set()
    for i, score in ranked:
        item = bank[i]
        q = item["question"]
        if q in seen_q:
            continue
        seen_q.add(q)
        nitem = idx["q_norm"][i]
        nans = idx["a_norm"][i]
        boost = 0
        if nq and nq in nitem:
            boost = 15
        elif nq and nq in nans:
            boost = 12
        elif nitem and nitem in nq:
            boost = 10
        final = min(100.0, float(score) + boost)
        results.append(
            {
                "question": q,
                "answer": item["answer"],
                "score": round(final, 1),
                "source": item.get("source", ""),
            }
        )
        if len(results) >= limit:
            break
    return results


def _prepare_image(image_bytes: bytes, *, region_mode: bool = False) -> Image.Image | None:
    if not image_bytes:
        return None
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    w, h = img.size
    if w < 20 or h < 20:
        return None
    # Region captures are already cropped — keep pixels for small digits (14根).
    if region_mode:
        return img
    # Keep OCR input modest — biggest speed win after disabling cls
    max_w = 900
    if w > max_w:
        nh = max(1, int(h * (max_w / w)))
        img = img.resize((max_w, nh), Image.Resampling.BILINEAR)
    return img


def _enhance_region_for_ocr(img: Image.Image) -> Image.Image:
    """Light contrast boost; avoid upscaling — largest cost driver for RapidOCR."""
    from PIL import ImageEnhance, ImageOps

    w, h = img.size
    # Cap around detector limit; never enlarge much (790→800 was wasting time)
    target = 640
    if w > target:
        scale = target / w
        img = img.resize((target, max(1, int(h * scale))), Image.Resampling.BILINEAR)
    img = ImageOps.autocontrast(img, cutoff=1)
    img = ImageEnhance.Contrast(img).enhance(1.25)
    return img


def _neutralize_green_highlight(img: Image.Image) -> Image.Image:
    """Turn selected green option buttons into brown; keep light text pixels."""
    arr = np.asarray(img.convert("RGB")).copy()
    r = arr[:, :, 0].astype(np.int16)
    g = arr[:, :, 1].astype(np.int16)
    b = arr[:, :, 2].astype(np.int16)
    greenish = (g > r + 25) & (g > b + 15) & (g > 80)
    # Preserve cream/white glyphs on the green button (否则「苹果」会被涂掉)
    lum = (0.3 * r + 0.59 * g + 0.11 * b)
    bright_text = lum > 145
    greenish = greenish & ~bright_text
    arr[greenish] = (90, 70, 45)
    return Image.fromarray(arr)


def _green_button_bbox(img: Image.Image) -> tuple[int, int, int, int] | None:
    """Bounding box of the selected green option button, or None."""
    arr = np.asarray(img.convert("RGB"))
    r = arr[:, :, 0].astype(np.int16)
    g = arr[:, :, 1].astype(np.int16)
    b = arr[:, :, 2].astype(np.int16)
    mask = (g > r + 25) & (g > b + 15) & (g > 80)
    if int(mask.sum()) < 80:
        return None
    ys, xs = np.where(mask)
    pad = 12
    y0 = max(0, int(ys.min()) - pad)
    y1 = min(arr.shape[0], int(ys.max()) + pad)
    x0 = max(0, int(xs.min()) - pad)
    x1 = min(arr.shape[1], int(xs.max()) + pad)
    if y1 - y0 < 12 or x1 - x0 < 20:
        return None
    return x0, y0, x1, y1


def _strip_option_letter(text: str) -> str:
    """Strip leading A/B/C/D from OCR fragments like 'C苹果' / 'C 冰雹'."""
    t = (text or "").strip()
    m = _OPTION_RE.match(t)
    if m:
        return _clean_option_text(m.group(2))
    if t[:1] in "ABCDＡＢＣＤ" and len(t) > 1:
        return _clean_option_text(t[1:])
    return _clean_option_text(t)


def _extract_option_bodies_from_ocr(result) -> list[str]:
    texts: list[str] = []
    for item in result or []:
        if not item or len(item) < 2:
            continue
        t = _strip_option_letter(str(item[1] or ""))
        if t and _looks_like_option_body(t):
            texts.append(t)
    return texts


def _is_near_dup_option(a: str, b: str) -> bool:
    """True when OCR likely copied a neighbor (葡萄牙 vs 猫萄牙)."""
    a = re.sub(r"\s+", "", a or "")
    b = re.sub(r"\s+", "", b or "")
    if not a or not b:
        return False
    if a == b:
        return True
    shared = len(set(a) & set(b))
    if shared < 2:
        return False
    # Almost the same characters (cat-葡萄牙 / 须葡萄牙)
    return shared >= min(len(a), len(b)) - 1


def _ocr_green_option(
    img: Image.Image,
    ocr,
    *,
    avoid: list[str] | None = None,
) -> tuple[str, tuple[float, float] | None]:
    """OCR only the green selected button; return (text, center_xy)."""
    box = _green_button_bbox(img)
    if not box:
        return "", None
    x0, y0, x1, y1 = box
    # Bias toward the label side — checkmark/99% pull the geometric center right,
    # which otherwise maps C (bottom-left) into the D quadrant.
    center = (x0 + (x1 - x0) * 0.28, (y0 + y1) / 2.0)
    # Crop left ~62% only (label); drop 97%/checkmark which confuses OCR
    x_mid = x0 + max(24, int((x1 - x0) * 0.62))
    crop = img.crop((x0, y0, min(x_mid, x1), y1))
    neut = _neutralize_green_highlight(crop)
    w, h = neut.size
    scale = min(2.5, max(1.8, 280 / max(w, 1), 96 / max(h, 1)))
    big = neut.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.Resampling.BILINEAR)
    from PIL import ImageOps, ImageEnhance

    big = ImageOps.autocontrast(big, cutoff=1)
    big = ImageEnhance.Contrast(big).enhance(1.5)
    pad = 16
    canvas = Image.new("RGB", (big.size[0] + pad * 2, big.size[1] + pad * 2), (90, 70, 45))
    canvas.paste(big, (pad, pad))
    gray = ImageOps.grayscale(canvas).convert("RGB")
    gray = ImageEnhance.Contrast(gray).enhance(1.35)
    # One pass only — second color pass nearly doubled green OCR time
    variants = [gray]

    avoid = [re.sub(r"\s+", "", t) for t in (avoid or []) if t]
    picked = ""
    for variant in variants:
        try:
            result, _ = ocr(np.asarray(variant))
        except Exception:
            continue
        texts = [_fix_option_ocr_typo(t) for t in _extract_option_bodies_from_ocr(result)]
        texts = [t for t in texts if t and _looks_like_option_body(t)]
        if not texts:
            continue
        if "苹果" in texts:
            return "苹果", center
        texts.sort(key=lambda s: (len(re.findall(r"[\u4e00-\u9fff]", s)), len(s)), reverse=True)
        for t in texts:
            if any(_is_near_dup_option(t, a) for a in avoid):
                continue
            picked = t
            break
        if picked:
            return picked, center
        if not picked and texts:
            picked = texts[0]
    return picked, center


def _drop_near_dup_options(options: list[dict]) -> list[dict]:
    """Remove later ABCD slots that are OCR copies of an earlier option."""
    kept: list[dict] = []
    for o in options or []:
        t = str(o.get("text") or "")
        if any(_is_near_dup_option(t, str(k.get("text") or "")) for k in kept):
            continue
        kept.append(o)
    return kept


def _fix_option_ocr_typo(text: str) -> str:
    """Map frequent RapidOCR confusions to real quiz answers."""
    t = (text or "").strip()
    if not t:
        return t
    force = {
        "冰盘": "冰雹",
        "冰霉": "冰雹",
        "冰福": "冰雹",
        "冰爸": "冰雹",
        "冰猫": "冰雹",
        "冰霍": "冰雹",
        "华果": "苹果",
        "单果": "苹果",
        "羊果": "苹果",
        "毕果": "苹果",
        "卒果": "苹果",
        "芈果": "苹果",
        "苹巢": "苹果",  # 果→巢
        "苹杲": "苹果",
        "苹果": "苹果",
        "榴连": "榴莲",
        "格连": "榴莲",
        "相莲": "榴莲",
        "银石": "陨石",
        "限石": "陨石",
        "贤石": "陨石",
        "阳石": "陨石",
    }
    if t in force:
        return force[t]
    # Any 苹X misread (苹巢/苹某) — RoK only uses 苹果
    if re.fullmatch(r"苹[\u4e00-\u9fff]", t) and t != "苹果":
        return "苹果"
    return t


def _repair_known_option_ocr(options: list[dict], lines: list[str]) -> list[dict]:
    """Fix frequent RapidOCR confusions when the correct form also appeared."""
    if not options:
        return options
    blob = "\n".join(lines or [])
    fixes = [
        ({"冰盘", "冰霉", "冰福", "冰爸", "冰猫", "冰霍", "冰電"}, "冰雹"),
        ({"华果", "单果", "羊果", "毕果", "卒果", "芈果", "苹巢", "苹杲"}, "苹果"),
        ({"榴连", "格连", "相莲"}, "榴莲"),
        ({"银石", "限石", "贤石", "陨右", "阳石"}, "陨石"),
    ]
    out: list[dict] = []
    for o in options:
        t = _fix_option_ocr_typo(str(o.get("text") or "").strip())
        if t not in {"冰雹", "苹果", "榴莲", "陨石"}:
            for bad, good in fixes:
                if t in bad and good in blob:
                    t = good
                    break
        out.append({**o, "text": t})
    # Dedup after repair (B冰盘+D冰霉 both → 冰雹)
    seen: set[str] = set()
    uniq: list[dict] = []
    for o in out:
        key = re.sub(r"\s+", "", str(o.get("text") or ""))
        if not key or key in seen:
            continue
        seen.add(key)
        uniq.append(o)
    return uniq


def _refine_green_text(green_text: str, lines: list[str], options: list[dict]) -> str:
    """Prefer a cleaner reading from full-frame OCR when green crop is garbled (华果→苹果)."""
    green = _fix_option_ocr_typo(_strip_option_letter(green_text))
    used = {re.sub(r"\s+", "", str(o.get("text") or "")) for o in (options or []) if o.get("text")}
    if green:
        used.discard(re.sub(r"\s+", "", green))

    candidates: list[str] = []
    if green and _looks_like_option_body(green):
        candidates.append(green)
    for ln in lines or []:
        t = _strip_option_letter(ln)
        if not t or not _looks_like_option_body(t):
            continue
        key = re.sub(r"\s+", "", t)
        if key in used:
            continue
        candidates.append(t)

    if not candidates:
        return green

    # If several *果 variants appear, prefer the most frequent / longest CJK form
    if green and "果" in green:
        fruit = [c for c in candidates if "果" in c]
        if fruit:
            counts: dict[str, int] = {}
            for c in fruit:
                counts[c] = counts.get(c, 0) + 1
            fruit_ranked = sorted(
                counts.keys(),
                key=lambda s: (counts[s], len(re.findall(r"[\u4e00-\u9fff]", s)), len(s)),
                reverse=True,
            )
            # Prefer 苹果 when it showed up at least once among *果 readings
            if "苹果" in counts:
                return "苹果"
            return fruit_ranked[0]

    if green:
        return green
    # No green reading: take longest unused option-looking line
    candidates.sort(key=lambda s: (len(re.findall(r"[\u4e00-\u9fff]", s)), len(s)), reverse=True)
    return candidates[0]


def _ocr_box_items(result) -> list[dict]:
    """Normalize RapidOCR boxes to {text,cx,cy,x0,y0,x1,y1}."""
    items: list[dict] = []
    for item in result or []:
        if not item or len(item) < 2:
            continue
        text = str(item[1] or "").strip()
        if not text or _is_ui_noise(text):
            continue
        box = item[0]
        try:
            xs = [float(p[0]) for p in box]
            ys = [float(p[1]) for p in box]
            x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
        except Exception:
            continue
        items.append(
            {
                "text": text,
                "cx": (x0 + x1) / 2.0,
                "cy": (y0 + y1) / 2.0,
                "x0": x0,
                "y0": y0,
                "x1": x1,
                "y1": y1,
            }
        )
    items.sort(key=lambda it: (it["y0"], it["x0"]))
    return items


def _ocr_result_lines(result) -> list[str]:
    return [it["text"] for it in _ocr_box_items(result)]


def _merge_ocr_line_passes(passes: list[list[str]]) -> str:
    """Merge multi-pass OCR lines; keep longer variants of similar text."""
    merged: list[str] = []
    for lines in passes:
        for line in lines:
            s = line.strip()
            if not s or _is_ui_noise(s):
                continue
            replaced = False
            for i, old in enumerate(merged):
                if s == old:
                    replaced = True
                    break
                # Prefer longer completion of the same stem (法兰 → 法兰西, 拜占 → 拜占庭)
                if s.startswith(old) and len(s) > len(old):
                    merged[i] = s
                    replaced = True
                    break
                if old.startswith(s):
                    replaced = True
                    break
            if not replaced:
                merged.append(s)
    return "\n".join(merged)


def _options_from_spatial_boxes(
    items: list[dict],
    *,
    frame_w: int,
    frame_h: int,
    stem_ratio: float = 0.30,
) -> tuple[list[dict], str]:
    """Map option texts into ABCD using 2x2 layout; hint from highest nearby %.

    stem_ratio: fraction of frame height treated as question stem (ignored for
    options). Use 0 when ``items`` already come from an options-only crop.
    """
    if not items or frame_w < 8 or frame_h < 8:
        return [], ""

    opt_top = frame_h * max(0.0, min(0.6, float(stem_ratio)))
    mid_x = frame_w * 0.50
    mid_y = (opt_top + frame_h) * 0.50

    letters: dict[str, dict] = {}
    bodies: list[dict] = []
    votes: list[tuple[float, float, int]] = []  # cx, cy, pct

    for it in items:
        text = it["text"]
        if text in {"A", "B", "C", "D", "Ａ", "Ｂ", "Ｃ", "Ｄ"}:
            letters[_LETTER_MAP.get(text, text.upper())] = it
            continue
        m = _OPTION_RE.match(text)
        if m:
            key = _LETTER_MAP.get(m.group(1), m.group(1).upper())
            body = _clean_option_text(m.group(2))
            vote_m = _PCT_RE.search(text)
            if body and _looks_like_option_body(body):
                bodies.append({**it, "text": body, "forced_key": key})
            if vote_m:
                votes.append((it["cx"], it["cy"], int(vote_m.group(1))))
            continue
        if re.fullmatch(r"\d{1,3}\s*%", text):
            votes.append((it["cx"], it["cy"], int(re.search(r"\d+", text).group(0))))
            continue
        if it["cy"] < opt_top:
            continue
        if _looks_like_option_body(text):
            bodies.append({**it, "text": _clean_option_text(text), "forced_key": ""})

    # Deduplicate bodies: keep longer text when centers are close
    bodies.sort(key=lambda b: (b["cy"], b["cx"]))
    deduped: list[dict] = []
    for b in bodies:
        merged = False
        for i, old in enumerate(deduped):
            if abs(old["cx"] - b["cx"]) < frame_w * 0.18 and abs(old["cy"] - b["cy"]) < frame_h * 0.12:
                if len(b["text"]) > len(old["text"]):
                    deduped[i] = b
                elif b.get("forced_key") and not old.get("forced_key"):
                    deduped[i] = {**old, "forced_key": b["forced_key"]}
                merged = True
                break
        if not merged:
            deduped.append(b)
    bodies = deduped[:6]

    def quadrant(b: dict) -> str:
        left = b["cx"] < mid_x
        top = b["cy"] < mid_y
        if top and left:
            return "A"
        if top and not left:
            return "B"
        if not top and left:
            return "C"
        return "D"

    slot: dict[str, dict] = {}
    for b in bodies:
        key = b.get("forced_key") or ""
        if not key:
            # nearest letter badge wins over pure geometry
            best_letter = ""
            best_dist = 1e18
            for lk, lit in letters.items():
                dist = (lit["cx"] - b["cx"]) ** 2 + (lit["cy"] - b["cy"]) ** 2
                if dist < best_dist and dist < (frame_w * 0.35) ** 2:
                    best_dist = dist
                    best_letter = lk
            key = best_letter or quadrant(b)
        prev = slot.get(key)
        if not prev or len(b["text"]) > len(prev["text"]):
            slot[key] = b

    # Drop duplicate texts (C 冰雹 copying B) — keep first by ABCD order
    seen_text: set[str] = set()
    cleaned: dict[str, dict] = {}
    for k in "ABCD":
        if k not in slot:
            continue
        t = re.sub(r"\s+", "", str(slot[k]["text"]))
        if t in seen_text:
            continue
        seen_text.add(t)
        cleaned[k] = slot[k]
    slot = cleaned

    options = [{"key": k, "text": slot[k]["text"]} for k in "ABCD" if k in slot]

    hinted = ""
    best_vote = -1
    for b in slot.values():
        local_best = -1
        for vx, vy, pct in votes:
            if abs(vx - b["cx"]) < frame_w * 0.28 and abs(vy - b["cy"]) < frame_h * 0.20:
                local_best = max(local_best, pct)
        if local_best > best_vote:
            best_vote = local_best
            hinted = b["text"]
    return options, hinted


def _inject_green_option(
    options: list[dict],
    green_text: str,
    green_center: tuple[float, float] | None,
    *,
    frame_w: int,
    frame_h: int,
    stem_ratio: float = 0.30,
) -> list[dict]:
    """Force green-button OCR text into the matching ABCD slot (fixes 苹果→冰雹)."""
    if not green_text or not _looks_like_option_body(green_text):
        return options
    options = [dict(o) for o in options]
    # Decide target letter from green button position (2x2)
    key = "C"
    if green_center and frame_w > 0 and frame_h > 0:
        cx, cy = green_center
        opt_top = frame_h * max(0.0, min(0.6, float(stem_ratio)))
        mid_x = frame_w * 0.50
        mid_y = (opt_top + frame_h) * 0.50
        left = cx < mid_x
        top = cy < mid_y
        if top and left:
            key = "A"
        elif top and not left:
            key = "B"
        elif not top and left:
            key = "C"
        else:
            key = "D"
    # Refuse copying a neighbor (C←猫萄牙 from B葡萄牙)
    for o in options:
        if str(o.get("key")) == key:
            continue
        other = str(o.get("text") or "")
        if _is_near_dup_option(green_text, other):
            # Green OCR also failed — drop the garbled green-slot text
            return [x for x in options if str(x.get("key")) != key]
    # Remove duplicates of this text from other slots
    options = [
        o
        for o in options
        if str(o.get("key")) == key or not _is_near_dup_option(str(o.get("text") or ""), green_text)
    ]
    by_key = {str(o.get("key") or ""): o for o in options}
    by_key[key] = {"key": key, "text": green_text}
    order = {k: i for i, k in enumerate("ABCD")}
    out = list(by_key.values())
    out.sort(key=lambda o: order.get(str(o.get("key") or ""), 99))
    return out[:4]


def _best_voted_from_lines(options: list[dict], lines: list[str]) -> str:
    """Pick option with highest nearby percentage across loose OCR lines."""
    best_vote = -1
    best_text = ""
    prev_text = ""
    for raw in lines:
        line = (raw or "").strip()
        if not line:
            continue
        vote_m = _PCT_RE.search(line)
        m = _OPTION_RE.match(line)
        if m:
            text = _clean_option_text(m.group(2))
            if text and _looks_like_option_body(text):
                prev_text = text
                if vote_m:
                    vote = int(vote_m.group(1))
                    if vote > best_vote:
                        best_vote = vote
                        best_text = text
            continue
        if re.fullmatch(r"\d{1,3}\s*%\.?", line) and prev_text:
            vote = int(re.search(r"\d+", line).group(0))
            if vote > best_vote:
                best_vote = vote
                best_text = prev_text
            continue
        if _looks_like_option_body(line):
            prev_text = _clean_option_text(line)
            if vote_m:
                vote = int(vote_m.group(1))
                if vote > best_vote:
                    best_vote = vote
                    best_text = prev_text
    if best_text:
        mapped = resolve_option_text(best_text, options)
        return mapped or best_text
    return ""


def options_look_reliable(options: list[dict] | None, question: str = "") -> bool:
    """False when OCR options are too garbled to trust for filtering/import."""
    if not options or len(options) < 2:
        return False
    texts = [str(o.get("text") or "").strip() for o in options]
    texts = [t for t in texts if t]
    if len(texts) < 2:
        return False
    if any(_is_ui_noise(t) or _CHAT_META_RE.search(t) for t in texts):
        return False
    # Single-character options are usually OCR debris (use raw text, not normalize)
    if sum(1 for t in texts if len(re.sub(r"\s+", "", t)) <= 1) >= 2:
        return False
    # Chat scraps often lack shared quiz shape (mix of EN junk + random CJK)
    en_n = sum(1 for t in texts if re.fullmatch(r"[A-Za-z][A-Za-z '\-]{1,20}", t))
    if en_n >= 2 and not re.search(r"[A-Za-z]{3,}", question or ""):
        return False
    # Quantity questions should have digits in options
    if re.search(r"多少|几[个根支条次]|数量|上限|下限", question or ""):
        if not any(re.search(r"\d", t) for t in texts):
            return False
    return True


def _hint_from_green_selection(img: Image.Image, options: list[dict], items: list[dict]) -> str:
    """If a green selected button is visible, hint that option text."""
    if not options or not items:
        return ""
    arr = np.asarray(img.convert("RGB"))
    r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
    mask = (g.astype(np.int16) > r.astype(np.int16) + 25) & (
        g.astype(np.int16) > b.astype(np.int16) + 15
    ) & (g > 90)
    if int(mask.sum()) < 80:
        return ""
    ys, xs = np.where(mask)
    cy = float(ys.mean())
    cx = float(xs.mean())
    best = ""
    best_dist = 1e18
    for it in items:
        if not _looks_like_option_body(it["text"]):
            continue
        dist = (it["cx"] - cx) ** 2 + (it["cy"] - cy) ** 2
        if dist < best_dist:
            best_dist = dist
            best = _clean_option_text(it["text"])
    if not best:
        return ""
    return resolve_option_text(best, options) or best


def ocr_region_quiz(image_bytes: bytes) -> tuple[str, list[dict], str]:
    """Region OCR — one full pass by default; green crop only when needed."""
    img = _prepare_image(image_bytes, region_mode=True)
    if img is None:
        return "", [], ""

    neut = _enhance_region_for_ocr(_neutralize_green_highlight(img))
    bw, bh = neut.size
    top = int(bh * 0.28)

    passes: list[list[str]] = []
    best_spatial: list[dict] = []
    best_hint = ""
    all_items: list[dict] = []
    green_text = ""
    green_center = None
    need_green = False

    def _ingest(result, ox: int, oy: int) -> None:
        nonlocal best_spatial, best_hint
        items = _ocr_box_items(result)
        if ox or oy:
            for it in items:
                it["cx"] += ox
                it["cy"] += oy
                it["x0"] += ox
                it["x1"] += ox
                it["y0"] += oy
                it["y1"] += oy
        passes.append([it["text"] for it in items])
        all_items.extend(items)
        opts, hint = _options_from_spatial_boxes(items, frame_w=bw, frame_h=bh)
        if len(opts) > len(best_spatial):
            best_spatial = opts
            best_hint = hint or best_hint
        elif len(opts) == len(best_spatial) and hint and not best_hint:
            best_hint = hint

    with _OCR_LOCK:
        ocr = get_ocr()
        # Pass 1: full frame only (≈1s). Extra passes were the 3–6s slowdown.
        try:
            result, _ = ocr(np.asarray(neut))
            _ingest(result, 0, 0)
        except Exception:
            pass
        # Pass 2 (rare): options band only when ABCD badly incomplete
        if len(best_spatial) < 2 and top > 8 and bh - top > 40:
            try:
                result, _ = ocr(np.asarray(neut.crop((0, top, bw, bh))))
                _ingest(result, 0, top)
            except Exception:
                pass

        best_spatial = _drop_near_dup_options(best_spatial)
        texts = [str(o.get("text") or "") for o in best_spatial]
        has_near_dup = any(
            _is_near_dup_option(texts[i], texts[j])
            for i in range(len(texts))
            for j in range(i + 1, len(texts))
        )
        # Green OCR only when ABCD incomplete or a neighbor-copy is suspected.
        # Vote hint alone is NOT worth an extra ~1–2s OCR call.
        box = _green_button_bbox(img)
        need_green = bool(box) and (len(best_spatial) < 4 or has_near_dup)
        if need_green and box:
            green_text, green_center = _ocr_green_option(img, ocr, avoid=texts)
            if green_center and img.size[0] > 0 and img.size[1] > 0:
                green_center = (
                    green_center[0] * (bw / img.size[0]),
                    green_center[1] * (bh / img.size[1]),
                )
        elif box and img.size[0] > 0:
            x0, y0, x1, y1 = box
            green_center = (
                (x0 + (x1 - x0) * 0.28) * (bw / img.size[0]),
                ((y0 + y1) / 2.0) * (bh / img.size[1]),
            )
            need_green = False

    text = _merge_ocr_line_passes(passes)
    lines = text.splitlines()
    if green_text or (need_green and _green_button_bbox(img)):
        green_text = _refine_green_text(green_text, lines, best_spatial)
    if green_text:
        best_spatial = _inject_green_option(
            best_spatial, green_text, green_center, frame_w=bw, frame_h=bh
        )
        best_hint = green_text
    # Merge spatial + line orphans so missing C/D still get filled
    best_spatial = _fill_missing_letter_options(best_spatial, lines)
    # Drop accidental duplicates again after fill
    seen: set[str] = set()
    uniq: list[dict] = []
    for o in best_spatial:
        t = re.sub(r"\s+", "", str(o.get("text") or ""))
        if not t or t in seen:
            continue
        seen.add(t)
        uniq.append(o)
    best_spatial = uniq
    best_spatial = _drop_near_dup_options(best_spatial)
    if green_text:
        green_text = _refine_green_text(green_text, lines, best_spatial)
        # Discard only when green reading is a copy of a *different* option
        if any(
            t != green_text and _is_near_dup_option(green_text, t)
            for t in (str(o.get("text") or "") for o in best_spatial)
        ):
            green_text = ""
        if green_text:
            best_spatial = _inject_green_option(
                best_spatial, green_text, green_center, frame_w=bw, frame_h=bh
            )
            best_hint = green_text
    best_spatial = _repair_known_option_ocr(best_spatial, lines)
    # Re-fill slots emptied by dedup after repair
    best_spatial = _fill_missing_letter_options(best_spatial, lines)
    best_spatial = _drop_near_dup_options(best_spatial)
    best_spatial = _repair_known_option_ocr(best_spatial, lines)
    if green_text:
        # Keep green slot authoritative after repairs
        best_spatial = _inject_green_option(
            best_spatial, green_text, green_center, frame_w=bw, frame_h=bh
        )
        best_hint = green_text
    if not best_hint:
        best_hint = _best_voted_from_lines(best_spatial, lines)
    if not best_hint:
        best_hint = _hint_from_green_selection(img, best_spatial, all_items)
    if best_hint:
        best_hint = _fix_option_ocr_typo(best_hint)
        if any(
            t != best_hint and _is_near_dup_option(best_hint, t)
            for t in (str(o.get("text") or "") for o in best_spatial)
        ):
            best_hint = green_text or ""
    return text, best_spatial, best_hint


def ocr_image(image_bytes: bytes, *, region_mode: bool = False) -> str:
    if region_mode:
        text, _, _ = ocr_region_quiz(image_bytes)
        return text

    img = _prepare_image(image_bytes, region_mode=False)
    if img is None:
        return ""

    w, h = img.size
    if h / max(w, 1) < 0.55:
        # Already-cropped short screenshots: OCR full frame
        crop = img
    else:
        crop = img.crop((int(w * 0.18), int(h * 0.08), int(w * 0.82), int(h * 0.45)))
        cw, ch = crop.size
        if cw < 80 or ch < 40:
            crop = img

    with _OCR_LOCK:
        ocr = get_ocr()
        result, _ = ocr(np.asarray(crop))
        text_lines = _ocr_result_lines(result)
        text = "\n".join(text_lines)
        zh_chars = len(re.findall(r"[\u4e00-\u9fff]", text))
        if zh_chars < 6 and crop is not img:
            result2, _ = ocr(np.asarray(img))
            text_lines2 = _ocr_result_lines(result2)
            if len("".join(text_lines2)) > len("".join(text_lines)):
                text = "\n".join(text_lines2)
    return text


def _ocr_question_stem_fast(image_bytes: bytes) -> str:
    """OCR only the top question band — much cheaper than a full-frame pass."""
    img = _prepare_image(image_bytes, region_mode=True)
    if img is None:
        return ""
    w, h = img.size
    band = img.crop((0, 0, w, max(48, int(h * 0.40))))
    # Question text sits on cream panel — skip green neutralize
    from PIL import ImageEnhance, ImageOps

    if band.size[0] > 480:
        nh = max(1, int(band.size[1] * (480 / band.size[0])))
        band = band.resize((480, nh), Image.Resampling.BILINEAR)
    band = ImageOps.autocontrast(band, cutoff=1)
    band = ImageEnhance.Contrast(band).enhance(1.2)
    with _OCR_LOCK:
        ocr = get_ocr()
        try:
            result, _ = ocr(np.asarray(band))
        except Exception:
            return ""
        return "\n".join(_ocr_result_lines(result))


def _ocr_options_band(image_bytes: bytes) -> tuple[str, list[dict], str]:
    """OCR the lower options area only (used after a stem-only pass)."""
    img = _prepare_image(image_bytes, region_mode=True)
    if img is None:
        return "", [], ""
    w, h = img.size
    top = int(h * 0.28)
    band = img.crop((0, top, w, h))
    orig_bw, orig_bh = band.size
    neut = _enhance_region_for_ocr(_neutralize_green_highlight(band))
    bw, bh = neut.size
    sx = bw / max(orig_bw, 1)
    sy = bh / max(orig_bh, 1)
    passes: list[list[str]] = []
    best_spatial: list[dict] = []
    best_hint = ""
    with _OCR_LOCK:
        ocr = get_ocr()
        try:
            result, _ = ocr(np.asarray(neut))
        except Exception:
            return "", [], ""
        items = _ocr_box_items(result)
        passes.append([it["text"] for it in items])
        # Band crop is already options-only — do not apply full-frame stem_ratio
        # (that used to drop A/B sitting in the top 30% of the band).
        opts, hint = _options_from_spatial_boxes(
            items, frame_w=bw, frame_h=bh, stem_ratio=0.0
        )
        best_spatial = opts
        best_hint = hint or ""
        texts = [str(o.get("text") or "") for o in best_spatial]
        has_near_dup = any(
            _is_near_dup_option(texts[i], texts[j])
            for i in range(len(texts))
            for j in range(i + 1, len(texts))
        )
        box = _green_button_bbox(img)
        if box and (len(best_spatial) < 4 or has_near_dup):
            green_text, green_center = _ocr_green_option(img, ocr, avoid=texts)
            if green_text:
                if green_center and orig_bw > 0 and orig_bh > 0:
                    # Map full-image green center into enhanced band-local coords
                    green_center = (
                        green_center[0] * sx,
                        (green_center[1] - top) * sy,
                    )
                best_spatial = _inject_green_option(
                    best_spatial, green_text, green_center, frame_w=bw, frame_h=bh, stem_ratio=0.0
                )
                best_hint = green_text
    text = _merge_ocr_line_passes(passes)
    best_spatial = _repair_known_option_ocr(best_spatial, text.splitlines())
    best_spatial = _drop_near_dup_options(best_spatial)
    if not best_hint:
        best_hint = _best_voted_from_lines(best_spatial, text.splitlines())
    if best_hint:
        best_hint = _fix_option_ocr_typo(best_hint)
    return text, best_spatial, best_hint


def solve_image_bytes(image_bytes: bytes, *, region_mode: bool = False) -> dict:
    t0 = time.perf_counter()
    spatial_opts: list[dict] = []
    spatial_hint = ""
    ocr_text = ""

    if region_mode:
        # 1) Cheap stem OCR → bank hit → done (typical path ~1s)
        stem_text = _ocr_question_stem_fast(image_bytes)
        stem_parsed = extract_quiz_fields(stem_text)
        stem_q = str(stem_parsed.get("question") or "").strip()
        if not stem_q:
            lines = [ln.strip() for ln in stem_text.splitlines() if ln.strip()]
            lines.sort(key=lambda s: len(re.findall(r"[\u4e00-\u9fff]", s)), reverse=True)
            stem_q = lines[0] if lines else ""
        if stem_q and question_looks_like_quiz(stem_q):
            hits = search_bank(stem_q, limit=3, include_answers=False)
            if hits and hits[0]["score"] >= 90:
                t_ocr = time.perf_counter()
                return {
                    "ocr_text": stem_text,
                    "matched_query": stem_q,
                    "answer": hits[0]["answer"],
                    "confidence": hits[0]["score"],
                    "matched_question": hits[0]["question"],
                    "candidates": hits,
                    "parsed": {"question": stem_q, "options": [], "hinted_answer": ""},
                    "needs_import": False,
                    "options_reliable": False,
                    "timing_ms": {
                        "ocr": round((t_ocr - t0) * 1000),
                        "match": round((time.perf_counter() - t_ocr) * 1000),
                        "total": round((time.perf_counter() - t0) * 1000),
                    },
                }
        # 2) Bank miss: OCR options band only (avoid a second full-frame pass)
        opt_text, spatial_opts, spatial_hint = _ocr_options_band(image_bytes)
        ocr_text = (stem_text + "\n" + opt_text).strip()
    else:
        ocr_text = ocr_image(image_bytes, region_mode=False)
    t_ocr = time.perf_counter()

    parsed = extract_quiz_fields(ocr_text)
    raw_options = parsed.get("options") or []
    # Prefer spatially ordered options (fixes C/D swap from line order)
    if len(spatial_opts) >= 3:
        raw_options = spatial_opts
    elif spatial_opts and len(spatial_opts) > len(raw_options):
        raw_options = spatial_opts
    if spatial_hint:
        parsed = {**parsed, "hinted_answer": spatial_hint}
    elif not parsed.get("hinted_answer"):
        voted = _best_voted_from_lines(raw_options, (ocr_text or "").splitlines())
        if voted:
            parsed = {**parsed, "hinted_answer": voted}
    parsed = {**parsed, "options": raw_options}
    question = str(parsed.get("question") or "")
    # Drop garbled options so they don't kill a good bank match
    if options_look_reliable(raw_options, question):
        options = raw_options
    else:
        options = []
        parsed = {**parsed, "options": raw_options}  # keep raw for manual pick UI
    # Cursor/chat OCR must not become an importable "question"
    if question and not question_looks_like_quiz(question):
        parsed = {**parsed, "question": "", "options": [], "hinted_answer": ""}
        question = ""
        options = []
        raw_options = []
    candidates = extract_question_candidates(ocr_text)
    # Prefer the structured question stem when available
    if parsed.get("question"):
        stem = parsed["question"]
        candidates = [stem] + [c for c in candidates if normalize_text(c) != normalize_text(stem)]

    best_results: list[dict] = []
    used_query = ""
    for cand in candidates:
        results = search_bank(cand, limit=8, include_answers=False)
        results = filter_results_by_options(results, options)
        if results and (not best_results or results[0]["score"] > best_results[0]["score"]):
            best_results = results
            used_query = cand
        # Only early-stop when confident AND compatible with on-screen options
        if best_results and best_results[0]["score"] >= 92:
            if not options or answer_in_options(best_results[0]["answer"], options):
                break
    t_end = time.perf_counter()

    answer = best_results[0] if best_results else None
    conf = answer["score"] if answer else 0
    # Reject leftover incompatible answers (safety net)
    if answer and options and not answer_in_options(answer["answer"], options):
        answer = None
        conf = 0
        best_results = []
    # Reject weak fuzzy hits (「千年虫是什么」→「光年是什么计量单位」 at ~73)
    if answer and conf < 82:
        answer = None
        conf = 0
        best_results = []
    # Prefer on-screen option wording (e.g. 拜占庭 → 拜占庭式)
    display_answer = None
    if answer:
        display_answer = resolve_option_text(answer["answer"], options) or answer["answer"]
    # If bank miss but screen vote is clear (e.g. 97%), prefer that for import seed
    if not display_answer and parsed.get("hinted_answer"):
        display_answer = str(parsed.get("hinted_answer") or "")
    # Weak / missing match → offer import only for real quiz stems
    needs_import = conf < 82 and question_looks_like_quiz(str(parsed.get("question") or ""))
    # For import UI, prefer reliable options; otherwise still show raw for hand-fix
    ui_options = options if options else raw_options
    if ui_options and not options_look_reliable(ui_options, str(parsed.get("question") or "")):
        ui_options = []
    parsed_out = {**parsed, "options": ui_options}
    return {
        "ocr_text": ocr_text,
        "matched_query": used_query,
        "answer": display_answer,
        "confidence": conf,
        "matched_question": answer["question"] if answer else None,
        "candidates": best_results,
        "parsed": parsed_out,
        "needs_import": needs_import,
        "options_reliable": bool(options),
        "timing_ms": {
            "ocr": round((t_ocr - t0) * 1000),
            "match": round((t_end - t_ocr) * 1000),
            "total": round((t_end - t0) * 1000),
        },
    }


@app.get("/")
def index():
    return send_from_directory(WEB, "index.html")


@app.get("/api/health")
def health():
    try:
        count = len(load_bank())
        return jsonify({"ok": True, "app": APP_ID, "total": count, "ocr_ready": _OCR_READY})
    except Exception as exc:
        return jsonify({"ok": False, "app": APP_ID, "error": str(exc)}), 500


@app.get("/api/warmup")
@app.post("/api/warmup")
def api_warmup():
    try:
        return jsonify(warm_up())
    except Exception as exc:
        log.exception("warmup failed")
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.get("/api/stats")
def stats():
    try:
        bank = load_bank()
        users = bank_io.load_user_questions()
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    zh = sum(1 for q in bank if re.search(r"[\u4e00-\u9fff]", q["question"]))
    return jsonify(
        {
            "total": len(bank),
            "zh": zh,
            "user_added": len(users),
            "app": APP_ID,
            "ocr_ready": _OCR_READY,
            "user_data_dir": str(bank_io.user_data_dir()),
        }
    )


@app.get("/api/search")
def api_search():
    q = request.args.get("q", "")
    try:
        limit = max(1, min(int(request.args.get("limit", 5)), 20))
    except ValueError:
        limit = 5
    try:
        return jsonify({"query": q, "results": search_bank(q, limit=limit, include_answers=True)})
    except Exception as exc:
        return jsonify({"error": str(exc), "results": []}), 500


@app.post("/api/questions/add")
def api_add_question():
    data = request.get_json(silent=True) or {}
    question = str(data.get("question", "")).strip()
    answer = str(data.get("answer", "")).strip()
    try:
        result = bank_io.add_user_question(question, answer, source="manual")
        bank = reload_bank()
        return jsonify({**result, "total": len(bank), "user_added": len(bank_io.load_user_questions())})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        log.exception("add question failed")
        return jsonify({"error": str(exc)}), 500


@app.post("/api/questions/import-online")
def api_import_online():
    try:
        report = bank_io.import_from_online()
        bank = reload_bank()
        report["total"] = len(bank)
        report["user_added"] = len(bank_io.load_user_questions())
        return jsonify(report)
    except Exception as exc:
        log.exception("online import failed")
        return jsonify({"error": str(exc)}), 500


@app.get("/api/questions/user")
def api_user_questions():
    items = bank_io.load_user_questions()
    items = list(reversed(items[-50:]))
    return jsonify({"count": len(bank_io.load_user_questions()), "recent": items})


@app.get("/api/questions/user/export")
def api_user_export():
    """Download user-added questions only (never the builtin bank)."""
    try:
        payload = bank_io.export_user_bank()
    except Exception as exc:
        log.exception("export user bank failed")
        return jsonify({"error": str(exc)}), 500
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    filename = f"rok-user-bank-{time.strftime('%Y%m%d')}.json"
    return Response(
        body,
        mimetype="application/json; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@app.post("/api/questions/user/import")
def api_user_import():
    """Upload a user bank JSON; merges into local user store only."""
    try:
        raw: bytes | None = None
        if "file" in request.files:
            raw = request.files["file"].read()
        elif "bank" in request.files:
            raw = request.files["bank"].read()
        elif request.is_json:
            data = request.get_json(silent=True)
            items = bank_io.parse_user_bank_payload(data)
            report = bank_io.import_user_bank(items, source="upload")
            bank = reload_bank()
            return jsonify(
                {
                    **report,
                    "total": len(bank),
                    "user_added": len(bank_io.load_user_questions()),
                }
            )
        else:
            raw = request.get_data(cache=False)

        if not raw:
            return jsonify({"error": "请选择要上传的自建题库 JSON 文件"}), 400
        if len(raw) > 8 * 1024 * 1024:
            return jsonify({"error": "文件过大（上限 8MB）"}), 400

        text = None
        for enc in ("utf-8-sig", "utf-8", "gb18030"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            return jsonify({"error": "无法解码文件，请使用 UTF-8 JSON"}), 400

        data = json.loads(text)
        items = bank_io.parse_user_bank_payload(data)
        if not items:
            return jsonify({"error": "文件里没有有效的题目（需要 question + answer）"}), 400

        report = bank_io.import_user_bank(items, source="upload")
        bank = reload_bank()
        return jsonify(
            {
                **report,
                "total": len(bank),
                "user_added": len(bank_io.load_user_questions()),
            }
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except json.JSONDecodeError:
        return jsonify({"error": "JSON 解析失败，请检查文件格式"}), 400
    except Exception as exc:
        log.exception("import user bank failed")
        return jsonify({"error": str(exc)}), 500


@app.post("/api/solve")
def api_solve():
    if "image" not in request.files and "file" not in request.files:
        return jsonify({"error": "请上传截图 image"}), 400

    file = request.files.get("image") or request.files.get("file")
    image_bytes = file.read()
    if not image_bytes:
        return jsonify({"error": "图片为空"}), 400

    region_mode = request.args.get("region") == "1" or request.form.get("region") == "1"
    try:
        return jsonify(solve_image_bytes(image_bytes, region_mode=region_mode))
    except Exception as exc:
        log.exception("OCR failed")
        return jsonify(
            {
                "error": f"识别失败: {exc}",
                "ocr_text": "",
                "answer": None,
                "confidence": 0,
                "candidates": [],
            }
        ), 500


def launch_overlay_process() -> dict:
    """Start overlay, or focus the existing one. Returns status payload."""
    from overlay import focus_existing_overlay

    if focus_existing_overlay():
        return {"ok": True, "already": True, "message": "悬浮识题框已在运行，已切换到前台"}

    import subprocess

    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "--overlay"]
    else:
        cmd = [sys.executable, str(ROOT / "overlay.py")]
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        creationflags=flags,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return {"ok": True, "already": False, "message": "悬浮识题框已启动"}


@app.post("/api/overlay/start")
def api_overlay_start():
    try:
        return jsonify(launch_overlay_process())
    except Exception as exc:
        log.exception("overlay start failed")
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.errorhandler(Exception)
def handle_unexpected(exc):
    log.exception("Unhandled error")
    return jsonify({"error": str(exc), "detail": traceback.format_exc()[-800:]}), 500


def open_browser_later(url: str, delay: float = 1.0) -> None:
    def _open() -> None:
        time.sleep(delay)
        try:
            webbrowser.open(url)
        except Exception:
            pass

    threading.Thread(target=_open, daemon=True).start()


def main(*, open_browser: bool = False) -> None:
    warm_up()
    count = len(load_bank())
    url = f"http://{HOST}:{PORT}"
    print("=" * 48)
    print("  万国觉醒 · 国士无双题库")
    print(f"  已加载题库: {count} 题")
    print(f"  服务地址: {url}")
    print("  关闭窗口即可退出程序")
    print("=" * 48)
    if open_browser:
        open_browser_later(url)
    app.run(host=HOST, port=PORT, debug=False, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main(open_browser=("--browser" in sys.argv))
