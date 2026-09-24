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

from PIL import Image, ImageDraw, ImageOps

from . import __version__
from .capture import DesktopError, Screen, VirtualDisplay, discover_accessible_x11_display, make_controller
from .terminal import KittyRenderer, Terminal, begin_terminal, render_blocks, terminal_size

KEY_SEQUENCES = {
    b"\x1b[A": "up", b"\x1b[B": "down", b"\x1b[C": "right", b"\x1b[D": "left",
    b"\x1b[H": "home", b"\x1b[F": "end", b"\x1b[1~": "home", b"\x1b[4~": "end",
    b"\x1b[5~": "page_up", b"\x1b[6~": "page_down", b"\x1b[2~": "insert",
    b"\x1b[3~": "delete", b"\x1b[Z": "tab",
}

PRESETS = {
    # All Kitty zlib levels are lossless.  Higher compression reduces bytes
    # at a CPU cost; it does not improve visual quality.
    "quality": {"fps": 60.0, "max_width": None, "compression_level": 6, "width": 1920, "height": 1080},
    "balanced": {"fps": 60.0, "max_width": 1600, "compression_level": 1, "width": 1600, "height": 900},
    "fast": {"fps": 30.0, "max_width": 960, "compression_level": 1, "width": 1280, "height": 800},
}


def resolve_performance(preset: str, fps: float | None, max_width: int | None,
                        width: int | None = None, height: int | None = None
                        ) -> tuple[float, int | None, int, int, int]:
    """Resolve preset defaults while letting explicit CLI values take precedence."""
    defaults = PRESETS[preset]
    return (fps if fps is not None else defaults["fps"],
            max_width if max_width is not None else defaults["max_width"],
            defaults["compression_level"],
            width if width is not None else defaults["width"],
            height if height is not None else defaults["height"])


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
    return fit_ansi(frame, cols, rows)[0]


