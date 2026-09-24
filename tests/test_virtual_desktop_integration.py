"""Real Xvfb/Xfce integration; CI installs the complete Linux desktop stack."""

import os
import shutil
import subprocess
import time

import pytest

from tisplay.capture import DesktopError, Screen, VirtualDisplay, XTestController


REQUIRED = ("Xvfb", "xauth", "dbus-run-session", "startxfce4", "xfce4-panel", "xprop", "pgrep", "xterm", "xdotool")


@pytest.mark.skipif(os.name != "posix" or not hasattr(os, "uname") or os.uname().sysname != "Linux", reason="Linux desktop integration")
def test_virtual_xfce_capture_and_input(tmp_path):
    missing = [name for name in REQUIRED if not shutil.which(name)]
    if missing:
        pytest.skip(f"Linux desktop integration requirements missing: {', '.join(missing)}")

    marker = tmp_path / "input-received"
    ready = tmp_path / "input-ready"
    with VirtualDisplay(1024, 768) as desktop:
        screen = Screen()
        frame = screen.frame()
        colors = frame.getcolors(maxcolors=1_000_000)
        assert colors and len(colors) > 8, "Xfce capture is blank or uniform"
        assert any(max(pixel) > 32 for _, pixel in colors), "captured desktop is entirely black"

        tree = subprocess.run([shutil.which("xwininfo"), "-root", "-tree"], capture_output=True, text=True, check=True)
        assert "xfce4-panel" in tree.stdout.lower() or subprocess.run([shutil.which("pgrep"), "-u", str(os.getuid()), "-x", "xfce4-panel"]).returncode == 0

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
        time.sleep(0.2)
        controller = XTestController()
        try:
            controller.key("t", True)
            controller.key("t", False)
            time.sleep(0.1)
            controller.key("enter", True)
            controller.key("enter", False)
        finally:
            controller.close()
        for _ in range(30):
            if marker.exists():
                break
            time.sleep(0.1)
        assert marker.exists() and marker.read_text() == "t", "XTest input did not reach the application"
