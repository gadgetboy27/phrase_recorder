"""Runs ON THE NANO (imported by panel_api.py). Renders text the panel cannot draw itself.

The panel (LVGL) can draw Latin and pre-shaped Arabic script. Everything else — Devanagari,
Gurmukhi, Lao, CJK — needs a real shaping engine, so the Nano renders it to a PNG with
Pillow + HarfBuzz (raqm) and the panel shows the image in place of the label. Rendered
files are cached under ~/panel/render/ by a hash of all parameters.

    GET /render?text=…&lang=hi&size=28&w=760&fg=1B2621&bg=F6F4EF&align=center
    GET /render/replies?lang=hi&w=776&h=168&cols=4&rows=2&bg=…&fg=…&t0=…&s0=…&t1=…   (a grid strip)
"""
import hashlib
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONTS = Path.home() / "panel" / "fonts"
CACHE = Path.home() / "panel" / "render"
CJK = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
FONT_FOR = {                                   # (file, face index)
    "hi": (FONTS / "NotoSansDevanagari.ttf", 0),
    "pa": (FONTS / "NotoSansGurmukhi.ttf", 0),
    "lo": (FONTS / "NotoSansLao.ttf", 0),
    "ja": (CJK, 0), "ko": (CJK, 1), "zh": (CJK, 2), "yue": (CJK, 4),
    "ar": (FONTS / "NotoSansArabic.ttf", 0), "fa": (FONTS / "NotoSansArabic.ttf", 0), "prs": (FONTS / "NotoSansArabic.ttf", 0),
}
LATIN = (FONTS / "NotoSans.ttf", 0)
RTL = {"ar", "fa", "prs"}

_fonts = {}


def font(lang, size):
    key = (lang, size)
    if key not in _fonts:
        path, idx = FONT_FOR.get(lang, LATIN)
        _fonts[key] = ImageFont.truetype(str(path), size, index=idx, layout_engine=ImageFont.LAYOUT_RAQM)
    return _fonts[key]


def wrap(text, f, width):
    """Greedy word wrap by measured width (CJK has no spaces: fall back to per-character)."""
    words = text.split(" ") if " " in text or len(text) < 12 else list(text)
    joiner = " " if " " in text or len(text) < 12 else ""
    lines, cur = [], ""
    for w in words:
        cand = (cur + joiner + w) if cur else w
        if f.getlength(cand) <= width or not cur:
            cur = cand
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _hex(c, default):
    try:
        return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))
    except (TypeError, ValueError):
        return default


def render_text(text, lang, size=28, w=760, fg="1B2621", bg="F6F4EF", align="center", max_lines=3):
    key = hashlib.sha1(f"t|{text}|{lang}|{size}|{w}|{fg}|{bg}|{align}|{max_lines}".encode()).hexdigest()[:16]
    out = CACHE / f"{key}.png"
    if out.exists():
        return out
    f = font(lang, size)
    lines = wrap(text, f, w - 8)[:max_lines]
    lh = int(size * 1.45)
    im = Image.new("RGB", (w, lh * len(lines) + 6), _hex(bg, (246, 244, 239)))
    d = ImageDraw.Draw(im)
    for i, line in enumerate(lines):
        tw = f.getlength(line)
        x = (w - tw) / 2 if align == "center" else (w - tw - 4 if (align == "right" or lang in RTL) else 4)
        d.text((x, 3 + i * lh), line, font=f, fill=_hex(fg, (27, 38, 33)), direction="rtl" if lang in RTL else None)
    CACHE.mkdir(parents=True, exist_ok=True)
    im.save(out, optimize=True)
    return out


def render_grid(cells, lang, w=776, h=168, cols=4, rows=2, gap=8, fg="1B2621", bg="F6F4EF", sub="8A8474"):
    """A strip of cells, each {'t': patient text, 's': clinician text, 'amber': bool}, laid out like the
    panel's reply buttons so the image can sit behind them."""
    key = hashlib.sha1(("g|" + repr(cells) + f"|{lang}|{w}|{h}|{cols}|{rows}|{gap}|{fg}|{bg}|{sub}").encode()).hexdigest()[:16]
    out = CACHE / f"{key}.png"
    if out.exists():
        return out
    cw, ch = (w - gap * (cols - 1)) / cols, (h - gap * (rows - 1)) / rows
    im = Image.new("RGB", (w, h), _hex(bg, (246, 244, 239)))
    d = ImageDraw.Draw(im)
    big, small = font(lang, 22), font("en", 13)
    for n, c in enumerate(cells[:cols * rows]):
        x0, y0 = (n % cols) * (cw + gap), (n // cols) * (ch + gap)
        lines = wrap(c["t"], big, cw - 12)[:2]
        for i, line in enumerate(lines):
            tw = big.getlength(line)
            d.text((x0 + (cw - tw) / 2, y0 + 8 + i * 27), line, font=big,
                   fill=(184, 132, 60) if c.get("amber") else _hex(fg, (27, 38, 33)), direction="rtl" if lang in RTL else None)
        s = c.get("s") or ""
        if s:
            s = wrap(s, small, cw - 12)[0]
            d.text((x0 + (cw - small.getlength(s)) / 2, y0 + ch - 22), s, font=small, fill=_hex(sub, (138, 132, 116)))
    CACHE.mkdir(parents=True, exist_ok=True)
    im.save(out, optimize=True)
    return out
