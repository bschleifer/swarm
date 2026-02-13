"""Tests for server/webctl.py — web dashboard process control."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

from swarm.server import webctl


# ---------- Subprocess web server ----------


class TestWebIsRunning:
    """Tests for web_is_running()."""

    def test_no_pid_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(webctl, "_WEB_PID_FILE", tmp_path / "web.pid")
        assert webctl.web_is_running() is None

    def test_pid_file_valid_process(self, tmp_path, monkeypatch):
        pid_file = tmp_path / "web.pid"
        pid_file.write_text("12345")
        monkeypatch.setattr(webctl, "_WEB_PID_FILE", pid_file)
        monkeypatch.setattr("os.kill", lambda pid, sig: None)  # Process exists
        assert webctl.web_is_running() == 12345

    def test_pid_file_dead_process(self, tmp_path, monkeypatch):
        pid_file = tmp_path / "web.pid"
        pid_file.write_text("99999")
        monkeypatch.setattr(webctl, "_WEB_PID_FILE", pid_file)
        monkeypatch.setattr("os.kill", MagicMock(side_effect=ProcessLookupError))
        assert webctl.web_is_running() is None
        assert not pid_file.exists()  # Stale PID file cleaned up

    def test_pid_file_invalid_content(self, tmp_path, monkeypatch):
        pid_file = tmp_path / "web.pid"
        pid_file.write_text("not-a-number")
        monkeypatch.setattr(webctl, "_WEB_PID_FILE", pid_file)
        assert webctl.web_is_running() is None
        assert not pid_file.exists()


class TestWebStart:
    """Tests for web_start()."""

    def test_already_running(self, tmp_path, monkeypatch):
        pid_file = tmp_path / "web.pid"
        pid_file.write_text("12345")
        monkeypatch.setattr(webctl, "_WEB_PID_FILE", pid_file)
        monkeypatch.setattr(webctl, "_PID_DIR", tmp_path)
        monkeypatch.setattr("os.kill", lambda pid, sig: None)
        ok, msg = webctl.web_start()
        assert not ok
        assert "Already running" in msg

    def test_start_success(self, tmp_path, monkeypatch):
        pid_file = tmp_path / "web.pid"
        log_file = tmp_path / "web.log"
        monkeypatch.setattr(webctl, "_WEB_PID_FILE", pid_file)
        monkeypatch.setattr(webctl, "_WEB_LOG_FILE", log_file)
        monkeypatch.setattr(webctl, "_PID_DIR", tmp_path)

        mock_proc = MagicMock()
        mock_proc.pid = 42
        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            ok, msg = webctl.web_start(host="0.0.0.0", port=8080)
        assert ok
        assert "42" in msg
        assert pid_file.read_text() == "42"
        # Verify correct command constructed
        cmd = mock_popen.call_args[0][0]
        assert "--host" in cmd
        assert "8080" in cmd

    def test_start_with_config_and_session(self, tmp_path, monkeypatch):
        pid_file = tmp_path / "web.pid"
        log_file = tmp_path / "web.log"
        monkeypatch.setattr(webctl, "_WEB_PID_FILE", pid_file)
        monkeypatch.setattr(webctl, "_WEB_LOG_FILE", log_file)
        monkeypatch.setattr(webctl, "_PID_DIR", tmp_path)

        mock_proc = MagicMock()
        mock_proc.pid = 99
        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            ok, msg = webctl.web_start(config_path="/tmp/my.yaml", session="my-swarm")
        assert ok
        cmd = mock_popen.call_args[0][0]
        assert "-c" in cmd
        assert "/tmp/my.yaml" in cmd
        assert "-s" in cmd
        assert "my-swarm" in cmd


class TestWebStop:
    """Tests for web_stop()."""

    def test_stop_no_pid_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(webctl, "_WEB_PID_FILE", tmp_path / "web.pid")
        ok, msg = webctl.web_stop()
        assert not ok
        assert "Not running" in msg

    def test_stop_invalid_pid_file(self, tmp_path, monkeypatch):
        pid_file = tmp_path / "web.pid"
        pid_file.write_text("garbage")
        monkeypatch.setattr(webctl, "_WEB_PID_FILE", pid_file)
        ok, msg = webctl.web_stop()
        assert not ok
        assert "Invalid" in msg
        assert not pid_file.exists()

    def test_stop_running(self, tmp_path, monkeypatch):
        pid_file = tmp_path / "web.pid"
        pid_file.write_text("54321")
        monkeypatch.setattr(webctl, "_WEB_PID_FILE", pid_file)
        kill_mock = MagicMock()
        monkeypatch.setattr("os.kill", kill_mock)
        ok, msg = webctl.web_stop()
        assert ok
        assert "54321" in msg
        assert not pid_file.exists()

    def test_stop_already_dead(self, tmp_path, monkeypatch):
        pid_file = tmp_path / "web.pid"
        pid_file.write_text("99999")
        monkeypatch.setattr(webctl, "_WEB_PID_FILE", pid_file)
        monkeypatch.setattr("os.kill", MagicMock(side_effect=ProcessLookupError))
        ok, msg = webctl.web_stop()
        assert ok
        assert "already gone" in msg
        assert not pid_file.exists()


# ---------- Embedded web server ----------


class TestEmbedded:
    """Tests for the embedded web server functions."""

    def test_is_running_when_no_thread(self, monkeypatch):
        monkeypatch.setattr(webctl, "_embedded_thread", None)
        assert webctl.web_is_running_embedded() is False

    def test_is_running_when_dead_thread(self, monkeypatch):
        mock_thread = MagicMock(spec=threading.Thread)
        mock_thread.is_alive.return_value = False
        monkeypatch.setattr(webctl, "_embedded_thread", mock_thread)
        assert webctl.web_is_running_embedded() is False

    def test_is_running_when_alive_thread(self, monkeypatch):
        mock_thread = MagicMock(spec=threading.Thread)
        mock_thread.is_alive.return_value = True
        monkeypatch.setattr(webctl, "_embedded_thread", mock_thread)
        assert webctl.web_is_running_embedded() is True

    def test_start_already_running(self, monkeypatch):
        mock_thread = MagicMock(spec=threading.Thread)
        mock_thread.is_alive.return_value = True
        monkeypatch.setattr(webctl, "_embedded_thread", mock_thread)
        ok, msg = webctl.web_start_embedded(MagicMock())
        assert not ok
        assert "Already running" in msg

    def test_stop_not_running(self, monkeypatch):
        monkeypatch.setattr(webctl, "_embedded_loop", None)
        monkeypatch.setattr(webctl, "_embedded_thread", None)
        monkeypatch.setattr(webctl, "_embedded_runner", None)
        ok, msg = webctl.web_stop_embedded()
        assert not ok
        assert "Not running" in msg

    def test_stop_running(self, monkeypatch):
        mock_loop = MagicMock()
        mock_loop.is_closed.return_value = False
        mock_thread = MagicMock(spec=threading.Thread)
        monkeypatch.setattr(webctl, "_embedded_loop", mock_loop)
        monkeypatch.setattr(webctl, "_embedded_thread", mock_thread)
        monkeypatch.setattr(webctl, "_embedded_runner", MagicMock())
        ok, msg = webctl.web_stop_embedded()
        assert ok
        assert "Stopped" in msg
        mock_loop.call_soon_threadsafe.assert_called_once()
        mock_thread.join.assert_called_once_with(timeout=5)
