from types import SimpleNamespace

from tisplay import client as client_module
from tisplay.client import ENGINE_GENERATION, LEGACY_ENGINE_GENERATION, EngineError, SessionClient, socket_path


def test_local_client_probes_until_spawned_daemon_is_ready(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    clock = [0.0]
    sleeps = []
    attempts = []

    class FakeSocket:
        def settimeout(self, _timeout):
            pass

        def connect(self, path):
            attempts.append(path)
            if len(attempts) == 1:
                raise FileNotFoundError(path)

        def makefile(self, _mode):
            return object()

        def close(self):
            pass

    monkeypatch.setattr(client_module.socket, "socket", lambda *_: FakeSocket())
    monkeypatch.setattr(client_module.subprocess, "Popen", lambda *_args, **_kwargs: SimpleNamespace())
    monkeypatch.setattr(client_module.time, "monotonic", lambda: clock[0])

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(client_module.time, "sleep", sleep)

    client = SessionClient.__new__(SessionClient)
    client.timeout = 1
    client.generation = ENGINE_GENERATION
    client.runtime_dir = str(tmp_path)
    client.spawn_daemon = True
    client._sock = None
    client._stream = None
    client._connect_local()

    assert len(attempts) == 2
    assert sleeps == []
    assert client._sock is not None
    assert client._stream is not None


def test_client_pins_runtime_directory_and_routes_generation_paths(monkeypatch, tmp_path):
    first_runtime = tmp_path / "first-runtime"
    later_runtime = tmp_path / "later-runtime"
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(first_runtime))
    monkeypatch.setattr(SessionClient, "_connect_local", lambda self: None)

    client = SessionClient()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(later_runtime))

    assert client.runtime_dir == str(first_runtime)
    assert socket_path(ENGINE_GENERATION, client.runtime_dir) == first_runtime / "tisplay" / f"engine-v{ENGINE_GENERATION}.sock"
    assert socket_path(LEGACY_ENGINE_GENERATION, client.runtime_dir) == first_runtime / "tisplay" / f"engine-v{LEGACY_ENGINE_GENERATION}.sock"


def test_legacy_fallback_only_follows_explicit_not_found(monkeypatch):
    client = SessionClient.__new__(SessionClient)
    client.generation = ENGINE_GENERATION
    client.legacy_fallback = True
    client._legacy_sessions = set()
    legacy_calls = []

    def not_found(*_args, **_kwargs):
        raise EngineError("session not found", "not_found")

    monkeypatch.setattr(client, "_request_once", not_found)
    monkeypatch.setattr(client, "_legacy_request", lambda *args, **kwargs: legacy_calls.append((args, kwargs)) or {"status": "legacy"})

    assert client.request("input", "legacy-session", actions=[{"type": "move", "x": 1, "y": 2}]) == {"status": "legacy"}
    assert legacy_calls == [(('status', 'legacy-session'), {}),
                            (('input', 'legacy-session'), {"actions": [{"type": "move", "x": 1, "y": 2}]})]

    def disconnected(*_args, **_kwargs):
        raise EngineError("transport lost", "protocol_error")

    monkeypatch.setattr(client, "_request_once", disconnected)
    client._legacy_sessions.clear()
    legacy_calls.clear()
    try:
        client.request("input", "legacy-session", actions=[{"type": "move", "x": 1, "y": 2}])
    except EngineError as exc:
        assert exc.code == "protocol_error"
    else:
        raise AssertionError("transport failure should propagate")
    assert legacy_calls == []


def test_action_not_found_on_generation_four_is_not_replayed_on_legacy(monkeypatch):
    client = SessionClient.__new__(SessionClient)
    client.generation = ENGINE_GENERATION
    client.legacy_fallback = True
    client._legacy_sessions = set()
    legacy_calls = []

    def current_request(verb, *_args, **_kwargs):
        if verb == "status":
            return {"session_id": "current-session"}
        raise EngineError("action target not found", "not_found")

    monkeypatch.setattr(client, "_request_once", current_request)
    monkeypatch.setattr(client, "_legacy_request", lambda *args, **kwargs: legacy_calls.append((args, kwargs)))
    try:
        client.request("control", "current-session", action="click")
    except EngineError as exc:
        assert str(exc) == "action target not found"
    else:
        raise AssertionError("current-session error should propagate without legacy replay")
    assert legacy_calls == []


