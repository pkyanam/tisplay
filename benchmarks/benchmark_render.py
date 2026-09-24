"""Repeatable local benchmark for representative Kitty and ANSI render work.

Run with ``.venv/bin/python benchmarks/benchmark_render.py``. This isolates
image transformation and terminal encoding; it does not include X11 capture,
SSH delivery, or terminal redraw time.
"""

from __future__ import annotations

import random
import statistics
import time

from PIL import Image, ImageDraw, ImageFont

from tisplay.terminal import KittyRenderer, render_blocks, render_kitty


def detailed_frame(size: tuple[int, int] = (1600, 900), motion: int = 0) -> Image.Image:
    """Make a repeatable desktop-like frame with UI, text, and image detail."""
    width, height = size
    image = Image.new("RGB", size, (236, 239, 243))
    draw = ImageDraw.Draw(image)
    if width > 500:
        draw.rectangle((0, 0, width, 40), fill=(32, 37, 45))
        draw.rectangle((0, 40, 230, height), fill=(45, 52, 63))
        draw.rectangle((230, 40, width, 92), fill=(248, 249, 251))
    font = ImageFont.load_default()
    for row in range(18 if width > 500 else 2):
        y = 62 + row * 34
        if y + 20 >= height:
            break
        draw.rounded_rectangle((18, y, 205, y + 24), 4, fill=(64 + row * 2, 87, 112))
        draw.text((250, y), f"Workspace {row + 1:02d}  ·  settings, status, and recent activity", font=font, fill=(42, 49, 59))
        draw.line((250, y + 25, width - 24, y + 25), fill=(218, 222, 228))
    # A photographic-detail panel gives the compressor a realistic mix of
    # smooth regions, text edges, and high-entropy pixels without network IO.
    panel = ((width // 2, height // 3, width - 24, height - 24)
             if width > 500 else (width // 3, height // 3, width - 1, height - 1))
    pw, ph = panel[2] - panel[0], panel[3] - panel[1]
    rng = random.Random(42)
    texture = Image.frombytes("RGB", (pw, ph), rng.randbytes(pw * ph * 3))
    texture = texture.filter(__import__("PIL.ImageFilter", fromlist=["ImageFilter"]).GaussianBlur(2))
    image.paste(texture, (panel[0], panel[1]))
    draw = ImageDraw.Draw(image)
    draw.rectangle((panel[0] + 12, panel[1] + 12, panel[0] + 232, panel[1] + 60), fill=(250, 250, 252))
    draw.text((panel[0] + 24, panel[1] + 28), "Live monitor", font=font, fill=(31, 43, 57))
    # Motion is deterministic and affects a small foreground region.
    x = panel[0] + min(300, max(1, pw - 37)) + (motion * 37) % max(1, pw - min(337, pw))
    y = panel[1] + min(100, max(1, ph - 36))
    draw.ellipse((x, y, x + min(36, pw), y + min(36, ph)), fill=(245, 91, 64), outline="white", width=3)
    return image


def timed(callable_, count: int = 7) -> tuple[float, int]:
    samples: list[float] = []
    output_size = 0
    for index in range(count):
        start = time.perf_counter()
        output = callable_(index)
        samples.append(time.perf_counter() - start)
        output_size = len(output)
    return statistics.median(samples), output_size


def main() -> None:
    frames = [detailed_frame(motion=i) for i in range(8)]
    print(f"Desktop-like test frames: {frames[0].width}x{frames[0].height}, motion changes each frame")
    for name, quality, level in (
        ("lossless zlib-1", "lossless", 1),
        ("high 7-bit zlib-1", "high", 1),
        ("medium 6-bit zlib-1", "medium", 1),
        ("low 5-bit zlib-1", "low", 1),
    ):
        renderer = KittyRenderer(first_image_id=100, compression_level=level, stream_quality=quality)
        seconds, size = timed(lambda i: renderer.render(frames[i], 100, 35))
        print(f"{name}: {seconds * 1000:.2f} ms/frame, {size / 1024:.1f} KiB/frame, "
              f"{size * 8 / seconds / 1e6:.1f} Mbit/s encode throughput; "
              f"{size * 8 * 60 / 1e6:.1f} Mbit/s if every frame is sent at 60 fps")

    static_renderer = KittyRenderer(first_image_id=300)
    static_renderer.render(frames[0], 100, 35)
    seconds, size = timed(lambda _i: static_renderer.render(frames[0], 100, 35))
    print(f"unchanged Kitty frame: {seconds * 1000:.2f} ms/frame comparison, {size} output bytes")
    seconds, size = timed(lambda _i: render_kitty(frames[0], 100, 35))
    print(f"previous always-encode behavior on same static frame: {seconds * 1000:.2f} ms/frame, {size / 1024:.1f} KiB/frame")

    ansi_frame = detailed_frame((100, 100))
    seconds, size = timed(lambda _i: render_blocks(ansi_frame))
    print(f"ANSI half-block 100x100 sample grid: {seconds * 1000:.2f} ms/frame, {size / 1024:.1f} KiB/frame")


if __name__ == "__main__":
    main()
