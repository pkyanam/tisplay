"""Shared JSONL protocol definitions for the tisplay session engine."""
from __future__ import annotations

import json
from typing import Any

PROTOCOL_VERSION = 1
MAX_MESSAGE_BYTES = 8 * 1024 * 1024


def encode_message(value: dict[str, Any]) -> bytes:
    data = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"
    if len(data) > MAX_MESSAGE_BYTES:
        raise ValueError("protocol message exceeds size limit")
    return data


def decode_message(data: bytes) -> dict[str, Any]:
    if len(data) > MAX_MESSAGE_BYTES:
        raise ValueError("protocol message exceeds size limit")
    value = json.loads(data.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("protocol message must be a JSON object")
    return value
