"""Real Xvfb/Xfce integration; CI installs the complete Linux desktop stack."""

import os
import shutil
import subprocess
import time

import pytest

from tisplay.capture import DesktopError, Screen, VirtualDisplay, XTestController
from tisplay.cli import process_input


REQUIRED = ("Xvfb", "xauth", "dbus-run-session", "startxfce4", "xfce4-panel", "xprop", "xwininfo", "xterm", "xdotool")


@pytest.mark.skipif(os.name != "posix" or not hasattr(os, "uname") or os.uname().sysname != "Linux", reason="Linux desktop integration")
def test_virtual_xfce_capture_and_input(tmp_path):
    missing = [name for name in REQUIRED if not shutil.which(name)]
    if missing:
        pytest.skip(f"Linux desktop integration requirements missing: {', '.join(missing)}")

    marker = tmp_path / "input-received"
    ready = tmp_path / "input-ready"
    with VirtualDisplay(1024, 768) as desktop:
        screen = Screen(allow_wayland=True)
        frame = screen.frame()
        colors = frame.getcolors(maxcolors=1_000_000)
        assert colors and len(colors) > 8, "Xfce capture is blank or uniform"
        assert any(max(pixel) > 32 for _, pixel in colors), "captured desktop is entirely black"

        tree = subprocess.run([shutil.which("xwininfo"), "-root", "-tree"], capture_output=True, text=True, check=True)
        assert "xfce4-panel" in tree.stdout.lower(), "XFCE panel window is not present on the virtual display"

        desktop.launch(["sh", "-c", "echo expected-app-error >&2; exit 7"])
        for _ in range(30):
            if desktop.command.poll() is not None:
                break
            time.sleep(0.1)
        with pytest.raises(DesktopError, match="status 7") as error:
            desktop.check_command()
        assert "expected-app-error" in str(error.value)

        desktop.launch(["xterm", "-e", "sh", "-c", f'touch "{ready}"; read value; printf %s "$value" > "{marker}"; sleep 3'])
        window = None
        for _ in range(50):
            result = subprocess.run([shutil.which("xdotool"), "search", "--onlyvisible", "--class", "XTerm"], capture_output=True, text=True)
            if result.stdout.strip():
                window = result.stdout.splitlines()[0]
                break
            time.sleep(0.1)
        assert window, "launched application window did not appear"
        subprocess.run([shutil.which("xdotool"), "windowactivate", "--sync", window], check=True)
        subprocess.run([shutil.which("xdotool"), "windowfocus", "--sync", window], check=True)
        for _ in range(30):
            if ready.exists() and subprocess.run([shutil.which("xdotool"), "getwindowfocus"], capture_output=True, text=True).stdout.strip() == window:
                break
            time.sleep(0.1)
        assert ready.exists(), "launched application did not reach its input prompt"
        assert subprocess.run([shutil.which("xdotool"), "getwindowfocus"], capture_output=True, text=True).stdout.strip() == window, "launched application did not retain focus"
        time.sleep(0.2)
        controller = XTestController()
        try:
            assert process_input(bytearray(b"t\r"), controller, screen, 80, 24)
        finally:
            controller.close()
        for _ in range(30):
            if marker.exists():
                break
            time.sleep(0.1)
        assert marker.exists() and marker.read_text() == "t", "XTest input did not reach the application"


@pytest.mark.skipif(os.name != "posix" or not hasattr(os, "uname") or os.uname().sysname != "Linux", reason="Linux desktop integration")
def test_keyboard_interrupt_during_startup_cleans_processes_and_xauthority(monkeypatch):
    missing = [name for name in REQUIRED if not shutil.which(name)]
    if missing:
        pytest.skip(f"Linux desktop integration requirements missing: {', '.join(missing)}")

    old_display = os.environ.get("DISPLAY")
    old_xauthority = os.environ.get("XAUTHORITY")
    desktop = VirtualDisplay(1024, 768)

    def interrupt_after_window_manager():
        raise KeyboardInterrupt

    monkeypatch.setattr(VirtualDisplay, "_has_visible_desktop", staticmethod(interrupt_after_window_manager))
    with pytest.raises(KeyboardInterrupt):
        desktop.__enter__()

    assert desktop.process and desktop.process.poll() is not None
    assert desktop.session and desktop.session.poll() is not None
    assert desktop._auth_dir and not os.path.exists(desktop._auth_dir)
    assert os.environ.get("DISPLAY") == old_display
    assert os.environ.get("XAUTHORITY") == old_xauthority
