from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from cardrag.domain import Issuer
from cardrag.issuers.base import DiscoveryMode, IssuerMarkupChanged, SourceRecord, UnsupportedCategory
from cardrag.issuers.common import canonical_snapshot
from cardrag.issuers.kb import DETAIL_PATH as KB_DETAIL_PATH
from cardrag.issuers.kb import KB_CATEGORIES, KBAdapter
from cardrag.issuers.kb import parse_history as parse_kb_history
from cardrag.issuers.kb import parse_listing as parse_kb_listing
from cardrag.issuers.registry import adapter_for
from cardrag.issuers.shinhan import HISTORY_PATH as SHINHAN_HISTORY_PATH
from cardrag.issuers.shinhan import LIST_PATH as SHINHAN_LIST_PATH
from cardrag.issuers.shinhan import ShinhanAdapter
from cardrag.issuers.shinhan import parse_history as parse_shinhan_history
from cardrag.issuers.shinhan import parse_listing as parse_shinhan_listing
from cardrag.issuers.woori import DETAIL_PATH as WOORI_DETAIL_PATH
from cardrag.issuers.woori import MAIN_PATH as WOORI_MAIN_PATH
from cardrag.issuers.woori import (
    WooriAdapter,
    encrypt_raonk,
    parse_detail_records,
    raonk_plaintext,
)

FIXTURES = Path(__file__).parents[1] / "fixtures" / "issuers"
DISCOVERED_AT = datetime(2026, 8, 12, tzinfo=UTC)


def _text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _json(name: str) -> dict[str, object]:
    return json.loads(_text(name))


def test_registry_exposes_only_the_three_v1_issuers() -> None:
    for issuer, adapter_type in (
        (Issuer.WOORI, WooriAdapter),
        (Issuer.KB, KBAdapter),
        (Issuer.SHINHAN, ShinhanAdapter),
    ):
        adapter = adapter_for(issuer)
        assert isinstance(adapter, adapter_type)
        assert adapter.issuer is issuer
        assert adapter.allowed_hosts
        assert adapter.parser_version


def test_snapshot_identity_includes_discovery_contract() -> None:
    record = parse_kb_listing(
        _text("kb_listing.html"),
        category_code="0",
        discovered_at=DISCOVERED_AT,
    )[0]
    common = {
        "issuer": Issuer.KB,
        "source_url": "https://card.kbcard.com/fixture",
        "records": [record],
        "started_at": DISCOVERED_AT,
    }

    current = canonical_snapshot(
        **common,
        mode=DiscoveryMode.CURRENT,
        parser_version="kb-fixture.v1",
    )
    history = canonical_snapshot(
        **common,
        mode=DiscoveryMode.HISTORY,
        parser_version="kb-fixture.v1",
    )
    parser_upgrade = canonical_snapshot(
        **common,
        mode=DiscoveryMode.CURRENT,
        parser_version="kb-fixture.v2",
    )

    assert len({current.snapshot_id, history.snapshot_id, parser_upgrade.snapshot_id}) == 3


def test_snapshot_identity_covers_all_normalized_source_fields() -> None:
    record = parse_kb_listing(
        _text("kb_listing.html"),
        category_code="0",
        discovered_at=DISCOVERED_AT,
    )[0]

    def snapshot(changed: SourceRecord) -> str:
        return canonical_snapshot(
            issuer=Issuer.KB,
            mode=DiscoveryMode.CURRENT,
            source_url="https://card.kbcard.com/fixture",
            parser_version="kb-fixture.v1",
            records=[changed],
            started_at=DISCOVERED_AT,
        ).snapshot_id

    changed_name = record.model_copy(update={"product_name": record.product_name + " 개정"})
    changed_metadata = record.model_copy(update={"metadata": {"category_code": "changed"}})
    changed_current = record.model_copy(update={"is_current": False})

    assert len(
        {
            snapshot(record),
            snapshot(changed_name),
            snapshot(changed_metadata),
            snapshot(changed_current),
        }
    ) == 4


