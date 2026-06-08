"""
Unit tests for netwatch.checker.http

All urllib calls are mocked.
"""

import urllib.error
import urllib.request
from unittest.mock import MagicMock, patch

import pytest

from netwatch.checker.http import HttpResult, check_http


def _make_response(status_code: int) -> MagicMock:
    """Create a mock urllib response object."""
    mock_resp = MagicMock()
    mock_resp.status = status_code
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


class TestCheckHttp:
    def test_invalid_url_raises(self):
        with pytest.raises(ValueError, match="must start with"):
            check_http("ftp://example.com")

    def test_invalid_timeout_raises(self):
        with pytest.raises(ValueError, match="timeout must be"):
            check_http("http://example.com", timeout=0)

    def test_200_is_healthy(self):
        mock_resp = _make_response(200)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = check_http("http://example.com")

        assert result.healthy is True
        assert result.status_code == 200
        assert result.rtt_ms is not None
        assert result.error is None

    def test_404_is_unhealthy_by_default(self):
        exc = urllib.error.HTTPError(
            url="http://example.com",
            code=404,
            msg="Not Found",
            hdrs=None,  # type: ignore[arg-type]
            fp=None,
        )
        with patch("urllib.request.urlopen", side_effect=exc):
            result = check_http("http://example.com")

        assert result.healthy is False
        assert result.status_code == 404

    def test_404_is_healthy_with_custom_codes(self):
        exc = urllib.error.HTTPError(
            url="http://example.com",
            code=404,
            msg="Not Found",
            hdrs=None,  # type: ignore[arg-type]
            fp=None,
        )
        with patch("urllib.request.urlopen", side_effect=exc):
            result = check_http("http://example.com", healthy_codes=[200, 404])

        assert result.healthy is True

    def test_connection_error(self):
        url_err = urllib.error.URLError("connection refused")
        with patch("urllib.request.urlopen", side_effect=url_err):
            result = check_http("http://10.0.0.1")

        assert result.healthy is False
        assert result.status_code is None
        assert result.error is not None

    def test_os_error(self):
        with patch("urllib.request.urlopen", side_effect=OSError("network down")):
            result = check_http("http://10.0.0.1")

        assert result.healthy is False
        assert result.error is not None

    def test_https_verify_ssl_false(self):
        """Passing verify_ssl=False should build an unverified SSL context."""
        mock_resp = _make_response(200)
        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_open:
            result = check_http(
                "https://self-signed.example.com",
                verify_ssl=False,
            )

        assert result.healthy is True
        # urlopen should have been called with a context kwarg
        call_kwargs = mock_open.call_args.kwargs
        assert "context" in call_kwargs

    def test_https_default_ssl_no_context(self):
        """https:// with verify_ssl=True should not pass an explicit context."""
        mock_resp = _make_response(200)
        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_open:
            check_http("https://example.com", verify_ssl=True)

        call_kwargs = mock_open.call_args.kwargs
        assert "context" not in call_kwargs

    def test_returns_http_result_dataclass(self):
        mock_resp = _make_response(200)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = check_http("http://example.com")

        assert isinstance(result, HttpResult)
        assert result.url == "http://example.com"

    def test_rtt_is_positive(self):
        mock_resp = _make_response(200)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = check_http("http://example.com")

        assert result.rtt_ms >= 0

    def test_500_is_unhealthy(self):
        exc = urllib.error.HTTPError(
            url="http://example.com",
            code=500,
            msg="Internal Server Error",
            hdrs=None,  # type: ignore[arg-type]
            fp=None,
        )
        with patch("urllib.request.urlopen", side_effect=exc):
            result = check_http("http://example.com")

        assert result.healthy is False
        assert result.status_code == 500
