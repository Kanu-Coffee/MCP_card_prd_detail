from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import parse_qs, unquote

import httpx
import pytest

from cardrag_worker.contracts import SourceSnapshot
from cardrag_worker.issuers.common import IssuerMarkupChanged
from cardrag_worker.issuers.hana import HanaAdapter
from cardrag_worker.issuers.hana import parse_listing as parse_hana_listing
from cardrag_worker.issuers.hyundai import HyundaiAdapter, ListingProduct, parse_detail
from cardrag_worker.issuers.hyundai import parse_listing as parse_hyundai_listing

NOW = datetime(2026, 9, 6, tzinfo=UTC)
PRODUCT = ListingProduct("123", "현대 테스트 카드", date(2024, 5, 13), date(2025, 11, 19))
HYUNDAI_HTML = """
<li><p>현대 테스트 카드</p><ul><li>상품출시일:2024. 5. 13</li><li>발급중단일:2025. 11. 19</li></ul>
<a sqno="123" webBlbdTitl="현대 테스트 카드" onclick="getDownloadList(123, '0', '현대 테스트 카드');">PDF</a>
</li>
"""


def hyundai_payload(*attachments: dict[str, Any]) -> dict[str, Any]:
    return {"hdr": {"rsltCd": "0000"}, "bdy": {"result": {"cpuug2001DAO": list(attachments)}}}


def attachment(name: str = "상품 안내장 (신규).pdf", effective: str = "20260901") -> dict[str, Any]:
    return {"webPsnlBlbdSqno": "123", "apndFileNm": name, "agmRrfmDt": effective}


def hana_record(code: str = "00123", **overrides: Any) -> dict[str, Any]:
    return {
        "ADD_VAR3": code,
        "AN_TIT_NM": "하나 테스트 카드",
        "AN_SDT": "20260901",
        "APN_FILE_NM": "상품 안내장 (신규).pdf",
        "APN_FILE_PH_NM": "https://m.hanacard.co.kr/leaflet/00/",
        "ADD_VAR5": "Y",
        **overrides,
    }


def hana_payload(
    rows: list[dict[str, Any]], *, cursor: str = "", count: str = "1", **overrides: Any
) -> dict[str, Any]:
    return {
        "result": "success",
        "dataMap": {
            "RESULT_LIST": {"data": rows},
            "AMM_NEXT_KEY": cursor,
            "listCount": count,
            **overrides,
        },
    }


async def hana_discover(pages: list[dict[str, Any]], *, minimum: int = 1) -> SourceSnapshot:
    page_index = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal page_index
        if request.method == "GET":
            return httpx.Response(200, text="landing")
        assert request.url.path == "/OSA95000000D.ajax"
        assert request.headers["X-Requested-With"] == "XMLHttpRequest"
        data = parse_qs(request.content.decode(), keep_blank_values=True)
        assert data["CT_ID"] == ["CDPD"]
        assert data["SEARCHKEY"] == [""]
        expected_cursor = "" if page_index == 0 else pages[page_index - 1]["dataMap"]["AMM_NEXT_KEY"]
        assert data["AMM_NEXT_KEY"] == [expected_cursor]
        response = httpx.Response(
            200, content=json.dumps(pages[page_index], ensure_ascii=False).encode("euc-kr")
        )
        page_index += 1
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        return await HanaAdapter(minimum_records=minimum).discover_current(client)


def test_hyundai_listing_retains_discontinued_dates_and_deduplicates() -> None:
    assert parse_hyundai_listing(HYUNDAI_HTML * 2) == [PRODUCT]


@pytest.mark.parametrize(
    "html",
    [
        HYUNDAI_HTML.replace('sqno="123"', 'sqno=""'),
        HYUNDAI_HTML.replace("getDownloadList(123", "getDownloadList(456"),
        HYUNDAI_HTML.replace("2024. 5. 13", "2024. 13. 13"),
        HYUNDAI_HTML.replace("2025. 11. 19", "unknown"),
        HYUNDAI_HTML + HYUNDAI_HTML.replace("현대 테스트 카드", "다른 카드"),
    ],
)
def test_hyundai_rejects_invalid_listing(html: str) -> None:
    with pytest.raises(IssuerMarkupChanged):
        parse_hyundai_listing(html)


