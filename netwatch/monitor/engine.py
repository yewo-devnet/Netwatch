"""
NetWatch monitoring engine.

Architecture:
- One threading.Thread per device runs an independent poll loop.
- Each thread evaluates all configured checks (ping / port / http).
- State machine: UNKNOWN → UP / DOWN; transitions emit events to a queue.
- Alert cooldown (default 300 s) suppresses repeated DOWN emails.
- Graceful shutdown on SIGINT / SIGTERM: threads are stopped, logs flushed.
- All shared state protected by threading.Lock.
"""

import queue
import signal
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from netwatch.alerts.email import send_alert
from netwatch.checker.http import HttpResult, check_http
from netwatch.checker.ping import PingResult, ping
from netwatch.checker.port import PortResult, check_ports
from netwatch.utils.logger import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# State definitions
# ---------------------------------------------------------------------------

class DeviceState(str, Enum):
    UNKNOWN = "UNKNOWN"
    UP = "UP"
    DOWN = "DOWN"


@dataclass
class DeviceStatus:
    """Live status record for a single monitored device."""

    label: str
    host: str
    checks: List[str]
    state: DeviceState = DeviceState.UNKNOWN
    last_checked: Optional[float] = None       # epoch seconds
    last_rtt_ms: Optional[float] = None
    consecutive_failures: int = 0
    last_alert_time: Optional[float] = None
    last_error: Optional[str] = None


