"""Native Wayland desktop capture and input helpers.

The provider attaches to the caller's existing compositor. It never launches,
stops, or reconfigures a compositor. Capture uses grim; input is sent through a
short-lived WayVNC server bound only to a private Unix-domain socket.
"""
from __future__ import annotations

import os
import re
import shutil
import socket
import stat
import struct
import sys
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from PIL import Image

from .capture import DesktopError


def _discover_environment() -> dict[str, str] | None:
    """Resolve this user's active Wayland socket without changing process env."""
    uid = os.getuid()
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{uid}"
    try:
        runtime_stat = os.stat(runtime)
    except OSError:
        return None
    if runtime_stat.st_uid != uid or not stat.S_ISDIR(runtime_stat.st_mode):
        return None
    configured_display = os.environ.get("WAYLAND_DISPLAY")
    candidates = [configured_display] if configured_display else []
    try:
        candidates.extend(sorted(p.name for p in Path(runtime).glob("wayland-*") if p.name not in candidates))
    except OSError:
        pass
    for display in candidates:
        if not display:
            continue
        path = Path(display)
        if not path.is_absolute(): path = Path(runtime) / path
        try:
            info = path.stat()
        except OSError:
            continue
        if info.st_uid != uid or not stat.S_ISSOCK(info.st_mode):
            continue
        result = {"WAYLAND_DISPLAY": display, "XDG_RUNTIME_DIR": runtime}
        if os.environ.get("TISPLAY_WAYLAND_OUTPUT"):
            result["TISPLAY_WAYLAND_OUTPUT"] = os.environ["TISPLAY_WAYLAND_OUTPUT"]
        bus = Path(runtime) / "bus"
        try:
            if bus.stat().st_uid == uid and stat.S_ISSOCK(bus.stat().st_mode):
                result["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus}"
        except OSError:
            pass
        return result
    return None


def native_available(environment: dict[str, str] | None = None) -> dict[str, Any]:
    """Describe whether a usable native Wayland desktop is available."""
    if os.name != "posix" or not Path("/proc").exists():
        return {"available": False, "reason": "native Wayland desktops are currently supported on Linux only"}
    environment = environment or _discover_environment()
    if not environment:
        return {"available": False, "reason": "no active Wayland socket owned by this user was found"}
    missing = [name for name in ("grim", "wlr-randr", "wayvnc") if not shutil.which(name)]
    if missing:
        return {"available": False, "reason": f"missing native desktop tools: {', '.join(missing)}"}
    try:
        outputs = _outputs(environment)
    except DesktopError as exc:
        return {"available": False, "reason": str(exc)}
    if not outputs:
        return {"available": False, "reason": "Wayland compositor has no enabled output"}
    return {"available": True, "outputs": outputs, "headless": any(o["name"].startswith("NOOP-") for o in outputs), "environment": environment}


def _outputs(environment: dict[str, str] | None = None) -> list[dict[str, Any]]:
    try:
        result = subprocess.run([shutil.which("wlr-randr") or "wlr-randr"], capture_output=True, text=True, timeout=3, env=environment)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DesktopError(f"Cannot inspect Wayland outputs with wlr-randr: {exc}") from exc
    if result.returncode:
        raise DesktopError(f"wlr-randr failed: {result.stderr.strip() or result.stdout.strip()}")
    outputs: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in result.stdout.splitlines():
        if line and not line[0].isspace():
            if current and current.get("enabled"):
                outputs.append(current)
            current = {"name": line.split()[0], "enabled": False}
        elif current:
            stripped = line.strip()
            if stripped.startswith("Enabled:"):
                current["enabled"] = stripped.endswith("yes")
            elif stripped.startswith("Position:"):
                m = re.search(r"(-?\d+),\s*(-?\d+)", stripped)
                if m: current["left"], current["top"] = map(int, m.groups())
            elif stripped.startswith("Modes:"):
                pass
            elif re.search(r"\bcurrent\b", stripped):
                m = re.search(r"(\d+)x(\d+)", stripped)
                if m: current["width"], current["height"] = map(int, m.groups())
    if current and current.get("enabled"):
        outputs.append(current)
    # Some wlroots versions omit the current annotation on the only mode.
    for output in outputs:
        output.setdefault("left", 0); output.setdefault("top", 0)
    return outputs


def _select_output(outputs: list[dict[str, Any]], require_headless: bool) -> dict[str, Any]:
    if require_headless:
        selected = next((o for o in outputs if o["name"].startswith("NOOP-")), None)
        if selected is None:
            raise DesktopError("no enabled NOOP headless output is available")
        return selected
    return next((o for o in outputs if o.get("left") == 0 and o.get("top") == 0), outputs[0])


def _processes_with_runtime(runtime: str) -> list[tuple[int, int]]:
    """Find this user's processes belonging to one unique private runtime."""
    found: list[tuple[int, int]] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != os.getuid():
                continue
            environment = entry.joinpath("environ").read_bytes().split(b"\0")
            marker = b"XDG_RUNTIME_DIR=" + os.fsencode(runtime)
            if marker in environment:
                found.append((int(entry.name), os.getpgid(int(entry.name))))
        except (OSError, PermissionError, ProcessLookupError):
            continue
    return found


def _signal_runtime_groups(runtime: str, sig: int) -> None:
    members = _processes_with_runtime(runtime)
    for pgid in {group for _, group in members}:
        # Revalidate the marker immediately before signalling a group to avoid
        # acting on a recycled PID/PGID after a concurrent process exit.
        if not any(group == pgid for _, group in _processes_with_runtime(runtime)):
            continue
        try: os.killpg(pgid, sig)
        except ProcessLookupError: pass


def _mounted_paths_below(root: str) -> list[str]:
    """Return mount points below an owned runtime without traversing them."""
    root_path = Path(root).absolute()
    mounts: list[str] = []
    try:
        lines = Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
    except OSError:
        # If mount state cannot be checked, recursive deletion is unsafe.
        return [root]
    for line in lines:
        fields = line.split(" - ", 1)[0].split()
        if len(fields) < 5:
            continue
        mount = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), fields[4])
        mount_path = Path(mount)
        try:
            mount_path.relative_to(root_path)
        except ValueError:
            continue
        mounts.append(mount)
    return mounts


