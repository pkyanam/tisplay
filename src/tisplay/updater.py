"""Atomic, low-overhead self-update support for installed tisplay releases."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path
from typing import Callable

REPOSITORY = "pkyanam/tisplay"
API_URL = f"https://api.github.com/repos/{REPOSITORY}/commits/main"
CUA_VERSION = "0.28.2"
CUA_RELEASE = "https://github.com/trycua/cua/releases/download/cua-driver-rs-v0.28.2"
CUA_ASSETS = {
    "x86_64": ("cua-driver-rs-0.28.2-linux-x86_64-binary.tar.gz", "a1d99fd04bb4927ef5ffdbe60eb91ed8b51a2bab60e10fc604a75bd59ce69c3e"),
    "aarch64": ("cua-driver-rs-0.28.2-linux-arm64-binary.tar.gz", "55e8a32839a4ac369a773df4dac87b345bd4567779221ade4a5e39223a45a2e8"),
}


def data_dir() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "tisplay"


def _request(url: str, opener: Callable = urllib.request.urlopen) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "tisplay-updater", "Accept": "application/vnd.github+json"})
    with opener(request, timeout=20) as response:
        return response.read()


def _download(url: str, destination: Path, opener: Callable = urllib.request.urlopen) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "tisplay-updater", "Accept": "application/octet-stream"})
    digest = hashlib.sha256()
    with opener(request, timeout=60) as response, destination.open("wb") as output:
        while chunk := response.read(256 * 1024):
            digest.update(chunk)
            output.write(chunk)
    return digest.hexdigest()


def latest_commit(opener: Callable = urllib.request.urlopen) -> str:
    payload = json.loads(_request(API_URL, opener))
    sha = payload.get("sha", "")
    if not isinstance(sha, str) or len(sha) != 40 or any(c not in "0123456789abcdef" for c in sha):
        raise RuntimeError("GitHub returned an invalid source revision")
    return sha


def _link(path: Path, target: str) -> None:
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.unlink(missing_ok=True)
    temp.symlink_to(target)
    os.replace(temp, path)


def _current(app_dir: Path) -> Path | None:
    path = app_dir / "current"
    if not path.is_symlink():
        return None
    try:
        target = (app_dir / os.readlink(path)).resolve()
        target.relative_to((app_dir / "releases").resolve())
        return target
    except (OSError, ValueError):
        return None


def _manifest(release: Path) -> dict:
    try:
        return json.loads((release / "release.json").read_text())
    except (OSError, ValueError):
        return {}


def _safe_extract(bundle: tarfile.TarFile, destination: Path) -> None:
    """Extract archives without allowing paths or links to escape the staging dir."""
    base = destination.resolve()
    for member in bundle.getmembers():
        if not (member.isfile() or member.isdir()):
            raise RuntimeError("archive contains an unsupported special file")
        target = (base / member.name).resolve()
        target.relative_to(base)
    bundle.extractall(destination)


def check_update(app_dir: Path | None = None, opener: Callable = urllib.request.urlopen) -> dict:
    """Read remote revision metadata and local state without writing anything."""
    root = app_dir or data_dir()
    commit = latest_commit(opener)
    current = _current(root)
    installed = _manifest(current) if current else {}
    installed_commit = installed.get("commit")
    return {"current": installed_commit, "latest": commit,
            "update_available": installed_commit != commit,
            "revision_known": bool(installed_commit),
            "message": "installed revision is unknown; updating will migrate it" if current and not installed_commit else None}


def _profile(release: Path | None) -> str:
    if release:
        name = release.name
        for profile in ("virtual", "native", "minimal"):
            if name.endswith("-" + profile):
                return profile
        manifest = _manifest(release)
        if manifest.get("profile") in {"virtual", "native", "minimal"}:
            return manifest["profile"]
    return "virtual"


def _valid(release: Path) -> bool:
    binary = release / "venv/bin/tisplay"
    if not binary.is_file() or not os.access(binary, os.X_OK):
        return False
    try:
        subprocess.run([str(binary), "--version"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def update_local(*, check: bool = False, rollback: bool = False, app_dir: Path | None = None,
                 opener: Callable = urllib.request.urlopen) -> dict:
    """Update (or roll back) this host's active symlink without restarting daemons."""
    root = app_dir or data_dir()
    current = _current(root)
    if check:
        return check_update(root, opener)
    if rollback:
        lock = root / ".install-lock"
        try:
            lock.mkdir()
        except FileExistsError as exc:
            raise RuntimeError("another installation or update is in progress") from exc
        try:
            previous = None
            previous_link = root / "previous"
            if previous_link.is_symlink():
                try:
                    previous = (root / os.readlink(previous_link)).resolve()
                    previous.relative_to((root / "releases").resolve())
                except (OSError, ValueError):
                    previous = None
            if previous is None or not _valid(previous):
                raise RuntimeError("no previous valid release is available to roll back to")
            current = _current(root)
            if current:
                _link(previous_link, str(current))
            _link(root / "current", str(previous))
        finally:
            lock.rmdir()
        return {"updated": False, "rolled_back": True, "release": str(previous)}

    commit = latest_commit(opener)
    if current and _manifest(current).get("commit") == commit and _valid(current):
        return {"updated": False, "already_current": True, "commit": commit, "profile": _profile(current)}

    releases = root / "releases"
    releases.mkdir(parents=True, exist_ok=True)
    lock = root / ".install-lock"
    try:
        lock.mkdir()
    except FileExistsError as exc:
        raise RuntimeError("another installation or update is in progress") from exc
    build_release: Path | None = None
    try:
        current = _current(root)
        if current and _manifest(current).get("commit") == commit and _valid(current):
            return {"updated": False, "already_current": True, "commit": commit, "profile": _profile(current)}
        profile = _profile(current)
        release = releases / f"{commit[:16]}-{profile}"
        existing_manifest = _manifest(release)
        if existing_manifest.get("commit") == commit and _valid(release):
            candidate = release
        else:
            releases.mkdir(parents=True, exist_ok=True)
            if release.exists():
                if current == release:
                    raise RuntimeError("the active release is damaged; it was left in place")
                shutil.rmtree(release)
            # Venv scripts embed absolute paths, so build at its permanent path.
            build_release = release
            build_release.mkdir()
            with tempfile.TemporaryDirectory(prefix="tisplay-update-") as temp:
                temp_path = Path(temp)
                archive = temp_path / "source.tar.gz"
                digest = _download(f"https://github.com/{REPOSITORY}/archive/{commit}.tar.gz", archive, opener)
                source = temp_path / "source"
                source.mkdir()
                with tarfile.open(archive, "r:gz") as bundle:
                    _safe_extract(bundle, source)
                source = next(source.iterdir())
                python = Path(sys.executable)
                venv = build_release / "venv"
                subprocess.run([str(python), "-m", "venv", str(venv)], check=True, capture_output=True, text=True)
                if current and (current / "venv/bin/python").is_file():
                    probe = "import json,sys,sysconfig; print(json.dumps([sys.version_info[:2],sysconfig.get_config_var('SOABI'),sysconfig.get_paths()['purelib']]))"
                    old_python = subprocess.run([str(current / "venv/bin/python"), "-c", probe], check=True, capture_output=True, text=True)
                    new_python = subprocess.run([str(venv / "bin/python"), "-c", probe], check=True, capture_output=True, text=True)
                    old_version, old_soabi, old_site = json.loads(old_python.stdout)
                    new_version, new_soabi, new_site = json.loads(new_python.stdout)
                    if old_version == new_version and old_soabi == new_soabi:
                        shutil.copytree(old_site, new_site, dirs_exist_ok=True, symlinks=True)
                    cmd = [str(venv / "bin/python"), "-m", "pip", "install"]
                    cmd.append(str(source))
                else:
                    cmd = [str(venv / "bin/python"), "-m", "pip", "install", str(source)]
                subprocess.run(cmd, check=True, capture_output=True, text=True)
                candidate = build_release
                version = subprocess.run([str(candidate / "venv/bin/tisplay"), "--version"], check=True, capture_output=True, text=True, timeout=10).stdout.strip()
                (candidate / "release.json").write_text(json.dumps({"commit": commit, "archive_sha256": digest, "profile": profile, "version": version}, indent=2) + "\n")
                build_release = None
        if not _valid(candidate):
            raise RuntimeError("candidate release failed its version check; the active release was left untouched")
        current_after_lock = _current(root)
        if current_after_lock:
            _link(root / "previous", str(current_after_lock))
        _link(root / "current", str(candidate))
        return {"updated": candidate != current_after_lock, "commit": commit, "profile": profile, "release": str(candidate)}
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc)).strip()
        raise RuntimeError(f"update build failed; the active release was left untouched: {detail}") from exc
    finally:
        if build_release and build_release.exists():
            shutil.rmtree(build_release, ignore_errors=True)
        lock.rmdir()