def test_woori_parser_distinguishes_current_and_full_history() -> None:
    payload = _json("woori_detail.json")
    current = parse_detail_records(
        payload,
        product_code="W-FIXTURE-001",
        product_name="우리 픽스처 카드",
        current_only=True,
        discovered_at=DISCOVERED_AT,
    )
    history = parse_detail_records(
        payload,
        product_code="W-FIXTURE-001",
        product_name="우리 픽스처 카드",
        current_only=False,
        discovered_at=DISCOVERED_AT,
    )

    assert len(current) == 1
    assert current[0].source_version == "10"
    assert current[0].is_current is True
    assert [record.source_version for record in history] == ["10", "9"]
    assert sum(record.is_current for record in history) == 1


@pytest.mark.asyncio
async def test_woori_adapter_current_and_history_snapshots() -> None:
    main = _json("woori_main.json")
    detail = _json("woori_detail.json")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="synthetic landing")
        if request.url.path == WOORI_MAIN_PATH:
            return httpx.Response(200, json=main)
        if request.url.path == WOORI_DETAIL_PATH:
            return httpx.Response(200, json=detail)
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        current = await WooriAdapter().discover(
            client,
            mode=DiscoveryMode.CURRENT,
            categories=frozenset({"personal"}),
        )
        history = await WooriAdapter().discover(
            client,
            mode=DiscoveryMode.HISTORY,
            categories=frozenset({"personal"}),
        )

    assert len(current.records) == 1
    assert len(history.records) == 2
    assert current.issuer is history.issuer is Issuer.WOORI


def test_kb_supports_all_five_legacy_categories() -> None:
    expected = {
        "0": "personal_credit",
        "1": "personal_check",
        "2": "corporate_credit",
        "3": "corporate_check",
        "4": "international_brand",
    }
    assert expected == KB_CATEGORIES
    for code, name in expected.items():
        records = parse_kb_listing(
            _text("kb_listing.html"),
            category_code=code,
            discovered_at=DISCOVERED_AT,
        )
        assert len(records) == 1
        assert records[0].category == name


@pytest.mark.asyncio
async def test_kb_adapter_current_and_history_snapshots() -> None:
    listing = _text("kb_listing.html")
    history_html = _text("kb_history.html")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="synthetic landing")
        if request.url.path == KB_DETAIL_PATH.split("?", 1)[0]:
            return httpx.Response(200, text=history_html)
        return httpx.Response(200, text=listing)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        current = await KBAdapter().discover(
            client,
            mode=DiscoveryMode.CURRENT,
            categories=frozenset({"0"}),
        )
        history = await KBAdapter().discover(
            client,
            mode=DiscoveryMode.HISTORY,
            categories=frozenset({"0"}),
        )

    assert len(current.records) == 1
    assert len(history.records) == 2
    assert sum(record.is_current for record in history.records) == 1


def test_kb_history_preserves_product_identity() -> None:
    current = parse_kb_listing(
        _text("kb_listing.html"),
        category_code="0",
        discovered_at=DISCOVERED_AT,
    )[0]
    history = parse_kb_history(_text("kb_history.html"), current=current)

    assert len(history) == 2
    assert {item.product_code for item in history} == {current.product_code}
    assert {item.effective_date.isoformat() for item in history} == {"2025-07-01", "2026-08-01"}


def test_shinhan_parser_supports_current_pages_and_history() -> None:
    first, cursor, done = parse_shinhan_listing(
        _text("shinhan_page_1.html"),
        category="credit",
        discovered_at=DISCOVERED_AT,
    )
    second, final_cursor, final_done = parse_shinhan_listing(
        _text("shinhan_page_2.html"),
        category="check",
        discovered_at=DISCOVERED_AT,
    )
    history = parse_shinhan_history(_text("shinhan_history.html"), current=first[0])

    assert len(first) == len(second) == 1
    assert (cursor, done) == ("cursor-2", False)
    assert (final_cursor, final_done) == (None, True)
    assert len(history) == 2
    assert {item.product_code for item in history} == {first[0].product_code}
    assert {item.source_version for item in history} == {"20250601", "20260801"}


