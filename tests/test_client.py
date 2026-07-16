"""Tests for the httpx wrapper in client.py.

Requests are served by an in-process ``httpx.MockTransport`` (see the
``make_client`` fixture) so nothing touches the network.
"""

from __future__ import annotations

import httpx
import pytest

from opensearch_dashboard_mcp.client import AUTHINFO_PATH, LOGIN_PATH, OSDashboardsClient
from opensearch_dashboard_mcp.config import Settings


def _settings(**overrides: object) -> Settings:
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


def json_response(status: int, payload: object) -> httpx.Response:
    return httpx.Response(status, json=payload)


def text_response(status: int, body: str) -> httpx.Response:
    return httpx.Response(status, text=body)


class TestGetJson:
    async def test_success_returns_parsed_json(self, make_client) -> None:
        client = make_client(lambda req: json_response(200, {"ok": True}))
        assert await client.get_json("/x") == {"ok": True}

    async def test_params_and_headers_forwarded(self, make_client) -> None:
        seen: dict[str, object] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            seen["params"] = dict(req.url.params)
            seen["tenant"] = req.headers.get("securitytenant")
            seen["xsrf"] = req.headers.get("osd-xsrf")
            return json_response(200, {})

        client = make_client(handler)
        await client.get_json(
            "/api/saved_objects/_find",
            params={"type": "dashboard", "per_page": 20},
            headers={"securitytenant": "Global"},
        )
        assert seen["params"] == {"type": "dashboard", "per_page": "20"}
        assert seen["tenant"] == "Global"
        # osd-xsrf is a client-level default and rides along on every request.
        assert seen["xsrf"] == "true"

    async def test_http_error_raises_runtimeerror_with_status_and_snippet(
        self, make_client
    ) -> None:
        client = make_client(lambda req: text_response(503, "upstream is down"))
        with pytest.raises(RuntimeError) as exc:
            await client.get_json("/x")
        msg = str(exc.value)
        assert "HTTP 503" in msg
        assert "upstream is down" in msg
        assert "GET" in msg

    async def test_non_json_2xx_body_raises_readable_error(self, make_client) -> None:
        # A reverse-proxy SSO login page served with 200 is the real-world case.
        client = make_client(lambda req: text_response(200, "<html>login</html>"))
        with pytest.raises(RuntimeError, match="non-JSON body"):
            await client.get_json("/x")

    async def test_error_snippet_truncated(self, make_client) -> None:
        client = make_client(lambda req: text_response(500, "E" * 2000))
        with pytest.raises(RuntimeError) as exc:
            await client.get_json("/x")
        msg = str(exc.value)
        # Snippet is capped at ~500 chars, so the full 2000-char body never
        # ends up in the error the LLM sees.
        assert "E" * 500 in msg
        assert "E" * 600 not in msg


class TestPostJson:
    async def test_success_sends_body_and_returns_json(self, make_client) -> None:
        seen: dict[str, object] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            seen["body"] = req.content
            return json_response(200, {"rawResponse": {"hits": {}}})

        client = make_client(handler)
        out = await client.post_json("/internal/search/opensearch", {"params": {}})
        assert out == {"rawResponse": {"hits": {}}}
        assert b"params" in seen["body"]  # type: ignore[operator]

    async def test_error_raises(self, make_client) -> None:
        client = make_client(lambda req: text_response(400, "bad query"))
        with pytest.raises(RuntimeError, match="HTTP 400"):
            await client.post_json("/x", {"a": 1})


class TestLogin:
    async def test_posts_credentials_to_login_endpoint(self, make_client) -> None:
        seen: dict[str, object] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            seen["path"] = req.url.path
            seen["body"] = req.content
            return json_response(200, {})

        client = make_client(handler, username="admin", password="secret")
        await client.login()
        assert seen["path"] == LOGIN_PATH
        assert b"admin" in seen["body"]  # type: ignore[operator]
        assert b"secret" in seen["body"]  # type: ignore[operator]


