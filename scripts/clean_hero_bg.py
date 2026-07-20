# -*- coding: utf-8 -*-
"""Crop chrome off battle screenshot for landing hero background."""
from pathlib import Path
from PIL import Image, ImageEnhance, ImageFilter

SRC = Path(
    r"C:\Users\zhou\.cursor\projects\f\assets"
    r"\c__Users_zhou_AppData_Roaming_Cursor_User_workspaceStorage_empty-window_images_image-c629b4f9-b50c-4d59-b1f5-7ff9fcf3752e.png"
)
OUT = Path(r"F:\万国觉醒\site\assets\hero-battle.jpg")


def main() -> None:
    im = Image.open(SRC).convert("RGB")
    w, h = im.size
    # Cut HUD / watermark / side menus
    left = int(w * 0.04)
    top = int(h * 0.09)
    right = int(w * 0.90)
    bottom = int(h * 0.70)
    crop = im.crop((left, top, right, bottom))

    # Upscale + clarity
    tw, th = crop.size
    out = crop.resize((tw * 2, th * 2), Image.Resampling.LANCZOS)
    out = ImageEnhance.Sharpness(out).enhance(1.45)
    out = ImageEnhance.Contrast(out).enhance(1.12)
    out = ImageEnhance.Color(out).enhance(1.08)
    # Slight edge soften to hide residual HUD dots when scaled as cover
    soft = out.filter(ImageFilter.GaussianBlur(radius=0.6))
    out = Image.blend(out, soft, 0.15)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    out.save(OUT, "JPEG", quality=88, optimize=True)
    print("wrote", OUT, out.size, flush=True)


if __name__ == "__main__":
    main()
