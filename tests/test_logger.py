"""
Unit tests for netwatch.utils.logger
"""

import logging

from netwatch.utils.logger import (
    _ColourFormatter,
    configure_root_logger,
    get_logger,
)


class TestColourFormatter:
    def _make_record(self, level: int, msg: str) -> logging.LogRecord:
        record = logging.LogRecord(
            name="test",
            level=level,
            pathname="test.py",
            lineno=1,
            msg=msg,
            args=(),
            exc_info=None,
        )
        return record

    def test_colour_applied_on_tty(self):
        fmt = _ColourFormatter(
            fmt="%(levelname)s: %(message)s",
            datefmt="%H:%M:%S",
            use_colour=True,
        )
        record = self._make_record(logging.INFO, "hello")
        output = fmt.format(record)
        # Should start with an ANSI escape code
        assert output.startswith("\033[")

    def test_no_colour_without_tty(self):
        fmt = _ColourFormatter(
            fmt="%(levelname)s: %(message)s",
            datefmt="%H:%M:%S",
            use_colour=False,
        )
        record = self._make_record(logging.INFO, "hello")
        output = fmt.format(record)
        assert not output.startswith("\033[")

    def test_all_levels_have_colour_code(self):
        fmt = _ColourFormatter(
            fmt="%(levelname)s: %(message)s",
            datefmt=None,
            use_colour=True,
        )
        for level in (logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR):
            record = self._make_record(level, "msg")
            output = fmt.format(record)
            assert "\033[" in output


class TestConfigureRootLogger:
    def teardown_method(self):
        """Reset root logger after each test."""
        root = logging.getLogger()
        root.handlers.clear()

    def test_sets_log_level_info(self):
        configure_root_logger("INFO")
        assert logging.getLogger().level == logging.INFO

    def test_sets_log_level_debug(self):
        configure_root_logger("DEBUG")
        assert logging.getLogger().level == logging.DEBUG

    def test_adds_console_handler(self):
        configure_root_logger("INFO")
        root = logging.getLogger()
        assert any(isinstance(h, logging.StreamHandler) for h in root.handlers)

    def test_adds_file_handler(self, tmp_path):
        log_file = str(tmp_path / "test.log")
        configure_root_logger("INFO", log_file=log_file)
        from logging.handlers import RotatingFileHandler

        root = logging.getLogger()
        assert any(isinstance(h, RotatingFileHandler) for h in root.handlers)
        # Clean up
        for h in root.handlers:
            h.close()

    def test_clears_existing_handlers_on_reconfigure(self):
        configure_root_logger("INFO")
        configure_root_logger("DEBUG")
        root = logging.getLogger()
        # Should not accumulate duplicate handlers
        console_handlers = [
            h for h in root.handlers if type(h) is logging.StreamHandler
        ]
        assert len(console_handlers) == 1

    def test_invalid_log_file_does_not_crash(self):
        """An unwritable log file path should log a warning and not raise."""
        configure_root_logger("INFO", log_file="/nonexistent_dir/test.log")
        # Should still have the console handler
        root = logging.getLogger()
        assert len(root.handlers) >= 1


class TestGetLogger:
    def test_returns_logger(self):
        logger = get_logger("netwatch.test")
        assert isinstance(logger, logging.Logger)

    def test_logger_name(self):
        logger = get_logger("my.module")
        assert logger.name == "my.module"

    def test_same_name_returns_same_instance(self):
        a = get_logger("netwatch.utils.test")
        b = get_logger("netwatch.utils.test")
        assert a is b
