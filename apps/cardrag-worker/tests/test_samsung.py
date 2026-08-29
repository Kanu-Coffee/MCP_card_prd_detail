from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from helpers import pdf_bytes

from cardrag_worker.issuers.common import IssuerMarkupChanged
from cardrag_worker.issuers.samsung import (
    ANNOUNCEMENT_LAST_CHANGED_TIMESTAMP_METADATA_KEY,
    ANNOUNCEMENT_POST_ID_METADATA_KEY,
    ANNOUNCEMENT_START_DATE_METADATA_KEY,
    ANNOUNCEMENT_WRITE_DATE_METADATA_KEY,
    ATTACHMENT_GROUP_METADATA_KEY,
    ATTACHMENT_SEQUENCE_METADATA_KEY,
    CATEGORY,
    ENCODED_ATTACHMENT_GROUP_METADATA_KEY,
    LIST_PATH,
    LOGICAL_GUIDE_KEY_VERSION,
    LOGICAL_GUIDE_KEY_VERSION_METADATA_KEY,
    NOTICE_PATH,
    OFFICIAL_PRODUCT_CODE_METADATA_KEY,
    RAW_SEMANTIC_GUIDE_NAME_METADATA_KEY,
    REGISTERED_TIMESTAMP_METADATA_KEY,
    SEMANTIC_GUIDE_NAME_METADATA_KEY,
    VARIANT_KEY_METADATA_KEY,
    SamsungAdapter,
    parse_listing_page,
)
from cardrag_worker.pdf_cache import PDFCache, PDFSourceIdentity
from cardrag_worker.state import WorkerState

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "samsung_listing_pages.json"


class Response:
    def __init__(self, payload: object = None, *, text: str = "") -> None:
        self.payload = payload
        self.text = text

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self.payload


class SamsungClient:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self.pages = pages
        self.gets: list[tuple[str, dict[str, Any]]] = []
        self.posts: list[tuple[str, dict[str, Any]]] = []

    async def get(self, url: str, **kwargs: Any) -> Response:
        self.gets.append((url, kwargs))
        return Response(text="landing")

    async def post(self, url: str, **kwargs: Any) -> Response:
        self.posts.append((url, kwargs))
        page_number = int(kwargs["json"]["cndt"]["pgeNo"])
        return Response(copy.deepcopy(self.pages[page_number - 1]))


def fixture_pages() -> list[dict[str, Any]]:
    payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    return payload["pages"]


def one_row_payload(*, page: int = 0, row: int = 0) -> dict[str, Any]:
    payload = copy.deepcopy(fixture_pages()[page])
    payload["totInqrCt"] = "1"
    payload["blbdInqrRsList"] = [payload["blbdInqrRsList"][row]]
    return payload


def logical_product_code(official_product_code: str, semantic_guide_name: str) -> str:
    digest = hashlib.sha256(semantic_guide_name.encode("utf-8")).hexdigest()
    return f"{official_product_code}--v-{digest[:16]}"


