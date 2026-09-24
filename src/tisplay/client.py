"""Client for the per-user tisplay session daemon."""
from __future__ import annotations

import os
import re
import secrets
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .protocol import MAX_MESSAGE_BYTES, PROTOCOL_VERSION, decode_message, encode_message

# Generation 3 adds screenshot grounding and session-owned Cua runtimes. An
# older daemon must not silently accept requests with these semantics missing.
ENGINE_GENERATION = 3


class EngineError(RuntimeError):
    def __init__(self, message: str, code: str = "engine_error", details: Any = None):
        super().__init__(message)
        self.code, self.details = code, details


def socket_path() -> Path:
    base = Path(os.environ.get("XDG_RUNTIME_DIR", f"/tmp/tisplay-{os.getuid()}"))
    return base / "tisplay" / f"engine-v{ENGINE_GENERATION}.sock"


class SessionClient:
    def __init__(self, host: str | None = None, ssh_options: list[str] | None = None, timeout: float = 45):
        self.host, self.ssh_options, self.timeout = host, ssh_options or [], timeout
        self._proc: subprocess.Popen | None = None
        self._sock: socket.socket | None = None
        self._stream = None
        if host:
            if host.startswith("-") or not re.fullmatch(r"[A-Za-z0-9_.@:%\[\]-]+", host):
                raise EngineError("SSH destination contains unsupported characters", "invalid_host")
            remote_command = 'exec "${XDG_BIN_HOME:-$HOME/.local/bin}/tisplay" --engine-stdio'
            self._proc = subprocess.Popen(["ssh", "-T", *self.ssh_options, "--", host, remote_command], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None)
            self._stream = self._proc
        else:
            self._connect_local()

    def _connect_local(self) -> None:
        path = socket_path()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        parent = path.parent.lstat()
        if stat.S_ISLNK(parent.st_mode) or parent.st_uid != os.getuid():
            raise EngineError("unsafe tisplay runtime directory", "unsafe_socket")
        os.chmod(path.parent, 0o700)
        if path.exists() or path.is_symlink():
            candidate = path.lstat()
            if not stat.S_ISSOCK(candidate.st_mode) or candidate.st_uid != os.getuid():
                raise EngineError("unsafe tisplay socket path", "unsafe_socket")
        for attempt in range(2):
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(self.timeout)
            try:
                sock.connect(str(path))
                self._sock = sock
                self._stream = sock.makefile("rwb")
                return
            except OSError:
                sock.close()
                if attempt:
                    raise EngineError("Could not connect to tisplay daemon", "daemon_unavailable")
                subprocess.Popen([sys.executable, "-m", "tisplay.daemon"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    time.sleep(.05)

    def request(self, verb: str, session: str | None = None, **args: Any) -> dict[str, Any]:
        if not self._stream:
            raise EngineError("client is closed", "client_closed")
        req = {"protocol_version": PROTOCOL_VERSION, "id": secrets.token_hex(8), "command": verb, "args": args}
        if session is not None:
            req["session"] = session
        try:
            self._stream.stdin.write(encode_message(req)) if self.host else self._stream.write(encode_message(req))
            self._stream.stdin.flush() if self.host else self._stream.flush()
            line = self._stream.stdout.readline(MAX_MESSAGE_BYTES + 1) if self.host else self._stream.readline(MAX_MESSAGE_BYTES + 1)
            if not line:
                raise EngineError("tisplay daemon closed the connection", "daemon_disconnected")
            response = decode_message(line)
            if response.get("protocol_version") != PROTOCOL_VERSION or response.get("id") != req["id"]:
                raise EngineError("response does not match the request protocol version and id", "protocol_error")
        except (OSError, ValueError) as exc:
            raise EngineError(str(exc), "protocol_error") from exc
        if not response.get("ok"):
            error = response.get("error") or {}
            raise EngineError(error.get("message", "request failed"), error.get("code", "engine_error"), error.get("details"))
        return response.get("result", {})

    def start(self, **args: Any): return self.request("start", **args)
    def list(self): return self.request("list")
    def status(self, session: str): return self.request("status", session)
    def resize(self, session: str, **args: Any): return self.request("resize", session, **args)
    def stop(self, session: str): return self.request("stop", session)
    def capture(self, session: str | None = None, **args: Any): return self.request("capture", session, **args)
    def open_url(self, session: str, url: str, owner: str | None = None):
        return self.request("open-url", session, url=url, **({"owner": owner} if owner else {}))
    def input(self, session: str, actions: Any, owner: str | None = None): return self.request("input", session, actions=actions, **({"owner": owner} if owner else {}))
    def batch(self, session: str, actions: Any, owner: str | None = None, **args: Any): return self.request("batch", session, actions=actions, **({"owner": owner} if owner else {}), **args)
    def control(self, session: str, action: str, **args: Any): return self.request("control", session, action=action, **args)
    def capabilities(self, session: str | None = None): return self.request("capabilities", session)
    def environment(self, session: str): return self.request("environment", session)

    def close(self) -> None:
        if self._stream:
            if self._proc:
                for pipe in (self._proc.stdin, self._proc.stdout):
                    try:
                        if pipe: pipe.close()
                    except OSError: pass
            else:
                try: self._stream.close()
                except OSError: pass
            self._stream = None
        if self._sock:
            self._sock.close(); self._sock = None
        if self._proc:
            self._proc.terminate()
            try: self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired: self._proc.kill()
            self._proc = None

    def __enter__(self): return self
    def __exit__(self, *_): self.close()
