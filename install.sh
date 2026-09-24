#!/usr/bin/env bash
set -euo pipefail

REPO="https://github.com/pkyanam/tisplay"
APP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/tisplay"
BIN_DIR="${XDG_BIN_HOME:-$HOME/.local/bin}"
PROFILE=virtual
PLAN_ONLY=no
TMP_DIR="$(mktemp -d)"
STAGING=
RELEASE_BUILD=
INSTALL_LOCK=
LAUNCHER_TMP=
cleanup() {
  rm -rf "$TMP_DIR"
  [ -z "$STAGING" ] || rm -rf "$STAGING"
  [ -z "$RELEASE_BUILD" ] || rm -rf "$RELEASE_BUILD"
  [ -z "$INSTALL_LOCK" ] || rmdir "$INSTALL_LOCK" 2>/dev/null || true
  [ -z "$LAUNCHER_TMP" ] || rm -f "$LAUNCHER_TMP"
}
trap cleanup EXIT

fail() { printf 'tisplay installer: %s\n' "$*" >&2; exit 1; }
usage() {
  cat <<'EOF'
Usage: install.sh [--profile virtual|native|minimal]

virtual (default) installs the Xvfb/XFCE fallback dependencies.
native installs a managed labwc/Wayland session provider (labwc, D-Bus, grim, wlr-randr, wayvnc).
On Raspberry Pi OS, it adds the desktop session metapackage when the configured apt sources provide it.
minimal installs Python build/runtime prerequisites only.
--plan prints the platform and missing-package plan without making changes.
EOF
}
while (($#)); do
  case "$1" in
    --profile) (($# >= 2)) || fail "--profile requires virtual, native, or minimal"; PROFILE=$2; shift 2 ;;
    --plan) PLAN_ONLY=yes; shift ;;
    --help|-h) usage; exit 0 ;;
    *) fail "unknown option: $1" ;;
  esac
done
case "$PROFILE" in virtual|native|minimal) ;; *) fail "unknown profile '$PROFILE' (choose virtual, native, or minimal)" ;; esac

# Read os-release as data rather than sourcing it as shell code.
OS_ID=unknown
OS_ID_LIKE=
OS_RELEASE_FILE=${TISPLAY_OS_RELEASE_FILE:-/etc/os-release}
DEVICE_TREE=${TISPLAY_DEVICE_TREE:-/proc/device-tree}
if [ -r "$OS_RELEASE_FILE" ]; then
  OS_ID="$(sed -n 's/^ID=//p' "$OS_RELEASE_FILE" | head -n 1 | tr -d '"' | tr '[:upper:]' '[:lower:]')"
  OS_ID_LIKE="$(sed -n 's/^ID_LIKE=//p' "$OS_RELEASE_FILE" | head -n 1 | tr -d '"' | tr '[:upper:]' '[:lower:]')"
fi
PI_MODEL=unknown
if [ -r "$DEVICE_TREE/model" ]; then PI_MODEL="$(tr -d '\000' <"$DEVICE_TREE/model")"; fi
PI_COMPATIBLE=
if [ -r "$DEVICE_TREE/compatible" ]; then PI_COMPATIBLE="$(tr '\000' ' ' <"$DEVICE_TREE/compatible")"; fi
PI_DATA="$(printf '%s %s' "$PI_MODEL" "$PI_COMPATIBLE" | tr '[:upper:]' '[:lower:]')"
case "$PI_DATA" in *raspberry*|*bcm27*|*bcm283*) IS_PI=yes ;; *) IS_PI=no ;; esac

