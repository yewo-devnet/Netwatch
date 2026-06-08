"""
Unit tests for netwatch.utils.config

Uses tmp_path (pytest fixture) for file I/O; env vars patched via monkeypatch.
"""

import pytest
import yaml

from netwatch.utils.config import (
    ConfigError,
    _expand_env_vars,
    load_config,
    load_config_safe,
    validate_config,
)


# ---------------------------------------------------------------------------
# Minimal valid config used as a baseline
# ---------------------------------------------------------------------------

def _valid_config() -> dict:
    return {
        "monitoring": {
            "interval": 30,
            "alert_cooldown": 300,
            "log_level": "INFO",
        },
        "devices": [
            {
                "label": "Test Host",
                "host": "8.8.8.8",
                "checks": ["ping"],
            }
        ],
    }


def _write_yaml(path, data: dict) -> str:
    """Write a dict as YAML to a temp file and return its path string."""
    fpath = str(path)
    with open(fpath, "w") as fh:
        yaml.dump(data, fh)
    return fpath


# ---------------------------------------------------------------------------
# _expand_env_vars
# ---------------------------------------------------------------------------

class TestExpandEnvVars:
    def test_plain_string_unchanged(self):
        assert _expand_env_vars("hello") == "hello"

    def test_expands_env_var(self, monkeypatch):
        monkeypatch.setenv("MY_VAR", "world")
        assert _expand_env_vars("hello ${MY_VAR}") == "hello world"

    def test_missing_env_var_raises(self, monkeypatch):
        monkeypatch.delenv("MISSING_VAR", raising=False)
        with pytest.raises(ConfigError, match="MISSING_VAR"):
            _expand_env_vars("${MISSING_VAR}")

    def test_expands_nested_dict(self, monkeypatch):
        monkeypatch.setenv("PW", "secret")
        data = {"smtp": {"password": "${PW}"}}
        result = _expand_env_vars(data)
        assert result["smtp"]["password"] == "secret"

    def test_expands_list_items(self, monkeypatch):
        monkeypatch.setenv("RECIP", "admin@x.com")
        result = _expand_env_vars(["${RECIP}", "static@x.com"])
        assert result[0] == "admin@x.com"
        assert result[1] == "static@x.com"

    def test_non_string_passthrough(self):
        assert _expand_env_vars(42) == 42
        assert _expand_env_vars(True) is True


# ---------------------------------------------------------------------------
# validate_config
# ---------------------------------------------------------------------------

