"""Install/uninstall a systemd or launchd service for ``swarm serve``."""

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


# --- macOS launchd (Launch Agent plist) ---

_PLIST_LABEL = "com.swarm.dashboard"
_PLIST_DIR = Path.home() / "Library" / "LaunchAgents"
_PLIST_PATH = _PLIST_DIR / f"{_PLIST_LABEL}.plist"
_SWARM_LOG_DIR = Path.home() / ".swarm"

_PLIST_TEMPLATE = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" \
"http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
{program_arguments}
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{stdout_log}</string>
    <key>StandardErrorPath</key>
    <string>{stderr_log}</string>
</dict>
</plist>
"""


def is_macos() -> bool:
    """Return True if running on macOS."""
    import sys

    return sys.platform == "darwin"


def _check_launchd() -> str | None:
    """Return an error message if launchd is unavailable, else None."""
    if not is_macos():
        return "launchd is only available on macOS."
    return None


def _launchctl(*args: str) -> subprocess.CompletedProcess[str]:
    """Run ``launchctl <args>``."""
    return subprocess.run(
        ["launchctl", *args],
        capture_output=True,
        text=True,
    )


def generate_plist(config_path: str | None = None) -> str:
    """Generate the launchd plist content."""
    swarm_bin = shutil.which("swarm")
    if not swarm_bin:
        msg = "swarm binary not found in PATH. Install with: uv tool install swarm-ai"
        raise FileNotFoundError(msg)

    resolved_config = _resolve_config_path(config_path)

    args = [swarm_bin, "serve"]
    if resolved_config:
        args.extend(["-c", str(resolved_config)])

    indent = " " * 8
    program_arguments = "\n".join(f"{indent}<string>{arg}</string>" for arg in args)

    _SWARM_LOG_DIR.mkdir(parents=True, exist_ok=True)

    return _PLIST_TEMPLATE.format(
        label=_PLIST_LABEL,
        program_arguments=program_arguments,
        stdout_log=str(_SWARM_LOG_DIR / "launchd-stdout.log"),
        stderr_log=str(_SWARM_LOG_DIR / "launchd-stderr.log"),
    )


def install_launchd(config_path: str | None = None) -> Path:
    """Install and load the launchd Launch Agent.

    Returns the path to the installed plist file.
    """
    error = _check_launchd()
    if error:
        raise RuntimeError(error)

    plist_content = generate_plist(config_path)

    _PLIST_DIR.mkdir(parents=True, exist_ok=True)
    _PLIST_PATH.write_text(plist_content)

    _launchctl("load", str(_PLIST_PATH))

    return _PLIST_PATH


def uninstall_launchd() -> bool:
    """Unload and remove the launchd Launch Agent.

    Returns True if the plist file existed and was removed.
    """
    if _PLIST_PATH.exists():
        _launchctl("unload", str(_PLIST_PATH))
        _PLIST_PATH.unlink()
        return True
    return False


def launchd_status() -> str:
    """Return the launchd status for the swarm service."""
    result = _launchctl("list", _PLIST_LABEL)
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


_WSL_CONF = Path("/etc/wsl.conf")


def _wsl_systemd_enabled() -> bool:
    """Return True if systemd is already enabled in /etc/wsl.conf."""
    try:
        text = _WSL_CONF.read_text()
    except OSError:
        return False
    import configparser

    cp = configparser.ConfigParser()
    cp.read_string(text)
    return cp.getboolean("boot", "systemd", fallback=False)


def enable_wsl_systemd() -> bool:
    """Enable systemd in /etc/wsl.conf (requires sudo).

    Reads the existing config (or starts empty), ensures ``[boot] systemd=true``
    is present, and writes back via ``sudo tee``.

    Returns True on success.
    """
    if _wsl_systemd_enabled():
        return True

    try:
        text = _WSL_CONF.read_text()
    except OSError:
        text = ""

    import configparser

    cp = configparser.ConfigParser()
    cp.read_string(text)
    if not cp.has_section("boot"):
        cp.add_section("boot")
    cp.set("boot", "systemd", "true")

    import io

    buf = io.StringIO()
    cp.write(buf)
    new_content = buf.getvalue()

    result = subprocess.run(
        ["sudo", "tee", str(_WSL_CONF)],
        input=new_content,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        msg = f"Failed to write {_WSL_CONF}: {result.stderr.strip()}"
        raise RuntimeError(msg)
    return True


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
