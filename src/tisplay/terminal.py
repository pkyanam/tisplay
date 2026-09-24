"""Terminal input mode and frame renderers."""

from __future__ import annotations

import base64
import errno
import os
import secrets
import sys
import termios
import tty
import zlib
from dataclasses import dataclass

from PIL import Image, ImageOps

STREAM_QUALITY_BITS = {"lossless": 8, "high": 7, "medium": 6, "low": 5}


def write_all(fd: int, data: bytes) -> None:
    """Write a complete terminal frame even when the fd accepts partial data."""
    view = memoryview(data)
    while view:
        try:
            written = os.write(fd, view)
        except InterruptedError:
            continue
        if written <= 0:
            raise OSError(errno.EIO, "terminal write made no progress")
        view = view[written:]


@dataclass
class Terminal:
    fd: int
    old_settings: list | None = None

    def __enter__(self) -> "Terminal":
        self.old_settings = termios.tcgetattr(self.fd)
        tty.setraw(self.fd)
        return self

    def __exit__(self, *_: object) -> None:
        if self.old_settings is not None:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old_settings)
        sys.stdout.write("\x1b[?25h\x1b[?1000l\x1b[?1002l\x1b[?1006l\x1b[0m\x1b[?1049l")
        sys.stdout.flush()


def begin_terminal(mouse: bool = True) -> None:
    sys.stdout.write("\x1b[?1049h\x1b[?25l\x1b[2J\x1b[H")
    if mouse:
        # SGR mouse mode carries unambiguous coordinates and button-up events.
        sys.stdout.write("\x1b[?1000h\x1b[?1002h\x1b[?1006h")
    sys.stdout.flush()


def terminal_size() -> tuple[int, int]:
    size = os.get_terminal_size(sys.stdout.fileno())
    return max(1, size.columns), max(1, size.lines)


def _encode_kitty(image: Image.Image, cols: int, rows: int, image_id: int,
                  raw_rgb: bytes | None = None, compression_level: int = 1) -> bytes:
    """Encode one full-color frame using an explicit Kitty image ID."""
    if image.mode != "RGB":
        image = image.convert("RGB")
    width, height = image.size
    encoded = base64.b64encode(zlib.compress(raw_rgb if raw_rgb is not None else image.tobytes(), level=compression_level))
    # Append chunks directly to the output. Keeping a list of 4 KiB slices and
    # then joining it duplicates the encoded payload and creates hundreds of
    # short-lived bytes objects for every moving frame.
    chunk_count = (len(encoded) + 4095) // 4096
    output = bytearray(b"\x1b[H")
    view = memoryview(encoded)
    for index, start in enumerate(range(0, len(encoded), 4096)):
        more = 1 if index < chunk_count - 1 else 0
        if index == 0:
            header = f"\x1b_Ga=T,f=24,o=z,q=2,i={image_id},s={width},v={height},c={cols},r={rows},C=1,m={more};".encode()
        else:
            header = f"\x1b_Gm={more};".encode()
        output.extend(header)
        output.extend(view[start:start + 4096])
        output.extend(b"\x1b\\")
    return bytes(output)


class KittyRenderer:
    """Keep Kitty frames visible while replacing them over an in-band stream."""

    def __init__(self, first_image_id: int | None = None, compression_level: int = 1,
                 stream_quality: str = "lossless"):
        # IDs are terminal-session global, so avoid a fixed ID that could
        # collide with another application sharing the terminal.
        self._next_id = first_image_id or (secrets.randbelow(0xFFFFFFFF) + 1)
        self._current_id: int | None = None
        self._compression_level = compression_level
        if stream_quality not in STREAM_QUALITY_BITS:
            raise ValueError(f"unknown stream quality: {stream_quality}")
        self._stream_quality = stream_quality
        self._last_frame: tuple[tuple[int, int], int, int, bytes] | None = None

    def render(self, image: Image.Image, cols: int, rows: int) -> bytes:
        """Display the new frame, then delete the previous frame's image."""
        if image.mode != "RGB":
            image = image.convert("RGB")
        source_rgb = image.tobytes()
        signature = (image.size, cols, rows, source_rgb)
        if signature == self._last_frame:
            return b""
        bits = STREAM_QUALITY_BITS[self._stream_quality]
        if bits < 8:
            image = ImageOps.posterize(image, bits)
            raw_rgb = image.tobytes()
        else:
            raw_rgb = source_rgb
        image_id = self._next_id
        self._next_id = 1 if image_id == 0xFFFFFFFF else image_id + 1
        output = bytearray(_encode_kitty(image, cols, rows, image_id, raw_rgb, self._compression_level))
        if self._current_id is not None:
            output.extend(self._delete(self._current_id))
        self._current_id = image_id
        self._last_frame = signature
        return bytes(output)

    def close(self) -> bytes:
        """Delete the last frame, if any, when leaving the renderer."""
        if self._current_id is None:
            return b""
        output = self._delete(self._current_id)
        self._current_id = None
        self._last_frame = None
        return output

    @staticmethod
    def _delete(image_id: int) -> bytes:
        return f"\x1b_Ga=d,d=i,i={image_id},q=2\x1b\\".encode()


def render_kitty(image: Image.Image, cols: int, rows: int) -> bytes:
    """Render one Kitty frame; use :class:`KittyRenderer` for streams."""
    image_id = secrets.randbelow(0xFFFFFFFF) + 1
    return _encode_kitty(image, cols, rows, image_id)


def render_blocks(image: Image.Image) -> bytes:
    """Render one RGB pixel per terminal half-cell using ANSI true color."""
    width, height = image.size
    pix = image.load()
    result: list[str] = ["\x1b[H"]
    for y in range(0, height - 1, 2):
        current = None
        for x in range(width):
            top = pix[x, y]
            bottom = pix[x, y + 1]
            colors = (*top[:3], *bottom[:3])
            if colors != current:
                result.append(f"\x1b[38;2;{top[0]};{top[1]};{top[2]}m\x1b[48;2;{bottom[0]};{bottom[1]};{bottom[2]}m")
                current = colors
            result.append("▀")
        result.append("\x1b[0m\r\n")
    return "".join(result).encode("utf-8")
