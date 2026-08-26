from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

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


def test_exactly_five_approved_mcp_tools(active_runtime) -> None:
    store, repository, _, _ = active_runtime
    app = build_app(repository, store, settings_for(store.root))
    tools = asyncio.run(app.state.mcp_server.list_tools())
    assert {tool.name for tool in tools} == {
        "search_evidence",
        "get_evidence",
        "get_product",
        "get_source_pdf",
        "get_source_page",
    }
    by_name = {tool.name: tool for tool in tools}
    assert "document_id" not in by_name["search_evidence"].input_schema["properties"]
    assert set(by_name["get_evidence"].input_schema["properties"]) == {
        "evidence_id",
        "cursor",
        "limit",
    }
    assert "unsupported_drm" in (by_name["get_product"].description or "")


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
