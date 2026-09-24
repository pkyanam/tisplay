from argparse import Namespace
import time
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

    monkeypatch.setattr(cli, "run_managed_default", interrupt)
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


def test_plain_viewer_reuses_idle_ttl_session_and_prints_usable_reconnect(monkeypatch):
    from tisplay import client as client_module

    existing = {"session_id": "session-123", "name": "interactive", "idle_ttl": 900,
                "idle_expires_at": time.time() + 600, "viewer_count": 0,
                "width": 1600, "height": 900, "mode": "auto"}

    class FakeClient:
        closed = False
        def capabilities(self): return {"session_idle_ttl": True, "viewer_leases": True}
        def list(self): return {"sessions": [existing]}
        def start(self, **kwargs): raise AssertionError("reusable session should be selected")
        def close(self): self.closed = True

    fake = FakeClient()
    monkeypatch.setattr(client_module, "SessionClient", lambda: fake)
    monkeypatch.setattr(cli.sys, "argv", ["/opt/tisplay"])
    monkeypatch.setattr(cli, "run_session_viewer", lambda args, client, sid, reconnect:
                        (sid, reconnect))
    args = Namespace(mode="auto", command=[], width=1600, height=900, graphics="kitty",
                     fps=60, max_width=1600, stream_quality="low")

    sid, reconnect = cli.run_managed_default(args)
    assert sid == "session-123"
    assert reconnect == "/opt/tisplay attach --session session-123 --graphics kitty --fps 60 --max-width 1600 --stream-quality low"
    assert "None" not in reconnect
    assert fake.closed


def test_engine_stdio_accepts_legacy_generation_argument(monkeypatch):
    from tisplay import daemon

    called = []
    monkeypatch.setattr(cli.sys, "argv", ["tisplay", "--engine-stdio", "--generation", "3"])
    monkeypatch.setattr(daemon, "run_stdio", lambda generation=None: called.append(generation) or 0)
    with pytest.raises(SystemExit) as result:
        cli.main()
    assert result.value.code == 0
    assert called == [3]


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
