from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from cardrag_mcp.app import build_mcp_server
from cardrag_mcp.config import Settings
from cardrag_mcp.issuer_input import (
    CANONICAL_ISSUER_CODES,
    normalize_issuer,
    normalize_optional_issuer,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    (
        ("BC카드", "bc"),
        ("비씨 카드", "bc"),
        ("Hana Card", "hana"),
        ("하나카드", "hana"),
        ("HYUNDAI-CARD", "hyundai"),
        ("현대카드", "hyundai"),
        ("KB Kookmin Card", "kb"),
        ("ＫＢ국민카드", "kb"),
        ("국민카드", "kb"),
        ("Lotte Card", "lotte"),
        ("롯데카드", "lotte"),
        ("Samsung Card", "samsung"),
        ("삼성카드", "samsung"),
        ("Shinhan Card", "shinhan"),
        ("신한카드", "shinhan"),
        ("Woori Card", "woori"),
        ("우리카드", "woori"),
    ),
)
def test_normalize_issuer_aliases(value: str, expected: str) -> None:
    assert normalize_issuer(value) == expected


def test_normalize_issuer_preserves_omission_and_rejects_unknown() -> None:
    assert normalize_optional_issuer(None) is None
    with pytest.raises(
        ValueError,
        match=(
            "unknown issuer; use one of the canonical values: "
            "bc, hana, hyundai, kb, lotte, samsung, shinhan, woori"
        ),
    ):
        normalize_issuer("없는카드사")


def _result() -> SimpleNamespace:
    return SimpleNamespace(model_dump=lambda *, mode: {"mode": mode})


def _repository() -> SimpleNamespace:
    return SimpleNamespace(
        search_contracts=AsyncMock(return_value=_result()),
        list_product_revisions=AsyncMock(return_value=_result()),
        search=AsyncMock(return_value=_result()),
        get_product=AsyncMock(return_value=_result()),
        list_recent_products=AsyncMock(return_value=_result()),
        find_products=AsyncMock(return_value=_result()),
        find_cards_by_merchant=AsyncMock(return_value=_result()),
        get_product_summary=AsyncMock(return_value=_result()),
    )


def _server(tmp_path):
    repository = _repository()
    settings = Settings(
        environment="test",
        mcp_bearer_token="test-static-bearer-token-000000000000",
        mcp_state_dir=tmp_path,
        mcp_public_base_url="http://testserver",
    )
    return build_mcp_server(repository, Mock(), settings), repository


@pytest.mark.asyncio
async def test_every_public_issuer_tool_normalizes_before_repository_lookup(tmp_path) -> None:
    server, repository = _server(tmp_path)

    await server.call_tool("search_contracts", {"query": "혜택", "issuer": "BC카드"})
    await server.call_tool(
        "list_product_revisions",
        {"issuer": "하나카드", "product_lineage_id": "lineage-1"},
    )
    await server.call_tool("search_evidence", {"query": "혜택", "issuer": "현대카드"})
    await server.call_tool("get_product", {"issuer": "KB국민카드", "product_code": "P1"})
    await server.call_tool("list_recent_products", {"issuer": "롯데카드"})
    await server.call_tool("find_products", {"keyword": "카드", "issuer": "삼성카드"})
    await server.call_tool(
        "find_cards_by_merchant",
        {"merchant_name": "스타벅스", "issuer": "신한카드"},
    )
    await server.call_tool(
        "get_product_summary",
        {"issuer": "우리카드", "identifier": "P1"},
    )

    assert repository.search_contracts.await_args.args[0].issuer == "bc"
    assert repository.list_product_revisions.await_args.args == ("hana", "lineage-1")
    assert repository.search.await_args.args[0].filters.issuer == "hyundai"
    assert repository.get_product.await_args.args == ("kb", "P1")
    assert repository.list_recent_products.await_args.kwargs["issuer"] == "lotte"
    assert repository.find_products.await_args.kwargs["issuer"] == "samsung"
    assert repository.find_cards_by_merchant.await_args.kwargs["issuer"] == "shinhan"
    assert repository.get_product_summary.await_args.kwargs["issuer"] == "woori"


@pytest.mark.asyncio
async def test_every_public_issuer_tool_rejects_unknown_before_repository_lookup(tmp_path) -> None:
    server, repository = _server(tmp_path)
    cases = (
        ("search_contracts", {"query": "혜택", "issuer": "없는카드사"}),
        (
            "list_product_revisions",
            {"issuer": "없는카드사", "product_lineage_id": "lineage-1"},
        ),
        ("search_evidence", {"query": "혜택", "issuer": "없는카드사"}),
        ("get_product", {"issuer": "없는카드사", "product_code": "P1"}),
        ("list_recent_products", {"issuer": "없는카드사"}),
        ("find_products", {"keyword": "카드", "issuer": "없는카드사"}),
        (
            "find_cards_by_merchant",
            {"merchant_name": "스타벅스", "issuer": "없는카드사"},
        ),
        ("get_product_summary", {"issuer": "없는카드사", "identifier": "P1"}),
    )

    for tool_name, arguments in cases:
        with pytest.raises(ToolError, match="unknown issuer"):
            await server.call_tool(tool_name, arguments)

    for method_name in (
        "search_contracts",
        "list_product_revisions",
        "search",
        "get_product",
        "list_recent_products",
        "find_products",
        "find_cards_by_merchant",
        "get_product_summary",
    ):
        assert getattr(repository, method_name).await_count == 0


def test_canonical_issuer_contract_is_closed() -> None:
    assert CANONICAL_ISSUER_CODES == (
        "bc",
        "hana",
        "hyundai",
        "kb",
        "lotte",
        "samsung",
        "shinhan",
        "woori",
    )
