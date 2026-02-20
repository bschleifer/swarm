"""Install/uninstall a systemd user service for ``swarm serve``."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

_SERVICE_NAME = "swarm.service"
_SERVICE_DIR = Path.home() / ".config" / "systemd" / "user"
_SERVICE_PATH = _SERVICE_DIR / _SERVICE_NAME

_UNIT_TEMPLATE = """\
[Unit]
Description=Swarm Web Dashboard
After=network.target

[Service]
Type=simple
ExecStart={exec_start}
Restart=always
RestartSec=5
Environment=PATH={path_env}
WorkingDirectory={work_dir}

[Install]
WantedBy=default.target
"""


def _systemctl(*args: str) -> subprocess.CompletedProcess[str]:
    """Run ``systemctl --user <args>``."""
    return subprocess.run(
        ["systemctl", "--user", *args],
        capture_output=True,
        text=True,
    )


def _check_systemd() -> str | None:
    """Return an error message if systemd user services are unavailable, else None."""
    if not shutil.which("systemctl"):
        return (
            "systemctl not found. systemd is required for this feature.\n"
            "On WSL2, add '[boot] systemd=true' to /etc/wsl.conf and restart."
        )
    result = _systemctl("--version")
    if result.returncode != 0:
        return (
            "systemd user services are not available.\n"
            "On WSL2, add '[boot] systemd=true' to /etc/wsl.conf and restart."
        )
    return None


def _resolve_config_path(config_path: str | None) -> Path | None:
    """Find the config file to use."""
    if config_path:
        return Path(config_path).resolve()
    # Check common locations
    cwd_config = Path.cwd() / "swarm.yaml"
    if cwd_config.exists():
        return cwd_config.resolve()
    home_config = Path.home() / ".config" / "swarm" / "config.yaml"
    if home_config.exists():
        return home_config.resolve()
    return None


def generate_unit(config_path: str | None = None) -> str:
    """Generate the systemd unit file content."""
    swarm_bin = shutil.which("swarm")
    if not swarm_bin:
        msg = "swarm binary not found in PATH. Install with: uv tool install swarm-ai"
        raise FileNotFoundError(msg)

    resolved_config = _resolve_config_path(config_path)

    exec_start = swarm_bin + " serve"
    if resolved_config:
        exec_start += f" -c {resolved_config}"

    # Build PATH: include directory of swarm binary + standard paths
    bin_dir = str(Path(swarm_bin).parent)
    local_bin = str(Path.home() / ".local" / "bin")
    path_parts = []
    for p in [bin_dir, local_bin, "/usr/local/bin", "/usr/bin", "/bin"]:
        if p not in path_parts:
            path_parts.append(p)
    path_env = ":".join(path_parts)

    # WorkingDirectory: use config dir or home
    work_dir = str(resolved_config.parent) if resolved_config else str(Path.home())

    return _UNIT_TEMPLATE.format(
        exec_start=exec_start,
        path_env=path_env,
        work_dir=work_dir,
    )


def install_service(config_path: str | None = None) -> Path:
    """Install and start the systemd user service.

    Returns the path to the installed service file.
    """
    error = _check_systemd()
    if error:
        raise RuntimeError(error)

    unit_content = generate_unit(config_path)

    _SERVICE_DIR.mkdir(parents=True, exist_ok=True)
    _SERVICE_PATH.write_text(unit_content)

    _systemctl("daemon-reload")
    _systemctl("enable", _SERVICE_NAME)
    _systemctl("start", _SERVICE_NAME)

    return _SERVICE_PATH


def uninstall_service() -> bool:
    """Stop, disable, and remove the systemd user service.

    Returns True if the service file existed and was removed.
    """
    _systemctl("stop", _SERVICE_NAME)
    _systemctl("disable", _SERVICE_NAME)

    if _SERVICE_PATH.exists():
        _SERVICE_PATH.unlink()
        _systemctl("daemon-reload")
        return True
    return False


def service_status() -> str:
    """Return the systemd status output for the swarm service."""
    result = _systemctl("status", _SERVICE_NAME)
    return result.stdout or result.stderr or "unknown"


# --- WSL auto-start (Windows Startup folder) ---

_VBS_CONTENT = 'CreateObject("WScript.Shell").Run "wsl -d {distro} --exec /bin/true", 0, False\n'
_VBS_NAME = "start-wsl.vbs"


def _wsl_startup_dir() -> Path | None:
    """Return the Windows Startup folder path via /mnt/c, or None if unavailable."""
    try:
        # Read Windows username from environment
        import os

        win_user = os.environ.get("USER") or os.environ.get("LOGNAME", "")
        candidate = Path(
            f"/mnt/c/Users/{win_user}/AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Startup"
        )
        if candidate.is_dir():
            return candidate
    except OSError:
        pass
    return None


def _wsl_distro_name() -> str:
    """Return the current WSL distro name."""
    import os

    return os.environ.get("WSL_DISTRO_NAME", "Ubuntu")


def is_wsl() -> bool:
    """Return True if running inside WSL."""
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except OSError:
        return False


def install_wsl_startup() -> Path | None:
    """Install a VBS script in the Windows Startup folder to auto-launch WSL.

    Returns the path to the VBS file, or None if not applicable.
    """
    if not is_wsl():
        return None
    startup = _wsl_startup_dir()
    if not startup:
        return None
    vbs_path = startup / _VBS_NAME
    distro = _wsl_distro_name()
    vbs_path.write_text(_VBS_CONTENT.format(distro=distro))
    return vbs_path


def uninstall_wsl_startup() -> bool:
    """Remove the WSL auto-start VBS script. Returns True if it existed."""
    startup = _wsl_startup_dir()
    if not startup:
        return False
    vbs_path = startup / _VBS_NAME
    if vbs_path.exists():
        vbs_path.unlink()
        return True
    return False


def wsl_startup_installed() -> bool:
    """Check if the WSL auto-start VBS script is already installed."""
    startup = _wsl_startup_dir()
    if not startup:
        return False
    return (startup / _VBS_NAME).exists()
