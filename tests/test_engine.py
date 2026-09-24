from __future__ import annotations

from pathlib import Path

import pytest

from tisplay.daemon import Engine, EngineError, Session, _response
from tisplay.client import ENGINE_GENERATION, SessionClient, socket_path
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
    assert socket_path() == tmp_path / "tisplay" / f"engine-v{ENGINE_GENERATION}.sock"


def test_viewer_leases_cancel_and_restart_idle_cleanup(monkeypatch):
    import tisplay.daemon as daemon

    now = [100.0]
    monkeypatch.setattr(daemon.time, "time", lambda: now[0])
    engine = Engine()
    session = Session("managed", 800, 600, idle_ttl=900, idle_deadline=1000.0)
    engine.sessions[session.id] = session

    engine.viewer(session, {"action": "connect", "viewer_id": "viewer-a"})
    assert session.idle_deadline is None
    engine.viewer(session, {"action": "connect", "viewer_id": "viewer-b"})
    engine.viewer(session, {"action": "disconnect", "viewer_id": "viewer-a"})
    assert session.idle_deadline is None
    engine.viewer(session, {"action": "disconnect", "viewer_id": "viewer-b"})
    assert session.idle_deadline == 1000.0

    # A delayed viewer heartbeat is an idempotent reconnect within the grace.
    engine.viewer(session, {"action": "heartbeat", "viewer_id": "viewer-b"})
    assert session.idle_deadline is None
    assert session.viewers == {"viewer-b": now[0]}
    engine.viewer(session, {"action": "disconnect", "viewer_id": "viewer-b"})
    assert session.idle_deadline == 1000.0

    now[0] = 999.0
    engine._record_activity(session)
    assert session.idle_deadline == 1899.0
    now[0] = 1898.0
    engine.expire_leases()
    assert session.id in engine.sessions
    session.idle_deadline = now[0]
    session.control_owner, session.control_until = "agent", now[0] + 10
    engine.expire_leases()
    assert session.id in engine.sessions
    now[0] += 11
    engine.expire_leases()
    assert session.id not in engine.sessions


def test_viewer_lease_expiry_starts_grace_and_duplicate_disconnect_does_not_extend(monkeypatch):
    import tisplay.daemon as daemon

    now = [100.0]
    monkeypatch.setattr(daemon.time, "time", lambda: now[0])
    engine = Engine()
    engine.viewer_lease_seconds = 60
    session = Session("managed", 800, 600, idle_ttl=900)
    engine.sessions[session.id] = session
    engine.viewer(session, {"action": "connect", "viewer_id": "viewer-a"})
    now[0] = 161.0
    engine.expire_leases()
    assert session.viewers == {}
    assert session.idle_deadline == 1061.0
    engine.viewer(session, {"action": "disconnect", "viewer_id": "viewer-a"})
    assert session.idle_deadline == 1061.0


def test_persistent_sessions_ignore_idle_expiration():
    engine = Engine()
    session = Session("persistent", 800, 600)
    engine.sessions[session.id] = session
    engine.expire_leases()
    assert engine.sessions[session.id] is session


def test_passive_status_does_not_extend_managed_idle_deadline(monkeypatch):
    import tisplay.daemon as daemon

    now = [100.0]
    monkeypatch.setattr(daemon.time, "time", lambda: now[0])
    engine = Engine()
    session = Session("managed", 800, 600, idle_ttl=900, idle_deadline=500.0)
    engine.sessions[session.id] = session
    engine.dispatch({"command": "control", "session": session.id, "args": {"action": "status"}})
    engine.dispatch({"command": "status", "session": session.id, "args": {}})
    assert session.idle_deadline == 500.0


