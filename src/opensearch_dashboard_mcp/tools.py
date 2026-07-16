"""MCP tool registrations.

Tools are registered through ``register`` (closure over ``mcp`` + ``client``)
instead of module-level decorators, so there is no circular import between
``server`` and ``tools``.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from .client import OSDashboardsClient


def register(mcp: FastMCP, client: OSDashboardsClient) -> None:
    """Attach all tools to the given FastMCP server."""

    @mcp.tool()
    async def ping() -> dict[str, Any]:
        """Check OpenSearch Dashboards connectivity and authenticate.

        Hits the security authinfo endpoint. If unauthenticated (HTTP 401) and
        credentials are configured, performs a session login and re-checks.
        Reports {connected, authenticated, user, tenants} or the error reason.
        """
        return await client.connect()

    @mcp.tool()
    async def list_saved_objects(
        type_: str = "dashboard", per_page: int = 20, tenant: str | None = None
    ) -> dict[str, Any]:
        """List saved objects of a given type (dashboard, index-pattern, search, ...).

        Calls the Dashboards ``saved_objects/_find`` API and returns a compact
        list of ``{id, title, type}`` so the LLM is not flooded with raw fields.

        Saved objects are stored per multi-tenancy tenant. ``tenant`` selects
        which one to read via the ``securitytenant`` header:
          - a tenant name (e.g. "Developers Core", "Global")
          - "__user__" for the caller's private tenant
          - None (default) uses the user's configured default tenant
        Use the ``get_tenants`` tool to discover available tenant names.
        """
        headers = {"securitytenant": tenant} if tenant is not None else None
        data = await client.get_json(
            "/api/saved_objects/_find",
            params={"type": type_, "per_page": per_page},
            headers=headers,
        )
        objects = [
            {
                "id": obj.get("id"),
                "type": obj.get("type"),
                "title": (obj.get("attributes") or {}).get("title"),
            }
            for obj in data.get("saved_objects", [])
        ]
        return {
            "type": type_,
            "tenant": tenant,
            "total": data.get("total", len(objects)),
            "objects": objects,
        }

    @mcp.tool()
    async def get_tenants() -> dict[str, Any]:
        """List multi-tenancy tenants configured in OpenSearch Dashboards.

        Uses the security plugin config API (``/api/v1/configuration/tenants``),
        which returns a map of tenant name -> metadata. Flattened here to a list
        of ``{name, description, reserved, hidden}``.
        """
        data = await client.get_json("/api/v1/configuration/tenants")
        tenants = [
            {
                "name": name,
                "description": meta.get("description"),
                "reserved": meta.get("reserved", False),
                "hidden": meta.get("hidden", False),
            }
            for name, meta in (data.get("data") or {}).items()
        ]
        return {"total": len(tenants), "tenants": tenants}

    @mcp.tool()
    async def get_index_patterns(per_page: int = 50) -> dict[str, Any]:
        """List index patterns configured in OpenSearch Dashboards.

        Index patterns are saved objects of type ``index-pattern``. Returns
        ``{id, title, time_field}`` where ``title`` is the pattern glob
        (e.g. ``logs-*``) and ``time_field`` its time field, if any.
        """
        data = await client.get_json(
            "/api/saved_objects/_find",
            params={"type": "index-pattern", "per_page": per_page},
        )
        patterns = [
            {
                "id": obj.get("id"),
                "title": (obj.get("attributes") or {}).get("title"),
                "time_field": (obj.get("attributes") or {}).get("timeFieldName"),
            }
            for obj in data.get("saved_objects", [])
        ]
        return {"total": data.get("total", len(patterns)), "index_patterns": patterns}

    @mcp.tool()
    async def discover_search(
        index: str,
        query: str | None = None,
        time_from: str | None = None,
        time_to: str | None = None,
        time_field: str = "@timestamp",
        size: int = 10,
        fields: list[str] | None = None,
        tenant: str | None = None,
    ) -> dict[str, Any]:
        """Fetch documents like the Discover app.

        Runs a search against an index pattern through the internal search API
        that OpenSearch Dashboards' Discover uses (``/internal/search/opensearch``),
        so it works even when the raw OpenSearch REST API is not exposed.

        - ``index``: index-pattern glob, e.g. ``logs-api-*``
          (discover them via ``list_saved_objects`` with ``type_="index-pattern"``).
        - ``query``: Lucene query_string, e.g. ``level:error AND service:api``.
          ``None`` matches everything.
        - ``time_from`` / ``time_to``: range on ``time_field``, e.g. ``now-15m`` /
          ``now`` or an ISO timestamp. Both optional.
        - ``time_field``: timestamp field to filter/sort on (default ``@timestamp``).
        - ``size``: max hits to return (default 10; keep small — these are logs).
        - ``fields``: restrict ``_source`` to these fields to avoid flooding the
          context. ``None`` returns the full document.
        - ``tenant``: ``securitytenant`` header; usually unnecessary for raw data.

        Returns ``{index, total, count, hits: [{_id, _index, _source}]}`` where
        ``total`` is the full match count (may be an estimate for large indices).
        """
        must: list[dict[str, Any]] = []
        if query:
            must.append({"query_string": {"query": query, "analyze_wildcard": True}})

        filt: list[dict[str, Any]] = []
        if time_from or time_to:
            rng: dict[str, Any] = {"format": "strict_date_optional_time"}
            if time_from:
                rng["gte"] = time_from
            if time_to:
                rng["lte"] = time_to
            filt.append({"range": {time_field: rng}})

        body: dict[str, Any] = {
            "size": size,
            "query": {"bool": {"must": must or [{"match_all": {}}], "filter": filt}},
            # unmapped_type keeps the sort from erroring on indices lacking the field.
            "sort": [{time_field: {"order": "desc", "unmapped_type": "date"}}],
        }
        if fields:
            body["_source"] = fields

        headers = {"securitytenant": tenant} if tenant is not None else None
        data = await client.post_json(
            "/internal/search/opensearch",
            {"params": {"index": index, "body": body}},
            headers=headers,
        )

        raw = data.get("rawResponse", data)
        hits_obj = raw.get("hits", {}) or {}
        total = hits_obj.get("total")
        if isinstance(total, dict):
            total = total.get("value")
        hits = [
            {"_id": h.get("_id"), "_index": h.get("_index"), "_source": h.get("_source")}
            for h in hits_obj.get("hits", [])
        ]
        return {"index": index, "total": total, "count": len(hits), "hits": hits}
