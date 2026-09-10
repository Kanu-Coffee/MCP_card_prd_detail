from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import numpy as np
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from v5_fixtures import V5Fixture, install_v5_fixture

import cardrag_mcp.repository as repository_module
from cardrag_mcp.app import build_mcp_server
from cardrag_mcp.config import Settings
from cardrag_mcp.launch_date import parse_launch_date
from cardrag_mcp.models import (
    MerchantSearchPage,
    ProductCatalogPage,
    ProductSummary,
    RecentProductCatalogPage,
)
from cardrag_mcp.repository import ServingRepository
from cardrag_mcp.store import GenerationStore


class FakeEmbedder:
    def __init__(self, vector: np.ndarray) -> None:
        self.vector = vector

    async def embed(self, *args, **kwargs) -> list[float]:
        return [float(v) for v in self.vector]


@pytest.fixture
def v5_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[GenerationStore, ServingRepository, V5Fixture]:
    _freeze_today(monkeypatch, date(2026, 9, 9))
    store = GenerationStore(tmp_path / "state", maximum_vector_bytes=2 * 1024 * 1024)
    fixture, _ = install_v5_fixture(store)
    query = np.zeros((4096,), dtype=np.float32)
    query[0] = 1.0
    embedder = FakeEmbedder(query)
    repository = ServingRepository(
        store,
        embedder,  # type: ignore[arg-type]
        cursor_secret=b"v5-metadata-test-cursor-secret-123456",
        maximum_candidates=20,
    )
    return store, repository, fixture


def _freeze_today(monkeypatch: pytest.MonkeyPatch, today: date) -> None:
    monkeypatch.setattr(repository_module, "_seoul_today", lambda: today)


def _set_launch_texts(fixture: V5Fixture, *texts: str) -> None:
    """Change only the isolated temporary metadata fixture."""
    with sqlite3.connect(fixture.database) as connection:
        connection.execute(
            "UPDATE contract_revisions SET effective_date = ? WHERE contract_revision_id = ?",
            ("2026-09-08", fixture.current_revision_id),
        )
        for ordinal, text in zip((3, 5), texts, strict=False):
            connection.execute(
                "UPDATE structure_nodes SET display_text = ? "
                "WHERE contract_revision_id = ? AND ordinal = ?",
                (text, fixture.current_revision_id, ordinal),
            )


# ── Launch Date Parser Tests ──────────────────────────────────────────


def test_parse_launch_date_woori_format() -> None:
    text = "- 상품 출시일 : 2026년 09월 01일"
    assert parse_launch_date(text) == date(2026, 9, 1)

    text_short = "• 상품 출시일: 2026년 4월 15일"
    assert parse_launch_date(text_short) == date(2026, 4, 15)


def test_parse_launch_date_shinhan_format() -> None:
    text = (
        "※ 카드 이용 시 제공되는 부가서비스는 "
        "카드 신규 출시(2026년 06월 09일) 이후 3년 이상 유지됩니다."
    )
    assert parse_launch_date(text) == date(2026, 6, 9)

    text_no_space = "카드 신규출시(2026년 07월 03일) 이후 3년"
    assert parse_launch_date(text_no_space) == date(2026, 7, 3)


def test_parse_launch_date_samsung_format() -> None:
    text = "* 카드를 이용하는 경우 부가서비스는 카드 신규 출시(2026년 8월 4일) 이후 변경 불가"
    assert parse_launch_date(text) == date(2026, 8, 4)


def test_parse_launch_date_kb_format() -> None:
    text = "• KB On the Go 체크카드(2026.06.29 출시)를 이용하는 경우"
    assert parse_launch_date(text) == date(2026, 6, 29)

    text_old = "▪ KB국민 의사카드(1993년 10월 02일 출시)를 이용하는 경우"
    assert parse_launch_date(text_old) == date(1993, 10, 2)


def test_parse_launch_date_no_date_or_invalid() -> None:
    assert parse_launch_date("국내외 모든 가맹점 0.8% 청구할인") is None
    assert parse_launch_date("상품 출시일 : 2026년 02월 31일") is None
    assert parse_launch_date("") is None


# ── ServingRepository Metadata Tool Tests ──────────────────────────────


