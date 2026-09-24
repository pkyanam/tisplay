"""Per-user tisplay daemon. The public transport is a private Unix socket;
`--stdio` is a JSONL SSH bridge and never opens a TCP listener."""
from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from PIL import Image, ImageChops

from . import __version__
from .capture import DesktopError, Screen, VirtualDisplay, discover_accessible_x11_display, make_controller
from .client import ENGINE_GENERATION, EngineError, socket_path
from .protocol import MAX_MESSAGE_BYTES, PROTOCOL_VERSION, decode_message, encode_message


@dataclass
class Session:
    id: str
    width: int
    height: int
    virtual: bool = False
    display: str | None = None
    desktop: VirtualDisplay | None = None
    command: list[str] = field(default_factory=list)
    created: float = field(default_factory=time.time)
    control_owner: str | None = None
    control_until: float = 0
    keys_down: set[str] = field(default_factory=set)
    buttons_down: set[str] = field(default_factory=set)
    frame_id: str = ""
    env: dict[str, str | None] = field(default_factory=dict)
    name: str | None = None
    held_owner: str | None = None
    mode: str | None = None
    backend: str | None = None
    readiness_reason: str | None = None
    provider_owned: bool = False

    def __post_init__(self) -> None:
        if self.mode is None:
            self.mode = "virtual" if self.virtual else "native-existing"
        if self.virtual:
            self.provider_owned = True