OS=${TISPLAY_TEST_OS:-$(uname -s)}
PYTHON=
if [ "$OS" = Linux ]; then
  if command -v apt-get >/dev/null 2>&1 && [[ " $OS_ID $OS_ID_LIKE " =~ (debian|ubuntu|raspbian) ]]; then PKG=apt-get
  elif command -v dnf >/dev/null 2>&1; then PKG=dnf
  elif command -v yum >/dev/null 2>&1; then PKG=yum
  elif command -v pacman >/dev/null 2>&1; then PKG=pacman
  elif command -v zypper >/dev/null 2>&1; then PKG=zypper
  else fail "No supported package manager was found for Linux ($OS_ID). Install Python 3.10+, pip, and the dependencies for the selected profile manually."; fi
  case "$PKG:$PROFILE" in
    apt-get:virtual) PACKAGES=(python3 python3-venv python3-pip xvfb xauth x11-utils dbus-daemon xfce4-session xfce4-panel xfce4-settings xfwm4 xfdesktop4 thunar xfce4-terminal xdotool) ;;
    apt-get:native) PACKAGES=(python3 python3-venv python3-pip grim wlr-randr wayvnc labwc dbus-daemon) ;;
    apt-get:minimal) PACKAGES=(python3 python3-venv python3-pip) ;;
    dnf:virtual|yum:virtual) PACKAGES=(python3 python3-pip xorg-x11-server-Xvfb xorg-x11-xauth xorg-x11-utils xrandr dbus-daemon xfce4-session xfce4-panel xfce4-settings xfwm4 xfdesktop thunar xfce4-terminal xdotool) ;;
    dnf:native|yum:native) PACKAGES=(python3 python3-pip grim wlr-randr wayvnc labwc dbus-daemon) ;;
    dnf:minimal|yum:minimal) PACKAGES=(python3 python3-pip) ;;
    pacman:virtual) PACKAGES=(python python-pip xorg-server-xvfb xorg-xauth xorg-xprop xorg-xrandr dbus xfce4-session xfce4-panel xfce4-settings xfwm4 xfdesktop thunar xfce4-terminal xdotool) ;;
    pacman:native) PACKAGES=(python python-pip grim wlr-randr wayvnc labwc dbus) ;;
    pacman:minimal) PACKAGES=(python python-pip) ;;
    zypper:virtual) PACKAGES=(python3 python3-pip xorg-x11-server-extra xauth x11-tools xrandr dbus-1 xfce4-session xfce4-panel xfce4-settings xfwm4 xfdesktop thunar xfce4-terminal xdotool) ;;
    zypper:native) PACKAGES=(python3 python3-pip grim wlr-randr wayvnc labwc dbus-1-daemon) ;;
    zypper:minimal) PACKAGES=(python3 python3-pip) ;;
  esac
  if [ "$PROFILE" = native ] && [ "$IS_PI" = yes ] && [ "$OS_ID" = debian ] \
      && command -v apt-cache >/dev/null 2>&1 && apt-cache show rpd-wayland-core >/dev/null 2>&1; then
    PACKAGES+=(rpd-wayland-core)
  fi
  need_sudo() {
    if [ "$(id -u)" -eq 0 ]; then SUDO=(); return; fi
    command -v sudo >/dev/null 2>&1 || fail "sudo is required to install missing system packages."
    if sudo -n -v >/dev/null 2>&1; then SUDO=(sudo -n); return; fi
    if [ ! -t 0 ] && [ ! -t 1 ] && [ ! -t 2 ]; then fail "system dependencies are missing; rerun from an interactive terminal so sudo can ask for your password."; fi
    sudo -v || fail "could not obtain sudo access."
    SUDO=(sudo)
  }
  installed() {
    case "$PKG" in
      apt-get) dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q 'install ok installed' ;;
      dnf|yum|zypper) rpm -q "$1" >/dev/null 2>&1 ;;
      pacman) pacman -Q "$1" >/dev/null 2>&1 ;;
    esac
  }
  MISSING=()
  for package in "${PACKAGES[@]}"; do installed "$package" || MISSING+=("$package"); done
  if [ "$PLAN_ONLY" = yes ]; then
    printf 'os=%s distro=%s pi=%s profile=%s package_manager=%s missing=' "$OS" "$OS_ID" "$IS_PI" "$PROFILE" "$PKG"
    printf '%s ' "${MISSING[@]}"
    printf '\n'
    exit 0
  fi
  if ((${#MISSING[@]})); then
    need_sudo
    case "$PKG" in
      apt-get) "${SUDO[@]}" apt-get update || fail "package index update failed; no application files were changed."; "${SUDO[@]}" apt-get install -y "${MISSING[@]}" || fail "system dependency installation failed." ;;
      dnf|yum) "${SUDO[@]}" "$PKG" install -y "${MISSING[@]}" || fail "system dependency installation failed." ;;
      pacman) "${SUDO[@]}" pacman -S --needed --noconfirm "${MISSING[@]}" || fail "system dependency installation failed." ;;
      zypper) "${SUDO[@]}" zypper --non-interactive install "${MISSING[@]}" || fail "system dependency installation failed." ;;
    esac
  fi
  PYTHON=$(command -v python3 || command -v python || true)
  [ -n "$PYTHON" ] || fail "Python 3 could not be installed or found."
elif [ "$OS" = Darwin ]; then
  command -v python3 >/dev/null 2>&1 || fail "install Python 3.10+ from python.org or Homebrew, then rerun this installer."
  PYTHON=$(command -v python3)
  command -v curl >/dev/null 2>&1 && command -v tar >/dev/null 2>&1 || fail "curl and tar are required."
  if [ "$PLAN_ONLY" = yes ]; then printf 'os=Darwin profile=%s package_manager=none missing=\n' "$PROFILE"; exit 0; fi
