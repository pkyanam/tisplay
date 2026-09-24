# Examples

Check the installed CLI and backend before depending on an operation:

```sh
tisplay --help
tisplay capabilities --json
```

## Inspect a remote installation

```sh
tisplay --host alice@workstation capabilities --json
tisplay --host alice@workstation session list --json
```

## Start and capture a virtual Linux desktop

```sh
tisplay session start --virtual --name agent-check --json
tisplay screenshot --session SESSION_ID --out screen.png --json
```

Screenshot `--out` is a local path, even when `--host` points to a remote computer.

## Send a bounded action sequence

```sh
tisplay act --session SESSION_ID --actions '[{"type":"click","x":640,"y":360},{"type":"wait","timeout_ms":2000},{"type":"text","text":"hello"}]' --json
```

A sequence runs in order; by default it stops at the first error, and completed actions are not rolled back. Use `--continue-on-error` to record per-action errors and continue when that is safe. For large lists, store JSON in a file and pass `--actions @actions.json` or `--file actions.json`.

## End the session

```sh
tisplay session stop --session SESSION_ID --json
```

See the [agent quickstart](../quickstart.md) and [CLI/action contract](../protocol.md) for coordinate, lease, and platform details.
