from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest

from cardrag_worker.issuers.common import IssuerMarkupChanged
from cardrag_worker.issuers.kb import SPEC as KB_SPEC
from cardrag_worker.issuers.kb import KBAdapter, parse_listing
from cardrag_worker.issuers.shinhan import ShinhanAdapter
from cardrag_worker.issuers.woori import DETAIL_PATH, MAIN_PATH, WooriAdapter


class Response:
    def __init__(
        self,
        *,
        payload: Any = None,
        text: str = "",
        content: bytes | None = None,
    ) -> None:
        self.payload = payload
        self.text = text
        self.content = content if content is not None else text.encode()

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self.payload


class WooriClient:
    def __init__(self, *, fail_header: bool = False) -> None:
        self.fail_header = fail_header

    async def get(self, url: str, **kwargs: Any) -> Response:
        return Response(text="landing")

    async def post(self, url: str, **kwargs: Any) -> Response:
        if self.fail_header:
            return Response(payload={"elHeader": {"resSuc": False, "resCode": "E100"}})
        if url.endswith(MAIN_PATH):
            return Response(
                payload={
                    "elHeader": {"resSuc": True},
                    "mainDataList": {
                        "cct11PrdntcAgrmMainVo": [
                            {"goodsDescAt": "Y", "code": "P1", "codeName": "우리 테스트 카드"}
                        ]
                    },
                }
            )
        assert url.endswith(DETAIL_PATH)
        return Response(
            payload={
                "rows": [
                    {
                        "filePath": "/files/v9.pdf",
                        "fileNm": "v9.pdf",
                        "beginDt": "20260825",
                        "gdccVer": "v9",
                    },
                    {
                        "filePath": "/files/v10.pdf",
                        "fileNm": "v10.pdf",
                        "beginDt": "20260825",
                        "gdccVer": "v10",
                    },
                ]
            }
        )


KB_PAGE_1 = """
<table><tr><td>KB 테스트 카드</td>
<td><a href="/obj/card/download/card_20260825.pdf">PDF</a></td>
<td><a href="javascript:goDetail('P1','KB 테스트 카드','dtlView_0');">상세</a></td></tr></table>
<a href="javascript:doSearchSpider('HSHMCXCRSZZC0002','2');">2</a>
"""
KB_PAGE_2 = """
<table><tr><td>KB 두번째 카드</td>
<td><a href="https://img2.kbcard.com/obj/card/download/card_20260826.pdf">PDF</a></td>
<td onclick="goDetail('P2','KB 두번째 카드')">상세</td></tr></table>
"""


class KBClient:
    def __init__(self, *, empty: bool = False) -> None:
        self.empty = empty
        self.posts: list[str] = []

    async def get(self, url: str, **kwargs: Any) -> Response:
        return Response(text="landing")

    async def post(self, url: str, **kwargs: Any) -> Response:
        page = str(kwargs["data"]["pageCount"])
        self.posts.append(page)
        return Response(text="" if self.empty else (KB_PAGE_1 if page == "1" else KB_PAGE_2))


def shinhan_page(code: str, *, cursor: str | None = None, done: bool = False) -> str:
    marker = "<!-- DONE -->" if done else f"<!-- MORE({cursor}) -->"
    return f"""
    <table><tr name="pdPbnRow"><td>신한 {code} 카드 2026.08.25</td>
    <td onclick="fnRetrieveFile('TOKEN-{code}','{code}.pdf')">PDF</td>
    <td onclick="openSvPifPop('{code}','신한 {code} 카드')">이력</td></tr></table>
    {marker}
    """


class ShinhanClient:
    def __init__(self, *, empty: bool = False) -> None:
        self.empty = empty
        self.cursors: list[str] = []

    async def get(self, url: str, **kwargs: Any) -> Response:
        return Response(text="landing")

    async def post(self, url: str, **kwargs: Any) -> Response:
        cursor = str(kwargs["data"]["nxtQyKey"])
        self.cursors.append(cursor)
        if self.empty:
            return Response(content="<!-- DONE -->".encode("euc-kr"))
        html = shinhan_page("P1", cursor="next") if not cursor else shinhan_page("P2", done=True)
        return Response(content=html.encode("euc-kr"))


