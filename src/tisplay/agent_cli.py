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
from typing import Any, Sequence

from . import __version__


class HelpFormatter(argparse.RawDescriptionHelpFormatter):
    """Keep examples readable while aligning option descriptions cleanly."""


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(2, f"tisplay: error: {message}\nRun `tisplay --help` for usage.\n")


EPILOG = """\
EXAMPLES
  Human terminal             tisplay --virtual --preset balanced
  Start a named desktop      tisplay session start --virtual --name work --json
  Start one on a Pi          tisplay --host pi session start --virtual --name work --json
  Inspect / save a frame     tisplay screenshot --session SESSION --out frame.png
  Control it                 tisplay click --session SESSION 640 360
  Attach to that live desktop tisplay attach --session SESSION

AGENT WORKFLOW
  Start once, then use session status/list, screenshot, click, text, key, and
  wait changed across separate commands. `--json` returns one JSON object on
  stdout; diagnostics go to stderr. Screenshot files are written on this
  machine, including when the desktop is remote. `act` accepts an ordered JSON
  action list for low-latency multi-step automation.

  tisplay act --session SESSION --actions '[{"type":"click","x":640,"y":360},{"type":"text","text":"hello"}]' --json

PERFORMANCE
  `--preset quality|balanced|fast` controls capture defaults; explicit size or
  frame-rate options override the preset. `attach` needs an interactive TTY.
  Remote control requires SSH access and Python on the remote host. A virtual
  desktop requires the Linux Xvfb/Xfce dependencies installed by install.sh.

INTERACTIVE MODE
  `tisplay --virtual --preset balanced` opens a terminal viewer directly.
  Options for that mode: --graphics auto|kitty|ansi, --preset quality|balanced|
  fast, --fps N, --max-width PIXELS, --virtual, --test-pattern, --width PIXELS,
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
    roots = parser.add_subparsers(dest="agent_command", metavar="COMMAND")

    session = roots.add_parser("session", help="start and manage persistent desktop sessions", formatter_class=HelpFormatter)
    sessions = session.add_subparsers(dest="session_command", required=True, metavar="ACTION")
    start = sessions.add_parser("start", help="start a desktop session", formatter_class=HelpFormatter)
    _common(start)
    start.add_argument("--name", help="optional label shown in session listings")
    start.add_argument("--virtual", action="store_true", help="create a private Xvfb desktop (Linux)")
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

    screenshot = roots.add_parser("screenshot", help="save a PNG snapshot of a session", formatter_class=HelpFormatter)
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
            p.add_argument("x", type=int); p.add_argument("y", type=int)
        elif verb == "drag":
            p.add_argument("x", type=int); p.add_argument("y", type=int)
            p.add_argument("to_x", type=int); p.add_argument("to_y", type=int)
        else:
            p.add_argument("direction", choices=("up", "down", "left", "right"))
            p.add_argument("--x", type=int, default=0); p.add_argument("--y", type=int, default=0)
            p.add_argument("--amount", type=int, default=1)

    for verb, help_text in (("text", "type text into the desktop"), ("key", "press a key or chord")):
        p = roots.add_parser(verb, help=help_text, formatter_class=HelpFormatter)
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

    attach = roots.add_parser("attach", help="interactively view and control a live session", formatter_class=HelpFormatter)
    _common(attach, input_owner=True); _session(attach)
    attach.add_argument("--graphics", choices=("auto", "kitty", "ansi"), default="auto")
    attach.add_argument("--fps", type=float, default=15.0, help="remote capture refresh target (default: 15)")
    attach.add_argument("--max-width", type=int, default=1600, help="cap each remote frame width (default: 1600)")
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
        action: dict[str, Any] = {"type": command.replace("-", "_"), "x": args.x, "y": args.y}
    elif command == "drag":
            action = {"type": "drag", "from_x": args.x, "from_y": args.y, "to_x": args.to_x, "to_y": args.to_y}
    elif command == "scroll":
            action = {"type": "scroll", "x": args.x, "y": args.y,
                      "delta_y": (args.amount if args.direction == "up" else -args.amount) if args.direction in ("up", "down") else 0,
                      "delta_x": (-args.amount if args.direction == "left" else args.amount) if args.direction in ("left", "right") else 0}
    elif command == "text":
        action = {"type": "text", "text": args.value}
    elif command == "key":
        action = {"type": "key", "name": args.value}
    else:
        raise ValueError(f"unsupported action command: {command}")
    return [action]


def _attach(client: Any, args: argparse.Namespace) -> int:
    """Attach by polling PNG frames and forwarding terminal input events."""
    import select
    import signal
    import termios
    import tty
    from PIL import Image
    from . import cli as interactive
    from .terminal import KittyRenderer, Terminal, begin_terminal, render_blocks, terminal_size

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ValueError("attach needs an interactive terminal")
    status = client.status(args.session)
    mon = status.get("monitor") or {"left": 0, "top": 0, "width": status.get("width", 1920), "height": status.get("height", 1080)}
    screen = type("RemoteScreen", (), {"monitor": mon})()
    graphics = interactive.detect_graphics() if args.graphics == "auto" else args.graphics
    renderer = KittyRenderer()
    controller = _AttachController(client, args.session)
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
        client.control(args.session, action="acquire", owner=controller.owner)
        leased = True
        with Terminal(sys.stdin.fileno()):
            begin_terminal()
            sys.stderr.write("tisplay attach: Ctrl-] disconnects | input controls the live desktop\n")
            renew_at = time.monotonic() + 20
            while True:
                now = time.monotonic()
                if now >= renew_at:
                    client.control(args.session, action="acquire", owner=controller.owner)
                    renew_at = now + 20
                if now >= next_frame:
                    result = client.capture(args.session, max_width=args.max_width)
                    monitor = result.get("frame", {}).get("monitor")
                    if monitor:
                        screen.monitor = monitor
                    controller.frame_id = result.get("frame", {}).get("frame_id")
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
                        if output: os.write(sys.stdout.fileno(), output)
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
            controller.close()
        finally:
            if leased:
                try: client.control(args.session, action="release", owner=controller.owner)
                except Exception: pass
            cleanup = renderer.close()
            if cleanup: os.write(sys.stdout.fileno(), cleanup)
            signal.signal(signal.SIGWINCH, old_winch)
    return 0


class _AttachController:
    """Adapt the existing terminal parser to the remote input API."""
    def __init__(self, client: Any, session_id: str):
        self.client, self.session_id = client, session_id
        self.owner: str | None = None
        self._held: tuple[str, int, int] | None = None
        self._actions: list[dict[str, Any]] = []
        self._held_keys: set[str] = set()
        self.frame_id: int | str | None = None

    def key(self, key: str, down: bool) -> None:
        self._actions.append({"type": "key_down" if down else "key_up", "name": key})
        if down: self._held_keys.add(key)
        else: self._held_keys.discard(key)

    def button(self, button: str, down: bool, x: int, y: int) -> None:
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
            if held and held[1:] != (x, y):
                _, from_x, from_y = held
                action = self._frame_action({"type": "drag", "from_x": from_x, "from_y": from_y, "to_x": x, "to_y": y, "button": button})
            else:
                action = self._frame_action({"type": "click", "x": x, "y": y, "button": button})
        self._actions.append(action)

    def _frame_action(self, action: dict[str, Any]) -> dict[str, Any]:
        if self.frame_id is not None:
            action["frame_id"] = self.frame_id
        return action

    def flush(self) -> None:
        if self._actions:
            actions, self._actions = self._actions, []
            self.client.input(self.session_id, actions=actions, **({"owner": self.owner} if self.owner else {}))

    def close(self) -> None:
        if self._held_keys:
            self._actions.extend({"type": "key_up", "name": key} for key in sorted(self._held_keys))
            self._held_keys.clear()
        self.flush()


def dispatch(args: argparse.Namespace) -> int:
    client = _client(args.host)
    try:
        root = args.agent_command
        if root == "session":
            verb = args.session_command
            if verb == "start":
                command = args.command[1:] if args.command[:1] == ["--"] else args.command
                from .cli import PRESETS
                defaults = PRESETS[args.preset]
                result = client.start(name=args.name, virtual=args.virtual,
                                      width=args.width or defaults["width"],
                                      height=args.height or defaults["height"], command=command or None)
            elif verb == "list": result = client.list()
            elif verb == "status": result = client.status(args.session)
            elif verb == "resize": result = client.resize(args.session, width=args.width, height=args.height)
            else: result = client.stop(args.session)
        elif root == "screenshot":
            result = _capture(client, args.session, args)
        elif root in ("click", "double-click", "move", "drag", "scroll", "text", "key", "act"):
            actions = _actions_for(args)
            owner = {"owner": args.owner} if args.owner else {}
            result = client.input(args.session, actions=actions, **owner) if root != "act" else client.batch(args.session, actions=actions, stop_on_error=args.stop_on_error, **owner)
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
        elif root == "attach": return _attach(client, args)
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