class Engine:
    def __init__(self) -> None:
        self.sessions: dict[str, Session] = {}
        self.lock = threading.RLock()
        self.base_env = {k: os.environ.get(k) for k in ("DISPLAY", "XAUTHORITY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME", "TISPLAY_WAYLAND_OUTPUT", "LABWC_FALLBACK_OUTPUT", "DBUS_SESSION_BUS_ADDRESS", "DBUS_SESSION_BUS_PID")}
        if sys.platform.startswith("linux") and not self.base_env.get("DISPLAY"):
            found = discover_accessible_x11_display()
            if found: self.base_env["DISPLAY"] = found

    @contextmanager
    def session_env(self, s: Session):
        old = {k: os.environ.get(k) for k in ("DISPLAY", "XAUTHORITY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME", "TISPLAY_WAYLAND_OUTPUT", "LABWC_FALLBACK_OUTPUT", "DBUS_SESSION_BUS_ADDRESS", "DBUS_SESSION_BUS_PID")}
        for key in old:
            value = s.env.get(key)
            if value is None: os.environ.pop(key, None)
            else: os.environ[key] = value
        try: yield
        finally:
            for key, value in old.items():
                if value is None: os.environ.pop(key, None)
                else: os.environ[key] = value

    def _session(self, sid: str | None) -> Session:
        if not sid:
            raise EngineError("session is required", "invalid_request")
        try: return self.sessions[sid]
        except KeyError as exc: raise EngineError(f"unknown session: {sid}", "not_found") from exc

    def dispatch(self, req: dict[str, Any]) -> dict[str, Any]:
        command = req.get("command")
        args = req.get("args") or {}
        if not isinstance(args, dict): raise EngineError("args must be an object", "invalid_request")
        sid = req.get("session")
        with self.lock:
            if command == "start": return self.start(args)
            if command == "list": return {"sessions": [self.describe(s) for s in self.sessions.values()]}
            if command == "capabilities": return self.capabilities(sid)
            session = self._session(sid)
            if command == "status": return self.describe(session)
            if command == "stop": return self.stop(session)
            if command == "resize": return self.resize(session, args)
            if command == "capture": return self.capture(session, args)
            if command == "open-url": return self.open_url(session, args)
            if command == "input": return self.input(session, args)
            if command == "batch": return self.batch(session, args)
            if command == "control": return self.control(session, args)
        raise EngineError(f"unknown command: {command}", "invalid_request")

    def start(self, args: dict[str, Any]) -> dict[str, Any]:
        width, height = int(args.get("width", 1280)), int(args.get("height", 800))
        if not 320 <= width <= 7680 or not 240 <= height <= 4320:
            raise EngineError("width or height is outside supported bounds", "invalid_request")
        mode = args.get("mode")
        if mode is None:
            mode = "virtual" if args.get("virtual", False) else "auto"
        if mode not in ("auto", "native-existing", "native-headless", "virtual"):
            raise EngineError("mode must be auto, native-existing, native-headless, or virtual", "invalid_request")
        virtual = mode == "virtual"
        sid = str(args.get("session_id") or uuid.uuid4().hex[:12])
        if sid in self.sessions: raise EngineError("session id already exists", "conflict")
        command = args.get("command") or []
        if not isinstance(command, list) or any(not isinstance(x, str) for x in command):
            raise EngineError("command must be a list of strings", "invalid_request")
        if command and not virtual:
            raise EngineError("launching a command requires mode=virtual", "invalid_request")
        desktop = None
        virtual_env = None
        native_env = None
        if virtual:
            if any(s.virtual for s in self.sessions.values()):
                raise EngineError("only one virtual desktop can be active per daemon", "resource_busy")
            desktop = VirtualDisplay(width, height)
            try:
                desktop.__enter__()
                virtual_env = {k: os.environ.get(k) for k in self.base_env}
                if command: desktop.launch(command)
            except Exception as exc:
                desktop.__exit__(None, None, None)
                raise EngineError(str(exc), "start_failed") from exc
            display = os.environ.get("DISPLAY")
        else:
            old_env = {k: os.environ.get(k) for k in self.base_env}
            try:
                for key, value in self.base_env.items():
                    if value is None: os.environ.pop(key, None)
                    else: os.environ[key] = value
                selected_mode = mode
                native_ready = False
                if sys.platform.startswith("linux") and mode in ("auto", "native-existing"):
                    try:
                        from .native import native_available
                        native_ready = bool(native_available().get("available"))
                    except ImportError:
                        native_ready = False
                if mode == "auto":
                    selected_mode = "native-existing"
                    if os.environ.get("WAYLAND_DISPLAY") and not native_ready:
                        selected_mode = "virtual"
                    elif sys.platform.startswith("linux") and not native_ready and not os.environ.get("DISPLAY"):
                        found = discover_accessible_x11_display()
                        if found:
                            os.environ["DISPLAY"] = found
                        else:
                            selected_mode = "virtual"
                if selected_mode == "virtual":
                    # Legacy implicit fallback, used only by auto. Explicit native
                    # requests are never redirected to an Xfce desktop.
                    desktop = VirtualDisplay(width, height)
                    desktop.__enter__()
                    virtual_env = {k: os.environ.get(k) for k in self.base_env}
                    display = os.environ.get("DISPLAY")
                    virtual = True
                    mode = "virtual"
                    screen = Screen(allow_wayland=True)
                elif selected_mode == "native-headless" or (selected_mode == "native-existing" and (native_ready or os.environ.get("WAYLAND_DISPLAY"))):
                    from .native import NativeDisplay
                    display_context = NativeDisplay(require_headless=selected_mode == "native-headless", width=width, height=height)
                    display_context.__enter__()
                    desktop = display_context
                    screen = display_context
                    native_env = getattr(display_context, "environment_for_session", None) or getattr(display_context, "environment", None)
                    display = getattr(display_context, "display", None)
                    mode = selected_mode
                elif selected_mode == "native-existing":
                    screen = Screen()
                    display = self.base_env.get("DISPLAY")
                    mode = "native-existing"
                else:
                    raise DesktopError("native Wayland is unavailable; use --virtual to create an isolated Xfce desktop")
                width, height = int(screen.monitor["width"]), int(screen.monitor["height"])
                if screen is not desktop and hasattr(screen, "close"):
                    screen.close()
                if screen is not desktop and hasattr(screen, "grabber"):
                    screen.grabber.close()
            except DesktopError as exc:
                if desktop:
                    desktop.__exit__(None, None, None)
                raise EngineError(str(exc), "display_unavailable") from exc
            finally:
                for key, value in old_env.items():
                    if value is None: os.environ.pop(key, None)
                    else: os.environ[key] = value
        selected_env = virtual_env if virtual else (native_env or self.base_env.copy())
        session = Session(sid, width, height, virtual, display, desktop, command, env=selected_env, name=args.get("name"), mode=mode,
                          backend="virtual-x11" if virtual else ("wayland-native" if mode.startswith("native") and selected_env.get("WAYLAND_DISPLAY") else "x11"),
                          readiness_reason="ready" if virtual or display else "native desktop connected",
                          provider_owned=bool(virtual or getattr(desktop, "owned", False)))
        self.sessions[sid] = session
        return self.describe(session)

    def describe(self, s: Session) -> dict[str, Any]:
        return {"session_id": s.id, "name": s.name, "display": s.display, "width": s.width, "height": s.height,
                "virtual": s.virtual, "mode": s.mode, "backend": s.backend,
                "provider_ownership": "session" if s.provider_owned else "shared",
                "readiness": {"ready": True, "reason": s.readiness_reason}, "created": s.created, "running": True,
                "control": {"owner": s.control_owner if s.control_until > time.time() else None,
                            "expires_at": s.control_until if s.control_until > time.time() else None}}

    def stop(self, s: Session) -> dict[str, Any]:
        self._release_all(s)
        if s.desktop: s.desktop.__exit__(None, None, None)
        self.sessions.pop(s.id, None)
        return {"stopped": True, "session_id": s.id}

    def expire_leases(self) -> None:
        with self.lock:
            now = time.time()
            for s in self.sessions.values():
                if s.control_owner and s.control_until <= now:
                    self._release_all(s)
                    s.control_owner, s.control_until, s.held_owner = None, 0, None

    def resize(self, s: Session, args: dict[str, Any]) -> dict[str, Any]:
        if not s.virtual: raise EngineError("resize is supported only for tisplay virtual desktops", "unsupported")
        width, height = int(args.get("width", s.width)), int(args.get("height", s.height))
        if not 320 <= width <= 7680 or not 240 <= height <= 4320: raise EngineError("invalid dimensions", "invalid_request")
        # Resizing a running Xvfb is not reliable on all supported X servers.
        raise EngineError("virtual display resize is unavailable for this X server; stop and restart with new dimensions", "unsupported")

    def capabilities(self, sid: str | None) -> dict[str, Any]:
        display = self.base_env.get("DISPLAY")
        linux_x11 = sys.platform.startswith("linux") and bool(display)
        pynput = importlib.util.find_spec("pynput") is not None
        xlib = importlib.util.find_spec("Xlib") is not None
        backend = "xtest" if linux_x11 and xlib else ("pynput" if not sys.platform.startswith("linux") and pynput else None)
        text = (not sys.platform.startswith("linux") and pynput) or (linux_x11 and shutil_which("xdotool") is not None)
        old = {key: os.environ.get(key) for key in self.base_env}
        try:
            for key, value in self.base_env.items():
                if value is None: os.environ.pop(key, None)
                else: os.environ[key] = value
            try:
                from .native import native_available
                native_info = native_available()
            except ImportError as exc:
                native_info = {"available": False, "reason": f"native provider unavailable: {exc}"}
        finally:
            for key, value in old.items():
                if value is None: os.environ.pop(key, None)
                else: os.environ[key] = value
        native = bool(native_info.get("available"))
        selected = self.sessions.get(sid) if sid else None
        provider = selected.backend if selected else ("wayland-native" if native else ("x11" if linux_x11 else "platform"))
        can_manage_headless = sys.platform.startswith("linux") and bool(shutil_which("labwc") and shutil_which("dbus-run-session"))
        return {"engine_version": __version__, "engine_generation": ENGINE_GENERATION,
                "capture": bool(native or linux_x11 or not sys.platform.startswith("linux")), "pointer": backend is not None or native,
                "keyboard": backend is not None or native,
                "input_backend": "wayvnc-unix" if native else backend, "unicode_text": bool(text or native),
                "resize": False, "remote_transport": "ssh-stdio", "control_leases": True,
                "provider": provider,
                "native": {"available": native, "reason": native_info.get("reason", "ready"), "outputs": native_info.get("outputs", [])},
                "virtual_output": {"supported": sys.platform.startswith("linux"), "available": bool(native_info.get("headless")),
                                   "provisionable": can_manage_headless,
                                   "reason": "active NOOP output detected" if native_info.get("headless") else ("managed labwc headless session can be started" if can_manage_headless else "requires labwc and dbus-run-session")}}

    def _screen(self, s: Session, args: dict[str, Any]) -> tuple[Image.Image, dict[str, Any]]:
        screen = self._screen_adapter(s)
        try:
            image = screen.frame(max_width=None)
            mon = dict(screen.monitor)
        finally:
            if screen is not s.desktop and hasattr(screen, "close"): screen.close()
            if screen is not s.desktop and hasattr(screen, "grabber"): screen.grabber.close()
        region = args.get("region")
        if region:
            x, y = int(region.get("x", 0)), int(region.get("y", 0))
            w, h = int(region.get("width", 0)), int(region.get("height", 0))
            if w <= 0 or h <= 0 or x < 0 or y < 0 or x + w > image.width or y + h > image.height:
                raise EngineError("capture region falls outside the display", "invalid_request")
            image = image.crop((x, y, x + w, y + h))
        else: x = y = 0
        native = image.size
        scale = args.get("scale")
        max_width = args.get("max_width")
        if scale is not None:
            scale = float(scale)
            if not 0.05 <= scale <= 1: raise EngineError("scale must be between 0.05 and 1", "invalid_request")
            if scale != 1: image = image.resize((max(1, round(image.width * scale)), max(1, round(image.height * scale))), Image.Resampling.LANCZOS)
        elif max_width is not None:
            max_width = int(max_width)
            if not 32 <= max_width <= 10000: raise EngineError("max_width must be between 32 and 10000", "invalid_request")
            if image.width > max_width: image.thumbnail((max_width, image.height), Image.Resampling.LANCZOS)
        display_width = int(mon["width"]); display_height = int(mon["height"])
        # Geometry token stays stable across animation and changes on monitor/layout change.
        geom = f"{mon['left']}:{mon['top']}:{display_width}:{display_height}"
        frame_id = hashlib.sha256(geom.encode()).hexdigest()[:20]
        s.frame_id = frame_id
        from io import BytesIO
        buf = BytesIO(); image.save(buf, format="PNG", optimize=False)
        png = buf.getvalue()
        meta = {"frame_id": frame_id, "content_id": hashlib.sha256(png).hexdigest()[:20], "timestamp": time.time(), "width": image.width, "height": image.height,
                "display_width": display_width, "display_height": display_height,
                "monitor": {"left": mon["left"], "top": mon["top"], "width": mon["width"], "height": mon["height"]},
                "region": {"x": x, "y": y, "width": native[0], "height": native[1]}}
        return image, {"png": base64.b64encode(png).decode("ascii"), "frame": meta}

    @staticmethod
    def _screen_adapter(s: Session):
        if s.backend == "wayland-native":
            if s.desktop is None:
                raise DesktopError("native session provider has stopped")
            return s.desktop
        return Screen(allow_wayland=s.virtual)

    @staticmethod
    def _controller_adapter(s: Session):
        if s.backend == "wayland-native":
            from .native import NativeWaylandController
            return NativeWaylandController()
        return make_controller()

    def capture(self, s: Session, args: dict[str, Any]) -> dict[str, Any]:
        try:
            with self.session_env(s):
                if s.desktop and hasattr(s.desktop, "check_command"): s.desktop.check_command()
                result = self._screen(s, args)[1]
            if len(json.dumps(result, separators=(",", ":")).encode()) > MAX_MESSAGE_BYTES - 1024:
                raise EngineError("capture is too large for the protocol response; retry with a smaller scale or max_width", "response_too_large")
            return result
        except (DesktopError, OSError) as exc: raise EngineError(str(exc), "capture_failed") from exc

    def open_url(self, s: Session, args: dict[str, Any]) -> dict[str, Any]:
        url = args.get("url")
        if not isinstance(url, str) or not 1 <= len(url) <= 4096 or any(ord(ch) < 0x20 or ch.isspace() for ch in url):
            raise EngineError("URL must be a non-empty HTTP or HTTPS URL without whitespace", "invalid_request")
        try:
            parsed = urlsplit(url)
            valid_url = parsed.scheme.lower() in ("http", "https") and bool(parsed.hostname)
            _ = parsed.port
        except ValueError:
            valid_url = False
        if not valid_url or parsed.username is not None or parsed.password is not None:
            raise EngineError("URL must use HTTP or HTTPS and include a valid host (credentials are not accepted)", "invalid_request")
        owner = str(args.get("owner", "agent"))
        if s.control_owner and s.control_until > time.time() and s.control_owner != owner:
            raise EngineError("control is held by another owner", "control_denied", {"owner": s.control_owner})
        if sys.platform == "darwin":
            command = ["open", url]
        elif sys.platform.startswith("linux"):
            opener = shutil_which("xdg-open") or shutil_which("gio")
            if not opener:
                raise EngineError("URL opening is unavailable; install xdg-utils or GLib gio on the target", "unsupported")
            command = [opener, *( ["open"] if Path(opener).name == "gio" else [] ), url]
        else:
            raise EngineError("URL opening is unavailable on this operating system", "unsupported")
        try:
            with self.session_env(s):
                process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                           stderr=subprocess.DEVNULL, env=os.environ.copy(),
                                           start_new_session=True, close_fds=True)
        except OSError as exc:
            raise EngineError(f"could not start the session URL opener: {exc}", "start_failed") from exc
        return {"opened": True, "url": url, "pid": process.pid, "backend": "session-opener"}

    def control(self, s: Session, args: dict[str, Any]) -> dict[str, Any]:
        action = args.get("action")
        now = time.time()
        if s.control_until < now:
            self._release_all(s); s.control_owner, s.control_until, s.held_owner = None, 0, None
        if action == "status": return {"owner": s.control_owner, "expires_at": s.control_until or None}
        owner = str(args.get("owner", "agent"))
        if action == "acquire":
            seconds = max(1, min(int(args.get("lease_seconds", 60)), 3600))
            if s.control_owner and s.control_owner != owner:
                if not args.get("takeover"): raise EngineError("control is held by another owner", "control_busy", {"owner": s.control_owner})
                self._release_all(s)
            s.control_owner, s.control_until = owner, now + seconds
            return {"owner": owner, "expires_at": s.control_until}
        if action == "release":
            if s.control_owner and s.control_owner != owner: raise EngineError("only the control owner may release control", "control_denied")
            self._release_all(s); s.control_owner, s.control_until = None, 0
            s.held_owner = None
            return {"released": True}
        raise EngineError("action must be acquire, release, or status", "invalid_request")

    @staticmethod
    def _actions(args: dict[str, Any]) -> list[dict[str, Any]]:
        actions = args.get("actions")
        if isinstance(actions, dict): actions = [actions]
        if not isinstance(actions, list) or not actions or len(actions) > 500 or any(not isinstance(x, dict) for x in actions):
            raise EngineError("actions must be a non-empty array of at most 500 objects", "invalid_request")
        normalized = []
        for original in actions:
            action = dict(original)
            if action.get("type") == "press_key": action["type"] = "key"
            elif action.get("type") == "type_text": action["type"] = "text"
            if action.get("type") in ("click", "double_click"):
                if "mouse_button" in action:
                    if "button" in action: raise EngineError("use either button or mouse_button, not both", "invalid_request")
                    action["button"] = action.pop("mouse_button")
                if "click_count" in action:
                    count = action.pop("click_count")
                    if count not in (1, 2): raise EngineError("click_count must be 1 or 2", "invalid_request")
                    if action["type"] == "double_click" and count == 1: raise EngineError("double_click cannot use click_count=1", "invalid_request")
                    if count == 2: action["type"] = "double_click"
            normalized.append(action)
        return normalized

    def input(self, s: Session, args: dict[str, Any]) -> dict[str, Any]:
        actions = self._actions(args)
        owner = str(args.get("owner", "agent"))
        now = time.time()
        if s.control_owner and s.control_until <= now:
            self._release_all(s); s.control_owner, s.control_until, s.held_owner = None, 0, None
        if s.control_until > now and s.control_owner != owner: raise EngineError("control is held by another owner", "control_denied", {"owner": s.control_owner})
        needs_frame = any(action.get("type") in ("move", "click", "double_click", "drag", "scroll") or action.get("frame_id") is not None for action in actions)
        if needs_frame:
            try:
                with self.session_env(s):
                    _, result = self._screen(s, {"max_width": 32})
                frame = result["frame"]
                latest = frame["frame_id"]
            except Exception as exc:
                if isinstance(exc, EngineError): raise
                raise EngineError(f"cannot validate input coordinates: {exc}", "input_failed") from exc
        else:
            latest = s.frame_id or None
            frame = {"frame_id": latest, "display_width": s.width, "display_height": s.height,
                     "monitor": {"left": 0, "top": 0}}
        results = 0
        try:
            with self.session_env(s):
                controller = self._controller_adapter(s)
                try:
                    for action in actions:
                        monitor = frame["monitor"]
                        self._do_action(s, controller, action, latest or "", frame["display_width"], frame["display_height"], monitor["left"], monitor["top"], owner)
                        results += 1
                finally: controller.close()
        except EngineError:
            self._release_all(s); raise
        except Exception as exc:
            self._release_all(s)
            raise EngineError(str(exc), "input_failed") from exc
        return {"completed": results, "frame_id": latest}

    def batch(self, s: Session, args: dict[str, Any]) -> dict[str, Any]:
        actions = self._actions(args)
        done = []
        stop_on_error = bool(args.get("stop_on_error", True))
        if stop_on_error:
            index = 0
            while index < len(actions):
                if actions[index].get("type") == "wait":
                    action = actions[index]
                    try:
                        timeout = min(max(int(action.get("timeout_ms", 1000)), 1), 30000) / 1000
                        interval = min(max(int(action.get("interval_ms", 100)), 20), 1000) / 1000
                        before = self.capture(s, {"max_width": 400})["png"]
                        deadline = time.monotonic() + timeout
                        changed = False
                        while time.monotonic() < deadline:
                            time.sleep(min(interval, max(0, deadline-time.monotonic())))
                            if before != self.capture(s, {"max_width": 400})["png"]:
                                changed = True; break
                        done.append({"type": "wait", "changed": changed})
                    except EngineError:
                        raise
                    index += 1
                    continue
                end = index
                while end < len(actions) and actions[end].get("type") != "wait": end += 1
                result = self.input(s, {"actions": actions[index:end], "owner": args.get("owner", "agent")})
                done.extend({"completed": 1, "frame_id": result.get("frame_id")} for _ in actions[index:end])
                index = end
            return {"completed": len(done), "results": done}
        for action in actions:
            try:
                if action.get("type") == "wait":
                    timeout = min(max(int(action.get("timeout_ms", 1000)), 1), 30000) / 1000
                    interval = min(max(int(action.get("interval_ms", 100)), 20), 1000) / 1000
                    before = self.capture(s, {"max_width": 400})["png"]
                    deadline = time.monotonic() + timeout
                    changed = False
                    while time.monotonic() < deadline:
                        time.sleep(min(interval, max(0, deadline-time.monotonic())))
                        after = self.capture(s, {"max_width": 400})["png"]
                        if before != after: changed = True; break
                    done.append({"type": "wait", "changed": changed})
                else: done.append(self.input(s, {"actions": [action], "owner": args.get("owner", "agent")}))
            except EngineError as exc:
                if stop_on_error: raise
                done.append({"error": {"code": exc.code, "message": str(exc), **({"details": exc.details} if exc.details is not None else {})}})
        return {"completed": len(done), "results": done}

    def _do_action(self, s: Session, c: Any, a: dict[str, Any], frame_id: str, display_width: int, display_height: int, offset_x: int, offset_y: int, owner: str) -> None:
        typ = a.get("type")
        if a.get("frame_id") is not None and a["frame_id"] != frame_id:
            raise EngineError("input frame has expired because display geometry changed", "stale_frame", {"current_frame_id": frame_id})
        def xy():
            x, y = int(a["x"]), int(a["y"])
            if x < 0 or y < 0 or x >= display_width or y >= display_height:
                raise EngineError("coordinates fall outside the primary display", "invalid_request")
            return x + offset_x, y + offset_y
        if typ == "move": c.move(*xy())
        elif typ in ("click", "double_click"):
            x, y = xy(); b = a.get("button", "left"); count = 2 if typ == "double_click" else 1
            if b not in ("left", "middle", "right"): raise EngineError("button must be left, middle, or right", "invalid_request")
            for _ in range(count):
                c.button(b, True, x, y); s.buttons_down.add(b)
                c.button(b, False, x, y); s.buttons_down.discard(b)
        elif typ == "drag":
            button = a.get("button", "left")
            if button not in ("left", "middle", "right"): raise EngineError("button must be left, middle, or right", "invalid_request")
            x, y = int(a["from_x"]), int(a["from_y"])
            end_x, end_y = int(a["to_x"]), int(a["to_y"])
            if any(v < 0 for v in (x, y, end_x, end_y)) or x >= display_width or end_x >= display_width or y >= display_height or end_y >= display_height:
                raise EngineError("drag coordinates fall outside the primary display", "invalid_request")
            x, y, end_x, end_y = x + offset_x, y + offset_y, end_x + offset_x, end_y + offset_y
            c.button(button, True, x, y)
            s.buttons_down.add(button)
            for i in range(1, 11): c.move(round(x+(end_x-x)*i/10), round(y+(end_y-y)*i/10)); time.sleep(.01)
            c.button(button, False, end_x, end_y); s.buttons_down.discard(button)
        elif typ == "scroll":
            x, y = xy(); dx, dy = int(a.get("delta_x", 0)), int(a.get("delta_y", 0))
            if abs(dx) > 100 or abs(dy) > 100: raise EngineError("scroll delta is limited to 100 steps", "invalid_request")
            c.scroll(x, y, dx, dy)
        elif typ in ("key", "key_down", "key_up"):
            names = a.get("name")
            if isinstance(names, str): names = [p.strip() for p in names.split("+") if p.strip()]
            if not isinstance(names, list) or not names: raise EngineError("key action requires a key name", "invalid_request")
            valid_names = {"ctrl", "control", "alt", "shift", "meta", "cmd", "super", "enter", "return", "tab", "backspace", "delete", "escape", "esc", "up", "down", "left", "right", "home", "end", "page_up", "page_down", "insert", "space"}
            for key in names:
                if not isinstance(key, str) or not (len(key) == 1 and key.isprintable() or key.lower() in valid_names or (key.lower().startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24)):
                    raise EngineError(f"unsupported key name: {key}", "invalid_request")
            has_chord = len(names) > 1
            names = [key.lower() if has_chord and len(key) == 1 and key.isascii() and key.isalpha() else (key if len(key) == 1 else key.lower()) for key in names]
            if typ == "key":
                for key in names: c.key(key, True); s.keys_down.add(key)
                for key in reversed(names): c.key(key, False); s.keys_down.discard(key)
            else:
                down = typ == "key_down"
                if down and (s.control_owner != owner or s.control_until <= time.time()):
                    raise EngineError("key_down requires an active control lease; acquire control first", "control_required")
                for key in names: c.key(key, down); (s.keys_down.add if down else s.keys_down.discard)(key)
                s.held_owner = owner if down else (None if not s.keys_down else s.held_owner)
        elif typ == "text":
            value = a.get("text")
            if not isinstance(value, str) or len(value) > 10000: raise EngineError("text must be a string of at most 10000 characters", "invalid_request")
            if hasattr(c, "type_text"): c.type_text(value)
            elif sys.platform.startswith("linux") and shutil_which("xdotool"):
                subprocess.run(["xdotool", "type", "--clearmodifiers", "--", value], check=True, timeout=15)
            else: raise EngineError("Unicode text entry is unavailable; install xdotool on X11 or use a platform keyboard backend", "unsupported")
        else: raise EngineError(f"unsupported input action: {typ}", "invalid_request")

    def _release_all(self, s: Session) -> None:
        if not s.keys_down and not s.buttons_down:
            s.held_owner = None
            return
        try:
            with self.session_env(s): c = self._controller_adapter(s)
        except Exception: s.keys_down.clear(); s.buttons_down.clear(); return
        try:
            with self.session_env(s):
                for key in list(s.keys_down):
                    try: c.key(key, False)
                    except Exception: pass
                for button in list(s.buttons_down):
                    try:
                        if hasattr(c, "release_button"): c.release_button(button)
                        else: c.button(button, False, 0, 0)
                    except Exception: pass
        finally:
            s.keys_down.clear(); s.buttons_down.clear(); s.held_owner = None; c.close()


