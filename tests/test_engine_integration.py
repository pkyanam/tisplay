"""End-to-end session engine check on the project's Xvfb/Xfce CI image."""
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tisplay.client import SessionClient, socket_path
from tisplay.protocol import decode_message, encode_message


DEPS = ("Xvfb", "xauth", "dbus-run-session", "startxfce4", "xfce4-panel", "xprop", "xterm")


@pytest.mark.skipif(not hasattr(os, "uname") or os.uname().sysname != "Linux", reason="Linux desktop integration")
def test_engine_virtual_session_lifecycle_capture_and_input(tmp_path, monkeypatch):
    missing = [name for name in DEPS if not shutil.which(name)]
    if missing:
        pytest.skip(f"Linux desktop integration requirements missing: {', '.join(missing)}")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).parents[1] / "src"))
    server = subprocess.Popen([sys.executable, "-m", "tisplay.daemon"], stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    client = None
    sid = None
    try:
        deadline = time.monotonic() + 10
        while not socket_path().exists() and time.monotonic() < deadline:
            if server.poll() is not None: pytest.fail("tisplay daemon exited during startup")
            time.sleep(.05)
        assert socket_path().exists(), "tisplay daemon socket did not appear"
        bridged = subprocess.run([sys.executable, "-m", "tisplay.cli", "--engine-stdio"],
                                 input=encode_message({"protocol_version": 1, "id": "bridge", "command": "list", "args": {}}),
                                 stdout=subprocess.PIPE, check=True, timeout=10)
        bridge_result = decode_message(bridged.stdout)
        assert bridge_result["id"] == "bridge" and bridge_result["ok"] is True
        client = SessionClient(timeout=60)
        created = client.start(virtual=True, width=800, height=600, name="engine-check",
                              command=["sh", "-c", "sleep 1; exec xterm -geometry 40x10 -e sleep 2"])
        sid = created["session_id"]
        assert client.status(sid)["name"] == "engine-check"
        capture = client.capture(sid, scale=0.5)
        assert capture["png"].startswith("iVBOR")
        assert capture["frame"]["width"] == 400
        assert capture["frame"]["content_id"]
        result = client.input(sid, actions=[{"type": "move", "x": 10, "y": 10}])
        assert result["completed"] == 1
        waited = client.batch(sid, [{"type": "wait", "timeout_ms": 5000, "interval_ms": 100}])
        assert waited["results"] == [{"type": "wait", "changed": True}]
        assert client.stop(sid)["stopped"] is True
        sid = None
    finally:
        if client:
            if sid:
                try: client.stop(sid)
                except Exception: pass
            client.close()
        server.terminate()
        try: server.wait(timeout=5)
        except subprocess.TimeoutExpired: server.kill()
