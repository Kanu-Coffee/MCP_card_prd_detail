"""Lotte Card's current disclosure search, including discontinued products."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote

import httpx

from cardrag_worker.contracts import (
    DownloadRequest,
    IssuerSpec,
    SourceRecord,
    SourceSnapshot,
    snapshot_from_records,
)

from .common import IssuerMarkupChanged, absolute_https_url, clean_text, parse_source_date, require_minimum

BASE_URL = "https://www.lottecard.co.kr"
NOTICE_PATH = "/app/LPCMNPD_V100.lc"
LIST_PATH = "/app/LPSCHAA_V100.lc"
PDF_BASE_URL = "https://image.lottecard.co.kr/UploadFiles/cardProvisionPath/"
PAGE_SIZE = 100
MAXIMUM_PAGES = 100
MAXIMUM_RECORDS = 10_000
CATEGORY = "shared"
# Reviewed against the official listing and current attachment URLs on 2026-09-06.
# A product, metadata, date, or URL change invalidates this exact identity binding.
# Only a fresh HTTP 404 permits exclusion; repaired attachments return automatically.
SOURCE_UNAVAILABLE_ALLOWANCES = {
    "1603": (
        "source_3942b85e984b704350f7f60e93cf89cd820439a68bacdf5c7f2c9890065c9195",
        "20190730",
        "이엠이코리아 Wonderful_가이드북_220120OL.pdf",
    ),
    "1678": (
        "source_29e7ea5b8261cb950be7fbc614afbbb5089d3c9ad32d12a4b1119ac6c17c1d49",
        "20210930",
        "캐시노트 상품안내장 _230310OL.pdf",
    ),
}

SPEC = IssuerSpec(
    code="lotte",
    display_name="롯데카드",
    sort_order=70,
    allowed_hosts=frozenset({"www.lottecard.co.kr", "image.lottecard.co.kr"}),
    categories=(CATEGORY,),
    minimum_records=25,
)


def _required_string(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise IssuerMarkupChanged(f"Lotte disclosure {key} is invalid")
    return value.strip()


def _count(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value).isdigit():
        raise IssuerMarkupChanged(f"Lotte disclosure {label} is invalid")
    return int(value)


def _pdf_url(file_name: str) -> str:
    if (
        PurePosixPath(file_name).name != file_name
        or "\\" in file_name
        or not file_name.casefold().endswith(".pdf")
        or any(ord(character) < 32 for character in file_name)
    ):
        raise IssuerMarkupChanged("Lotte disclosure attachment filename is invalid")
    return absolute_https_url(PDF_BASE_URL, quote(file_name, safe=""), SPEC.allowed_hosts)


def parse_listing_page(
    payload: object,
    *,
    offset: int,
    page_size: int,
    discovered_at: datetime,
) -> tuple[int, list[SourceRecord]]:
    if not isinstance(payload, dict):
        raise IssuerMarkupChanged("Lotte disclosure response is invalid")
    status = payload.get("Status")
    if not isinstance(status, dict) or status.get("code") != 0:
        raise IssuerMarkupChanged("Lotte disclosure lookup failed")
    content = payload.get("Content")
    if not isinstance(content, str):
        raise IssuerMarkupChanged("Lotte disclosure Content is not a JSON string")
    try:
        decoded = json.loads(content)
    except ValueError as exc:
        raise IssuerMarkupChanged("Lotte disclosure Content is not JSON") from exc
    result = decoded.get("result") if isinstance(decoded, dict) else None
    if not isinstance(result, dict):
        raise IssuerMarkupChanged("Lotte disclosure result is invalid")
    collections = result.get("collection")
    if not isinstance(collections, list) or len(collections) != 1:
        raise IssuerMarkupChanged("Lotte disclosure collection is invalid")
    collection = collections[0]
    if not isinstance(collection, dict) or collection.get("id") != "disclosure":
        raise IssuerMarkupChanged("Lotte disclosure collection binding is invalid")
    parameters = result.get("parameter")
    if (
        not isinstance(parameters, dict)
        or parameters.get("collection") != "disclosure"
        or parameters.get("query") != ""
        # The API accepts a row offset but echoes its zero-based page index.
        or _count(parameters.get("startcount"), label="startcount") != offset // page_size
    ):
        raise IssuerMarkupChanged("Lotte disclosure pagination binding is invalid")
    total = _count(collection.get("totalcount"), label="totalcount")
    if _count(result.get("totalcount"), label="result totalcount") != total:
        raise IssuerMarkupChanged("Lotte disclosure totalcount mismatch")
    rows = collection.get("docs")
    if (
        not isinstance(rows, list)
        or len(rows) > page_size
        or _count(collection.get("count"), label="count") != len(rows)
        or offset + len(rows) > total
    ):
        raise IssuerMarkupChanged("Lotte disclosure page count mismatch")

    records: list[SourceRecord] = []
    for raw in rows:
        if not isinstance(raw, dict):
            raise IssuerMarkupChanged("Lotte disclosure record is invalid")
        post_id = _required_string(raw, "DOCID")
        if not post_id.isascii() or not post_id.isdigit():
            raise IssuerMarkupChanged("Lotte disclosure DOCID is invalid")
        product_name = clean_text(_required_string(raw, "VT_CD_KND_NM"))
        file_name = _required_string(raw, "OCY_FILE_NM")
        date_text = _required_string(raw, "BULT_SDT")
        try:
            effective_date = parse_source_date(date_text)
        except ValueError as exc:
            raise IssuerMarkupChanged(f"Lotte disclosure {post_id} BULT_SDT is invalid") from exc
        ended = _required_string(raw, "ISU_E_YN")
        if ended not in {"Y", "N"}:
            raise IssuerMarkupChanged("Lotte disclosure ISU_E_YN is invalid")
        official_code = raw.get("VT_CD_KND_C", "")
        if not isinstance(official_code, str):
            raise IssuerMarkupChanged("Lotte disclosure VT_CD_KND_C is invalid")
        records.append(
            SourceRecord(
                issuer=SPEC.code,
                # The official code is currently blank and may appear later.
                # DOCID is always the stable product identity on this endpoint.
                product_code=post_id,
                product_name=product_name,
                effective_date=effective_date,
                source_version=effective_date.strftime("%Y%m%d"),
                source_url=_pdf_url(file_name),
                source_post_id=post_id,
                file_name=file_name,
                category=CATEGORY,
                discovered_at=discovered_at,
                metadata={
                    "classification_basis": "official_shared_listing",
                    "official_product_code": official_code.strip(),
                    "issuance_ended": ended == "Y",
                },
            )
        )
    return total, records


class LotteAdapter:
    spec = SPEC
    parser_version = "lotte.current.v2"

    def __init__(
        self,
        *,
        base_url: str = BASE_URL,
        minimum_records: int | None = None,
        page_size: int = PAGE_SIZE,
        maximum_pages: int = MAXIMUM_PAGES,
        maximum_records: int = MAXIMUM_RECORDS,
    ) -> None:
        if page_size < 1 or maximum_pages < 1 or maximum_records < page_size:
            raise ValueError("Lotte pagination limits must be positive and internally consistent")
        self.base_url = absolute_https_url(BASE_URL, base_url, self.spec.allowed_hosts).rstrip("/")
        self.minimum_records = minimum_records or self.spec.minimum_records
        self.page_size = page_size
        self.maximum_pages = maximum_pages
        self.maximum_records = maximum_records

    async def discover_current(self, client: httpx.AsyncClient) -> SourceSnapshot:
        started = datetime.now(UTC)
        notice_url = self.base_url + NOTICE_PATH
        landing = await client.get(notice_url)
        landing.raise_for_status()
        records: list[SourceRecord] = []
        seen: set[str] = set()
        expected_total: int | None = None
        for _ in range(self.maximum_pages):
            response = await client.post(
                self.base_url + LIST_PATH,
                data={
                    "collection": "disclosure",
                    "listcount": str(self.page_size),
                    "startcount": str(len(records)),
                    "query": "",
                },
                headers={"X-Requested-With": "XMLHttpRequest", "Referer": notice_url},
            )
            response.raise_for_status()
            try:
                payload = response.json()
            except ValueError as exc:
                raise IssuerMarkupChanged("Lotte disclosure response is not JSON") from exc
            total, page = parse_listing_page(
                payload, offset=len(records), page_size=self.page_size, discovered_at=started
            )
            if total > self.maximum_records:
                raise IssuerMarkupChanged("Lotte disclosure exceeded its record limit")
            if expected_total is not None and total != expected_total:
                raise IssuerMarkupChanged("Lotte disclosure total changed during pagination")
            expected_total = total
            for row in page:
                if row.product_code in seen:
                    raise IssuerMarkupChanged(
                        "Lotte disclosure repeated a product identity during pagination"
                    )
                seen.add(row.product_code)
            records.extend(page)
            if len(records) == total:
                break
            if not page:
                raise IssuerMarkupChanged("Lotte disclosure pagination ended before totalcount")
        else:
            raise IssuerMarkupChanged("Lotte disclosure exceeded its page limit")
        # Validate the complete raw inventory before applying reviewed omissions.
        # This leaves all unknown/changed sources on the ordinary download path.
        available: list[SourceRecord] = []
        warnings: list[str] = []
        for source in records:
            allowance = SOURCE_UNAVAILABLE_ALLOWANCES.get(source.product_code)
            if (
                allowance is not None
                and source.source_id == allowance[0]
                and source.source_version == allowance[1]
                and source.source_url == _pdf_url(allowance[2])
            ):
                async with client.stream("GET", source.source_url, follow_redirects=False) as response:
                    if response.status_code == 404:
                        warnings.append(
                            f"source_unavailable issuer=lotte product_code={source.product_code} "
                            f"source_id={source.source_id} status=404 "
                            f"source_version={source.source_version} source_url={source.source_url}"
                        )
                        continue
                    response.raise_for_status()
            available.append(source)
        records = available
        require_minimum(records, label="Lotte current PDF disclosure", minimum=self.minimum_records)
        return snapshot_from_records(
            issuer=self.spec.code,
            source_url=notice_url,
            parser_version=self.parser_version,
            records=records,
            started_at=started,
            warnings=warnings,
        )

    async def prepare_download(self, client: httpx.AsyncClient, source: SourceRecord) -> DownloadRequest:
        del client
        if source.issuer != self.spec.code:
            raise ValueError("source issuer does not match adapter")
        if source.category not in self.spec.categories:
            raise ValueError("source category does not match adapter")
        if source.source_url != _pdf_url(source.file_name):
            raise ValueError("source does not satisfy the Lotte discovery identity")
        return DownloadRequest(url=source.source_url)
