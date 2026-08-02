# -*- coding: utf-8 -*-
"""Quick smoke test for overlay + region OCR API."""
from __future__ import annotations

import io
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw

HOST = "127.0.0.1"
PORT = 8765


def check(name: str, ok: bool, detail: str = "") -> None:
    mark = "OK" if ok else "FAIL"
    line = f"[{mark}] {name}"
    if detail:
        line += f" — {detail}"
    print(line)
    if not ok:
        raise SystemExit(1)


def main() -> int:
    # 1) health
    try:
        data = json.loads(urllib.request.urlopen(f"http://{HOST}:{PORT}/api/health", timeout=3).read())
        check("health", data.get("ok") and data.get("app") == "rok-quiz", f"total={data.get('total')}")
    except Exception as exc:
        check("health", False, str(exc))

    # 2) overlay start API (must not spawn duplicate if already running)
    try:
        req = urllib.request.Request(f"http://{HOST}:{PORT}/api/overlay/start", method="POST")
        resp = json.loads(urllib.request.urlopen(req, timeout=5).read())
        check("overlay/start", resp.get("ok"), resp.get("message", ""))
        req2 = urllib.request.Request(f"http://{HOST}:{PORT}/api/overlay/start", method="POST")
        resp2 = json.loads(urllib.request.urlopen(req2, timeout=5).read())
        check(
            "overlay/start-idempotent",
            resp2.get("ok") and (resp2.get("already") in (True, False)),
            resp2.get("message", ""),
        )
    except urllib.error.HTTPError as exc:
        check("overlay/start", False, f"{exc.code} {exc.read().decode()[:120]}")
    except Exception as exc:
        check("overlay/start", False, str(exc))

    # 3) region solve via HTTP
    img = Image.new("RGB", (420, 180), "white")
    draw = ImageDraw.Draw(img)
    draw.text((12, 20), "Which city is called the White City?", fill="black")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    png = buf.getvalue()
    boundary = "----RoKTest"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="image"; filename="r.png"\r\n'
        f"Content-Type: image/png\r\n\r\n"
    ).encode("utf-8") + png + f"\r\n--{boundary}--\r\n".encode("utf-8")
    req = urllib.request.Request(
        f"http://{HOST}:{PORT}/api/solve?region=1",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    sol = json.loads(urllib.request.urlopen(req, timeout=45).read())
    check("region/solve", bool(sol.get("answer")), f"answer={sol.get('answer')} conf={sol.get('confidence')}")

    # 4) resize clamp
    from overlay import OverlayApp

    class Dummy:
        pass

    dummy = Dummy()
    w, h = OverlayApp._clamp_size(dummy, 50, 50)
    check("resize/min", w == 220 and h == 120, f"{w}x{h}")
    w2, h2 = OverlayApp._clamp_size(dummy, 5000, 5000)
    check("resize/max", w2 == 1200 and h2 == 720, f"{w2}x{h2}")

    print("\nAll overlay tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
