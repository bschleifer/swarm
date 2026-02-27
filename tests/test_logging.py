"""Tests for swarm.logging — setup_logging and get_logger."""

from __future__ import annotations

import logging

from swarm.logging import get_logger, setup_logging


class TestSetupLogging:
    def test_returns_swarm_root_logger(self):
        logger = setup_logging(level="WARNING", stderr=False)
        assert logger.name == "swarm"

    def test_sets_level(self):
        logger = setup_logging(level="DEBUG", stderr=False)
        assert logger.level == logging.DEBUG

    def test_invalid_level_defaults_to_warning(self):
        logger = setup_logging(level="BOGUS", stderr=False)
        assert logger.level == logging.WARNING

    def test_creates_file_handler(self, tmp_path):
        log_file = str(tmp_path / "test.log")
        logger = setup_logging(level="INFO", log_file=log_file, stderr=False)
        handler_types = [type(h).__name__ for h in logger.handlers]
        assert "RotatingFileHandler" in handler_types

    def test_stderr_handler_when_enabled(self):
        logger = setup_logging(level="WARNING", stderr=True)
        handler_types = [type(h).__name__ for h in logger.handlers]
        assert "StreamHandler" in handler_types

    def test_no_stderr_handler_when_disabled(self, tmp_path):
        log_file = str(tmp_path / "test.log")
        logger = setup_logging(level="WARNING", log_file=log_file, stderr=False)
        handler_types = [type(h).__name__ for h in logger.handlers]
        assert "StreamHandler" not in handler_types

    def test_reconfigure_clears_old_handlers(self, tmp_path):
        log_file = str(tmp_path / "test.log")
        setup_logging(level="WARNING", log_file=log_file, stderr=False)
        logger = setup_logging(level="DEBUG", log_file=log_file, stderr=False)
        # Should only have the file handler, not duplicates
        assert len(logger.handlers) == 1


class TestGetLogger:
    def test_returns_child_logger(self):
        child = get_logger("test.module")
        assert child.name == "swarm.test.module"

    def test_child_inherits_parent_level(self):
        setup_logging(level="DEBUG", stderr=False)
        child = get_logger("test.inherit")
        assert child.getEffectiveLevel() == logging.DEBUG
