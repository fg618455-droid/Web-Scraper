"""
Run once to generate icon.ico.
python make_icon.py
"""
import os

try:
    from PIL import Image, ImageDraw
except ImportError:
    print("Pillow fehlt.  pip install pillow")
    raise SystemExit(1)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT      = os.path.join(BASE_DIR, 'icon.ico')
SIZES    = [16, 24, 32, 48, 64, 128, 256]


def _rounded_rect_mask(draw, box, radius, fill):
    """Draw a filled rounded rectangle."""
    x0, y0, x1, y1 = box
    r = min(radius, (x1 - x0) // 2, (y1 - y0) // 2)
    draw.rectangle([x0 + r, y0, x1 - r, y1], fill=fill)
    draw.rectangle([x0, y0 + r, x1, y1 - r], fill=fill)
    draw.ellipse([x0, y0, x0 + 2 * r, y0 + 2 * r], fill=fill)
    draw.ellipse([x1 - 2 * r, y0, x1, y0 + 2 * r], fill=fill)
    draw.ellipse([x0, y1 - 2 * r, x0 + 2 * r, y1], fill=fill)
    draw.ellipse([x1 - 2 * r, y1 - 2 * r, x1, y1], fill=fill)


def make_frame(size: int) -> Image.Image:
    img  = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # ── Background: deep indigo rounded rect ──────────────────────
    m = max(1, size // 10)
    radius = size // 4
    _rounded_rect_mask(draw, [m, m, size - m, size - m], radius,
                       (55, 48, 163, 255))   # #3730a3

    # Subtle lighter inner top half (simulate gradient)
    inner_h = (size - 2 * m) // 2
    _rounded_rect_mask(draw, [m, m, size - m, m + inner_h], radius,
                       (99, 102, 241, 40))   # faint indigo overlay

    # ── Price tag shape ────────────────────────────────────────────
    # A rounded rectangle body + triangle pointer at bottom
    pad   = size * 0.15
    tw    = int(size - 2 * pad)            # tag width
    th    = int(tw * 0.72)                 # tag body height
    tx0   = int(pad)
    ty0   = int(size * 0.12)
    tx1   = int(tx0 + tw)
    ty1   = int(ty0 + th)
    tr    = max(2, int(tw * 0.14))         # corner radius

    _rounded_rect_mask(draw, [tx0, ty0, tx1, ty1], tr,
                       (255, 255, 255, 240))

    # Triangle pointer (pointing down)
    cx    = size // 2
    tri_h = int(tw * 0.18)
    # Overlap slightly with body to avoid gap
    draw.polygon(
        [(cx - int(tw * 0.22), ty1 - 1),
         (cx + int(tw * 0.22), ty1 - 1),
         (cx, ty1 + tri_h)],
        fill=(255, 255, 255, 240)
    )

    # Hole at top-center (tag eyelet)
    hole_r = max(2, int(tw * 0.09))
    hcx    = cx
    hcy    = ty0
    draw.ellipse(
        [hcx - hole_r, hcy - hole_r, hcx + hole_r, hcy + hole_r],
        fill=(55, 48, 163, 255)
    )

    # ── Lines inside tag (price label look) ───────────────────────
    lw   = max(1, int(tw * 0.07))
    lx0  = tx0 + int(tw * 0.15)
    lx1  = tx1 - int(tw * 0.15)
    midY = (ty0 + ty1) // 2

    # Top line — thick (represents the price number)
    y1_line = midY - int(th * 0.1)
    draw.rounded_rectangle(
        [lx0, y1_line - lw, lx1, y1_line + lw],
        radius=lw,
        fill=(79, 70, 229, 200)
    )

    # Bottom line — shorter, thinner
    y2_line = midY + int(th * 0.18)
    draw.rounded_rectangle(
        [lx0, y2_line, lx0 + int((lx1 - lx0) * 0.62), y2_line + max(1, lw - 1)],
        radius=lw,
        fill=(79, 70, 229, 130)
    )

    return img


frames = [make_frame(s) for s in SIZES]
frames[0].save(
    OUT,
    format='ICO',
    sizes=[(s, s) for s in SIZES],
    append_images=frames[1:],
)
print(f"icon.ico erstellt: {OUT}")