def test_hyundai_latest_attachment_is_order_independent_and_url_encoded() -> None:
    attachments = [attachment("old.pdf", "20240101"), attachment(), attachment()]
    row = parse_detail(hyundai_payload(*attachments), product=PRODUCT, discovered_at=NOW)
    reverse = parse_detail(hyundai_payload(*reversed(attachments)), product=PRODUCT, discovered_at=NOW)
    assert row is not None and reverse is not None
    assert row.source_id == reverse.source_id
    assert row.effective_date == date(2026, 9, 1)
    assert row.product_code == row.source_post_id == "123"
    assert HyundaiAdapter.spec.categories == ("shared",)
    assert row.category == "shared"
    assert row.file_name == "상품 안내장 (신규).pdf"
    assert " " not in row.source_url
    assert unquote(row.source_url) == "https://www.hyundaicard.com/upload/card/상품 안내장 (신규).pdf"
    assert row.metadata == {
        "classification_basis": "official_shared_listing",
        "issuance_discontinued": True,
        "issuance_discontinued_date": "2025-11-19",
        "product_launch_date": "2024-05-13",
    }


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"hdr": {"rsltCd": "error"}},
        {"bdy": {"result": {"cpuug2001DAO": None}}},
        hyundai_payload(attachment(effective="")),
        hyundai_payload(attachment(effective="20260230")),
        hyundai_payload(attachment("https://attacker.test/card.pdf")),
        hyundai_payload(attachment("../card.pdf")),
        hyundai_payload(attachment("card.html")),
        hyundai_payload({**attachment(), "webPsnlBlbdSqno": "456"}),
        hyundai_payload(attachment("a.pdf"), attachment("b.pdf")),
    ],
)
def test_hyundai_rejects_malformed_or_conflicting_attachments(payload: dict[str, Any]) -> None:
    with pytest.raises(IssuerMarkupChanged):
        parse_detail(payload, product=PRODUCT, discovered_at=NOW)


async def test_hyundai_discovery_and_direct_download() -> None:
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.method == "GET":
            return httpx.Response(200, text=HYUNDAI_HTML)
        assert parse_qs(request.content.decode()) == {"sqno": ["123"]}
        return httpx.Response(200, json=hyundai_payload(attachment()))

    adapter = HyundaiAdapter(minimum_records=1)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        snapshot = await adapter.discover_current(client)
        row = snapshot.records[0]
        download = await adapter.prepare_download(client, row)
        assert download.method == "GET"
        assert download.url == row.source_url
        assert len(calls) == 2
        with pytest.raises(ValueError):
            await adapter.prepare_download(client, replace(row, source_url="https://attacker.test/a.pdf"))
        with pytest.raises(ValueError):
            await adapter.prepare_download(client, replace(row, issuer="hana"))
        with pytest.raises(ValueError):
            await adapter.prepare_download(client, replace(row, category="personal"))


async def test_hyundai_empty_listing_fails_minimum() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text=""))
    ) as client:
        with pytest.raises(IssuerMarkupChanged, match="expected at least"):
            await HyundaiAdapter().discover_current(client)


def test_hyundai_ignores_withdrawn_older_file_but_never_missing_latest() -> None:
    row = parse_detail(
        hyundai_payload(attachment("", "20240101"), attachment()), product=PRODUCT, discovered_at=NOW
    )
    assert row is not None and row.effective_date == date(2026, 9, 1)
    with pytest.raises(IssuerMarkupChanged, match="latest attachment"):
        parse_detail(
            hyundai_payload(attachment("", "20270101"), attachment()), product=PRODUCT, discovered_at=NOW
        )


async def test_hyundai_empty_attachment_history_is_audited() -> None:
    empty_product = HYUNDAI_HTML.replace("123", "456").replace("현대 테스트 카드", "공시 없는 카드")

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text=HYUNDAI_HTML + empty_product)
        code = parse_qs(request.content.decode())["sqno"][0]
        return httpx.Response(200, json=hyundai_payload(attachment()) if code == "123" else hyundai_payload())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        snapshot = await HyundaiAdapter(minimum_records=1).discover_current(client)
    assert len(snapshot.records) == 1
    assert snapshot.warnings == ("Hyundai product 456: no published PDF attachment",)


async def test_hyundai_exact_unknown_date_sentinel_is_audited_without_inventing_date() -> None:
    unknown_product = HYUNDAI_HTML.replace("123", "456").replace("현대 테스트 카드", "시행일 없는 카드")

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text=HYUNDAI_HTML + unknown_product)
        code = parse_qs(request.content.decode())["sqno"][0]
        raw = (
            attachment() if code == "123" else {**attachment(effective="99999999"), "webPsnlBlbdSqno": "456"}
        )
        return httpx.Response(200, json=hyundai_payload(raw))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        snapshot = await HyundaiAdapter(minimum_records=1).discover_current(client)
    assert len(snapshot.records) == 1
    assert snapshot.warnings == ("Hyundai product 456: source effective date unavailable (99999999)",)


