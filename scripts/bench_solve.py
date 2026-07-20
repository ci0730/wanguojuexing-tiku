# -*- coding: utf-8 -*-
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import app, reload_bank


def post_image(client, path: Path):
    data = path.read_bytes()
    boundary = "----Bound" + uuid.uuid4().hex
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="image"; filename="q.png"\r\n'
        f"Content-Type: image/png\r\n\r\n"
    ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
    return client.post(
        "/api/solve",
        data=body,
        content_type=f"multipart/form-data; boundary={boundary}",
    )


def main():
    reload_bank()
    c = app.test_client()
    sample = Path("data/sample-q1.png")

    t0 = time.perf_counter()
    warm = c.post("/api/warmup").get_json()
    print("warmup", warm, "client_ms", round((time.perf_counter() - t0) * 1000))

    r1 = post_image(c, sample).get_json()
    print("solve1", r1.get("answer"), r1.get("confidence"), r1.get("timing_ms"))
    r2 = post_image(c, sample).get_json()
    print("solve2", r2.get("answer"), r2.get("confidence"), r2.get("timing_ms"))

    t0 = time.perf_counter()
    for _ in range(30):
        c.get("/api/search?q=" + "布匿战争")
    print("30_searches_ms", round((time.perf_counter() - t0) * 1000))

    assert r2.get("answer") == "伦敦国家美术馆", r2
    print("PASS")


if __name__ == "__main__":
    main()
