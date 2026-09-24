"""Terminal input mode and frame renderers."""

from __future__ import annotations

import base64
import os
import secrets
import sys
import termios
import tty
import zlib
from dataclasses import dataclass

from PIL import Image


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


def _encode_kitty(image: Image.Image, cols: int, rows: int, image_id: int) -> bytes:
    """Encode one full-color frame using an explicit Kitty image ID."""
    image = image.convert("RGB")
    width, height = image.size
    encoded = base64.b64encode(zlib.compress(image.tobytes(), level=1))
    chunks = [encoded[i:i + 4096] for i in range(0, len(encoded), 4096)]
    pieces = [b"\x1b[H"]
    for index, chunk in enumerate(chunks):
        more = 1 if index < len(chunks) - 1 else 0
        if index == 0:
            header = f"\x1b_Ga=T,f=24,o=z,q=2,i={image_id},s={width},v={height},c={cols},r={rows},C=1,m={more};".encode()
        else:
            header = f"\x1b_Gm={more};".encode()
        pieces.extend((header, chunk, b"\x1b\\"))
    return b"".join(pieces)


class KittyRenderer:
    """Keep Kitty frames visible while replacing them over an in-band stream."""

    def __init__(self, first_image_id: int | None = None):
        # IDs are terminal-session global, so avoid a fixed ID that could
        # collide with another application sharing the terminal.
        self._next_id = first_image_id or (secrets.randbelow(0xFFFFFFFF) + 1)
        self._current_id: int | None = None

    def render(self, image: Image.Image, cols: int, rows: int) -> bytes:
        """Display the new frame, then delete the previous frame's image."""
        image_id = self._next_id
        self._next_id = 1 if image_id == 0xFFFFFFFF else image_id + 1
        output = bytearray(_encode_kitty(image, cols, rows, image_id))
        if self._current_id is not None:
            output.extend(self._delete(self._current_id))
        self._current_id = image_id
        return bytes(output)

    def close(self) -> bytes:
        """Delete the last frame, if any, when leaving the renderer."""
        if self._current_id is None:
            return b""
        output = self._delete(self._current_id)
        self._current_id = None
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
