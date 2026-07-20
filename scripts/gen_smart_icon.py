# -*- coding: utf-8 -*-
"""Generate a scholarly owl app icon to replace the old mascot."""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
SIZE = 1024


def main() -> None:
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))

    bg = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    bg_draw = ImageDraw.Draw(bg)
    bg_draw.rounded_rectangle((0, 0, SIZE - 1, SIZE - 1), radius=180, fill=(18, 42, 58, 255))

    glow = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    for i, a in enumerate(range(40, 0, -4)):
        r = 220 + i * 18
        gd.ellipse(
            (SIZE // 2 - r, SIZE // 2 - r + 40, SIZE // 2 + r, SIZE // 2 + r + 40),
            fill=(212, 168, 75, a),
        )
    glow = glow.filter(ImageFilter.GaussianBlur(40))
    bg = Image.alpha_composite(bg, glow)
    img = Image.alpha_composite(img, bg)
    draw = ImageDraw.Draw(img)

    cx, cy = SIZE // 2, SIZE // 2 - 20
    head_color = (236, 228, 210, 255)
    gold = (212, 168, 75, 255)

    # Head + ear tufts
    draw.ellipse((cx - 230, cy - 260, cx + 230, cy + 120), fill=head_color)
    draw.polygon([(cx - 180, cy - 200), (cx - 240, cy - 340), (cx - 80, cy - 230)], fill=head_color)
    draw.polygon([(cx + 180, cy - 200), (cx + 240, cy - 340), (cx + 80, cy - 230)], fill=head_color)
    draw.ellipse((cx - 200, cy - 200, cx + 200, cy + 80), fill=(245, 240, 228, 255))

    # Scholarly glasses
    lx, rx = cx - 95, cx + 95
    gy = cy - 70
    gr = 78
    draw.ellipse((lx - gr, gy - gr, lx + gr, gy + gr), outline=gold, width=14)
    draw.ellipse((lx - gr + 8, gy - gr + 8, lx + gr - 8, gy + gr - 8), fill=(40, 70, 90, 90))
    draw.ellipse((rx - gr, gy - gr, rx + gr, gy + gr), outline=gold, width=14)
    draw.ellipse((rx - gr + 8, gy - gr + 8, rx + gr - 8, gy + gr - 8), fill=(40, 70, 90, 90))
    draw.arc((lx + gr - 20, gy - 30, rx - gr + 20, gy + 20), 200, 340, fill=gold, width=12)
    draw.line((lx - gr, gy, lx - gr - 50, gy - 10), fill=gold, width=12)
    draw.line((rx + gr, gy, rx + gr + 50, gy - 10), fill=gold, width=12)

    # Focused eyes
    for ex in (lx, rx):
        draw.ellipse((ex - 42, gy - 38, ex + 42, gy + 42), fill=(250, 250, 248, 255))
        draw.ellipse((ex - 28, gy - 20, ex + 28, gy + 36), fill=(35, 95, 110, 255))
        draw.ellipse((ex - 14, gy - 5, ex + 14, gy + 24), fill=(15, 30, 40, 255))
        draw.ellipse((ex - 8, gy - 12, ex - 1, gy - 5), fill=(255, 255, 255, 220))
    draw.arc((lx - 55, gy - 95, lx + 55, gy - 20), 200, 340, fill=(90, 70, 45, 255), width=10)
    draw.arc((rx - 55, gy - 95, rx + 55, gy - 20), 200, 340, fill=(90, 70, 45, 255), width=10)

    # Beak
    draw.polygon([(cx - 28, cy + 20), (cx + 28, cy + 20), (cx, cy + 70)], fill=(200, 140, 50, 255))
    draw.polygon([(cx - 14, cy + 28), (cx + 14, cy + 28), (cx, cy + 55)], fill=(230, 180, 80, 255))

    # Robe + gold collar
    draw.ellipse((cx - 160, cy + 80, cx + 160, cy + 340), fill=(28, 72, 88, 255))
    draw.arc((cx - 150, cy + 70, cx + 150, cy + 200), 200, 340, fill=gold, width=18)

    # Open book
    book_y = cy + 200
    draw.polygon(
        [(cx - 140, book_y - 20), (cx - 10, book_y + 10), (cx - 10, book_y + 90), (cx - 140, book_y + 55)],
        fill=(180, 130, 60, 255),
    )
    draw.polygon(
        [(cx + 140, book_y - 20), (cx + 10, book_y + 10), (cx + 10, book_y + 90), (cx + 140, book_y + 55)],
        fill=(180, 130, 60, 255),
    )
    draw.polygon(
        [(cx - 125, book_y - 10), (cx - 10, book_y + 18), (cx - 10, book_y + 78), (cx - 125, book_y + 45)],
        fill=(245, 240, 225, 255),
    )
    draw.polygon(
        [(cx + 125, book_y - 10), (cx + 10, book_y + 18), (cx + 10, book_y + 78), (cx + 125, book_y + 45)],
        fill=(245, 240, 225, 255),
    )
    for i in range(4):
        y = book_y + 22 + i * 12
        draw.line((cx - 100, y, cx - 25, y + 8), fill=(160, 145, 120, 200), width=3)
        draw.line((cx + 100, y, cx + 25, y + 8), fill=(160, 145, 120, 200), width=3)

    for px, py, r in (
        (180, 200, 6),
        (820, 240, 5),
        (200, 720, 4),
        (800, 700, 6),
        (150, 500, 3),
        (860, 480, 4),
    ):
        draw.ellipse((px - r, py - r, px + r, py + r), fill=(230, 200, 120, 200))

    mask = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, SIZE - 1, SIZE - 1), radius=180, fill=255)
    out = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)

    assets = ROOT / "assets"
    web_assets = ROOT / "web" / "assets"
    assets.mkdir(exist_ok=True)
    web_assets.mkdir(exist_ok=True)

    out.save(assets / "app.png", "PNG")
    out.save(web_assets / "app.png", "PNG")
    out.save(assets / "mascot.png", "PNG")
    out.save(web_assets / "mascot.png", "PNG")

    ico_sizes = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    icons = [out.resize(s, Image.Resampling.LANCZOS) for s in ico_sizes]
    icons[0].save(
        assets / "app.ico",
        format="ICO",
        sizes=[(i.width, i.height) for i in icons],
        append_images=icons[1:],
    )

    print("OK", (assets / "app.png").stat().st_size, (assets / "app.ico").stat().st_size)


if __name__ == "__main__":
    main()