@dataclass
class StateEvent:
    """A state-transition event emitted to the event queue."""

    label: str
    host: str
    check_type: str
    old_state: DeviceState
    new_state: DeviceState
    failure_reason: Optional[str]
    consecutive_failures: int
    timestamp: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class MonitorEngine:
    """
    Coordinates per-device polling threads and dispatches alerts.

    Args:
        config:    Validated config dict (from utils.config.load_config).
        interval:  Poll interval override in seconds (None → use config value).
        no_alert:  If True, email alerts are suppressed.
        on_status: Optional callback(device_statuses) called after each poll
                   cycle – used by the CLI to redraw the status table.
    """

    def __init__(
        self,
        config: Dict[str, Any],
        interval: Optional[int] = None,
        no_alert: bool = False,
        on_status: Optional[Callable[[List[DeviceStatus]], None]] = None,
    ) -> None:
        self._config = config
        monitoring_cfg = config.get("monitoring", {})

        self._interval: int = interval or monitoring_cfg.get("interval", 60)
        self._cooldown: int = monitoring_cfg.get("alert_cooldown", 300)
        self._no_alert = no_alert
        self._on_status = on_status

        self._smtp_cfg: Optional[Dict[str, Any]] = config.get("smtp")

        # Shared state – protected by _lock
        self._lock = threading.Lock()
        self._device_statuses: Dict[str, DeviceStatus] = {}
        self._stop_event = threading.Event()

        # Event queue for state transitions
        self._event_queue: queue.Queue = queue.Queue()

        # Worker threads (one per device)
        self._threads: List[threading.Thread] = []

        # Initialise DeviceStatus for each device
        for device in config.get("devices", []):
            label = device["label"]
            self._device_statuses[label] = DeviceStatus(
                label=label,
                host=device["host"],
                checks=device.get("checks", []),
            )

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start all polling threads and the event-processing loop."""
        log.info(
            "NetWatch engine starting – %d device(s), interval=%ds, cooldown=%ds",
            len(self._device_statuses),
            self._interval,
            self._cooldown,
        )

        # Register signal handlers for clean shutdown
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

        # Start one thread per device
        for device in self._config.get("devices", []):
            t = threading.Thread(
                target=self._poll_loop,
                args=(device,),
                name=f"netwatch-{device['label']}",
                daemon=True,
            )
            t.start()
            self._threads.append(t)

        # Event-processing loop (runs on the main thread)
        self._event_loop()

    def stop(self) -> None:
        """Signal all threads to stop and wait for them to finish."""
        log.info("NetWatch engine stopping…")
        self._stop_event.set()
        for t in self._threads:
            t.join(timeout=self._interval + 5)
        log.info("All polling threads stopped.")

    def get_statuses(self) -> List[DeviceStatus]:
        """Return a snapshot of all device statuses (thread-safe)."""
        with self._lock:
            return list(self._device_statuses.values())

    # ------------------------------------------------------------------
    # Signal handling
    # ------------------------------------------------------------------

    def _handle_signal(self, signum: int, _frame: Any) -> None:
        log.info("Received signal %d – shutting down…", signum)
        self._stop_event.set()

    # ------------------------------------------------------------------
    # Poll loop (per-device thread)
    # ------------------------------------------------------------------

    def _poll_loop(self, device: Dict[str, Any]) -> None:
        """Polling loop executed inside each device thread."""
        label = device["label"]
        host = device["host"]

        log.debug("Poll loop started for device '%s' (%s)", label, host)

        while not self._stop_event.is_set():
            poll_start = time.time()
            self._run_checks(device)
            elapsed = time.time() - poll_start
            sleep_time = max(0.0, self._interval - elapsed)

            # Sleep in small increments so we can respond to stop_event quickly
            deadline = time.time() + sleep_time
            while time.time() < deadline and not self._stop_event.is_set():
                time.sleep(min(1.0, deadline - time.time()))

        log.debug("Poll loop stopped for device '%s'", label)

    def _run_checks(self, device: Dict[str, Any]) -> None:
        """Run all checks for a single device and update shared state."""
        label = device["label"]
        host = device["host"]
        checks = device.get("checks", [])

        overall_alive = True
        failure_reason: Optional[str] = None
        check_type_failed = "unknown"
        rtt_ms: Optional[float] = None

        # --- ping check ---
        if "ping" in checks:
            try:
                result: PingResult = ping(host)
                if result.rtt_ms is not None:
                    rtt_ms = result.rtt_ms
                if not result.alive:
                    overall_alive = False
                    failure_reason = result.error or "ICMP ping failed"
                    check_type_failed = "ping"
            except Exception as exc:
                overall_alive = False
                failure_reason = str(exc)
                check_type_failed = "ping"
                log.warning("Ping check error for '%s': %s", label, exc)

        # --- port check ---
        if "port" in checks and overall_alive:
            ports = device.get("ports", [])
            try:
                port_results: List[PortResult] = check_ports(host, ports)
                for pr in port_results:
                    if pr.status != "open":
                        overall_alive = False
                        failure_reason = (
                            f"Port {pr.port} is {pr.status}"
                        )
                        check_type_failed = "port"
                        break
            except Exception as exc:
                overall_alive = False
                failure_reason = str(exc)
                check_type_failed = "port"
                log.warning("Port check error for '%s': %s", label, exc)

        # --- http check ---
        if "http" in checks and overall_alive:
            url = device.get("url", f"http://{host}")
            try:
                http_result: HttpResult = check_http(url)
                if http_result.rtt_ms is not None and rtt_ms is None:
                    rtt_ms = http_result.rtt_ms
                if not http_result.healthy:
                    overall_alive = False
                    failure_reason = (
                        http_result.error
                        or f"HTTP {http_result.status_code}"
                    )
                    check_type_failed = "http"
            except Exception as exc:
                overall_alive = False
                failure_reason = str(exc)
                check_type_failed = "http"
                log.warning("HTTP check error for '%s': %s", label, exc)

        # --- update shared state ---
        self._update_state(
            label=label,
            host=host,
            check_type=check_type_failed if not overall_alive else checks[0] if checks else "ping",
            alive=overall_alive,
            rtt_ms=rtt_ms,
            failure_reason=failure_reason,
        )

    def _update_state(
        self,
        label: str,
        host: str,
        check_type: str,
        alive: bool,
        rtt_ms: Optional[float],
        failure_reason: Optional[str],
    ) -> None:
        """Update device state and emit a transition event if state changed."""
        with self._lock:
            status = self._device_statuses[label]
            old_state = status.state
            new_state = DeviceState.UP if alive else DeviceState.DOWN

            status.last_checked = time.time()
            status.last_rtt_ms = rtt_ms
            status.last_error = failure_reason

            if alive:
                status.consecutive_failures = 0
            else:
                status.consecutive_failures += 1

            state_changed = new_state != old_state or old_state == DeviceState.UNKNOWN
            status.state = new_state

        if state_changed:
            event = StateEvent(
                label=label,
                host=host,
                check_type=check_type,
                old_state=old_state,
                new_state=new_state,
                failure_reason=failure_reason,
                consecutive_failures=status.consecutive_failures,
            )
            self._event_queue.put(event)
            log.info(
                "State transition for '%s': %s → %s",
                label,
                old_state.value,
                new_state.value,
            )

    # ------------------------------------------------------------------
    # Event loop (main thread)
    # ------------------------------------------------------------------

    def _event_loop(self) -> None:
        """
        Process state-transition events from the queue.

        Runs on the main thread until stop_event is set.
        """
        while not self._stop_event.is_set():
            try:
                event = self._event_queue.get(timeout=1.0)
                self._handle_event(event)
                self._event_queue.task_done()
            except queue.Empty:
                pass

            # Call the status callback (e.g. to redraw the table)
            if self._on_status:
                try:
                    self._on_status(self.get_statuses())
                except Exception as exc:
                    log.debug("Status callback error: %s", exc)

        # Drain remaining events after stop
        while True:
            try:
                event = self._event_queue.get_nowait()
                self._handle_event(event)
                self._event_queue.task_done()
            except queue.Empty:
                break

        self.stop()

    def _handle_event(self, event: StateEvent) -> None:
        """Handle a state-transition event: log it and dispatch alerts."""
        if event.new_state == DeviceState.DOWN:
            log.error(
                "ALERT: '%s' (%s) is DOWN via %s – %s (failures: %d)",
                event.label,
                event.host,
                event.check_type,
                event.failure_reason or "unknown",
                event.consecutive_failures,
            )
        elif event.new_state == DeviceState.UP:
            log.info(
                "RECOVERED: '%s' (%s) is UP", event.label, event.host
            )

        if self._no_alert or not self._smtp_cfg:
            return

        # Check cooldown
        with self._lock:
            status = self._device_statuses.get(event.label)
            if status is None:
                return
            now = time.time()
            # Only apply cooldown to DOWN alerts; always send RECOVERED
            if event.new_state == DeviceState.DOWN:
                if (
                    status.last_alert_time is not None
                    and (now - status.last_alert_time) < self._cooldown
                ):
                    log.debug(
                        "Alert cooldown active for '%s'; suppressing DOWN email.",
                        event.label,
                    )
                    return
            status.last_alert_time = now

        # Send email (outside the lock to avoid blocking other threads)
        is_down = event.new_state == DeviceState.DOWN
        send_alert(
            smtp_cfg=self._smtp_cfg,
            label=event.label,
            host=event.host,
            check_type=event.check_type,
            is_down=is_down,
            failure_reason=event.failure_reason,
            consecutive_failures=event.consecutive_failures,
        )
