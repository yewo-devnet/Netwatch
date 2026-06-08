"""
TCP port checker for NetWatch.

Uses socket.create_connection() with a user-defined timeout.
Returns one PortResult per port.
"""

import socket
import time
from dataclasses import dataclass, field
from typing import List, Literal, Optional

from netwatch.utils.config import HOSTNAME_RE
from netwatch.utils.logger import get_logger

log = get_logger(__name__)


@dataclass
class PortResult:
    """Result of a single TCP port probe."""

    host: str
    port: int
    status: Literal["open", "closed", "timeout"]
    rtt_ms: Optional[float] = field(default=None)


def check_port(
    host: str,
    port: int,
    timeout: int = 3,
) -> PortResult:
    """
    Check whether a single TCP *port* on *host* is open.

    Args:
        host:    Hostname or IP address.
        port:    TCP port number (1–65535).
        timeout: Connection timeout in seconds.

    Returns:
        PortResult for this port.

    Raises:
        ValueError: if host or port is invalid.
    """
    if not HOSTNAME_RE.match(host):
        raise ValueError(f"Invalid hostname or IP address: '{host}'")
    if not (1 <= port <= 65535):
        raise ValueError(f"Port must be in range 1–65535, got {port}")
    if timeout < 1:
        raise ValueError(f"timeout must be >= 1, got {timeout}")

    log.debug("Checking TCP port host='%s' port=%d timeout=%d", host, port, timeout)

    start = time.perf_counter()
    try:
        conn = socket.create_connection((host, port), timeout=timeout)
        elapsed = (time.perf_counter() - start) * 1000.0
        conn.close()
        log.debug("Port %d on '%s' is OPEN (%.1f ms)", port, host, elapsed)
        return PortResult(
            host=host,
            port=port,
            status="open",
            rtt_ms=round(elapsed, 3),
        )
    except socket.timeout:
        log.debug("Port %d on '%s' TIMED OUT", port, host)
        return PortResult(host=host, port=port, status="timeout", rtt_ms=None)
    except (ConnectionRefusedError, OSError) as exc:
        log.debug("Port %d on '%s' is CLOSED: %s", port, host, exc)
        return PortResult(host=host, port=port, status="closed", rtt_ms=None)


def check_ports(
    host: str,
    ports: List[int],
    timeout: int = 3,
) -> List[PortResult]:
    """
    Check multiple TCP ports on *host*.

    Args:
        host:    Hostname or IP address.
        ports:   List of TCP port numbers.
        timeout: Per-connection timeout in seconds.

    Returns:
        List of PortResult, one per port, in the same order as *ports*.

    Raises:
        ValueError: if host is invalid or ports list is empty.
    """
    if not HOSTNAME_RE.match(host):
        raise ValueError(f"Invalid hostname or IP address: '{host}'")
    if not ports:
        raise ValueError("ports list must not be empty")

    results: List[PortResult] = []
    for port in ports:
        results.append(check_port(host, port, timeout=timeout))
    return results
