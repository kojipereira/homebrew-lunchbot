#!/usr/bin/env python3
"""Compose assets/lunchbot.icon into src/lunchbot/resources/Lunchbot.icns.

Lunchbot.app is hand-rolled (see lunchbot/appbundle.py), so nothing compiles the
Icon Composer bundle for us the way Xcode would. This script does that job once,
on a developer machine, and the resulting .icns is committed — installs must not
need a rasterizer.

    pip install pillow cairosvg
    ./tools/build-icon.py            # rewrites the .icns (+ --preview for a PNG)

It reads icon.json the way Icon Composer does: the gradient `fill` paints the
whole 1024pt canvas, `groups[].layers` stack bottom-to-top over it with the
group's soft shadow behind them, and the canvas is masked to the squircle. The
macOS wrapper (824pt of art centred in a 1024pt canvas, plus the ambient
shadow) matches Apple's app-icon template.
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
from io import BytesIO
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "assets" / "lunchbot.icon"
DEST = ROOT / "src" / "lunchbot" / "resources" / "Lunchbot.icns"

CANVAS = 1024          # Icon Composer authoring canvas
CONTENT = 824          # Apple's macOS art box inside a 1024pt canvas
SQUIRCLE_EXPONENT = 5  # superellipse that approximates Apple's continuous corner
SUPERSAMPLE = 4

# OSType → pixel size. The modern PNG-based set: every size macOS asks for,
# including the @2x variants that share a pixel size with a 1x slot.
ICNS_ENTRIES = [
    ("icp4", 16), ("icp5", 32), ("ic07", 128), ("ic08", 256), ("ic09", 512),
    ("ic10", 1024), ("ic11", 32), ("ic12", 64), ("ic13", 256), ("ic14", 512),
]


# ---- colour ----------------------------------------------------------------

def _srgb_to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _linear_to_srgb(c: float) -> float:
    c = min(max(c, 0.0), 1.0)
    return c * 12.92 if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055


# Display P3 → sRGB, both gamma-encoded with the sRGB transfer function (that's
# how CSS/Icon Composer spell `display-p3`). P3 is the wider gamut, so saturated
# values clip; Lunchbot's teal is well inside sRGB and survives intact.
_P3_TO_SRGB = (
    (1.2249401762805756, -0.2249401762805754, 0.0),
    (-0.04205697879954574, 1.0420569787995457, 0.0),
    (-0.019636226147153617, -0.07863715131193373, 1.0982733774590873),
)


def parse_color(spec: str) -> tuple[int, int, int, int]:
    """`display-p3:r,g,b,a` (or `srgb:...`) → an 8-bit sRGB tuple."""
    space, _, values = spec.partition(":")
    r, g, b, a = (float(v) for v in values.split(","))
    if space == "display-p3":
        lin = [_srgb_to_linear(c) for c in (r, g, b)]
        r, g, b = (
            _linear_to_srgb(sum(row[i] * lin[i] for i in range(3)))
            for row in _P3_TO_SRGB
        )
    elif space not in ("srgb", "extended-srgb", ""):
        raise SystemExit(f"unsupported colour space: {space!r}")
    return tuple(round(min(max(c, 0.0), 1.0) * 255) for c in (r, g, b, a))


# ---- composition -----------------------------------------------------------

def squircle_mask(size: int) -> "Image.Image":
    """An antialiased superellipse mask filling `size` x `size`."""
    import math

    from PIL import Image, ImageDraw

    hi = size * SUPERSAMPLE
    a = hi / 2
    n = 2 / SQUIRCLE_EXPONENT
    points = []
    for step in range(1440):
        t = 2 * math.pi * step / 1440
        cos_t, sin_t = math.cos(t), math.sin(t)
        points.append((
            a + math.copysign(abs(cos_t) ** n, cos_t) * a,
            a + math.copysign(abs(sin_t) ** n, sin_t) * a,
        ))
    mask = Image.new("L", (hi, hi), 0)
    ImageDraw.Draw(mask).polygon(points, fill=255)
    return mask.resize((size, size), Image.LANCZOS)


def vertical_gradient(size: int, top: tuple, bottom: tuple) -> "Image.Image":
    from PIL import Image

    strip = Image.new("RGBA", (1, size))
    px = strip.load()
    for y in range(size):
        f = y / max(size - 1, 1)
        px[0, y] = tuple(round(top[i] + (bottom[i] - top[i]) * f) for i in range(4))
    return strip.resize((size, size), Image.NEAREST)


def render_svg(path: Path, size: int) -> "Image.Image":
    import cairosvg
    from PIL import Image

    png = cairosvg.svg2png(url=str(path), output_width=size, output_height=size)
    return Image.open(BytesIO(png)).convert("RGBA")


def compose(source: Path) -> "Image.Image":
    """Render the .icon bundle to a 1024x1024 macOS-ready RGBA image."""
    from PIL import Image, ImageFilter

    spec = json.loads((source / "icon.json").read_text())
    fill = spec.get("fill", {})
    if "linear-gradient" not in fill:
        raise SystemExit("icon.json: expected a linear-gradient fill")
    stops = [parse_color(c) for c in fill["linear-gradient"]]

    canvas = vertical_gradient(CANVAS, stops[0], stops[-1])

    for group in spec.get("groups", []):
        art = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
        for layer in group.get("layers", []):
            svg = source / "Assets" / layer["image-name"]
            if not svg.exists():
                raise SystemExit(f"missing layer asset: {svg}")
            art.alpha_composite(render_svg(svg, CANVAS))

        shadow_spec = group.get("shadow")
        if shadow_spec:
            opacity = float(shadow_spec.get("opacity", 0.5))
            shadow = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
            shadow.putalpha(
                art.getchannel("A")
                .point(lambda v, o=opacity: round(v * o * 0.7))
                .filter(ImageFilter.GaussianBlur(CANVAS * 0.012))
            )
            canvas.alpha_composite(shadow, (0, round(CANVAS * 0.008)))

        canvas.alpha_composite(art)

    canvas.putalpha(squircle_mask(CANVAS))

    # macOS template: shrink the masked art to 824pt, centre it, and lay the
    # ambient shadow underneath so the icon sits at the same visual weight as
    # every other app in the Dock.
    inset = (CANVAS - CONTENT) // 2
    art = canvas.resize((CONTENT, CONTENT), Image.LANCZOS)
    out = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    shadow = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    shadow.paste((0, 0, 0, 255), (inset, inset, inset + CONTENT, inset + CONTENT),
                 art.getchannel("A"))
    shadow.putalpha(shadow.getchannel("A").point(lambda v: round(v * 0.28))
                    .filter(ImageFilter.GaussianBlur(CANVAS * 0.013)))
    out.alpha_composite(shadow, (0, round(CANVAS * 0.008)))
    out.alpha_composite(art, (inset, inset))
    return out


# ---- .icns -----------------------------------------------------------------

def build_icns(image: "Image.Image") -> bytes:
    from PIL import Image

    chunks = []
    for ostype, size in ICNS_ENTRIES:
        buf = BytesIO()
        scaled = image if size == image.width else image.resize((size, size), Image.LANCZOS)
        # optimize=True keeps the committed binary small and byte-stable.
        scaled.save(buf, format="PNG", optimize=True)
        data = buf.getvalue()
        chunks.append(ostype.encode("ascii") + struct.pack(">I", len(data) + 8) + data)
    body = b"".join(chunks)
    return b"icns" + struct.pack(">I", len(body) + 8) + body


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", type=Path, default=SOURCE, help="path to the .icon bundle")
    ap.add_argument("--out", type=Path, default=DEST, help="path to the .icns to write")
    ap.add_argument("--preview", type=Path, help="also write a flattened PNG here")
    args = ap.parse_args()

    try:
        import cairosvg  # noqa: F401
        from PIL import Image  # noqa: F401
    except ImportError:
        print("this script needs pillow and cairosvg:  pip install pillow cairosvg",
              file=sys.stderr)
        return 1

    if not (args.source / "icon.json").exists():
        print(f"no icon.json under {args.source}", file=sys.stderr)
        return 1

    image = compose(args.source)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(build_icns(image))
    print(f"wrote {args.out} ({args.out.stat().st_size:,} bytes)")
    if args.preview:
        image.save(args.preview)
        print(f"wrote {args.preview}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
