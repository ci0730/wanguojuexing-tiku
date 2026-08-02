# -*- coding: utf-8 -*-
"""Build a portable fast RapidOCR config at runtime (no machine-specific paths)."""
from __future__ import annotations

from pathlib import Path


def write_fast_ocr_config(target: Path) -> Path:
    import os

    import rapidocr_onnxruntime as r
    import yaml

    pkg = Path(r.__file__).resolve().parent
    cfg = yaml.safe_load((pkg / "config.yaml").read_text(encoding="utf-8"))

    # Aggressive speed knobs for quiz crops (~700×300)
    cfg["Global"]["use_cls"] = False
    cfg["Global"]["print_verbose"] = False
    cfg["Global"]["max_side_len"] = 560
    cfg["Det"]["limit_side_len"] = 384
    cfg["Det"]["max_candidates"] = 40
    cfg["Det"]["box_thresh"] = 0.5
    cfg["Det"]["thresh"] = 0.35
    cfg["Det"]["unclip_ratio"] = 1.6
    cfg["Rec"]["rec_batch_num"] = 6

    for sec in ("Det", "Cls", "Rec"):
        rel = cfg[sec]["model_path"]
        cfg[sec]["model_path"] = str((pkg / rel).resolve())

    threads = max(2, min(6, (os.cpu_count() or 4)))
    for sec in ("Global", "Det", "Cls", "Rec"):
        if isinstance(cfg.get(sec), dict) and "intra_op_num_threads" in cfg[sec]:
            cfg[sec]["intra_op_num_threads"] = threads
            cfg[sec]["inter_op_num_threads"] = 1

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return target
