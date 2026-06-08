"""
NetWatch CLI entry point.

Sub-commands:
    ping   <host>          – ICMP ping with RTT and packet-loss
    port   <host>          – TCP port check
    http   <url>           – HTTP/HTTPS health probe
    monitor                – Start the polling engine
    status                 – Show current device statuses (read-only snapshot)
    config validate <file> – Validate a YAML config file

Usage examples:
    python -m netwatch ping 8.8.8.8
    python -m netwatch port 8.8.8.8 -p 53,80
    python -m netwatch http https://example.com
    python -m netwatch monitor -c netwatch.yaml
    python -m netwatch config validate netwatch.yaml
"""

import argparse
import sys
import time
from typing import List, Optional

from netwatch.utils.logger import configure_root_logger, get_logger

# Lazy imports keep startup fast and allow --help without heavy imports


def _parse_ports(value: str) -> List[int]:
    """Parse a comma-separated list of ports into a list of ints.

    Example: '22,80,443' -> [22, 80, 443]
    """
    try:
        ports = [int(p.strip()) for p in value.split(",") if p.strip()]
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"Invalid port list: '{value}'. Expected comma-separated integers."
        )
    for p in ports:
        if not (1 <= p <= 65535):
            raise argparse.ArgumentTypeError(
                f"Port {p} is out of valid range 1–65535."
            )
    return ports


def _parse_codes(value: str) -> List[int]:
    """Parse comma-separated HTTP status codes into a list of ints.

    Example: '200,201,204' -> [200, 201, 204]
    """
    try:
        codes = [int(c.strip()) for c in value.split(",") if c.strip()]
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"Invalid status code list: '{value}'."
        )
    return codes


# ---------------------------------------------------------------------------
# Sub-command handlers
# ---------------------------------------------------------------------------

def cmd_ping(args: argparse.Namespace) -> int:
    """Handle: netwatch ping <host>"""
    if args.verbose:
        configure_root_logger("DEBUG")

    from netwatch.checker.ping import ping

    try:
        result = ping(args.host, count=args.count, timeout=args.timeout)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    if result.alive:
        loss_pct = result.packet_loss * 100
        print(
            f"PING {result.host}: alive=True  "
            f"rtt={result.rtt_ms:.3f} ms  "
            f"packet_loss={loss_pct:.0f}%"
        )
    else:
        loss_pct = result.packet_loss * 100
        print(
            f"PING {result.host}: alive=False  "
            f"packet_loss={loss_pct:.0f}%  "
            f"error={result.error}"
        )
    return 0 if result.alive else 1


def cmd_port(args: argparse.Namespace) -> int:
    """Handle: netwatch port <host> -p <ports>"""
    from netwatch.checker.port import check_ports

    try:
        results = check_ports(args.host, args.ports, timeout=args.timeout)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    try:
        from tabulate import tabulate

        rows = [
            [r.host, r.port, r.status.upper(), f"{r.rtt_ms:.1f}" if r.rtt_ms else "—"]
            for r in results
        ]
        print(
            tabulate(
                rows,
                headers=["Host", "Port", "Status", "RTT (ms)"],
                tablefmt="simple",
            )
        )
    except ImportError:
        # Graceful fallback without tabulate
        print(f"{'Host':<20} {'Port':>6}  {'Status':<10}  {'RTT (ms)':>10}")
        print("-" * 52)
        for r in results:
            rtt = f"{r.rtt_ms:.1f}" if r.rtt_ms else "—"
            print(f"{r.host:<20} {r.port:>6}  {r.status.upper():<10}  {rtt:>10}")

    any_closed = any(r.status != "open" for r in results)
    return 1 if any_closed else 0


