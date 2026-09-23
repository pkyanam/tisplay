# tisplay

View and control a computer's primary desktop from a terminal, including over SSH. Install on the computer whose desktop you want to use:

```sh
curl -fsSL https://raw.githubusercontent.com/pkyanam/tisplay/main/install.sh | bash
```

Then run it locally with `tisplay`, or connect from another computer with:

```sh
ssh -t user@computer tisplay
```

If SSH reports `tisplay: command not found`, use its install path directly:

```sh
ssh -t user@computer '~/.local/bin/tisplay'
```

The installer puts the command in `~/.local/bin` and installs missing system and Python dependencies. Linux gets X11 input support plus Xvfb, Openbox, and xterm for headless use. On macOS, use a logged-in desktop and grant your terminal or Python **Screen Recording** and **Accessibility** permissions when prompted. `--virtual` is for headless Linux only.

`tisplay` shows an interactive desktop inside a terminal. It captures the primary display, streams each frame through the terminal's normal output (including an SSH PTY), and sends keyboard and mouse input back to the captured machine. Nothing listens on a network port and the video does not rely on a shared filesystem.

On Kitty Graphics Protocol terminals, including Ghostty-based Cmux, it sends full-color compressed frames inline. Other terminals use a lower-resolution ANSI true-color renderer. `--graphics auto` probes for Kitty support after checking common terminal markers; use `--graphics kitty` to force the sharp path or `--graphics ansi` for maximum compatibility. Kitty graphics preserve the capture's full color; output is capped at 1600 pixels wide by default to keep SSH traffic manageable.

## Use locally

```sh
tisplay
```

Press `q` or `Ctrl-C` to leave. Keyboard input, clicks, pointer movement, and wheel scrolling are forwarded to the desktop. In macOS, allow the Python executable or terminal app under **System Settings → Privacy & Security → Screen Recording** and **Accessibility** if macOS prompts; restart `tisplay` after granting access.

## Use over SSH

For Cmux or another Kitty-graphics terminal, the remote side receives the same inline graphics protocol over the SSH session. If auto-detection picks ANSI, try `ssh -t user@computer 'tisplay --graphics kitty'`. The SSH client terminal must itself support Kitty graphics for full-resolution output; otherwise force ANSI. Mouse reporting must be enabled by the local terminal, as it is in Cmux.

## Headless Linux

`--virtual` starts a private Xvfb screen and Openbox, then opens xterm as a usable desktop by default. The installer installs these system dependencies. To launch a different application instead:

```sh
ssh -t user@server 'tisplay --virtual -- firefox --no-remote'
ssh -t user@server 'tisplay --virtual --width 1600 --height 900 -- xfce4-session'
```

The virtual desktop and launched command stop when `tisplay` exits. Linux Wayland capture is not currently supported directly; use an X11 session or `--virtual`. macOS has no virtual-display mode and needs an active logged-in desktop.

## Options

```text
--graphics auto|kitty|ansi   renderer selection (default auto)
--fps N                     refresh cap, 1 to 60 (default 12)
--max-width PX              capture width cap (default 1600)
--virtual                   start private Xvfb desktop on Linux
--width PX --height PX      virtual desktop size (default 1280x800)
```

## Development

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e '.[linux]'
python3 -m pytest
```

The automated tests cover stream encoding and input mapping. Hardware capture, platform permission prompts, and live Cmux rendering need a real display and terminal and are not simulated by those tests.
