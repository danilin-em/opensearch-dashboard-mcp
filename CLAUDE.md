# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An MCP (Model Context Protocol) server that exposes OpenSearch Dashboards to an LLM
client over **stdio**. Built on `FastMCP` (from the `mcp[cli]` package). Managed with
`uv`.

## Commands

```bash
uv sync --group dev                       # install incl. dev deps
uv run opensearch-dashboard-mcp           # run the server on stdio (Ctrl-D to exit)

uv run pytest                             # full suite, no coverage gate
uv run pytest tests/test_client.py        # single file
uv run pytest tests/test_client.py::test_name   # single test
uv run pytest -k tunnel                   # by keyword
uv run pytest --cov --cov-report=term-missing   # enables the fail_under=100 gate
```

Coverage is **opt-in**: `--cov` is what turns on the `fail_under = 100` gate (see
`pyproject.toml`), so running a single file locally won't trip it. CI always passes
`--cov`, so any merged change must keep coverage at 100%. CI matrix: Python 3.10–3.13.

## Architecture

One layer per file, wired together in `server.py`:

- `config.py` — `load_settings()` reads `OPENSEARCH_DASHBOARDS_*` / `SSH_PROXY_*` env
  vars into a frozen `Settings` dataclass. No I/O beyond env.
- `client.py` — `OSDashboardsClient`, a thin async httpx wrapper. The `httpx.AsyncClient`
  is built **lazily** (`_http()`), so importing the module opens no socket — this is the
  seam tests inject a `MockTransport` into.
- `tools.py` — `register(mcp, client)` attaches every `@mcp.tool()` as a closure over
  `mcp` + `client`. Tools are **not** module-level decorators; this deliberately avoids a
  `server`↔`tools` circular import. Adding a tool = add a function inside `register`.
- `server.py` — builds `settings`/`client`/`mcp` at import time, then `main()` runs it.
- `tunnel.py` — optional embedded SSH SOCKS5 tunnel (see below).

### Two error contracts (important, not obvious)

- `get_json` / `post_json` **raise** `RuntimeError` on HTTP ≥ 400 or non-JSON bodies.
  FastMCP turns the raised message into a tool error the LLM can read — never a silent
  empty result. `_parse_json` wraps `resp.json()` because a 2xx SSO/HTML login page would
  otherwise throw a bare `JSONDecodeError`.
- `connect()` (backing the `ping` tool) **never raises** for auth/HTTP problems — its
  whole job is to *report* them in a status dict. Flow: `authinfo` → if `401` and creds
  present → `login` → `authinfo` again. A **403 is not retried** (it's authorization, not a
  missing session; replaying credentials can lock the account).

### OpenSearch Dashboards specifics

- Every API call carries the `osd-xsrf: true` header — Dashboards rejects calls without it.
- Session auth stores the `security_authentication` cookie in the httpx cookie jar after
  `login()`; basic auth is also set from creds when present.
- Multi-tenancy: saved objects are per-tenant. Tools pass a `securitytenant` header
  (`tenant` arg; `"__user__"` = private tenant, `None` = user default).
- `discover_search` goes through the **internal** search API
  (`/internal/search/opensearch`) that Discover uses, so it works even when the raw
  OpenSearch REST API isn't exposed.

### SSH SOCKS5 tunnel

If `SSH_PROXY_TO` (e.g. `user@host`) is set, `main()` spawns `ssh -N -D <port>` before
serving and points the client at a `socks5h://` proxy — DNS resolves on the *remote* side,
for targets only reachable from a remote network.
The tunnel's lifetime == the server's. **stdout is the JSON-RPC channel** — the tunnel and
anything else must log only to **stderr** or it corrupts the protocol.

## Testing seams

`tests/conftest.py` provides two fixtures the whole suite leans on:

- `make_client(handler, **settings)` — drops an `httpx.AsyncClient` backed by
  `MockTransport` straight into `client._client`, so no socket is ever opened.
- `registered_tools(client)` — reaches into `mcp._tool_manager._tools` to return the raw
  async tool closures `{name: fn}`, so tests `await` the real functions with kwargs
  instead of round-tripping through MCP JSON content blocks.

## Gotchas

- `.mcp.json` at repo root contains a real dev connection with **plaintext credentials** —
  do not echo or commit further secrets there.
- `uv.lock` is tracked; CI installs with `--frozen`. After changing `pyproject.toml` deps,
  run `uv sync` and commit the updated lock or CI fails.
