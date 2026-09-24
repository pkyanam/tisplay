"""Command line interface for persistent, scriptable tisplay sessions.

The original interactive invocation remains in :mod:`tisplay.cli`; this
module adds a structured command surface over the same local/SSH session API.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
import sys
import time
from pathlib import Path
from typing import Any, Callable, Sequence

from . import __version__


class HelpFormatter(argparse.RawDescriptionHelpFormatter):
    """Keep examples readable while aligning option descriptions cleanly."""


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(2, f"tisplay: error: {message}\nRun `tisplay --help` for usage.\n")


EPILOG = """\
EXAMPLES
  Human terminal             tisplay --native --preset balanced
  Reconnect after disconnect tisplay attach --session SESSION --graphics kitty --fps 60
  Start a named desktop      tisplay session start --mode virtual --name work --json
  Start one on a Pi          tisplay --host pi session start --mode native-headless --name work --json
  Inspect / save a frame     tisplay screenshot --session SESSION --out frame.png
  Control it                 tisplay click --session SESSION 640 360
  Attach to that live desktop tisplay attach --session SESSION
  Watch without control       tisplay attach --session SESSION --view-only

AGENT WORKFLOW
  Start once, then use session status/list, screenshot, click, text, key, and
  wait changed across separate commands. `--json` returns one JSON object on
  stdout; diagnostics go to stderr. Screenshot files are written on this
  machine, including when the desktop is remote. `act` accepts an ordered JSON
  action list for low-latency multi-step automation.

  tisplay act --session SESSION --actions '[{"type":"click","x":640,"y":360},{"type":"text","text":"hello"}]' --json

CUA DRIVER (OPTIONAL, PINNED LINUX INTEGRATION)
  tisplay cua install
  tisplay --host pi cua install
  tisplay --host pi cua tools
  tisplay --host pi cua call --session SESSION get_window_state --args '{"pid":123,"window_id":456}' --out state.png
  tisplay --host pi cua call --session SESSION click --args '{"pid":123,"window_id":456,"element_token":"...","snapshot_id":"..."}'
  tisplay --host pi cua mcp --session SESSION

  The optional Cua Driver runs inside the selected session and persists until
  that Tisplay session stops. `mcp` is a persistent stdio stream. Linux
  semantic accessibility is experimental and depends on the desktop's AT-SPI
  support; `--out` saves returned screenshots locally, and default output omits
  image payloads. Use `--raw` only when full driver output is needed.

UPDATES
  tisplay update --check
  tisplay --host pi update --check
  tisplay update --rollback

PERFORMANCE
  `--preset quality|balanced|fast` controls capture defaults; explicit size or
  frame-rate options override the preset. `--stream-quality` trades image color
  precision for smaller Kitty frames; its default is lossless. `attach` needs an interactive TTY.
  Remote control requires SSH access and Python on the remote host. A virtual
  desktop requires the Linux Xvfb/Xfce dependencies installed by install.sh.

INTERACTIVE MODE
  `tisplay --native --preset balanced` opens a terminal viewer on the active desktop.
  Options for that mode: --graphics auto|kitty|ansi, --preset quality|balanced|
  fast, --fps N, --max-width PIXELS, --stream-quality lossless|high|medium|low,
  --native, --native-headless, --virtual,
  --test-pattern, --width PIXELS,
  and --height PIXELS. Add `-- command [args...]` to launch a program inside a
  private virtual desktop. Use `Ctrl-]` to disconnect from an attached session.
