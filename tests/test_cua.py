from __future__ import annotations

import base64
import json
import shlex

import pytest

from tisplay import agent_cli, cua


def test_image_payload_is_saved_locally_or_removed_from_default_output(tmp_path):
    pixel = base64.b64encode(b"png bytes").decode()
    result = {"screenshot_png_b64": pixel, "elements": [{"label": "Button"}], "tree_markdown": "duplicate"}
    saved = cua._save_image_blocks(result, tmp_path / "state.png")
    assert saved[0]["bytes"] == 9
    assert (tmp_path / "state.png").read_bytes() == b"png bytes"
    assert "screenshot_png_b64" not in result
    assert result["screenshot_file_path"] == str((tmp_path / "state.png").resolve())


def test_image_payload_is_omitted_without_out_and_raw_is_explicit():
    pixel = base64.b64encode(b"png bytes").decode()
    result = {"screenshot_png_b64": pixel}
    assert cua._save_image_blocks(result, None) == []
    assert "screenshot_png_b64" not in result and result["screenshot_omitted"]
    raw_result = {"screenshot_png_b64": pixel}
    cua._save_image_blocks(raw_result, None, raw=True)
    assert raw_result["screenshot_png_b64"] == pixel


def test_cua_call_compacts_snapshot_and_injects_stable_session_label(tmp_path, monkeypatch):
    requests = []

    class FakeClient:
        def __init__(self, host=None): pass
        def control(self, session, **kwargs): requests.append((session, kwargs))
        def close(self): pass
        def __enter__(self): return self
        def __exit__(self, *_): self.close()

    monkeypatch.setattr(cua, "SessionClient", FakeClient)
    monkeypatch.setattr(cua, "_ensure_daemon", lambda host, session: ("/tmp/cua.sock", "/tmp/cua-driver"))
    monkeypatch.setattr(cua, "_prefix", lambda host, binary=None: "cua-driver")
    monkeypatch.setattr(cua, "inspect", lambda *args: "input_schema:\n{\"properties\":{\"session\":{\"type\":\"string\"}}}")
    calls = []
    def run(host, command, **kwargs):
        calls.append(command)
        return json.dumps({"elements": [{"label": "OK"}], "element_count": 1,
                           "tree_markdown": "duplicate", "screenshot_png_b64": base64.b64encode(b"img").decode()}).encode()
    monkeypatch.setattr(cua, "_run", run)

    out = tmp_path / "frame.png"
    result = cua.call(None, "s1", "get_window_state", {"pid": 4, "window_id": 5}, str(out))
    assert "tree_markdown" not in result
    assert "screenshot_png_b64" not in result
    assert result["saved_images"][0]["path"] == str(out.resolve())
    command_args = shlex.split(calls[0])
    tool_args = json.loads(command_args[5])
    assert tool_args["session"] == "tisplay-s1"
    assert requests[0][1]["action"] == "acquire" and requests[-1][1]["action"] == "release"


def test_cua_call_reports_upstream_tool_errors(monkeypatch):
    class FakeClient:
        def __init__(self, host=None): pass
        def control(self, session, **kwargs): pass
        def close(self): pass
        def __enter__(self): return self
        def __exit__(self, *_): self.close()

    monkeypatch.setattr(cua, "SessionClient", FakeClient)
    monkeypatch.setattr(cua, "_ensure_daemon", lambda host, session: ("/tmp/cua.sock", "/tmp/cua-driver"))
    monkeypatch.setattr(cua, "_prefix", lambda host, binary=None: "cua-driver")
    monkeypatch.setattr(cua, "inspect", lambda *args: "")
    monkeypatch.setattr(cua, "_run", lambda *args, **kwargs: b'{"isError":true,"content":[{"text":"denied"}]}')
    with pytest.raises(RuntimeError, match="denied"):
        cua.call(None, "s1", "click", {})


def test_remote_call_rejects_remote_screenshot_paths(monkeypatch):
    monkeypatch.setattr(cua, "_ensure_daemon", lambda host, session: ("/tmp/cua.sock", "/tmp/cua-driver"))
    monkeypatch.setattr(cua, "_prefix", lambda host, binary=None: "cua-driver")
    with pytest.raises(ValueError, match="remote screenshot paths"):
        cua.call("pi", "s1", "get_window_state", {"screenshot_out_file": "/tmp/remote.png"})


def test_remote_update_check_forwards_to_host_without_local_update(monkeypatch, capsys):
    calls = []
    class Completed:
        returncode = 0
        stdout = b'{"update_available":true}\n'
    monkeypatch.setattr("subprocess.run", lambda argv, **kwargs: (calls.append((argv, kwargs)) or Completed()))
    args = agent_cli.build_parser().parse_args(["--host", "pi", "update", "--check", "--json"])
    assert agent_cli.dispatch(args) == 0
    argv, _ = calls[0]
    assert argv[:4] == ["ssh", "-T", "--", "pi"]
    assert "--json update --check" in argv[4]
    assert json.loads(capsys.readouterr().out) == {"update_available": True}