def _cleanup_owned_runtime(root: str) -> bool:
    """Remove an owned tree only after confirming it contains no mounts."""
    mounts = _mounted_paths_below(root)
    if mounts:
        print(f"tisplay: leaving owned native runtime in place because mounts remain below it: {', '.join(mounts)}", file=sys.stderr, flush=True)
        return False
    shutil.rmtree(root, ignore_errors=True)
    return not Path(root).exists()


class NativeDisplay:
    """Capture the active Wayland desktop or own an isolated labwc session."""
    def __init__(self, require_headless: bool = False, width: int = 1280, height: int = 800, force_new: bool = False) -> None:
        self.require_headless = require_headless
        self.width, self.height = width, height
        self.process: subprocess.Popen | None = None
        self._runtime: str | None = None
        self._log: Any = None
        self.managed = self.owned = False
        availability = native_available()
        if not force_new and availability["available"] and (not require_headless or availability["headless"]):
            self.environment = dict(availability["environment"])
            self.outputs = availability["outputs"]
            self.monitor = _select_output(self.outputs, require_headless)
        elif require_headless:
            self._start_headless()
        else:
            raise DesktopError(availability["reason"])
        self.display = self.environment["WAYLAND_DISPLAY"]
        self.xdg_runtime_dir = self.environment["XDG_RUNTIME_DIR"]
        self.environment["TISPLAY_WAYLAND_OUTPUT"] = self.monitor["name"]
        self.headless = self.monitor["name"].startswith("NOOP-")

    def _start_headless(self) -> None:
        missing = [name for name in ("labwc", "dbus-run-session", "grim", "wlr-randr", "wayvnc") if not shutil.which(name)]
        if missing:
            raise DesktopError(f"cannot create a native headless desktop; missing binaries: {', '.join(missing)}")
        # Keep the runtime and compositor private to this session.  A failed
        # startup cleans up its own process group, never another desktop.
        self._runtime = tempfile.mkdtemp(prefix="tisplay-labwc-")
        os.chmod(self._runtime, 0o700)
        runtime = Path(self._runtime) / "runtime"
        config = Path(self._runtime) / "config"
        (config / "labwc").mkdir(parents=True, mode=0o700)
        runtime.mkdir(mode=0o700)
        self.environment = {
            "WAYLAND_DISPLAY": "wayland-0",
            "XDG_RUNTIME_DIR": str(runtime),
            "XDG_CONFIG_HOME": str(config),
            "LABWC_FALLBACK_OUTPUT": "NOOP-tisplay",
            "WLR_BACKENDS": "headless",
            # With zero backend outputs, labwc's own fallback-output support
            # creates the NOOP output rather than wlroots naming it HEADLESS-1.
            "WLR_HEADLESS_OUTPUTS": "0",
            "WLR_HEADLESS_OUTPUT_WIDTH": str(self.width),
            "WLR_HEADLESS_OUTPUT_HEIGHT": str(self.height),
            "WLR_RENDERER": "pixman",
            "WLR_RENDERER_ALLOW_SOFTWARE": "1",
            # Bypass the distro helper that can create files in ~/.config
            # before it execs Xwayland. The native compositor still starts
            # Xwayland with its own generated authorization arguments.
            "WLR_XWAYLAND": shutil.which("Xwayland") or "",
            "XDG_SESSION_TYPE": "wayland",
            "XDG_CURRENT_DESKTOP": "labwc:wlroots",
            "TISPLAY_WAYLAND_OUTPUT": "NOOP-tisplay",
        }
        launch_env = os.environ.copy()
        for key in ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS", "DBUS_SESSION_BUS_PID", "SESSION_MANAGER"):
            launch_env.pop(key, None)
        launch_env.update(self.environment)
        self._log = tempfile.TemporaryFile(mode="w+b")
        command = [shutil.which("dbus-run-session") or "dbus-run-session", "--", shutil.which("labwc") or "labwc", "-m"]
        try:
            self.process = subprocess.Popen(command, env=launch_env, stdin=subprocess.DEVNULL,
                                            stdout=self._log, stderr=subprocess.STDOUT,
                                            start_new_session=True)
        except OSError as exc:
            self.close()
            raise DesktopError(f"Cannot start an isolated headless labwc session: {exc}") from exc
        deadline = time.monotonic() + 20
        saw_output = False
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                break
            if Path(self.environment["XDG_RUNTIME_DIR"], self.environment["WAYLAND_DISPLAY"]).exists():
                try:
                    outputs = _outputs(self.environment)
                    if outputs:
                        self.outputs = outputs
                        try: self.monitor = _select_output(outputs, True)
                        except DesktopError: pass
                        else:
                            mode = f"{self.width}x{self.height}"
                            try:
                                subprocess.run([shutil.which("wlr-randr") or "wlr-randr", "--output", self.monitor["name"], "--custom-mode", mode], env=self.environment, capture_output=True, timeout=3)
                                outputs = _outputs(self.environment)
                                self.outputs = outputs
                                self.monitor = _select_output(outputs, True)
                            except (OSError, subprocess.TimeoutExpired, DesktopError):
                                pass
                            bus_address = self._find_session_bus()
                            if bus_address:
                                self.environment["DBUS_SESSION_BUS_ADDRESS"] = bus_address
                            try:
                                image = self.frame(max_width=64)
                                saw_output = True
                                if self._visible_frame(image):
                                    self.managed = self.owned = True
                                    return
                            except DesktopError:
                                pass
                except DesktopError:
                    pass
            time.sleep(.1)
        detail = self._log_text()
        self.close()
        reason = "isolated labwc output remained blank" if saw_output else "isolated labwc did not create a capturable headless output"
        raise DesktopError(reason + (f"; startup log: {detail}" if detail else ""))

    @staticmethod
    def _visible_frame(image: Image.Image) -> bool:
        sample = image.resize((32, 24))
        colors = set(sample.getdata())
        return len(colors) > 1 and any(max(color) > 16 for color in colors)

    def _log_text(self) -> str:
        if not self._log: return ""
        try:
            self._log.flush(); self._log.seek(0)
            return self._log.read(3000).decode(errors="replace").strip()
        except OSError:
            return ""

    def _find_session_bus(self) -> str | None:
        runtime = self.environment["XDG_RUNTIME_DIR"]
        for pid in _processes_with_runtime(runtime):
            try:
                raw = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
                for item in raw:
                    if item.startswith(b"DBUS_SESSION_BUS_ADDRESS="):
                        value = item.partition(b"=")[2].decode(errors="replace")
                        if value: return value
            except (OSError, PermissionError):
                continue
        bus = Path(runtime) / "bus"
        return f"unix:path={bus}" if bus.exists() else None

    @property
    def environment_for_session(self) -> dict[str, str | None]:
        return {key: self.environment.get(key) for key in ("DISPLAY", "XAUTHORITY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME", "TISPLAY_WAYLAND_OUTPUT", "DBUS_SESSION_BUS_ADDRESS")}

    def __enter__(self) -> "NativeDisplay":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        if self._runtime:
            runtime = self.environment.get("XDG_RUNTIME_DIR")
            if runtime:
                _signal_runtime_groups(runtime, 15)
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and _processes_with_runtime(runtime):
                    time.sleep(.05)
                remaining = _processes_with_runtime(runtime)
                if remaining:
                    _signal_runtime_groups(runtime, 9)
            else:
                try: os.killpg(self.process.pid, 15)
                except ProcessLookupError: pass
            if self.process:
                try: self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    try: os.killpg(self.process.pid, 9)
                    except ProcessLookupError: pass
                    self.process.wait(timeout=2)
            if runtime:
                for mount in (Path(runtime) / "doc", Path(runtime) / "gvfs"):
                    if mount.exists():
                        tool = shutil.which("fusermount3") or shutil.which("fusermount")
                        if tool:
                            subprocess.run([tool, "-u", str(mount)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2)
        self.process = None
        if self._log:
            self._log.close(); self._log = None
        if self._runtime:
            _cleanup_owned_runtime(self._runtime)
            self._runtime = None
        self.managed = self.owned = False

    def frame(self, max_width: int | None = None) -> Image.Image:
        # Keep capture and injection on the same named output, even on a
        # multi-monitor desktop; output selection changes are detected here.
        try:
            outputs = _outputs(self.environment)
            monitor = next((o for o in outputs if o["name"] == self.monitor["name"]), None)
            if not monitor: raise DesktopError(f"selected Wayland output disappeared: {self.monitor['name']}")
            self.outputs, self.monitor = outputs, monitor
            result = subprocess.run([shutil.which("grim") or "grim", "-o", monitor["name"], "-t", "png", "-"], capture_output=True, timeout=10, env=self.environment)
            if result.returncode:
                raise DesktopError(f"grim capture failed: {result.stderr.decode(errors='replace').strip()}")
            from io import BytesIO
            image = Image.open(BytesIO(result.stdout)).convert("RGB")
        except DesktopError:
            raise
        except Exception as exc:
            raise DesktopError(f"Cannot capture the Wayland desktop with grim: {exc}") from exc
        if max_width is not None and image.width > max_width:
            height = max(1, round(image.height * max_width / image.width))
            image = image.resize((max_width, height), Image.Resampling.LANCZOS)
        return image


class _Rfb:
    """Small RFB 3.8 event client used only for pointer and key injection."""
    KEY_SYMS = {
        "backspace": 0xFF08, "tab": 0xFF09, "enter": 0xFF0D, "return": 0xFF0D,
        "escape": 0xFF1B, "esc": 0xFF1B, "delete": 0xFFFF, "home": 0xFF50,
        "left": 0xFF51, "up": 0xFF52, "right": 0xFF53, "down": 0xFF54,
        "page_up": 0xFF55, "page_down": 0xFF56, "end": 0xFF57, "insert": 0xFF63,
        "space": 0x20, "shift": 0xFFE1, "ctrl": 0xFFE3, "control": 0xFFE3,
        "meta": 0xFFE7, "super": 0xFFEB, "cmd": 0xFFEB, "alt": 0xFFE9,
    }

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.width = self.height = 0
        self._handshake()

    def _read(self, n: int) -> bytes:
        parts = bytearray()
        while len(parts) < n:
            data = self.sock.recv(n - len(parts))
            if not data: raise DesktopError("WayVNC closed the input connection")
            parts.extend(data)
        return bytes(parts)

    def _handshake(self) -> None:
        banner = self._read(12)
        if banner != b"RFB 003.008\n":
            raise DesktopError("WayVNC returned an unsupported RFB protocol banner")
        self.sock.sendall(banner)
        count = self._read(1)[0]
        if not count: raise DesktopError("WayVNC rejected the RFB connection")
        security = self._read(count)
        if 1 not in security:
            raise DesktopError("WayVNC requires an authentication method that tisplay does not support")
        self.sock.sendall(b"\x01")
        result = struct.unpack(">I", self._read(4))[0]
        if result:
            length = struct.unpack(">I", self._read(4))[0]
            reason = self._read(min(length, 2048)).decode(errors="replace")
            raise DesktopError(f"WayVNC rejected the private input connection: {reason}")
        self.sock.sendall(b"\x01")  # shared session
        header = self._read(24)
        self.width, self.height = struct.unpack(">HH", header[:4])
        name_len = struct.unpack(">I", header[20:24])[0]
        if name_len > 65536: raise DesktopError("WayVNC framebuffer name is too long")
        if name_len: self._read(name_len)
        # Complete the client setup before emitting input. Several wlroots RFB
        # servers defer seat event handling until a pixel format and encoding
        # have been negotiated.
        pixel_format = bytes((32, 24, 0, 1)) + struct.pack(">HHH", 255, 255, 255) + bytes((16, 8, 0, 0, 0, 0))
        self.sock.sendall(b"\x00\x00\x00\x00" + pixel_format)
        self.sock.sendall(struct.pack(">BBH", 2, 0, 1) + struct.pack(">i", 0))  # raw encoding
        # Ask for one pixel only. Capture is handled by grim, so reading a full
        # framebuffer here would waste bandwidth and could stall the RFB event
        # loop while the client only needs input.
        self.sock.sendall(struct.pack(">BBHHHH", 3, 0, 0, 0, 1, 1))
        self._read_initial_update()

    def _read_initial_update(self) -> None:
        # Drain the bounded 1x1 response so WayVNC finishes client setup before
        # it processes the controller's pointer and keyboard messages.
        for _ in range(4):
            message_type = self._read(1)[0]
            if message_type == 2:  # Bell
                continue
            if message_type != 0:
                raise DesktopError(f"WayVNC sent unsupported RFB server message {message_type}")
            update_header = self._read(3)
            padding, count = update_header[0], struct.unpack(">H", update_header[1:])[0]
            if padding != 0 or count > 16:
                raise DesktopError("WayVNC returned an invalid framebuffer update header")
            for _ in range(count):
                rect = self._read(12)
                x, y, width, height = struct.unpack(">HHHH", rect[:8])
                encoding = struct.unpack(">i", rect[8:])[0]
                if encoding == 0:
                    if width * height > 1_000_000:
                        raise DesktopError("WayVNC initial framebuffer update exceeded its size limit")
                    self._read(width * height * 4)
                elif encoding == -224:  # LastRect pseudo-encoding
                    break
                else:
                    raise DesktopError(f"WayVNC sent unsupported framebuffer encoding {encoding}")
            return
        raise DesktopError("WayVNC did not send a framebuffer update")

    @staticmethod
    def keysym(name: str) -> int:
        lower = name.lower()
        if lower in _Rfb.KEY_SYMS: return _Rfb.KEY_SYMS[lower]
        if lower.startswith("f") and lower[1:].isdigit() and 1 <= int(lower[1:]) <= 24:
            return 0xFFBD + int(lower[1:])
        if len(name) == 1:
            code = ord(name)
            return code if code <= 0xff else 0x01000000 | code
        raise DesktopError(f"Unsupported Wayland key name: {name}")

    def key(self, name: str, down: bool) -> None:
        self.sock.sendall(struct.pack(">BBHI", 4, int(down), 0, self.keysym(name)))

    def pointer(self, mask: int, x: int, y: int) -> None:
        x = max(0, min(int(x), max(0, self.width - 1)))
        y = max(0, min(int(y), max(0, self.height - 1)))
        self.sock.sendall(struct.pack(">BBHH", 5, mask, x, y))


class NativeWaylandController:
    """RFB-backed Wayland input controller with the engine controller API."""
    BUTTONS = {"left": 1, "middle": 2, "right": 4}

    def __init__(self, environment: dict[str, str] | None = None) -> None:
        info = native_available(environment)
        if not info["available"]: raise DesktopError(info["reason"])
        self._tmp = tempfile.TemporaryDirectory(prefix="tisplay-wayvnc-")
        os.chmod(self._tmp.name, 0o700)
        self._socket_path = str(Path(self._tmp.name) / "input.sock")
        self._env = os.environ.copy()
        self._env.update(info["environment"])
        target = info["environment"].get("TISPLAY_WAYLAND_OUTPUT")
        self.monitor = next((o for o in info["outputs"] if o["name"] == target), None)
        if self.monitor is None:
            self.monitor = _select_output(info["outputs"], info["headless"])
        self.button_mask = 0
        self._log = tempfile.TemporaryFile(mode="w+b")
        config = Path(self._tmp.name) / "wayvnc.conf"
        config.write_text("", encoding="utf-8")
        config.chmod(0o600)
        self.button_mask = 0
        level = os.environ.get("TISPLAY_WAYVNC_LOG_LEVEL", "error").lower()
        if level not in {"error", "warning", "info", "debug", "trace", "quiet"}: level = "error"
        command = [shutil.which("wayvnc") or "wayvnc", "--config", str(config), "--log-level", level,
                   "--output", self.monitor["name"], "--disable-resizing", "--unix-socket",
                   "--socket", str(Path(self._tmp.name) / "control.sock"), self._socket_path]
        try:
            self.process = subprocess.Popen(command, env=self._env, stdin=subprocess.DEVNULL,
                                            stdout=self._log, stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as exc:
            self._log.close()
            self._tmp.cleanup()
            raise DesktopError(f"Cannot start private WayVNC input endpoint: {exc}") from exc
        try:
            deadline = time.monotonic() + 5
            while not Path(self._socket_path).exists() and time.monotonic() < deadline:
                if self.process.poll() is not None: break
                time.sleep(.05)
            if not Path(self._socket_path).exists():
                detail = self._log_text()
                raise DesktopError(f"Private WayVNC input endpoint did not start{': ' + detail if detail else ''}")
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(5)
            self.sock.connect(self._socket_path)
            self.rfb = _Rfb(self.sock)
        except Exception:
            self.close()
            raise

    def _log_text(self) -> str:
        try:
            self._log.flush(); self._log.seek(0)
            return self._log.read(2000).decode(errors="replace").strip()
        except (OSError, AttributeError):
            return ""

    def _pointer(self, mask: int, x: int, y: int) -> None:
        self.rfb.pointer(mask, x - int(self.monitor.get("left", 0)), y - int(self.monitor.get("top", 0)))

    def key(self, name: str, down: bool) -> None:
        self.rfb.key(name, down)

    def button(self, name: str, down: bool, x: int, y: int) -> None:
        mask = self.BUTTONS.get(name)
        if mask is not None:
            if down: self.button_mask |= mask
            else: self.button_mask &= ~mask
            self._pointer(self.button_mask, x, y)
        elif down and name in ("wheel_up", "wheel_down"):
            wheel_mask = 8 if name == "wheel_up" else 16
            self._pointer(self.button_mask | wheel_mask, x, y)
            self._pointer(self.button_mask, x, y)

    def move(self, x: int, y: int) -> None:
        self._pointer(self.button_mask, x, y)

    def scroll(self, x: int, y: int, delta_x: int, delta_y: int) -> None:
        self.move(x, y)
        for mask, count in ((32 if delta_x < 0 else 64, abs(delta_x)), (8 if delta_y > 0 else 16, abs(delta_y))):
            for _ in range(min(count, 100)):
                self._pointer(self.button_mask | mask, x, y); self._pointer(self.button_mask, x, y)

    def release_button(self, name: str) -> None:
        mask = self.BUTTONS.get(name)
        if mask is not None: self.button_mask &= ~mask
        self._pointer(self.button_mask, 0, 0)

    def type_text(self, text: str) -> None:
        for char in text:
            code = ord(char)
            if code == 0 or code > 0x10ffff: continue
            key = chr(code) if code <= 0xff else f"U+{code:04X}"
            if key == "\n": key = "enter"
            elif key == "\t": key = "tab"
            # RFB Unicode keysyms follow the X11 0x01000000 encoding.
            if key.startswith("U+"):
                keysym = 0x01000000 | code
                self.sock.sendall(struct.pack(">BBHI", 4, 1, 0, keysym)); self.sock.sendall(struct.pack(">BBHI", 4, 0, 0, keysym))
            else:
                self.key(key, True); self.key(key, False)

    def close(self) -> None:
        sock = getattr(self, "sock", None)
        if sock:
            try: sock.close()
            except OSError: pass
            self.sock = None
        process = getattr(self, "process", None)
        if process and process.poll() is None:
            try: os.killpg(process.pid, 15)
            except ProcessLookupError: pass
            try: process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try: os.killpg(process.pid, 9)
                except ProcessLookupError: pass
                process.wait(timeout=2)
        log = getattr(self, "_log", None)
        if log: log.close(); self._log = None
        tmp = getattr(self, "_tmp", None)
        if tmp: tmp.cleanup(); self._tmp = None