@pytest.mark.asyncio
async def test_shinhan_adapter_follows_cursor_for_current_and_history() -> None:
    page_1 = _text("shinhan_page_1.html")
    page_2 = _text("shinhan_page_2.html")
    history_html = _text("shinhan_history.html")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="synthetic landing")
        form = parse_qs(request.content.decode())
        if request.url.path == SHINHAN_HISTORY_PATH:
            body = history_html
        elif request.url.path == SHINHAN_LIST_PATH and form.get("nxtQyKey", [""])[0] == "cursor-2":
            body = page_2
        else:
            body = page_1
        return httpx.Response(200, content=body.encode("euc-kr"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        current = await ShinhanAdapter().discover(
            client,
            mode=DiscoveryMode.CURRENT,
            categories=frozenset({"credit"}),
        )
        history = await ShinhanAdapter().discover(
            client,
            mode=DiscoveryMode.HISTORY,
            categories=frozenset({"credit"}),
        )

    assert len(current.records) == 2
    assert len(history.records) == 5
    assert {
        (record.product_code, record.source_post_id)
        for record in current.records
    }.issubset(
        {
            (record.product_code, record.source_post_id)
            for record in history.records
        }
    )
    assert current.mode is DiscoveryMode.CURRENT
    assert history.mode is DiscoveryMode.HISTORY


@pytest.mark.asyncio
@pytest.mark.parametrize("forbidden", ["corporate", "prepaid"])
async def test_shinhan_rejects_corporate_and_prepaid_before_network(forbidden: str) -> None:
    def fail_if_called(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("network must not be called for an unsupported category")

    async with httpx.AsyncClient(transport=httpx.MockTransport(fail_if_called)) as client:
        with pytest.raises(UnsupportedCategory, match="excludes corporate/prepaid"):
            await ShinhanAdapter().discover(
                client,
                mode=DiscoveryMode.CURRENT,
                categories=frozenset({forbidden}),
            )


@pytest.mark.asyncio
async def test_shinhan_rejects_repeated_pagination_cursor() -> None:
    repeated = _text("shinhan_page_1.html")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="synthetic landing")
        return httpx.Response(200, content=repeated.encode("euc-kr"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError, match="cursor repeated"):
            await ShinhanAdapter().discover(
                client,
                mode=DiscoveryMode.CURRENT,
                categories=frozenset({"credit"}),
            )


@pytest.mark.asyncio
async def test_all_adapters_treat_zero_rows_as_markup_anomaly() -> None:
    def woori_empty(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="landing")
        return httpx.Response(200, json={"mainDataList": {"cct11PrdntcAgrmMainVo": []}})

    def kb_empty(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="landing" if request.method == "GET" else "<table></table>")

    def shinhan_empty(request: httpx.Request) -> httpx.Response:
        body = "landing" if request.method == "GET" else "<!-- DONE -->"
        return httpx.Response(200, content=body.encode("euc-kr"))

    cases = (
        (WooriAdapter(), frozenset({"personal"}), woori_empty),
        (KBAdapter(), frozenset({"0"}), kb_empty),
        (ShinhanAdapter(), frozenset({"credit"}), shinhan_empty),
    )
    for adapter, categories, handler in cases:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(IssuerMarkupChanged):
                await adapter.discover(client, mode=DiscoveryMode.CURRENT, categories=categories)


def test_woori_raonk_payload_has_a_deterministic_golden_value() -> None:
    plaintext = raonk_plaintext(
        "/fixture/W_FIXTURE_20260801.pdf",
        "W_FIXTURE_20260801.pdf",
        guid="0123456789abcdef0123456789abcdef",
    )
    assert plaintext == (
        "kc\x0cc11\x0bk01\x0c2\x0bk12\x0c0123456789abcdef0123456789abcdef"
        "\x0bk05\x0c0\x0bk26\x0c/data/upload/uploadfiles/fixture/W_FIXTURE_20260801.pdf"
        "\x0bk31\x0cW_FIXTURE_20260801.pdf\x0bk21\x0c/fixture/W_FIXTURE_20260801.pdf"
    )
    assert encrypt_raonk(plaintext) == (
        "2kO1TCmzXLECES4BJAJsmVBFy8dtAatjlV38SRHXzndAtEeSpZQSEgQl97IclrgM95edThhY0nn0j9RZaDwRR%2B8Q4"
        "NZK0wsh3TNIRdBnIq4QbtukLfIjz599IN1GwMxJqn2tyEDZNSwZJndVTy2LFcb6Dn2Ku5f1pOVHZKAEmTSOs2MFBiA0"
        "LJMFtjy92DyyUkkEcOUkJu7d3nBTQtrW88IS5FLSRZaTggK9Jb6ve60mRLGx0IyCAaLxYqipisxU"
    )
