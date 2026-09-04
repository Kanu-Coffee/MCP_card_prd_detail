from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import pytest
from v5_fixtures import V5Fixture, install_v5_fixture

from cardrag_mcp.launch_date import parse_launch_date
from cardrag_mcp.models import (
    MerchantSearchPage,
    ProductCatalogPage,
    ProductSummary,
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
) -> tuple[GenerationStore, ServingRepository, V5Fixture]:
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
    # The fixture has revisions with effective dates
    page: ProductCatalogPage = await repository.list_recent_products(months=120)
    assert isinstance(page, ProductCatalogPage)
    assert page.generation_id == fixture.generation_id
    # Current revisions in fixture should be returned
    assert page.total_count >= 1
    assert any(item.product_name == "알파 카드" for item in page.items)


@pytest.mark.asyncio
async def test_list_recent_products_issuer_filter(v5_runtime) -> None:
    _, repository, _ = v5_runtime
    kb_page = await repository.list_recent_products(months=120, issuer="kb")
    assert kb_page.total_count >= 1

    woori_page = await repository.list_recent_products(months=120, issuer="woori")
    assert woori_page.total_count == 0


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