def shutil_which(name: str) -> str | None:
    import shutil
    return shutil.which(name)


def _response(engine: Engine, raw: bytes) -> bytes:
    reqid = None
    try:
        req = decode_message(raw); reqid = req.get("id")
        if req.get("protocol_version") != PROTOCOL_VERSION:
            raise EngineError(f"unsupported protocol version: {req.get('protocol_version')}", "unsupported_protocol")
        result = engine.dispatch(req)
        return encode_message({"protocol_version": PROTOCOL_VERSION, "id": reqid, "ok": True, "result": result})
    except EngineError as exc:
        return encode_message({"protocol_version": PROTOCOL_VERSION, "id": reqid, "ok": False, "error": {"code": exc.code, "message": str(exc), **({"details": exc.details} if exc.details is not None else {})}})
    except Exception as exc:
        return encode_message({"protocol_version": PROTOCOL_VERSION, "id": reqid, "ok": False, "error": {"code": "internal_error", "message": str(exc)}})


def _serve_client(conn: socket.socket, engine: Engine) -> None:
    with conn, conn.makefile("rwb") as stream:
        while True:
            line = stream.readline(MAX_MESSAGE_BYTES + 1)
            if not line: break
            if len(line) > MAX_MESSAGE_BYTES:
                stream.write(encode_message({"protocol_version": PROTOCOL_VERSION, "id": None, "ok": False, "error": {"code": "message_too_large", "message": "protocol message exceeds size limit"}})); break
            stream.write(_response(engine, line)); stream.flush()


