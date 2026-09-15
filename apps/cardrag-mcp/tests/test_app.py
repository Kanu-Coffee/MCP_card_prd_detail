from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient
from mcp.server.mcpserver.exceptions import ToolError

from cardrag_mcp.app import build_app
from cardrag_mcp.config import Settings

AUTH_VALUE = "test-static-bearer-token-000000000000"


def settings_for(state) -> Settings:
    return Settings(
        environment="test",
        mcp_bearer_token=AUTH_VALUE,
        mcp_state_dir=state,
        mcp_public_base_url="http://testserver",
    )


def test_approved_mcp_tools(active_runtime) -> None:
    store, repository, _, _ = active_runtime
    app = build_app(repository, store, settings_for(store.root))
    tools = asyncio.run(app.state.mcp_server.list_tools())
    assert {tool.name for tool in tools} == {
        "search_contracts",
        "get_contract_bundle",
        "list_product_revisions",
        "search_evidence",
        "get_evidence",
        "get_product",
        "get_source_pdf",
        "get_source_page",
        "list_recent_products",
        "find_products",
        "find_cards_by_merchant",
        "get_product_summary",
    }
    by_name = {tool.name: tool for tool in tools}
    assert "document_id" not in by_name["search_evidence"].input_schema["properties"]
    assert set(by_name["get_evidence"].input_schema["properties"]) == {
        "evidence_id",
        "cursor",
        "limit",
    }
    assert set(by_name["search_contracts"].input_schema["properties"]) == {
        "query",
        "issuer",
        "product_lineage_id",
        "as_of",
        "include_history",
        "mode",
        "limit",
        "response_mode",
        "product_lineage_ids",
        "launch_start_date",
        "launch_end_date",
        "expected_generation_id",
    }
    assert set(by_name["get_contract_bundle"].input_schema["properties"]) == {
        "contract_revision_id",
        "scope",
        "include_links",
    }
    assert set(by_name["list_product_revisions"].input_schema["properties"]) == {
        "issuer",
        "product_lineage_id",
    }
    assert set(by_name["list_recent_products"].input_schema["properties"]) == {
        "months",
        "issuer",
        "start_date",
        "end_date",
        "issuers",
        "limit",
        "cursor",
        "expected_generation_id",
    }
    assert set(by_name["find_products"].input_schema["properties"]) == {
        "keyword",
        "issuer",
        "mode",
        "issuers",
        "sort",
        "limit",
        "cursor",
        "expected_generation_id",
    }
    assert set(by_name["find_cards_by_merchant"].input_schema["properties"]) == {
        "merchant_name",
        "issuer",
    }
    assert set(by_name["get_product_summary"].input_schema["properties"]) == {
        "issuer",
        "identifier",
        "products",
        "expected_generation_id",
    }
    issuer_tools = {
        "search_contracts",
        "list_product_revisions",
        "search_evidence",
        "get_product",
        "list_recent_products",
        "find_products",
        "find_cards_by_merchant",
        "get_product_summary",
    }
    for tool_name in issuer_tools:
        issuer_schema = by_name[tool_name].input_schema["properties"]["issuer"]
        serialized_schema = str(issuer_schema)
        for issuer in ("bc", "hana", "hyundai", "kb", "lotte", "samsung", "shinhan", "woori"):
            assert issuer in serialized_schema
        assert "KB국민카드" in serialized_schema
    assert "unsupported_drm" in (by_name["get_product"].description or "")
    assert by_name["search_contracts"].input_schema["properties"]["mode"]["enum"] == [
        "exact",
        "exhaustive",
    ]
    assert by_name["find_products"].input_schema["properties"]["mode"]["enum"] == [
        "search",
        "catalog",
        "coverage",
    ]
    # The unchanged LibreChat bridge consumes inline schemas; nested batch fields
    # must not introduce definitions or references it cannot resolve.
    import json

    assert "$ref" not in json.dumps([tool.input_schema for tool in tools])


