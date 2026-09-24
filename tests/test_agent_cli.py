from __future__ import annotations

import sys
import pytest

from tisplay import cli
from tisplay import agent_cli
from tisplay.client import EngineError


@pytest.mark.parametrize("flag", ["-h", "--help", "-help"])
def test_top_level_help_aliases_show_command_menu(flag, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["tisplay", flag])
    with pytest.raises(SystemExit) as result:
        cli.main()
    assert result.value.code == 0
    output = capsys.readouterr().out
    assert "session" in output
    assert "AGENT WORKFLOW" in output
    assert "--skill" in output


def test_bundled_skill_is_printed_offline():
    import os
    import subprocess
    from pathlib import Path

    expected = Path(__file__).parents[1].joinpath("src", "tisplay", "SKILL.md").read_text()
    skill_discovery_copy = Path(__file__).parents[1].joinpath("skills", "tisplay", "SKILL.md")
    assert not skill_discovery_copy.is_symlink()
    assert skill_discovery_copy.read_text() == expected
    script = r"""
import importlib.abc, sys
class BlockDesktopImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {'tisplay.capture', 'tisplay.terminal', 'tisplay.daemon'}:
            raise AssertionError('unexpected desktop import: ' + fullname)
sys.meta_path.insert(0, BlockDesktopImports())
sys.argv = ['tisplay', '--skill']
from tisplay.cli import main
try:
    main()
except SystemExit as error:
    if error.code != 0: raise
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(Path(__file__).parents[1] / "src"), env.get("PYTHONPATH", "")])
    completed = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env, check=True)
    assert completed.stdout == expected


@pytest.mark.parametrize("argv,expected", [
    (["session", "-help"], "ACTION"),
    (["session", "start", "-help"], "tisplay session start"),
    (["click", "-help"], "tisplay click"),
])
def test_nested_single_dash_help_alias(argv, expected, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["tisplay", *argv])
    with pytest.raises(SystemExit) as result:
        cli.main()
    assert result.value.code == 0
    assert expected in capsys.readouterr().out


def test_parser_accepts_host_prefix_and_command_after_double_dash():
    args = agent_cli.build_parser().parse_args(
        ["--host", "user@pi", "session", "start", "--virtual", "--name", "work", "--", "xterm"]
    )
    assert args.host == "user@pi"
    assert args.name == "work"
    assert args.command == ["--", "xterm"]


def test_attach_accepts_explicit_lossy_stream_quality():
    args = agent_cli.build_parser().parse_args([
        "attach", "--session", "s1", "--stream-quality", "low"
    ])
    assert args.stream_quality == "low"


def test_same_host_v4_attach_uses_local_fast_viewer(monkeypatch):
    args = agent_cli.build_parser().parse_args(["attach", "--session", "s1"])

    class FakeClient:
        closed = False
        def capabilities(self, session):
            assert session == "s1"
            return {"viewer_leases": True}
        def close(self): self.closed = True

    client = FakeClient()
    monkeypatch.setattr(agent_cli, "_client", lambda host: client)
    monkeypatch.setattr(cli, "run_session_viewer", lambda args, client, sid: 17)
    assert agent_cli.dispatch(args) == 17
    assert client.closed


def test_drag_cli_action_uses_engine_coordinate_contract():
    args = agent_cli.build_parser().parse_args(
        ["drag", "--session", "s1", "12", "34", "56", "78"]
    )
    assert agent_cli._actions_for(args) == [
        {"type": "drag", "from_x": 12, "from_y": 34, "to_x": 56, "to_y": 78}
    ]


@pytest.mark.parametrize("argv", [
    ["click", "--session", "s1", "640", "360"],
    ["click", "--session", "s1", "--x", "640", "--y", "360"],
])
def test_click_accepts_positional_or_flag_coordinates(argv):
    args = agent_cli.build_parser().parse_args(argv)
    assert agent_cli._actions_for(args) == [{"type": "click", "x": 640, "y": 360, "button": "left"}]


def test_click_capture_id_is_forwarded_for_screenshot_pixel_coordinates():
    args = agent_cli.build_parser().parse_args([
        "click", "--session", "s1", "--x", "7", "--y", "9", "--capture-id", "capture-1"
    ])
    assert agent_cli._actions_for(args) == [
        {"type": "click", "x": 7, "y": 9, "button": "left", "capture_id": "capture-1"}
    ]


def test_click_rejects_mixed_coordinate_forms():
    args = agent_cli.build_parser().parse_args(["click", "--session", "s1", "640", "360", "--x", "1", "--y", "2"])
    with pytest.raises(ValueError, match="do not mix"):
        agent_cli._actions_for(args)


def test_click_count_and_key_aliases_normalize_to_engine_actions():
    click = agent_cli.build_parser().parse_args(["click", "--session", "s1", "--x", "1", "--y", "2", "--click-count", "2", "--button", "right"])
    key = agent_cli.build_parser().parse_args(["press-key", "--session", "s1", "CTRL+L"])
    text = agent_cli.build_parser().parse_args(["type-text", "--session", "s1", "hello"])
    assert agent_cli._actions_for(click) == [{"type": "double_click", "x": 1, "y": 2, "button": "right"}]
    assert agent_cli._actions_for(key) == [{"type": "key", "name": ["ctrl", "l"]}]
    assert agent_cli._actions_for(text) == [{"type": "text", "text": "hello"}]


def test_start_resolves_preset_dimensions_and_omits_preset(monkeypatch):
    calls = []

    class FakeClient:
        def start(self, **kwargs):
            calls.append(kwargs)
            return {"session_id": "s1"}
        def close(self):
            pass

    monkeypatch.setattr(agent_cli, "_client", lambda host: FakeClient())
    args = agent_cli.build_parser().parse_args(["session", "start", "--virtual", "--preset", "fast"])
    assert agent_cli.dispatch(args) == 0
    assert calls == [{"name": None, "virtual": True, "width": 1280, "height": 800, "command": None}]


def test_attach_controller_batches_input_and_releases_held_keys():
    calls = []

    class FakeClient:
        def input(self, session, actions, **kwargs):
            calls.append((session, actions, kwargs))

    controller = agent_cli._AttachController(FakeClient(), "s1")
    controller.owner = "attach-1"
    controller.frame_id = "geometry"
    controller.capture_id = "capture-token"
    controller.key("ctrl", True)
    controller.key("l", True)
    controller.key("l", False)
    controller.button("left", True, 10, 20)
    controller.button("left", False, 12, 25)
    controller.flush()

    assert len(calls) == 1
    assert calls[0][0] == "s1"
    assert calls[0][2] == {"owner": "attach-1"}
    assert calls[0][1][-1] == {
        "type": "drag", "from_x": 10, "from_y": 20, "to_x": 12, "to_y": 25,
        "button": "left", "frame_id": "geometry", "capture_id": "capture-token"
    }

    controller.close()
    assert calls[-1][1] == [{"type": "key_up", "name": ["ctrl"]}]


def test_attach_cleanup_drops_unsubmitted_actions_but_releases_held_key():
    calls = []

    class FakeClient:
        def input(self, session, actions, **kwargs):
            calls.append(actions)

    controller = agent_cli._AttachController(FakeClient(), "s1")
    controller.key("a", True)
    controller.discard_pending()
    controller.close()
    assert calls == [[{"type": "key_up", "name": ["a"]}]]


def test_attach_literal_space_plus_and_ctrl_key_reach_engine(monkeypatch):
    from tisplay.daemon import Engine, Session

    engine = Engine()
    session = Session("attach-keys", 800, 600)
    events = []

    class RecordingController:
        def key(self, key, down): events.append((key, down))
        def close(self): pass

    monkeypatch.setattr("tisplay.daemon.make_controller", RecordingController)
    owner = "attach-operator"
    engine.control(session, {"action": "acquire", "owner": owner})

    class EngineClient:
        def input(self, session_id, actions, **kwargs):
            assert session_id == session.id
            return engine.input(session, {"actions": actions, **kwargs})

    bridge = agent_cli._AttachController(EngineClient(), session.id)
    bridge.owner = owner

    class Screen:
        monitor = {"left": 0, "top": 0, "width": 800, "height": 600}

    assert cli.process_input(bytearray(b" +\r\x03"), bridge, Screen(), 80, 24)
    bridge.flush()
    assert events == [
        (" ", True), (" ", False),
        ("+", True), ("+", False),
        ("enter", True), ("enter", False),
        ("ctrl", True), ("c", True), ("c", False), ("ctrl", False),
    ]
    bridge.key("+", True)
    bridge.close()
    assert events[-2:] == [("+", True), ("+", False)]


def test_attach_renews_expired_lease_before_input(monkeypatch):
    from tisplay.daemon import Engine, Session

    engine = Engine()
    session = Session("attach-lease", 800, 600)
    events = []
    owner = "attach-operator"

    class RecordingController:
        def key(self, key, down): events.append((key, down))
        def close(self): pass

    monkeypatch.setattr("tisplay.daemon.make_controller", RecordingController)
    engine.control(session, {"action": "acquire", "owner": owner})
    session.control_until = 0  # Simulate a long blocking capture/write.

    class EngineClient:
        def control(self, session_id, **kwargs):
            assert session_id == session.id
            return engine.control(session, kwargs)
        def input(self, session_id, actions, **kwargs):
            assert session_id == session.id
            return engine.input(session, {"actions": actions, **kwargs})

    client = EngineClient()
    bridge = agent_cli._AttachController(
        client, session.id,
        before_flush=lambda: client.control(session.id, action="acquire", owner=owner, lease_seconds=60),
    )
    bridge.owner = owner
    bridge.key("a", True)
    bridge.flush()
    assert events == [("a", True)]
    assert session.control_owner == owner


def test_attach_does_not_take_over_another_owner_during_renewal():
    from tisplay.daemon import Engine, EngineError, Session

    engine = Engine()
    session = Session("attach-lease-busy", 800, 600, control_owner="another-owner", control_until=9999999999)
    sends = []
    renewals = []

    class Client:
        def control(self, session_id, **kwargs):
            renewals.append(kwargs)
            return engine.control(session, kwargs)
        def input(self, *args, **kwargs): sends.append((args, kwargs))

    client = Client()
    bridge = agent_cli._AttachController(
        client, session.id,
        before_flush=lambda: client.control(session.id, action="acquire", owner="attach-owner", lease_seconds=60),
    )
    bridge.owner = "attach-owner"
    bridge.key("a", True)
    with pytest.raises(EngineError, match="control is held by another owner"):
        bridge.flush()
    bridge.close()
    assert not sends
    assert len(renewals) == 1  # Cleanup did not retry the failed user batch.
    assert session.control_owner == "another-owner"


def test_attach_renews_before_input_after_blocking_capture(monkeypatch):
    import base64
    import io
    import signal
    import time as wall_clock
    from PIL import Image
    from tisplay.daemon import Engine, Session
    import tisplay.terminal as terminal

    engine = Engine()
    session = Session("attach-slow-frame", 800, 600)
    events = []
    clock = [0.0]
    wall_start = wall_clock.time()
    monkeypatch.setattr("tisplay.daemon.time.time", lambda: wall_start + clock[0])
    input_chunks = [b"a", b"b", b"\x1d"]
    png_buffer = io.BytesIO()
    Image.new("RGB", (2, 2), "black").save(png_buffer, format="PNG")
    png = base64.b64encode(png_buffer.getvalue()).decode("ascii")

    class RecordingController:
        def key(self, key, down): events.append((key, down))
        def close(self): pass

    monkeypatch.setattr("tisplay.daemon.make_controller", RecordingController)
    control_calls = []

    class Client:
        def status(self, sid):
            assert sid == session.id
            return {"monitor": {"left": 0, "top": 0, "width": 800, "height": 600}}
        def control(self, sid, **kwargs):
            control_calls.append((kwargs["action"], clock[0]))
            return engine.control(session, kwargs)
        def capture(self, sid, **kwargs):
            assert sid == session.id
            clock[0] = 61.0  # Capture/write stalled beyond the 60s lease.
            return {"png": png, "frame": {"content_id": "same", "monitor": {"left": 0, "top": 0, "width": 800, "height": 600}}}
        def input(self, sid, actions, **kwargs):
            assert control_calls[-1] == ("acquire", 61.0)
            return engine.input(session, {"actions": actions, **kwargs})

    class FakeTerminal:
        def __init__(self, _fd): pass
        def __enter__(self): return self
        def __exit__(self, *_args): pass

    class FakeTTY:
        def __init__(self, fd): self.fd = fd
        def isatty(self): return True
        def fileno(self): return self.fd

    with open("/dev/null", "rb") as stdin_file, open("/dev/null", "wb") as stdout_file:
        monkeypatch.setattr(agent_cli.sys, "stdin", FakeTTY(stdin_file.fileno()))
        monkeypatch.setattr(agent_cli.sys, "stdout", FakeTTY(stdout_file.fileno()))
        monkeypatch.setattr(terminal, "Terminal", FakeTerminal)
        monkeypatch.setattr(terminal, "begin_terminal", lambda: None)
        monkeypatch.setattr(terminal, "terminal_size", lambda: (80, 24))
        monkeypatch.setattr(terminal, "write_all", lambda *_args: None)
        monkeypatch.setattr(agent_cli.time, "monotonic", lambda: clock[0])
        monkeypatch.setattr(agent_cli.os, "read", lambda *_args: input_chunks.pop(0))
        select_calls = [0]
        def ready_once(readers, _writers, _errors, _timeout):
            select_calls[0] += 1
            return (readers if select_calls[0] <= 3 else [], [], [])
        monkeypatch.setattr("select.select", ready_once)
        monkeypatch.setattr(agent_cli, "_content_id", lambda _result: "same")
        monkeypatch.setattr("tisplay.cli.fit_ansi", lambda image, *_args: (image, (0, 0, 1, 1)))
        monkeypatch.setattr(terminal, "render_blocks", lambda *_args: b"")
        monkeypatch.setattr("tisplay.cli.detect_graphics", lambda: "ansi")
        monkeypatch.setattr("tisplay.cli.terminal_pixel_size", lambda: (800, 600))
        old_signal = signal.getsignal(signal.SIGWINCH)
        monkeypatch.setattr(signal, "signal", lambda *_args: old_signal)

        args = type("Args", (), {
            "session": session.id, "view_only": False, "graphics": "ansi", "fps": 60,
            "max_width": 800, "stream_quality": "lossless", "owner": "attach-test",
        })()
        assert agent_cli._attach(Client(), args) == 0

    assert [action for action, _at in control_calls] == ["acquire", "acquire", "release"]
    assert control_calls[0][1] == 0.0
    assert control_calls[1][1] == 61.0
    assert events == [("a", True), ("a", False), ("b", True), ("b", False)]


def test_view_only_attach_controller_never_queues_input():
    class FakeClient:
        def input(self, *_args, **_kwargs):
            raise AssertionError("view-only attach must not send input")

    controller = agent_cli._AttachController(FakeClient(), "s1", readonly=True)
    controller.key("a", True)
    controller.button("left", True, 1, 2)
    controller.flush()
    controller.close()


def test_attach_orphan_mouse_release_does_not_become_a_click():
    calls = []

    class FakeClient:
        def input(self, session, actions, **kwargs):
            calls.append(actions)

    controller = agent_cli._AttachController(FakeClient(), "s1")
    controller.button("left", False, 0, 0)
    controller.flush()
    assert calls == []


def test_open_url_command_preserves_owner(monkeypatch, capsys):
    calls = []

    class FakeClient:
        def open_url(self, session, url, owner=None):
            calls.append((session, url, owner))
            return {"opened": True}
        def close(self): pass

    monkeypatch.setattr(agent_cli, "_client", lambda _host: FakeClient())
    args = agent_cli.build_parser().parse_args(["open-url", "--session", "s1", "--owner", "operator", "https://example.com"])
    assert agent_cli.dispatch(args) == 0
    assert calls == [("s1", "https://example.com", "operator")]
    assert '"opened": true' in capsys.readouterr().out


def test_act_screenshot_returns_compact_summary_by_default(monkeypatch, capsys):
    class FakeClient:
        def batch(self, session, actions, **kwargs):
            return {"completed": 2, "results": [{"completed": 1}, {"completed": 1}]}
        def close(self): pass

    monkeypatch.setattr(agent_cli, "_client", lambda _host: FakeClient())
    monkeypatch.setattr(agent_cli, "_capture", lambda _client, _session, _args: {
        "path": "/tmp/frame.png", "bytes": 123, "frame": {"frame_id": "geometry"}
    })
    args = agent_cli.build_parser().parse_args([
        "act", "--session", "s1", "--actions", '[{"type":"press_key","name":"ctrl+l"}]',
        "--screenshot", "--out", "frame.png",
    ])
    assert agent_cli.dispatch(args) == 0
    output = capsys.readouterr().out
    assert '"actions": 2' in output and '"path": "/tmp/frame.png"' in output
    assert '"results"' not in output


def test_json_engine_error_is_one_machine_readable_stdout_object(monkeypatch, capsys):
    class FailingClient:
        def input(self, *args, **kwargs):
            raise EngineError("out of bounds", "invalid_request")
        def close(self):
            pass

    monkeypatch.setattr(agent_cli, "_client", lambda host: FailingClient())
    code = agent_cli.main(["click", "--session", "s1", "4", "9", "--json"])
    assert code == 3
    output = capsys.readouterr()
    assert output.out == '{"ok":false,"error":{"code":"invalid_request","message":"out of bounds"}}\n'
    assert output.err == ""