def run_stdio() -> int:
    """SSH bridge: forward JSONL to the remote user's daemon socket."""
    path = socket_path()
    end = time.monotonic() + 5
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    while True:
        try: client.connect(str(path)); break
        except OSError:
            if time.monotonic() >= end:
                subprocess.Popen([sys.executable, "-m", "tisplay.daemon"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
                end = time.monotonic() + 5
                time.sleep(.1)
            else: time.sleep(.05)
    remote = client.makefile("rwb")
    try:
        while True:
            line = sys.stdin.buffer.readline(MAX_MESSAGE_BYTES + 1)
            if not line: break
            remote.write(line); remote.flush()
            response = remote.readline(MAX_MESSAGE_BYTES + 1)
            if not response: break
            sys.stdout.buffer.write(response); sys.stdout.buffer.flush()
    finally: remote.close(); client.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("--stdio", action="store_true")
    args = parser.parse_args()
    if args.stdio: return run_stdio()
    path = socket_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent_stat = path.parent.lstat()
    if stat.S_ISLNK(parent_stat.st_mode) or parent_stat.st_uid != os.getuid():
        raise SystemExit("unsafe tisplay runtime directory")
    os.chmod(path.parent, 0o700)
    if path.exists() or path.is_symlink():
        old = path.lstat()
        if not stat.S_ISSOCK(old.st_mode) or old.st_uid != os.getuid(): raise SystemExit("unsafe existing tisplay socket path")
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.connect(str(path))
            raise SystemExit("tisplay daemon is already running")
        except (ConnectionRefusedError, FileNotFoundError):
            path.unlink()
        except OSError as exc:
            raise SystemExit(f"could not verify existing tisplay socket: {exc}") from exc
        finally: probe.close()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); server.bind(str(path)); os.chmod(path, 0o600); server.listen(16)
    engine = Engine(); stopping = threading.Event()
    def lease_watchdog():
        while not stopping.wait(.5): engine.expire_leases()
    threading.Thread(target=lease_watchdog, daemon=True).start()
    def stop(*_): stopping.set(); server.close()
    signal.signal(signal.SIGTERM, stop); signal.signal(signal.SIGINT, stop)
    try:
        while not stopping.is_set():
            try: conn, _ = server.accept()
            except OSError: break
            threading.Thread(target=_serve_client, args=(conn, engine), daemon=True).start()
    finally:
        for s in list(engine.sessions.values()): engine.stop(s)
        try: path.unlink()
        except OSError: pass
    return 0


if __name__ == "__main__": raise SystemExit(main())
