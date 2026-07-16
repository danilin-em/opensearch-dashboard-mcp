"""HTTP client for the OpenSearch Dashboards API.

Thin async wrapper over httpx: builds the client lazily, handles session login
and the ``osd-xsrf`` header, and exposes ``get_json`` / ``post_json`` that raise
on HTTP errors so failures surface as MCP tool errors.
"""

from __future__ import annotations

from typing import Any

import httpx

from .config import Settings

# OpenSearch Dashboards security plugin endpoints.
AUTHINFO_PATH = "/api/v1/auth/authinfo"
LOGIN_PATH = "/auth/login"


class OSDashboardsClient:
    """Thin async wrapper around httpx for OpenSearch Dashboards.

    The httpx.AsyncClient is created lazily so importing this module (e.g. for
    tests or tool discovery) never opens a socket.
    """

    def __init__(self, settings: Settings, proxy: str | None = None) -> None:
        self._settings = settings
        self._proxy = proxy
        self._client: httpx.AsyncClient | None = None

    def set_proxy(self, proxy: str | None) -> None:
        """Route requests through ``proxy`` (e.g. an ssh SOCKS5 tunnel).

        Must be called before the first request: the httpx client is built
        lazily and captures the proxy at construction time.
        """
        self._proxy = proxy

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            auth: tuple[str, str] | None = None
            if self._settings.username and self._settings.password:
                auth = (self._settings.username, self._settings.password)
            self._client = httpx.AsyncClient(
                base_url=self._settings.base_url,
                auth=auth,
                verify=self._settings.verify_tls,
                # OpenSearch Dashboards rejects API calls without this header.
                headers={"osd-xsrf": "true"},
                timeout=30.0,
                proxy=self._proxy,
            )
        return self._client

    async def get_json(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """GET ``path`` on the Dashboards API and return parsed JSON.

        ``headers`` are merged on top of the client defaults for this one call
        (used e.g. to pass ``securitytenant`` for multi-tenant saved objects).

        Errors are surfaced by raising: HTTP >= 400 becomes a RuntimeError
        carrying the status and a short response snippet, network failures
        propagate as httpx exceptions. FastMCP turns the raised message into a
        tool error the LLM can read — no silent empty results.
        """
        resp = await self._http().get(path, params=params, headers=headers)
        if resp.is_error:
            snippet = resp.text[:500]
            raise RuntimeError(
                f"OpenSearch Dashboards {resp.request.method} {path} "
                f"failed: HTTP {resp.status_code} {snippet}"
            )
        return self._parse_json(resp, path)

    async def post_json(
        self,
        path: str,
        json_body: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> Any:
        """POST ``json_body`` to ``path`` and return parsed JSON.

        Same error contract as :meth:`get_json`. Used for the internal search
        API that backs Discover (``/internal/search/opensearch``).
        """
        resp = await self._http().post(path, json=json_body, headers=headers)
        if resp.is_error:
            snippet = resp.text[:500]
            raise RuntimeError(
                f"OpenSearch Dashboards {resp.request.method} {path} "
                f"failed: HTTP {resp.status_code} {snippet}"
            )
        return self._parse_json(resp, path)

    @staticmethod
    def _parse_json(resp: httpx.Response, path: str) -> Any:
        """Parse a non-error response body as JSON with the same error contract.

        httpx's ``resp.json()`` raises a bare ``JSONDecodeError`` (a subclass of
        ``ValueError``) when the body isn't JSON — e.g. an SSO/reverse-proxy HTML
        login page served with a 2xx status. Wrap it so callers get the readable
        RuntimeError the docstrings promise instead of a cryptic decode traceback.
        """
        try:
            return resp.json()
        except ValueError as exc:
            snippet = resp.text[:500]
            raise RuntimeError(
                f"OpenSearch Dashboards {resp.request.method} {path} "
                f"returned non-JSON body: HTTP {resp.status_code} {snippet}"
            ) from exc

    async def login(self) -> httpx.Response:
        """POST credentials to the session login endpoint.

        On success the security_authentication cookie is stored in the client's
        cookie jar and reused on every later request automatically.
        """
        return await self._http().post(
            LOGIN_PATH,
            json={
                "username": self._settings.username,
                "password": self._settings.password,
            },
        )

    async def connect(self) -> dict[str, Any]:
        """Check connectivity and authenticate, logging in if needed.

        Returns a status dict (never raises for auth/HTTP problems — the point
        of ping is to report them). Flow:
          authinfo -> if 401 and creds present -> login -> authinfo again.
        """
        # Only the initial fetch proves connectivity; a failure here means the
        # server is unreachable.
        try:
            resp = await self._http().get(AUTHINFO_PATH)
        except httpx.HTTPError as exc:
            return {"connected": False, "authenticated": False, "error": str(exc)}

        # 401 is the security plugin's "missing/expired session" signal — retry
        # login. A 403 is authenticated-but-forbidden (authorization); replaying
        # credentials there is wrong and can lock the account, so report it below.
        if resp.status_code == 401:
            if not (self._settings.username and self._settings.password):
                return {
                    "connected": True,
                    "authenticated": False,
                    "error": "not authenticated and no credentials configured",
                }
            # The server already answered, so it stays connected even if the
            # login POST or refetch then hits a network drop.
            try:
                login = await self.login()
                if login.is_error:
                    return {
                        "connected": True,
                        "authenticated": False,
                        "logged_in": False,
                        "error": f"login failed: HTTP {login.status_code} {login.text[:300]}",
                    }
                resp = await self._http().get(AUTHINFO_PATH)
            except httpx.HTTPError as exc:
                return {
                    "connected": True,
                    "authenticated": False,
                    "error": f"reachable but login flow failed: {exc}",
                }

        if resp.is_error:
            return {
                "connected": True,
                "authenticated": False,
                "error": f"authinfo HTTP {resp.status_code} {resp.text[:300]}",
            }

        try:
            info = resp.json()
        except ValueError:
            return {
                "connected": True,
                "authenticated": False,
                "error": f"authinfo returned non-JSON body: HTTP {resp.status_code} {resp.text[:300]}",
            }
        if not isinstance(info, dict):
            return {
                "connected": True,
                "authenticated": False,
                "error": f"authinfo returned unexpected JSON shape: HTTP {resp.status_code} {resp.text[:300]}",
            }
        tenants = info.get("tenants")
        return {
            "connected": True,
            "authenticated": True,
            "user": info.get("user_name") or info.get("user"),
            "tenants": list(tenants.keys()) if isinstance(tenants, dict) else [],
        }

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
