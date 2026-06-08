"""
ICMP ping checker for NetWatch.

Strategy:
1. Attempt raw ICMP socket (requires root / CAP_NET_RAW on Linux).
2. On EPERM / EACCES fall back to the OS ``ping`` binary transparently.

Returns a PingResult dataclass with RTT, packet loss, and liveness.
"""

import os
import platform
import re
import socket
import struct
import subprocess
import time
from dataclasses import dataclass, field
from typing import Optional

from netwatch.utils.config import HOSTNAME_RE
from netwatch.utils.logger import get_logger

log = get_logger(__name__)

# ICMP constants
_ICMP_ECHO_REQUEST = 8
_ICMP_CODE = 0


@dataclass
class PingResult:
    """Result of a ping probe."""

    host: str
    alive: bool
    rtt_ms: Optional[float]
    packet_loss: float  # 0.0 – 1.0
    error: Optional[str] = field(default=None)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _checksum(data: bytes) -> int:
    """Compute the ICMP checksum."""
    s = 0
    n = len(data) % 2
    for i in range(0, len(data) - n, 2):
        s += (data[i]) + ((data[i + 1]) << 8)
    if n:
        s += data[-1]
    while s >> 16:
        s = (s & 0xFFFF) + (s >> 16)
    return ~s & 0xFFFF


def _build_packet(sequence: int) -> bytes:
    """Build an ICMP Echo Request packet."""
    pid = os.getpid() & 0xFFFF
    header = struct.pack("bbHHh", _ICMP_ECHO_REQUEST, _ICMP_CODE, 0, pid, sequence)
    data = b"netwatch" * 2
    chk = _checksum(header + data)
    header = struct.pack("bbHHh", _ICMP_ECHO_REQUEST, _ICMP_CODE, chk, pid, sequence)
    return header + data


def _raw_ping_once(host: str, timeout: int, sequence: int) -> Optional[float]:
    """
    Send a single ICMP Echo Request via a raw socket.

    Returns:
        RTT in milliseconds, or None on failure / timeout.

    Raises:
        PermissionError: if the process lacks CAP_NET_RAW.
    """
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP)
    except OSError as exc:
        if exc.errno in (1, 13):  # EPERM / EACCES
            raise PermissionError("Raw socket requires elevated privileges.") from exc
        raise

    sock.settimeout(timeout)
    packet = _build_packet(sequence)
    try:
        dest = socket.gethostbyname(host)
        sock.sendto(packet, (dest, 1))
        start = time.perf_counter()
        while True:
            try:
                raw, _ = sock.recvfrom(1024)
            except socket.timeout:
                return None
            elapsed = (time.perf_counter() - start) * 1000.0
            # IP header is 20 bytes; ICMP starts at offset 20
            icmp_header = raw[20:28]
            icmp_type, _, _, recv_id, _ = struct.unpack("bbHHh", icmp_header)
            our_pid = os.getpid() & 0xFFFF
            if icmp_type == 0 and recv_id == our_pid:
                return round(elapsed, 3)
    except socket.gaierror as exc:
        raise OSError(f"DNS resolution failed for '{host}': {exc}") from exc
    finally:
        sock.close()


def _ping_via_binary(
    host: str, count: int, timeout: int
) -> PingResult:
    """
    Fall back to the OS ``ping`` binary.

    Parses RTT and packet-loss from stdout on both Linux and macOS/BSD.
    """
    system = platform.system().lower()
    if system == "windows":
        cmd = ["ping", "-n", str(count), "-w", str(timeout * 1000), host]
    else:
        cmd = ["ping", "-c", str(count), "-W", str(timeout), host]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout * count + 5,
        )
    except FileNotFoundError:
        return PingResult(
            host=host,
            alive=False,
            rtt_ms=None,
            packet_loss=1.0,
            error="ping binary not found on PATH",
        )
    except subprocess.TimeoutExpired:
        return PingResult(
            host=host,
            alive=False,
            rtt_ms=None,
            packet_loss=1.0,
            error="ping binary timed out",
        )

    output = result.stdout + result.stderr

    # Parse packet loss  (e.g. "25% packet loss" or "25.0% packet loss")
    loss_match = re.search(r"(\d+(?:\.\d+)?)%\s+packet loss", output)
    packet_loss = float(loss_match.group(1)) / 100.0 if loss_match else 1.0

    # Parse RTT  (e.g. "rtt min/avg/max/mdev = 0.123/0.456/0.789/0.100 ms")
    rtt_match = re.search(
        r"(?:rtt|round-trip)[^\d]*([\d.]+)/([\d.]+)/([\d.]+)", output
    )
    rtt_ms: Optional[float] = None
    if rtt_match:
        rtt_ms = float(rtt_match.group(2))  # avg

    alive = packet_loss < 1.0

    error: Optional[str] = None
    if not alive:
        error = f"ping binary reported 100% packet loss (rc={result.returncode})"

    return PingResult(
        host=host,
        alive=alive,
        rtt_ms=rtt_ms,
        packet_loss=packet_loss,
        error=error,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def ping(
    host: str,
    count: int = 4,
    timeout: int = 2,
) -> PingResult:
    """
    Ping *host* using ICMP Echo.

    Tries a raw socket first; falls back to the OS ping binary on EPERM.

    Args:
        host:    Hostname or IP address to ping.
        count:   Number of echo requests to send.
        timeout: Per-packet timeout in seconds.

    Returns:
        PingResult dataclass.

    Raises:
        ValueError: if host fails hostname validation.
    """
    if not HOSTNAME_RE.match(host):
        raise ValueError(f"Invalid hostname or IP address: '{host}'")

    if count < 1:
        raise ValueError(f"count must be >= 1, got {count}")
    if timeout < 1:
        raise ValueError(f"timeout must be >= 1, got {timeout}")

    # Validate that host resolves before spending time on probes
    log.debug("Pinging host='%s' count=%d timeout=%d", host, count, timeout)

    # Try raw ICMP socket — probe once first to test permissions.
    # If that raises PermissionError, fall back to the OS binary entirely.
    try:
        _raw_ping_once(host, timeout, 0)
    except PermissionError:
        log.debug(
            "Raw socket unavailable for '%s', falling back to ping binary.", host
        )
        return _ping_via_binary(host, count, timeout)
    except OSError:
        pass  # DNS errors etc. — handle inside the loop below

    # Permission check passed; run the full count using raw sockets.
    sent = 0
    received = 0
    rtts: list = []
    last_error: Optional[str] = None

    # seq 0 was already consumed by the permission probe above;
    # run all 'count' packets starting from seq 0 for consistent statistics.
    for seq in range(count):
        sent += 1
        try:
            rtt = _raw_ping_once(host, timeout, seq)
            if rtt is not None:
                received += 1
                rtts.append(rtt)
            else:
                last_error = "Request timed out"
        except OSError as exc:
            last_error = str(exc)

    packet_loss = (sent - received) / sent if sent > 0 else 1.0
    avg_rtt = round(sum(rtts) / len(rtts), 3) if rtts else None
    alive = received > 0

    return PingResult(
        host=host,
        alive=alive,
        rtt_ms=avg_rtt,
        packet_loss=packet_loss,
        error=last_error if not alive else None,
    )