class TestConnect:
    async def test_authenticated_reports_user_and_tenants(self, make_client) -> None:
        payload = {
            "user_name": "alice",
            "tenants": {"global": True, "Developers": True},
        }
        client = make_client(lambda req: json_response(200, payload))
        out = await client.connect()
        assert out["connected"] is True
        assert out["authenticated"] is True
        assert out["user"] == "alice"
        assert sorted(out["tenants"]) == ["Developers", "global"]

    async def test_user_fallback_and_missing_tenants(self, make_client) -> None:
        # No user_name -> falls back to `user`; tenants not a dict -> [].
        client = make_client(lambda req: json_response(200, {"user": "bob"}))
        out = await client.connect()
        assert out["user"] == "bob"
        assert out["tenants"] == []

    async def test_connectivity_failure_reported(self, make_client) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("name resolution failed")

        client = make_client(handler)
        out = await client.connect()
        assert out == {
            "connected": False,
            "authenticated": False,
            "error": "name resolution failed",
        }

    async def test_401_without_credentials(self, make_client) -> None:
        client = make_client(lambda req: text_response(401, "Unauthorized"))
        out = await client.connect()
        assert out["connected"] is True
        assert out["authenticated"] is False
        assert "no credentials" in out["error"]

    async def test_401_then_login_then_authenticated(self, make_client) -> None:
        calls: list[str] = []

        def handler(req: httpx.Request) -> httpx.Response:
            calls.append(f"{req.method} {req.url.path}")
            if req.url.path == LOGIN_PATH:
                return json_response(200, {})
            # First authinfo 401, second (post-login) 200.
            if calls.count(f"GET {AUTHINFO_PATH}") == 1:
                return text_response(401, "Unauthorized")
            return json_response(200, {"user_name": "alice", "tenants": {"g": 1}})

        client = make_client(handler, username="admin", password="secret")
        out = await client.connect()
        assert out["authenticated"] is True
        assert out["user"] == "alice"
        assert calls == [
            f"GET {AUTHINFO_PATH}",
            f"POST {LOGIN_PATH}",
            f"GET {AUTHINFO_PATH}",
        ]

    async def test_401_then_login_fails(self, make_client) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            if req.url.path == LOGIN_PATH:
                return text_response(403, "bad password")
            return text_response(401, "Unauthorized")

        client = make_client(handler, username="admin", password="wrong")
        out = await client.connect()
        assert out["authenticated"] is False
        assert out["logged_in"] is False
        assert "login failed" in out["error"]
        assert "HTTP 403" in out["error"]

    async def test_401_then_login_network_drop(self, make_client) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            if req.url.path == LOGIN_PATH:
                raise httpx.ConnectError("dropped")
            return text_response(401, "Unauthorized")

        client = make_client(handler, username="admin", password="secret")
        out = await client.connect()
        assert out["authenticated"] is False
        assert "login flow failed" in out["error"]

    async def test_403_is_not_retried(self, make_client) -> None:
        # 403 = authenticated-but-forbidden; replaying creds is wrong, so connect
        # must report it without ever POSTing to login.
        calls: list[str] = []

        def handler(req: httpx.Request) -> httpx.Response:
            calls.append(req.url.path)
            return text_response(403, "Forbidden")

        client = make_client(handler, username="admin", password="secret")
        out = await client.connect()
        assert out["authenticated"] is False
        assert "HTTP 403" in out["error"]
        assert LOGIN_PATH not in calls

    async def test_authinfo_non_json_body(self, make_client) -> None:
        client = make_client(lambda req: text_response(200, "<html>sso</html>"))
        out = await client.connect()
        assert out["authenticated"] is False
        assert "non-JSON body" in out["error"]

    async def test_authinfo_unexpected_json_shape(self, make_client) -> None:
        # A JSON list (not an object) must not blow up on `.get`.
        client = make_client(lambda req: json_response(200, ["not", "a", "dict"]))
        out = await client.connect()
        assert out["authenticated"] is False
        assert "unexpected JSON shape" in out["error"]


class TestAclose:
    async def test_close_resets_client_and_is_idempotent(self, make_client) -> None:
        client = make_client(lambda req: json_response(200, {}))
        assert client._client is not None
        await client.aclose()
        assert client._client is None
        # Second close is a no-op, not an error.
        await client.aclose()


class TestHttpBuild:
    """The real lazy-build path (bypassed by the make_client fixture).

    Building the httpx client never opens a socket, so these exercise the
    production ``_http()`` wiring directly.
    """

    async def test_lazy_build_caches_and_carries_defaults(self) -> None:
        client = OSDashboardsClient(_settings())
        http = client._http()
        assert http.base_url == httpx.URL("http://osd.test")
        assert http.headers["osd-xsrf"] == "true"
        # Second call returns the same cached client, not a fresh one.
        assert client._http() is http
        await client.aclose()

    async def test_auth_set_when_credentials_present(self) -> None:
        client = OSDashboardsClient(_settings(username="admin", password="secret"))
        assert client._http().auth is not None
        await client.aclose()

    async def test_no_auth_without_credentials(self) -> None:
        client = OSDashboardsClient(_settings(username="admin", password=None))
        assert client._http().auth is None
        await client.aclose()

    async def test_set_proxy_captured_at_build(self) -> None:
        # set_proxy before the first request must feed the built client
        # (socks5h requires the socksio extra, installed as a dep).
        client = OSDashboardsClient(_settings())
        client.set_proxy("socks5h://127.0.0.1:1080")
        assert client._proxy == "socks5h://127.0.0.1:1080"
        client._http()  # builds with the proxy, no request issued
        await client.aclose()
