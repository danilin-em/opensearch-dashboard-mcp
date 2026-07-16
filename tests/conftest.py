"""Shared test fixtures and helpers.

Two seams the whole suite leans on:

* ``make_client`` injects an ``httpx.MockTransport`` straight into an
  ``OSDashboardsClient``'s lazily-built ``_client`` slot, so requests are served
  by an in-process handler and no socket is ever opened.
* ``registered_tools`` reaches into FastMCP's tool manager to hand back the raw
  async tool functions, letting tests await the real closures with kwargs
  instead of round-tripping through MCP's JSON content blocks.
"""

from __future__ import annotations

from typing import Any, Callable

import httpx
import pytest
from mcp.server.fastmcp import FastMCP

from opensearch_dashboard_mcp import tools
from opensearch_dashboard_mcp.client import OSDashboardsClient
from opensearch_dashboard_mcp.config import Settings

Handler = Callable[[httpx.Request], httpx.Response]


def make_settings(**overrides: Any) -> Settings:
    """A Settings with sane defaults; override individual fields per test."""
    base = dict(
        base_url="http://osd.test",
        username=None,
        password=None,
        verify_tls=True,
        ssh_proxy_to=None,
        ssh_proxy_port=None,
    )
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


@pytest.fixture
def make_client() -> Callable[..., OSDashboardsClient]:
    """Factory: build an OSDashboardsClient whose HTTP is served by ``handler``.

    The MockTransport is wired into a real httpx.AsyncClient that we drop into
    ``client._client``; because ``_http()`` returns that slot when set, the
    client never builds its own (socket-opening) transport.
    """

    def _factory(handler: Handler, **settings_overrides: Any) -> OSDashboardsClient:
        settings = make_settings(**settings_overrides)
        client = OSDashboardsClient(settings)
        auth: tuple[str, str] | None = None
        if settings.username and settings.password:
            auth = (settings.username, settings.password)
        client._client = httpx.AsyncClient(
            base_url=settings.base_url,
            auth=auth,
            headers={"osd-xsrf": "true"},
            transport=httpx.MockTransport(handler),
        )
        return client

    return _factory


@pytest.fixture
def registered_tools() -> Callable[[Any], dict[str, Callable[..., Any]]]:
    """Factory: register tools with a (possibly fake) client, return {name: fn}."""

    def _factory(client: Any) -> dict[str, Callable[..., Any]]:
        mcp = FastMCP("test")
        tools.register(mcp, client)
        return {name: tool.fn for name, tool in mcp._tool_manager._tools.items()}

    return _factory
