"""
Unit tests for netwatch.checker.port

All socket calls are mocked.
"""

import socket
from unittest.mock import MagicMock, patch

import pytest

from netwatch.checker.port import PortResult, check_port, check_ports


class TestCheckPort:
    def test_invalid_host_raises(self):
        with pytest.raises(ValueError, match="Invalid hostname"):
            check_port("bad host!!!", 80)

    def test_invalid_port_low(self):
        with pytest.raises(ValueError, match="Port must be"):
            check_port("8.8.8.8", 0)

    def test_invalid_port_high(self):
        with pytest.raises(ValueError, match="Port must be"):
            check_port("8.8.8.8", 65536)

    def test_invalid_timeout(self):
        with pytest.raises(ValueError, match="timeout must be"):
            check_port("8.8.8.8", 80, timeout=0)

    def test_open_port(self):
        mock_conn = MagicMock()
        with patch("socket.create_connection", return_value=mock_conn):
            result = check_port("8.8.8.8", 53)

        assert result.status == "open"
        assert result.rtt_ms is not None
        assert result.rtt_ms >= 0
        mock_conn.close.assert_called_once()

    def test_connection_refused(self):
        with patch(
            "socket.create_connection",
            side_effect=ConnectionRefusedError("refused"),
        ):
            result = check_port("8.8.8.8", 9999)

        assert result.status == "closed"
        assert result.rtt_ms is None

    def test_timeout(self):
        with patch(
            "socket.create_connection",
            side_effect=socket.timeout("timed out"),
        ):
            result = check_port("8.8.8.8", 9999)

        assert result.status == "timeout"
        assert result.rtt_ms is None

    def test_os_error(self):
        with patch(
            "socket.create_connection",
            side_effect=OSError("network unreachable"),
        ):
            result = check_port("192.0.2.1", 80)

        assert result.status == "closed"

    def test_returns_port_result_dataclass(self):
        mock_conn = MagicMock()
        with patch("socket.create_connection", return_value=mock_conn):
            result = check_port("8.8.8.8", 80)

        assert isinstance(result, PortResult)
        assert result.host == "8.8.8.8"
        assert result.port == 80


class TestCheckPorts:
    def test_empty_ports_raises(self):
        with pytest.raises(ValueError, match="must not be empty"):
            check_ports("8.8.8.8", [])

    def test_invalid_host_raises(self):
        with pytest.raises(ValueError, match="Invalid hostname"):
            check_ports("!!!", [80])

    def test_multiple_ports(self):
        mock_conn = MagicMock()

        def side_effect(addr, timeout):
            host, port = addr
            if port == 80:
                return mock_conn
            raise ConnectionRefusedError("refused")

        with patch("socket.create_connection", side_effect=side_effect):
            results = check_ports("8.8.8.8", [80, 9999])

        assert len(results) == 2
        assert results[0].status == "open"
        assert results[1].status == "closed"

    def test_order_preserved(self):
        mock_conn = MagicMock()
        with patch("socket.create_connection", return_value=mock_conn):
            results = check_ports("8.8.8.8", [443, 80, 22])

        ports = [r.port for r in results]
        assert ports == [443, 80, 22]
