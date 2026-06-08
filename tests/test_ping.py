"""
Unit tests for netwatch.checker.ping

All network calls are mocked via unittest.mock.
"""

import socket
import struct
from unittest.mock import MagicMock, patch

import pytest

from netwatch.checker.ping import (
    PingResult,
    _build_packet,
    _checksum,
    _ping_via_binary,
    _raw_ping_once,
    ping,
)


# ---------------------------------------------------------------------------
# _checksum
# ---------------------------------------------------------------------------

class TestChecksum:
    def test_returns_int(self):
        data = b"\x08\x00\x00\x00\x00\x01\x00\x01"
        result = _checksum(data)
        assert isinstance(result, int)

    def test_even_length(self):
        data = b"\x00\x01\x00\x02"
        assert isinstance(_checksum(data), int)

    def test_odd_length(self):
        data = b"\x00\x01\x02"
        assert isinstance(_checksum(data), int)


# ---------------------------------------------------------------------------
# _build_packet
# ---------------------------------------------------------------------------

class TestBuildPacket:
    def test_returns_bytes(self):
        pkt = _build_packet(1)
        assert isinstance(pkt, bytes)

    def test_minimum_length(self):
        pkt = _build_packet(1)
        # 8-byte ICMP header + 16-byte payload
        assert len(pkt) >= 8

    def test_type_field(self):
        pkt = _build_packet(0)
        # The first field (ICMP type) in the raw packet should be 8 (echo request)
        icmp_type = struct.unpack("B", pkt[0:1])[0]
        assert icmp_type == 8


# ---------------------------------------------------------------------------
# _raw_ping_once
# ---------------------------------------------------------------------------

class TestRawPingOnce:
    def test_permission_error_on_eperm(self):
        """EPERM while creating a raw socket raises PermissionError."""
        import errno as _errno
        err = OSError(_errno.EPERM, "Operation not permitted")
        with patch("socket.socket", side_effect=err):
            with pytest.raises(PermissionError):
                _raw_ping_once("8.8.8.8", 1, 0)

    def test_successful_round_trip(self):
        """Simulate a successful ICMP echo reply."""
        import os

        pid = os.getpid() & 0xFFFF
        # Build a fake ICMP echo reply: type=0, code=0, checksum=0, id=pid, seq=0
        fake_reply = b"\x45\x00" + b"\x00" * 18  # fake IP header (20 bytes)
        icmp_reply = struct.pack("bbHHh", 0, 0, 0, pid, 0) + b"\x00" * 8
        fake_packet = fake_reply + icmp_reply

        mock_sock = MagicMock()
        mock_sock.recvfrom.return_value = (fake_packet, ("8.8.8.8", 0))

        with patch("socket.socket", return_value=mock_sock):
            with patch("socket.gethostbyname", return_value="8.8.8.8"):
                result = _raw_ping_once("8.8.8.8", 2, 0)

        assert result is not None
        assert isinstance(result, float)

    def test_timeout_returns_none(self):
        """Socket timeout results in None RTT."""
        mock_sock = MagicMock()
        mock_sock.recvfrom.side_effect = socket.timeout

        with patch("socket.socket", return_value=mock_sock):
            with patch("socket.gethostbyname", return_value="8.8.8.8"):
                result = _raw_ping_once("8.8.8.8", 1, 0)

        assert result is None


# ---------------------------------------------------------------------------
# _ping_via_binary
# ---------------------------------------------------------------------------

