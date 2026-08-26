from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from cardrag_worker.issuers.common import IssuerMarkupChanged
from cardrag_worker.issuers.kb import SPEC as KB_SPEC
from cardrag_worker.issuers.kb import KBAdapter, parse_listing
from cardrag_worker.issuers.shinhan import (
    DOWNLOAD_NAME_METADATA_KEY,
    MOBILE_DOWNLOAD_PATH,
    MOBILE_LIST_PATH,
    MOBILE_NOTICE_PATH,
    REFRESH_MAXIMUM_PAGES,
    ShinhanAdapter,
    _parse_mobile_listing,
)
from cardrag_worker.issuers.woori import DETAIL_PATH, MAIN_PATH, WooriAdapter, parse_detail_records
from cardrag_worker.issuers.woori import SPEC as WOORI_SPEC


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


def shinhan_page(
    code: str,
    *,
    tokens: tuple[str, ...] | None = None,
    download_name: str | None = None,
    download_names: tuple[str, ...] | None = None,
    product_name: str | None = None,
    cursor: str | None = None,
    done: bool = False,
) -> str:
    marker = "<!-- DONE -->" if done else f"<!-- MORE({cursor}) -->"
    current_tokens = tokens or (f"TOKEN-{code}",)
    current_download_name = download_name or f"{code} 안내장"
    current_download_names = download_names or tuple(current_download_name for _ in current_tokens)
    if len(current_download_names) != len(current_tokens):
        raise ValueError("test fixture token and download-name counts differ")
    current_product_name = product_name or f"신한 {code} 카드"
    rows = "".join(
        f"""
        <tr name="pdPbnRow"><td>{current_product_name}</td><td>2026.08.25</td>
        <td onclick="fnRetrieveFile('{token}','{row_download_name}')">PDF</td>
        <td onclick="openSvPifPop('{code}','{current_product_name}')">이력</td></tr>
        """
        for token, row_download_name in zip(current_tokens, current_download_names, strict=True)
    )
    return f"""
    <table>{rows}</table>
    {marker}
    """


def shinhan_mobile_payload(
    code: str = "P1",
    *,
    tokens: tuple[str, ...] = ("TOKEN-REFRESHED",),
    product_name: str = "신한 P1 카드",
    cursor: str = "",
    category_code: str = "0",
    query: str = "신한 P1 카드",
) -> dict[str, Any]:
    return {
        "mbw_result": "S",
        "mbw_json": {
            "crdPdPbnList": [
                {
                    "CRD_PD_GUI_N": code,
                    "CRD_PD_GUI_NM": product_name,
                    "CRD_PD_GUI_BUL_D": "2026.08.25",
                    "CRD_PD_GUI_FIL_NM": token,
                }
                for token in tokens
            ],
            "data": {
                "nxtQyKey": cursor,
                "crdTcd": category_code,
                "crdPdGuiNm": query,
            },
        },
    }


class ShinhanClient:
    def __init__(
        self,
        *,
        empty: bool = False,
        initial_fil_nm_prefix: str = "FILNM",
        refresh_code: str = "P1",
        refresh_tokens: tuple[str, ...] = ("TOKEN-REFRESHED",),
        refresh_download_name: str = "신한 P1 카드",
    ) -> None:
        self.empty = empty
        self.initial_fil_nm_prefix = initial_fil_nm_prefix
        self.refresh_code = refresh_code
        self.refresh_tokens = refresh_tokens
        self.refresh_download_name = refresh_download_name
        self.cursors: list[str] = []
        self.searches: list[str] = []
        self.gets = 0

    async def get(self, url: str, **kwargs: Any) -> Response:
        self.gets += 1
        return Response(text="landing")

    async def post(self, url: str, **kwargs: Any) -> Response:
        cursor = str(kwargs["data"]["nxtQyKey"])
        search = str(kwargs["data"]["crdPdGuiNm"])
        self.cursors.append(cursor)
        self.searches.append(search)
        if url.endswith(MOBILE_LIST_PATH):
            return Response(
                payload=shinhan_mobile_payload(
                    self.refresh_code,
                    tokens=self.refresh_tokens,
                    product_name=self.refresh_download_name,
                    category_code=str(kwargs["data"]["crdTcd"]),
                    query=search,
                )
            )
        if self.empty:
            return Response(content="<!-- DONE -->".encode("euc-kr"))
        if search:
            html = shinhan_page(
                self.refresh_code,
                tokens=self.refresh_tokens,
                download_name=self.refresh_download_name,
                done=True,
            )
        elif not cursor:
            html = shinhan_page(
                "P1",
                tokens=(f"{self.initial_fil_nm_prefix}-P1",),
                cursor="next",
            )
        else:
            html = shinhan_page(
                "P2",
                tokens=(f"{self.initial_fil_nm_prefix}-P2",),
                done=True,
            )
        return Response(content=html.encode("euc-kr"))


