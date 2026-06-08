"""
Unit tests for netwatch.monitor.engine

Uses mocked checkers and threading events.
"""

import queue
import time
from unittest.mock import patch

import pytest

from netwatch.checker.http import HttpResult
from netwatch.checker.ping import PingResult
from netwatch.checker.port import PortResult
from netwatch.monitor.engine import DeviceState, MonitorEngine, StateEvent


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_BASE_CONFIG = {
    "monitoring": {
        "interval": 1,
        "alert_cooldown": 300,
        "log_level": "DEBUG",
    },
    "devices": [
        {
            "label": "Test Device",
            "host": "192.168.1.1",
            "checks": ["ping"],
        }
    ],
}

_MULTI_CHECK_CONFIG = {
    "monitoring": {
        "interval": 1,
        "alert_cooldown": 300,
        "log_level": "DEBUG",
    },
    "devices": [
        {
            "label": "Multi",
            "host": "10.0.0.1",
            "checks": ["ping", "port", "http"],
            "ports": [80],
            "url": "http://10.0.0.1/health",
        }
    ],
}


def _make_engine(config=None, no_alert=True):
    cfg = config or _BASE_CONFIG
    return MonitorEngine(config=cfg, no_alert=no_alert, interval=1)


# ---------------------------------------------------------------------------
# DeviceStatus initialisation
# ---------------------------------------------------------------------------

class TestEngineInit:
    def test_devices_initialised_as_unknown(self):
        engine = _make_engine()
        statuses = engine.get_statuses()
        assert len(statuses) == 1
        assert statuses[0].state == DeviceState.UNKNOWN
        assert statuses[0].label == "Test Device"

    def test_get_statuses_returns_copy(self):
        engine = _make_engine()
        s1 = engine.get_statuses()
        s2 = engine.get_statuses()
        assert s1 is not s2  # different list objects


# ---------------------------------------------------------------------------
# _update_state
# ---------------------------------------------------------------------------

class TestUpdateState:
    def test_unknown_to_up(self):
        engine = _make_engine()
        engine._update_state("Test Device", "192.168.1.1", "ping", True, 5.0, None)
        statuses = engine.get_statuses()
        assert statuses[0].state == DeviceState.UP

    def test_unknown_to_down(self):
        engine = _make_engine()
        engine._update_state(
            "Test Device", "192.168.1.1", "ping", False, None, "timeout"
        )
        statuses = engine.get_statuses()
        assert statuses[0].state == DeviceState.DOWN
        assert statuses[0].consecutive_failures == 1

    def test_consecutive_failure_increments(self):
        engine = _make_engine()
        for _ in range(3):
            engine._update_state(
                "Test Device", "192.168.1.1", "ping", False, None, "timeout"
            )
        statuses = engine.get_statuses()
        assert statuses[0].consecutive_failures == 3

    def test_recovery_resets_failures(self):
        engine = _make_engine()
        engine._update_state(
            "Test Device", "192.168.1.1", "ping", False, None, "fail"
        )
        engine._update_state(
            "Test Device", "192.168.1.1", "ping", True, 5.0, None
        )
        statuses = engine.get_statuses()
        assert statuses[0].consecutive_failures == 0

    def test_state_transition_emits_event(self):
        engine = _make_engine()
        engine._update_state(
            "Test Device", "192.168.1.1", "ping", False, None, "fail"
        )
        event = engine._event_queue.get_nowait()
        assert event.new_state == DeviceState.DOWN

    def test_no_event_for_same_state_transition(self):
        """UP → UP should not emit a new event."""
        engine = _make_engine()
        # First transition: UNKNOWN → UP (emits event)
        engine._update_state("Test Device", "192.168.1.1", "ping", True, 5.0, None)
        engine._event_queue.get_nowait()  # consume first event

        # Same state: UP → UP (should not emit)
        engine._update_state("Test Device", "192.168.1.1", "ping", True, 6.0, None)
        with pytest.raises(queue.Empty):
            engine._event_queue.get_nowait()


# ---------------------------------------------------------------------------
# _run_checks integration
# ---------------------------------------------------------------------------

