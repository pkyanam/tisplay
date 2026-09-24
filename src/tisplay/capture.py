"""Screen capture and input injection backends."""

from __future__ import annotations

import os
import shutil
import secrets
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import mss
from PIL import Image


class DesktopError(RuntimeError):
    pass


class Screen:
    def __init__(self, allow_wayland: bool = False) -> None:
        if os.uname().sysname == "Linux" and os.environ.get("WAYLAND_DISPLAY") and not allow_wayland:
            raise DesktopError("Direct Wayland capture is not supported. Use an X11 session or --virtual.")
        try:
            self.grabber = mss.MSS()
            monitors = self.grabber.monitors
            if len(monitors) < 2:
                raise DesktopError("No display is available to capture.")
            # MSS index 0 is the union. On X11, xrandr identifies the actual
            # primary output; on macOS MSS lists the primary display first.
            self.monitor = monitors[1]
            if os.uname().sysname == "Linux":
                xrandr = shutil.which("xrandr")
                if xrandr:
                    output = subprocess.run([xrandr, "--query"], capture_output=True, text=True, timeout=2)
                    for line in output.stdout.splitlines():
                        if " connected primary " not in line:
                            continue
                        import re
                        match = re.search(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)", line)
                        if match:
                            w, h, left, top = map(int, match.groups())
                            found = next((m for m in monitors[1:] if (m["width"], m["height"], m["left"], m["top"]) == (w, h, left, top)), None)
                            if found:
                                self.monitor = found
                        break
        except Exception as exc:
            if isinstance(exc, DesktopError):
                raise
            raise DesktopError(f"Cannot open the display ({exc}). On Linux, use an X11 display or --virtual.") from exc

    def frame(self, max_width: int = 1920) -> Image.Image:
        try:
            shot = self.grabber.grab(self.monitor)
        except Exception as exc:
            raise DesktopError(f"Screen capture failed: {exc}") from exc
        image = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        if image.width > max_width:
            height = max(1, round(image.height * max_width / image.width))
            image = image.resize((max_width, height), Image.Resampling.LANCZOS)
        return image


def discover_accessible_x11_display() -> str | None:
    """Find a same-user local X11 display when SSH left DISPLAY unset.

    We rely on normal Xauthority checks when probing local sockets. We never
    relax X server access control or inspect another user's session credentials.
    """
    if os.uname().sysname != "Linux" or os.environ.get("DISPLAY"):
        return None
    socket_dir = Path("/tmp/.X11-unix")
    if not socket_dir.is_dir():
        return None
    original_display = os.environ.get("DISPLAY")
    for socket_path in sorted(socket_dir.glob("X*")):
        suffix = socket_path.name[1:]
        if not suffix.isdigit():
            continue
        display_name = f":{suffix}"
        os.environ["DISPLAY"] = display_name
        try:
            with mss.MSS():
                return display_name
        except Exception:
            pass
        finally:
            if original_display is None:
                os.environ.pop("DISPLAY", None)
            else:
                os.environ["DISPLAY"] = original_display
    return None

class XTestController:
    """Inject pointer and key events into the current X display via XTest."""

    def __init__(self) -> None:
        try:
            from Xlib import X, XK, display
            from Xlib.ext import xtest
            self.X, self.XK, self.xtest = X, XK, xtest
            self.display = display.Display()
        except Exception as exc:
            raise DesktopError(f"Cannot initialize X11 input (install tisplay[linux] and ensure DISPLAY is set): {exc}") from exc

    def key(self, name: str, down: bool) -> None:
        aliases = {
            "enter": "Return", "backspace": "BackSpace", "delete": "Delete", "ctrl": "Control_L",
            "tab": "Tab", "up": "Up", "down": "Down", "left": "Left", "right": "Right",
            "home": "Home", "end": "End", "page_up": "Page_Up", "page_down": "Page_Down",
            "insert": "Insert", "escape": "Escape",
        }
        keysym = self.XK.string_to_keysym(aliases.get(name, name))
        if not keysym:
            if len(name) == 1:
                keysym = ord(name)
            else:
                return
        keycode = self.display.keysym_to_keycode(keysym)
        if keycode:
            shifted = name.isupper() or name in "~!@#$%^&*()_+{}|:\"<>?"
            shift_code = self.display.keysym_to_keycode(self.XK.string_to_keysym("Shift_L"))
            if shifted and down:
                self.xtest.fake_input(self.display, self.X.KeyPress, shift_code)
            self.xtest.fake_input(self.display, self.X.KeyPress if down else self.X.KeyRelease, keycode)
            if shifted and not down:
                self.xtest.fake_input(self.display, self.X.KeyRelease, shift_code)
            self.display.flush()

    def button(self, name: str, down: bool, x: int, y: int) -> None:
        root = self.display.screen().root
        root.warp_pointer(x - self.display.screen().root.get_geometry().x, y - self.display.screen().root.get_geometry().y)
        number = {"left": 1, "middle": 2, "right": 3, "wheel_up": 4, "wheel_down": 5}.get(name)
        if number:
            self.xtest.fake_input(self.display, self.X.ButtonPress if down else self.X.ButtonRelease, number)
        self.display.flush()

    def close(self) -> None:
        self.display.close()


