# opensearch-dashboard-mcp

MCP server that exposes OpenSearch Dashboards to an LLM client over stdio. Read
saved objects, list tenants and index patterns, and query logs like the Discover
app — all through the Dashboards API, so it works even when the raw OpenSearch
REST API isn't exposed.

## Run

```bash
uvx opensearch-dashboard-mcp
```

(After publishing to PyPI. For local dev use `uvx --from . opensearch-dashboard-mcp`.)

## Configure

Environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `OPENSEARCH_DASHBOARDS_URL` | `http://localhost:5601` | Base URL |
| `OPENSEARCH_DASHBOARDS_USERNAME` | — | Basic auth / session-login user |
| `OPENSEARCH_DASHBOARDS_PASSWORD` | — | Basic auth / session-login password |
| `OPENSEARCH_DASHBOARDS_VERIFY_TLS` | `true` | Verify TLS certs |
| `SSH_PROXY_TO` | — | If set (e.g. `user@host`), route all requests through an embedded SSH SOCKS5 tunnel to that host |
| `SSH_PROXY_PORT` | auto | Local port for the tunnel; a free port is picked when unset |

### SSH tunnel

When `SSH_PROXY_TO` is set, the server spawns `ssh -N -D <port> <host>` before
serving and routes HTTP through the resulting `socks5h://` proxy. DNS is resolved
on the remote side, so this reaches targets only visible from a remote network
(e.g. a VPN living inside a container). Requires key-based SSH auth (runs with
`BatchMode=yes`). The tunnel lives exactly as long as the server.

## Use in an MCP client

```json
{
  "mcpServers": {
    "opensearch-dashboards": {
      "command": "uvx",
      "args": ["opensearch-dashboard-mcp"],
      "env": {
        "OPENSEARCH_DASHBOARDS_URL": "http://localhost:5601",
        "OPENSEARCH_DASHBOARDS_USERNAME": "admin",
        "OPENSEARCH_DASHBOARDS_PASSWORD": "admin"
      }
    }
  }
}
```

## Tools

- `ping()` — check connectivity + auth. Hits the security authinfo endpoint;
  on a 401 with credentials set, performs a session login and re-checks.
  Returns `{connected, authenticated, user, tenants}` or the error reason.
- `list_saved_objects(type_="dashboard", per_page=20, tenant=None)` — saved
  objects via `saved_objects/_find`, returned as compact `{id, title, type}`.
  `tenant` selects the multi-tenancy tenant (name, `"__user__"` for the private
  tenant, or `None` for the user default).
- `get_tenants()` — multi-tenancy tenants via the security plugin config API,
  as `{name, description, reserved, hidden}`.
- `get_index_patterns(per_page=50)` — index patterns (saved objects of type
  `index-pattern`), as `{id, title, time_field}`.
- `discover_search(index, query=None, time_from=None, time_to=None, time_field="@timestamp", size=10, fields=None, tenant=None)`
  — fetch documents like the Discover app through the internal search API
  (`/internal/search/opensearch`). `query` is a Lucene query_string; `time_from`/
  `time_to` filter a range (`now-15m`, ISO, …); `fields` trims `_source` to avoid
  flooding context. Returns `{index, total, count, hits}`.

Multi-tenancy note: saved objects are stored per tenant. Discover available tenant
names with `get_tenants`, then pass one as `tenant` to the other tools.

## Develop

```bash
uv sync --group dev
uv run opensearch-dashboard-mcp        # starts on stdio; Ctrl-D to exit
uv run pytest                          # tests (add --cov for the 100% gate)
```

Layout: `config.py` (env → `Settings`), `client.py` (async httpx wrapper),
`tools.py` (tool registrations), `server.py` (entry point + tunnel), `tunnel.py`
(SSH SOCKS5 tunnel). See `CLAUDE.md` for the architecture in depth.