def test_missing_legacy_daemon_preserves_generation_four_not_found(monkeypatch):
    client = SessionClient.__new__(SessionClient)
    client.generation = ENGINE_GENERATION
    client.legacy_fallback = True

    def not_found(*_args, **_kwargs):
        raise EngineError("session not found", "not_found")

    def no_legacy(*_args, **_kwargs):
        raise EngineError("legacy daemon is not running", "daemon_unavailable")

    monkeypatch.setattr(client, "_request_once", not_found)
    monkeypatch.setattr(client, "_legacy_request", no_legacy)

    try:
        client.request("status", "unknown-session")
    except EngineError as exc:
        assert exc.code == "not_found"
    else:
        raise AssertionError("unknown session should remain a not_found error")


def test_list_merges_legacy_sessions_without_rewriting_ids(monkeypatch):
    client = SessionClient.__new__(SessionClient)
    client.generation = ENGINE_GENERATION
    client.legacy_fallback = True
    client._legacy_sessions = set()
    current = {"sessions": [{"session_id": "same-id", "name": "new"}]}
    legacy = {"sessions": [{"session_id": "same-id", "name": "old-copy"},
                           {"session_id": "legacy-id", "name": "existing"}]}
    monkeypatch.setattr(client, "_request_once", lambda *_args, **_kwargs: current)
    monkeypatch.setattr(client, "_legacy_request", lambda *_args, **_kwargs: legacy)

    assert client.list() == {"sessions": [current["sessions"][0], legacy["sessions"][1]]}
    assert client._legacy_sessions == {"legacy-id"}


def test_known_legacy_session_routes_repeated_requests_without_generation_four(monkeypatch):
    client = SessionClient.__new__(SessionClient)
    client.generation = ENGINE_GENERATION
    client.legacy_fallback = True
    client._legacy_sessions = {"legacy-id"}
    legacy_calls = []

    def forbidden_v4(*_args, **_kwargs):
        raise AssertionError("known legacy session should not query generation four")

    monkeypatch.setattr(client, "_request_once", forbidden_v4)
    monkeypatch.setattr(client, "_legacy_request", lambda *args, **kwargs: legacy_calls.append((args, kwargs)) or {"ok": True})

    assert client.request("input", "legacy-id", actions=[]) == {"ok": True}
    assert client.request("status", "legacy-id") == {"ok": True}
    assert legacy_calls == [(('input', 'legacy-id'), {"actions": []}), (('status', 'legacy-id'), {})]


def test_legacy_client_never_spawns_an_old_daemon(monkeypatch, tmp_path):
    attempts = []

    class RefusedSocket:
        def settimeout(self, _timeout):
            pass

        def connect(self, path):
            attempts.append(path)
            raise FileNotFoundError(path)

        def close(self):
            pass

    def forbidden_spawn(*_args, **_kwargs):
        raise AssertionError("legacy fallback must not start an old daemon")

    monkeypatch.setattr(client_module.socket, "socket", lambda *_: RefusedSocket())
    monkeypatch.setattr(client_module.subprocess, "Popen", forbidden_spawn)
    legacy = SessionClient.__new__(SessionClient)
    legacy.generation = LEGACY_ENGINE_GENERATION
    legacy.runtime_dir = str(tmp_path)
    legacy.timeout = 1
    legacy.spawn_daemon = False

    try:
        legacy._connect_local()
    except EngineError as exc:
        assert exc.code == "daemon_unavailable"
    else:
        raise AssertionError("missing legacy daemon unexpectedly connected")
    assert len(attempts) == 1


def test_viewer_helper_sends_session_scoped_viewer_command(monkeypatch):
    client = SessionClient.__new__(SessionClient)
    calls = []
    monkeypatch.setattr(client, "request", lambda *args, **kwargs: calls.append((args, kwargs)) or {"ok": True})

    result = client.viewer("default", "attach", "viewer-1", lease_seconds=30)

    assert result == {"ok": True}
    assert calls == [(('viewer', 'default'), {"action": "attach", "viewer_id": "viewer-1", "lease_seconds": 30})]
