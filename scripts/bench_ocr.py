# -*- coding: utf-8 -*-
import time
from pathlib import Path

import numpy as np
import rapidocr_onnxruntime as r
import yaml
from PIL import Image
from rapidocr_onnxruntime import RapidOCR

pkg = Path(r.__file__).parent
img = Image.open(r"f:/万国觉醒/data/sample-q1.png").convert("RGB")
w, h = img.size
if w > 900:
    nh = int(h * (900 / w))
    img = img.resize((900, nh), Image.Resampling.BILINEAR)
w, h = img.size
crop = img.crop((int(w * 0.20), int(h * 0.10), int(w * 0.80), int(h * 0.40)))
arr = np.array(crop)
print("crop", arr.shape)


def make_cfg(det: int, max_side: int) -> Path:
    cfg = yaml.safe_load((pkg / "config.yaml").read_text(encoding="utf-8"))
    cfg["Global"]["use_cls"] = False
    cfg["Global"]["print_verbose"] = False
    cfg["Global"]["max_side_len"] = max_side
    cfg["Det"]["limit_side_len"] = det
    cfg["Det"]["max_candidates"] = 100
    cfg["Det"]["box_thresh"] = 0.55
    cfg["Rec"]["rec_batch_num"] = 8
    for sec in ("Det", "Cls", "Rec"):
        cfg[sec]["model_path"] = str((pkg / cfg[sec]["model_path"]).resolve())
    for sec in ("Global", "Det", "Cls", "Rec"):
        if "intra_op_num_threads" in cfg[sec]:
            cfg[sec]["intra_op_num_threads"] = 4
            cfg[sec]["inter_op_num_threads"] = 4
    path = Path(r"f:/万国觉醒/assets") / f"ocr_det{det}.yaml"
    path.write_text(yaml.dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


for det, ms in [(480, 960), (320, 720), (256, 640)]:
    path = make_cfg(det, ms)
    ocr = RapidOCR(config_path=str(path))
    ocr(arr)
    times = []
    text = ""
    for _ in range(3):
        t0 = time.perf_counter()
        result, _ = ocr(arr)
        t1 = time.perf_counter()
        times.append((t1 - t0) * 1000)
        text = " ".join(x[1] for x in (result or []))
    print(
        f"det={det} max={ms} avg={sum(times)/len(times):.0f}ms "
        f"times={[round(x) for x in times]} text={text[:50]}"
    )