class TestRunChecks:
    def test_ping_check_updates_state(self):
        engine = _make_engine()
        ok_result = PingResult(host="192.168.1.1", alive=True, rtt_ms=5.0, packet_loss=0.0)

        with patch("netwatch.monitor.engine.ping", return_value=ok_result):
            engine._run_checks(_BASE_CONFIG["devices"][0])

        statuses = engine.get_statuses()
        assert statuses[0].state == DeviceState.UP

    def test_ping_failure_marks_down(self):
        engine = _make_engine()
        fail_result = PingResult(
            host="192.168.1.1",
            alive=False,
            rtt_ms=None,
            packet_loss=1.0,
            error="timeout",
        )

        with patch("netwatch.monitor.engine.ping", return_value=fail_result):
            engine._run_checks(_BASE_CONFIG["devices"][0])

        statuses = engine.get_statuses()
        assert statuses[0].state == DeviceState.DOWN

    def test_multi_check_all_pass(self):
        engine = _make_engine(config=_MULTI_CHECK_CONFIG)
        ping_ok = PingResult(host="10.0.0.1", alive=True, rtt_ms=2.0, packet_loss=0.0)
        port_ok = PortResult(host="10.0.0.1", port=80, status="open", rtt_ms=1.0)
        http_ok = HttpResult(
            url="http://10.0.0.1/health", status_code=200, healthy=True, rtt_ms=3.0
        )

        with (
            patch("netwatch.monitor.engine.ping", return_value=ping_ok),
            patch("netwatch.monitor.engine.check_ports", return_value=[port_ok]),
            patch("netwatch.monitor.engine.check_http", return_value=http_ok),
        ):
            engine._run_checks(_MULTI_CHECK_CONFIG["devices"][0])

        statuses = engine.get_statuses()
        assert statuses[0].state == DeviceState.UP

    def test_port_failure_marks_down(self):
        engine = _make_engine(config=_MULTI_CHECK_CONFIG)
        ping_ok = PingResult(host="10.0.0.1", alive=True, rtt_ms=2.0, packet_loss=0.0)
        port_fail = PortResult(host="10.0.0.1", port=80, status="closed")

        with (
            patch("netwatch.monitor.engine.ping", return_value=ping_ok),
            patch("netwatch.monitor.engine.check_ports", return_value=[port_fail]),
        ):
            engine._run_checks(_MULTI_CHECK_CONFIG["devices"][0])

        statuses = engine.get_statuses()
        assert statuses[0].state == DeviceState.DOWN

    def test_checker_exception_marks_down(self):
        engine = _make_engine()
        with patch("netwatch.monitor.engine.ping", side_effect=Exception("dns fail")):
            engine._run_checks(_BASE_CONFIG["devices"][0])

        statuses = engine.get_statuses()
        assert statuses[0].state == DeviceState.DOWN


# ---------------------------------------------------------------------------
# Alert cooldown
# ---------------------------------------------------------------------------

class TestAlertCooldown:
    def test_alert_suppressed_within_cooldown(self):
        smtp = {"host": "smtp.test", "port": 587, "username": "u", "recipients": ["r@test.com"]}
        config = {**_BASE_CONFIG, "smtp": smtp}
        config["monitoring"] = {**_BASE_CONFIG["monitoring"], "alert_cooldown": 3600}
        engine = _make_engine(config=config, no_alert=False)

        with engine._lock:
            status = engine._device_statuses["Test Device"]
            status.last_alert_time = time.time()  # Just sent an alert

        event = StateEvent(
            label="Test Device",
            host="192.168.1.1",
            check_type="ping",
            old_state=DeviceState.UP,
            new_state=DeviceState.DOWN,
            failure_reason="timeout",
            consecutive_failures=2,
        )

        with patch("netwatch.monitor.engine.send_alert") as mock_send:
            engine._handle_event(event)

        mock_send.assert_not_called()

    def test_recovery_always_sends_alert(self):
        smtp = {"host": "smtp.test", "port": 587, "username": "u", "recipients": ["r@test.com"]}
        config = {**_BASE_CONFIG, "smtp": smtp}
        engine = _make_engine(config=config, no_alert=False)

        with engine._lock:
            status = engine._device_statuses["Test Device"]
            status.last_alert_time = time.time()  # simulates recent DOWN alert

        event = StateEvent(
            label="Test Device",
            host="192.168.1.1",
            check_type="ping",
            old_state=DeviceState.DOWN,
            new_state=DeviceState.UP,
            failure_reason=None,
            consecutive_failures=0,
        )

        with patch("netwatch.monitor.engine.send_alert") as mock_send:
            engine._handle_event(event)

        mock_send.assert_called_once()
