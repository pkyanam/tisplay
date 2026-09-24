"""Pinned Cua Driver adapter for Tisplay sessions.

The Cua process is scoped to a Tisplay session's desktop environment. Its
private Unix socket keeps the native accessibility/runtime state alive across
separate ``tisplay cua call`` invocations without exposing a network listener.
"""
from __future__ import annotations

import json
import base64
import os
import re
import shlex
import subprocess
import sys
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Sequence

from .client import EngineError, SessionClient

_ALLOWED_ENV = {
    "DISPLAY", "XAUTHORITY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME",
    "DBUS_SESSION_BUS_ADDRESS", "TISPLAY_WAYLAND_OUTPUT",
}
_VERSIONED_BINARY = Path("tisplay/cua/current/bin/cua-driver")


def _binary(host: str | None = None) -> str:
    if host:
        result = _ssh(host, 'printf %s "${XDG_DATA_HOME:-$HOME/.local/share}/tisplay/cua/current/bin/cua-driver"')
        if result.returncode or not result.stdout:
            raise RuntimeError("could not locate Cua Driver on remote host; run `tisplay cua install`")
        return result.stdout.decode("utf-8", "strict")
    data = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    path = data / _VERSIONED_BINARY
    if path.is_file() and os.access(path, os.X_OK):
        return str(path)
    raise RuntimeError("Cua Driver is not installed; run `tisplay cua install`")


