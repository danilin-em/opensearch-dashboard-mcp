"""Tests for env-var parsing in config.py."""

from __future__ import annotations

import pytest

from opensearch_dashboard_mcp.config import (
    _env_bool,
    _env_int,
    load_settings,
)

ALL_ENV = [
    "OPENSEARCH_DASHBOARDS_URL",
    "OPENSEARCH_DASHBOARDS_USERNAME",
    "OPENSEARCH_DASHBOARDS_PASSWORD",
    "OPENSEARCH_DASHBOARDS_VERIFY_TLS",
    "SSH_PROXY_TO",
    "SSH_PROXY_PORT",
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts from a clean slate — no host env bleed-through."""
    for name in ALL_ENV:
        monkeypatch.delenv(name, raising=False)


class TestEnvBool:
    def test_default_used_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert _env_bool("MISSING", True) is True
        assert _env_bool("MISSING", False) is False

    @pytest.mark.parametrize("raw", ["1", "true", "TRUE", "Yes", "on", "  On  "])
    def test_truthy_values(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        monkeypatch.setenv("FLAG", raw)
        assert _env_bool("FLAG", False) is True

    @pytest.mark.parametrize("raw", ["0", "false", "no", "off", "", "maybe"])
    def test_falsy_values(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        monkeypatch.setenv("FLAG", raw)
        assert _env_bool("FLAG", True) is False


class TestEnvInt:
    def test_none_when_unset(self) -> None:
        assert _env_int("MISSING") is None

    def test_none_when_blank(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PORT", "   ")
        assert _env_int("PORT") is None

    def test_parses_int(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PORT", "1080")
        assert _env_int("PORT") == 1080

    def test_bad_value_raises_with_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PORT", "nope")
        with pytest.raises(ValueError, match="PORT must be an integer"):
            _env_int("PORT")


class TestLoadSettings:
    def test_defaults(self) -> None:
        s = load_settings()
        assert s.base_url == "http://localhost:5601"
        assert s.username is None
        assert s.password is None
        assert s.verify_tls is True
        assert s.ssh_proxy_to is None
        assert s.ssh_proxy_port is None

    def test_base_url_trailing_slash_stripped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENSEARCH_DASHBOARDS_URL", "https://osd.example.com/")
        assert load_settings().base_url == "https://osd.example.com"

    def test_full_population(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENSEARCH_DASHBOARDS_URL", "https://osd:5601")
        monkeypatch.setenv("OPENSEARCH_DASHBOARDS_USERNAME", "admin")
        monkeypatch.setenv("OPENSEARCH_DASHBOARDS_PASSWORD", "secret")
        monkeypatch.setenv("OPENSEARCH_DASHBOARDS_VERIFY_TLS", "false")
        monkeypatch.setenv("SSH_PROXY_TO", "user@host")
        monkeypatch.setenv("SSH_PROXY_PORT", "1080")
        s = load_settings()
        assert s.base_url == "https://osd:5601"
        assert s.username == "admin"
        assert s.password == "secret"
        assert s.verify_tls is False
        assert s.ssh_proxy_to == "user@host"
        assert s.ssh_proxy_port == 1080

    def test_empty_ssh_proxy_to_is_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `os.getenv(...) or None` collapses "" to None so an empty var doesn't
        # trigger the tunnel branch in server.main().
        monkeypatch.setenv("SSH_PROXY_TO", "")
        assert load_settings().ssh_proxy_to is None

    def test_settings_is_frozen(self) -> None:
        s = load_settings()
        with pytest.raises(Exception):
            s.base_url = "mutated"  # type: ignore[misc]