class ShinhanConflictingDiscoveryClient(ShinhanClient):
    async def post(self, url: str, **kwargs: Any) -> Response:
        html = shinhan_page(
            "P1",
            tokens=("FILNM-A", "FILNM-B"),
            download_names=("P1 안내장", "P1 변경 안내장"),
            done=True,
        )
        return Response(content=html.encode("euc-kr"))


class ShinhanUnboundedRefreshClient:
    def __init__(self, *, repeat_cursor: bool = False) -> None:
        self.repeat_cursor = repeat_cursor
        self.posts = 0
        self.gets = 0

    async def get(self, url: str, **kwargs: Any) -> Response:
        self.gets += 1
        return Response(text="landing")

    async def post(self, url: str, **kwargs: Any) -> Response:
        assert kwargs["data"]["crdPdGuiNm"] == "신한 P1 카드"
        self.posts += 1
        next_cursor = "repeat" if self.repeat_cursor else str(self.posts)
        return Response(
            payload=shinhan_mobile_payload(
                tokens=(f"FILNM-REFRESH-{self.posts}",),
                cursor=next_cursor,
                query="신한 P1 카드",
            )
        )


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


def test_woori_protected_sources_are_exact_current_byte_identities() -> None:
    assert tuple(item.contract_payload for item in WOORI_SPEC.protected_source_allowances) == (
        {
            "magic": "FASOO_DRMONE",
            "product_code": "102958",
            "sha256": "ea143c393bed26325e75ed55be266c07281678a1e37cac0fc53d6b248c3f6f46",
            "size_bytes": 2_132_864,
            "source_id": "source_854d1b4effb9473acc693aacc76484de24bfb658fb31df126b908d093a4e815a",
            "source_url": (
                "https://pc.wooricard.com/upload/cardClause/2024/3/19/"
                "574d01a3-65d0-42f3-bec0-e82c77710f49.pdf"
            ),
            "source_version": "2",
        },
        {
            "magic": "FASOO_DRMONE",
            "product_code": "203988",
            "sha256": "1fefe29375616a983941675b6394e5f8406f601cef362ffb799cd61f49c8f0e3",
            "size_bytes": 1_097_360,
            "source_id": "source_caa72dffbeafe404460e0a0a427467b5d99ee619c802a4b6db77719f666d0854",
            "source_url": (
                "https://pc.wooricard.com/upload/cardClause/2021/10/5/"
                "be9faab5-be15-4a2a-9db1-9189aafc7852.pdf"
            ),
            "source_version": "11",
        },
        {
            "magic": "SCDSA004",
            "product_code": "832388",
            "sha256": "dfba0d8a61a0ec3783f133be6e45c2e465a6bcbd83f056b6b68048a0643e5c61",
            "size_bytes": 417_596,
            "source_id": "source_8aea463c3fc9d8e43ae5dd894af4ca54d47686dc9a77326d1ab6576f34f074bc",
            "source_url": (
                "https://pc.wooricard.com/upload/cardClause/2026/8/11/"
                "f1f9d290-30e9-43ea-abe0-6f43e947eaa5.pdf"
            ),
            "source_version": "11",
        },
    )