def test_tool_dispatch_metrics_are_bounded_and_do_not_contain_arguments(active_runtime) -> None:
    import pytest

    store, repository, _, _ = active_runtime
    app = build_app(repository, store, settings_for(store.root))
    server = app.state.mcp_server
    asyncio.run(server.call_tool("find_products", {"keyword": "private-query-never-a-label"}))
    with pytest.raises(ToolError):
        asyncio.run(server.call_tool("private-invalid-tool", {}))
    with pytest.raises(ToolError):
        asyncio.run(server.call_tool("list_recent_products", {"months": -1}))
    body = app.state.metrics.body().decode()
    assert 'cardrag_mcp_tool_calls_total{outcome="success",tool="find_products"} 1.0' in body
    assert 'cardrag_mcp_tool_calls_total{outcome="error",tool="unknown"} 1.0' in body
    assert 'cardrag_mcp_tool_calls_total{outcome="error",tool="list_recent_products"} 1.0' in body
    assert 'cardrag_mcp_tool_response_bytes_count{tool="find_products"} 1.0' in body
    assert "private-query" not in body and "private-invalid-tool" not in body


def test_public_health_and_protected_resources_metrics_and_mcp(active_runtime) -> None:
    store, repository, _, _ = active_runtime
    app = build_app(repository, store, settings_for(store.root))
    with TestClient(app) as client:
        assert client.get("/health/live").json() == {"live": True}
        ready = client.get("/health/ready")
        assert ready.status_code == 200
        assert ready.json() == {"ready": True}
        for path in ("/resources/issuers", "/metrics", "/mcp"):
            response = client.get(path)
            assert response.status_code == 401
            assert response.headers["www-authenticate"] == "Bearer"
        authorized = client.get(
            "/resources/issuers",
            headers={"Authorization": f"Bearer {AUTH_VALUE}"},
        )
        assert authorized.status_code == 200
        assert {row["code"] for row in authorized.json()} == {"woori", "kb"}
        products = client.get(
            "/resources/products",
            headers={"Authorization": f"Bearer {AUTH_VALUE}"},
        )
        assert products.status_code == 200
        assert {row["availability"] for row in products.json()} == {
            "available",
            "unsupported_drm",
        }
        protected = client.get(
            "/resources/products/woori/P-DRM",
            headers={"Authorization": f"Bearer {AUTH_VALUE}"},
        )
        assert protected.status_code == 200
        assert protected.json()["availability"] == "unsupported_drm"
        assert "document" not in protected.json()
        assert protected.json()["protected_magic"] == "SCDSA002"
        metrics = client.get(
            "/metrics",
            headers={"Authorization": f"Bearer {AUTH_VALUE}"},
        )
        assert metrics.status_code == 200
        assert "cardrag_mcp_ready" in metrics.text


def test_local_pdf_hash_mime_bound_and_single_range_206(active_runtime) -> None:
    store, repository, _, fixture = active_runtime
    app = build_app(repository, store, settings_for(store.root))
    document_id, digest, size, body = fixture.documents[0]
    headers = {"Authorization": f"Bearer {AUTH_VALUE}"}
    with TestClient(app) as client:
        denied = client.get(f"/sources/{document_id}/pdf")
        assert denied.status_code == 401
        partial = client.get(
            f"/sources/{document_id}/pdf",
            headers={**headers, "Range": "bytes=0-4"},
        )
        assert partial.status_code == 206
        assert partial.content == b"%PDF-"
        assert partial.headers["content-range"] == f"bytes 0-4/{size}"
        assert partial.headers["accept-ranges"] == "bytes"
        assert partial.headers["etag"] == f'"{digest}"'
        assert partial.headers["content-type"].startswith("application/pdf")
        assert partial.headers["content-disposition"] == (
            f'attachment; filename="cardrag-{digest}.pdf"'
        )

        whole = client.get(f"/sources/{document_id}/pdf", headers=headers)
        assert whole.status_code == 200
        assert whole.content == body
        unsatisfied = client.get(
            f"/sources/{document_id}/pdf",
            headers={**headers, "Range": f"bytes={size}-"},
        )
        assert unsatisfied.status_code == 416
        assert unsatisfied.headers["content-range"] == f"bytes */{size}"
