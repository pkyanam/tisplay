"""Terminal input mode and frame renderers."""

from __future__ import annotations

import base64
import os
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


def render_kitty(image: Image.Image, cols: int, rows: int) -> bytes:
    """Send full-color pixels compressed inline through Kitty's graphics protocol."""
    image = image.convert("RGB")
    width, height = image.size
    encoded = base64.b64encode(zlib.compress(image.tobytes(), level=1))
    chunks = [encoded[i:i + 4096] for i in range(0, len(encoded), 4096)]
    pieces = [b"\x1b[H", b"\x1b_Ga=d,d=i,i=31\x1b\\"]
    for index, chunk in enumerate(chunks):
        more = 1 if index < len(chunks) - 1 else 0
        if index == 0:
            header = f"\x1b_Ga=T,f=24,o=z,q=1,i=31,s={width},v={height},c={cols},r={rows},C=1,m={more};".encode()
        else:
            header = f"\x1b_Gm={more};".encode()
        pieces.extend((header, chunk, b"\x1b\\"))
    return b"".join(pieces)


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
