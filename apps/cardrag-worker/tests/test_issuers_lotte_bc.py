from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime
from html import escape
from typing import Any
from urllib.parse import parse_qs, unquote

import httpx
import pytest

from cardrag_worker.issuers.bc import BCAdapter
from cardrag_worker.issuers.bc import parse_listing as parse_bc_listing
from cardrag_worker.issuers.common import IssuerMarkupChanged
from cardrag_worker.issuers.lotte import LotteAdapter
from cardrag_worker.issuers.lotte import parse_listing_page as parse_lotte_page

NOW = datetime(2026, 9, 6, tzinfo=UTC)


def lotte_row(post_id: str = "1924", **overrides: Any) -> dict[str, Any]:
    return {
        "DOCID": post_id,
        "VT_CD_KND_NM": "롯데 테스트 카드",
        "OCY_FILE_NM": "상품 안내장 (최종)+.pdf",
        "VT_CD_KND_C": "",
        "BULT_SDT": "20260904",
        "ISU_E_YN": "N",
        **overrides,
    }


def lotte_payload(
    rows: list[dict[str, Any]], *, offset: int = 0, total: int | None = None, page_size: int = 100
) -> dict[str, Any]:
    total = len(rows) if total is None else total
    return {
        "Status": {"code": 0},
        "Content": json.dumps(
            {
                "result": {
                    "totalcount": str(total),
                    "parameter": {
                        "collection": "disclosure",
                        "query": "",
                        "startcount": str(offset // page_size),
                    },
                    "collection": [
                        {"id": "disclosure", "totalcount": str(total), "count": str(len(rows)), "docs": rows}
                    ],
                }
            },
            ensure_ascii=False,
        ),
    }


def bc_row(
    *,
    name: str = "BC 테스트 카드",
    launch: str = "2020.12.03",
    ended: str = "-",
    number: str = "1",
    guides: tuple[tuple[str, str], ...] = (("안내장 받기", "상품 안내장.pdf"),),
) -> str:
    links = "".join(
        f'<a href="{escape(path if ":" in path or path.startswith("/") else "/down/individual/customer/" + path)}">'
        f"{escape(label)}</a>"
        for label, path in guides
    )
    return (
        f"<tr><td>{number}</td><td>{escape(name)}</td>"
        '<td><a href="/down/individual/customer/terms.pdf">약관</a></td>'
        f"<td>{links}</td>"
        '<td><a href="/down/individual/customer/old.pdf">상품개정</a></td>'
        f"<td>{launch}</td><td>{ended}</td></tr>"
    )


def bc_html(*rows: str) -> str:
    headings = "".join(
        f"<th>{label}</th>"
        for label in ("No.", "상품명", "약관", "상품 안내장", "개정이력", "상품출시일", "발급중단일")
    )
    return f'<table class="tbColAc"><thead><tr>{headings}</tr></thead><tbody>{"".join(rows)}</tbody></table>'


@pytest.mark.asyncio
async def test_lotte_offset_advances_by_actual_page_size_and_keeps_discontinued_products() -> None:
    offsets: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="landing")
        data = parse_qs(request.content.decode(), keep_blank_values=True)
        offset = int(data["startcount"][0])
        offsets.append(offset)
        assert data["query"] == [""]
        assert data["collection"] == ["disclosure"]
        assert request.headers["X-Requested-With"] == "XMLHttpRequest"
        page = [lotte_row("1", ISU_E_YN="Y")] if offset == 0 else [lotte_row("2"), lotte_row("3")]
        return httpx.Response(200, json=lotte_payload(page, offset=offset, total=3))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = LotteAdapter(minimum_records=1, page_size=100)
        snapshot = await adapter.discover_current(client)
        assert offsets == [0, 1]
        assert [record.product_code for record in snapshot.records] == ["1", "2", "3"]
        assert adapter.spec.categories == ("shared",)
        assert all(record.category == "shared" for record in snapshot.records)
        assert all(
            record.metadata["classification_basis"] == "official_shared_listing"
            for record in snapshot.records
        )
        assert snapshot.records[0].metadata["issuance_ended"] is True
        request = await adapter.prepare_download(client, snapshot.records[0])
        assert request.method == "GET"
        assert request.form is None
        assert unquote(request.url).endswith("상품 안내장 (최종)+.pdf")
        assert "%20" in request.url and "%2B" in request.url


