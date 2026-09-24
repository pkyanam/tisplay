"""Command line entry point."""

from __future__ import annotations

import argparse
import array
import fcntl
import os
import select
import shutil
import signal
import sys
import time
import termios
from contextlib import contextmanager, nullcontext

from PIL import Image, ImageOps

from . import __version__
from .capture import DesktopError, Screen, VirtualDisplay, discover_accessible_x11_display, make_controller
from .terminal import KittyRenderer, Terminal, begin_terminal, render_blocks, terminal_size

KEY_SEQUENCES = {
    b"\x1b[A": "up", b"\x1b[B": "down", b"\x1b[C": "right", b"\x1b[D": "left",
    b"\x1b[H": "home", b"\x1b[F": "end", b"\x1b[1~": "home", b"\x1b[4~": "end",
    b"\x1b[5~": "page_up", b"\x1b[6~": "page_down", b"\x1b[2~": "insert",
    b"\x1b[3~": "delete", b"\x1b[Z": "tab",
}


def detect_graphics() -> str:
    term = os.environ.get("TERM", "").lower()
    program = os.environ.get("TERM_PROGRAM", "").lower()
    if ("kitty" in term or "ghostty" in term or "cmux" in term or "wezterm" in term
            or "ghostty" in program or "cmux" in program or "wezterm" in program
            or os.environ.get("KITTY_WINDOW_ID")):
        return "kitty"
    return "ansi"


def kitty_probe() -> bool:
    """Query the terminal protocol, retaining any keys pressed during the probe."""
    query = b"\x1b_Gi=31,s=1,v=1,a=q,t=d,f=24;AAAA\x1b\\\x1b[c"
    os.write(sys.stdout.fileno(), query)
    deadline = time.monotonic() + 0.25
    data = bytearray()
    while time.monotonic() < deadline:
        ready, _, _ = select.select([sys.stdin.fileno()], [], [], max(0, deadline - time.monotonic()))
        if not ready:
            break
        chunk = os.read(sys.stdin.fileno(), 128)
        if not chunk:
            break
        data.extend(chunk)
        if b"\x1b_Gi=31;" in data and b"\x1b\\" in data:
            supported = b"\x1b_Gi=31;OK\x1b\\" in data
            # The probe response is a graphics reply, remove it; preserve user input.
            response_start = data.find(b"\x1b_Gi=31;")
            response_end = data.find(b"\x1b\\", response_start) + 2
            rest = data[:response_start] + data[response_end:]
            if rest:
                os.write(sys.stdin.fileno(), b"")  # Keep the code path explicit; bytes are handled below.
                _PENDING_INPUT.extend(rest)
            return supported
        if b"\x1b[?" in data and b"c" in data:
            _PENDING_INPUT.extend(data)
            return False
    if data:
        _PENDING_INPUT.extend(data)
    return False


_PENDING_INPUT = bytearray()


