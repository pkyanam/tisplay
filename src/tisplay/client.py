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

# Generation 4 adds reconnectable default viewer lifetimes. Keep generation 3
# available for explicit sessions owned by an already-running older daemon.
ENGINE_GENERATION = 4
LEGACY_ENGINE_GENERATION = 3


class EngineError(RuntimeError):
    def __init__(self, message: str, code: str = "engine_error", details: Any = None):
        super().__init__(message)
        self.code, self.details = code, details


def socket_path(generation: int | None = None, runtime_dir: str | None = None) -> Path:
    if generation is not None and generation not in (LEGACY_ENGINE_GENERATION, ENGINE_GENERATION):
        raise ValueError(f"unsupported tisplay engine generation: {generation}")
    base = Path(runtime_dir or os.environ.get("XDG_RUNTIME_DIR", f"/tmp/tisplay-{os.getuid()}"))
    return base / "tisplay" / f"engine-v{generation or ENGINE_GENERATION}.sock"


class SessionClient:
    def __init__(self, host: str | None = None, ssh_options: list[str] | None = None, timeout: float = 45,
                 generation: int = ENGINE_GENERATION, legacy_fallback: bool = True,
                 spawn_daemon: bool = True, runtime_dir: str | None = None):
        self.host, self.ssh_options, self.timeout = host, ssh_options or [], timeout
        if generation not in (LEGACY_ENGINE_GENERATION, ENGINE_GENERATION):
            raise EngineError("unsupported tisplay engine generation", "invalid_generation")
        self.generation = generation
        self.legacy_fallback = legacy_fallback
        # Generation 3 is read/operate-only compatibility for a daemon that
        # was already running; never resurrect an obsolete daemon binary.
        self.spawn_daemon = bool(spawn_daemon and generation == ENGINE_GENERATION)
        self.runtime_dir = runtime_dir or os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/tisplay-{os.getuid()}"
        self._proc: subprocess.Popen | None = None
        self._sock: socket.socket | None = None
        self._stream = None
        self._legacy_client: SessionClient | None = None
        self._legacy_sessions: set[str] = set()
        if host:
            if host.startswith("-") or not re.fullmatch(r"[A-Za-z0-9_.@:%\[\]-]+", host):
                raise EngineError("SSH destination contains unsupported characters", "invalid_host")
            generation_arg = f" --generation {generation}" if generation != ENGINE_GENERATION else ""
            remote_command = f'exec "${{XDG_BIN_HOME:-$HOME/.local/bin}}/tisplay" --engine-stdio{generation_arg}'
            self._proc = subprocess.Popen(["ssh", "-T", *self.ssh_options, "--", host, remote_command], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None)
            self._stream = self._proc
        else:
            self._connect_local()

    def _connect_local(self) -> None:
        path = socket_path(self.generation, self.runtime_dir)
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
            except OSError as exc:
                sock.close()
                if attempt:
                    raise EngineError("Could not connect to tisplay daemon", "daemon_unavailable") from exc
                if not self.spawn_daemon:
                    raise EngineError("legacy tisplay daemon is not running", "daemon_unavailable") from exc
                subprocess.Popen([sys.executable, "-m", "tisplay.daemon"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
                deadline = time.monotonic() + 5
                last_error = exc
                while time.monotonic() < deadline:
                    remaining = deadline - time.monotonic()
                    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    probe.settimeout(min(self.timeout, max(.01, remaining)))
                    try:
                        probe.connect(str(path))
                        probe.settimeout(self.timeout)
                        self._sock = probe
                        self._stream = probe.makefile("rwb")
                        return
                    except OSError as probe_error:
                        last_error = probe_error
                        probe.close()
                        remaining = deadline - time.monotonic()
                        if remaining > 0:
                            time.sleep(min(.05, remaining))
                raise EngineError("Timed out waiting for the tisplay daemon to start", "daemon_unavailable") from last_error

    def _request_once(self, verb: str, session: str | None = None, **args: Any) -> dict[str, Any]:
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

    def _legacy_request(self, verb: str, session: str | None = None, **args: Any) -> dict[str, Any]:
        client = getattr(self, "_legacy_client", None)
        if client is None:
            client = SessionClient(host=self.host, ssh_options=self.ssh_options, timeout=self.timeout,
                                   generation=LEGACY_ENGINE_GENERATION, legacy_fallback=False,
                                   spawn_daemon=False, runtime_dir=self.runtime_dir)
            self._legacy_client = client
        result = client._request_once(verb, session, **args)
        return result

    def request(self, verb: str, session: str | None = None, **args: Any) -> dict[str, Any]:
        if session and session in getattr(self, "_legacy_sessions", set()):
            return self._legacy_request(verb, session, **args)
        try:
            result = self._request_once(verb, session, **args)
        except EngineError as exc:
            if self.generation == ENGINE_GENERATION and self.legacy_fallback and session and exc.code == "not_found":
                try:
                    # Confirm the session itself is absent from v4; an
                    # operation-level not_found must never be replayed on v3.
                    self._request_once("status", session)
                except EngineError as current_error:
                    if current_error.code != "not_found":
                        raise
                else:
                    raise exc
                try:
                    # Resolve ownership with a read-only probe first. A
                    # mutation's not_found response can describe the action,
                    # not the session; only route when both ownership probes
                    # establish that v4 does not own this ID and v3 does.
                    self._legacy_request("status", session)
                except EngineError as legacy_error:
                    if legacy_error.code not in ("daemon_unavailable", "not_found"):
                        raise
                    raise exc
                self._legacy_sessions.add(session)
                return self._legacy_request(verb, session, **args)
            raise
        if verb == "list" and self.generation == ENGINE_GENERATION and self.legacy_fallback:
            try:
                legacy = self._legacy_request("list")
            except EngineError as exc:
                if exc.code != "daemon_unavailable":
                    raise
                return result
            seen = {item.get("session_id") for item in result.get("sessions", [])}
            legacy_sessions = legacy.get("sessions", [])
            known_legacy = getattr(self, "_legacy_sessions", None)
            if known_legacy is None:
                known_legacy = self._legacy_sessions = set()
            known_legacy.update(item["session_id"] for item in legacy_sessions
                                if item.get("session_id") and item["session_id"] not in seen)
            result["sessions"] = result.get("sessions", []) + [item for item in legacy_sessions
                                                                 if item.get("session_id") not in seen]
        return result

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
    def viewer(self, session: str, action: str, viewer_id: str, **args: Any):
        return self.request("viewer", session, action=action, viewer_id=viewer_id, **args)

    def close(self) -> None:
        if self._legacy_client:
            self._legacy_client.close()
            self._legacy_client = None
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