@pytest.mark.asyncio
async def test_samsung_discovery_paginates_and_builds_stable_variant_identities() -> None:
    client = SamsungClient(fixture_pages())
    adapter = SamsungAdapter(minimum_records=1, page_size=2)

    snapshot = await adapter.discover_current(client)  # type: ignore[arg-type]

    by_code = {row.product_code: row for row in snapshot.records}
    aap100 = logical_product_code("AAP100", "삼성 테스트 카드")
    aap200 = logical_product_code("AAP200", "단일 상품설명")
    aap300_a = logical_product_code("AAP300", "복수 안내장 a")
    aap300_b = logical_product_code("AAP300", "복수 안내장 b")
    aap300_other = logical_product_code("AAP300", "다른 게시물")
    assert set(by_code) == {
        aap100,
        aap200,
        aap300_a,
        aap300_b,
        aap300_other,
    }
    assert len(client.gets) == 1
    assert client.gets[0][0].endswith(NOTICE_PATH)
    assert [call[1]["json"]["cndt"]["pgeNo"] for call in client.posts] == ["1", "2"]
    assert all(call[0].endswith(LIST_PATH) for call in client.posts)
    assert all(call[1]["json"]["cndt"]["no1PgeSize"] == "2" for call in client.posts)
    assert all(call[1]["json"]["cndt"]["itgBlbdTpDvC"] == "19" for call in client.posts)

    ordinary = by_code[aap100]
    assert ordinary.file_name == "삼성 테스트 카드 이용안내장.pdf"
    assert ordinary.source_post_id == "100:000000000000000000000100:1"
    assert ordinary.source_version == "20260825150000000001"
    assert ordinary.category == CATEGORY
    assert ordinary.metadata == {
        OFFICIAL_PRODUCT_CODE_METADATA_KEY: "AAP100",
        ANNOUNCEMENT_POST_ID_METADATA_KEY: "100",
        ANNOUNCEMENT_START_DATE_METADATA_KEY: "20260825",
        ANNOUNCEMENT_WRITE_DATE_METADATA_KEY: "20260820",
        REGISTERED_TIMESTAMP_METADATA_KEY: "20260820123456789012",
        ANNOUNCEMENT_LAST_CHANGED_TIMESTAMP_METADATA_KEY: "20260825150000000001",
        ATTACHMENT_GROUP_METADATA_KEY: "000000000000000000000100",
        ENCODED_ATTACHMENT_GROUP_METADATA_KEY: "TOKEN100%2B%2F%3D",
        ATTACHMENT_SEQUENCE_METADATA_KEY: "1",
        VARIANT_KEY_METADATA_KEY: aap100.removeprefix("AAP100--"),
        RAW_SEMANTIC_GUIDE_NAME_METADATA_KEY: "삼성 테스트 카드",
        SEMANTIC_GUIDE_NAME_METADATA_KEY: "삼성 테스트 카드",
        LOGICAL_GUIDE_KEY_VERSION_METADATA_KEY: LOGICAL_GUIDE_KEY_VERSION,
    }
    assert by_code[aap200].file_name == "단일 상품설명.pdf"
    assert all(source.metadata[VARIANT_KEY_METADATA_KEY] for source in by_code.values())
    assert all(
        source.metadata[LOGICAL_GUIDE_KEY_VERSION_METADATA_KEY] == LOGICAL_GUIDE_KEY_VERSION
        for source in by_code.values()
    )

    request = await adapter.prepare_download(client, ordinary)  # type: ignore[arg-type]
    parsed = urlsplit(request.url)
    assert request.method == "GET"
    assert request.form is None
    assert parsed.path == "/filedownload.do"
    assert parse_qs(parsed.query) == {"grpNo": ["TOKEN100+/="], "sn": ["1"]}
    assert request.headers == {"Referer": "https://www.samsungcard.com" + NOTICE_PATH}


@pytest.mark.asyncio
async def test_samsung_logical_product_code_survives_sibling_addition_and_removal() -> None:
    two_guides = one_row_payload(page=1, row=0)
    row = two_guides["blbdInqrRsList"][0]
    row["uploadFileList"] = row["uploadFileList"][:2]
    one_guide = copy.deepcopy(two_guides)
    one_guide["blbdInqrRsList"][0]["uploadFileList"] = [one_guide["blbdInqrRsList"][0]["uploadFileList"][1]]
    adapter = SamsungAdapter(minimum_records=1, page_size=1)

    multiple = await adapter.discover_current(SamsungClient([two_guides]))  # type: ignore[arg-type]
    single = await adapter.discover_current(SamsungClient([one_guide]))  # type: ignore[arg-type]

    retained = logical_product_code("AAP300", "복수 안내장 a")
    assert {record.product_code for record in multiple.records} == {
        retained,
        logical_product_code("AAP300", "복수 안내장 b"),
    }
    assert [record.product_code for record in single.records] == [retained]


