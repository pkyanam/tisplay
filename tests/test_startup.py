from argparse import Namespace
from tisplay import cli


def args(virtual=False, command=None):
    return Namespace(virtual=virtual, width=1280, height=800, command=command or [])


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
