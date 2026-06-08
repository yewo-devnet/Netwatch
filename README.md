# NetWatch

A lightweight, real-time network monitoring CLI tool written in Python.

NetWatch can ping hosts, probe TCP ports, and perform HTTP/HTTPS health checks — all from a single command. Run it once for a quick check or continuously with `monitor` to watch your infrastructure and get email alerts on state changes.

---

## Features

- **ICMP ping** — raw socket with transparent fallback to the OS `ping` binary
- **TCP port check** — open / closed / timeout per port
- **HTTP/HTTPS probe** — stdlib `urllib` only; configurable healthy status codes; SSL bypass option
- **Continuous monitor** — one thread per device, live status table, SIGINT/SIGTERM clean shutdown
- **Email alerts** — STARTTLS (587) or implicit TLS (465), exponential back-off retry, 300 s cooldown
- **YAML config** — `${ENV_VAR}` placeholder expansion, full validation with descriptive errors
- **Zero heavy runtime deps** — only PyYAML, tabulate, jsonschema

---

## Installation

```bash
pip install -r requirements.txt
# optional: install the package so `netwatch` is on PATH
pip install -e .
```

Python 3.9+ required.

---

## Quick Start

```bash
# Ping a host
python -m netwatch ping 8.8.8.8

# Check specific TCP ports
python -m netwatch port 8.8.8.8 -p 53,80,443

# HTTP health probe
python -m netwatch http https://example.com

# Validate your config
NETWATCH_SMTP_PASSWORD=secret python -m netwatch config validate netwatch.yaml

# Start the live monitor
NETWATCH_SMTP_PASSWORD=secret python -m netwatch monitor -c netwatch.yaml
```

---

## CLI Reference

```
netwatch ping   <host>  [-n COUNT] [-t TIMEOUT] [-v]
netwatch port   <host>  [-p PORTS] [-t TIMEOUT]
netwatch http   <url>   [-t TIMEOUT] [--no-verify] [--codes CODES]
netwatch monitor        -c CONFIG  [-i INTERVAL] [--no-alert] [-v]
netwatch status
netwatch config validate <file>
```

Run any sub-command with `--help` for full details.

---

## Configuration

Copy `netwatch.yaml` and edit it for your environment. Set the SMTP password in the environment — never in the file:

```bash
export NETWATCH_SMTP_PASSWORD=your_password
```

```yaml
smtp:
  host: smtp.example.com
  port: 587                        # 587=STARTTLS, 465=implicit TLS
  username: alerts@example.com
  password: ${NETWATCH_SMTP_PASSWORD}
  recipients:
    - admin@example.com

monitoring:
  interval: 60          # seconds between polls
  alert_cooldown: 300   # seconds before re-alerting on same DOWN device
  log_file: netwatch.log
  log_level: INFO

devices:
  - label: Core Router
    host: 192.168.1.1
    checks: [ping]

  - label: Web Server
    host: 10.0.0.50
    checks: [ping, port, http]
    ports: [22, 80, 443]
    url: https://10.0.0.50/health
```

---

## Development

```bash
pip install -r requirements-dev.txt

# Run tests
pytest --tb=short -q

# Coverage report
pytest --cov=netwatch --cov-report=term-missing

# Lint
flake8 netwatch/ tests/
```

---

## Project Structure

```
netwatch/
├── __init__.py
├── __main__.py          # CLI entry point (argparse)
├── checker/
│   ├── ping.py          # ICMP ping with raw socket / binary fallback
│   ├── port.py          # TCP port checker
│   └── http.py          # HTTP/HTTPS health probe (urllib only)
├── monitor/
│   └── engine.py        # Threading engine, state machine, event queue
├── alerts/
│   └── email.py         # SMTP dispatcher with TLS and retry
└── utils/
    ├── config.py         # YAML loader, env-var expansion, validator
    └── logger.py         # Rotating file + colour console logger
tests/
├── test_ping.py
├── test_port.py
├── test_http.py
├── test_engine.py
├── test_email.py
├── test_config.py
└── test_logger.py
```

---

## Security

- SMTP password is read exclusively from `NETWATCH_SMTP_PASSWORD` at send time.
- It is never stored as an attribute, never passed through tracebacks, and never written to any log handler.
- All network calls respect user-defined timeouts — no blocking call without a timeout argument.
- Hostnames and IPs are validated against a regex before any socket call.

---

## License

MIT