@pytest.mark.parametrize(
    ("product_code", "product_name", "file_path", "file_name", "begin", "version"),
    (
        (
            "102958",
            "KREAM 우리카드_메탈",
            "/upload/cardClause/2024/3/19/574d01a3-65d0-42f3-bec0-e82c77710f49.pdf",
            "3.상품안내장_KREAM 우리카드(메탈).pdf",
            "20240320",
            "2",
        ),
        (
            "203988",
            "우리V카드 Oil 100",
            "/upload/cardClause/2021/10/5/be9faab5-be15-4a2a-9db1-9189aafc7852.pdf",
            "203988_우리V카드 Oil 100_210909 .pdf",
            "20210924",
            "11",
        ),
        (
            "832388",
            "위메프 우리체크",
            "/upload/cardClause/2026/8/11/f1f9d290-30e9-43ea-abe0-6f43e947eaa5.pdf",
            "7.(832388)위메프 우리체크_202311.pdf",
            "20230102",
            "11",
        ),
    ),
)
def test_woori_current_protected_detail_rows_recalculate_allowlisted_source_ids(
    product_code: str,
    product_name: str,
    file_path: str,
    file_name: str,
    begin: str,
    version: str,
) -> None:
    records = parse_detail_records(
        {
            "rows": [
                {
                    "filePath": file_path,
                    "fileNm": file_name,
                    "beginDt": begin,
                    "gdccVer": version,
                }
            ]
        },
        product_code=product_code,
        product_name=product_name,
        discovered_at=datetime(2026, 8, 26, tzinfo=UTC),
    )
    allowance = next(
        item for item in WOORI_SPEC.protected_source_allowances if item.product_code == product_code
    )

    assert len(records) == 1
    assert records[0].source_id == allowance.source_id
    assert records[0].source_url == allowance.source_url
    assert records[0].source_version == allowance.source_version


@pytest.mark.asyncio
async def test_shinhan_cursor_and_mobile_get_download_contract() -> None:
    adapter = ShinhanAdapter(minimum_records=1)
    adapter.spec = replace(adapter.spec, categories=("credit",))
    client = ShinhanClient()
    snapshot = await adapter.discover_current(client)  # type: ignore[arg-type]
    assert [row.product_code for row in snapshot.records] == ["P1", "P2"]
    assert client.cursors == ["", "next"]
    assert snapshot.records[0].source_post_id == "credit:P1"
    assert snapshot.records[0].file_name == "P1 안내장.pdf"
    assert snapshot.records[0].metadata == {DOWNLOAD_NAME_METADATA_KEY: "P1 안내장"}
    request = await adapter.prepare_download(client, snapshot.records[0])  # type: ignore[arg-type]
    parsed = urlsplit(request.url)
    assert request.method == "GET"
    assert request.form is None
    assert parsed.path == MOBILE_DOWNLOAD_PATH
    assert parse_qs(parsed.query) == {
        "aFilenm": ["TOKEN-REFRESHED"],
        "pbnNm": ["신한 P1 카드"],
    }
    assert request.headers == {"Referer": "https://www.shinhancard.com" + MOBILE_NOTICE_PATH + "?page=CRE"}
    assert client.searches == ["", "", "신한 P1 카드"]
    assert client.gets == 2


