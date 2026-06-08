"""
Rotating file + colour console logger for NetWatch.

Colour codes by severity:
    DEBUG   -> cyan
    INFO    -> green
    WARNING -> yellow
    ERROR   -> red

Degrades gracefully on non-TTY outputs (colour stripped).
"""

import logging
import sys
from logging.handlers import RotatingFileHandler
from typing import Optional

# ANSI colour codes
_RESET = "\033[0m"
_COLOURS = {
    logging.DEBUG: "\033[36m",     # cyan
    logging.INFO: "\033[32m",      # green
    logging.WARNING: "\033[33m",   # yellow
    logging.ERROR: "\033[31m",     # red
    logging.CRITICAL: "\033[31m",  # red (same as ERROR)
}

# Default format strings
_CONSOLE_FMT = "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s"
_FILE_FMT = "%(asctime)s [%(levelname)-8s] %(name)s (%(filename)s:%(lineno)d): %(message)s"
_DATE_FMT = "%Y-%m-%d %H:%M:%S"

# Max log file size: 10 MB, keep 5 backups
_MAX_BYTES = 10 * 1024 * 1024
_BACKUP_COUNT = 5

# Registry so we don't add duplicate handlers
_configured_loggers: dict = {}


class _ColourFormatter(logging.Formatter):
    """Formatter that injects ANSI colour codes on TTY streams."""

    def __init__(self, fmt: str, datefmt: str, use_colour: bool) -> None:
        super().__init__(fmt=fmt, datefmt=datefmt)
        self._use_colour = use_colour

    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        if self._use_colour:
            colour = _COLOURS.get(record.levelno, "")
            message = f"{colour}{message}{_RESET}"
        return message


def configure_root_logger(
    log_level: str = "INFO",
    log_file: Optional[str] = None,
) -> None:
    """
    Configure the root logger once at application start-up.

    Args:
        log_level: Logging level string (DEBUG / INFO / WARNING / ERROR).
        log_file:  Optional path to the rotating log file.
    """
    root = logging.getLogger()
    level = getattr(logging, log_level.upper(), logging.INFO)
    root.setLevel(level)

    # Avoid adding handlers more than once
    if root.handlers:
        root.handlers.clear()

    # Console handler
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(level)
    use_colour = hasattr(sys.stderr, "isatty") and sys.stderr.isatty()
    console_handler.setFormatter(
        _ColourFormatter(_CONSOLE_FMT, _DATE_FMT, use_colour=use_colour)
    )
    root.addHandler(console_handler)

    # File handler (optional)
    if log_file:
        try:
            file_handler = RotatingFileHandler(
                log_file,
                maxBytes=_MAX_BYTES,
                backupCount=_BACKUP_COUNT,
                encoding="utf-8",
            )
            file_handler.setLevel(level)
            file_handler.setFormatter(
                logging.Formatter(fmt=_FILE_FMT, datefmt=_DATE_FMT)
            )
            root.addHandler(file_handler)
        except OSError as exc:
            root.warning("Could not open log file %s: %s", log_file, exc)


def get_logger(name: str) -> logging.Logger:
    """
    Return a module-level logger.

    All loggers share the root configuration set by configure_root_logger().
    Calling this before configure_root_logger() will use Python's default
    logging configuration (no handlers, propagates to root).

    Args:
        name: Typically __name__ of the calling module.

    Returns:
        logging.Logger instance.

    Example::

        from netwatch.utils.logger import get_logger
        log = get_logger(__name__)
        log.info("Module loaded.")
    """
    return logging.getLogger(name)