def resize_for_ansi(frame: Image.Image, cols: int, rows: int) -> Image.Image:
    size = (cols, max(2, rows * 2))
    content = ImageOps.contain(frame, size, Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", size, "black")
    canvas.paste(content, ((size[0] - content.width) // 2, (size[1] - content.height) // 2))
    return canvas


def fit_kitty(frame: Image.Image, cols: int, rows: int, pixel_size: tuple[int, int] | None) -> tuple[Image.Image, tuple[float, float, float, float]]:
    """Letterbox source into Kitty's full-screen placement and return content bounds."""
    if pixel_size and all(pixel_size):
        aspect = pixel_size[0] / pixel_size[1]
    else:
        aspect = cols / max(1, rows * 2)
    width = frame.width
    height = max(1, round(width / aspect))
    canvas = Image.new("RGB", (width, height), "black")
    content = ImageOps.contain(frame, canvas.size, Image.Resampling.LANCZOS)
    left, top = (width - content.width) // 2, (height - content.height) // 2
    canvas.paste(content, (left, top))
    return canvas, (left / width, top / height, content.width / width, content.height / height)


def terminal_pixel_size() -> tuple[int, int] | None:
    try:
        values = array.array("H", [0, 0, 0, 0])
        fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, values, True)
        return (values[2], values[3]) if values[2] and values[3] else None
    except (OSError, AttributeError):
        return None


def _csi_final(data: bytes) -> int | None:
    for i in range(2, len(data)):
        if 0x40 <= data[i] <= 0x7e:
            return i
    return None


def process_input(buffer: bytearray, controller: object, screen: Screen, cols: int, rows: int,
                  graphics: str = "kitty", content_box: tuple[float, float, float, float] = (0, 0, 1, 1),
                  escape_expired: bool = False) -> bool:
    """Consume complete key/mouse sequences. Return false when the user quits."""
    width, height = screen.monitor["width"], screen.monitor["height"]
    while buffer:
        if buffer[0] == 0x1b:
            if len(buffer) == 1:
                if not escape_expired:
                    return True
                del buffer[0]
                controller.key("escape", True)
                controller.key("escape", False)
                continue
            if buffer[1] != ord("["):
                del buffer[0]
                controller.key("escape", True)
                controller.key("escape", False)
                continue
            end = _csi_final(buffer)
            if end is None:
                return True
            seq = bytes(buffer[:end + 1])
            del buffer[:end + 1]
            if seq.startswith(b"\x1b[<"):
                try:
                    code, x, y = map(int, seq[3:-1].split(b";"))
                except (ValueError, TypeError):
                    continue
                if graphics == "ansi":
                    half_y = 2 * (y - 1) + 1
                    nx = (x - 1) / max(1, cols - 1)
                    ny = half_y / max(1, rows * 2 - 1)
                else:
                    nx = (x - 1) / max(1, cols - 1)
                    ny = (y - 1) / max(1, rows - 1)
                left, top, box_width, box_height = content_box
                if not (left <= nx <= left + box_width and top <= ny <= top + box_height):
                    continue
                fx = min(1.0, max(0.0, (nx - left) / max(0.0001, box_width)))
                fy = min(1.0, max(0.0, (ny - top) / max(0.0001, box_height)))
                px = round(screen.monitor["left"] + fx * (width - 1))
                py = round(screen.monitor["top"] + fy * (height - 1))
                if code & 64:
                    name = "wheel_up" if (code & 1) == 0 else "wheel_down"
                    controller.button(name, True, px, py)
                elif code & 32:
                    controller.button("move", False, px, py)
                else:
                    name = {0: "left", 1: "middle", 2: "right"}.get(code & 3)
                    if name:
                        controller.button(name, seq[-1:] == b"M", px, py)
                continue
            if seq in KEY_SEQUENCES:
                name = KEY_SEQUENCES[seq]
                controller.key(name, True)
                controller.key(name, False)
            continue
        value = buffer[0]
        del buffer[0]
        if value in (ord("q"), 0x03):
            return False
        if value == 0x1b:
            return False
        if 1 <= value <= 26:
            name = chr(value + 96)
            controller.key("ctrl", True)
            controller.key(name, True)
            controller.key(name, False)
            controller.key("ctrl", False)
        elif value in (10, 13):
            controller.key("enter", True)
            controller.key("enter", False)
        elif value == 9:
            controller.key("tab", True)
            controller.key("tab", False)
        elif value in (8, 127):
            controller.key("backspace", True)
            controller.key("backspace", False)
        elif value >= 32 and value < 127:
            char = chr(value)
            controller.key(char, True)
            controller.key(char, False)
    return True


@contextmanager
def startup_display(args: argparse.Namespace):
    """Select a reachable local X11 display, falling back to private Xvfb."""
    original_display = os.environ.get("DISPLAY")
    auto_virtual = False
    discovered_display = None
    if sys.platform.startswith("linux") and not args.virtual and not original_display:
        discovered_display = discover_accessible_x11_display()
        if discovered_display:
            os.environ["DISPLAY"] = discovered_display
        else:
            auto_virtual = True
    use_virtual = args.virtual or auto_virtual
    if use_virtual:
        missing = [name for name in ("Xvfb", "openbox") if not shutil.which(name)]
        if not args.command and not shutil.which("xterm"):
            missing.append("xterm")
        if missing:
            raise DesktopError(f"Virtual desktop dependencies missing ({', '.join(missing)}). Install them with: sudo apt install xvfb openbox xterm")
    context = VirtualDisplay(args.width, args.height) if use_virtual else nullcontext()
    try:
        with context as display:
            yield display, use_virtual
    finally:
        if discovered_display:
            if original_display is None:
                os.environ.pop("DISPLAY", None)
            else:
                os.environ["DISPLAY"] = original_display


def run(args: argparse.Namespace) -> int:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise DesktopError("tisplay needs an interactive terminal. Over SSH, connect with `ssh -t host tisplay`.")
    if args.graphics == "kitty":
        graphics = "kitty"
    elif args.graphics == "ansi":
        graphics = "ansi"
    else:
        graphics = detect_graphics()

    with startup_display(args) as (display, use_virtual):
        if use_virtual:
            if args.command:
                display.launch(args.command)
            else:
                display.launch([shutil.which("xterm"), "-geometry", "100x30+40+40"])
        screen = Screen(allow_wayland=use_virtual)
        controller = make_controller()
        kitty_renderer = KittyRenderer()
        pending = bytearray()
        escape_since = None
        old_winch = signal.getsignal(signal.SIGWINCH)
        signal.signal(signal.SIGWINCH, lambda *_: None)
        try:
            with Terminal(sys.stdin.fileno()):
                try:
                    begin_terminal()
                    if args.graphics == "auto" and graphics == "ansi":
                        if kitty_probe():
                            graphics = "kitty"
                    sys.stderr.write("tisplay: q or Ctrl-C quits | keyboard and mouse pass through to the desktop\n")
                    sys.stderr.flush()
                    next_frame = 0.0
                    while True:
                        now = time.monotonic()
                        if now >= next_frame:
                            cols, rows = terminal_size()
                            frame = screen.frame(args.max_width)
                            if graphics == "kitty":
                                frame, content_box = fit_kitty(frame, cols, rows, terminal_pixel_size())
                                data = kitty_renderer.render(frame, cols, rows)
                            else:
                                rendered = resize_for_ansi(frame, cols, rows)
                                rendered_content = ImageOps.contain(frame, (cols, max(2, rows * 2)), Image.Resampling.LANCZOS)
                                content_box = ((cols - rendered_content.width) / (2 * cols),
                                               (rows * 2 - rendered_content.height) / (4 * rows),
                                               rendered_content.width / cols,
                                               rendered_content.height / (2 * rows))
                                data = render_blocks(rendered)
                            os.write(sys.stdout.fileno(), data)
                            next_frame = now + 1.0 / args.fps
                        timeout = max(0, min(0.025, next_frame - time.monotonic()))
                        ready, _, _ = select.select([sys.stdin.fileno()], [], [], timeout)
                        if ready:
                            chunk = os.read(sys.stdin.fileno(), 256)
                            if not chunk:
                                break
                            pending.extend(chunk)
                            if pending == b"\x1b":
                                escape_since = time.monotonic()
                            elif pending:
                                escape_since = None
                            while _PENDING_INPUT:
                                pending.insert(0, _PENDING_INPUT.pop())
                            if not process_input(pending, controller, screen, *terminal_size(), graphics, content_box):
                                break
                        if escape_since is not None and time.monotonic() - escape_since >= 0.15:
                            if not process_input(pending, controller, screen, *terminal_size(), graphics, content_box, True):
                                break
                            escape_since = None
                finally:
                    cleanup = kitty_renderer.close()
                    if cleanup:
                        os.write(sys.stdout.fileno(), cleanup)
        finally:
            controller.close()
            signal.signal(signal.SIGWINCH, old_winch)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="tisplay", description="Interactive desktop stream in a terminal or over SSH.")
    parser.add_argument("--version", action="version", version=f"tisplay {__version__}")
    parser.add_argument("--graphics", choices=("auto", "kitty", "ansi"), default="auto", help="auto-detect Kitty graphics, or force a renderer")
    parser.add_argument("--fps", type=float, default=12, help="maximum refresh rate (default: 12)")
    parser.add_argument("--max-width", type=int, default=1600, help="maximum captured image width (default: 1600)")
    parser.add_argument("--virtual", action="store_true", help="start a private Xvfb desktop on headless Linux")
    parser.add_argument("--width", type=int, default=1280, help="virtual display width (default: 1280)")
    parser.add_argument("--height", type=int, default=800, help="virtual display height (default: 800)")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="command to launch in the virtual desktop (after --)")
    args = parser.parse_args()
    args.command = args.command[1:] if args.command and args.command[0] == "--" else args.command
    if args.fps <= 0 or args.fps > 60:
        parser.error("--fps must be greater than 0 and at most 60")
    if args.max_width < 160 or args.width < 320 or args.height < 240:
        parser.error("capture and virtual display dimensions are too small")
    try:
        raise SystemExit(run(args))
    except DesktopError as exc:
        print(f"tisplay: {exc}", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
