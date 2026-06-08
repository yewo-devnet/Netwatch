"""
Unit tests for netwatch.alerts.email

SMTP calls are fully mocked; password env var is patched.
"""

import smtplib
from unittest.mock import MagicMock, patch

import pytest

from netwatch.alerts.email import _build_message, _get_password, _send_once, send_alert


# ---------------------------------------------------------------------------
# _get_password
# ---------------------------------------------------------------------------

class TestGetPassword:
    def test_returns_env_value(self):
        with patch.dict("os.environ", {"NETWATCH_SMTP_PASSWORD": "secret"}):
            assert _get_password() == "secret"

    def test_returns_none_when_unset(self):
        with patch.dict("os.environ", {}, clear=True):
            # Remove the variable if it exists
            import os
            os.environ.pop("NETWATCH_SMTP_PASSWORD", None)
            assert _get_password() is None


# ---------------------------------------------------------------------------
# _build_message
# ---------------------------------------------------------------------------

class TestBuildMessage:
    def test_subject_set(self):
        msg = _build_message("from@x.com", ["to@x.com"], "Test Subject", "body text")
        assert msg["Subject"] == "Test Subject"

    def test_from_header(self):
        msg = _build_message("from@x.com", ["to@x.com"], "Subj", "body")
        assert msg["From"] == "from@x.com"

    def test_multiple_recipients(self):
        msg = _build_message("f@x.com", ["a@x.com", "b@x.com"], "S", "B")
        assert "a@x.com" in msg["To"]
        assert "b@x.com" in msg["To"]


# ---------------------------------------------------------------------------
# _send_once
# ---------------------------------------------------------------------------

_SMTP_CFG = {
    "host": "smtp.example.com",
    "port": 587,
    "username": "alerts@example.com",
    "recipients": ["admin@example.com"],
}

_SMTP_CFG_TLS = {
    "host": "smtp.example.com",
    "port": 465,
    "username": "alerts@example.com",
    "recipients": ["admin@example.com"],
}


class TestSendOnce:
    def test_raises_when_password_unset(self):
        import os
        os.environ.pop("NETWATCH_SMTP_PASSWORD", None)
        with patch.dict("os.environ", {}, clear=True):
            dummy_msg = _build_message("f@x.com", ["t@x.com"], "s", "b")
            with pytest.raises(smtplib.SMTPException, match="NETWATCH_SMTP_PASSWORD"):
                _send_once(_SMTP_CFG, dummy_msg, ["t@x.com"])

    def test_starttls_path(self):
        dummy_msg = _build_message("f@x.com", ["t@x.com"], "s", "b")
        mock_smtp = MagicMock()
        mock_smtp.__enter__ = lambda s: s
        mock_smtp.__exit__ = MagicMock(return_value=False)

        with patch.dict("os.environ", {"NETWATCH_SMTP_PASSWORD": "pw"}):
            with patch("smtplib.SMTP", return_value=mock_smtp):
                _send_once(_SMTP_CFG, dummy_msg, ["t@x.com"])

        mock_smtp.starttls.assert_called_once()
        mock_smtp.login.assert_called_once()
        mock_smtp.sendmail.assert_called_once()

    def test_implicit_tls_path(self):
        dummy_msg = _build_message("f@x.com", ["t@x.com"], "s", "b")
        mock_smtp = MagicMock()
        mock_smtp.__enter__ = lambda s: s
        mock_smtp.__exit__ = MagicMock(return_value=False)

        with patch.dict("os.environ", {"NETWATCH_SMTP_PASSWORD": "pw"}):
            with patch("smtplib.SMTP_SSL", return_value=mock_smtp):
                _send_once(_SMTP_CFG_TLS, dummy_msg, ["t@x.com"])

        mock_smtp.login.assert_called_once()
        mock_smtp.sendmail.assert_called_once()


