"""Screen capture and input injection backends."""

from __future__ import annotations

import os
import shutil
import subprocess
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
    wm: subprocess.Popen | None = None
    command: subprocess.Popen | None = None
    old_display: str | None = None

    def __enter__(self) -> "VirtualDisplay":
        if os.uname().sysname != "Linux":
            raise DesktopError("--virtual is supported on Linux with Xvfb. macOS cannot create a headless virtual desktop.")
        xvfb = shutil.which("Xvfb")
        if not xvfb:
            raise DesktopError("Xvfb is required for --virtual. Install it with: sudo apt install xvfb")
        self.old_display = os.environ.get("DISPLAY")
        existing = {int(p.name[1:]) for p in Path("/tmp/.X11-unix").glob("X*") if p.name[1:].isdigit()} if Path("/tmp/.X11-unix").exists() else set()
        display_no = next((n for n in range(90, 120) if n not in existing), None)
        if display_no is None:
            raise DesktopError("No free X display number in the range :90-:119.")
        display_name = f":{display_no}"
        try:
            self.process = subprocess.Popen([xvfb, display_name, "-screen", "0", f"{self.width}x{self.height}x24", "-nolisten", "tcp"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            os.environ["DISPLAY"] = display_name
            for _ in range(50):
                if self.process.poll() is not None:
                    raise DesktopError("Xvfb exited while starting the virtual display.")
                try:
                    with mss.MSS():
                        break
                except Exception:
                    time.sleep(0.1)
            else:
                raise DesktopError("Xvfb started but the display did not become ready.")
            openbox = shutil.which("openbox")
            if openbox:
                self.wm = subprocess.Popen([openbox], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                time.sleep(0.2)
            return self
        except Exception:
            self.__exit__(None, None, None)
            raise

    def launch(self, argv: list[str]) -> None:
        if argv:
            self.command = subprocess.Popen(argv, env=os.environ.copy())

    def __exit__(self, *_: object) -> None:
        for process in (self.command, self.wm, self.process):
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
