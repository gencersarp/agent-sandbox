"""Structured logging configuration for Agent Sandbox.

Call :func:`setup_logging` once at application startup (CLI or entrypoint).
All modules should use ``logging.getLogger(__name__)`` to obtain their logger.
"""

from __future__ import annotations

import logging
import sys

_LOG_FORMAT = "%(asctime)s %(levelname)-8s [%(name)s] %(message)s"
_LOG_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"

_configured = False


def setup_logging(*, verbose: bool = False, log_file: str | None = None) -> None:
    """Configure the root logger for the application.

    Parameters
    ----------
    verbose:
        If ``True``, set the level to DEBUG; otherwise INFO.
    log_file:
        Optional path to a log file.  If provided, a file handler is added
        in addition to the stderr handler.
    """
    global _configured
    if _configured:
        return
    _configured = True

    level = logging.DEBUG if verbose else logging.INFO

    root = logging.getLogger()
    root.setLevel(level)

    # Console handler (stderr)
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(level)
    console.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATE_FORMAT))
    root.addHandler(console)

    # File handler (optional)
    if log_file:
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATE_FORMAT))
        root.addHandler(fh)

    # Suppress noisy third-party loggers
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
