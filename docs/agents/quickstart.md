# Quickstart

Start a session and keep its returned ID for every command:

```sh
tisplay session start --json
```

Take a screenshot, choose an action from what it shows, then inspect the result:

```sh
tisplay screenshot --session SESSION_ID --out screen.png --json
tisplay click --session SESSION_ID --x 640 --y 360 --json
tisplay text --session SESSION_ID 'hello' --json
tisplay key --session SESSION_ID CTRL+L --json
tisplay screenshot --session SESSION_ID --out after.png --json
```

Coordinates are pixels in the primary display. To watch a live session without input control, use `tisplay attach --session SESSION_ID --view-only`. To use another machine, add `--host user@computer` to each command; tisplay uses SSH and opens no listener port.

Stop the session when finished:

```sh
tisplay session stop --session SESSION_ID --json
```

Linux chooses an available supported desktop. `--native` requires an existing native desktop; explicit `--native-headless` can create an isolated labwc/D-Bus session. `--virtual` starts a private Xvfb/XFCE desktop. Native support depends on installed tools and compositor capabilities; inspect `tisplay capabilities --json` on the target. macOS uses the logged-in desktop with Screen Recording and Accessibility permissions.

For action batches, output fields, SSH examples, and the JSONL transport schema, see the [CLI protocol](protocol.md), [examples](examples/README.md), and [schema](../../schemas/agent-v1.schema.json).