class PynputController:
    """Physical desktop input backend (Quartz on macOS, fallback elsewhere)."""

    KEY_NAMES = {
        "enter": "enter", "tab": "tab", "backspace": "backspace", "delete": "delete",
        "escape": "esc", "up": "up", "down": "down", "left": "left", "right": "right",
        "ctrl": "ctrl",
        "home": "home", "end": "end", "page_up": "page_up", "page_down": "page_down",
        "insert": "insert", "f1": "f1", "f2": "f2", "f3": "f3", "f4": "f4",
        "f5": "f5", "f6": "f6", "f7": "f7", "f8": "f8", "f9": "f9", "f10": "f10",
    }

    def __init__(self) -> None:
        try:
            from pynput.keyboard import Controller as Keyboard, Key
            from pynput.mouse import Button, Controller as Mouse
            self.keyboard = Keyboard()
            self.mouse = Mouse()
            self.Key, self.Button = Key, Button
        except Exception as exc:
            raise DesktopError(f"Cannot initialize desktop input: {exc}") from exc

    def key(self, name: str, down: bool) -> None:
        key = getattr(self.Key, self.KEY_NAMES.get(name, ""), None)
        if key is None and len(name) == 1:
            key = name
        if key is not None:
            (self.keyboard.press if down else self.keyboard.release)(key)

    def button(self, name: str, down: bool, x: int, y: int) -> None:
        self.mouse.position = (x, y)
        button = {"left": self.Button.left, "middle": self.Button.middle, "right": self.Button.right}.get(name)
        if button:
            (self.mouse.press if down else self.mouse.release)(button)
        elif down and name in ("wheel_up", "wheel_down"):
            self.mouse.scroll(0, 1 if name == "wheel_up" else -1)

    def close(self) -> None:
        pass


def make_controller() -> XTestController | PynputController:
    if os.name == "posix" and os.uname().sysname == "Linux":
        return XTestController()
    return PynputController()


