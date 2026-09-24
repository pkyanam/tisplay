"""Installer planning tests use mock package queries and device-tree metadata."""

import os
import shutil
import subprocess
import tarfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _mock_install_environment(tmp_path, source_archive, *, fail_pip=False):
    mockbin = tmp_path / ("mock-bin-fail" if fail_pip else "mock-bin")
    mockbin.mkdir()
    real_python = shutil.which("python3")
    py = mockbin / "python3"
    py.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = -m ] && [ \"$2\" = venv ]; then\n"
        "  mkdir -p \"$3/bin\"; ln -s \"$MOCK_PYTHON\" \"$3/bin/python\"; exit 0\n"
        "fi\n"
        "if [ \"$1\" = -m ] && [ \"$2\" = pip ]; then\n"
        "  echo install >> \"$MOCK_PIP_LOG\"\n"
        "  [ \"${MOCK_FAIL_PIP:-no}\" = no ] || exit 1\n"
        "  printf '#!/bin/sh\\necho installed\\n' > \"$(dirname \"$0\")/tisplay\"; chmod +x \"$(dirname \"$0\")/tisplay\"; exit 0\n"
        "fi\n"
        "if [ \"$1\" = --version ]; then echo 'tisplay 0.1.0'; exit 0; fi\n"
        "exec \"$MOCK_REAL_PYTHON\" \"$@\"\n"
    )
    py.chmod(0o755)
    dpkg = mockbin / "dpkg-query"
    dpkg.write_text("#!/bin/sh\ncase \"$3\" in rpd-wayland-core) echo 'deinstall ok config-files';; *) echo 'install ok installed';; esac\n")
    dpkg.chmod(0o755)
    apt = mockbin / "apt-get"
    apt.write_text("#!/bin/sh\necho \"$*\" >> \"$MOCK_APT_LOG\"\nexit 91\n")
    apt.chmod(0o755)
    apt_cache = mockbin / "apt-cache"
    apt_cache.write_text("#!/bin/sh\n[ \"${MOCK_RPD_AVAILABLE:-no}\" = yes ]\n")
    apt_cache.chmod(0o755)
    os_release = tmp_path / "os-release"
    os_release.write_text("ID=debian\n")
    pip_log = tmp_path / "pip.log"
    apt_log = tmp_path / "apt.log"
    env = os.environ | {
        "PATH": f"{mockbin}:{os.environ['PATH']}",
        "TISPLAY_TEST_OS": "Linux",
        "TISPLAY_OS_RELEASE_FILE": str(os_release),
        "TISPLAY_DEVICE_TREE": str(tmp_path / "empty-device-tree"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_BIN_HOME": str(tmp_path / "bin-home"),
        "MOCK_PYTHON": str(py),
        "MOCK_REAL_PYTHON": real_python,
        "MOCK_SOURCE_ARCHIVE": str(source_archive),
        "MOCK_PIP_LOG": str(pip_log),
        "MOCK_APT_LOG": str(apt_log),
        "MOCK_FAIL_PIP": "yes" if fail_pip else "no",
    }
    curl = mockbin / "curl"
    curl.write_text("#!/bin/sh\ncp \"$MOCK_SOURCE_ARCHIVE\" \"$4\"\n")
    curl.chmod(0o755)
    return env, pip_log, apt_log


def _source_archive(path, content):
    payload = path.parent / "payload"
    (payload / "tisplay").mkdir(parents=True, exist_ok=True)
    (payload / "tisplay" / "version").write_text(content)
    with tarfile.open(path, "w:gz") as archive:
        archive.add(payload / "tisplay", arcname="tisplay-main")


def test_installer_detects_distro_and_pi_and_only_plans_missing_packages(tmp_path):
    mockbin = tmp_path / "bin"
    mockbin.mkdir()
    apt = mockbin / "apt-get"
    apt.write_text("#!/bin/sh\necho called >> \"$MOCK_LOG\"\nexit 0\n")
    apt.chmod(0o755)
    dpkg = mockbin / "dpkg-query"
    dpkg.write_text(
        "#!/bin/sh\n"
        "case \"$3\" in\n"
        "  xdotool) echo 'deinstall ok config-files'; exit 0 ;;\n"
        "  *) echo 'install ok installed'; exit 0 ;;\n"
        "esac\n"
    )
    dpkg.chmod(0o755)
    release = tmp_path / "os-release"
    release.write_text('ID=debian\nID_LIKE="debian"\n')
    tree = tmp_path / "device-tree"
    tree.mkdir()
    (tree / "model").write_text("Raspberry Pi 4 Model B\0")
    (tree / "compatible").write_bytes(b"raspberrypi,4-model-b\0brcm,bcm2711\0")
    log = tmp_path / "apt.log"
    env = os.environ | {
        "PATH": f"{mockbin}:{os.environ['PATH']}",
        "TISPLAY_TEST_OS": "Linux",
        "TISPLAY_OS_RELEASE_FILE": str(release),
        "TISPLAY_DEVICE_TREE": str(tree),
        "MOCK_LOG": str(log),
    }
    result = subprocess.run(
        [str(ROOT / "install.sh"), "--profile", "virtual", "--plan"],
        check=True,
        text=True,
        capture_output=True,
        env=env,
    )
    assert "os=Linux distro=debian pi=yes profile=virtual package_manager=apt-get" in result.stdout, result.stdout
    assert result.stdout.rsplit("missing=", 1)[1].split() == ["xdotool"]
    assert not log.exists(), "planning must not run a package manager"


def test_native_plan_adds_pi_desktop_metapackage_only_when_available(tmp_path):
    archive = tmp_path / "source.tar.gz"
    _source_archive(archive, "irrelevant")
    env, _, _ = _mock_install_environment(tmp_path, archive)
    tree = Path(env["TISPLAY_DEVICE_TREE"])
    tree.mkdir()
    (tree / "model").write_text("Raspberry Pi 4 Model B\0")
    (tree / "compatible").write_bytes(b"brcm,bcm2711\0")
    env |= {"MOCK_RPD_AVAILABLE": "yes"}
    result = subprocess.run(
        [str(ROOT / "install.sh"), "--profile", "native", "--plan"],
        check=True,
        text=True,
        capture_output=True,
        env=env,
    )
    assert "pi=yes profile=native" in result.stdout
    assert result.stdout.rsplit("missing=", 1)[1].split() == ["rpd-wayland-core"]
    env["MOCK_RPD_AVAILABLE"] = "no"
    unavailable = subprocess.run(
        [str(ROOT / "install.sh"), "--profile", "native", "--plan"],
        check=True,
        text=True,
        capture_output=True,
        env=env,
    )
    assert unavailable.stdout.rsplit("missing=", 1)[1].split() == []


def test_installer_help_and_invalid_profile_are_side_effect_free():
    help_result = subprocess.run([str(ROOT / "install.sh"), "--help"], capture_output=True, text=True)
    assert help_result.returncode == 0
    assert "--profile virtual|native|minimal" in help_result.stdout
    invalid = subprocess.run(
        [str(ROOT / "install.sh"), "--profile", "guess"], capture_output=True, text=True
    )
    assert invalid.returncode != 0
    assert "unknown profile" in invalid.stderr
    plan = subprocess.run(
        [str(ROOT / "install.sh"), "--profile", "native", "--plan"],
        capture_output=True,
        text=True,
        env=os.environ | {"TISPLAY_TEST_OS": "Darwin"},
    )
    assert plan.returncode == 0, plan.stderr
    assert "os=Darwin profile=native package_manager=none" in plan.stdout


def test_installer_reuses_identical_release_and_failed_upgrade_keeps_current(tmp_path):
    archive = tmp_path / "source.tar.gz"
    _source_archive(archive, "first")
    env, pip_log, apt_log = _mock_install_environment(tmp_path, archive)
    first = subprocess.run([str(ROOT / "install.sh")], capture_output=True, text=True, env=env)
    assert first.returncode == 0, first.stderr
    current = Path(env["XDG_DATA_HOME"]) / "tisplay/current"
    first_target = current.resolve()
    assert (Path(env["XDG_BIN_HOME"]) / "tisplay").is_file()
    subprocess.run([str(Path(env["XDG_BIN_HOME"]) / "tisplay")], check=True, capture_output=True)
    second = subprocess.run([str(ROOT / "install.sh")], capture_output=True, text=True, env=env)
    assert second.returncode == 0, second.stderr
    assert current.resolve() == first_target
    assert pip_log.read_text().splitlines() == ["install"]
    assert not apt_log.exists(), "already installed packages must not trigger apt"

    _source_archive(archive, "second")
    failing_env, _, _ = _mock_install_environment(tmp_path, archive, fail_pip=True)
    failure = subprocess.run([str(ROOT / "install.sh")], capture_output=True, text=True, env=failing_env)
    assert failure.returncode != 0
    assert "Python package installation failed" in failure.stderr
    assert current.resolve() == first_target


def test_failed_lock_acquisition_does_not_remove_another_installs_lock(tmp_path):
    archive = tmp_path / "source.tar.gz"
    _source_archive(archive, "first")
    env, _, _ = _mock_install_environment(tmp_path, archive)
    app_dir = Path(env["XDG_DATA_HOME"]) / "tisplay"
    app_dir.mkdir(parents=True)
    lock = app_dir / ".install-lock"
    lock.mkdir()
    result = subprocess.run([str(ROOT / "install.sh")], capture_output=True, text=True, env=env)
    assert result.returncode != 0
    assert "another tisplay installation is in progress" in result.stderr
    assert lock.is_dir()
