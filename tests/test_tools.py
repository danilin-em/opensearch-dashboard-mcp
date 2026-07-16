"""Tests for the MCP tool closures in tools.py.

Tools are exercised through their raw async functions (see ``registered_tools``)
backed by a ``FakeClient`` that records calls and returns canned payloads — so
each test asserts on the request the tool *builds* and the shape it *returns*,
with no HTTP involved.
"""

from __future__ import annotations

from typing import Any


class FakeClient:
    """Duck-typed stand-in for OSDashboardsClient.

    Records every get/post and replays queued return values so a test can assert
    both the request a tool constructs and how it reshapes the response.
    """

    def __init__(
        self,
        *,
        get_return: Any = None,
        post_return: Any = None,
        connect_return: Any = None,
    ) -> None:
        self._get_return = get_return
        self._post_return = post_return
        self._connect_return = connect_return
        self.get_calls: list[dict[str, Any]] = []
        self.post_calls: list[dict[str, Any]] = []
        self.connect_calls = 0

    async def get_json(self, path, params=None, headers=None):
        self.get_calls.append({"path": path, "params": params, "headers": headers})
        return self._get_return

    async def post_json(self, path, json_body, headers=None):
        self.post_calls.append({"path": path, "body": json_body, "headers": headers})
        return self._post_return

    async def connect(self):
        self.connect_calls += 1
        return self._connect_return


class TestPing:
    async def test_delegates_to_connect(self, registered_tools) -> None:
        fake = FakeClient(connect_return={"connected": True, "authenticated": True})
        tools = registered_tools(fake)
        out = await tools["ping"]()
        assert out == {"connected": True, "authenticated": True}
        assert fake.connect_calls == 1


class TestListSavedObjects:
    async def test_maps_objects_and_reads_total(self, registered_tools) -> None:
        fake = FakeClient(
            get_return={
                "total": 2,
                "saved_objects": [
                    {"id": "a", "type": "dashboard", "attributes": {"title": "One"}},
                    {"id": "b", "type": "dashboard", "attributes": {"title": "Two"}},
                ],
            }
        )
        tools = registered_tools(fake)
        out = await tools["list_saved_objects"](type_="dashboard", per_page=20)

        call = fake.get_calls[0]
        assert call["path"] == "/api/saved_objects/_find"
        assert call["params"] == {"type": "dashboard", "per_page": 20}
        # No tenant -> no securitytenant header at all.
        assert call["headers"] is None
        assert out == {
            "type": "dashboard",
            "tenant": None,
            "total": 2,
            "objects": [
                {"id": "a", "type": "dashboard", "title": "One"},
                {"id": "b", "type": "dashboard", "title": "Two"},
            ],
        }

    async def test_tenant_sets_securitytenant_header(self, registered_tools) -> None:
        fake = FakeClient(get_return={"saved_objects": []})
        tools = registered_tools(fake)
        out = await tools["list_saved_objects"](type_="search", tenant="Developers")
        assert fake.get_calls[0]["headers"] == {"securitytenant": "Developers"}
        assert out["tenant"] == "Developers"

    async def test_missing_attributes_yield_none_title(self, registered_tools) -> None:
        # `attributes` absent -> title None, not a KeyError.
        fake = FakeClient(get_return={"saved_objects": [{"id": "a", "type": "x"}]})
        tools = registered_tools(fake)
        out = await tools["list_saved_objects"]()
        assert out["objects"] == [{"id": "a", "type": "x", "title": None}]

    async def test_total_falls_back_to_object_count(self, registered_tools) -> None:
        fake = FakeClient(
            get_return={"saved_objects": [{"id": "a", "type": "x", "attributes": {}}]}
        )
        tools = registered_tools(fake)
        out = await tools["list_saved_objects"]()
        assert out["total"] == 1


class TestGetTenants:
    async def test_flattens_tenant_map(self, registered_tools) -> None:
        fake = FakeClient(
            get_return={
                "data": {
                    "global": {"description": "Global", "reserved": True},
                    "Devs": {"description": "team", "hidden": False},
                }
            }
        )
        tools = registered_tools(fake)
        out = await tools["get_tenants"]()
        assert fake.get_calls[0]["path"] == "/api/v1/configuration/tenants"
        assert out["total"] == 2
        assert {
            "name": "global",
            "description": "Global",
            "reserved": True,
            "hidden": False,
        } in out["tenants"]
        assert {
            "name": "Devs",
            "description": "team",
            "reserved": False,
            "hidden": False,
        } in out["tenants"]

    async def test_empty_data(self, registered_tools) -> None:
        fake = FakeClient(get_return={})
        tools = registered_tools(fake)
        out = await tools["get_tenants"]()
        assert out == {"total": 0, "tenants": []}


