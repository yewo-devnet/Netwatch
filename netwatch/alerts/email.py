"""
SMTP email alert dispatcher for NetWatch.

Features:
- Auto-detects STARTTLS (port 587) vs implicit TLS (port 465) from port.
- Reads NETWATCH_SMTP_PASSWORD from the environment; never logs it.
- Retries up to 3 times with exponential back-off (1 s → 2 s → 4 s).
- Subject format:
    [NetWatch] ALERT – <label> is DOWN
    [NetWatch] RECOVERED – <label> is UP

Security note:
    The SMTP password is read from the environment variable
    NETWATCH_SMTP_PASSWORD at send time. It is never stored on the object,
    never passed as a function argument visible in tracebacks, and never
    written to any log handler.
"""

import datetime
import os
import smtplib
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Dict, List, Optional

from netwatch.utils.logger import get_logger

log = get_logger(__name__)

_MAX_RETRIES = 3
_BACKOFF_BASE = 1  # seconds  (1 → 2 → 4)


def _get_password() -> Optional[str]:
    """Read SMTP password from the environment without ever logging it."""
    return os.environ.get("NETWATCH_SMTP_PASSWORD")


def _build_message(
    smtp_username: str,
    recipients: List[str],
    subject: str,
    body: str,
) -> MIMEMultipart:
    """Construct a MIME email message."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = smtp_username
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(body, "plain", "utf-8"))
    return msg


def _send_once(
    smtp_cfg: Dict[str, Any],
    msg: MIMEMultipart,
    recipients: List[str],
) -> None:
    """
    Attempt one SMTP delivery.

    Raises:
        smtplib.SMTPException: on any SMTP-level failure.
        OSError: on connection-level failure.
    """
    host: str = smtp_cfg["host"]
    port: int = smtp_cfg["port"]
    username: str = smtp_cfg["username"]
    password: Optional[str] = _get_password()

    if password is None:
        raise smtplib.SMTPException(
            "NETWATCH_SMTP_PASSWORD environment variable is not set."
        )

    # Port 465 → implicit TLS (SMTP_SSL); port 587 → STARTTLS
    if port == 465:
        log.debug("Connecting to SMTP %s:%d with implicit TLS", host, port)
        with smtplib.SMTP_SSL(host, port, timeout=10) as server:
            server.login(username, password)
            server.sendmail(username, recipients, msg.as_string())
    else:
        log.debug("Connecting to SMTP %s:%d with STARTTLS", host, port)
        with smtplib.SMTP(host, port, timeout=10) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(username, password)
            server.sendmail(username, recipients, msg.as_string())


def send_alert(
    smtp_cfg: Dict[str, Any],
    label: str,
    host: str,
    check_type: str,
    is_down: bool,
    failure_reason: Optional[str],
    consecutive_failures: int,
) -> bool:
    """
    Send an alert email with retry logic.

    Args:
        smtp_cfg:             Dict with keys: host, port, username, recipients.
        label:                Human-readable device label.
        host:                 Device hostname or IP.
        check_type:           Which check triggered the alert (ping/port/http).
        is_down:              True → DOWN alert, False → RECOVERED alert.
        failure_reason:       Brief description of the failure (may be None).
        consecutive_failures: How many consecutive failures have occurred.

    Returns:
        True if the email was sent successfully, False after all retries failed.
    """
    state_word = "DOWN" if is_down else "UP"
    alert_word = "ALERT" if is_down else "RECOVERED"
    subject = f"[NetWatch] {alert_word} – {label} is {state_word}"

    utc_now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    reason_line = failure_reason or "N/A"

    body_lines = [
        "NetWatch Notification",
        "=" * 40,
        f"Device Label       : {label}",
        f"Host               : {host}",
        f"Check Type         : {check_type}",
        f"Status             : {state_word}",
        f"Failure Reason     : {reason_line}",
        f"Consecutive Fails  : {consecutive_failures}",
        f"Timestamp (UTC)    : {utc_now}",
        "=" * 40,
        "",
        "This is an automated alert from NetWatch.",
    ]
    body = "\n".join(body_lines)

    recipients: List[str] = smtp_cfg.get("recipients", [])
    username: str = smtp_cfg.get("username", "")

    msg = _build_message(username, recipients, subject, body)

    # Log the subject but never the password
    log.info(
        "Sending email alert: '%s' to %d recipient(s)", subject, len(recipients)
    )

    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            _send_once(smtp_cfg, msg, recipients)
            log.info("Email alert sent successfully on attempt %d.", attempt)
            return True
        except (smtplib.SMTPException, OSError) as exc:
            wait = _BACKOFF_BASE * (2 ** (attempt - 1))
            if attempt < _MAX_RETRIES:
                log.warning(
                    "Email send attempt %d/%d failed: %s. Retrying in %ds…",
                    attempt,
                    _MAX_RETRIES,
                    exc,
                    wait,
                )
                time.sleep(wait)
            else:
                log.error(
                    "Email send failed after %d attempts: %s",
                    _MAX_RETRIES,
                    exc,
                )

    return False
