#!/usr/bin/env python3
"""Generate MapLibre glyph PBF ranges (signed-distance-field fonts) from a TTF/OTF file.

Kestrel serves its own label font so the map never depends on a third-party glyph server, which
matters on a disconnected laptop or an air-gapped network. The output is the same format fontnik
produces for Mapbox/MapLibre: one protobuf file per 256-codepoint range, each glyph a 24 px SDF
bitmap with a 3 px buffer (fontnik defaults: radius 8, cutoff 0.25).

    python tools/make_glyphs.py /usr/share/fonts/opentype/inter/Inter-SemiBold.otf "Inter SemiBold" \
        --out kestrelcop/web/static/glyphs --ranges 0-255 256-511 8192-8447

Dependencies: Pillow, numpy, scipy (all pure-build wheels). No protobuf library is needed; the
schema is five fields and is encoded by hand below.
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from scipy.ndimage import distance_transform_edt

FONT_SIZE = 24          # MapLibre lays text out at 24 px ("ONE_EM")
BUFFER = 3              # GLYPH_PBF_BORDER in MapLibre
RADIUS = 8.0            # SDF_PX
CUTOFF = 0.25           # edge sits at 255 * (1 - cutoff) = 191
SUPERSAMPLE = 4         # render at 96 px, measure distances at sub-pixel precision


# ---- protobuf encoding (glyphs.proto) ----------------------------------------------------------
def _varint(value: int) -> bytes:
    out = bytearray()
    value &= (1 << 64) - 1
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _zigzag(value: int) -> int:
    return (value << 1) ^ (value >> 31)


def _field_varint(number: int, value: int) -> bytes:
    return _varint((number << 3) | 0) + _varint(value)


def _field_bytes(number: int, data: bytes) -> bytes:
    return _varint((number << 3) | 2) + _varint(len(data)) + data


def encode_glyph(gid: int, bitmap: bytes | None, width: int, height: int, left: int, top: int, advance: int) -> bytes:
    body = _field_varint(1, gid)
    if bitmap:
        body += _field_bytes(2, bitmap)
    body += _field_varint(3, width) + _field_varint(4, height)
    body += _field_varint(5, _zigzag(left)) + _field_varint(6, _zigzag(top)) + _field_varint(7, advance)
    return body


def encode_fontstack(name: str, rng: str, glyphs: list[bytes]) -> bytes:
    stack = _field_bytes(1, name.encode("utf-8")) + _field_bytes(2, rng.encode("utf-8"))
    for g in glyphs:
        stack += _field_bytes(3, g)
    return _field_bytes(1, stack)  # glyphs { repeated fontstack stacks = 1 }


# ---- rasterising --------------------------------------------------------------------------------
def render_glyph(font_hi: ImageFont.FreeTypeFont, ch: str, ascender_hi: int) -> tuple[np.ndarray, int, int] | None:
    """Supersampled coverage array for one character plus its (left, top) bearing in hi-res pixels.

    `top` is measured from the baseline upwards (FreeType's bitmap_top convention).
    """
    try:
        l, t, r, b = font_hi.getbbox(ch, anchor="ls")  # anchor on the baseline, left
    except (OSError, ValueError):
        return None
    if r <= l or b <= t:
        return None
    pad = int(math.ceil((BUFFER + 1) * SUPERSAMPLE))
    w, h = r - l + 2 * pad, b - t + 2 * pad
    img = Image.new("L", (w, h), 0)
    ImageDraw.Draw(img).text((pad - l, pad - t), ch, font=font_hi, fill=255, anchor="ls")
    arr = np.asarray(img, dtype=np.float32) / 255.0
    # trim to the glyph's real ink box (metrics follow the ink, like FreeType's bitmap metrics)
    ys, xs = np.nonzero(arr > 0.0)
    if len(xs) == 0:
        return None
    x0, x1, y0, y1 = xs.min(), xs.max() + 1, ys.min(), ys.max() + 1
    ink = arr[y0:y1, x0:x1]
    left_hi = l - pad + x0            # bearing from the pen position to the ink's left edge
    top_hi = -(t - pad + y0)          # distance from the baseline up to the ink's top edge
    return ink, int(left_hi), int(top_hi)


def sdf_from_coverage(ink: np.ndarray) -> np.ndarray:
    """Pad with the 3 px buffer, compute a signed distance field and quantise it fontnik-style."""
    pad = BUFFER * SUPERSAMPLE
    cov = np.pad(ink, pad)
    inside = cov >= 0.5
    # distance to the nearest inside pixel (for outside points) and to the nearest outside pixel (for inside points)
    d_out = distance_transform_edt(~inside)
    d_in = distance_transform_edt(inside)
    signed = (d_out - d_in) / SUPERSAMPLE  # px at 24 px scale; positive outside the glyph
    # back to the 24 px grid: sample at pixel centres
    hs = SUPERSAMPLE // 2
    low = signed[hs::SUPERSAMPLE, hs::SUPERSAMPLE]
    value = 255.0 * (1.0 - (low / RADIUS + CUTOFF))
    return np.clip(np.rint(value), 0, 255).astype(np.uint8)


def build_range(font_path: str, start: int, end: int) -> tuple[list[bytes], int]:
    font_hi = ImageFont.truetype(font_path, FONT_SIZE * SUPERSAMPLE)
    font_lo = ImageFont.truetype(font_path, FONT_SIZE)
    ascender_hi, _ = font_hi.getmetrics()
    ascender = int(round(ascender_hi / SUPERSAMPLE))
    glyphs: list[bytes] = []
    for cp in range(start, end + 1):
        ch = chr(cp)
        if cp < 32 or (0x7F <= cp < 0xA0) or not ch.isprintable():
            continue
        advance = int(round(font_lo.getlength(ch)))
        rendered = render_glyph(font_hi, ch, ascender_hi)
        if rendered is None:
            if ch.isspace():
                glyphs.append(encode_glyph(cp, None, 0, 0, 0, 0, advance))
            continue
        ink, left_hi, top_hi = rendered
        sdf = sdf_from_coverage(ink)
        height, width = sdf.shape[0] - 2 * BUFFER, sdf.shape[1] - 2 * BUFFER
        left = int(round(left_hi / SUPERSAMPLE))
        bitmap_top = int(round(top_hi / SUPERSAMPLE))
        # fontnik stores the glyph top relative to the font ascender, which is what MapLibre's shaping expects
        top = bitmap_top - ascender
        glyphs.append(encode_glyph(cp, sdf.tobytes(), width, height, left, top, advance))
    return glyphs, ascender


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("font", help="path to a .ttf/.otf file")
    ap.add_argument("name", help='fontstack name used in the style, e.g. "Inter SemiBold"')
    ap.add_argument("--out", default="kestrelcop/web/static/glyphs")
    ap.add_argument("--ranges", nargs="+", default=["0-255", "256-511"], help="codepoint ranges, 256 wide, e.g. 0-255")
    args = ap.parse_args()
    out_dir = Path(args.out) / args.name
    out_dir.mkdir(parents=True, exist_ok=True)
    for rng in args.ranges:
        start, end = (int(x) for x in rng.split("-"))
        if end - start != 255 or start % 256:
            raise SystemExit(f"range {rng} must be 256 codepoints wide and aligned, like 256-511")
        glyphs, ascender = build_range(args.font, start, end)
        data = encode_fontstack(args.name, rng, glyphs)
        (out_dir / f"{rng}.pbf").write_bytes(data)
        print(f"{out_dir / rng}.pbf: {len(glyphs)} glyphs, {len(data)} bytes (ascender {ascender}px)")


if __name__ == "__main__":
    main()