def _session_key(session: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", session):
        raise ValueError("invalid session ID")
    return session


def _ssh(host: str, command: str, *, input_data: bytes | None = None) -> subprocess.CompletedProcess:
    if host.startswith("-") or not re.fullmatch(r"[A-Za-z0-9_.@:%\[\]-]+", host):
        raise ValueError("SSH destination contains unsupported characters")
    return subprocess.run(["ssh", "-T", "--", host, command], input=input_data,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)


def _session_environment(host: str | None, session: str) -> dict[str, str]:
    with SessionClient(host=host) as client:
        result = client.environment(session)
    values = result.get("environment") or {}
    return {key: str(value) for key, value in values.items()
            if key in _ALLOWED_ENV and isinstance(value, str) and value}


def _ensure_daemon(host: str | None, session: str) -> tuple[str, str]:
    sid = _session_key(session)
    with SessionClient(host=host) as client:
        result = client.request("cua-service", sid, action="start")
    socket = result.get("socket") or result.get("cua_socket")
    binary = result.get("cua_binary")
    if not isinstance(socket, str) or not socket.startswith("/"):
        raise RuntimeError("Tisplay engine did not return the private Cua socket path")
    if not isinstance(binary, str) or not binary.startswith("/"):
        raise RuntimeError("Tisplay engine did not return the installed Cua binary path")
    return socket, binary


def _run(host: str | None, command: str, *, input_data: bytes | None = None) -> bytes:
    result = _ssh(host, command, input_data=input_data) if host else subprocess.run(
        shlex.split(command), input=input_data, stdout=subprocess.PIPE, stderr=None, check=False)
    if result.returncode:
        message = result.stderr.decode("utf-8", "replace")[-2000:] if result.stderr else ""
        raise RuntimeError(message or f"Cua Driver exited with status {result.returncode}")
    return result.stdout


def _prefix(host: str | None, binary: str | None = None) -> str:
    return shlex.join(["env", "CUA_DRIVER_RS_TELEMETRY_ENABLED=false", "CUA_DRIVER_RS_UPDATE_CHECK=false", binary or _binary(host)])


def install(host: str | None = None) -> dict[str, Any]:
    if host:
        result = _ssh(host, 'exec "${XDG_BIN_HOME:-$HOME/.local/bin}/tisplay" --json cua install')
        if result.returncode:
            raise RuntimeError(result.stderr.decode("utf-8", "replace")[-2000:] or f"remote install exited with status {result.returncode}")
        return json.loads(result.stdout)
    from .updater import install_cua
    path = install_cua()
    return {"installed": True, "path": str(path), "version": "0.28.2"}


def inspect(command: str, host: str | None = None, tool: str | None = None) -> bytes:
    prefix = _prefix(host)
    args = [command]
    if tool:
        args.append(tool)
    line = prefix + " " + shlex.join(args)
    return _run(host, line)


def call(host: str | None, session: str, tool: str, arguments: dict[str, Any], out: str | None = None,
         *, raw: bool = False) -> dict[str, Any]:
    args_dict = dict(arguments)
    if host and "screenshot_out_file" in args_dict:
        raise ValueError("remote screenshot paths are not returned to this computer; use Tisplay's local --out option instead")
    socket, binary = _ensure_daemon(host, session)
    prefix = _prefix(host, binary)
    schema_result = inspect("describe", host, tool)
    schema_text = schema_result.decode("utf-8", "replace") if isinstance(schema_result, bytes) else str(schema_result)
    marker = "input_schema:\n"
    if marker in schema_text:
        try:
            schema = json.loads(schema_text.split(marker, 1)[1])
            if "session" in schema.get("properties", {}):
                args_dict.setdefault("session", f"tisplay-{_session_key(session)}")
        except (json.JSONDecodeError, AttributeError, TypeError):
            pass
    if tool == "get_window_state":
        args_dict.setdefault("max_elements", 120)
        args_dict.setdefault("max_depth", 10)
        args_dict.setdefault("max_dimension", 960)
    args = ["call", "--socket", socket, tool, json.dumps(args_dict, separators=(",", ":"))]
    owner = f"tisplay-cua-{secrets.token_hex(6)}"
    with SessionClient(host=host) as client:
        client.control(session, action="acquire", owner=owner, lease_seconds=300)
        try:
            response_bytes = _run(host, prefix + " " + shlex.join(args))
        finally:
            try: client.control(session, action="release", owner=owner)
            except EngineError: pass
    try:
        result = json.loads(response_bytes)
    except json.JSONDecodeError:
        return {"output": response_bytes.decode("utf-8", "replace")}
    saved = _save_image_blocks(result, Path(out).expanduser() if out else None, raw=raw)
    if saved:
        if isinstance(result, dict): result["saved_images"] = saved
        else: result = {"result": result, "saved_images": saved}
    if not raw and isinstance(result, dict):
        result.pop("tree_markdown", None)
        if isinstance(result.get("elements"), list):
            result["summary"] = {"elements_returned": len(result["elements"]),
                                 "elements_total": result.get("total_element_count", result.get("element_count")),
                                 "truncated": not result.get("elements_complete", True)}
    if isinstance(result, dict) and result.get("isError"):
        raise RuntimeError(json.dumps(result, ensure_ascii=False))
    return result if isinstance(result, dict) else {"result": result}


def _save_image_blocks(value: Any, path: Path | None, *, raw: bool = False) -> list[dict[str, Any]]:
    saved: list[dict[str, Any]] = []
    counter = 0
    def visit(node: Any) -> None:
        nonlocal counter
        if isinstance(node, dict):
            image_key = "data" if node.get("type") == "image" and isinstance(node.get("data"), str) else \
                ("screenshot_png_b64" if isinstance(node.get("screenshot_png_b64"), str) else None)
            if image_key and not raw:
                mime = str(node.get("mimeType", node.get("screenshot_mime_type", "image/png")))
                ext = ".jpg" if "jpeg" in mime else (".webp" if "webp" in mime else ".png")
                payload = base64.b64decode(node[image_key], validate=True)
                if path:
                    counter += 1
                    target = path if counter == 1 else path.with_name(f"{path.stem}-{counter}{path.suffix or ext}")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(payload)
                    saved.append({"path": str(target.resolve()), "bytes": len(payload), "mime_type": mime})
                    node["screenshot_file_path"] = str(target.resolve())
                else:
                    node["screenshot_omitted"] = True
                node.pop(image_key, None)
                if image_key == "data": node["bytes"] = len(payload)
            for item in node.values(): visit(item)
        elif isinstance(node, list):
            for item in node: visit(item)
    visit(value)
    return saved


def doctor(host: str | None, session: str | None = None) -> dict[str, Any]:
    prefix = _prefix(host)
    command = prefix
    integration: dict[str, Any] = {"installed_version": "0.28.2", "session_scoped": bool(session),
                                   "support": "experimental; Cua Driver Linux support varies by desktop provider"}
    if session:
        with SessionClient(host=host) as client:
            session_info = client.environment(session)
        values = session_info.get("environment") or {}
        integration.update({"provider": session_info.get("backend"), "mode": session_info.get("mode")})
        command = shlex.join(["env", "CUA_DRIVER_RS_TELEMETRY_ENABLED=false", "CUA_DRIVER_RS_UPDATE_CHECK=false",
                              *[f"{key}={value}" for key, value in values.items()], _binary(host), "doctor", "--json"])
    else:
        command += " doctor --json"
    completed = _ssh(host, command) if host else subprocess.run(shlex.split(command), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        report = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        detail = completed.stderr.decode("utf-8", "replace")[-2000:]
        raise RuntimeError(detail or "Cua Driver doctor did not return JSON") from exc
    report["integration"] = integration
    if completed.returncode:
        report["exit_code"] = completed.returncode
    return report


def mcp(host: str | None, session: str) -> int:
    socket, binary = _ensure_daemon(host, session)
    prefix = _prefix(host, binary)
    command = prefix + " " + shlex.join(["mcp", "--socket", socket])
    owner = f"tisplay-cua-mcp-{secrets.token_hex(6)}"
    with SessionClient(host=host) as client:
        client.control(session, action="acquire", owner=owner, lease_seconds=60)
    stopping = threading.Event()
    renew_failed = threading.Event()
    child: dict[str, subprocess.Popen] = {}
    def renew() -> None:
        while not stopping.wait(20):
            try:
                with SessionClient(host=host) as client:
                    client.control(session, action="acquire", owner=owner, lease_seconds=60)
            except Exception:
                renew_failed.set()
                process = child.get("process")
                if process and process.poll() is None:
                    try: process.terminate()
                    except OSError: pass
                return
    thread = threading.Thread(target=renew, daemon=True)
    thread.start()
    try:
        if host:
            proc = subprocess.Popen(["ssh", "-T", "--", host, command], stdin=sys.stdin.buffer, stdout=sys.stdout.buffer, stderr=None)
        else:
            proc = subprocess.Popen(shlex.split(command), stdin=sys.stdin.buffer, stdout=sys.stdout.buffer, stderr=None)
        child["process"] = proc
        while True:
            try:
                status = proc.wait(timeout=1)
                break
            except subprocess.TimeoutExpired:
                if renew_failed.is_set():
                    try: proc.terminate()
                    except OSError: pass
                    try: proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        try: proc.kill()
                        except OSError: pass
                        try: proc.wait(timeout=2)
                        except subprocess.TimeoutExpired: pass
                    raise RuntimeError("input-control lease renewal failed during Cua MCP session")
        if renew_failed.is_set(): raise RuntimeError("input-control lease renewal failed during Cua MCP session")
        return status
    finally:
        stopping.set(); thread.join(timeout=1)
        proc = child.get("process")
        if proc and proc.poll() is None:
            try: proc.terminate()
            except OSError: pass
            try: proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try: proc.kill()
                except OSError: pass
                try: proc.wait(timeout=2)
                except subprocess.TimeoutExpired: pass
        try:
            with SessionClient(host=host) as client:
                client.control(session, action="release", owner=owner)
        except Exception:
            pass
