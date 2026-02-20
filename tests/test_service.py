"""Tests for service.py — systemd user service install/uninstall."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from swarm.service import generate_unit, install_service, uninstall_service


class TestGenerateUnit:
    """Test unit file generation."""

    def test_generates_valid_unit_file(self, tmp_path: Path) -> None:
        config = tmp_path / "swarm.yaml"
        config.write_text("workers: []")

        with patch("swarm.service.shutil.which", return_value="/usr/local/bin/swarm"):
            unit = generate_unit(str(config))

        assert "[Unit]" in unit
        assert "[Service]" in unit
        assert "[Install]" in unit
        assert "ExecStart=/usr/local/bin/swarm serve" in unit
        assert f"-c {config.resolve()}" in unit
        assert "Restart=always" in unit
        assert "WantedBy=default.target" in unit

    def test_includes_swarm_bin_dir_in_path(self, tmp_path: Path) -> None:
        config = tmp_path / "swarm.yaml"
        config.write_text("workers: []")

        with patch("swarm.service.shutil.which", return_value="/home/user/.local/bin/swarm"):
            unit = generate_unit(str(config))

        assert "/home/user/.local/bin" in unit

    def test_no_config_path_omits_flag(self) -> None:
        with (
            patch("swarm.service.shutil.which", return_value="/usr/local/bin/swarm"),
            patch("swarm.service._resolve_config_path", return_value=None),
        ):
            unit = generate_unit(None)

        assert "ExecStart=/usr/local/bin/swarm serve\n" in unit
        assert "-c " not in unit

    def test_raises_if_swarm_not_found(self) -> None:
        with (
            patch("swarm.service.shutil.which", return_value=None),
            pytest.raises(FileNotFoundError, match="swarm binary not found"),
        ):
            generate_unit(None)

    def test_workdir_is_config_parent(self, tmp_path: Path) -> None:
        config = tmp_path / "mydir" / "swarm.yaml"
        config.parent.mkdir(parents=True)
        config.write_text("workers: []")

        with patch("swarm.service.shutil.which", return_value="/usr/local/bin/swarm"):
            unit = generate_unit(str(config))

        assert f"WorkingDirectory={config.parent.resolve()}" in unit


class TestInstallService:
    """Test install_service function."""

    def test_creates_service_file(self, tmp_path: Path) -> None:
        service_dir = tmp_path / "systemd" / "user"
        service_path = service_dir / "swarm.service"

        with (
            patch("swarm.service._check_systemd", return_value=None),
            patch(
                "swarm.service.generate_unit",
                return_value="[Unit]\nDescription=Test\n",
            ),
            patch("swarm.service._SERVICE_DIR", service_dir),
            patch("swarm.service._SERVICE_PATH", service_path),
            patch("swarm.service._systemctl") as mock_ctl,
        ):
            result = install_service()

        assert result == service_path
        assert service_path.exists()
        assert "[Unit]" in service_path.read_text()

        # Verify systemctl calls
        calls = [c.args for c in mock_ctl.call_args_list]
        assert ("daemon-reload",) in calls
        assert ("enable", "swarm.service") in calls
        assert ("start", "swarm.service") in calls

    def test_raises_if_systemd_unavailable(self) -> None:
        with (
            patch("swarm.service._check_systemd", return_value="systemctl not found"),
            pytest.raises(RuntimeError, match="systemctl not found"),
        ):
            install_service()


class TestUninstallService:
    """Test uninstall_service function."""

    def test_removes_existing_service_file(self, tmp_path: Path) -> None:
        service_dir = tmp_path / "systemd" / "user"
        service_dir.mkdir(parents=True)
        service_path = service_dir / "swarm.service"
        service_path.write_text("[Unit]\nDescription=Test\n")

        with (
            patch("swarm.service._SERVICE_PATH", service_path),
            patch("swarm.service._systemctl") as mock_ctl,
        ):
            result = uninstall_service()

        assert result is True
        assert not service_path.exists()

        calls = [c.args for c in mock_ctl.call_args_list]
        assert ("stop", "swarm.service") in calls
        assert ("disable", "swarm.service") in calls
        assert ("daemon-reload",) in calls

    def test_returns_false_if_no_service_file(self, tmp_path: Path) -> None:
        service_path = tmp_path / "swarm.service"

        with (
            patch("swarm.service._SERVICE_PATH", service_path),
            patch("swarm.service._systemctl"),
        ):
            result = uninstall_service()

        assert result is False


class TestResolveConfigPath:
    """Test config path resolution."""

    def test_explicit_path_takes_priority(self, tmp_path: Path) -> None:
        from swarm.service import _resolve_config_path

        config = tmp_path / "custom.yaml"
        config.write_text("workers: []")

        result = _resolve_config_path(str(config))
        assert result == config.resolve()

    def test_finds_cwd_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from swarm.service import _resolve_config_path

        monkeypatch.chdir(tmp_path)
        config = tmp_path / "swarm.yaml"
        config.write_text("workers: []")

        result = _resolve_config_path(None)
        assert result == config.resolve()

    def test_returns_none_if_no_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from swarm.service import _resolve_config_path

        monkeypatch.chdir(tmp_path)

        with patch("swarm.service.Path.home", return_value=tmp_path):
            result = _resolve_config_path(None)

        assert result is None


class TestWslSystemdEnabled:
    """Test _wsl_systemd_enabled() detection."""

    def test_returns_true_when_enabled(self, tmp_path: Path) -> None:
        from swarm.service import _wsl_systemd_enabled

        conf = tmp_path / "wsl.conf"
        conf.write_text("[boot]\nsystemd=true\n")
        with patch("swarm.service._WSL_CONF", conf):
            assert _wsl_systemd_enabled() is True

    def test_returns_false_when_missing(self, tmp_path: Path) -> None:
        from swarm.service import _wsl_systemd_enabled

        conf = tmp_path / "wsl.conf"  # does not exist
        with patch("swarm.service._WSL_CONF", conf):
            assert _wsl_systemd_enabled() is False

    def test_returns_false_when_no_boot_section(self, tmp_path: Path) -> None:
        from swarm.service import _wsl_systemd_enabled

        conf = tmp_path / "wsl.conf"
        conf.write_text("[automount]\nenabled=true\n")
        with patch("swarm.service._WSL_CONF", conf):
            assert _wsl_systemd_enabled() is False

    def test_returns_false_when_systemd_false(self, tmp_path: Path) -> None:
        from swarm.service import _wsl_systemd_enabled

        conf = tmp_path / "wsl.conf"
        conf.write_text("[boot]\nsystemd=false\n")
        with patch("swarm.service._WSL_CONF", conf):
            assert _wsl_systemd_enabled() is False


class TestEnableWslSystemd:
    """Test enable_wsl_systemd() function."""

    def test_creates_wsl_conf_when_missing(self, tmp_path: Path) -> None:
        import subprocess as sp

        from swarm.service import enable_wsl_systemd

        conf = tmp_path / "wsl.conf"  # does not exist
        with (
            patch("swarm.service._WSL_CONF", conf),
            patch("swarm.service.subprocess.run") as mock_run,
        ):
            mock_run.return_value = sp.CompletedProcess([], 0, "", "")
            result = enable_wsl_systemd()

        assert result is True
        mock_run.assert_called_once()
        call_args = mock_run.call_args
        assert call_args[0][0] == ["sudo", "tee", str(conf)]
        written = call_args[1]["input"]
        assert "[boot]" in written
        assert "systemd = true" in written

    def test_appends_boot_section_to_existing(self, tmp_path: Path) -> None:
        import subprocess as sp

        from swarm.service import enable_wsl_systemd

        conf = tmp_path / "wsl.conf"
        conf.write_text("[automount]\nenabled = true\n")
        with (
            patch("swarm.service._WSL_CONF", conf),
            patch("swarm.service.subprocess.run") as mock_run,
        ):
            mock_run.return_value = sp.CompletedProcess([], 0, "", "")
            result = enable_wsl_systemd()

        assert result is True
        written = mock_run.call_args[1]["input"]
        assert "[automount]" in written
        assert "[boot]" in written
        assert "systemd = true" in written

    def test_inserts_systemd_into_existing_boot(self, tmp_path: Path) -> None:
        import subprocess as sp

        from swarm.service import enable_wsl_systemd

        conf = tmp_path / "wsl.conf"
        conf.write_text("[boot]\ncommand = /usr/bin/foo\n")
        with (
            patch("swarm.service._WSL_CONF", conf),
            patch("swarm.service.subprocess.run") as mock_run,
        ):
            mock_run.return_value = sp.CompletedProcess([], 0, "", "")
            result = enable_wsl_systemd()

        assert result is True
        written = mock_run.call_args[1]["input"]
        assert "systemd = true" in written
        assert "command = /usr/bin/foo" in written

    def test_noop_if_already_enabled(self, tmp_path: Path) -> None:
        from swarm.service import enable_wsl_systemd

        conf = tmp_path / "wsl.conf"
        conf.write_text("[boot]\nsystemd=true\n")
        with (
            patch("swarm.service._WSL_CONF", conf),
            patch("swarm.service.subprocess.run") as mock_run,
        ):
            result = enable_wsl_systemd()

        assert result is True
        mock_run.assert_not_called()

    def test_raises_on_sudo_failure(self, tmp_path: Path) -> None:
        import subprocess as sp

        from swarm.service import enable_wsl_systemd

        conf = tmp_path / "wsl.conf"
        with (
            patch("swarm.service._WSL_CONF", conf),
            patch("swarm.service.subprocess.run") as mock_run,
        ):
            mock_run.return_value = sp.CompletedProcess([], 1, "", "permission denied")
            with pytest.raises(RuntimeError, match="Failed to write"):
                enable_wsl_systemd()
