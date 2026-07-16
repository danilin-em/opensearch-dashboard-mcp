"""Tests for the main() wiring in server.py.

server.py builds ``settings``/``client``/``mcp`` at import time, so each test
monkeypatches those module globals (plus ``SshSocksTunnel``) to drive main()
through its two branches without a real server, client, or ssh process.
"""

from __future__ import annotations

import pytest

from opensearch_dashboard_mcp import server
from opensearch_dashboard_mcp.config import Settings


def _settings(**overrides) -> Settings:
    base = dict(
        base_url="http://osd.test",
        username=None,
        password=None,
        verify_tls=True,
        ssh_proxy_to=None,
        ssh_proxy_port=None,
    )
    base.update(overrides)
    return Settings(**base)


class FakeMcp:
    def __init__(self, run_raises: BaseException | None = None) -> None:
        self.ran = False
        self._run_raises = run_raises

    def run(self) -> None:
        self.ran = True
        if self._run_raises is not None:
            raise self._run_raises


class FakeClient:
    def __init__(self) -> None:
        self.proxy: str | None = None

    def set_proxy(self, proxy: str) -> None:
        self.proxy = proxy


class FakeTunnel:
    instances: list["FakeTunnel"] = []

    def __init__(self, target, port) -> None:
        self.target = target
        self.port = port
        self.started = False
        self.stopped = False
        FakeTunnel.instances.append(self)

    @property
    def proxy_url(self) -> str:
        return "socks5h://127.0.0.1:9999"

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


@pytest.fixture(autouse=True)
def _reset_tunnel_instances() -> None:
    FakeTunnel.instances.clear()


def _wire(monkeypatch, settings, *, run_raises=None) -> tuple[FakeMcp, FakeClient]:
    mcp, client = FakeMcp(run_raises), FakeClient()
    monkeypatch.setattr(server, "settings", settings)
    monkeypatch.setattr(server, "mcp", mcp)
    monkeypatch.setattr(server, "client", client)
    monkeypatch.setattr(server, "SshSocksTunnel", FakeTunnel)
    return mcp, client


class TestMain:
    def test_no_proxy_runs_without_tunnel(self, monkeypatch) -> None:
        mcp, client = _wire(monkeypatch, _settings())
        server.main()
        assert mcp.ran is True
        assert client.proxy is None
        assert FakeTunnel.instances == []

    def test_proxy_opens_tunnel_and_points_client(self, monkeypatch) -> None:
        mcp, client = _wire(
            monkeypatch, _settings(ssh_proxy_to="user@host", ssh_proxy_port=1080)
        )
        server.main()
        assert len(FakeTunnel.instances) == 1
        tun = FakeTunnel.instances[0]
        assert (tun.target, tun.port) == ("user@host", 1080)
        assert tun.started is True
        assert client.proxy == "socks5h://127.0.0.1:9999"
        assert mcp.ran is True
        # Tunnel is torn down after run() returns.
        assert tun.stopped is True

    def test_tunnel_stopped_even_if_run_raises(self, monkeypatch) -> None:
        boom = RuntimeError("server crashed")
        _wire(monkeypatch, _settings(ssh_proxy_to="user@host"), run_raises=boom)
        with pytest.raises(RuntimeError, match="server crashed"):
            server.main()
        # finally: stop() still ran despite the crash.
        assert FakeTunnel.instances[0].stopped is True