def test_lotte_docid_stays_product_identity_when_official_code_appears_or_file_changes() -> None:
    _, first = parse_lotte_page(lotte_payload([lotte_row()]), offset=0, page_size=100, discovered_at=NOW)
    _, updated = parse_lotte_page(
        lotte_payload([lotte_row(VT_CD_KND_C="P123", OCY_FILE_NM="revised.pdf", BULT_SDT="20260905")]),
        offset=0,
        page_size=100,
        discovered_at=NOW,
    )
    assert first[0].product_code == updated[0].product_code == "1924"
    assert updated[0].metadata["official_product_code"] == "P123"
    assert first[0].source_id != updated[0].source_id
    assert updated[0].effective_date == date(2026, 9, 5)
    assert updated[0].source_version == "20260905"


def test_lotte_response_echoes_page_index_for_requested_row_offset() -> None:
    payload = lotte_payload([lotte_row()], offset=100, total=101)
    assert json.loads(payload["Content"])["result"]["parameter"]["startcount"] == "1"
    total, records = parse_lotte_page(payload, offset=100, page_size=100, discovered_at=NOW)
    assert total == 101 and len(records) == 1


@pytest.mark.parametrize("field", ["root_total", "collection_count", "collection_identity"])
def test_lotte_rejects_inconsistent_page_contract(field: str) -> None:
    payload = lotte_payload([lotte_row()])
    content = json.loads(payload["Content"])
    result = content["result"]
    if field == "root_total":
        result["totalcount"] = "2"
    elif field == "collection_count":
        result["collection"][0]["count"] = "2"
    else:
        result["collection"][0]["id"] = "other"
    payload["Content"] = json.dumps(content)
    with pytest.raises(IssuerMarkupChanged):
        parse_lotte_page(payload, offset=0, page_size=100, discovered_at=NOW)


@pytest.mark.parametrize(
    "overrides",
    [
        {"DOCID": ""},
        {"DOCID": "abc"},
        {"VT_CD_KND_NM": ""},
        {"BULT_SDT": ""},
        {"BULT_SDT": "20260230"},
        {"OCY_FILE_NM": "../bad.pdf"},
        {"OCY_FILE_NM": "bad.txt"},
        {"OCY_FILE_NM": "https://evil.example/a.pdf"},
        {"ISU_E_YN": "unknown"},
        {"VT_CD_KND_C": None},
    ],
)
def test_lotte_required_fields_fail_closed(overrides: dict[str, Any]) -> None:
    with pytest.raises(IssuerMarkupChanged):
        parse_lotte_page(lotte_payload([lotte_row(**overrides)]), offset=0, page_size=100, discovered_at=NOW)


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {"Status": {"code": 1}, "Content": "{}"},
        {"Status": {"code": 0}, "Content": {}},
        {"Status": {"code": 0}, "Content": "not JSON"},
        lotte_payload([lotte_row()], offset=100),
        lotte_payload([lotte_row()], total=0),
    ],
)
def test_lotte_invalid_response_and_pagination_binding_fail_closed(payload: object) -> None:
    with pytest.raises(IssuerMarkupChanged):
        parse_lotte_page(payload, offset=0, page_size=100, discovered_at=NOW)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["repeated", "early", "changed_total", "page_limit", "record_limit"])