@pytest.mark.asyncio
async def test_list_recent_products(v5_runtime) -> None:
    _, repository, fixture = v5_runtime
    _set_launch_texts(fixture, "상품 출시일 : 2026년 08월 12일")
    page = await repository.list_recent_products(months=3)
    assert isinstance(page, RecentProductCatalogPage)
    assert page.generation_id == fixture.generation_id
    assert page.total_count == 1
    assert page.items[0].product_name == "알파 카드"
    assert page.items[0].launch_date == date(2026, 8, 12)
    assert page.items[0].effective_date == date(2026, 9, 8)
    assert page.period_start == date(2026, 6, 9)
    assert page.period_end == date(2026, 9, 9)
    assert page.unknown_launch_date_count == 0


@pytest.mark.asyncio
async def test_list_recent_products_issuer_filter(v5_runtime) -> None:
    _, repository, _ = v5_runtime
    kb_page = await repository.list_recent_products(months=120, issuer="kb")
    assert kb_page.total_count == 0
    assert kb_page.unknown_launch_date_count == 1

    woori_page = await repository.list_recent_products(months=120, issuer="woori")
    assert woori_page.total_count == 0
    assert woori_page.unknown_launch_date_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "unknown_count"),
    (
        ("상품 출시일 : 1993년 10월 02일", 0),
        ("출시일 미기재", 1),
        ("상품 출시일 : 2026년 09월 10일", 0),
        ("상품 출시일 : 2026년 02월 31일", 1),
    ),
)
async def test_recent_products_never_use_revision_date_or_future_launch(
    v5_runtime, text: str, unknown_count: int
) -> None:
    _, repository, fixture = v5_runtime
    _set_launch_texts(fixture, text)
    page = await repository.list_recent_products(months=3)
    assert page.items == ()
    assert page.total_count == 0
    assert page.unknown_launch_date_count == unknown_count


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("today", "months", "start", "launch", "included"),
    (
        (date(2026, 9, 9), 3, date(2026, 6, 9), "2026-06-08", False),
        (date(2026, 9, 9), 3, date(2026, 6, 9), "2026-06-09", True),
        (date(2026, 9, 9), 3, date(2026, 6, 9), "2026-09-09", True),
        (date(2026, 3, 31), 1, date(2026, 2, 28), "2026-02-28", True),
        (date(2024, 3, 31), 1, date(2024, 2, 29), "2024-02-28", False),
        (date(2024, 3, 31), 1, date(2024, 2, 29), "2024-02-29", True),
        (date(2026, 1, 31), 3, date(2025, 10, 31), "2025-10-30", False),
        (date(2026, 1, 31), 3, date(2025, 10, 31), "2025-10-31", True),
    ),
)
async def test_recent_products_calendar_month_boundaries(
    v5_runtime, monkeypatch, today, months, start, launch, included
) -> None:
    _, repository, fixture = v5_runtime
    _freeze_today(monkeypatch, today)
    _set_launch_texts(fixture, f"상품 출시일 : {launch.replace('-', '.')}")
    page = await repository.list_recent_products(months=months)
    assert page.period_start == start
    assert page.period_end == today
    assert page.total_count == int(included)
    assert page.unknown_launch_date_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("months", (-1, 0, 121, 1.5, True, "3"))
