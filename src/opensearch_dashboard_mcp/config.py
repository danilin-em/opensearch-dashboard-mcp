"""Runtime configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass

_TRUE_VALUES = {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    """Connection settings for an OpenSearch Dashboards instance."""

    base_url: str
    username: str | None
    password: str | None
    verify_tls: bool
    # Optional embedded SSH SOCKS5 tunnel. When ssh_proxy_to is set (e.g.
    # "user@remote-host"), the server opens `ssh -N -D` to that host and
    # routes httpx through it — useful when the target is only reachable from a
    # remote host's network.
    ssh_proxy_to: str | None
    ssh_proxy_port: int | None


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUE_VALUES


def _env_int(name: str) -> int | None:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


def load_settings() -> Settings:
    """Build Settings from the OPENSEARCH_DASHBOARDS_* / SSH_PROXY_* env vars."""
    return Settings(
        base_url=os.getenv("OPENSEARCH_DASHBOARDS_URL", "http://localhost:5601").rstrip("/"),
        username=os.getenv("OPENSEARCH_DASHBOARDS_USERNAME"),
        password=os.getenv("OPENSEARCH_DASHBOARDS_PASSWORD"),
        verify_tls=_env_bool("OPENSEARCH_DASHBOARDS_VERIFY_TLS", True),
        ssh_proxy_to=os.getenv("SSH_PROXY_TO") or None,
        ssh_proxy_port=_env_int("SSH_PROXY_PORT"),
    )