def test_hyundai_protected_allowance_is_exact_verified_source_and_bytes() -> None:
    assert tuple(item.contract_payload for item in HyundaiAdapter.spec.protected_source_allowances) == (
        {
            "source_id": "source_1841d83805f532fc31aa69249609448312dbb2b2a210e253d610b4d3a9ca9472",
            "product_code": "12396",
            "source_version": "20180822",
            "source_url": "https://www.hyundaicard.com/upload/card/210205_00687.pdf",
            "sha256": "605b2437087fb22300c9f060d72a1bc437b05373feff2e5196de22e356f228e6",
            "size_bytes": 317_916,
            "magic": "SCDSA004",
        },
    )


def test_hyundai_protected_current_source_recalculates_shared_source_identity() -> None:
    product = ListingProduct("12396", "LG U+-현대카드M", date(2010, 4, 20), date(2013, 6, 30))
    row = parse_detail(
        hyundai_payload(
            {"webPsnlBlbdSqno": "12396", "apndFileNm": "210205_00687.pdf", "agmRrfmDt": "20180822"},
            {"webPsnlBlbdSqno": "12396", "apndFileNm": "180822_00687.pdf", "agmRrfmDt": "20180501"},
        ),
        product=product,
        discovered_at=NOW,
    )
    assert row is not None
    allowance = HyundaiAdapter.spec.protected_source_allowances[0]
    assert row.source_id == allowance.source_id
    assert row.product_code == allowance.product_code
    assert row.source_version == allowance.source_version
    assert row.source_url == allowance.source_url
    assert row.category == "shared"
    assert row.metadata["classification_basis"] == "official_shared_listing"
    for changed in (
        replace(row, category="personal"),
        replace(row, metadata={}),
        replace(row, product_code="other"),
        replace(row, source_version="20180823"),
        replace(row, source_url="https://www.hyundaicard.com/upload/card/changed.pdf"),
    ):
        assert changed.source_id != allowance.source_id


async def test_hana_cursor_pagination_euckr_and_zero_count_sentinel() -> None:
    pages = [
        hana_payload([hana_record()], cursor="next", count="2", hasMore="Y", AMM_EOF_SWITCH="Y"),
        hana_payload([hana_record("00456", ADD_VAR5="N")], count="0", AMM_EOF_SWITCH="N"),
    ]
    snapshot = await hana_discover(pages)
    assert len(snapshot.records) == 2
    row = snapshot.records[0]
    assert row.product_code == row.source_post_id == "00123"
    assert row.product_name == "하나 테스트 카드"
    assert HanaAdapter.spec.categories == ("shared",)
    assert row.category == "shared"
    assert row.metadata == {"classification_basis": "official_shared_listing", "issuance_discontinued": True}
    assert " " not in row.source_url
    assert unquote(row.source_url) == "https://m.hanacard.co.kr/leaflet/00/상품 안내장 (신규).pdf"
    async with httpx.AsyncClient() as client:
        request = await HanaAdapter().prepare_download(client, row)
        assert request.method == "GET"
        assert request.url == row.source_url
        with pytest.raises(ValueError):
            await HanaAdapter().prepare_download(client, replace(row, category="corporate"))
        with pytest.raises(ValueError):
            await HanaAdapter().prepare_download(client, replace(row, category="personal"))
        with pytest.raises(ValueError):
            await HanaAdapter().prepare_download(
                client, replace(row, source_url="http://m.hanacard.co.kr/a.pdf")
            )


@pytest.mark.parametrize("flag", [False, "N"])
async def test_hana_explicit_terminal_flag(flag: bool | str) -> None:
    snapshot = await hana_discover([hana_payload([hana_record()], cursor="unused", hasMore=flag)])
    assert len(snapshot.records) == 1


async def test_hana_selects_latest_with_stable_product_identity() -> None:
    rows = [
        hana_record(AN_SDT="20240101", APN_FILE_NM="old.pdf"),
        hana_record(RG_DTTI="20241108"),
        hana_record(RG_DTTI="20241120"),
    ]
    first = await hana_discover([hana_payload(rows, count="3")])
    second = await hana_discover([hana_payload(list(reversed(rows)), count="3")])
    assert len(first.records) == 1
    assert first.snapshot_id == second.snapshot_id
    assert first.records[0].source_version == "20260901"


async def test_hana_latest_selection_ignores_older_conflicts_in_any_order() -> None:
    rows = [
        hana_record(AN_SDT="20240101", APN_FILE_NM="old-a.pdf"),
        hana_record(AN_SDT="20240101", APN_FILE_NM="old-b.pdf"),
        hana_record(),
    ]
    snapshot = await hana_discover([hana_payload(rows, count="3")])
    reversed_snapshot = await hana_discover([hana_payload(list(reversed(rows)), count="3")])
    assert snapshot.records[0].source_version == "20260901"
    assert snapshot.snapshot_id == reversed_snapshot.snapshot_id


