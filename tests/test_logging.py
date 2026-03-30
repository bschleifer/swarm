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


class TestJsonFormatter:
    def test_output_is_valid_json(self):
        import json

        from swarm.logging import JsonFormatter

        fmt = JsonFormatter(datefmt="%Y-%m-%dT%H:%M:%S")
        record = logging.LogRecord("swarm.test", logging.INFO, "", 0, "hello world", (), None)
        output = fmt.format(record)
        parsed = json.loads(output)
        assert parsed["level"] == "INFO"
        assert parsed["logger"] == "swarm.test"
        assert parsed["msg"] == "hello world"
        assert "exc" not in parsed

    def test_includes_exception(self):
        import json
        import sys

        from swarm.logging import JsonFormatter

        fmt = JsonFormatter(datefmt="%Y-%m-%dT%H:%M:%S")
        try:
            raise ValueError("boom")
        except ValueError:
            record = logging.LogRecord(
                "swarm.test", logging.ERROR, "", 0, "fail", (), sys.exc_info()
            )
        output = fmt.format(record)
        parsed = json.loads(output)
        assert "exc" in parsed
        assert "ValueError" in parsed["exc"]

    def test_json_format_flag(self, tmp_path):
        log_file = str(tmp_path / "test.log")
        logger = setup_logging(level="INFO", log_file=log_file, stderr=False, json_format=True)
        handler = logger.handlers[0]
        from swarm.logging import JsonFormatter

        assert isinstance(handler.formatter, JsonFormatter)


class TestGetLogger:
    def test_returns_child_logger(self):
        child = get_logger("test.module")
        assert child.name == "swarm.test.module"

    def test_child_inherits_parent_level(self):
        setup_logging(level="DEBUG", stderr=False)
        child = get_logger("test.inherit")
        assert child.getEffectiveLevel() == logging.DEBUG
