#!/usr/bin/env bash
set -euo pipefail

REPO="https://github.com/pkyanam/tisplay"
APP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/tisplay"
VENV="$APP_DIR/venv"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

fail() { printf 'tisplay installer: %s\n' "$*" >&2; exit 1; }
need_sudo() {
  if [ "$(id -u)" -eq 0 ]; then SUDO=; return; fi
  command -v sudo >/dev/null 2>&1 || fail "sudo is required to install system packages."
  if [ ! -t 0 ] && [ ! -t 1 ]; then fail "system dependencies are missing; rerun this command in an interactive terminal so sudo can ask for your password."; fi
  sudo -v || fail "could not obtain sudo access."
  SUDO=sudo
}

OS="$(uname -s)"
if [ "$OS" = Linux ]; then
  if command -v apt-get >/dev/null 2>&1; then
    PKG=apt-get
    PACKAGES=(python3 python3-venv python3-pip python3-dev build-essential libx11-dev libxtst-dev libevdev-dev xvfb openbox xterm x11-utils)
  elif command -v dnf >/dev/null 2>&1; then
    PKG=dnf
    PACKAGES=(python3 python3-pip python3-devel gcc gcc-c++ make libX11-devel libXtst-devel libevdev-devel xorg-x11-server-Xvfb openbox xterm xrandr)
  elif command -v yum >/dev/null 2>&1; then
    PKG=yum
    PACKAGES=(python3 python3-pip python3-devel gcc gcc-c++ make libX11-devel libXtst-devel libevdev-devel xorg-x11-server-Xvfb openbox xterm xrandr)
  elif command -v pacman >/dev/null 2>&1; then
    PKG=pacman
    PACKAGES=(python python-pip base-devel libx11 libxtst libevdev xorg-server-xvfb openbox xterm xorg-xrandr)
  elif command -v zypper >/dev/null 2>&1; then
    PKG=zypper
    PACKAGES=(python3 python3-pip python3-devel gcc gcc-c++ make libX11-devel libXtst-devel libevdev-devel xorg-x11-server-extra openbox xterm xrandr)
  else
    fail "unsupported Linux distribution: install Python 3.10+, pip, X11/XTest support, Xvfb, Openbox and xterm manually."
  fi
  need_sudo
  case "$PKG" in
    apt-get) $SUDO apt-get update; $SUDO apt-get install -y "${PACKAGES[@]}" ;;
    dnf|yum) $SUDO "$PKG" install -y "${PACKAGES[@]}" ;;
    pacman) $SUDO pacman -S --needed --noconfirm "${PACKAGES[@]}" ;;
    zypper) $SUDO zypper --non-interactive install "${PACKAGES[@]}" ;;
  esac
elif [ "$OS" = Darwin ]; then
  command -v python3 >/dev/null 2>&1 || fail "install Python 3.10+ from python.org or Homebrew, then rerun this installer."
  PYTHON="$(command -v python3)"
  "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' || fail "Python 3.10 or newer is required."
  if ! command -v curl >/dev/null 2>&1 || ! command -v tar >/dev/null 2>&1; then fail "curl and tar are required."; fi
else
  fail "unsupported operating system: $OS"
fi

PYTHON="${PYTHON:-$(command -v python3 || command -v python)}"
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' || fail "Python 3.10 or newer is required."
mkdir -p "$APP_DIR"
if [ ! -x "$VENV/bin/python" ]; then "$PYTHON" -m venv "$VENV"; fi
curl -fsSL "$REPO/archive/refs/heads/main.tar.gz" -o "$TMP_DIR/source.tar.gz" || fail "could not download the public tisplay source."
mkdir "$TMP_DIR/source"
tar -xzf "$TMP_DIR/source.tar.gz" --strip-components=1 -C "$TMP_DIR/source"
"$VENV/bin/python" -m pip install --upgrade "$TMP_DIR/source" || fail "Python package installation failed."

BIN_DIR="${XDG_BIN_HOME:-$HOME/.local/bin}"
mkdir -p "$BIN_DIR"
cat > "$BIN_DIR/tisplay" <<EOF
#!/usr/bin/env sh
exec "$VENV/bin/tisplay" "\$@"
EOF
chmod 755 "$BIN_DIR/tisplay"
printf '\ntisplay installed at %s\n' "$BIN_DIR/tisplay"
case ":$PATH:" in *":$BIN_DIR:"*) ;;
  *) printf 'Add this to your shell profile if tisplay is not found:\n  export PATH="%s:\$PATH"\n' "$BIN_DIR" ;;
esac
if [ "$OS" = Darwin ]; then
  printf 'macOS needs a logged-in desktop. Allow Screen Recording and Accessibility for your terminal or Python in System Settings if prompted.\n'
else
  printf 'Run `tisplay` for your primary X11 display, or `tisplay --virtual -- xterm` for headless Linux.\n'
fi