async def test_hana_rejects_conflicting_latest_documents() -> None:
    with pytest.raises(IssuerMarkupChanged, match="conflicting latest"):
        await hana_discover([hana_payload([hana_record(), hana_record(APN_FILE_NM="other.pdf")], count="2")])


@pytest.mark.parametrize(
    ("pages", "message"),
    [
        ([hana_payload([hana_record()], count="2")], "ended before"),
        ([hana_payload([hana_record()], count="0")], "exceeded the reported"),
        ([hana_payload([hana_record()], cursor="next")], "did not terminate"),
        ([hana_payload([], cursor="next", count="2")], "did not terminate"),
        (
            [
                hana_payload([hana_record()], cursor="next", count="3"),
                hana_payload([hana_record("2")], cursor="next", count="0"),
            ],
            "cursor repeated",
        ),
        (
            [
                hana_payload([hana_record()], cursor="next", count="3"),
                hana_payload([hana_record()], cursor="different", count="0"),
            ],
            "identical raw record",
        ),
        (
            [
                hana_payload([hana_record()], cursor="next", count="3"),
                hana_payload([hana_record("2")], count="4"),
            ],
            "total changed",
        ),
    ],
)
async def test_hana_rejects_incomplete_or_repeating_pagination(
    pages: list[dict[str, Any]], message: str
) -> None:
    with pytest.raises(IssuerMarkupChanged, match=message):
        await hana_discover(pages)


@pytest.mark.parametrize(
    "overrides",
    [
        {"ADD_VAR3": ""},
        {"AN_TIT_NM": None},
        {"AN_SDT": "20260230"},
        {"APN_FILE_NM": "../file.pdf"},
        {"APN_FILE_NM": "file.html"},
        {"APN_FILE_PH_NM": "http://m.hanacard.co.kr/leaflet/"},
        {"APN_FILE_PH_NM": "https://attacker.test/leaflet/"},
        {"ADD_VAR5": "unknown"},
    ],
)
def test_hana_rejects_invalid_records(overrides: dict[str, Any]) -> None:
    with pytest.raises(IssuerMarkupChanged):
        parse_hana_listing(hana_payload([hana_record(**overrides)]), discovered_at=NOW)


@pytest.mark.parametrize(
    "overrides",
    [
        {"AMM_NEXT_KEY": None},
        {"listCount": "missing"},
        {"listCount": True},
        {"RESULT_LIST": {}},
        {"hasMore": "unknown"},
        {"hasMore": "Y"},
        {"hasMore": "N", "AMM_EOF_SWITCH": "Y"},
        {"errorCode": "FAIL"},
    ],
)
def test_hana_rejects_invalid_page_contract(overrides: dict[str, Any]) -> None:
    with pytest.raises(IssuerMarkupChanged):
        parse_hana_listing(hana_payload([hana_record()], **overrides), discovered_at=NOW)


async def test_hana_empty_listing_fails_minimum() -> None:
    with pytest.raises(IssuerMarkupChanged, match="expected at least"):
        await hana_discover([hana_payload([], count="0")])


async def test_hana_minimum_applies_to_unique_products() -> None:
    with pytest.raises(IssuerMarkupChanged, match="expected at least"):
        await hana_discover(
            [hana_payload([hana_record(RG_DTTI="20241108"), hana_record(RG_DTTI="20241120")], count="2")],
            minimum=2,
        )


async def test_hana_accepts_distinct_publications_of_same_stable_source_across_pages() -> None:
    snapshot = await hana_discover(
        [
            hana_payload([hana_record(RG_DTTI="20241108")], cursor="next", count="2"),
            hana_payload([hana_record(RG_DTTI="20241120")], count="0"),
        ]
    )
    assert len(snapshot.records) == 1


@pytest.mark.parametrize(
    "pages",
    [
        [hana_payload([hana_record(), hana_record(), hana_record("2")], count="3")],
        [
            hana_payload([hana_record(), hana_record("2")], cursor="next", count="4"),
            hana_payload([hana_record("2"), hana_record("3")], count="0"),
        ],
    ],
)
async def test_hana_rejects_partial_raw_row_replay(pages: list[dict[str, Any]]) -> None:
    with pytest.raises(IssuerMarkupChanged, match="identical raw record"):
        await hana_discover(pages)


async def test_hana_rejects_invalid_encoding() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"landing" if request.method == "GET" else b"\xff")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(IssuerMarkupChanged, match="EUC-KR JSON"):
            await HanaAdapter(minimum_records=1).discover_current(client)