@dataclass
class VirtualDisplay:
    width: int = 1280
    height: int = 800
    process: subprocess.Popen | None = None
    session: subprocess.Popen | None = None
    command: subprocess.Popen | None = None
    old_display: str | None = None
    old_xauthority: str | None = None
    _auth_dir: str | None = None
    _logs: dict[str, object] | None = None

    def __enter__(self) -> "VirtualDisplay":
        if os.uname().sysname != "Linux":
            raise DesktopError("--virtual is supported on Linux with Xvfb. macOS cannot create a headless virtual desktop.")
        missing = [name for name in ("Xvfb", "xauth", "dbus-run-session", "startxfce4", "xfce4-panel", "xprop", "pgrep") if not shutil.which(name)]
        if missing:
            raise DesktopError(f"Full virtual desktop dependencies missing ({', '.join(missing)}). Install tisplay's Linux dependencies, including Xfce and D-Bus.")
        xvfb = shutil.which("Xvfb")
        self.old_display = os.environ.get("DISPLAY")
        existing = {int(p.name[1:]) for p in Path("/tmp/.X11-unix").glob("X*") if p.name[1:].isdigit()} if Path("/tmp/.X11-unix").exists() else set()
        display_no = next((n for n in range(90, 120) if n not in existing), None)
        if display_no is None:
            raise DesktopError("No free X display number in the range :90-:119.")
        display_name = f":{display_no}"
        self.old_xauthority = os.environ.get("XAUTHORITY")
        self._auth_dir = tempfile.mkdtemp(prefix="tisplay-xauth-")
        auth_file = os.path.join(self._auth_dir, "Xauthority")
        cookie = secrets.token_hex(16)
        auth_add = subprocess.run([shutil.which("xauth"), "-f", auth_file, "add", display_name, ".", cookie], capture_output=True, text=True)
        if auth_add.returncode:
            shutil.rmtree(self._auth_dir, ignore_errors=True)
            raise DesktopError(f"Could not create X11 authorization for the virtual display: {auth_add.stderr.strip()}")
        os.environ["XAUTHORITY"] = auth_file
        self._logs = {
            "Xvfb": tempfile.TemporaryFile(mode="w+t"),
            "Xfce": tempfile.TemporaryFile(mode="w+t"),
            "application": tempfile.TemporaryFile(mode="w+t"),
        }
        try:
            self.process = subprocess.Popen([xvfb, display_name, "-screen", "0", f"{self.width}x{self.height}x24", "-nolisten", "tcp", "-auth", auth_file], env=os.environ.copy(), stdout=self._logs["Xvfb"], stderr=subprocess.STDOUT)
            os.environ["DISPLAY"] = display_name
            for _ in range(100):
                if self.process.poll() is not None:
                    raise self._startup_error("Xvfb exited while starting the virtual display.")
                try:
                    with mss.MSS():
                        break
                except Exception:
                    time.sleep(0.1)
            else:
                raise self._startup_error("Xvfb did not become ready within 10 seconds.")
            self.session = subprocess.Popen([shutil.which("dbus-run-session"), "--", shutil.which("startxfce4")], env=os.environ.copy(), stdout=self._logs["Xfce"], stderr=subprocess.STDOUT)
            deadline = time.monotonic() + 25
            ready = False
            while time.monotonic() < deadline:
                if self.session.poll() is not None:
                    raise self._startup_error("Xfce exited while starting the headless desktop.")
                try:
                    wm = subprocess.run([shutil.which("xprop"), "-root", "_NET_SUPPORTING_WM_CHECK"], capture_output=True, text=True, timeout=1)
                    panel = subprocess.run([shutil.which("pgrep"), "-u", str(os.getuid()), "-x", "xfce4-panel"], capture_output=True, text=True, timeout=1)
                    panel_running = any(self._process_state(pid) not in (None, "Z") for pid in panel.stdout.split())
                    if wm.returncode == 0 and "window id" in wm.stdout.lower() and panel_running:
                        ready = True
                        break
                except (OSError, subprocess.TimeoutExpired):
                    pass
                time.sleep(0.2)
            if not ready:
                raise self._startup_error("Xfce did not finish starting (window manager and panel were not ready within 25 seconds).")
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if self._has_visible_desktop():
                    return self
                if self.session.poll() is not None:
                    raise self._startup_error("Xfce exited before its desktop produced a visible frame.")
                time.sleep(0.2)
            raise self._startup_error("Xfce started its window manager and panel, but the virtual screen remained blank for 10 seconds.")
        except Exception:
            self.__exit__(None, None, None)
            raise

    def launch(self, argv: list[str]) -> None:
        if argv:
            try:
                self.command = subprocess.Popen(argv, env=os.environ.copy(), stdin=subprocess.DEVNULL,
                                                stdout=self._logs["application"], stderr=subprocess.STDOUT)
            except OSError as exc:
                raise DesktopError(f"Could not launch desktop command {argv[0]!r}: {exc}") from exc

    def check_command(self) -> None:
        if not self.command or self.command.poll() is None or self.command.returncode == 0:
            return
        stream = self._logs["application"]
        stream.seek(0)
        output = stream.read().strip()
        detail = f"\nApplication output:\n{output[-3000:]}" if output else ""
        raise DesktopError(f"Desktop command exited with status {self.command.returncode}.{detail}")

    def _startup_error(self, message: str) -> DesktopError:
        details = []
        for name, stream in (self._logs or {}).items():
            if name == "application":
                continue
            stream.seek(0)
            content = stream.read().strip()
            if content:
                details.append(f"{name}: {content[-3000:]}")
        return DesktopError(message + ("\nStartup output:\n" + "\n".join(details) if details else " Check that the installed X11 and Xfce packages match your distribution."))

    @staticmethod
    def _has_visible_desktop() -> bool:
        try:
            with mss.MSS() as grabber:
                monitor = grabber.monitors[1]
                shot = grabber.grab(monitor)
            sample = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX").resize((32, 24))
            colors = set(sample.get_flattened_data())
            return len(colors) > 1 and any(max(color) > 16 for color in colors)
        except Exception:
            return False

    @staticmethod
    def _process_state(pid: str) -> str | None:
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
            return stat.rsplit(")", 1)[1].split()[0]
        except (OSError, IndexError):
            return None

    def __exit__(self, *_: object) -> None:
        for process in (self.command, self.session, self.process):
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
        if self.old_display is None:
            os.environ.pop("DISPLAY", None)
        else:
            os.environ["DISPLAY"] = self.old_display
        if self.old_xauthority is None:
            os.environ.pop("XAUTHORITY", None)
        else:
            os.environ["XAUTHORITY"] = self.old_xauthority
        if self._auth_dir:
            shutil.rmtree(self._auth_dir, ignore_errors=True)
        for stream in (self._logs or {}).values():
            stream.close()
