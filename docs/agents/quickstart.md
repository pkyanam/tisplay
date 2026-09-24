# Agent quickstart

## Discover

Read the installed command surface and backend capabilities first:

```sh
tisplay --help
tisplay capabilities --json
```

Local commands connect to a per-user daemon, which starts on demand. For a remote session, pass an SSH destination with `--host`; the client connects using SSH stdio, so keep SSH authentication and host-key checks enabled.

```sh
tisplay --host alice@workstation capabilities --json
tisplay --host alice@workstation session list --json
```

Avoid shell interpolation for untrusted values. If invoking from code, pass arguments as an array. `--host` is an SSH destination, not a shell snippet.

## Start and inspect a session

Start a private virtual desktop on Linux, save the returned session ID, then inspect and capture it:

```sh
tisplay session start --virtual --name agent-check --json
tisplay session status --session SESSION_ID --json
tisplay screenshot --session SESSION_ID --out screen.png --json
```

For an SSH host, use `tisplay --host alice@workstation ...` before each command. Screenshot files are written on the machine running the client, including when the session is remote. The JSON result includes the resolved local path, file byte count, and frame metadata; the PNG itself is in the file.

## Act

Send only the smallest action needed, using source-pixel coordinates from the primary display. Inspect a fresh screenshot before choosing coordinates. A compact sequence can be sent with `act`:

```sh
tisplay act --session SESSION_ID --actions '[{"type":"click","x":640,"y":360},{"type":"text","text":"hello"}]' --json
```

For longer action lists, use `--actions @actions.json` or `--file actions.json`; use `--file -` for standard input. An action list is ordered. A failure can happen after earlier actions completed, so do not blindly retry an entire list.

## Finish

Stop a session when it is no longer needed:

```sh
tisplay session stop --session SESSION_ID --json
```

`attach` is for a person at an interactive terminal: it streams frames and forwards terminal input, and <kbd>Ctrl</kbd>+<kbd>]</kbd> disconnects. It requires a TTY.

## Platform limits

- Linux uses an accessible X11 desktop. `--virtual` starts a private Xvfb/XFCE desktop. Direct Wayland capture is unsupported.
- macOS uses the logged-in desktop and requires Screen Recording and Accessibility permissions. It has no virtual desktop mode.
- `tisplay capabilities --json` reports capture, pointer, keyboard, Unicode text, virtual resize, remote transport, and control lease support for the current backend. The virtual resize operation currently reports unsupported; stop and restart with desired dimensions. `content_id` changes when captured PNG content changes; geometry-only `frame_id` remains stable during animation.