class TestValidateConfig:
    def test_valid_config_passes(self):
        validate_config(_valid_config())  # should not raise

    def test_missing_monitoring_raises(self):
        cfg = _valid_config()
        del cfg["monitoring"]
        with pytest.raises(ConfigError, match="monitoring"):
            validate_config(cfg)

    def test_missing_devices_raises(self):
        cfg = _valid_config()
        del cfg["devices"]
        with pytest.raises(ConfigError, match="devices"):
            validate_config(cfg)

    def test_empty_devices_raises(self):
        cfg = _valid_config()
        cfg["devices"] = []
        with pytest.raises(ConfigError, match="empty"):
            validate_config(cfg)

    def test_invalid_interval_type(self):
        cfg = _valid_config()
        cfg["monitoring"]["interval"] = "thirty"
        with pytest.raises(ConfigError, match="interval"):
            validate_config(cfg)

    def test_interval_zero_raises(self):
        cfg = _valid_config()
        cfg["monitoring"]["interval"] = 0
        with pytest.raises(ConfigError, match="interval"):
            validate_config(cfg)

    def test_invalid_log_level(self):
        cfg = _valid_config()
        cfg["monitoring"]["log_level"] = "VERBOSE"
        with pytest.raises(ConfigError, match="log_level"):
            validate_config(cfg)

    def test_invalid_check_name(self):
        cfg = _valid_config()
        cfg["devices"][0]["checks"] = ["traceroute"]
        with pytest.raises(ConfigError, match="traceroute"):
            validate_config(cfg)

    def test_port_check_without_ports(self):
        cfg = _valid_config()
        cfg["devices"][0]["checks"] = ["port"]
        with pytest.raises(ConfigError, match="ports"):
            validate_config(cfg)

    def test_http_check_without_url(self):
        cfg = _valid_config()
        cfg["devices"][0]["checks"] = ["http"]
        with pytest.raises(ConfigError, match="url"):
            validate_config(cfg)

    def test_invalid_host_raises(self):
        cfg = _valid_config()
        cfg["devices"][0]["host"] = "not a valid host!!!"
        with pytest.raises(ConfigError, match="valid hostname"):
            validate_config(cfg)

    def test_invalid_port_number(self):
        cfg = _valid_config()
        cfg["devices"][0]["checks"] = ["port"]
        cfg["devices"][0]["ports"] = [0]
        with pytest.raises(ConfigError, match="invalid port"):
            validate_config(cfg)

    def test_smtp_invalid_port(self):
        cfg = _valid_config()
        cfg["smtp"] = {
            "host": "smtp.example.com",
            "port": 25,
            "username": "u@x.com",
            "recipients": ["a@x.com"],
        }
        with pytest.raises(ConfigError, match="smtp.port"):
            validate_config(cfg)

    def test_smtp_empty_recipients(self):
        cfg = _valid_config()
        cfg["smtp"] = {
            "host": "smtp.example.com",
            "port": 587,
            "username": "u@x.com",
            "recipients": [],
        }
        with pytest.raises(ConfigError, match="recipients"):
            validate_config(cfg)

    def test_full_device_with_all_checks(self):
        cfg = _valid_config()
        cfg["devices"][0]["checks"] = ["ping", "port", "http"]
        cfg["devices"][0]["ports"] = [80, 443]
        cfg["devices"][0]["url"] = "https://8.8.8.8/health"
        validate_config(cfg)  # should not raise


# ---------------------------------------------------------------------------
# load_config (file-level tests)
# ---------------------------------------------------------------------------

class TestLoadConfig:
    def test_loads_valid_file(self, tmp_path, monkeypatch):
        monkeypatch.delenv("NETWATCH_SMTP_PASSWORD", raising=False)
        fpath = _write_yaml(tmp_path / "cfg.yaml", _valid_config())
        cfg = load_config(fpath)
        assert cfg["monitoring"]["interval"] == 30

    def test_file_not_found(self):
        with pytest.raises(ConfigError, match="not found"):
            load_config("/nonexistent/path/netwatch.yaml")

    def test_invalid_yaml(self, tmp_path):
        fpath = tmp_path / "bad.yaml"
        fpath.write_text(": invalid: [yaml")
        with pytest.raises(ConfigError, match="YAML parse error"):
            load_config(str(fpath))

    def test_non_mapping_top_level(self, tmp_path):
        fpath = tmp_path / "bad.yaml"
        fpath.write_text("- item1\n- item2\n")
        with pytest.raises(ConfigError, match="mapping"):
            load_config(str(fpath))

    def test_env_var_expansion(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NETWATCH_SMTP_PASSWORD", "testpw")
        cfg_data = _valid_config()
        cfg_data["smtp"] = {
            "host": "smtp.example.com",
            "port": 587,
            "username": "u@x.com",
            "password": "${NETWATCH_SMTP_PASSWORD}",
            "recipients": ["a@x.com"],
        }
        fpath = _write_yaml(tmp_path / "cfg.yaml", cfg_data)
        cfg = load_config(fpath)
        assert cfg["smtp"]["password"] == "testpw"


# ---------------------------------------------------------------------------
# load_config_safe (sys.exit on error)
# ---------------------------------------------------------------------------

class TestLoadConfigSafe:
    def test_exits_on_invalid_config(self, tmp_path):
        fpath = tmp_path / "bad.yaml"
        fpath.write_text("not_a_mapping: - invalid\n")
        with pytest.raises(SystemExit) as exc_info:
            load_config_safe(str(fpath))
        assert exc_info.value.code == 1

    def test_returns_config_on_valid_file(self, tmp_path):
        fpath = _write_yaml(tmp_path / "cfg.yaml", _valid_config())
        cfg = load_config_safe(str(fpath))
        assert "devices" in cfg
