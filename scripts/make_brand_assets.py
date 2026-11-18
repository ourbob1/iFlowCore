#!/usr/bin/env python3
"""
Build the app's brand assets from the source logo in images/.

    pip install numpy scipy pillow        # tooling only; not app dependencies
    python scripts/make_brand_assets.py [images/ifc_logo.png]

The source artwork has a grey "transparency" checkerboard painted into its pixels (it's RGB, no
alpha), plus a baked drop shadow under the emblem. This script:

  1. removes the checkerboard: light-grey, colourless regions connected to the border, plus the
     enclosed checker inside letter counters (O, R, A, D …) below the emblem;
  2. clips the emblem to the convex hull of its reliably coloured pixels (the hexagon is convex and
     solid), which drops the drop shadow and restores rim pixels that match the checker grey;
  3. un-mixes anti-aliased edges against the local background so no light halo remains;
  4. writes a dark-theme variant with light lettering (the gold i-dot and the emblem unchanged).

Outputs (checked in, so building the app never needs numpy/scipy):
  assets/brand/logo.png, logo_dark.png   full lock-up (emblem + wordmark + tagline), 960 px wide
  assets/brand/emblem.png                hexagon emblem, 512 px tall
  assets/icon.png                        1024×1024 app icon (`flet build` uses it for every platform)
  assets/icons/iflowcore.png|.ico|.icns  desktop installers (package_desktop.py picks them up)
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage as ndi
from scipy.spatial import ConvexHull

ROOT = Path(__file__).resolve().parents[1]


def cut_out(src: Path) -> tuple[Image.Image, Image.Image, Image.Image]:
    im = np.asarray(Image.open(src).convert("RGB")).astype(float)
    g = im.mean(axis=2); sat = im.max(axis=2) - im.min(axis=2)
    cand = (sat <= 12) & (g >= 160) & (g <= 240)
    lab, n = ndi.label(cand)
    border = set(np.unique(np.concatenate([lab[0], lab[-1], lab[:, 0], lab[:, -1]]))) - {0}
    bg = np.isin(lab, list(border))
    TEXT_TOP = 940                                  # below the emblem: enclosed light-grey = letter counters
    objs = ndi.find_objects(lab)
    for i, sl in enumerate(objs, 1):
        if sl is not None and i not in border and sl[0].start >= TEXT_TOP:
            bg |= lab == i
    fg = ~bg
    # The emblem carries a baked grey drop shadow (checker-textured, mid grey). Clip the emblem band to the
    # convex hull of reliably coloured pixels: saturated gold/blue, bright highlights, dark shading.
    H, W = g.shape
    reliable = fg & ((sat > 30) | (g > 238) | (g < 80))
    reliable[TEXT_TOP - 40:] = False
    reliable = ndi.binary_opening(reliable, iterations=2)
    ys, xs = np.nonzero(reliable)
    hull = ConvexHull(np.c_[xs, ys])
    SS = 4
    poly = Image.new("L", (W * SS, H * SS), 0)
    ImageDraw.Draw(poly).polygon([(xs[v] * SS, ys[v] * SS) for v in hull.vertices], fill=255)
    hull_alpha = np.asarray(poly.resize((W, H), Image.LANCZOS)).astype(float) / 255
    LETTERS_TOP = 948                               # first row of the wordmark (the i-dot sits left of the emblem)
    hx0, hx1 = xs[hull.vertices].min(), xs[hull.vertices].max()
    emblem_band = np.zeros_like(fg); emblem_band[:TEXT_TOP - 40] = True
    emblem_band[:LETTERS_TOP, max(hx0 - 12, 0):hx1 + 13] = True
    inside = hull_alpha > 0.98
    fg = (fg & ~(emblem_band & (hull_alpha < 0.02))) | (emblem_band & inside)   # the hexagon is solid
    bg = ~fg
    # edge unmixing: P = a*F + (1-a)*B with local estimates of B (removed bg) and F (solid interior)
    core = ndi.binary_erosion(fg, iterations=2)
    band = fg & ~core
    def local_mean(mask, k):
        w = ndi.uniform_filter(mask.astype(float), k)
        c = np.stack([ndi.uniform_filter(im[..., j] * mask, k) for j in range(3)], -1)
        return c / np.maximum(w, 1e-6)[..., None], w
    B, wb = local_mean(bg, 9)
    F, wf = local_mean(core, 9)
    alpha = fg.astype(float)
    P = im
    d = F - B
    num = ((P - B) * d).sum(-1); den = (d * d).sum(-1)
    a = np.clip(num / np.maximum(den, 1e-6), 0, 1)
    ok = band & (wb > 0.05) & (wf > 0.05) & (den > 400)
    alpha[ok] = a[ok]
    rgb = im.copy()
    rgb[band & (wf > 0.05)] = F[band & (wf > 0.05)]   # de-fringe: edge colour from the solid interior
    alpha[emblem_band] = hull_alpha[emblem_band]                  # geometric edge, anti-aliased
    rgb[emblem_band] = im[emblem_band]                            # emblem pixels are original colours
    rgba = np.dstack([np.clip(rgb, 0, 255), alpha * 255]).astype(np.uint8)
    res = Image.fromarray(rgba, "RGBA")
    box = res.getbbox()
    # dark-theme variant: navy/black lettering → light; the gold dot and the emblem stay as they are
    dark = rgba.copy()
    text = np.zeros(g.shape, bool); text[TEXT_TOP - 40:] = True
    hue_gold = (im[..., 0] > im[..., 2] + 25)
    letters = text & ~hue_gold & (alpha > 0)
    lum = np.clip(g / 255, 0, 1)
    tint = np.array([236, 240, 248], float)
    dark[letters, :3] = (tint * (0.82 + 0.18 * lum[letters, None])).clip(0, 255).astype(np.uint8)
    em_rows = np.nonzero(alpha[:TEXT_TOP - 40].max(axis=1) > 0)[0]
    em = res.crop((box[0], em_rows.min(), box[2], em_rows.max() + 1))
    em = em.crop(em.getbbox())
    return res.crop(box), Image.fromarray(dark, "RGBA").crop(box), em


def square_icon(emblem: Image.Image, size: int, padding: float = 0.08) -> Image.Image:
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    inner = int(size * (1 - 2 * padding))
    e = emblem.copy()
    e.thumbnail((inner, inner), Image.LANCZOS)
    canvas.alpha_composite(e, ((size - e.width) // 2, (size - e.height) // 2))
    return canvas


def fit_width(img: Image.Image, width: int) -> Image.Image:
    return img.resize((width, round(img.height * width / img.width)), Image.LANCZOS)


def main(argv: list[str]) -> int:
    src = Path(argv[0]) if argv else ROOT / "images" / "ifc_logo.png"
    light, dark, emblem = cut_out(src)
    brand, icons = ROOT / "assets" / "brand", ROOT / "assets" / "icons"
    brand.mkdir(parents=True, exist_ok=True)
    icons.mkdir(parents=True, exist_ok=True)
    fit_width(light, 960).save(brand / "logo.png", optimize=True)
    fit_width(dark, 960).save(brand / "logo_dark.png", optimize=True)
    e = emblem.copy()
    e.thumbnail((512, 512), Image.LANCZOS)
    e.save(brand / "emblem.png", optimize=True)
    icon = square_icon(emblem, 1024)
    icon.save(ROOT / "assets" / "icon.png", optimize=True)
    square_icon(emblem, 256).save(icons / "iflowcore.png", optimize=True)
    icon.save(icons / "iflowcore.ico", sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])
    icon.save(icons / "iflowcore.icns")
    for p in sorted([*brand.iterdir(), ROOT / "assets" / "icon.png", *icons.iterdir()]):
        print(f"{p.relative_to(ROOT)}  {p.stat().st_size // 1024} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
