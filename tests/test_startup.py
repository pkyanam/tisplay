from argparse import Namespace
from pathlib import Path
import subprocess
import sys
import time

import pytest

from tisplay import cli
from tisplay.capture import VirtualDisplay


def args(virtual=False, command=None):
    return Namespace(virtual=virtual, width=1280, height=800, command=command or [])


def test_wm_probe_accepts_xprop_window_formats_and_rejects_zero():
    assert VirtualDisplay._wm_property_is_set(0, "_NET_SUPPORTING_WM_CHECK = 0x600032")
    assert VirtualDisplay._wm_property_is_set(0, "_NET_SUPPORTING_WM_CHECK: window id # 0x600032")
    assert not VirtualDisplay._wm_property_is_set(0, "_NET_SUPPORTING_WM_CHECK: window id # 0x0")
    assert not VirtualDisplay._wm_property_is_set(1, "_NET_SUPPORTING_WM_CHECK = 0x600032")


def test_main_turns_keyboard_interrupt_into_clean_exit(monkeypatch, capsys):
    def interrupt(_args):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "run", interrupt)
    monkeypatch.setattr(cli.sys, "argv", ["tisplay"])
    try:
        cli.main()
    except SystemExit as exc:
        assert exc.code == 130
    else:
        raise AssertionError("Ctrl-C did not set the interrupted exit status")
    stderr = capsys.readouterr().err
    assert "interrupted; cleanup completed" in stderr
    assert "Traceback" not in stderr


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux process-group cleanup")
def test_virtual_display_cleanup_kills_group_descendants_after_leader_exit():
    child_code = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready', flush=True); time.sleep(30)"
    leader_code = (
        "import subprocess,sys; "
        f"child=subprocess.Popen([sys.executable, '-c', {child_code!r}], stdout=subprocess.PIPE, text=True); "
        "print(child.pid, flush=True); print(child.stdout.readline().strip(), flush=True)"
    )
    leader = subprocess.Popen([sys.executable, "-c", leader_code], stdout=subprocess.PIPE, text=True, start_new_session=True)
    assert leader.stdout is not None
    child_pid = int(leader.stdout.readline().strip())
    assert leader.stdout.readline().strip() == "ready"
    assert leader.wait(timeout=2) == 0

    display = VirtualDisplay()
    display.command = leader
    display.__exit__(None, None, None)

    stat = Path(f"/proc/{child_pid}/stat")
    for _ in range(20):
        if not stat.exists() or stat.read_text().rsplit(")", 1)[1].split()[0] == "Z":
            break
        time.sleep(0.05)
    assert not stat.exists() or stat.read_text().rsplit(")", 1)[1].split()[0] == "Z"


def test_unset_display_uses_accessible_x11_socket(monkeypatch):
    monkeypatch.setattr(cli.sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr(cli, "discover_accessible_x11_display", lambda: ":0")
    monkeypatch.setattr(cli, "VirtualDisplay", lambda *_: (_ for _ in ()).throw(AssertionError("unexpected Xvfb")))

    with cli.startup_display(args()) as (display, use_virtual):
        assert display is None
        assert not use_virtual
        assert cli.os.environ["DISPLAY"] == ":0"
    assert "DISPLAY" not in cli.os.environ


def test_unset_display_falls_back_to_private_virtual_desktop(monkeypatch):
    monkeypatch.setattr(cli.sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr(cli, "discover_accessible_x11_display", lambda: None)
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/usr/bin/{name}")

    class FakeVirtual:
        def __init__(self, *_):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

    monkeypatch.setattr(cli, "VirtualDisplay", FakeVirtual)
    with cli.startup_display(args()) as (display, use_virtual):
        assert isinstance(display, FakeVirtual)
        assert use_virtual


def test_explicit_invalid_display_is_not_replaced(monkeypatch):
    monkeypatch.setattr(cli.sys, "platform", "linux")
    monkeypatch.setenv("DISPLAY", ":99")
    monkeypatch.setattr(cli, "discover_accessible_x11_display", lambda: (_ for _ in ()).throw(AssertionError("probe should not run")))
    monkeypatch.setattr(cli, "VirtualDisplay", lambda *_: (_ for _ in ()).throw(AssertionError("unexpected Xvfb")))

    with cli.startup_display(args()) as (display, use_virtual):
        assert display is None
        assert not use_virtual
        assert cli.os.environ["DISPLAY"] == ":99"


def test_macos_keeps_existing_startup_behavior(monkeypatch):
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr(cli, "discover_accessible_x11_display", lambda: (_ for _ in ()).throw(AssertionError("probe should not run")))
    monkeypatch.setattr(cli, "VirtualDisplay", lambda *_: (_ for _ in ()).throw(AssertionError("unexpected Xvfb")))

    with cli.startup_display(args()) as (display, use_virtual):
        assert display is None
        assert not use_virtual
        assert "DISPLAY" not in cli.os.environ
