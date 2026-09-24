# tisplay

View and control a computer's primary desktop from a terminal, including over SSH. Install on the computer whose desktop you want to use:

```sh
curl -fsSL https://github.com/pkyanam/tisplay/raw/refs/heads/main/install.sh | bash
```

Then run it locally with `tisplay`, or connect from another computer with:

```sh
ssh -t user@computer '~/.local/bin/tisplay'
```

Or add `~/.local/bin` to the remote account's `PATH` and run `tisplay`:

```sh
ssh -t user@computer 'PATH="$HOME/.local/bin:$PATH" tisplay'
```

The installer puts the command in `~/.local/bin` and installs missing system and Python dependencies. Linux gets X11 input support and, for headless use, Xvfb, Xauthority tools, D-Bus, and the Xfce desktop (session, panel, window manager, desktop, file manager, and terminal). This adds a lightweight desktop environment to the system. On macOS, use a logged-in desktop and grant your terminal or Python **Screen Recording** and **Accessibility** permissions when prompted. `--virtual` is for headless Linux only.

`tisplay` shows an interactive desktop inside a terminal. On Linux, it uses an accessible X11 display; if no display is available, it starts a private virtual desktop automatically. It streams each frame through the terminal's normal output (including an SSH PTY), and sends keyboard and mouse input back to the captured machine. Nothing listens on a network port and the video does not rely on a shared filesystem.

On Kitty Graphics Protocol terminals, including Ghostty-based Cmux, it sends full-color losslessly compressed frames inline. Other terminals use an ANSI true-color half-block renderer, which samples two vertical pixels per terminal cell. `--graphics auto` probes for Kitty support after checking common terminal markers; use `--graphics kitty` to force the sharp path or `--graphics ansi` for maximum compatibility. The default `balanced` preset targets up to 60 frames per second, uses a 1600×900 virtual desktop when one is launched, and caps Kitty capture at 1600 pixels wide to manage SSH traffic; identical captured frames are not resent. `quality` uses a 1920×1080 virtual desktop when launched, preserves source resolution, and uses stronger lossless compression. `fast` uses a 1280×800 virtual desktop when launched, caps capture at 960 pixels wide, and targets 30 fps. Presets set starting values; explicit `--fps`, `--max-width`, `--width`, and `--height` options override them. Compression level affects CPU use and bandwidth only, not image fidelity.

To check the terminal's graphics output without opening an X11 session or reading the desktop, run `tisplay --test-pattern`. It displays four color fields with a `TISPLAY TEST` label through the same renderer and quits with `Ctrl-]`.

## Use locally

```sh
tisplay
```

Press `Ctrl-]` to leave. Keyboard input, clicks, pointer movement, and wheel scrolling are forwarded to the desktop; `Ctrl-C` is sent to the desktop as a key chord. In macOS, allow the Python executable or terminal app under **System Settings → Privacy & Security → Screen Recording** and **Accessibility** if macOS prompts; restart `tisplay` after granting access.

## Use over SSH

For Cmux or another Kitty-graphics terminal, the remote side receives the same inline graphics protocol over the SSH session. For the sharpest stream, use `ssh -t user@computer '~/.local/bin/tisplay --preset quality'`. If auto-detection picks ANSI, try `ssh -t user@computer '~/.local/bin/tisplay --graphics kitty'`. The SSH client terminal must itself support Kitty graphics for full-resolution output; otherwise force ANSI. Mouse reporting must be enabled by the local terminal, as it is in Cmux.

`--fps 60` is a refresh target, not a guarantee: capture speed, desktop motion, compression work, terminal redraw time, SSH bandwidth, and the receiving terminal all limit the delivered rate. Frames do not queue behind slow output, so input remains responsive and the stream resumes at the pace the machine can sustain. The ANSI fallback cannot provide pixel graphics or crisp source-resolution detail; it is restricted to the terminal's character-cell grid. A smaller terminal font can show more samples, but Kitty graphics are needed for detailed images.

On Linux, an unset `DISPLAY` makes `tisplay` look for a reachable local X11 desktop (using the account's normal X11 authorization). If it cannot access one, it starts Xvfb, Openbox, and xterm automatically. To explicitly select a particular X11 display, set `DISPLAY`, for example `ssh -t user@computer 'DISPLAY=:0 ~/.local/bin/tisplay'`; a configured but inaccessible display reports its error instead of switching desktops.

## Headless Linux

If no X11 desktop is accessible, the default command starts a private Xvfb screen and a complete Xfce session, including its panel, window manager, desktop, and applications menu. It authenticates the private X server with a temporary Xauthority cookie and waits for the window manager and panel before capturing. Use `--virtual` to force this mode even when an X11 display is available. The installer installs these system dependencies. To launch a different application inside that desktop:

```sh
ssh -t user@server '~/.local/bin/tisplay --virtual -- firefox --no-remote'
ssh -t user@server '~/.local/bin/tisplay --virtual --width 1600 --height 900 -- firefox --no-remote'
```

The virtual desktop and launched command stop when `tisplay` exits. The headless session is Xfce; it is a normal interactive desktop session without a login manager. Linux Wayland capture is not currently supported directly; `tisplay` does not capture the primary Wayland desktop. Use an accessible X11 session or the virtual desktop. macOS has no virtual-display mode and needs an active logged-in desktop.

## Options

```text
--graphics auto|kitty|ansi   renderer selection (default auto)
--preset quality|balanced|fast performance and resolution defaults (default balanced)
--fps N                     refresh target, 1 to 60 (default comes from preset; balanced: 60)
--max-width PX              maximum capture width (balanced: 1600, fast: 960, quality: uncapped)
--virtual                   force a full Xfce desktop on Xvfb on Linux
--test-pattern              show synthetic color fields without desktop access
--width PX --height PX      virtual desktop size (default comes from preset; balanced: 1600x900)
```

## Development

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e '.[linux]'
python3 -m pytest
```

The automated tests cover stream encoding and input mapping. Hardware capture, platform permission prompts, and live Cmux rendering need a real display and terminal and are not simulated by those tests.

See [CONTRIBUTING.md](CONTRIBUTING.md) for development and contribution guidance, and [SECURITY.md](SECURITY.md) to report a vulnerability.
