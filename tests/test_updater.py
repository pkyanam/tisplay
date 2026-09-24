"""The updater's metadata, atomic switching, and rollback behavior."""

import json
import io
import os
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from tisplay import updater


SHA_A = "a" * 40
SHA_B = "b" * 40


def _release(root: Path, name: str, commit: str | None = None) -> Path:
    path = root / "releases" / name
    executable = path / "venv/bin/tisplay"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\necho 'tisplay 0.1.0'\n")
    executable.chmod(0o755)
    if commit:
        (path / "release.json").write_text(json.dumps({"commit": commit, "profile": "virtual"}))
    return path


def _link(path: Path, target: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(os.path.relpath(target, path.parent))


def _opener_for(sha):
    response = Mock()
    response.read.return_value = json.dumps({"sha": sha}).encode()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    return Mock(return_value=response)


def test_check_reads_metadata_without_creating_or_changing_files(tmp_path):
    old = _release(tmp_path, "old-virtual", SHA_A)
    _link(tmp_path / "current", old)
    before = set(tmp_path.rglob("*"))

    result = updater.check_update(tmp_path, _opener_for(SHA_B))

    assert result == {"current": SHA_A, "latest": SHA_B, "update_available": True,
                      "revision_known": True, "message": None}
    assert set(tmp_path.rglob("*")) == before


def test_update_recognizes_installer_manifest_even_with_legacy_release_name(tmp_path):
    current = _release(tmp_path, "0123456789abcdef-virtual", SHA_B)
    _link(tmp_path / "current", current)
    result = updater.update_local(app_dir=tmp_path, opener=_opener_for(SHA_B))
    assert result["already_current"] is True
    assert result["profile"] == "virtual"
    assert not (tmp_path / ".install-lock").exists()


def test_legacy_check_reports_revision_unknown_without_mutating(tmp_path):
    old = _release(tmp_path, "legacy-virtual")
    _link(tmp_path / "current", old)
    result = updater.check_update(tmp_path, _opener_for(SHA_B))
    assert result["revision_known"] is False
    assert result["message"] == "installed revision is unknown; updating will migrate it"
    assert not (tmp_path / ".install-lock").exists()


def test_failed_revision_lookup_leaves_active_release_unchanged(tmp_path):
    old = _release(tmp_path, "legacy-virtual")
    _link(tmp_path / "current", old)

    def offline(*_args, **_kwargs):
        raise OSError("offline")

    with pytest.raises(OSError, match="offline"):
        updater.update_local(app_dir=tmp_path, opener=offline)
    assert (tmp_path / "current").resolve() == old.resolve()
    assert not (tmp_path / ".install-lock").exists()


def test_rollback_requires_lock_and_keeps_other_installers_lock(tmp_path):
    current = _release(tmp_path, "new-virtual", SHA_B)
    previous = _release(tmp_path, "old-virtual", SHA_A)
    _link(tmp_path / "current", current)
    _link(tmp_path / "previous", previous)

    lock = tmp_path / ".install-lock"
    lock.mkdir()
    with pytest.raises(RuntimeError, match="another installation"):
        updater.update_local(rollback=True, app_dir=tmp_path)
    assert lock.is_dir()
    lock.rmdir()

    result = updater.update_local(rollback=True, app_dir=tmp_path)
    assert result["rolled_back"] is True
    assert (tmp_path / "current").resolve() == previous
    assert (tmp_path / "previous").resolve() == current


def test_update_lock_failure_preserves_current_and_existing_lock(tmp_path):
    current = _release(tmp_path, "legacy-virtual")
    _link(tmp_path / "current", current)
    lock = tmp_path / ".install-lock"
    lock.mkdir()
    with pytest.raises(RuntimeError, match="another installation"):
        updater.update_local(app_dir=tmp_path, opener=_opener_for(SHA_B))
    assert lock.is_dir()
    assert (tmp_path / "current").resolve() == current.resolve()


def test_cua_installer_refuses_unverified_download_before_install(tmp_path):
    response = io.BytesIO(b"not the official archive")
    with pytest.raises(RuntimeError, match="checksum"):
        updater.install_cua(tmp_path, system="linux", machine="x86_64", opener=Mock(return_value=response))
    assert not (tmp_path / "cua/current").exists()
    assert not (tmp_path / "cua/.install-lock").exists()


def test_cua_platform_selection_rejects_unpublished_asset(tmp_path):
    with pytest.raises(RuntimeError, match="verified assets"):
        updater.install_cua(tmp_path, system="darwin", machine="arm64")
