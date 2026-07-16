"""FastMCP server entry point.

Run over stdio (the transport uvx / MCP clients expect):
    opensearch-dashboard-mcp
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from . import tools
from .client import OSDashboardsClient
from .config import load_settings
from .tunnel import SshSocksTunnel

settings = load_settings()
client = OSDashboardsClient(settings)

mcp = FastMCP("opensearch-dashboard-mcp")
tools.register(mcp, client)


def main() -> None:
    """Console-script entry point declared in pyproject.toml.

    If SSH_PROXY_TO is set, an embedded ssh SOCKS5 tunnel is opened first and the
    client is pointed at it, so requests egress from the remote host's network.
    The tunnel lives exactly as long as the server.
    """
    tunnel: SshSocksTunnel | None = None
    if settings.ssh_proxy_to:
        tunnel = SshSocksTunnel(settings.ssh_proxy_to, settings.ssh_proxy_port)
        tunnel.start()
        client.set_proxy(tunnel.proxy_url)
    try:
        mcp.run()
    finally:
        if tunnel is not None:
            tunnel.stop()


if __name__ == "__main__":
    main()