@pytest.mark.asyncio
async def test_samsung_new_post_and_attachment_revision_preserves_logical_code_and_supersedes_state(
    tmp_path: Path,
) -> None:
    original_payload = one_row_payload()
    renewed_payload = copy.deepcopy(original_payload)
    renewed = renewed_payload["blbdInqrRsList"][0]
    renewed.update(
        itgBlbdSn="900",
        bltnStrtdt="20260901",
        bltnbmWrteDt="20260830",
        sysFstRgTs="20260830123456789012",
        sysLtChTs="20260901150000000009",
    )
    for attachment in renewed["uploadFileList"]:
        attachment.update(
            apnFileGrpNo="000000000000000000000900",
            apnFileGrpNoE="TOKEN900%2B%2F%3D",
        )
    adapter = SamsungAdapter(minimum_records=1, page_size=1)

    original = (await adapter.discover_current(SamsungClient([original_payload]))).records[0]  # type: ignore[arg-type]
    replacement = (await adapter.discover_current(SamsungClient([renewed_payload]))).records[0]  # type: ignore[arg-type]

    assert replacement.product_code == original.product_code
    assert replacement.source_id != original.source_id
    assert replacement.source_version == "20260901150000000009"

    source_pdf = tmp_path / "guide.pdf"
    source_pdf.write_bytes(pdf_bytes())
    with WorkerState(tmp_path / "worker-state.sqlite3") as state:
        cache = PDFCache(tmp_path, state)
        cached = cache.ingest(source_pdf)
        cache.bind(
            PDFSourceIdentity.from_source_record(original),
            cached,
            final_url=original.source_url,
        )
        cache.bind(
            PDFSourceIdentity.from_source_record(replacement),
            cached,
            final_url=replacement.source_url,
        )
        original_binding = state.pdf_cache_source_binding(original.source_id)
        replacement_binding = state.pdf_cache_source_binding(replacement.source_id)
        assert original_binding is not None and replacement_binding is not None
        assert original_binding.superseded_by_source_id == replacement.source_id
        assert replacement_binding.superseded_by_source_id is None


@pytest.mark.asyncio
async def test_samsung_last_changed_timestamp_alone_changes_source_not_logical_product() -> None:
    original_payload = one_row_payload()
    changed_payload = copy.deepcopy(original_payload)
    changed_payload["blbdInqrRsList"][0]["sysLtChTs"] = "20260902150000000010"
    adapter = SamsungAdapter(minimum_records=1, page_size=1)

    original = (await adapter.discover_current(SamsungClient([original_payload]))).records[0]  # type: ignore[arg-type]
    changed = (await adapter.discover_current(SamsungClient([changed_payload]))).records[0]  # type: ignore[arg-type]

    assert changed.product_code == original.product_code
    assert changed.source_id != original.source_id
    assert changed.source_version == "20260902150000000010"


@pytest.mark.asyncio
async def test_samsung_semantic_name_normalization_is_product_code_stable() -> None:
    baseline_payload = one_row_payload()
    normalized_payload = copy.deepcopy(baseline_payload)
    normalized_payload["blbdInqrRsList"][0]["uploadFileList"][0]["apnFileNm"] = (
        "삼성\u3000테스트－카드_상품 이용 안내장.PDF"
    )
    adapter = SamsungAdapter(minimum_records=1, page_size=1)

    baseline = (await adapter.discover_current(SamsungClient([baseline_payload]))).records[0]  # type: ignore[arg-type]
    normalized = (await adapter.discover_current(SamsungClient([normalized_payload]))).records[0]  # type: ignore[arg-type]

    assert normalized.product_code == baseline.product_code
    assert normalized.metadata[SEMANTIC_GUIDE_NAME_METADATA_KEY] == "삼성 테스트 카드"
    assert normalized.metadata[RAW_SEMANTIC_GUIDE_NAME_METADATA_KEY] == "삼성 테스트-카드"
    assert normalized.source_id != baseline.source_id


@pytest.mark.asyncio
async def test_samsung_duplicate_semantic_key_for_one_official_product_fails_closed() -> None:
    payload = one_row_payload(page=1, row=0)
    attachments = payload["blbdInqrRsList"][0]["uploadFileList"][:2]
    attachments[0]["apnFileNm"] = "THE 1 (한양CC) 이용안내장.pdf"
    attachments[1]["apnFileNm"] = "ｔｈｅ\u3000１［한양ＣＣ］ 상품안내장.PDF"
    payload["blbdInqrRsList"][0]["uploadFileList"] = attachments

    with pytest.raises(IssuerMarkupChanged, match="logical guide identity"):
        await SamsungAdapter(minimum_records=1, page_size=1).discover_current(  # type: ignore[arg-type]
            SamsungClient([payload])
        )