def cmd_http(args: argparse.Namespace) -> int:
    """Handle: netwatch http <url>"""
    from netwatch.checker.http import check_http

    codes = args.codes if args.codes else None

    try:
        result = check_http(
            args.url,
            timeout=args.timeout,
            healthy_codes=codes,
            verify_ssl=not args.no_verify,
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    rtt = f"{result.rtt_ms:.1f} ms" if result.rtt_ms is not None else "—"
    status_str = str(result.status_code) if result.status_code else "N/A"
    healthy_str = "healthy" if result.healthy else "UNHEALTHY"

    print(
        f"HTTP {result.url}  status={status_str}  rtt={rtt}  [{healthy_str}]"
    )
    if result.error:
        print(f"  Error: {result.error}", file=sys.stderr)

    return 0 if result.healthy else 1


def cmd_monitor(args: argparse.Namespace) -> int:
    """Handle: netwatch monitor -c <config>"""
    from netwatch.monitor.engine import MonitorEngine
    from netwatch.utils.config import load_config_safe

    cfg = load_config_safe(args.config)

    monitoring_cfg = cfg.get("monitoring", {})
    log_level = monitoring_cfg.get("log_level", "INFO")
    if args.verbose:
        log_level = "DEBUG"
    log_file = monitoring_cfg.get("log_file")
    configure_root_logger(log_level, log_file)

    log = get_logger(__name__)
    log.info("Starting NetWatch monitor. Press Ctrl-C to stop.")

    def on_status(statuses):  # type: ignore[no-untyped-def]
        """Reprint a compact status table after each cycle."""
        try:
            from tabulate import tabulate

            rows = []
            for s in statuses:
                checked = (
                    time.strftime("%H:%M:%S", time.localtime(s.last_checked))
                    if s.last_checked
                    else "—"
                )
                rtt = f"{s.last_rtt_ms:.1f}" if s.last_rtt_ms else "—"
                rows.append(
                    [s.label, s.host, s.state.value, rtt, checked, s.consecutive_failures]
                )
            table = tabulate(
                rows,
                headers=["Label", "Host", "State", "RTT(ms)", "Last Check", "Failures"],
                tablefmt="simple",
            )
            # Clear screen and reprint
            print("\033[2J\033[H", end="")
            print("NetWatch – Live Status\n")
            print(table)
            print("\nPress Ctrl-C to stop.")
        except ImportError:
            for s in statuses:
                checked = (
                    time.strftime("%H:%M:%S", time.localtime(s.last_checked))
                    if s.last_checked
                    else "—"
                )
                rtt = f"{s.last_rtt_ms:.1f}" if s.last_rtt_ms else "—"
                print(
                    f"{s.label:<20} {s.host:<18} {s.state.value:<8} "
                    f"rtt={rtt:<8} last={checked} failures={s.consecutive_failures}"
                )

    engine = MonitorEngine(
        config=cfg,
        interval=args.interval,
        no_alert=args.no_alert,
        on_status=on_status,
    )
    engine.start()
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    """Handle: netwatch status  (informational placeholder)"""
    print(
        "The 'status' sub-command displays live device statuses while the monitor\n"
        "is running. Start monitoring with:\n\n"
        "    python -m netwatch monitor -c netwatch.yaml\n"
    )
    return 0


def cmd_config_validate(args: argparse.Namespace) -> int:
    """Handle: netwatch config validate <file>"""
    from netwatch.utils.config import ConfigError, load_config

    try:
        load_config(args.file)
        print(f"✓ Configuration '{args.file}' is valid.")
        return 0
    except ConfigError as exc:
        print(f"✗ Configuration error: {exc}", file=sys.stderr)
        return 1


# ---------------------------------------------------------------------------
# Argument parser construction
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    """Build and return the top-level argument parser."""
    parser = argparse.ArgumentParser(
        prog="netwatch",
        description=(
            "NetWatch – lightweight real-time network monitoring utility.\n\n"
            "Examples:\n"
            "  python -m netwatch ping 8.8.8.8\n"
            "  python -m netwatch port 8.8.8.8 -p 53,80,443\n"
            "  python -m netwatch http https://example.com\n"
            "  python -m netwatch monitor -c netwatch.yaml\n"
            "  python -m netwatch config validate netwatch.yaml"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version",
        action="version",
        version="%(prog)s 1.0.0",
    )

    sub = parser.add_subparsers(dest="command", metavar="<command>")
    sub.required = True

    # ------------------------------------------------------------------
    # ping
    # ------------------------------------------------------------------
    ping_p = sub.add_parser(
        "ping",
        help="Send ICMP echo requests to a host.",
        description=(
            "Send ICMP echo requests to HOST and report RTT and packet loss.\n\n"
            "Example:\n  python -m netwatch ping 8.8.8.8 -n 5 -t 3"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ping_p.add_argument("host", metavar="HOST", help="Hostname or IP address to ping.")
    ping_p.add_argument(
        "-n", "--count",
        type=int,
        default=4,
        metavar="N",
        help="Number of echo requests to send (default: 4).",
    )
    ping_p.add_argument(
        "-t", "--timeout",
        type=int,
        default=2,
        metavar="SECS",
        help="Per-packet timeout in seconds (default: 2).",
    )
    ping_p.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable debug-level output.",
    )
    ping_p.set_defaults(func=cmd_ping)

    # ------------------------------------------------------------------
    # port
    # ------------------------------------------------------------------
    port_p = sub.add_parser(
        "port",
        help="Check TCP port reachability on a host.",
        description=(
            "Test whether TCP ports on HOST are open, closed, or timing out.\n\n"
            "Example:\n  python -m netwatch port 8.8.8.8 -p 53,80,443"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    port_p.add_argument("host", metavar="HOST", help="Hostname or IP address.")
    port_p.add_argument(
        "-p", "--ports",
        type=_parse_ports,
        default=[80],
        metavar="PORTS",
        help="Comma-separated list of TCP ports (default: 80). Example: 22,80,443",
    )
    port_p.add_argument(
        "-t", "--timeout",
        type=int,
        default=3,
        metavar="SECS",
        help="Connection timeout per port in seconds (default: 3).",
    )
    port_p.set_defaults(func=cmd_port)

    # ------------------------------------------------------------------
    # http
    # ------------------------------------------------------------------
    http_p = sub.add_parser(
        "http",
        help="Perform an HTTP/HTTPS health probe.",
        description=(
            "Send an HTTP GET to URL and report status code and response time.\n\n"
            "Example:\n  python -m netwatch http https://example.com --codes 200,201"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    http_p.add_argument("url", metavar="URL", help="Full URL to probe (http:// or https://).")
    http_p.add_argument(
        "-t", "--timeout",
        type=int,
        default=5,
        metavar="SECS",
        help="Request timeout in seconds (default: 5).",
    )
    http_p.add_argument(
        "--no-verify",
        action="store_true",
        help="Disable SSL certificate verification.",
    )
    http_p.add_argument(
        "--codes",
        type=_parse_codes,
        default=None,
        metavar="CODES",
        help=(
            "Comma-separated list of HTTP status codes considered healthy "
            "(default: 200–299). Example: 200,201,204"
        ),
    )
    http_p.set_defaults(func=cmd_http)

    # ------------------------------------------------------------------
    # monitor
    # ------------------------------------------------------------------
    monitor_p = sub.add_parser(
        "monitor",
        help="Start the continuous polling monitor.",
        description=(
            "Start polling all devices defined in the config file.\n"
            "Prints a live status table and sends email alerts on state changes.\n\n"
            "Example:\n  python -m netwatch monitor -c netwatch.yaml -i 30"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    monitor_p.add_argument(
        "-c", "--config",
        required=True,
        metavar="FILE",
        help="Path to the YAML configuration file.",
    )
    monitor_p.add_argument(
        "-i", "--interval",
        type=int,
        default=None,
        metavar="SECS",
        help="Override polling interval in seconds (default: from config).",
    )
    monitor_p.add_argument(
        "--no-alert",
        action="store_true",
        help="Suppress email alerts (useful for testing).",
    )
    monitor_p.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable debug-level output.",
    )
    monitor_p.set_defaults(func=cmd_monitor)

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------
    status_p = sub.add_parser(
        "status",
        help="Show current device status overview.",
        description="Display current device statuses (informational).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    status_p.set_defaults(func=cmd_status)

    # ------------------------------------------------------------------
    # config (sub-parser group)
    # ------------------------------------------------------------------
    config_p = sub.add_parser(
        "config",
        help="Configuration management sub-commands.",
        description="Manage and validate NetWatch configuration files.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    config_sub = config_p.add_subparsers(dest="config_command", metavar="<subcommand>")
    config_sub.required = True

    validate_p = config_sub.add_parser(
        "validate",
        help="Validate a YAML configuration file.",
        description=(
            "Validate a NetWatch YAML config file.\n"
            "Exits with code 0 on success, 1 with a descriptive error on failure.\n\n"
            "Example:\n  python -m netwatch config validate netwatch.yaml"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    validate_p.add_argument(
        "file",
        metavar="FILE",
        help="Path to the YAML configuration file to validate.",
    )
    validate_p.set_defaults(func=cmd_config_validate)

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    """Parse arguments and dispatch to the appropriate sub-command.

    Args:
        argv: Argument list (defaults to sys.argv[1:] when None).

    Returns:
        Exit code: 0 on success, 1 on logical failure, 2 on usage error.
    """
    # Default logger (INFO, console only) – may be reconfigured by sub-commands
    configure_root_logger("INFO")

    parser = build_parser()
    args = parser.parse_args(argv)

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
