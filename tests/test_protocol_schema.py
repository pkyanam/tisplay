"""Regression checks for the private JSONL request schema."""

import json
from pathlib import Path

import pytest

jsonschema = pytest.importorskip("jsonschema")


@pytest.fixture(scope="module")
def request_validator():
    schema = json.loads((Path(__file__).parents[1] / "schemas/agent-v1.schema.json").read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    request_schema = dict(schema, **{"$ref": "#/$defs/request"})
    return jsonschema.Draft202012Validator(request_schema)


def test_schema_accepts_capture_grounding_and_session_adapter_requests(request_validator):
    examples = [
        {"protocol_version": 1, "id": "capture", "command": "capture", "session": "s1",
         "args": {"max_width": 800, "register_capture": False}},
        {"protocol_version": 1, "id": "input", "command": "input", "session": "s1",
         "args": {"capture_id": "opaque-token", "actions": [{"type": "click", "x": 12, "y": 20}]}},
        {"protocol_version": 1, "id": "batch", "command": "batch", "session": "s1",
         "args": {"capture_id": "opaque-token", "actions": [{"type": "move", "x": 12, "y": 20,
                                                                  "capture_id": "opaque-token"}]}},
        {"protocol_version": 1, "id": "env", "command": "environment", "session": "s1", "args": {}},
        {"protocol_version": 1, "id": "start-temporary", "command": "start",
         "args": {"mode": "virtual", "idle_ttl": 900}},
        *({"protocol_version": 1, "id": f"cua-{action}", "command": "cua-service", "session": "s1",
           "args": {"action": action}} for action in ("start", "status", "stop")),
        *({"protocol_version": 1, "id": f"viewer-{action}", "command": "viewer", "session": "s1",
           "args": {"action": action, "viewer_id": "viewer-a:123"}}
          for action in ("connect", "heartbeat", "disconnect")),
    ]
    for request in examples:
        request_validator.validate(request)


def test_schema_rejects_invalid_capture_and_cua_service_shapes(request_validator):
    invalid = [
        {"protocol_version": 1, "id": "bad-capture", "command": "capture", "session": "s1",
         "args": {"register_capture": "false"}},
        {"protocol_version": 1, "id": "bad-input", "command": "input", "session": "s1",
         "args": {"capture_id": "", "actions": [{"type": "click", "x": 1, "y": 1}]}},
        {"protocol_version": 1, "id": "bad-service", "command": "cua-service", "session": "s1",
         "args": {"action": "restart"}},
        {"protocol_version": 1, "id": "no-session", "command": "environment", "args": {}},
        {"protocol_version": 1, "id": "bad-ttl-low", "command": "start", "args": {"idle_ttl": 0}},
        {"protocol_version": 1, "id": "bad-ttl-high", "command": "start", "args": {"idle_ttl": 86401}},
        {"protocol_version": 1, "id": "bad-ttl-type", "command": "start", "args": {"idle_ttl": True}},
        {"protocol_version": 1, "id": "bad-viewer-action", "command": "viewer", "session": "s1",
         "args": {"action": "reconnect", "viewer_id": "viewer-a"}},
        {"protocol_version": 1, "id": "bad-viewer-id", "command": "viewer", "session": "s1",
         "args": {"action": "connect", "viewer_id": "not safe"}},
        {"protocol_version": 1, "id": "viewer-no-session", "command": "viewer",
         "args": {"action": "connect", "viewer_id": "viewer-a"}},
    ]
    for request in invalid:
        assert not request_validator.is_valid(request)
