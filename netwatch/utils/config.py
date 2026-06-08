"""
YAML configuration loader and validator for NetWatch.

Features:
- Loads a YAML file and resolves ${ENV_VAR} placeholders in string values.
- Validates required keys and types.
- Raises ConfigError (exits with code 1 at the CLI boundary) on any problem.
- Never logs SMTP password values.
"""

import os
import re
import sys
from typing import Any

import yaml

from netwatch.utils.logger import get_logger

log = get_logger(__name__)

# Regex for ${VAR_NAME} placeholder substitution
_ENV_PLACEHOLDER = re.compile(r"\$\{([^}]+)\}")

# Regex for valid hostname / IPv4 / IPv6 (liberal but safe)
HOSTNAME_RE = re.compile(
    r"^(?:"
    r"(?:[a-zA-Z0-9](?:[a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?"
    r"(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?)*)"  # hostname
    r"|(?:(?:\d{1,3}\.){3}\d{1,3})"                              # IPv4
    r"|(?:[0-9a-fA-F:]{2,39})"                                   # IPv6
    r")$"
)


class ConfigError(Exception):
    """Raised when the configuration is invalid."""


def _expand_env_vars(value: Any) -> Any:
    """Recursively expand ${ENV_VAR} placeholders in string values."""
    if isinstance(value, str):
        def replacer(match: re.Match) -> str:
            var_name = match.group(1)
            env_val = os.environ.get(var_name)
            if env_val is None:
                raise ConfigError(
                    f"Environment variable '{var_name}' referenced in config "
                    f"is not set."
                )
            return env_val

        return _ENV_PLACEHOLDER.sub(replacer, value)
    elif isinstance(value, dict):
        return {k: _expand_env_vars(v) for k, v in value.items()}
    elif isinstance(value, list):
        return [_expand_env_vars(item) for item in value]
    return value


def _require(cfg: dict, *keys: str, section: str = "") -> Any:
    """
    Navigate nested dict keys and raise ConfigError if missing.

    Args:
        cfg:     The (sub-)config dict.
        *keys:   Sequence of keys forming the path.
        section: Human-readable section name for error messages.

    Returns:
        The value at the given path.
    """
    node = cfg
    path = ".".join(keys)
    for key in keys:
        if not isinstance(node, dict) or key not in node:
            prefix = f"{section}." if section else ""
            raise ConfigError(
                f"Required configuration key '{prefix}{path}' is missing."
            )
        node = node[key]
    return node


def _check_type(value: Any, expected: type, key: str) -> None:
    """Raise ConfigError if value is not of the expected type."""
    if not isinstance(value, expected):
        raise ConfigError(
            f"Configuration key '{key}' must be of type "
            f"{expected.__name__}, got {type(value).__name__}."
        )


def validate_config(cfg: dict) -> None:
    """
    Validate the expanded configuration dictionary.

    Raises:
        ConfigError: on any structural or type issue.
    """
    # --- smtp section (optional but validated if present) ---
    if "smtp" in cfg:
        smtp = cfg["smtp"]
        _check_type(smtp, dict, "smtp")
        for key in ("host", "username"):
            val = _require(smtp, key, section="smtp")
            _check_type(val, str, f"smtp.{key}")
        port = _require(smtp, "port", section="smtp")
        _check_type(port, int, "smtp.port")
        if port not in (465, 587):
            raise ConfigError(
                f"smtp.port must be 465 (implicit TLS) or 587 (STARTTLS), "
                f"got {port}."
            )
        recipients = _require(smtp, "recipients", section="smtp")
        _check_type(recipients, list, "smtp.recipients")
        if not recipients:
            raise ConfigError("smtp.recipients must not be empty.")

    # --- monitoring section ---
    monitoring = _require(cfg, "monitoring")
    _check_type(monitoring, dict, "monitoring")
    interval = _require(monitoring, "interval", section="monitoring")
    _check_type(interval, int, "monitoring.interval")
    if interval < 1:
        raise ConfigError("monitoring.interval must be >= 1 second.")

    cooldown = monitoring.get("alert_cooldown", 300)
    _check_type(cooldown, int, "monitoring.alert_cooldown")

    log_level = monitoring.get("log_level", "INFO")
    _check_type(log_level, str, "monitoring.log_level")
    valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
    if log_level.upper() not in valid_levels:
        raise ConfigError(
            f"monitoring.log_level must be one of {sorted(valid_levels)}, "
            f"got '{log_level}'."
        )

    # --- devices section ---
    devices = _require(cfg, "devices")
    _check_type(devices, list, "devices")
    if not devices:
        raise ConfigError("'devices' list must not be empty.")

    valid_checks = {"ping", "port", "http"}
    for idx, device in enumerate(devices):
        _check_type(device, dict, f"devices[{idx}]")

        label = _require(device, "label", section=f"devices[{idx}]")
        _check_type(label, str, f"devices[{idx}].label")

        host = _require(device, "host", section=f"devices[{idx}]")
        _check_type(host, str, f"devices[{idx}].host")
        if not HOSTNAME_RE.match(host):
            raise ConfigError(
                f"devices[{idx}].host '{host}' is not a valid hostname or IP."
            )

        checks = _require(device, "checks", section=f"devices[{idx}]")
        _check_type(checks, list, f"devices[{idx}].checks")
        for chk in checks:
            if chk not in valid_checks:
                raise ConfigError(
                    f"devices[{idx}].checks contains unknown check "
                    f"'{chk}'. Valid: {sorted(valid_checks)}."
                )

        if "port" in checks:
            ports = device.get("ports")
            if ports is None:
                raise ConfigError(
                    f"devices[{idx}] has 'port' check but no 'ports' list."
                )
            _check_type(ports, list, f"devices[{idx}].ports")
            for p in ports:
                if not isinstance(p, int) or not (1 <= p <= 65535):
                    raise ConfigError(
                        f"devices[{idx}].ports contains invalid port '{p}'."
                    )

        if "http" in checks:
            url = device.get("url")
            if not url:
                raise ConfigError(
                    f"devices[{idx}] has 'http' check but no 'url'."
                )
            _check_type(url, str, f"devices[{idx}].url")


def load_config(path: str) -> dict:
    """
    Load, expand, and validate a YAML configuration file.

    Args:
        path: Filesystem path to the YAML config file.

    Returns:
        Validated configuration dictionary with all env vars expanded.

    Raises:
        ConfigError: on any load or validation failure.
        SystemExit(1): caller is expected to catch ConfigError and exit.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
    except FileNotFoundError:
        raise ConfigError(f"Configuration file not found: {path}")
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML parse error in '{path}': {exc}")

    if not isinstance(raw, dict):
        raise ConfigError(
            f"Configuration file '{path}' must contain a YAML mapping at "
            f"the top level."
        )

    # Expand ${ENV_VAR} placeholders – skip the password key in logs
    log.debug("Expanding environment variable placeholders in config.")
    try:
        expanded = _expand_env_vars(raw)
    except ConfigError:
        raise

    validate_config(expanded)
    log.debug("Configuration loaded and validated successfully from '%s'.", path)
    return expanded


def load_config_safe(path: str) -> dict:
    """
    Wrapper around load_config that prints the error and sys.exit(1) on failure.
    Intended for use at the CLI boundary.
    """
    try:
        return load_config(path)
    except ConfigError as exc:
        print(f"[NetWatch] Configuration error: {exc}", file=sys.stderr)
        sys.exit(1)