@pytest.mark.asyncio
async def test_samsung_same_semantic_name_is_separate_across_official_product_codes() -> None:
    payload = one_row_payload()
    original = payload["blbdInqrRsList"][0]
    sibling = copy.deepcopy(original)
    sibling.update(bgdAlncPdC="AAP101", itgBlbdSn="101")
    for attachment in sibling["uploadFileList"]:
        attachment.update(
            apnFileGrpNo="000000000000000000000101",
            apnFileGrpNoE="TOKEN101%2B%2F%3D",
        )
    payload["totInqrCt"] = "2"
    payload["blbdInqrRsList"] = [original, sibling]

    snapshot = await SamsungAdapter(minimum_records=1, page_size=2).discover_current(  # type: ignore[arg-type]
        SamsungClient([payload])
    )

    suffixes = {record.product_code.split("--", 1)[1] for record in snapshot.records}
    assert {record.metadata[OFFICIAL_PRODUCT_CODE_METADATA_KEY] for record in snapshot.records} == {
        "AAP100",
        "AAP101",
    }
    assert len(suffixes) == 1


@pytest.mark.asyncio
async def test_samsung_variant_and_snapshot_identity_ignore_listing_order_and_observation_time() -> None:
    pages = fixture_pages()
    reordered = copy.deepcopy(pages)
    reordered[1]["blbdInqrRsList"].reverse()
    reordered[1]["blbdInqrRsList"][1]["uploadFileList"].reverse()
    adapter = SamsungAdapter(minimum_records=1, page_size=2)

    first = await adapter.discover_current(SamsungClient(pages))  # type: ignore[arg-type]
    second = await adapter.discover_current(SamsungClient(reordered))  # type: ignore[arg-type]

    assert first.snapshot_id == second.snapshot_id
    assert [row.source_id for row in first.records] == [row.source_id for row in second.records]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("bgdAlncPdC", "AAP101"),
        ("itgBlbdSn", "101"),
        ("bltnStrtdt", "20260826"),
        ("bltnbmWrteDt", "20260821"),
        ("sysFstRgTs", "20260821123456789012"),
    ),
)
async def test_samsung_announcement_identity_fields_change_source_identity(
    field: str,
    replacement: str,
) -> None:
    baseline_payload = one_row_payload()
    changed_payload = copy.deepcopy(baseline_payload)
    changed_payload["blbdInqrRsList"][0][field] = replacement
    adapter = SamsungAdapter(minimum_records=1, page_size=1)

    baseline = await adapter.discover_current(  # type: ignore[arg-type]
        SamsungClient([baseline_payload])
    )
    changed = await adapter.discover_current(SamsungClient([changed_payload]))  # type: ignore[arg-type]

    assert baseline.records[0].source_id != changed.records[0].source_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("apnFileGrpNo", "000000000000000000000101"),
        ("apnFileGrpNoE", "TOKEN101%2B%2F%3D"),
        ("apnFileSn", "9"),
    ),
)
async def test_samsung_attachment_identity_fields_change_source_identity(
    field: str,
    replacement: str,
) -> None:
    baseline_payload = one_row_payload()
    changed_payload = copy.deepcopy(baseline_payload)
    changed_payload["blbdInqrRsList"][0]["uploadFileList"][0][field] = replacement
    adapter = SamsungAdapter(minimum_records=1, page_size=1)

    baseline = await adapter.discover_current(  # type: ignore[arg-type]
        SamsungClient([baseline_payload])
    )
    changed = await adapter.discover_current(SamsungClient([changed_payload]))  # type: ignore[arg-type]

    assert baseline.records[0].source_id != changed.records[0].source_id


@pytest.mark.asyncio
async def test_samsung_multiple_non_guide_pdfs_fail_closed() -> None:
    payload = one_row_payload()
    files = payload["blbdInqrRsList"][0]["uploadFileList"]
    files[0]["apnFileNm"] = "상품설명.pdf"
    files[1]["apnFileNm"] = "상품약관.pdf"

    with pytest.raises(IssuerMarkupChanged, match="unambiguous guide PDF"):
        await SamsungAdapter(minimum_records=1, page_size=1).discover_current(  # type: ignore[arg-type]
            SamsungClient([payload])
        )


