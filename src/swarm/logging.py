"""Centralized logging configuration for swarm."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

_DEFAULT_LOG_FILE = "~/.swarm/swarm.log"
_MAX_LOG_BYTES = 5 * 1024 * 1024  # 5 MB
_BACKUP_COUNT = 3


def setup_logging(
    level: str = "WARNING",
    log_file: str | None = None,
    stderr: bool = True,
) -> logging.Logger:
    """Configure and return the swarm root logger.

    Called once at startup (CLI entry point).  All modules use
    ``logging.getLogger("swarm.<module>")`` which inherit this config.

    Parameters
    ----------
    level:
        Logging verbosity (DEBUG, INFO, WARNING, ERROR).
    log_file:
        Explicit log file path.  When *None* the default
        ``~/.swarm/swarm.log`` is used so there is always a file to
        check for troubleshooting.
    stderr:
        Whether to also emit log records to stderr.  Should be *False*
        when running inside the Textual TUI (stderr output corrupts the
        terminal UI).
    """
    logger = logging.getLogger("swarm")

    # Close and clear existing handlers so re-configuration works correctly
    for h in logger.handlers[:]:
        h.close()
    logger.handlers.clear()

    logger.setLevel(getattr(logging, level.upper(), logging.WARNING))

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # stderr handler (skip for TUI mode)
    if stderr:
        stderr_handler = logging.StreamHandler(sys.stderr)
        stderr_handler.setFormatter(fmt)
        logger.addHandler(stderr_handler)

    # File handler — always present for troubleshooting
    file_path = Path(log_file or _DEFAULT_LOG_FILE).expanduser()
    file_path.parent.mkdir(parents=True, exist_ok=True)
    from logging.handlers import RotatingFileHandler

    file_handler = RotatingFileHandler(
        str(file_path),
        maxBytes=_MAX_LOG_BYTES,
        backupCount=_BACKUP_COUNT,
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    return logger


def get_logger(name: str) -> logging.Logger:
    """Return a child logger under the ``swarm`` namespace."""
    return logging.getLogger(f"swarm.{name}")