@pytest.mark.asyncio
async def test_woori_discovery_uses_natural_version_and_post_download_contract() -> None:
    adapter = WooriAdapter(minimum_records=1)
    snapshot = await adapter.discover_current(WooriClient())  # type: ignore[arg-type]
    assert len(snapshot.records) == 1
    assert snapshot.records[0].source_version == "10"
    request = await adapter.prepare_download(WooriClient(), snapshot.records[0])  # type: ignore[arg-type]
    assert request.method == "POST"
    assert request.form is not None and request.form["k01"]


@pytest.mark.asyncio
async def test_woori_api_failure_header_is_fail_closed() -> None:
    with pytest.raises(IssuerMarkupChanged, match="E100"):
        await WooriAdapter().discover_current(WooriClient(fail_header=True))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_kb_pagination_and_direct_get_download_contract() -> None:
    adapter = KBAdapter(minimum_records=1)
    adapter.spec = replace(adapter.spec, categories=("0",))
    client = KBClient()
    snapshot = await adapter.discover_current(client)  # type: ignore[arg-type]
    assert [row.product_code for row in snapshot.records] == ["P1", "P2"]
    assert snapshot.records[0].metadata == {"category_code": "0"}
    assert client.posts == ["1", "2"]
    request = await adapter.prepare_download(client, snapshot.records[0])  # type: ignore[arg-type]
    assert request.method == "GET"


def test_kb_rejects_non_javascript_href_with_handler_substring() -> None:
    html = """
    <table><tr><td>거부할 카드</td>
    <td><a href="/obj/card/download/card_20260825.pdf">PDF</a></td>
    <td><a href="https://invalid.example/goDetail('BAD','거부할 카드')">상세</a></td>
    </tr></table>
    """
    assert parse_listing(html, category_code="0", discovered_at=datetime.now(UTC)) == []


def test_kb_protected_sources_are_exact_current_byte_identities() -> None:
    assert tuple(item.contract_payload for item in KB_SPEC.protected_source_allowances) == (
        {
            "magic": "SCDSA002",
            "product_code": "04130",
            "sha256": "143f5afb2ab9a974e76cd7f3099b2affb69d55154fe131fb653d3cbc5dd6571e",
            "size_bytes": 545086,
            "source_id": "source_532c32eb860fd121fab8da902a41509ef51194ff90baa42cc7779f3cf91ef1c6",
            "source_url": "https://img2.kbcard.com/obj/card/download/_04130__prdctOpmn_20260204.pdf",
            "source_version": "20260204",
        },
        {
            "magic": "SCDSA002",
            "product_code": "04292",
            "sha256": "8357e5d0d8bb03e03b8388144db3be0d393d9f623ac4c883a2a9beca10f2ae45",
            "size_bytes": 691230,
            "source_id": "source_f9071260e77356c3e87d35df8920079899de77e48af0268a94420c65b9c95b5b",
            "source_url": "https://img2.kbcard.com/obj/card/download/04292__prdctOpmn_20260303.pdf",
            "source_version": "20260303",
        },
        {
            "magic": "SCDSA002",
            "product_code": "04460",
            "sha256": "46f6f921a8399c4f6f917aa929a01569f6442f27af9e5ded92f05c2272c66064",
            "size_bytes": 827358,
            "source_id": "source_5d449cf9fa203e64bed7894b2c071f885457fa426237c27abb2bd9168d062056",
            "source_url": "https://img2.kbcard.com/obj/card/download/04460__prdctOpmn_20260204.pdf",
            "source_version": "20260204",
        },
    )


@pytest.mark.asyncio
async def test_shinhan_cursor_and_post_download_contract() -> None:
    adapter = ShinhanAdapter(minimum_records=1)
    adapter.spec = replace(adapter.spec, categories=("credit",))
    client = ShinhanClient()
    snapshot = await adapter.discover_current(client)  # type: ignore[arg-type]
    assert [row.product_code for row in snapshot.records] == ["P1", "P2"]
    assert client.cursors == ["", "next"]
    request = await adapter.prepare_download(client, snapshot.records[0])  # type: ignore[arg-type]
    assert request.method == "POST"
    assert request.form == {"filNm": "TOKEN-P1", "pbnNm": "P1.pdf"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("adapter", "client"),
    [
        (KBAdapter(minimum_records=1), KBClient(empty=True)),
        (ShinhanAdapter(minimum_records=1), ShinhanClient(empty=True)),
    ],
)
async def test_empty_or_changed_markup_fails_minimum(adapter: Any, client: Any) -> None:
    adapter.spec = replace(adapter.spec, categories=(adapter.spec.categories[0],))
    with pytest.raises(IssuerMarkupChanged):
        await adapter.discover_current(client)