async def test_lotte_pagination_cannot_silently_drop_or_repeat_products(failure: str) -> None:
    posts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        if request.method == "GET":
            return httpx.Response(200, text="landing")
        offset = int(parse_qs(request.content.decode())["startcount"][0])
        posts += 1
        rows = [lotte_row(str(posts))]
        total = 3
        if posts == 2:
            if failure == "repeated":
                rows = [lotte_row("1", OCY_FILE_NM="conflicting.pdf")]
            elif failure == "early":
                rows = []
            elif failure == "changed_total":
                total = 4
        return httpx.Response(200, json=lotte_payload(rows, offset=offset, total=total, page_size=1))

    adapter = LotteAdapter(
        minimum_records=1,
        page_size=1,
        maximum_pages=1 if failure == "page_limit" else 10,
        maximum_records=2 if failure == "record_limit" else 100,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(IssuerMarkupChanged):
            await adapter.discover_current(client)


def lotte_unavailable_rows() -> list[dict[str, Any]]:
    return [
        lotte_row(
            "1603",
            VT_CD_KND_NM="이엠이코리아 롯데카드 아임원더풀",
            OCY_FILE_NM="이엠이코리아 Wonderful_가이드북_220120OL.pdf",
            BULT_SDT="20190730",
            ISU_E_YN="Y",
        ),
        lotte_row(
            "1678",
            VT_CD_KND_NM="캐시노트 롯데카드",
            OCY_FILE_NM="캐시노트 상품안내장 _230310OL.pdf",
            BULT_SDT="20210930",
        ),
    ]


@pytest.mark.asyncio
async def test_lotte_excludes_only_exact_reviewed_404s_after_validating_full_inventory() -> None:
    offsets: list[int] = []
    probes: list[str] = []
    rows = lotte_unavailable_rows() + [lotte_row()]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "image.lottecard.co.kr":
            assert offsets == [0, 1, 2]
            probes.append(str(request.url))
            return httpx.Response(404)
        if request.method == "GET":
            return httpx.Response(200, text="landing")
        offset = int(parse_qs(request.content.decode())["startcount"][0])
        offsets.append(offset)
        return httpx.Response(
            200, json=lotte_payload(rows[offset : offset + 1], offset=offset, total=3, page_size=1)
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        snapshot = await LotteAdapter(minimum_records=1, page_size=1).discover_current(client)
    assert [record.product_code for record in snapshot.records] == ["1924"]
    assert len(probes) == len(snapshot.warnings) == 2
    assert "product_code=1603 source_id=source_3942b85" in snapshot.warnings[0]
    assert "product_code=1678 source_id=source_29e7ea5" in snapshot.warnings[1]
    assert all("source_unavailable" in warning and "status=404" in warning for warning in snapshot.warnings)


@pytest.mark.asyncio
async def test_lotte_restores_reviewed_sources_when_current_urls_recover() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, content=b"%PDF-current")
        return httpx.Response(200, json=lotte_payload(lotte_unavailable_rows()))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        snapshot = await LotteAdapter(minimum_records=1).discover_current(client)
        assert [record.product_code for record in snapshot.records] == ["1603", "1678"]
        assert not snapshot.warnings
        for record in snapshot.records:
            download = await LotteAdapter().prepare_download(client, record)
            assert download.url == record.source_url


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"DOCID": "1679"},
        {"BULT_SDT": "20261001"},
        {"OCY_FILE_NM": "revised.pdf"},
        {"VT_CD_KND_NM": "변경된 상품명"},
        {"VT_CD_KND_C": "P13595-A13595"},
        {"ISU_E_YN": "Y"},
    ],
)
async def test_lotte_changed_source_identity_retains_normal_download_failures(
    changes: dict[str, str],
) -> None:
    row = {**lotte_unavailable_rows()[1], **changes}
    probes = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal probes
        if request.url.host == "image.lottecard.co.kr":
            probes += 1
            return httpx.Response(404)
        if request.method == "GET":
            return httpx.Response(200, text="landing")
        return httpx.Response(200, json=lotte_payload([row]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = LotteAdapter(minimum_records=1)
        snapshot = await adapter.discover_current(client)
        assert probes == 0
        assert len(snapshot.records) == 1 and not snapshot.warnings
        download = await adapter.prepare_download(client, snapshot.records[0])
        response = await client.get(download.url)
        with pytest.raises(httpx.HTTPStatusError):
            response.raise_for_status()
    assert probes == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [302, 403, 410, 429, 500])
async def test_lotte_reviewed_sources_reject_other_statuses_without_following_redirects(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host in {"www.lottecard.co.kr", "image.lottecard.co.kr"}
        if request.url.host == "image.lottecard.co.kr":
            return httpx.Response(status, headers={"Location": "https://untrusted.example/file.pdf"})
        if request.method == "GET":
            return httpx.Response(200, text="landing")
        return httpx.Response(200, json=lotte_payload(lotte_unavailable_rows()))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await LotteAdapter(minimum_records=1).discover_current(client)


@pytest.mark.asyncio
async def test_lotte_minimum_count_is_enforced_after_reviewed_omissions() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "image.lottecard.co.kr":
            return httpx.Response(404)
        if request.method == "GET":
            return httpx.Response(200, text="landing")
        return httpx.Response(200, json=lotte_payload(lotte_unavailable_rows()))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(IssuerMarkupChanged, match="yielded 0 records"):
            await LotteAdapter(minimum_records=1).discover_current(client)


def test_bc_uses_only_guide_column_preserves_launch_basis_and_discontinued_status() -> None:
    listing = parse_bc_listing(
        bc_html(bc_row(ended="개인 :<br/>2024.03.31<br/>기업 : 유지")), discovered_at=NOW
    )
    assert listing.product_rows == 1
    assert len(listing.records) == 1
    record = listing.records[0]
    assert record.file_name == "상품 안내장.pdf"
    assert record.effective_date == date(2020, 12, 3)
    assert unquote(record.source_version) == "/down/individual/customer/상품 안내장.pdf"
    assert record.metadata["date_basis"] == "product_launch"
    assert record.metadata["issuance_end_date"] == "2024-03-31"
    assert record.metadata["issuance_ended"] is True
    assert record.metadata["issuance_end_status"] == "개인 : 2024.03.31 기업 : 유지"


def test_bc_keeps_distinct_guides_but_excludes_application_and_corporate_files() -> None:
    guides = (
        ("안내장 받기(상품)", "product.pdf"),
        ("안내장 받기(브랜드_기본)", "brand-basic.pdf"),
        ("안내장 받기(브랜드_선택)", "brand-choice.pdf"),
        ("신청서 받기", "application.pdf"),
        ("안내장 받기(법인)", "corporate.pdf"),
    )
    listing = parse_bc_listing(bc_html(bc_row(guides=guides)), discovered_at=NOW)
    assert len(listing.records) == 3
    assert len({record.product_code for record in listing.records}) == 3
    assert len({record.metadata["root_product_code"] for record in listing.records}) == 1
    assert listing.excluded_application_forms == 1
    assert listing.excluded_corporate_guides == 1
    assert all(record.product_name.startswith("BC 테스트 카드 (") for record in listing.records)


def test_bc_product_identity_survives_reordering_revision_and_new_variant() -> None:
    first = parse_bc_listing(
        bc_html(bc_row(name="ＢＣ  테스트 카드", guides=(("안내장 받기(저축)", "old.pdf"),))),
        discovered_at=NOW,
    ).records[0]
    updated = parse_bc_listing(
        bc_html(
            bc_row(name="다른 카드", number="1"),
            bc_row(
                name="BC 테스트 카드",
                number="92",
                guides=(("안내장 받기(우체국)", "post.pdf"), ("안내장 받기(저축)", "new.pdf")),
            ),
        ),
        discovered_at=NOW,
    ).records[-1]
    assert first.product_code == updated.product_code
    assert first.metadata["root_product_code"] == updated.metadata["root_product_code"]
    assert first.source_version != updated.source_version
    assert first.source_id != updated.source_id


@pytest.mark.parametrize(
    "html",
    [
        "<html>changed</html>",
        bc_html(bc_row()).replace("상품 안내장", "알 수 없음"),
        bc_html(bc_row(name="")),
        bc_html(bc_row(launch="")),
        bc_html(bc_row(launch="2020.02.30")),
        bc_html(bc_row(ended="invalid")),
        bc_html(bc_row(guides=())),
        bc_html(bc_row(guides=(("안내장 받기", "https://evil.example/a.pdf"),))),
        bc_html(bc_row(guides=(("안내장 받기", "http://www.bccard.com/a.pdf"),))),
        bc_html(bc_row(guides=(("안내장 받기", "/other/location.pdf"),))),
        bc_html(bc_row(guides=(("안내장 받기", "a.txt"),))),
        bc_html(bc_row(guides=(("알 수 없음", "a.pdf"),))),
        bc_html(bc_row(guides=(("안내장 받기", "a.pdf"), ("안내장 받기", "b.pdf")))),
        bc_html(bc_row(), bc_row(number="2", guides=(("안내장 받기", "conflict.pdf"),))),
    ],
)
def test_bc_changed_markup_invalid_fields_and_conflicting_identities_fail_closed(html: str) -> None:
    with pytest.raises(IssuerMarkupChanged):
        parse_bc_listing(html, discovered_at=NOW)


@pytest.mark.asyncio
async def test_bc_discovery_download_and_minimum_apply_to_products_not_guide_variants() -> None:
    html = bc_html(bc_row(guides=(("안내장 받기(저축)", "a.pdf"), ("안내장 받기(우체국)", "b.pdf"))))
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=html))
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = BCAdapter(minimum_records=1)
        snapshot = await adapter.discover_current(client)
        assert len(snapshot.records) == 2
        assert "1 product rows; selected 2 guides" in snapshot.warnings[0]
        request = await adapter.prepare_download(client, snapshot.records[0])
        assert request.method == "GET" and request.form is None
        with pytest.raises(IssuerMarkupChanged, match="expected at least 2"):
            await BCAdapter(minimum_records=2).discover_current(client)