async def test_recent_products_reject_invalid_months_in_repository_and_mcp(
    v5_runtime, months
) -> None:
    store, repository, _ = v5_runtime
    with pytest.raises(ValueError, match="months must be an integer between 1 and 120"):
        await repository.list_recent_products(months=months)
    server = build_mcp_server(
        repository,
        store,
        Settings(
            environment="test",
            mcp_bearer_token="test-static-bearer-token-000000000000",
            mcp_state_dir=store.root,
            mcp_public_base_url="http://testserver",
        ),
    )
    with pytest.raises(ToolError):
        await server.call_tool("list_recent_products", {"months": months})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("texts", "expected"),
    (
        (("상품 출시일 : 2026.08.12", "카드 신규 출시(2026년 08월 12일)"), date(2026, 8, 12)),
        (("상품 출시일 : 2026.08.12", "상품 출시일 : 2026.09.01"), None),
        (("상품 출시일 : 2026.09.01", "상품 출시일 : 2026.08.12"), None),
        (("상품 출시일 : 2026.08.12; 상품 출시일 : 2026.09.01",), None),
        (("상품 출시일 : 2026.08.12", "상품 출시일 : 2026.02.31"), None),
        (("출시일 미기재",), None),
    ),
)
async def test_launch_dates_are_consistent_across_metadata_tools(
    v5_runtime, texts, expected
) -> None:
    _, repository, fixture = v5_runtime
    _set_launch_texts(fixture, *texts)
    recent = await repository.list_recent_products(months=3)
    found = await repository.find_products("알파")
    summary = await repository.get_product_summary("kb", "ALPHA")
    assert summary is not None
    assert found.items[0].launch_date == summary.launch_date == expected
    assert "unknown_launch_date_count" not in found.model_dump()
    if expected is None:
        assert recent.items == ()
        assert recent.unknown_launch_date_count == 1
        assert found.model_dump(mode="json")["items"][0]["launch_date"] is None
    else:
        assert recent.items[0].launch_date == expected
        assert recent.unknown_launch_date_count == 0


@pytest.mark.asyncio
async def test_find_products_by_keyword(v5_runtime) -> None:
    _, repository, fixture = v5_runtime
    # Match Korean name
    page: ProductCatalogPage = await repository.find_products("알파")
    assert page.generation_id == fixture.generation_id
    assert page.total_count == 1
    assert page.items[0].product_name == "알파 카드"
    assert page.items[0].product_code == "ALPHA"
    assert page.items[0].issuer == "kb"


@pytest.mark.asyncio
async def test_find_products_width_and_case_insensitive(v5_runtime) -> None:
    _, repository, _ = v5_runtime
    # NFKC case-insensitive normalization
    page1 = await repository.find_products("alpha")
    page2 = await repository.find_products("ALPHA")
    assert page1.total_count == page2.total_count


@pytest.mark.asyncio
async def test_find_products_empty_or_no_match(v5_runtime) -> None:
    _, repository, _ = v5_runtime
    page = await repository.find_products("존재하지않는카드이름")
    assert page.total_count == 0
    assert page.items == ()

    with pytest.raises(ValueError, match="keyword must not be blank"):
        await repository.find_products("   ")


@pytest.mark.asyncio
async def test_find_cards_by_merchant(v5_runtime) -> None:
    _, repository, fixture = v5_runtime
    # The fixture has node display_text containing discount/benefit words
    page: MerchantSearchPage = await repository.find_cards_by_merchant("할인")
    assert isinstance(page, MerchantSearchPage)
    assert page.merchant_query == "할인"
    assert page.generation_id == fixture.generation_id

    # Blank query check
    with pytest.raises(ValueError, match="merchant_name must not be blank"):
        await repository.find_cards_by_merchant("  ")


@pytest.mark.asyncio
async def test_find_cards_by_merchant_no_match(v5_runtime) -> None:
    _, repository, _ = v5_runtime
    page = await repository.find_cards_by_merchant("화성탐사선가맹점")
    assert page.total_count == 0
    assert page.items == ()


@pytest.mark.asyncio
async def test_get_product_summary_by_code(v5_runtime) -> None:
    _, repository, fixture = v5_runtime
    summary: ProductSummary | None = await repository.get_product_summary("kb", "ALPHA")
    assert summary is not None
    assert isinstance(summary, ProductSummary)
    assert summary.generation_id == fixture.generation_id
    assert summary.issuer == "kb"
    assert summary.product_code == "ALPHA"
    assert summary.product_name == "알파 카드"
    assert summary.effective_date is not None


@pytest.mark.asyncio
async def test_get_product_summary_by_name(v5_runtime) -> None:
    _, repository, _ = v5_runtime
    summary = await repository.get_product_summary("kb", "알파")
    assert summary is not None
    assert summary.product_name == "알파 카드"


@pytest.mark.asyncio
async def test_get_product_summary_not_found(v5_runtime) -> None:
    _, repository, _ = v5_runtime
    summary = await repository.get_product_summary("kb", "UNKNOWN_CARD")
    assert summary is None

    with pytest.raises(ValueError, match="identifier must not be blank"):
        await repository.get_product_summary("kb", "   ")
