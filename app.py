# -*- coding: utf-8 -*-
"""Rise of Kingdoms quiz helper: screenshot OCR + fuzzy match."""
from __future__ import annotations

import io
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
from flask import Flask, jsonify, request, send_from_directory
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
    text = re.sub(r"^[QqＱｑ]?\d+[.、:：]?", "", text)
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
        for shape in ((100, 560, 3), (160, 640, 3), (220, 720, 3)):
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


def extract_question_candidates(ocr_text: str) -> list[str]:
    lines = [ln.strip() for ln in ocr_text.splitlines() if ln.strip()]
    cleaned: list[str] = []
    for line in lines:
        if re.match(r"^[ABCDＡＢＣＤ]\s*", line):
            continue
        if re.fullmatch(r"\d+%.*", line):
            continue
        if re.fullmatch(r"[QqＱｑ]?\d+", line):
            continue
        if line in {"A", "B", "C", "D", "Q", "正确", "错误"}:
            continue
        if re.search(r"(大都会|卢浮宫|梵蒂冈|美术馆)$", line) and len(line) < 18:
            continue
        cleaned.append(line)

    candidates: list[str] = []
    if cleaned:
        # Best first: longest Chinese-heavy block, then joined top lines
        by_len = sorted(cleaned, key=len, reverse=True)
        candidates.append("".join(cleaned[:3]))
        candidates.extend(by_len[:3])
        if len(cleaned) >= 2:
            candidates.append("".join(cleaned[:2]))

    seen: set[str] = set()
    unique: list[str] = []
    for c in candidates:
        key = normalize_text(c)
        if len(key) < 6 or key in seen:
            continue
        seen.add(key)
        unique.append(c)
    return unique[:4]


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
            scorer=fuzz.partial_ratio,
            limit=max(limit * 3, 12),
            score_cutoff=55,
        ):
            scored_map[i] = max(scored_map.get(i, 0.0), float(score))

        if include_answers:
            for _match, score, i in process.extract(
                query,
                idx["a_map"],
                scorer=fuzz.partial_ratio,
                limit=max(limit * 2, 8),
                score_cutoff=70,
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


def _prepare_image(image_bytes: bytes) -> Image.Image | None:
    if not image_bytes:
        return None
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    w, h = img.size
    if w < 20 or h < 20:
        return None
    # Keep OCR input modest — biggest speed win after disabling cls
    max_w = 900
    if w > max_w:
        nh = max(1, int(h * (max_w / w)))
        img = img.resize((max_w, nh), Image.Resampling.BILINEAR)
    return img


def ocr_image(image_bytes: bytes) -> str:
    img = _prepare_image(image_bytes)
    if img is None:
        return ""

    w, h = img.size
    # Already-cropped short screenshots: OCR full frame
    if h / max(w, 1) < 0.55:
        crop = img
    else:
        crop = img.crop((int(w * 0.18), int(h * 0.08), int(w * 0.82), int(h * 0.45)))
        cw, ch = crop.size
        if cw < 80 or ch < 40:
            crop = img

    with _OCR_LOCK:
        ocr = get_ocr()
        result, _ = ocr(np.asarray(crop))
        text_lines = [item[1] for item in (result or []) if item and len(item) > 1]
        text = "\n".join(text_lines)
        zh_chars = len(re.findall(r"[\u4e00-\u9fff]", text))
        if zh_chars < 6 and crop is not img:
            result2, _ = ocr(np.asarray(img))
            text_lines2 = [item[1] for item in (result2 or []) if item and len(item) > 1]
            if len("".join(text_lines2)) > len("".join(text_lines)):
                text = "\n".join(text_lines2)
    return text


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


@app.post("/api/solve")
def api_solve():
    t0 = time.perf_counter()
    if "image" not in request.files and "file" not in request.files:
        return jsonify({"error": "请上传截图 image"}), 400

    file = request.files.get("image") or request.files.get("file")
    image_bytes = file.read()
    if not image_bytes:
        return jsonify({"error": "图片为空"}), 400

    try:
        ocr_text = ocr_image(image_bytes)
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
    t_ocr = time.perf_counter()

    candidates = extract_question_candidates(ocr_text)
    best_results: list[dict] = []
    used_query = ""
    try:
        for cand in candidates:
            # OCR solve: question-only matching is enough and much faster
            results = search_bank(cand, limit=3, include_answers=False)
            if results and (
                not best_results or results[0]["score"] > best_results[0]["score"]
            ):
                best_results = results
                used_query = cand
            if best_results and best_results[0]["score"] >= 92:
                break
    except Exception as exc:
        return jsonify({"error": f"匹配失败: {exc}", "ocr_text": ocr_text}), 500
    t_end = time.perf_counter()

    answer = best_results[0] if best_results else None
    return jsonify(
        {
            "ocr_text": ocr_text,
            "matched_query": used_query,
            "answer": answer["answer"] if answer else None,
            "confidence": answer["score"] if answer else 0,
            "matched_question": answer["question"] if answer else None,
            "candidates": best_results,
            "timing_ms": {
                "ocr": round((t_ocr - t0) * 1000),
                "match": round((t_end - t_ocr) * 1000),
                "total": round((t_end - t0) * 1000),
            },
        }
    )


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