@pytest.mark.asyncio
@pytest.mark.parametrize("issuer", ["lotte", "bc"])
async def test_new_adapter_empty_listing_fails_minimum(issuer: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if issuer == "bc":
            return httpx.Response(200, text=bc_html())
        if request.method == "GET":
            return httpx.Response(200, text="landing")
        return httpx.Response(200, json=lotte_payload([]))

    adapter = LotteAdapter(minimum_records=1) if issuer == "lotte" else BCAdapter(minimum_records=1)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(IssuerMarkupChanged, match="expected at least 1"):
            await adapter.discover_current(client)


@pytest.mark.asyncio
@pytest.mark.parametrize("issuer", ["lotte", "bc"])
async def test_new_adapter_download_rejects_wrong_binding_without_network(issuer: str) -> None:
    if issuer == "lotte":
        adapter: LotteAdapter | BCAdapter = LotteAdapter()
        _, records = parse_lotte_page(
            lotte_payload([lotte_row()]), offset=0, page_size=100, discovered_at=NOW
        )
        record = records[0]
    else:
        adapter = BCAdapter()
        record = parse_bc_listing(bc_html(bc_row()), discovered_at=NOW).records[0]

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("prepare_download should not make a network request")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="issuer"):
            await adapter.prepare_download(client, replace(record, issuer="kb"))
        with pytest.raises(ValueError, match="category"):
            await adapter.prepare_download(client, replace(record, category="corporate"))
        if issuer == "lotte":
            with pytest.raises(ValueError, match="category"):
                await adapter.prepare_download(client, replace(record, category="personal"))
        with pytest.raises(ValueError):
            await adapter.prepare_download(client, replace(record, source_url="https://evil.example/a.pdf"))


@pytest.mark.parametrize("adapter", [LotteAdapter, BCAdapter])
def test_new_adapter_requires_allowed_https_base_url(adapter: type[LotteAdapter] | type[BCAdapter]) -> None:
    with pytest.raises(ValueError, match="HTTPS allowlist"):
        adapter(base_url="https://evil.example")