def fit_ansi(frame: Image.Image, cols: int, rows: int) -> tuple[Image.Image, tuple[float, float, float, float]]:
    """Resize once for the ANSI cell grid and return its desktop mapping."""
    size = (cols, max(2, rows * 2))
    content = ImageOps.contain(frame, size, Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", size, "black")
    left, top = (size[0] - content.width) // 2, (size[1] - content.height) // 2
    canvas.paste(content, (left, top))
    box = (left / cols, top / (2 * rows), content.width / cols, content.height / (2 * rows))
    return canvas, box


def fit_kitty(frame: Image.Image, cols: int, rows: int, pixel_size: tuple[int, int] | None) -> tuple[Image.Image, tuple[float, float, float, float]]:
    """Letterbox source into Kitty's full-screen placement and return content bounds."""
    if pixel_size and all(pixel_size):
        aspect = pixel_size[0] / pixel_size[1]
    else:
        aspect = cols / max(1, rows * 2)
    width = frame.width
    height = max(1, round(width / aspect))
    if height == frame.height:
        return frame, (0.0, 0.0, 1.0, 1.0)
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


class TestPatternScreen:
    """Synthetic color bars for checking the terminal graphics path without X11."""

    def __init__(self, width: int = 1024, height: int = 768) -> None:
        self.monitor = {"left": 0, "top": 0, "width": width, "height": height}

    def frame(self, max_width: int | None = 1920) -> Image.Image:
        width, height = self.monitor["width"], self.monitor["height"]
        image = Image.new("RGB", (width, height), "black")
        draw = ImageDraw.Draw(image)
        middle_x, middle_y = width // 2, height // 2
        for box, color in (
            ((0, 0, middle_x, middle_y), (240, 32, 32)),
            ((middle_x, 0, width, middle_y), (32, 220, 64)),
            ((0, middle_y, middle_x, height), (40, 80, 240)),
            ((middle_x, middle_y, width, height), (240, 220, 32)),
        ):
            draw.rectangle(box, fill=color)
        draw.rectangle((width // 4, height // 2 - 28, 3 * width // 4, height // 2 + 28), fill=(0, 0, 0))
        draw.text((width // 2 - 68, height // 2 - 8), "TISPLAY TEST", fill=(255, 255, 255))
        if max_width is not None and width > max_width:
            image = image.resize((max_width, max(1, round(height * max_width / width))), Image.Resampling.LANCZOS)
        return image


class NoopController:
    def key(self, *_: object) -> None:
        pass

    def button(self, *_: object) -> None:
        pass

    def close(self) -> None:
        pass


def _csi_final(data: bytes) -> int | None:
    for i in range(2, len(data)):
        if 0x40 <= data[i] <= 0x7e:
            return i
    return None


def _protocol_key(seq: bytes) -> tuple[str, bool] | None:
    """Decode Kitty CSI-u and xterm modifyOtherKeys key presses/releases."""
    if not (seq.startswith(b"\x1b[") and len(seq) >= 3):
        return None
    final = seq[-1:]
    params = seq[2:-1].split(b";")
    try:
        if final == b"u":  # Kitty keyboard protocol: CSI unicode-key[:shifted];mods:event u
            keycode = int(params[0].split(b":", 1)[0])
            modifier_event = params[1].split(b":", 1) if len(params) > 1 else [b"1"]
            event = int(modifier_event[1]) if len(modifier_event) > 1 else 1
            down = event != 3
        elif final == b"~" and len(params) >= 3 and params[0] == b"27":
            # xterm modifyOtherKeys: CSI 27;modifier;unicode-key~
            keycode = int(params[2])
            down = True
        else:
            return None
    except ValueError:
        return None

    names = {
        9: "tab", 13: "enter", 27: "escape", 127: "backspace",
        57345: "enter", 57414: "enter",
    }
    name = names.get(keycode)
    if name is None and 32 <= keycode < 127:
        name = chr(keycode)
    if name is None:
        return None
    return name, down


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
            protocol_key = _protocol_key(seq)
            if protocol_key:
                name, down = protocol_key
                controller.key(name, down)
                if down:
                    # Most desktops need a complete key press when the
                    # terminal reports only Kitty's default press event.
                    controller.key(name, False)
                continue
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
        if value == 0x1d:
            return False
        if value in (10, 13):
            controller.key("enter", True)
            controller.key("enter", False)
        elif 1 <= value <= 26:
            name = chr(value + 96)
            controller.key("ctrl", True)
            controller.key(name, True)
            controller.key(name, False)
            controller.key("ctrl", False)
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
    requested = getattr(args, "mode", "auto")
    if getattr(args, "virtual", False):
        requested = "virtual"
    if requested == "auto" and sys.platform.startswith("linux") and os.environ.get("WAYLAND_DISPLAY"):
        try:
            from .native import native_available
            requested = "native-existing" if native_available().get("available") else "virtual"
        except ImportError:
            requested = "virtual"
    if requested == "auto" and sys.platform.startswith("linux") and not original_display:
        discovered_display = discover_accessible_x11_display()
        if discovered_display:
            os.environ["DISPLAY"] = discovered_display
        else:
            auto_virtual = True
    if requested == "auto":
        requested = "virtual" if auto_virtual else "native-existing"
    use_virtual = requested == "virtual"
    args._resolved_mode = requested
    if use_virtual:
        missing = [name for name in ("Xvfb", "xauth", "dbus-run-session", "startxfce4", "xfce4-panel", "xprop") if not shutil.which(name)]
        if missing:
            raise DesktopError(f"Full virtual desktop dependencies missing ({', '.join(missing)}). Re-run install.sh to install Xfce, D-Bus, Xvfb, and X11 tools.")
    if use_virtual:
        context = VirtualDisplay(args.width, args.height)
    elif requested == "native-headless" or (requested == "native-existing" and os.environ.get("WAYLAND_DISPLAY")):
        from .native import NativeDisplay
        context = NativeDisplay(require_headless=requested == "native-headless", width=args.width, height=args.height)
    elif requested == "native-existing" and sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or discovered_display):
        raise DesktopError("native desktop unavailable; use --virtual to create an isolated Xfce desktop")
    else:
        context = nullcontext()
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

    if getattr(args, "test_pattern", False):
        display_context = nullcontext((None, False))
    else:
        display_context = startup_display(args)
    with display_context as (display, use_virtual):
        if use_virtual and args.command:
            display.launch(args.command)
        test_pattern = getattr(args, "test_pattern", False)
        if test_pattern:
            screen, controller = TestPatternScreen(args.width, args.height), NoopController()
        elif args._resolved_mode.startswith("native") and display is not None:
            from .native import NativeDisplay, NativeWaylandController
            screen, controller = display, NativeWaylandController()
        else:
            screen, controller = Screen(allow_wayland=use_virtual), make_controller()
        fps, max_width, compression_level, _, _ = resolve_performance(args.preset, args.fps, args.max_width, args.width, args.height)
        kitty_renderer = KittyRenderer(compression_level=compression_level)
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
                    sys.stderr.write("tisplay: Ctrl-] quits | keyboard and mouse pass through to the desktop\n")
                    sys.stderr.flush()
                    next_frame = 0.0
                    while True:
                        now = time.monotonic()
                        if use_virtual and args.command:
                            display.check_command()
                        if now >= next_frame:
                            cols, rows = terminal_size()
                            frame = screen.frame(max_width)
                            if graphics == "kitty":
                                frame, content_box = fit_kitty(frame, cols, rows, terminal_pixel_size())
                                data = kitty_renderer.render(frame, cols, rows)
                            else:
                                rendered, content_box = fit_ansi(frame, cols, rows)
                                data = render_blocks(rendered)
                            if data:
                                os.write(sys.stdout.fileno(), data)
                            next_frame = now + 1.0 / fps
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
    # Keep the original one-command interactive mode intact, while routing the
    # persistent session command tree through its structured client frontend.
    argv = ["--help" if token == "-help" else token for token in sys.argv[1:]]
    if argv == ["--engine-stdio"]:
        from .daemon import run_stdio
        raise SystemExit(run_stdio())
    agent_commands = {"session", "screenshot", "observe", "state", "click", "double-click", "move", "drag", "scroll",
                      "text", "type-text", "key", "press-key", "open-url", "act", "wait", "control", "capabilities", "attach"}
    if argv and argv[0] in ("-h", "--help"):
        from .agent_cli import build_parser
        build_parser().print_help()
        raise SystemExit(0)
    command_index = 0
    while command_index < len(argv):
        if argv[command_index] == "--host" and command_index + 1 < len(argv):
            command_index += 2
        elif argv[command_index] == "--json":
            command_index += 1
        else:
            break
    if command_index < len(argv) and argv[command_index] in agent_commands:
        from .agent_cli import main as agent_main
        raise SystemExit(agent_main(argv))
    before_separator = argv[:argv.index("--")] if "--" in argv else argv
    if any(token in ("-h", "--help") for token in before_separator):
        from .agent_cli import build_parser
        build_parser().print_help()
        raise SystemExit(0)
    parser = argparse.ArgumentParser(prog="tisplay", description="Interactive desktop stream in a terminal or over SSH.")
    parser.add_argument("--version", action="version", version=f"tisplay {__version__}")
    parser.add_argument("--graphics", choices=("auto", "kitty", "ansi"), default="auto", help="auto-detect Kitty graphics, or force a renderer")
    parser.add_argument("--preset", choices=tuple(PRESETS), default="balanced", help="quality keeps source resolution, balanced targets 60 fps, fast reduces capture and bandwidth (default: balanced)")
    parser.add_argument("--fps", type=float, default=None, help="refresh target (default comes from --preset; up to 60)")
    parser.add_argument("--max-width", type=int, default=None, help="capture width cap in pixels (default comes from --preset; quality has no cap)")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--native", dest="mode", action="store_const", const="native-existing", help="require the active desktop; never fall back")
    mode_group.add_argument("--native-headless", dest="mode", action="store_const", const="native-headless", help="require an active native compositor with its configured headless output")
    mode_group.add_argument("--virtual", dest="mode", action="store_const", const="virtual", help="force a private Xfce desktop on Xvfb on Linux")
    parser.set_defaults(mode="auto")
    parser.add_argument("--test-pattern", action="store_true", help="stream synthetic color bars without opening a desktop or injecting input")
    parser.add_argument("--width", type=int, default=None, help="virtual display width (default comes from --preset)")
    parser.add_argument("--height", type=int, default=None, help="virtual display height (default comes from --preset)")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="optional command to launch inside the virtual desktop (after --)")
    args = parser.parse_args(argv)
    args.command = args.command[1:] if args.command and args.command[0] == "--" else args.command
    args.fps, args.max_width, _, args.width, args.height = resolve_performance(
        args.preset, args.fps, args.max_width, args.width, args.height)
    if args.fps is not None and (args.fps <= 0 or args.fps > 60):
        parser.error("--fps must be greater than 0 and at most 60")
    if (args.max_width is not None and args.max_width < 160) or args.width < 320 or args.height < 240:
        parser.error("capture and virtual display dimensions are too small")
    try:
        raise SystemExit(run(args))
    except KeyboardInterrupt:
        print("tisplay: interrupted; cleanup completed.", file=sys.stderr)
        raise SystemExit(130)
    except DesktopError as exc:
        print(f"tisplay: {exc}", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