"""


def _common(parser: argparse.ArgumentParser, *, input_owner: bool = False) -> None:
    parser.add_argument("--host", default=argparse.SUPPRESS, help="SSH destination (for example user@pi); omit for a local session")
    parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="print one machine-readable JSON object")
    if input_owner:
        parser.add_argument("--owner", help="input-control lease owner")


def _session(parser: argparse.ArgumentParser, required: bool = True) -> None:
    parser.add_argument("--session", required=required, help="session ID returned by `session start`")


def build_parser() -> Parser:
    parser = Parser(
        prog="tisplay", description="Control persistent local or remote desktops.",
        formatter_class=HelpFormatter, epilog=EPILOG,
    )
    parser.add_argument("--host", default=None, help="SSH destination for remote sessions (for example user@pi)")
    parser.add_argument("--json", action="store_true", default=False, help="print one machine-readable JSON object")
    parser.add_argument("--version", action="version", version=f"tisplay {__version__}")
    parser.add_argument("--skill", action="store_true", help="print the bundled agent skill (offline; no desktop required)")
    roots = parser.add_subparsers(dest="agent_command", metavar="COMMAND")

    session = roots.add_parser("session", help="start and manage persistent desktop sessions", formatter_class=HelpFormatter)
    sessions = session.add_subparsers(dest="session_command", required=True, metavar="ACTION")
    start = sessions.add_parser("start", help="start a desktop session", formatter_class=HelpFormatter)
    _common(start)
    start.add_argument("--name", help="optional label shown in session listings")
    modes = start.add_mutually_exclusive_group()
    modes.add_argument("--mode", choices=("auto", "native-existing", "native-headless", "virtual"), default="auto", help="desktop provider; explicit native modes never fall back")
    modes.add_argument("--native", dest="mode", action="store_const", const="native-existing", help="require an existing native desktop")
    modes.add_argument("--native-headless", dest="mode", action="store_const", const="native-headless", help="require the compositor's configured headless output")
    modes.add_argument("--virtual", dest="mode", action="store_const", const="virtual", help="create a private Xvfb/Xfce desktop")
    start.set_defaults(mode="auto")
    start.add_argument("--width", type=int, help="virtual display width in pixels")
    start.add_argument("--height", type=int, help="virtual display height in pixels")
    start.add_argument("--preset", choices=("quality", "balanced", "fast"), default="balanced")
    start.add_argument("command", nargs=argparse.REMAINDER, help="optional command to launch inside the desktop")
    for verb in ("list", "status"):
        p = sessions.add_parser(verb, help=f"{verb} desktop sessions", formatter_class=HelpFormatter)
        _common(p)
        if verb == "status":
            _session(p)
    resize = sessions.add_parser("resize", help="resize a virtual desktop", formatter_class=HelpFormatter)
    _common(resize); _session(resize)
    resize.add_argument("width", type=int); resize.add_argument("height", type=int)
    stop = sessions.add_parser("stop", help="stop a desktop session", formatter_class=HelpFormatter)
    _common(stop); _session(stop)

    screenshot = roots.add_parser("screenshot", aliases=("observe", "state"), help="save a PNG snapshot of a session", formatter_class=HelpFormatter)
    _common(screenshot); _session(screenshot)
    screenshot.add_argument("--out", help="local output path (default: tisplay-SESSION.png)")
    screenshot.add_argument("--scale", type=float, help="scale factor from 0.1 to 1.0")
    screenshot.add_argument("--max-width", type=int, help="maximum output width")
    screenshot.add_argument("--region", nargs=4, type=int, metavar=("X", "Y", "WIDTH", "HEIGHT"), help="crop to a desktop pixel region")

    for verb, help_text in (("move", "move the pointer"), ("click", "click a desktop coordinate"),
                            ("double-click", "double-click a desktop coordinate"), ("drag", "drag between two coordinates"),
                            ("scroll", "scroll at a desktop coordinate")):
        p = roots.add_parser(verb, help=help_text, formatter_class=HelpFormatter)
        _common(p, input_owner=True); _session(p)
        if verb in ("move", "click", "double-click"):
            p.add_argument("pos_x", type=int, nargs="?", help="desktop x coordinate (or use --x)")
            p.add_argument("pos_y", type=int, nargs="?", help="desktop y coordinate (or use --y)")
            p.add_argument("--x", dest="flag_x", type=int, help="desktop x coordinate")
            p.add_argument("--y", dest="flag_y", type=int, help="desktop y coordinate")
            if verb in ("click", "double-click"):
                p.add_argument("--button", choices=("left", "middle", "right"), default="left", help="mouse button (default: left)")
                p.add_argument("--click-count", type=int, choices=(1, 2), default=2 if verb == "double-click" else 1, help="click count: 1 or 2")
            p.add_argument("--capture-id", help="ground output-image coordinates in a recent screenshot token")
        elif verb == "drag":
            p.add_argument("x", type=int); p.add_argument("y", type=int)
            p.add_argument("to_x", type=int); p.add_argument("to_y", type=int)
            p.add_argument("--capture-id", help="ground output-image coordinates in a recent screenshot token")
        else:
            p.add_argument("direction", choices=("up", "down", "left", "right"))
            p.add_argument("--x", type=int, default=0); p.add_argument("--y", type=int, default=0)
            p.add_argument("--amount", type=int, default=1)
            p.add_argument("--capture-id", help="ground output-image coordinates in a recent screenshot token")

    for verb, alias, help_text in (("text", "type-text", "type text into the desktop"), ("key", "press-key", "press a key or chord")):
        p = roots.add_parser(verb, aliases=(alias,), help=help_text, formatter_class=HelpFormatter)
        _common(p, input_owner=True); _session(p)
        if verb == "text": p.add_argument("value", help="text to type")
        else: p.add_argument("value", help="key name or chord, for example CTRL+L or ENTER")

    act = roots.add_parser("act", help="run an ordered list of actions", formatter_class=HelpFormatter)
    _common(act, input_owner=True); _session(act)
    source = act.add_mutually_exclusive_group(required=True)
    source.add_argument("--actions", help="JSON array of actions or @path to a JSON file")
    source.add_argument("--file", help="read the JSON action array from a file (use - for stdin)")
    errors = act.add_mutually_exclusive_group()
    errors.add_argument("--stop-on-error", dest="stop_on_error", action="store_true", help="stop at the first failed action (default)")
    errors.add_argument("--continue-on-error", dest="stop_on_error", action="store_false", help="record action errors and continue the batch")
    act.set_defaults(stop_on_error=True)
    act.add_argument("--screenshot", action="store_true", help="capture one PNG after all actions")
    act.add_argument("--out", help="screenshot output path (requires --screenshot)")
    act.add_argument("--verbose", action="store_true", help="include per-action results in output")

    open_url = roots.add_parser("open-url", help="open an HTTP or HTTPS URL in the session desktop", formatter_class=HelpFormatter)
    _common(open_url, input_owner=True); _session(open_url)
    open_url.add_argument("url", help="HTTP or HTTPS URL to open in the session desktop")

    wait = roots.add_parser("wait", help="wait until the desktop frame changes", formatter_class=HelpFormatter)
    _common(wait); _session(wait)
    wait.add_argument("changed", nargs="?", choices=("changed",), default="changed")
    wait.add_argument("--timeout", type=float, default=30.0, help="maximum wait in seconds")
    wait.add_argument("--interval", type=float, default=0.25, help="polling interval in seconds")

    control = roots.add_parser("control", help="inspect or acquire exclusive input control", formatter_class=HelpFormatter)
    controls = control.add_subparsers(dest="control_command", required=True, metavar="ACTION")
    for verb in ("acquire", "release", "status"):
        p = controls.add_parser(verb, help=f"{verb} input control", formatter_class=HelpFormatter)
        _common(p, input_owner=True); _session(p)
        if verb == "acquire": p.add_argument("--lease-seconds", type=int, default=60, help="lease duration from 1 to 3600 seconds")

    capabilities = roots.add_parser("capabilities", help="show backend and action capabilities", formatter_class=HelpFormatter)
    _common(capabilities)
    _session(capabilities, required=False)

    update = roots.add_parser("update", help="update tisplay safely", formatter_class=HelpFormatter)
    _common(update)
    update_mode = update.add_mutually_exclusive_group()
    update_mode.add_argument("--check", action="store_true", help="check for a newer release without installing")
    update_mode.add_argument("--rollback", action="store_true", help="restore the previous installed release")

    cua = roots.add_parser("cua", help="use the optional Cua Driver semantic computer-use tools", formatter_class=HelpFormatter)
    cua_sub = cua.add_subparsers(dest="cua_command", required=True, metavar="ACTION")
    for verb, desc in (("install", "install the pinned optional Cua Driver"), ("doctor", "check Cua Driver runtime readiness"),
                       ("tools", "list Cua Driver tools")):
        p = cua_sub.add_parser(verb, help=desc, formatter_class=HelpFormatter)
        _common(p)
        if verb == "doctor": _session(p, required=False)
    describe = cua_sub.add_parser("describe", help="show a tool schema", formatter_class=HelpFormatter)
    _common(describe); describe.add_argument("tool")
    call = cua_sub.add_parser("call", help="call a Cua tool in a persistent session runtime", formatter_class=HelpFormatter)
    _common(call); _session(call); call.add_argument("tool")
    call.add_argument("--args", default="{}", help="JSON object of tool arguments")
    call.add_argument("--out", help="save returned screenshot image locally; base64 is omitted from output")
    call.add_argument("--raw", action="store_true", help="include unmodified tool output (may be large)")
    mcp = cua_sub.add_parser("mcp", help="run the session-scoped Cua MCP stdio server", formatter_class=HelpFormatter)
    _common(mcp); _session(mcp)

    attach = roots.add_parser("attach", help="interactively view and control a live session", formatter_class=HelpFormatter)
    _common(attach, input_owner=True); _session(attach)
    attach.add_argument("--graphics", choices=("auto", "kitty", "ansi"), default="auto")
    attach.add_argument("--view-only", action="store_true", help="watch without acquiring input control")
    attach.add_argument("--fps", type=float, default=15.0, help="remote capture refresh target (default: 15)")
    attach.add_argument("--max-width", type=int, default=1600, help="cap each remote frame width (default: 1600)")
    attach.add_argument("--stream-quality", choices=("lossless", "high", "medium", "low"), default="lossless",
                        help="Kitty color precision; lower quality can reduce bandwidth (default: lossless)")
    return parser


def _json_actions(value: str | None, file: str | None) -> list[dict[str, Any]]:
    if file == "-":
        raw = sys.stdin.read()
    elif file:
        raw = Path(file).read_text(encoding="utf-8")
    elif value and value.startswith("@"):
        raw = Path(value[1:]).read_text(encoding="utf-8")
    else:
        raw = value or "[]"
    parsed = json.loads(raw)
    if not isinstance(parsed, list) or not all(isinstance(item, dict) for item in parsed):
        raise ValueError("actions must be a JSON array of objects")
    return parsed


def _client(host: str | None):
    from .client import SessionClient
    return SessionClient(host=host)


def _emit(value: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    elif isinstance(value, dict):
        print(json.dumps(value, ensure_ascii=False, indent=2))
    else:
        print(value)


def _capture(client: Any, session_id: str, args: argparse.Namespace) -> dict[str, Any]:
    kw: dict[str, Any] = {}
    if getattr(args, "scale", None) is not None: kw["scale"] = args.scale
    if getattr(args, "max_width", None) is not None: kw["max_width"] = args.max_width
    if getattr(args, "region", None):
        x, y, width, height = args.region
        kw["region"] = {"x": x, "y": y, "width": width, "height": height}
    result = client.capture(session_id, **kw)
    png = base64.b64decode(result.pop("png"))
    filename = getattr(args, "out", None) or f"tisplay-{session_id}.png"
    path = Path(filename).expanduser()
    path.write_bytes(png)
    result["path"] = str(path.resolve())
    result["bytes"] = len(png)
    return result


def _content_id(capture: dict[str, Any]) -> str:
    content_id = capture.get("frame", {}).get("content_id")
    if content_id is not None:
        return str(content_id)
    return hashlib.sha256(base64.b64decode(capture["png"])).hexdigest()


def _actions_for(args: argparse.Namespace) -> list[dict[str, Any]]:
    if args.agent_command == "act":
        return _json_actions(args.actions, args.file)
    command = args.agent_command
    if command in ("move", "click", "double-click"):
        positional = (getattr(args, "pos_x", None), getattr(args, "pos_y", None))
        flags = (getattr(args, "flag_x", None), getattr(args, "flag_y", None))
        has_pos, has_flag = any(value is not None for value in positional), any(value is not None for value in flags)
        if has_pos and has_flag:
            raise ValueError("choose either positional coordinates or both --x and --y; do not mix forms")
        coords = flags if has_flag else positional
        if coords[0] is None or coords[1] is None:
            raise ValueError("provide both x and y coordinates, either positionally or with --x and --y")
        typ = command.replace("-", "_")
        if command == "click" and args.click_count == 2: typ = "double_click"
        action: dict[str, Any] = {"type": typ, "x": coords[0], "y": coords[1]}
        if command in ("click", "double-click"): action["button"] = args.button
    elif command == "drag":
            action = {"type": "drag", "from_x": args.x, "from_y": args.y, "to_x": args.to_x, "to_y": args.to_y}
    elif command == "scroll":
            action = {"type": "scroll", "x": args.x, "y": args.y,
                      "delta_y": (args.amount if args.direction == "up" else -args.amount) if args.direction in ("up", "down") else 0,
                      "delta_x": (-args.amount if args.direction == "left" else args.amount) if args.direction in ("left", "right") else 0}
    elif command in ("text", "type-text"):
        action = {"type": "text", "text": args.value}
    elif command in ("key", "press-key"):
        names = [part.strip() for part in args.value.split("+") if part.strip()]
        chord = len(names) > 1
        if chord:
            names = [part.lower() if part.lower() in {"ctrl", "control", "shift", "alt", "meta", "cmd", "super"} or (len(part) == 1 and part.isascii() and part.isalpha()) else part for part in names]
        action = {"type": "key", "name": names}
    else:
        raise ValueError(f"unsupported action command: {command}")
    token = getattr(args, "capture_id", None)
    if token and command in ("move", "click", "double-click", "drag", "scroll"):
        action["capture_id"] = token
    return [action]


def _attach(client: Any, args: argparse.Namespace) -> int:
    """Attach by polling PNG frames and forwarding terminal input events."""
    import select
    import signal
    import termios
    import tty
    from PIL import Image
    from . import cli as interactive
    from .terminal import KittyRenderer, Terminal, begin_terminal, render_blocks, terminal_size, write_all

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ValueError("attach needs an interactive terminal")
    status = client.status(args.session)
    mon = status.get("monitor") or {"left": 0, "top": 0, "width": status.get("width", 1920), "height": status.get("height", 1080)}
    screen = type("RemoteScreen", (), {"monitor": mon})()
    graphics = interactive.detect_graphics() if args.graphics == "auto" else args.graphics
    renderer = KittyRenderer(stream_quality=args.stream_quality)
    controller = _AttachController(client, args.session, readonly=args.view_only)
    controller.owner = args.owner or f"tisplay-attach-{os.getpid()}-{secrets.token_hex(4)}"
    old_winch = signal.getsignal(signal.SIGWINCH)
    signal.signal(signal.SIGWINCH, lambda *_: None)
    pending = bytearray()
    escape_since = None
    frame_digest = None
    interval = 1.0 / max(1.0, min(60.0, args.fps))
    next_frame = 0.0
    content_box = (0.0, 0.0, 1.0, 1.0)
    leased = False
    try:
        renew_at = 0.0

        def renew_control() -> None:
            nonlocal renew_at
            # Check immediately before input as well as from the render loop.
            # Slow capture or terminal output can block that loop past the
            # lease deadline; renew only when the shared deadline is due.
            if time.monotonic() < renew_at:
                return
            client.control(args.session, action="acquire", owner=controller.owner, lease_seconds=60)
            renew_at = time.monotonic() + 20

        if not args.view_only:
            renew_control()
            leased = True
            controller.before_flush = renew_control
        with Terminal(sys.stdin.fileno()):
            begin_terminal()
            sys.stderr.write("tisplay attach: Ctrl-] disconnects | " + ("view only" if args.view_only else "input controls the live desktop") + "\n")
            while True:
                now = time.monotonic()
                if not args.view_only and now >= renew_at:
                    renew_control()
                if now >= next_frame:
                    result = client.capture(args.session, max_width=args.max_width, register_capture=False)
                    monitor = result.get("frame", {}).get("monitor")
                    if monitor:
                        screen.monitor = monitor
                    controller.frame_id = result.get("frame", {}).get("frame_id")
                    controller.capture_id = result.get("frame", {}).get("capture_id")
                    current_digest = _content_id(result)
                    if current_digest != frame_digest:
                        png_bytes = base64.b64decode(result["png"])
                        frame = Image.open(__import__("io").BytesIO(png_bytes)).convert("RGB")
                        cols, rows = terminal_size()
                        if graphics == "kitty":
                            frame, content_box = interactive.fit_kitty(frame, cols, rows, interactive.terminal_pixel_size())
                            output = renderer.render(frame, cols, rows)
                        else:
                            rendered, content_box = interactive.fit_ansi(frame, cols, rows)
                            output = render_blocks(rendered)
                        if output: write_all(sys.stdout.fileno(), output)
                        frame_digest = current_digest
                    next_frame = now + interval
                timeout = max(0, min(0.025, next_frame - time.monotonic()))
                ready, _, _ = select.select([sys.stdin.fileno()], [], [], timeout)
                if ready:
                    chunk = os.read(sys.stdin.fileno(), 256)
                    if not chunk: break
                    pending.extend(chunk)
                    if pending == b"\x1b": escape_since = time.monotonic()
                    elif pending: escape_since = None
                    while interactive._PENDING_INPUT: pending.insert(0, interactive._PENDING_INPUT.pop())
                    if not interactive.process_input(pending, controller, screen, *terminal_size(), graphics, content_box):
                        break
                    controller.flush()
                if escape_since is not None and time.monotonic() - escape_since >= 0.15:
                    if not interactive.process_input(pending, controller, screen, *terminal_size(), graphics, content_box, True): break
                    controller.flush()
                    escape_since = None
    finally:
        try:
            if not args.view_only:
                # Cleanup may release keys only; it must never reacquire
                # control or retry queued user actions.
                controller.before_flush = None
                controller.discard_pending()
                try:
                    controller.close()
                except Exception as exc:
                    # Cleanup must not replace the input/capture error that
                    # caused attach to exit. The daemon releases held input
                    # when this lease expires or is released.
                    controller.discard()
                    sys.stderr.write(f"tisplay attach: could not release held keys: {exc}\n")
        finally:
            if leased:
                try: client.control(args.session, action="release", owner=controller.owner)
                except Exception: pass
            cleanup = renderer.close()
            if cleanup: write_all(sys.stdout.fileno(), cleanup)
            signal.signal(signal.SIGWINCH, old_winch)
    return 0


class _AttachController:
    """Adapt the existing terminal parser to the remote input API."""
    def __init__(self, client: Any, session_id: str, readonly: bool = False,
                 before_flush: Callable[[], None] | None = None):
        self.client, self.session_id = client, session_id
        self.readonly = readonly
        self.before_flush = before_flush
        self.owner: str | None = None
        self._held: tuple[str, int, int] | None = None
        self._actions: list[dict[str, Any]] = []
        self._held_keys: set[str] = set()
        self._failed = False
        self.frame_id: int | str | None = None
        self.capture_id: str | None = None

    def key(self, key: str, down: bool) -> None:
        if self.readonly: return
        # Send one key per action. The daemon accepts a string as a chord
        # shorthand ("ctrl+l"), so a literal space or plus in a string would
        # otherwise be stripped or split into an empty chord.
        self._actions.append({"type": "key_down" if down else "key_up", "name": [key]})
        if down: self._held_keys.add(key)
        else: self._held_keys.discard(key)

    def button(self, button: str, down: bool, x: int, y: int) -> None:
        if self.readonly: return
        if button == "move":
            action = self._frame_action({"type": "move", "x": x, "y": y})
        elif button.startswith("wheel_"):
            action = self._frame_action({"type": "scroll", "x": x, "y": y, "delta_y": 1 if button == "wheel_up" else -1, "delta_x": 0})
        elif down:
            self._held = (button, x, y)
            return
        else:
            held = self._held
            self._held = None
            if held is None:
                # A mouse-up outside the streamed image can arrive without a
                # forwarded down event; never turn that orphan release into a click.
                return
            if held[1:] != (x, y):
                _, from_x, from_y = held
                action = self._frame_action({"type": "drag", "from_x": from_x, "from_y": from_y, "to_x": x, "to_y": y, "button": button})
            else:
                action = self._frame_action({"type": "click", "x": x, "y": y, "button": button})
        self._actions.append(action)

    def _frame_action(self, action: dict[str, Any]) -> dict[str, Any]:
        if self.frame_id is not None:
            action["frame_id"] = self.frame_id
        if self.capture_id is not None:
            action["capture_id"] = self.capture_id
        return action

    def flush(self) -> None:
        if self._actions:
            actions, self._actions = self._actions, []
            try:
                if self.before_flush:
                    self.before_flush()
                self.client.input(self.session_id, actions=actions, **({"owner": self.owner} if self.owner else {}))
            except Exception:
                # The input request may have been rejected or partially
                # applied. Never replay its user actions during cleanup.
                self._failed = True
                raise

    def close(self) -> None:
        if self._failed:
            self.discard()
            return
        if self._held_keys:
            self._actions.extend({"type": "key_up", "name": [key]} for key in sorted(self._held_keys))
            self._held_keys.clear()
        self.flush()

    def discard_pending(self) -> None:
        """Drop unsubmitted actions before the final held-key release."""
        self._actions.clear()

    def discard(self) -> None:
        """Forget queued cleanup actions after control ownership is lost."""
        self._actions.clear()
        self._held_keys.clear()
        self._held = None


def dispatch(args: argparse.Namespace) -> int:
    root = args.agent_command
    if root == "update":
        if args.host:
            import re, subprocess
            if args.host.startswith("-") or not re.fullmatch(r"[A-Za-z0-9_.@:%\[\]-]+", args.host):
                raise ValueError("SSH destination contains unsupported characters")
            options = ["--check"] if args.check else (["--rollback"] if args.rollback else [])
            command = "exec \"${XDG_BIN_HOME:-$HOME/.local/bin}/tisplay\" --json update " + " ".join(options)
            completed = subprocess.run(["ssh", "-T", "--", args.host, command], stdout=subprocess.PIPE, stderr=None)
            if completed.returncode: raise RuntimeError(f"remote tisplay update exited with status {completed.returncode}")
            _emit(json.loads(completed.stdout), args.json)
        else:
            from .updater import update_local
            _emit(update_local(check=args.check, rollback=args.rollback), args.json)
        return 0
    if root == "cua":
        from . import cua as cua_api
        verb = args.cua_command
        if verb == "install": result = cua_api.install(args.host)
        elif verb == "tools": result = cua_api.inspect("list-tools", args.host).decode("utf-8", "replace")
        elif verb == "describe": result = cua_api.inspect("describe", args.host, args.tool).decode("utf-8", "replace")
        elif verb == "doctor":
            result = cua_api.doctor(args.host, getattr(args, "session", None))
        elif verb == "call":
            parsed = json.loads(args.args)
            if not isinstance(parsed, dict): raise ValueError("--args must be a JSON object")
            result = cua_api.call(args.host, args.session, args.tool, parsed, args.out, raw=args.raw)
        elif verb == "mcp": return cua_api.mcp(args.host, args.session)
        else: raise ValueError(f"unknown Cua action: {verb}")
        _emit(result, args.json)
        if verb == "doctor" and result.get("exit_code"):
            return int(result["exit_code"])
        return 0
    client = _client(args.host)
    try:
        root = args.agent_command
        if root == "session":
            verb = args.session_command
            if verb == "start":
                command = args.command[1:] if args.command[:1] == ["--"] else args.command
                from .cli import PRESETS
                defaults = PRESETS[args.preset]
                result = client.start(name=args.name, virtual=args.mode == "virtual",
                                      **({} if args.mode == "virtual" else {"mode": args.mode}),
                                      width=args.width or defaults["width"],
                                      height=args.height or defaults["height"], command=command or None)
            elif verb == "list": result = client.list()
            elif verb == "status": result = client.status(args.session)
            elif verb == "resize": result = client.resize(args.session, width=args.width, height=args.height)
            else: result = client.stop(args.session)
        elif root in ("screenshot", "observe", "state"):
            result = _capture(client, args.session, args)
        elif root == "open-url":
            result = client.open_url(args.session, args.url, owner=args.owner)
        elif root in ("click", "double-click", "move", "drag", "scroll", "text", "type-text", "key", "press-key", "act"):
            actions = _actions_for(args)
            owner = {"owner": args.owner} if args.owner else {}
            if root != "act":
                result = client.input(args.session, actions=actions, **owner)
            else:
                if args.out and not args.screenshot: raise ValueError("--out requires --screenshot")
                batch_result = client.batch(args.session, actions=actions, stop_on_error=args.stop_on_error, **owner)
                if args.screenshot:
                    screenshot = _capture(client, args.session, args)
                    if args.verbose:
                        result = {"actions": batch_result, "screenshot": screenshot}
                    else:
                        action_results = batch_result.get("results", [])
                        failed = sum(1 for item in action_results if isinstance(item, dict) and "error" in item)
                        result = {"actions": len(action_results), "failed": failed,
                                  "screenshot": {"path": screenshot["path"], "bytes": screenshot["bytes"], "frame": screenshot.get("frame")}}
                else:
                    result = batch_result
        elif root == "wait":
            first = client.capture(args.session, max_width=400)
            before = _content_id(first)
            deadline = time.monotonic() + max(0, args.timeout)
            result = None
            while time.monotonic() < deadline:
                time.sleep(max(0.01, args.interval))
                candidate = client.capture(args.session, max_width=400)
                digest = _content_id(candidate)
                if digest != before:
                    candidate.pop("png", None)
                    result = candidate
                    break
            result = result or {"changed": False}
        elif root == "control":
            action = args.control_command
            options = {"owner": args.owner} if args.owner else {}
            if action == "acquire": options["lease_seconds"] = args.lease_seconds
            result = client.control(args.session, action=action, **options)
        elif root == "capabilities": result = client.capabilities(session=getattr(args, "session", None))
        elif root == "attach":
            # Same-host generation-4 sessions can use the original fast local
            # capture path. Remote hosts and older daemons retain PNG polling.
            if not args.host:
                capabilities = client.capabilities(args.session)
                if capabilities.get("viewer_leases"):
                    from .cli import run_session_viewer
                    return run_session_viewer(args, client, args.session)
            return _attach(client, args)
        else: raise ValueError(f"unknown command {root}")
        _emit(result, args.json)
        return 0
    finally:
        client.close()


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # argparse supports -h and --help; normalize the long single-dash spelling
    # people often try, including after a nested command.
    argv = ["--help" if token == "-help" else token for token in argv]
    parser = build_parser()
    args = None
    try:
        args = parser.parse_args(argv)
        if not args.agent_command:
            parser.print_help()
            return 0
        return dispatch(args)
    except KeyboardInterrupt:
        print("tisplay: interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        try:
            from .client import EngineError
        except ImportError:
            EngineError = ()  # type: ignore[assignment,misc]
        if isinstance(exc, EngineError):
            code = getattr(exc, "code", "engine_error")
            if args is not None and args.json:
                _emit({"ok": False, "error": {"code": code, "message": str(exc)}}, True)
            else:
                print(f"tisplay: {code}: {exc}", file=sys.stderr)
            return 3
        if args is not None and args.json:
            _emit({"ok": False, "error": {"code": "client_error", "message": str(exc)}}, True)
        else:
            print(f"tisplay: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
