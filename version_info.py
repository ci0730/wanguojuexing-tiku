# -*- coding: utf-8 -*-
"""App version + remote update check (China-friendly CDN / site JSON)."""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

import bank_io

# Bump this when shipping a new installer / portable build.
APP_VERSION = "1.0.0"

# Prefer China site; jsDelivr as fallback (same repo file).
VERSION_URLS = (
    "https://wanguojuexing.cn.mt/version.json",
    "https://cdn.jsdelivr.net/gh/ci0730/wanguojuexing-tiku@master/site/version.json",
)

DEFAULT_DOWNLOAD_URL = "https://wwbuq.lanzouu.com/iDyEJ3xuc8da"

_CACHE: dict | None = None
_CACHE_AT = 0.0
_CACHE_TTL = 6 * 3600  # 6 hours


def _prefs_path() -> Path:
    return bank_io.user_data_dir() / "update_prefs.json"


def load_update_prefs() -> dict:
    path = _prefs_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_update_prefs(data: dict) -> None:
    path = _prefs_path()
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def skip_version(version: str) -> None:
    prefs = load_update_prefs()
    prefs["skipped_version"] = str(version or "").strip()
    prefs["skipped_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_update_prefs(prefs)


def parse_version(text: str) -> tuple[int, ...]:
    nums = [int(x) for x in re.findall(r"\d+", str(text or ""))]
    if not nums:
        return (0,)
    return tuple(nums[:4])


def is_newer(remote: str, local: str = APP_VERSION) -> bool:
    return parse_version(remote) > parse_version(local)


def _http_json(url: str, timeout: float = 6.0) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": f"RoKQuizBank/{APP_VERSION}",
            "Accept": "application/json",
            "Cache-Control": "no-cache",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("version.json 格式无效")
    return data


def fetch_remote_version(*, force: bool = False) -> dict | None:
    """Return remote manifest or None on failure. Cached briefly."""
    global _CACHE, _CACHE_AT
    now = time.time()
    if not force and _CACHE is not None and now - _CACHE_AT < _CACHE_TTL:
        return _CACHE
    last_err: Exception | None = None
    for url in VERSION_URLS:
        try:
            data = _http_json(url)
            ver = str(data.get("version") or data.get("latest") or "").strip()
            if not ver:
                continue
            normalized = {
                "version": ver.lstrip("vV"),
                "download_url": str(data.get("download_url") or DEFAULT_DOWNLOAD_URL).strip(),
                "notes": str(data.get("notes") or "").strip(),
                "force": bool(data.get("force")),
                "source": url,
            }
            _CACHE = normalized
            _CACHE_AT = now
            return normalized
        except Exception as exc:
            last_err = exc
            continue
    if last_err:
        return None
    return None


def check_for_update(*, force_fetch: bool = False) -> dict:
    """
    Return update status for UI / desktop popup.

    Keys: app_version, latest, update_available, download_url, notes, force, skipped, checked
    """
    remote = fetch_remote_version(force=force_fetch)
    prefs = load_update_prefs()
    skipped = str(prefs.get("skipped_version") or "").strip()
    result = {
        "app_version": APP_VERSION,
        "latest": None,
        "update_available": False,
        "download_url": DEFAULT_DOWNLOAD_URL,
        "notes": "",
        "force": False,
        "skipped": False,
        "checked": remote is not None,
    }
    if not remote:
        return result

    latest = remote["version"]
    result["latest"] = latest
    result["download_url"] = remote["download_url"] or DEFAULT_DOWNLOAD_URL
    result["notes"] = remote["notes"]
    result["force"] = remote["force"]

    if not is_newer(latest, APP_VERSION):
        return result

    if skipped and skipped == latest and not remote["force"]:
        result["skipped"] = True
        return result

    result["update_available"] = True
    return result