# ---------------------------------------------------------------------------
# send_alert (public API with retry logic)
# ---------------------------------------------------------------------------

class TestSendAlert:
    def _base_call(self, **kwargs):
        defaults = dict(
            smtp_cfg=_SMTP_CFG,
            label="Web Server",
            host="10.0.0.50",
            check_type="ping",
            is_down=True,
            failure_reason="ICMP timeout",
            consecutive_failures=3,
        )
        defaults.update(kwargs)
        return defaults

    def test_successful_send_returns_true(self):
        with patch.dict("os.environ", {"NETWATCH_SMTP_PASSWORD": "pw"}):
            with patch("netwatch.alerts.email._send_once"):
                result = send_alert(**self._base_call())

        assert result is True

    def test_subject_down(self):
        captured_msgs = []

        def capture(cfg, msg, recipients):
            captured_msgs.append(msg["Subject"])

        with patch.dict("os.environ", {"NETWATCH_SMTP_PASSWORD": "pw"}):
            with patch("netwatch.alerts.email._send_once", side_effect=capture):
                send_alert(**self._base_call(is_down=True))

        assert "[NetWatch] ALERT" in captured_msgs[0]
        assert "DOWN" in captured_msgs[0]

    def test_subject_recovered(self):
        captured_msgs = []

        def capture(cfg, msg, recipients):
            captured_msgs.append(msg["Subject"])

        with patch.dict("os.environ", {"NETWATCH_SMTP_PASSWORD": "pw"}):
            with patch("netwatch.alerts.email._send_once", side_effect=capture):
                send_alert(**self._base_call(is_down=False))

        assert "[NetWatch] RECOVERED" in captured_msgs[0]
        assert "UP" in captured_msgs[0]

    def test_retry_on_smtp_exception(self):
        """Should retry 3 times on SMTPException before giving up."""
        with patch.dict("os.environ", {"NETWATCH_SMTP_PASSWORD": "pw"}):
            with patch(
                "netwatch.alerts.email._send_once",
                side_effect=smtplib.SMTPException("conn failed"),
            ) as mock_send:
                with patch("time.sleep"):  # don't actually sleep
                    result = send_alert(**self._base_call())

        assert mock_send.call_count == 3
        assert result is False

    def test_succeeds_on_second_attempt(self):
        """Fails once, then succeeds."""
        call_count = 0

        def flaky(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise smtplib.SMTPException("transient error")

        with patch.dict("os.environ", {"NETWATCH_SMTP_PASSWORD": "pw"}):
            with patch("netwatch.alerts.email._send_once", side_effect=flaky):
                with patch("time.sleep"):
                    result = send_alert(**self._base_call())

        assert result is True
        assert call_count == 2

    def test_body_contains_expected_fields(self):
        """Email body must contain label, host, check type, and UTC timestamp."""
        captured_bodies = []

        def capture(cfg, msg, recipients):
            # Extract the plain-text part
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    captured_bodies.append(
                        part.get_payload(decode=True).decode("utf-8")
                    )

        with patch.dict("os.environ", {"NETWATCH_SMTP_PASSWORD": "pw"}):
            with patch("netwatch.alerts.email._send_once", side_effect=capture):
                send_alert(**self._base_call())

        assert captured_bodies, "No text/plain part found"
        body = captured_bodies[0]
        assert "Web Server" in body
        assert "10.0.0.50" in body
        assert "ping" in body
        assert "UTC" in body

    def test_password_not_in_log(self, caplog):
        """Ensure the SMTP password never appears in log output."""
        import logging

        with patch.dict("os.environ", {"NETWATCH_SMTP_PASSWORD": "super_secret_pw"}):
            with patch("netwatch.alerts.email._send_once"):
                with caplog.at_level(logging.DEBUG, logger="netwatch.alerts.email"):
                    send_alert(**self._base_call())

        for record in caplog.records:
            assert "super_secret_pw" not in record.getMessage()