class TestGetIndexPatterns:
    async def test_maps_pattern_fields(self, registered_tools) -> None:
        fake = FakeClient(
            get_return={
                "total": 1,
                "saved_objects": [
                    {
                        "id": "ip1",
                        "attributes": {"title": "logs-*", "timeFieldName": "@timestamp"},
                    }
                ],
            }
        )
        tools = registered_tools(fake)
        out = await tools["get_index_patterns"](per_page=50)
        assert fake.get_calls[0]["params"] == {
            "type": "index-pattern",
            "per_page": 50,
        }
        assert out == {
            "total": 1,
            "index_patterns": [
                {"id": "ip1", "title": "logs-*", "time_field": "@timestamp"}
            ],
        }


class TestDiscoverSearch:
    def _post_payload(self) -> dict[str, Any]:
        return {
            "rawResponse": {
                "hits": {
                    "total": {"value": 42},
                    "hits": [
                        {"_id": "1", "_index": "logs-2024", "_source": {"msg": "hi"}},
                    ],
                }
            }
        }

    async def test_defaults_build_match_all_query(self, registered_tools) -> None:
        fake = FakeClient(post_return=self._post_payload())
        tools = registered_tools(fake)
        out = await tools["discover_search"](index="logs-*")

        call = fake.post_calls[0]
        assert call["path"] == "/internal/search/opensearch"
        body = call["body"]["params"]["body"]
        assert body["query"]["bool"]["must"] == [{"match_all": {}}]
        assert body["query"]["bool"]["filter"] == []
        assert body["size"] == 10
        assert body["sort"] == [
            {"@timestamp": {"order": "desc", "unmapped_type": "date"}}
        ]
        assert "_source" not in body
        assert call["body"]["params"]["index"] == "logs-*"
        # No tenant -> no header.
        assert call["headers"] is None

        assert out == {
            "index": "logs-*",
            "total": 42,
            "count": 1,
            "hits": [{"_id": "1", "_index": "logs-2024", "_source": {"msg": "hi"}}],
        }

    async def test_query_string_added_when_query_given(self, registered_tools) -> None:
        fake = FakeClient(post_return=self._post_payload())
        tools = registered_tools(fake)
        await tools["discover_search"](index="logs-*", query="level:error")
        must = fake.post_calls[0]["body"]["params"]["body"]["query"]["bool"]["must"]
        assert must == [
            {"query_string": {"query": "level:error", "analyze_wildcard": True}}
        ]

    async def test_time_range_filter(self, registered_tools) -> None:
        fake = FakeClient(post_return=self._post_payload())
        tools = registered_tools(fake)
        await tools["discover_search"](
            index="logs-*",
            time_from="now-15m",
            time_to="now",
            time_field="ts",
        )
        filt = fake.post_calls[0]["body"]["params"]["body"]["query"]["bool"]["filter"]
        assert filt == [
            {
                "range": {
                    "ts": {
                        "format": "strict_date_optional_time",
                        "gte": "now-15m",
                        "lte": "now",
                    }
                }
            }
        ]

    async def test_partial_time_range_only_from(self, registered_tools) -> None:
        fake = FakeClient(post_return=self._post_payload())
        tools = registered_tools(fake)
        await tools["discover_search"](index="logs-*", time_from="now-1h")
        rng = fake.post_calls[0]["body"]["params"]["body"]["query"]["bool"]["filter"][0]
        assert rng["range"]["@timestamp"] == {
            "format": "strict_date_optional_time",
            "gte": "now-1h",
        }

    async def test_fields_restrict_source(self, registered_tools) -> None:
        fake = FakeClient(post_return=self._post_payload())
        tools = registered_tools(fake)
        await tools["discover_search"](index="logs-*", fields=["msg", "level"])
        body = fake.post_calls[0]["body"]["params"]["body"]
        assert body["_source"] == ["msg", "level"]

    async def test_tenant_header_forwarded(self, registered_tools) -> None:
        fake = FakeClient(post_return=self._post_payload())
        tools = registered_tools(fake)
        await tools["discover_search"](index="logs-*", tenant="Global")
        assert fake.post_calls[0]["headers"] == {"securitytenant": "Global"}

    async def test_total_as_plain_int(self, registered_tools) -> None:
        # Some responses return hits.total as a bare int, not {value: N}.
        fake = FakeClient(
            post_return={"rawResponse": {"hits": {"total": 7, "hits": []}}}
        )
        tools = registered_tools(fake)
        out = await tools["discover_search"](index="logs-*")
        assert out["total"] == 7
        assert out["count"] == 0

    async def test_response_without_rawresponse_wrapper(self, registered_tools) -> None:
        # Falls back to the top-level payload when rawResponse is absent.
        fake = FakeClient(post_return={"hits": {"total": {"value": 3}, "hits": []}})
        tools = registered_tools(fake)
        out = await tools["discover_search"](index="logs-*")
        assert out["total"] == 3
