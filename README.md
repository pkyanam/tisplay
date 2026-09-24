<div align="center">

# tisplay

**See and control a computer's desktop from your terminal—even over SSH.**

[Project site](https://pkyanam.github.io/tisplay/) · [Agent quickstart](https://pkyanam.github.io/tisplay/agents/) · [Source and issues](https://github.com/pkyanam/tisplay) · [Releases](https://github.com/pkyanam/tisplay/releases)

[![Linux desktop integration](https://github.com/pkyanam/tisplay/actions/workflows/linux-desktop.yml/badge.svg)](https://github.com/pkyanam/tisplay/actions/workflows/linux-desktop.yml)
[![MIT License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)

[Install](#install) · [Usage](#usage) · [Presets](#presets) · [Options](#options) · [Development](#development)

<img src="docs/assets/tisplay-cmux-desktop.png" alt="A Linux desktop streamed inline in a Cmux terminal, with the tisplay status bar and SSH session visible" width="920">

*A remote desktop streamed inline in Cmux over SSH.*

</div>

## Install

Run this on the computer whose desktop you want to access. The installer places `tisplay` in `~/.local/bin`. Its default `virtual` profile installs the Xvfb/XFCE fallback. Choose `native` to install the managed labwc/Wayland provider tools; on Raspberry Pi OS, the desktop session package is added when available from the configured apt sources. Choose `minimal` for Python prerequisites only.

```sh
curl -fsSL https://github.com/pkyanam/tisplay/raw/refs/heads/main/install.sh | bash
```

To select a different Linux dependency profile, pass the option to Bash:

```sh
curl -fsSL https://github.com/pkyanam/tisplay/raw/refs/heads/main/install.sh | bash -s -- --profile native
```

## Usage

Start a local session with:

```sh
tisplay
```

Or run it on a remote computer over SSH:

```sh
ssh -t user@computer '~/.local/bin/tisplay'
```

If `~/.local/bin` is on the remote account's `PATH`, you can use `ssh -t user@computer 'tisplay'` instead. The SSH session carries the stream and input; `tisplay` does not open a network port or require a shared filesystem.

`Ctrl-]` exits `tisplay`. Keyboard input, clicks, pointer movement, and scrolling are forwarded to the desktop. `Ctrl-C` is sent to the desktop as a key chord.

### Displays and terminals

On Linux, `tisplay` automatically selects an available supported desktop, then uses the installed virtual fallback when no native display is available. Use `--native` to require a supported existing native desktop, `--native-headless` to create or use an isolated headless Wayland compositor, or `--virtual` to force a private Xvfb/XFCE desktop. Existing native sessions are reused without changing the system compositor. Append `--` and a command to launch an application in a virtual desktop:

```sh
ssh -t user@server '~/.local/bin/tisplay --virtual -- firefox --no-remote'
```

Native Wayland support depends on the active compositor, installed tools, and reported capabilities. `--native-headless` can start an isolated labwc/D-Bus session when needed; it does not replace or reconfigure the system desktop. On macOS, use a logged-in desktop and grant your terminal or Python **Screen Recording** and **Accessibility** permissions when prompted; macOS has no virtual-display mode.

Kitty Graphics Protocol terminals, including Ghostty-based Cmux, show full-color losslessly compressed frames inline. Other terminals use an ANSI true-color half-block renderer, which fits two vertical pixels into each character cell. For detailed images, the SSH client's terminal must support Kitty graphics; otherwise `--graphics ansi` selects the compatible fallback.

## Presets

The default `balanced` preset targets up to 60 fps, starts a 1600×900 virtual desktop, and caps Kitty capture at 1600 pixels wide. `quality` starts a 1920×1080 virtual desktop, keeps the source resolution, and uses stronger lossless compression. `fast` starts a 1280×800 virtual desktop, caps capture at 960 pixels wide, and targets 30 fps.

These are starting values: explicit `--fps`, `--max-width`, `--width`, and `--height` options override them. The refresh rate is a target; capture speed, desktop motion, compression, terminal redraws, and SSH bandwidth determine the delivered rate. Frames do not queue behind slow output, so input remains responsive. Compression changes CPU use and bandwidth, not image fidelity.

## Options

```text
--graphics auto|kitty|ansi   renderer selection (default: auto)
--preset quality|balanced|fast performance and resolution defaults (default: balanced)
--fps N                     refresh target, 1 to 60 (default comes from preset)
--max-width PX              capture width cap (default comes from preset)
--virtual                   force a full Xfce desktop on Xvfb (Linux)
--native                    require a supported existing native desktop
--native-headless           use an isolated headless Wayland desktop
--test-pattern              show synthetic color fields without desktop access
--width PX --height PX      virtual desktop size (default comes from preset)
```

To check terminal graphics without opening a desktop, run `tisplay --test-pattern`. It displays four color fields through the selected renderer; press `Ctrl-]` to quit.

Automation can select `session start --mode auto|native-existing|native-headless|virtual`; the `--virtual`, `--native`, and `--native-headless` aliases remain available. Use `tisplay attach --view-only` to watch a session without input control. Check `tisplay capabilities` on the target before relying on a backend or operation: compositor and output support varies by machine.

Upgraded clients use a new engine socket generation so a daemon from an older installation can keep serving existing attached viewers. After upgrading, start a new session to use the new client and native features; old session IDs remain with the old daemon until it is stopped.

## Development

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e '.[linux]'
python3 -m pytest
```

Automated tests cover stream encoding and input mapping. Hardware capture, platform permission prompts, and live Cmux rendering require a real display and terminal.

See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution guidance and [SECURITY.md](SECURITY.md) to report a vulnerability.
