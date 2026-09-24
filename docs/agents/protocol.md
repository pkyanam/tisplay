# CLI and action contract

The supported machine interface is the CLI. Run `tisplay --help` and command-specific `--help` on the installed version. Most operations accept `--json`, which writes a JSON result to stdout; diagnostics go to stderr and failures return a nonzero process status. The [versioned JSON Schema](../../schemas/agent-v1.schema.json) describes the current private JSONL envelope used by the local daemon and SSH stdio bridge; it is not the CLI argument format or a promise that direct daemon access is a supported API.

## Global options and sessions

- `--host DESTINATION` selects SSH transport; omit it to use the local per-user daemon. It can appear before a command or on a command that accepts common options.
- `session start [--mode auto|native-existing|native-headless|virtual] [--name LABEL] [--width PX --height PX] [--preset quality|balanced|fast]` creates a session and returns its ID. `--virtual`, `--native`, and `--native-headless` are mode aliases. Explicit native headless mode may start an isolated labwc/D-Bus session; it does not replace or reconfigure the system desktop.
- `session list`, `session status --session ID`, and `session stop --session ID` enumerate, inspect, and stop sessions.
- `session resize --session ID WIDTH HEIGHT` is currently unsupported by the backend. Stop and restart with the desired dimensions.
- `screenshot --session ID [--out PATH] [--scale 0.1–1] [--max-width PX] [--region X Y WIDTH HEIGHT]` writes a PNG to a local path. JSON output reports the path and frame metadata, not base64 image bytes.
- `attach --session ID [--view-only]` provides a live interactive terminal stream and requires a TTY. `--view-only` watches the screen without acquiring input control.

Pass IDs explicitly. For SSH, commands execute through the host's SSH connection using stdio; no tisplay listening port is opened.

## Input actions

`act --session ID --actions JSON` takes a JSON array of action objects. The versioned schema validates the internal request envelope and shared action object forms; the CLI accepts the action array directly. `--actions @path.json` reads that path, `--file path.json` is an alternative, and `--file -` reads stdin. Up to 500 actions are accepted. Actions run in order. By default, execution stops at the first error; `--continue-on-error` records per-action errors and continues. Earlier actions are not rolled back.

Supported objects:

```json
{"type":"move","x":640,"y":360}
{"type":"click","x":640,"y":360,"button":"left"}
{"type":"double_click","x":640,"y":360}
{"type":"drag","from_x":10,"from_y":10,"to_x":200,"to_y":200,"button":"left"}
{"type":"scroll","x":640,"y":360,"delta_y":-3,"delta_x":0}
{"type":"key","name":"ctrl+l"}
{"type":"key_down","name":"shift"}
{"type":"key_up","name":"shift"}
{"type":"text","text":"hello"}
{"type":"wait","timeout_ms":1000,"interval_ms":100}
```

`screenshot` is also available as `observe` or `state`; `text` as `type-text`; and `key` as `press-key`. `click --x X --y Y` and positional coordinates are both accepted. `open-url --session ID URL` opens an HTTP(S) URL in the session desktop. Coordinates are integer pixels local to the primary monitor. Screenshot `--scale`, `--max-width`, and `--region` affect image output only; input coordinates remain source-display pixels. A capture frame's `frame_id` is based on display geometry, so animation does not expire it; including that value on an action prevents acting after the display geometry changes. `content_id` identifies the returned PNG bytes. Coordinates are checked against current primary-display bounds.

Key `name` can be one key or a chord joined by `+`, such as `ctrl+c`. `text` accepts up to 10,000 characters and requires the relevant backend's Unicode text support. Scroll deltas are limited to 100 steps in either direction. The batch wait action bounds timeout at 30 seconds and polling interval to 20–1000 ms.

## Exclusive control

`control acquire|release|status --session ID` manages an input lease. The default owner is `agent`; acquire can supply `--owner LABEL` and `--lease-seconds N` (1–3600, default 60). Pass the same owner to `release` when releasing a named lease. If another owner holds the lease, input is rejected. `key_down` also requires an active matching lease; ordinary `key` presses do not hold a key beyond the action. `attach` acquires a unique lease owner, renews the lease while connected, and releases it on disconnect. Held keys/buttons are released on lease cleanup.

## Capabilities and errors

`capabilities [--session ID] --json` reports support for capture, pointer, keyboard, Unicode text, virtual resize, remote transport, and control leases for the selected backend. Native support depends on the compositor, active output, and installed tools. Text support can depend on installed backend tools. The current engine reports `resize: false`.

Capabilities include `engine_version` and `engine_generation`. After an upgrade, existing viewers keep running, but their session IDs belong to the prior generation; start a fresh session for new commands.

A failed request is reported as `tisplay: CODE: message` on stderr and exits nonzero. Errors such as unsupported operations, stale geometry, invalid input, and a busy control lease should be surfaced to the caller. Do not automatically repeat a potentially state-changing input request after an ambiguous failure.

## Internal capture grounding and CUA service

The private JSONL protocol also supports `environment` and `cua-service` requests for session-aware adapters. `environment` requires a session and returns an allowlist of display variables, backend/mode metadata, and the expected private CUA socket path. `cua-service` requires `args.action` (`start`, `status`, or `stop`) and manages one Cua Driver process and private Unix socket for that Tisplay session; its response includes `cua_socket` and the selected `cua_binary` path. The service is stopped when the session stops. These commands are internal protocol operations, not additional CLI commands.

A normal `capture` registers an opaque `frame.capture_id` observation token. It is distinct from the geometry-only `frame_id`. Supply the capture token at the input request level, on an individual input action, or at the batch level to ground coordinates in that screenshot. The daemon maps screenshot pixels through the captured image scale and crop into monitor coordinates; legacy full-display coordinates remain available when no capture token is supplied. A token is valid for 60 seconds, at most 16 observations are retained per session, and the token is consumed by one input call. Unknown, expired, reused, or layout-stale tokens fail with `stale_capture`.

Capture requests may set `register_capture: false` for transient viewer polling. Such captures omit `frame.capture_id` and do not evict retained agent observations; this is intended for streaming and polling paths. Omit the field (or set it to `true`) when the returned image will ground a later input action.