@pytest.mark.asyncio
async def test_shinhan_token_rotation_does_not_change_source_or_snapshot_identity() -> None:
    adapter = ShinhanAdapter(minimum_records=1)
    adapter.spec = replace(adapter.spec, categories=("credit",))
    first = await adapter.discover_current(
        ShinhanClient(initial_fil_nm_prefix="FILNM-OLD")  # type: ignore[arg-type]
    )
    rotated = await adapter.discover_current(
        ShinhanClient(initial_fil_nm_prefix="FILNM-ROTATED")  # type: ignore[arg-type]
    )

    assert [row.source_id for row in first.records] == [row.source_id for row in rotated.records]
    assert first.snapshot_id == rotated.snapshot_id
    assert "FILNM-OLD" not in str(first.payload)
    assert "FILNM-ROTATED" not in str(rotated.payload)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "client",
    [
        ShinhanClient(refresh_download_name="P1 변경 안내장"),
        ShinhanClient(refresh_tokens=("TOKEN-A", "TOKEN-B")),
    ],
    ids=("stable-source-mismatch", "duplicate-stable-source"),
)
async def test_shinhan_download_refresh_requires_one_exact_stable_match(client: ShinhanClient) -> None:
    adapter = ShinhanAdapter(minimum_records=1)
    adapter.spec = replace(adapter.spec, categories=("credit",))
    snapshot = await adapter.discover_current(client)  # type: ignore[arg-type]

    with pytest.raises(IssuerMarkupChanged, match="exactly one stable source match"):
        await adapter.prepare_download(client, snapshot.records[0])  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "payload",
    [
        {"mbw_result": "E", "mbw_json": {}},
        {"mbw_result": "S", "mbw_json": {"crdPdPbnList": [], "data": {}}},
        shinhan_mobile_payload(query="different product"),
        shinhan_mobile_payload(tokens=("",)),
    ],
    ids=("result-failure", "missing-binding", "query-mismatch", "empty-token"),
)
def test_shinhan_mobile_listing_contract_fails_closed(payload: object) -> None:
    with pytest.raises(IssuerMarkupChanged, match="mobile disclosure"):
        _parse_mobile_listing(payload, category="credit", product_name="신한 P1 카드")


@pytest.mark.asyncio
async def test_shinhan_discovery_rejects_conflicting_stable_sources() -> None:
    adapter = ShinhanAdapter(minimum_records=1)
    adapter.spec = replace(adapter.spec, categories=("credit",))

    with pytest.raises(IssuerMarkupChanged, match="conflicting stable source"):
        await adapter.discover_current(ShinhanConflictingDiscoveryClient())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_shinhan_download_refresh_rejects_repeated_cursor() -> None:
    adapter = ShinhanAdapter(minimum_records=1)
    adapter.spec = replace(adapter.spec, categories=("credit",))
    snapshot = await adapter.discover_current(ShinhanClient())  # type: ignore[arg-type]
    client = ShinhanUnboundedRefreshClient(repeat_cursor=True)

    with pytest.raises(IssuerMarkupChanged, match="cursor repeated"):
        await adapter.prepare_download(client, snapshot.records[0])  # type: ignore[arg-type]
    assert client.posts == 2


@pytest.mark.asyncio
async def test_shinhan_download_refresh_is_strictly_page_bounded() -> None:
    adapter = ShinhanAdapter(minimum_records=1)
    adapter.spec = replace(adapter.spec, categories=("credit",))
    snapshot = await adapter.discover_current(ShinhanClient())  # type: ignore[arg-type]
    client = ShinhanUnboundedRefreshClient()

    with pytest.raises(IssuerMarkupChanged, match="page limit"):
        await adapter.prepare_download(client, snapshot.records[0])  # type: ignore[arg-type]
    assert client.posts == REFRESH_MAXIMUM_PAGES


@pytest.mark.asyncio
async def test_shinhan_download_rejects_wrong_issuer_or_category_before_network() -> None:
    adapter = ShinhanAdapter(minimum_records=1)
    adapter.spec = replace(adapter.spec, categories=("credit",))
    client = ShinhanClient()
    snapshot = await adapter.discover_current(client)  # type: ignore[arg-type]
    source = snapshot.records[0]
    calls_before = (client.gets, len(client.cursors))

    with pytest.raises(ValueError, match="issuer"):
        await adapter.prepare_download(client, replace(source, issuer="kb"))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="category"):
        await adapter.prepare_download(client, replace(source, category="corporate"))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="discovery identity"):
        await adapter.prepare_download(  # type: ignore[arg-type]
            client,
            replace(source, source_url="https://www.shinhancard.com/changed"),
        )
    with pytest.raises(ValueError, match="discovery identity"):
        await adapter.prepare_download(  # type: ignore[arg-type]
            client,
            replace(source, metadata={DOWNLOAD_NAME_METADATA_KEY: "changed"}),
        )
    assert (client.gets, len(client.cursors)) == calls_before


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