def test_legacy_stdio_bridge_does_not_start_second_daemon(monkeypatch):
    import io
    import tisplay.daemon as daemon

    attempts = []
    class MissingSocket:
        def connect(self, path):
            attempts.append(path)
            raise FileNotFoundError(path)
        def close(self):
            pass
    monkeypatch.setattr(daemon.socket, "socket", lambda *_args: MissingSocket())
    monkeypatch.setattr(daemon, "socket_path", lambda generation=None: Path(f"/tmp/engine-v{generation}.sock"))
    monotonic = iter((0.0, 6.0, 6.0))
    monkeypatch.setattr(daemon.time, "monotonic", lambda: next(monotonic))
    monkeypatch.setattr(daemon.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(daemon.subprocess, "Popen", lambda *_a, **_kw: pytest.fail("legacy bridge must not start a daemon"))
    monkeypatch.setattr(daemon.sys, "stderr", io.StringIO())
    assert daemon.run_stdio(generation=3) == 1
    assert attempts == ["/tmp/engine-v3.sock"]


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
    current_frame = engine._monitor_frame_id({"left": 0, "top": 0, "width": 800, "height": 600})
    monkeypatch.setattr(engine, "_screen", lambda *_: (object(), {"frame": {"frame_id": current_frame, "display_width": 800, "display_height": 600, "monitor": {"left": 0, "top": 0}}}))

    with pytest.raises(EngineError, match="requires an active control lease"):
        engine.input(session, {"actions": [{"type": "key_down", "name": "ctrl"}]})

    engine.control(session, {"action": "acquire", "owner": "operator", "lease_seconds": 20})
    result = engine.input(session, {"owner": "operator", "actions": [
        {"type": "click", "x": 42, "y": 31, "frame_id": current_frame},
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


def test_capture_token_maps_scaled_crop_coordinates_to_monitor_pixels(monkeypatch):
    engine = Engine()
    session = Session("grounded", 800, 600)
    token = "opaque-observation-token"
    monitor = {"left": -800, "top": 20, "width": 800, "height": 600, "name": "HDMI-A-1"}
    session.observations[token] = {
        "frame_id": engine._monitor_frame_id(monitor), "width": 200, "height": 100,
        "display_width": 800, "display_height": 600, "monitor": monitor,
        "region": {"x": 50, "y": 30, "width": 400, "height": 200}, "timestamp": __import__("time").time(),
    }
    fake = FakeController()
    monkeypatch.setattr(engine, "_current_monitor", lambda *_: monitor)
    monkeypatch.setattr("tisplay.daemon.make_controller", lambda: fake)
    result = engine.input(session, {"capture_id": token, "actions": [{"type": "click", "x": 100, "y": 50}]})
    assert result["completed"] == 1
    assert ("button", "left", True, -549, 151) in fake.events
    assert token not in session.observations  # tokens authorize one input batch


@pytest.mark.parametrize("case", ["unknown", "expired", "layout"])
def test_capture_token_rejects_unknown_expired_or_resized_observation(monkeypatch, case):
    import time
    engine = Engine()
    session = Session("grounded", 800, 600)
    token = "token"
    monitor = {"left": 0, "top": 0, "width": 800, "height": 600}
    if case != "unknown":
        session.observations[token] = {
            "frame_id": engine._monitor_frame_id(monitor), "width": 800, "height": 600,
            "display_width": 800, "display_height": 600, "monitor": monitor,
            "region": {"x": 0, "y": 0, "width": 800, "height": 600},
            "timestamp": time.time() - (61 if case == "expired" else 0),
        }
    changed = {**monitor, "width": 1024} if case == "layout" else monitor
    monkeypatch.setattr(engine, "_current_monitor", lambda *_: changed)
    with pytest.raises(EngineError) as err:
        engine.input(session, {"capture_id": token, "actions": [{"type": "click", "x": 4, "y": 5}]})
    assert err.value.code == "stale_capture"


def test_geometry_frame_id_is_not_accepted_as_capture_token(monkeypatch):
    engine = Engine()
    session = Session("grounded", 800, 600)
    monitor = {"left": 0, "top": 0, "width": 800, "height": 600}
    monkeypatch.setattr(engine, "_current_monitor", lambda *_: monitor)
    monkeypatch.setattr("tisplay.daemon.make_controller", lambda: FakeController())
    with pytest.raises(EngineError) as err:
        engine.input(session, {"actions": [{"type": "click", "x": 4, "y": 5, "frame_id": "not-a-capture-token"}]})
    assert err.value.code == "stale_frame"


def test_environment_exposes_only_allowlisted_session_variables():
    engine = Engine()
    session = Session("native", 800, 600, env={
        "DISPLAY": ":92", "WAYLAND_DISPLAY": "wayland-0", "XDG_RUNTIME_DIR": "/run/user/1000",
        "XAUTHORITY": "/private/auth", "SECRET_TOKEN": "never-return-this",
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
    }, backend="wayland-native", mode="native-existing")
    result = engine.environment(session)
    assert result == {"environment": {
        "DISPLAY": ":92", "XAUTHORITY": "/private/auth", "WAYLAND_DISPLAY": "wayland-0", "XDG_RUNTIME_DIR": "/run/user/1000",
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
    }, "backend": "wayland-native", "mode": "native-existing", "cua_socket": None}


def test_transient_viewer_captures_do_not_evict_agent_observation(monkeypatch):
    import time
    engine = Engine()
    session = Session("grounded", 800, 600)
    token = "agent-capture"
    monitor = {"left": 0, "top": 0, "width": 800, "height": 600}
    session.observations[token] = {
        "frame_id": engine._monitor_frame_id(monitor), "width": 800, "height": 600,
        "display_width": 800, "display_height": 600, "monitor": monitor,
        "region": {"x": 0, "y": 0, "width": 800, "height": 600}, "timestamp": time.time(),
    }
    frame = {"frame_id": engine._monitor_frame_id(monitor), "content_id": "c", "timestamp": time.time(),
             "width": 800, "height": 600, "display_width": 800, "display_height": 600,
             "monitor": monitor, "region": {"x": 0, "y": 0, "width": 800, "height": 600}}
    monkeypatch.setattr(engine, "_screen", lambda *_: (object(), {"png": "AAAA", "frame": dict(frame)}))
    for _ in range(20):
        result = engine.capture(session, {"max_width": 1600, "register_capture": False})
        assert "capture_id" not in result["frame"]
    assert list(session.observations) == [token]
    grounded = engine.capture(session, {})
    assert grounded["frame"]["capture_id"] in session.observations
    fake = FakeController()
    monkeypatch.setattr(engine, "_current_monitor", lambda *_: monitor)
    monkeypatch.setattr("tisplay.daemon.make_controller", lambda: fake)
    engine.input(session, {"actions": [{"type": "click", "x": 11, "y": 12, "capture_id": token}]})
    assert ("button", "left", True, 11, 12) in fake.events


def test_cua_service_is_session_owned_and_stops_with_session(monkeypatch, tmp_path):
    import socket as socket_module
    import stat
    import tempfile

    data_home = tmp_path / "data"
    binary = data_home / "tisplay/cua/current/bin/cua-driver"
    binary.parent.mkdir(parents=True)
    binary.write_text("stub", encoding="utf-8")
    binary.chmod(0o755)
    runtime = Path(tempfile.mkdtemp(prefix="tst-"))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    launched = {}

    class FakeProcess:
        pid = 99999999
        def poll(self): return None
        def wait(self, timeout=None): return 0

    def fake_popen(command, **kwargs):
        launched["command"], launched["env"] = command, kwargs["env"]
        server = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
        server.bind(command[command.index("--socket") + 1])
        server.close()
        return FakeProcess()

    monkeypatch.setattr("tisplay.daemon.subprocess.Popen", fake_popen)
    engine = Engine()
    session = Session("cua-owned", 800, 600, env={
        "DISPLAY": ":91", "XAUTHORITY": str(tmp_path / "Xauthority"), "XDG_RUNTIME_DIR": str(runtime),
    }, backend="x11")
    result = engine.cua_service(session, {"action": "start"})
    socket_path = Path(result["cua_socket"])
    assert result["running"] and result["started"] and socket_path.is_socket()
    assert stat.S_IMODE(socket_path.stat().st_mode) == 0o600
    assert result["cua_binary"] == str(binary)
    assert launched["command"] == [str(binary), "serve", "--socket", str(socket_path)]
    assert launched["env"]["XAUTHORITY"] == str(tmp_path / "Xauthority")
    assert launched["env"]["RUST_LOG"] == "warn"
    assert "CUA_DRIVER_RS_ENABLE_WAYLAND" not in launched["env"]
    assert "SECRET_TOKEN" not in launched["env"]

    engine.stop(session)
    assert session.cua_process is None and not socket_path.exists()
    (runtime / "tisplay" / "cua").rmdir()
    (runtime / "tisplay").rmdir()
    runtime.rmdir()


def test_virtual_environment_uses_private_xfce_dbus_bus(monkeypatch, tmp_path):
    import types

    monkeypatch.setattr(Engine, "_virtual_session_bus", staticmethod(lambda _s: "unix:path=/tmp/tisplay-private-bus"))
    session = Session("virtual-bus", 800, 600, virtual=True, display=":177",
                      desktop=types.SimpleNamespace(session=object()), env={"DISPLAY": ":177",
                      "XDG_RUNTIME_DIR": str(tmp_path)})
    resolved = Engine().environment(session)["environment"]
    assert resolved["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/tmp/tisplay-private-bus"


def test_cua_environment_enables_wayland_only_for_native_backend():
    engine = Engine()
    native = engine._cua_process_environment(Session("native", 800, 600, backend="wayland-native"), "/tmp/native-runtime")
    x11 = engine._cua_process_environment(Session("x11", 800, 600, backend="x11"), "/tmp/x11-runtime")
    assert native["CUA_DRIVER_RS_ENABLE_WAYLAND"] == "1"
    assert "CUA_DRIVER_RS_ENABLE_WAYLAND" not in x11


class SimpleCloser:
    def close(self): pass