else
  fail "unsupported operating system: $OS"
fi

"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' || fail "Python 3.10 or newer is required."
command -v curl >/dev/null 2>&1 && command -v tar >/dev/null 2>&1 || fail "curl and tar are required."
mkdir -p "$APP_DIR/releases" "$BIN_DIR"
LOCK_CANDIDATE="$APP_DIR/.install-lock"
mkdir "$LOCK_CANDIDATE" 2>/dev/null || fail "another tisplay installation is in progress; retry after it finishes."
INSTALL_LOCK="$LOCK_CANDIDATE"
curl -fsSL "$REPO/archive/refs/heads/main.tar.gz" -o "$TMP_DIR/source.tar.gz" || fail "could not download the public tisplay source; the installed version was left untouched."
if command -v sha256sum >/dev/null 2>&1; then SOURCE_HASH=$(sha256sum "$TMP_DIR/source.tar.gz" | cut -c1-16)
else SOURCE_HASH=$(shasum -a 256 "$TMP_DIR/source.tar.gz" | cut -c1-16); fi
RELEASE="$APP_DIR/releases/${SOURCE_HASH}-${PROFILE}"
RELEASE_OK=no
if [ -x "$RELEASE/venv/bin/tisplay" ] && "$RELEASE/venv/bin/tisplay" --version >/dev/null 2>&1; then RELEASE_OK=yes; fi
if [ "$RELEASE_OK" != yes ]; then
  CURRENT_TARGET=$(readlink "$APP_DIR/current" 2>/dev/null || true)
  if [ -e "$RELEASE" ] && [ "$CURRENT_TARGET" = "$RELEASE" ]; then
    fail "the active release is incomplete or damaged; it was left in place."
  fi
  rm -rf "$RELEASE"
  RELEASE_BUILD="$RELEASE"
  mkdir -p "$TMP_DIR/source" "$RELEASE_BUILD"
  tar -xzf "$TMP_DIR/source.tar.gz" --strip-components=1 -C "$TMP_DIR/source" || fail "downloaded source archive was invalid; the installed version was left untouched."
  "$PYTHON" -m venv "$RELEASE_BUILD/venv" || fail "could not create the Python environment."
  "$RELEASE_BUILD/venv/bin/python" -m pip install "$TMP_DIR/source" || fail "Python package installation failed; the installed version was left untouched."
  "$RELEASE_BUILD/venv/bin/tisplay" --version >/dev/null 2>&1 || fail "the installed release failed its version check; the installed version was left untouched."
  RELEASE_BUILD=
fi
ln -s "$RELEASE" "$APP_DIR/.current.$$"
"$PYTHON" -c 'import os,sys; os.replace(sys.argv[1], sys.argv[2])' "$APP_DIR/.current.$$" "$APP_DIR/current" || fail "could not switch the active release."
LAUNCHER_TMP="$BIN_DIR/.tisplay.$$"
cat > "$LAUNCHER_TMP" <<EOF
#!/usr/bin/env sh
exec "$APP_DIR/current/venv/bin/tisplay" "\$@"
EOF
chmod 755 "$LAUNCHER_TMP"
"$PYTHON" -c 'import os,sys; os.replace(sys.argv[1], sys.argv[2])' "$LAUNCHER_TMP" "$BIN_DIR/tisplay" || fail "could not update the launcher."
LAUNCHER_TMP=
rmdir "$INSTALL_LOCK"
INSTALL_LOCK=
printf '\ntisplay installed at %s (profile: %s)\n' "$BIN_DIR/tisplay" "$PROFILE"
case ":$PATH:" in *":$BIN_DIR:"*) ;; *) printf 'Add this to your shell profile if tisplay is not found:\n  export PATH="%s:\$PATH"\n' "$BIN_DIR" ;; esac
if [ "$OS" = Darwin ]; then
  printf 'macOS needs a logged-in desktop. Allow Screen Recording and Accessibility for your terminal or Python in System Settings if prompted.\n'
elif [ "$PROFILE" = native ]; then
  printf 'Native mode requires a running supported Wayland compositor; it does not start or reconfigure one.\n'
  [ "$IS_PI" = yes ] && printf 'Raspberry Pi hardware detected from device-tree metadata.\n'
else
  printf 'Use --virtual for the private Xvfb + XFCE desktop, or choose a native mode when a supported compositor is already running.\n'
fi
