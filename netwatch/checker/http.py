"""
HTTP/HTTPS health probe for NetWatch.

Uses urllib.request from the standard library only (no requests).
Supports:
- Custom timeout
- Configurable list of healthy status codes
- Optional SSL certificate verification bypass
"""

import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import List, Optional

from netwatch.utils.logger import get_logger

log = get_logger(__name__)

# Default range of healthy HTTP status codes (200–299)
_DEFAULT_HEALTHY_CODES: List[int] = list(range(200, 300))


@dataclass
class HttpResult:
    """Result of an HTTP/HTTPS health probe."""

    url: str
    status_code: Optional[int]
    healthy: bool
    rtt_ms: Optional[float]
    error: Optional[str] = field(default=None)


def check_http(
    url: str,
    timeout: int = 5,
    healthy_codes: Optional[List[int]] = None,
    verify_ssl: bool = True,
) -> HttpResult:
    """
    Perform an HTTP GET probe against *url*.

    Args:
        url:           Full URL including scheme (http:// or https://).
        timeout:       Request timeout in seconds.
        healthy_codes: List of HTTP status codes considered healthy.
                       Defaults to 200–299.
        verify_ssl:    If False, SSL certificate errors are ignored.

    Returns:
        HttpResult dataclass.

    Raises:
        ValueError: if url does not start with http:// or https://.
    """
    if not url.startswith(("http://", "https://")):
        raise ValueError(
            f"URL must start with 'http://' or 'https://', got: '{url}'"
        )
    if timeout < 1:
        raise ValueError(f"timeout must be >= 1, got {timeout}")

    codes = healthy_codes if healthy_codes is not None else _DEFAULT_HEALTHY_CODES

    if not codes:
        raise ValueError("healthy_codes must not be empty")

    log.debug(
        "HTTP probe url='%s' timeout=%d verify_ssl=%s healthy_codes=%s",
        url,
        timeout,
        verify_ssl,
        codes,
    )

    # Build SSL context
    ssl_context: Optional[ssl.SSLContext] = None
    if url.startswith("https://") and not verify_ssl:
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
        log.debug("SSL certificate verification disabled for '%s'", url)

    start = time.perf_counter()
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "NetWatch/1.0"},
        )
        if ssl_context:
            response = urllib.request.urlopen(
                req, timeout=timeout, context=ssl_context
            )
        else:
            response = urllib.request.urlopen(req, timeout=timeout)

        elapsed = (time.perf_counter() - start) * 1000.0
        status_code: int = response.status
        response.close()

        healthy = status_code in codes
        log.debug(
            "HTTP probe '%s' -> %d (%.1f ms) healthy=%s",
            url,
            status_code,
            elapsed,
            healthy,
        )
        return HttpResult(
            url=url,
            status_code=status_code,
            healthy=healthy,
            rtt_ms=round(elapsed, 3),
            error=None,
        )

    except urllib.error.HTTPError as exc:
        elapsed = (time.perf_counter() - start) * 1000.0
        status_code = exc.code
        healthy = status_code in codes
        log.debug(
            "HTTP probe '%s' -> HTTPError %d (%.1f ms)",
            url,
            status_code,
            elapsed,
        )
        return HttpResult(
            url=url,
            status_code=status_code,
            healthy=healthy,
            rtt_ms=round(elapsed, 3),
            error=str(exc) if not healthy else None,
        )

    except urllib.error.URLError as exc:
        elapsed = (time.perf_counter() - start) * 1000.0
        error_msg = str(exc.reason) if exc.reason else str(exc)
        log.debug("HTTP probe '%s' failed: %s", url, error_msg)
        return HttpResult(
            url=url,
            status_code=None,
            healthy=False,
            rtt_ms=round(elapsed, 3),
            error=error_msg,
        )

    except OSError as exc:
        elapsed = (time.perf_counter() - start) * 1000.0
        log.debug("HTTP probe '%s' OSError: %s", url, exc)
        return HttpResult(
            url=url,
            status_code=None,
            healthy=False,
            rtt_ms=round(elapsed, 3),
            error=str(exc),
        )