@pytest.mark.parametrize(
    "mutator",
    (
        lambda payload: payload["common"].update(procsRsDvC="1"),
        lambda payload: payload["message"].update(msgMarkDvC="99"),
        lambda payload: payload.update(totInqrCt=1),
        lambda payload: payload["blbdInqrRsList"][0].update(itgBlbdChnlDvC="02"),
        lambda payload: payload["blbdInqrRsList"][0].update(bltnStrtdt="not-a-date"),
        lambda payload: payload["blbdInqrRsList"][0].update(sysLtChTs="not-a-timestamp"),
        lambda payload: payload["blbdInqrRsList"][0].update(sysLtChTs="20260825999999999999"),
        lambda payload: payload["blbdInqrRsList"][0].pop("sysLtChTs"),
        lambda payload: payload["blbdInqrRsList"][0]["uploadFileList"][0].update(apnFileGrpNoE="bad&token"),
    ),
    ids=(
        "service-failure",
        "message-failure",
        "total-type",
        "binding",
        "date",
        "last-changed-timestamp",
        "invalid-last-changed-time",
        "missing-last-changed-timestamp",
        "token",
    ),
)
def test_samsung_listing_contract_fails_closed(mutator: Any) -> None:
    payload = one_row_payload()
    mutator(payload)

    with pytest.raises(IssuerMarkupChanged, match="Samsung"):
        parse_listing_page(payload, discovered_at=datetime.now(UTC), page_size=1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("total", "page_size", "maximum_pages", "maximum_records", "error"),
    (
        ("3", 2, 1, 10, "page limit"),
        ("2", 1, 10, 1, "record limit"),
    ),
)
async def test_samsung_discovery_is_bounded_before_requesting_more_pages(
    total: str,
    page_size: int,
    maximum_pages: int,
    maximum_records: int,
    error: str,
) -> None:
    payload = one_row_payload()
    payload["totInqrCt"] = total
    client = SamsungClient([payload])
    adapter = SamsungAdapter(
        minimum_records=1,
        page_size=page_size,
        maximum_pages=maximum_pages,
        maximum_records=maximum_records,
    )

    with pytest.raises(IssuerMarkupChanged, match=error):
        await adapter.discover_current(client)  # type: ignore[arg-type]
    assert len(client.posts) == 1


@pytest.mark.asyncio
async def test_samsung_selected_pdf_variants_are_record_bounded() -> None:
    payload = one_row_payload(page=1, row=0)
    client = SamsungClient([payload])
    adapter = SamsungAdapter(
        minimum_records=1,
        page_size=1,
        maximum_pages=1,
        maximum_records=1,
    )

    with pytest.raises(IssuerMarkupChanged, match="selected PDFs exceeded"):
        await adapter.discover_current(client)  # type: ignore[arg-type]


@pytest.mark.asyncio
@pytest.mark.parametrize("second_total", ("4", "5"), ids=("repeated-page", "changed-total"))
async def test_samsung_pagination_requires_a_stable_complete_listing(second_total: str) -> None:
    pages = fixture_pages()
    if second_total == "4":
        pages[1]["blbdInqrRsList"] = copy.deepcopy(pages[0]["blbdInqrRsList"])
    else:
        pages[1]["totInqrCt"] = second_total
    client = SamsungClient(pages)

    with pytest.raises(IssuerMarkupChanged, match="stable complete|total changed"):
        await SamsungAdapter(minimum_records=1, page_size=2).discover_current(  # type: ignore[arg-type]
            client
        )


@pytest.mark.asyncio
async def test_samsung_prepare_download_rejects_forged_discovery_identity() -> None:
    payload = one_row_payload()
    client = SamsungClient([payload])
    adapter = SamsungAdapter(minimum_records=1, page_size=1)
    snapshot = await adapter.discover_current(client)  # type: ignore[arg-type]
    source = snapshot.records[0]

    with pytest.raises(ValueError, match="discovery identity"):
        await adapter.prepare_download(client, replace(source, issuer="kb"))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="discovery identity"):
        await adapter.prepare_download(  # type: ignore[arg-type]
            client,
            replace(source, source_url="https://www.samsungcard.com/filedownload.do?grpNo=x&sn=1"),
        )
    with pytest.raises(ValueError, match="discovery identity"):
        await adapter.prepare_download(  # type: ignore[arg-type]
            client,
            replace(source, metadata={**source.metadata, ENCODED_ATTACHMENT_GROUP_METADATA_KEY: "x&sn=9"}),
        )
    invalid_last_changed = "20260825999999999999"
    with pytest.raises(ValueError, match="discovery identity"):
        await adapter.prepare_download(  # type: ignore[arg-type]
            client,
            replace(
                source,
                source_version=invalid_last_changed,
                metadata={
                    **source.metadata,
                    ANNOUNCEMENT_LAST_CHANGED_TIMESTAMP_METADATA_KEY: invalid_last_changed,
                },
            ),
        )
