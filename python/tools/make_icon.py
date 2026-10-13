"""Generate assets/kopy.ico: documents flowing along an arrow into a locked USB drive.

Small sizes (16-32 px) use a simplified drawing (arrow + locked drive) so they
stay legible in Explorer's list view and the taskbar; 48 px and up add the
documents. Requires Pillow (dev only - the built exe does not need it):

    pip install pillow
    python tools/make_icon.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
OUT_ICO = ROOT / "assets" / "kopy.ico"
OUT_PNG = ROOT / "assets" / "kopy-256.png"

SIZES = [16, 24, 32, 48, 64, 128, 256]
SIMPLE_MAX = 32  # sizes at or below this use the simplified drawing
SS = 4  # supersampling factor for smooth edges

BG_TOP, BG_BOTTOM = (31, 72, 112), (16, 38, 62)
PAPER, PAPER_BACK, PAPER_LINE = (255, 255, 255), (200, 214, 228), (150, 166, 182)
ARROW = (61, 220, 132)
DRIVE, DRIVE_EDGE = (214, 222, 230), (120, 136, 152)
CONNECTOR, CONNECTOR_HOLE = (140, 154, 168), (60, 74, 88)
LOCK, LOCK_DARK = (245, 184, 52), (120, 80, 10)


def _s(*v: float) -> list[int]:
    return [round(x * SS) for x in v]


def _background(size: int) -> Image.Image:
    grad = Image.new("RGBA", (size, size))
    px = grad.load()
    for y in range(size):
        t = y / (size - 1)
        c = tuple(round(BG_TOP[i] + (BG_BOTTOM[i] - BG_TOP[i]) * t) for i in range(3)) + (255,)
        for x in range(size):
            px[x, y] = c
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(_s(8, 8, 248, 248), radius=48 * SS, fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(grad, (0, 0), mask)
    return out


def _drive(d: ImageDraw.ImageDraw, x0: float, y0: float, w: float, h: float) -> None:
    """Vertical USB stick with a padlock on its body. (x0, y0) is the body's top-left."""
    cw = w * 0.52
    cx0 = x0 + (w - cw) / 2
    ch = h * 0.24
    # connector
    d.rounded_rectangle(_s(cx0, y0 - ch, cx0 + cw, y0 + 6), radius=4 * SS, fill=CONNECTOR)
    hw = cw * 0.22
    for hx in (cx0 + cw * 0.18, cx0 + cw * 0.60):
        d.rectangle(_s(hx, y0 - ch * 0.70, hx + hw, y0 - ch * 0.38), fill=CONNECTOR_HOLE)
    # body
    d.rounded_rectangle(_s(x0, y0, x0 + w, y0 + h), radius=14 * SS, fill=DRIVE,
                        outline=DRIVE_EDGE, width=3 * SS)
    # padlock
    lw = w * 0.60
    lx0 = x0 + (w - lw) / 2
    ly0 = y0 + h * 0.46
    lh = h * 0.30
    sr = lw * 0.32  # shackle radius
    scx = x0 + w / 2
    sw = lw * 0.15  # shackle thickness
    acy = ly0 - sr * 0.55  # arc centre; straight legs run from here into the body
    d.arc(_s(scx - sr, acy - sr, scx + sr, acy + sr), start=180, end=360,
          fill=LOCK_DARK, width=round(sw * SS))
    for lx in (scx - sr, scx + sr - sw):
        d.rectangle(_s(lx, acy, lx + sw, ly0 + 2), fill=LOCK_DARK)
    d.rounded_rectangle(_s(lx0, ly0, lx0 + lw, ly0 + lh), radius=6 * SS, fill=LOCK)
    kr = lw * 0.10
    d.ellipse(_s(scx - kr, ly0 + lh * 0.28, scx + kr, ly0 + lh * 0.28 + 2 * kr), fill=LOCK_DARK)
    d.rectangle(_s(scx - kr * 0.45, ly0 + lh * 0.40, scx + kr * 0.45, ly0 + lh * 0.80), fill=LOCK_DARK)


def _arrow(d: ImageDraw.ImageDraw, x0: float, x1: float, cy: float, thick: float, head: float) -> None:
    d.rectangle(_s(x0, cy - thick / 2, x1 - head * 0.8, cy + thick / 2), fill=ARROW)
    d.polygon([tuple(_s(x1 - head, cy - head)), tuple(_s(x1, cy)), tuple(_s(x1 - head, cy + head))], fill=ARROW)


def _documents(d: ImageDraw.ImageDraw) -> None:
    d.rounded_rectangle(_s(22, 66, 88, 156), radius=6 * SS, fill=PAPER_BACK)
    fold = 20
    x0, y0, x1, y1 = 34, 80, 102, 172
    d.polygon([tuple(_s(x0, y0)), tuple(_s(x1 - fold, y0)), tuple(_s(x1, y0 + fold)),
               tuple(_s(x1, y1)), tuple(_s(x0, y1))], fill=PAPER)
    d.polygon([tuple(_s(x1 - fold, y0)), tuple(_s(x1 - fold, y0 + fold)), tuple(_s(x1, y0 + fold))],
              fill=PAPER_BACK)
    for i, y in enumerate((106, 120, 134, 148, 160)):
        d.line(_s(44, y, 92 if i % 2 == 0 else 78, y), fill=PAPER_LINE, width=5 * SS)


def render(simple: bool) -> Image.Image:
    big = 256 * SS
    img = _background(big)
    d = ImageDraw.Draw(img)
    if simple:
        _arrow(d, 26, 118, 128, thick=34, head=40)
        _drive(d, 128, 74, 100, 150)
    else:
        _documents(d)
        _arrow(d, 106, 162, 128, thick=20, head=24)
        _drive(d, 168, 80, 66, 128)
    return img.resize((256, 256), Image.LANCZOS)


def main() -> None:
    full, simple = render(simple=False), render(simple=True)
    frames = {s: (simple if s <= SIMPLE_MAX else full).resize((s, s), Image.LANCZOS) for s in SIZES}
    OUT_ICO.parent.mkdir(parents=True, exist_ok=True)
    frames[256].save(OUT_ICO, format="ICO", sizes=[(s, s) for s in SIZES],
                     append_images=[frames[s] for s in SIZES if s != 256])
    frames[256].save(OUT_PNG)
    print(f"wrote {OUT_ICO} ({', '.join(map(str, SIZES))} px) and {OUT_PNG}")


if __name__ == "__main__":
    main()
