from __future__ import annotations

import pytest

from tisplay.daemon import Engine, EngineError, Session, _response
from tisplay.client import SessionClient, socket_path
from tisplay.protocol import decode_message, encode_message


class FakeController:
    def __init__(self): self.events = []
    def move(self, x, y): self.events.append(("move", x, y))
    def button(self, button, down, x, y): self.events.append(("button", button, down, x, y))
    def key(self, key, down): self.events.append(("key", key, down))
    def scroll(self, x, y, dx, dy): self.events.append(("scroll", x, y, dx, dy))
    def close(self): pass


def test_json_protocol_and_structured_error():
    engine = Engine()
    good = decode_message(_response(engine, encode_message({"protocol_version": 1, "id": "1", "command": "list", "args": {}})))
    assert good == {"protocol_version": 1, "id": "1", "ok": True, "result": {"sessions": []}}
    bad = decode_message(_response(engine, encode_message({"protocol_version": 1, "id": "2", "command": "unknown", "args": {}})))
    assert bad["ok"] is False and bad["error"]["code"] == "invalid_request"


def test_protocol_rejects_unsupported_version_and_invalid_ssh_host():
    response = decode_message(_response(Engine(), encode_message({"protocol_version": 2, "id": "v2", "command": "list", "args": {}})))
    assert response["error"]["code"] == "unsupported_protocol"
    with pytest.raises(EngineError, match="unsupported characters"):
        SessionClient(host="-oProxyCommand=bad")
    with pytest.raises(EngineError, match="unsupported characters"):
        SessionClient(host="user@some host")


def test_new_engine_uses_generation_scoped_socket(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert socket_path() == tmp_path / "tisplay" / "engine-v2.sock"


def test_input_uses_source_pixels_and_requires_lease_for_held_keys(monkeypatch):
    engine = Engine()
    class FakeScreen:
        monitor = {"left": 0, "top": 0, "width": 800, "height": 600}
        grabber = SimpleCloser()
    monkeypatch.setattr("tisplay.daemon.Screen", lambda: FakeScreen())
    started = engine.start({"width": 800, "height": 600, "name": "work"})
    sid = started["session_id"]
    session = engine.sessions[sid]
    fake = FakeController()
    monkeypatch.setattr("tisplay.daemon.make_controller", lambda: fake)
    monkeypatch.setattr(engine, "_screen", lambda *_: (object(), {"frame": {"frame_id": "geom", "display_width": 800, "display_height": 600, "monitor": {"left": 0, "top": 0}}}))

    with pytest.raises(EngineError, match="requires an active control lease"):
        engine.input(session, {"actions": [{"type": "key_down", "name": "ctrl"}]})

    engine.control(session, {"action": "acquire", "owner": "operator", "lease_seconds": 20})
    result = engine.input(session, {"owner": "operator", "actions": [
        {"type": "click", "x": 42, "y": 31, "frame_id": "geom"},
        {"type": "key_down", "name": "ctrl"},
        {"type": "key_up", "name": "ctrl"},
    ]})
    assert result["completed"] == 3
    assert ("button", "left", True, 42, 31) in fake.events
    assert ("key", "ctrl", True) in fake.events
    assert not session.keys_down


def test_key_only_input_skips_capture_and_normalizes_control_chord(monkeypatch):
    engine = Engine()
    session = Session("keys", 800, 600)
    fake = FakeController()
    monkeypatch.setattr(engine, "_screen", lambda *_: (_ for _ in ()).throw(AssertionError("key-only input must not capture")))
    monkeypatch.setattr("tisplay.daemon.make_controller", lambda: fake)
    result = engine.input(session, {"actions": [{"type": "press_key", "name": "CTRL+L"}]})
    assert result == {"completed": 1, "frame_id": None}
    assert fake.events == [("key", "ctrl", True), ("key", "l", True), ("key", "l", False), ("key", "ctrl", False)]


def test_batch_coalesces_actions_until_observation_boundary():
    engine = Engine()
    session = Session("batch", 800, 600)
    calls = []
    engine.input = lambda _session, args: calls.append((_session, args)) or {"completed": len(args["actions"]), "frame_id": None}
    result = engine.batch(session, {"actions": [
        {"type": "key", "name": "ctrl+l"},
        {"type": "type_text", "text": "hello"},
    ]})
    assert len(calls) == 1
    assert calls[0][0] is session
    assert [a["type"] for a in calls[0][1]["actions"]] == ["key", "text"]
    assert result["completed"] == 2


def test_open_url_is_validated_and_uses_session_environment(monkeypatch):
    engine = Engine()
    session = Session("native", 800, 600, env={"DISPLAY": None, "WAYLAND_DISPLAY": "wayland-test", "XDG_RUNTIME_DIR": "/run/user/1000"})
    calls = []
    class Started: pid = 123
    monkeypatch.setattr("tisplay.daemon.sys.platform", "linux")
    monkeypatch.setattr("tisplay.daemon.shutil_which", lambda _name: "/usr/bin/xdg-open")
    monkeypatch.setattr("tisplay.daemon.subprocess.Popen", lambda command, **kwargs: calls.append((command, kwargs["env"].copy())) or Started())
    result = engine.open_url(session, {"url": "https://example.com/path"})
    assert result["opened"] is True and result["pid"] == 123
    assert calls[0][0] == ["/usr/bin/xdg-open", "https://example.com/path"]
    assert calls[0][1]["WAYLAND_DISPLAY"] == "wayland-test"
    for url in ("file:///etc/passwd", "javascript:alert(1)", "https://user:pass@example.com"):
        with pytest.raises(EngineError, match="HTTP or HTTPS"):
            engine.open_url(session, {"url": url})


def test_resize_reports_unsupported_instead_of_claiming_success():
    engine = Engine()
    record = Session("test", 800, 600, virtual=True)
    with pytest.raises(EngineError) as err:
        engine.resize(record, {"width": 1600, "height": 900})
    assert err.value.code == "unsupported"


def test_batch_wait_observes_content_changes_and_stale_frame_is_rejected(monkeypatch):
    engine = Engine()
    session = Session("test", 800, 600)
    engine.sessions[session.id] = session
    frames = iter(("before", "after"))
    monkeypatch.setattr(engine, "capture", lambda *_: {"png": next(frames)})
    result = engine.batch(session, {"actions": [{"type": "wait", "timeout_ms": 200, "interval_ms": 20}]})
    assert result["results"] == [{"type": "wait", "changed": True}]

    monkeypatch.setattr("tisplay.daemon.make_controller", lambda: FakeController())
    monkeypatch.setattr(engine, "_screen", lambda *_: (object(), {"frame": {"frame_id": "new", "display_width": 800, "display_height": 600, "monitor": {"left": 0, "top": 0}}}))
    with pytest.raises(EngineError) as err:
        engine.input(session, {"actions": [{"type": "move", "x": 3, "y": 4, "frame_id": "old"}]})
    assert err.value.code == "stale_frame"


def test_expired_lease_releases_held_keys(monkeypatch):
    engine = Engine()
    session = Session("test", 800, 600, control_owner="operator", control_until=1, keys_down={"ctrl"}, held_owner="operator")
    engine.sessions[session.id] = session
    fake = FakeController()
    monkeypatch.setattr("tisplay.daemon.make_controller", lambda: fake)
    engine.expire_leases()
    assert session.control_owner is None and not session.keys_down
    assert fake.events == [("key", "ctrl", False)]


class SimpleCloser:
    def close(self): pass