class TestPingViaBinary:
    def test_successful_ping(self):
        fake_output = (
            "PING 8.8.8.8: 56 data bytes\n"
            "64 bytes from 8.8.8.8: icmp_seq=0 ttl=56 time=14.2 ms\n"
            "\n"
            "--- 8.8.8.8 ping statistics ---\n"
            "4 packets transmitted, 4 received, 0% packet loss\n"
            "rtt min/avg/max/mdev = 12.0/14.2/16.0/1.5 ms\n"
        )
        mock_result = MagicMock()
        mock_result.stdout = fake_output
        mock_result.stderr = ""
        mock_result.returncode = 0

        with patch("subprocess.run", return_value=mock_result):
            result = _ping_via_binary("8.8.8.8", 4, 2)

        assert result.alive is True
        assert result.packet_loss == 0.0
        assert result.rtt_ms == pytest.approx(14.2, 0.01)

    def test_100_percent_loss(self):
        fake_output = (
            "--- 10.0.0.1 ping statistics ---\n"
            "4 packets transmitted, 0 received, 100% packet loss\n"
        )
        mock_result = MagicMock()
        mock_result.stdout = fake_output
        mock_result.stderr = ""
        mock_result.returncode = 1

        with patch("subprocess.run", return_value=mock_result):
            result = _ping_via_binary("10.0.0.1", 4, 2)

        assert result.alive is False
        assert result.packet_loss == 1.0
        assert result.rtt_ms is None

    def test_binary_not_found(self):
        with patch(
            "subprocess.run",
            side_effect=FileNotFoundError("ping not found"),
        ):
            result = _ping_via_binary("8.8.8.8", 4, 2)

        assert result.alive is False
        assert result.error is not None
        assert "not found" in result.error


# ---------------------------------------------------------------------------
# ping (public API)
# ---------------------------------------------------------------------------

class TestPing:
    def test_invalid_host_raises(self):
        with pytest.raises(ValueError, match="Invalid hostname"):
            ping("not a valid host!!!")

    def test_invalid_count_raises(self):
        with pytest.raises(ValueError, match="count must be"):
            ping("8.8.8.8", count=0)

    def test_invalid_timeout_raises(self):
        with pytest.raises(ValueError, match="timeout must be"):
            ping("8.8.8.8", timeout=0)

    def test_falls_back_to_binary_on_permission_error(self):
        """When raw socket raises PermissionError, fall back to OS ping."""
        fake_result = PingResult(
            host="8.8.8.8", alive=True, rtt_ms=12.5, packet_loss=0.0
        )

        # The PermissionError must be raised *before* the per-packet loop so
        # ping() catches it at the outer try/except and falls back to binary.
        # We simulate this by making the first call to _raw_ping_once raise.
        with patch(
            "netwatch.checker.ping._raw_ping_once",
            side_effect=PermissionError("no CAP_NET_RAW"),
        ):
            with patch(
                "netwatch.checker.ping._ping_via_binary",
                return_value=fake_result,
            ) as mock_bin:
                result = ping("8.8.8.8", count=1)

        mock_bin.assert_called_once()
        assert result.alive is True

    def test_successful_raw_ping(self):
        """All four packets succeed; computes average RTT."""
        with patch(
            "netwatch.checker.ping._raw_ping_once",
            return_value=10.0,
        ):
            result = ping("8.8.8.8", count=4, timeout=2)

        assert result.alive is True
        assert result.rtt_ms == pytest.approx(10.0)
        assert result.packet_loss == 0.0

    def test_partial_packet_loss(self):
        """Two out of four packets time out.
        Note: ping() makes one extra probe call (seq 0) to test permissions,
        so we supply 5 responses for count=4.
        """
        # Probe call + 4 loop calls; 2 succeed, 2 time out
        responses = [10.0, 10.0, None, 12.0, None]
        with patch(
            "netwatch.checker.ping._raw_ping_once",
            side_effect=responses,
        ):
            result = ping("8.8.8.8", count=4)

        assert result.alive is True
        assert result.packet_loss == pytest.approx(0.5)
        assert result.rtt_ms == pytest.approx(11.0)

    def test_all_packets_lost(self):
        with patch(
            "netwatch.checker.ping._raw_ping_once",
            return_value=None,
        ):
            result = ping("8.8.8.8", count=2)

        assert result.alive is False
        assert result.packet_loss == 1.0

    def test_returns_ping_result_dataclass(self):
        with patch("netwatch.checker.ping._raw_ping_once", return_value=5.0):
            result = ping("8.8.8.8", count=1)
        assert isinstance(result, PingResult)