def install_cua(app_dir: Path | None = None, *, system: str | None = None, machine: str | None = None,
                opener: Callable = urllib.request.urlopen) -> Path:
    """Install the pinned CUA driver after checking its published SHA-256."""
    system = system or platform.system().lower()
    machine = machine or platform.machine().lower()
    aliases = {"amd64": "x86_64", "x64": "x86_64", "arm64": "aarch64"}
    machine = aliases.get(machine, machine)
    if system != "linux" or machine not in CUA_ASSETS:
        raise RuntimeError(f"CUA driver {CUA_VERSION} has verified assets for Linux x86_64 and aarch64 only")
    filename, expected = CUA_ASSETS[machine]
    root = (app_dir or data_dir()) / "cua"
    target = root / CUA_VERSION
    binary = target / "bin/cua-driver"
    if binary.is_file() and os.access(binary, os.X_OK):
        manifest = _manifest(target)
        env = os.environ | {"CUA_DRIVER_RS_TELEMETRY_ENABLED": "false", "CUA_DRIVER_RS_UPDATE_CHECK": "false"}
        if manifest.get("sha256") == expected:
            result = subprocess.run([str(binary), "--version"], check=True, capture_output=True, text=True, timeout=15, env=env)
            if CUA_VERSION in result.stdout:
                return binary
        raise RuntimeError("the installed CUA driver does not match the pinned, verified release; remove it manually before reinstalling")
    root.mkdir(parents=True, exist_ok=True)
    lock = root / ".install-lock"
    try:
        lock.mkdir()
    except FileExistsError as exc:
        raise RuntimeError("another CUA driver installation is in progress") from exc
    stage: Path | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="tisplay-cua-") as temp:
            archive = Path(temp) / filename
            if _download(f"{CUA_RELEASE}/{filename}", archive, opener) != expected:
                raise RuntimeError("downloaded CUA driver checksum does not match the published release checksum")
            stage = Path(tempfile.mkdtemp(prefix=f".{CUA_VERSION}-", dir=root))
            with tarfile.open(archive, "r:gz") as bundle:
                _safe_extract(bundle, stage)
            candidates = list(stage.rglob("cua-driver"))
            if not candidates:
                raise RuntimeError("CUA archive did not contain the expected cua-driver executable")
            binary_stage = stage / "bin"
            binary_stage.mkdir(exist_ok=True)
            source_binary = candidates[0]
            if source_binary != binary_stage / "cua-driver":
                shutil.copy2(source_binary, binary_stage / "cua-driver")
            (binary_stage / "cua-driver").chmod(0o755)
            env = os.environ | {"CUA_DRIVER_RS_TELEMETRY_ENABLED": "false", "CUA_DRIVER_RS_UPDATE_CHECK": "false"}
            result = subprocess.run([str(binary_stage / "cua-driver"), "--version"], check=True, capture_output=True, text=True, timeout=15, env=env)
            if CUA_VERSION not in result.stdout:
                raise RuntimeError(f"downloaded CUA executable did not report version {CUA_VERSION}")
            (stage / "release.json").write_text(json.dumps({"version": CUA_VERSION, "asset": filename, "sha256": expected}, indent=2) + "\n")
            if target.exists():
                raise RuntimeError("a release directory already exists but is not a verified installation; refusing to replace it")
            os.replace(stage, target)
            stage = None
            _link(root / "current", CUA_VERSION)
        return target / "bin/cua-driver"
    finally:
        if stage and stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
        lock.rmdir()
