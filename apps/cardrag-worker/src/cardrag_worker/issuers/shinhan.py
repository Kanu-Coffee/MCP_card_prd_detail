"""Shinhan personal credit/check disclosure interpretation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime

import httpx
from bs4 import BeautifulSoup

from cardrag_worker.contracts import (
    DownloadRequest,
    IssuerSpec,
    SourceRecord,
    SourceSnapshot,
    snapshot_from_records,
)

from .common import IssuerMarkupChanged, absolute_https_url, clean_text, parse_source_date, require_minimum

BASE_URL = "https://www.shinhancard.com"
NOTICE_PATH = "/hpp/HPPCARDN/HPPPdPbnA01C.shc?creChkCcd=2"
LIST_PATH = "/hpp/HPPCUSTMN/CrdPdPbn02.ahtml"
# Keep this retired transport URL in SourceRecord identity so already published
# sources and OCR assets remain adoptable. Only the ephemeral download request
# moves to the official mobile endpoints below.
DOWNLOAD_PATH = "/hpp/HPPCUSTMN/CrdPdPbn01FileDn.shc"
MOBILE_NOTICE_PATH = "/mob/MOBFM12051N/MOBFM12051R01.shc"
MOBILE_LIST_PATH = "/mob/MOBFM12051N/MOBFM12051R01C.ajax"
MOBILE_DOWNLOAD_PATH = "/mob/MOBFM12051N/MOBFM12051R03.shc"
SHINHAN_CATEGORIES = {"credit": "0", "check": "1"}
SHINHAN_MOBILE_PAGES = {"credit": "CRE", "check": "CHK"}
DOWNLOAD_NAME_METADATA_KEY = "download_pbn_name"
REFRESH_MAXIMUM_PAGES = 5
REFRESH_MAXIMUM_RECORDS = 50
REFRESH_SEARCH_MAXIMUM_BYTES = 50
REFRESH_FULL_LIST_MAXIMUM_PAGES = 100
REFRESH_FULL_LIST_MAXIMUM_RECORDS = 1_000
SPEC = IssuerSpec(
    code="shinhan",
    display_name="신한카드",
    sort_order=30,
    allowed_hosts=frozenset({"www.shinhancard.com"}),
    categories=tuple(SHINHAN_CATEGORIES),
    minimum_records=25,
)


@dataclass(frozen=True, slots=True)
class _ListingRecord:
    source: SourceRecord
    file_token: str
    download_name: str


@dataclass(frozen=True, slots=True)
class _MobileListingRecord:
    product_code: str
    product_name: str
    effective_date: date
    source_version: str
    file_token: str


def _parse_listing_records(
    page_html: str, *, category: str, discovered_at: datetime
) -> tuple[list[_ListingRecord], str | None, bool]:
    if category not in SHINHAN_CATEGORIES:
        raise ValueError("unknown Shinhan disclosure category")
    soup = BeautifulSoup(page_html, "lxml")
    rows: list[_ListingRecord] = []
    for row in soup.select("tr[name='pdPbnRow'], tr"):
        download = row.select_one("[onclick*='fnRetrieveFile']")
        history = row.select_one("[onclick*='openSvPifPop']")
        if download is None or history is None:
            continue
        file_match = re.search(
            r"fnRetrieveFile\(\s*'([^']+)'\s*,\s*'([^']+)'", str(download.get("onclick") or "")
        )
        product_match = re.search(
            r"openSvPifPop\(\s*'([^']+)'\s*,\s*'([^']*)'", str(history.get("onclick") or "")
        )
        date_match = re.search(r"20\d{2}\D{1,3}\d{1,2}\D{1,3}\d{1,2}", row.get_text(" ", strip=True))
        if not file_match or not product_match or not date_match:
            continue
        token, download_name = file_match.groups()
        product_code, fallback_name = product_match.groups()
        product_code = product_code.strip()
        fallback_name = fallback_name.strip()
        local_file_name = download_name.strip()
        if not token.strip() or not local_file_name or not product_code:
            continue
        name_node = row.find(["th", "td"])
        product_name = clean_text(name_node.get_text(" ", strip=True) if name_node else fallback_name)
        effective = parse_source_date(date_match.group(0))
        source = SourceRecord(
            issuer=SPEC.code,
            product_code=product_code,
            product_name=product_name,
            effective_date=effective,
            source_version=effective.strftime("%Y%m%d"),
            source_url=absolute_https_url(BASE_URL, DOWNLOAD_PATH, SPEC.allowed_hosts),
            source_post_id=f"{category}:{product_code}",
            file_name=(
                local_file_name if local_file_name.casefold().endswith(".pdf") else local_file_name + ".pdf"
            ),
            category=category,
            discovered_at=discovered_at,
            metadata={DOWNLOAD_NAME_METADATA_KEY: download_name},
        )
        rows.append(_ListingRecord(source=source, file_token=token, download_name=download_name))
    more = re.search(r"<!--\s*MORE\(([^)]+)\)\s*-->", page_html, flags=re.I)
    return rows, (more.group(1) if more else None), bool(re.search(r"<!--\s*DONE\s*-->", page_html, re.I))


def parse_listing(
    page_html: str, *, category: str, discovered_at: datetime
) -> tuple[list[SourceRecord], str | None, bool]:
    rows, next_cursor, done = _parse_listing_records(
        page_html,
        category=category,
        discovered_at=discovered_at,
    )
    return [row.source for row in rows], next_cursor, done


def _parse_mobile_listing(
    payload: object,
    *,
    category: str,
    product_name: str,
) -> tuple[list[_MobileListingRecord], str]:
    category_code = SHINHAN_CATEGORIES.get(category)
    if category_code is None:
        raise ValueError("unknown Shinhan disclosure category")
    if not isinstance(payload, dict) or payload.get("mbw_result") != "S":
        raise IssuerMarkupChanged("Shinhan mobile disclosure lookup failed")
    body = payload.get("mbw_json")
    if not isinstance(body, dict):
        raise IssuerMarkupChanged("Shinhan mobile disclosure response is invalid")
    raw_records = body.get("crdPdPbnList")
    data = body.get("data")
    if not isinstance(raw_records, list) or not isinstance(data, dict):
        raise IssuerMarkupChanged("Shinhan mobile disclosure response is invalid")
    next_cursor = data.get("nxtQyKey")
    echoed_category = data.get("crdTcd")
    echoed_product_name = data.get("crdPdGuiNm")
    if (
        not isinstance(next_cursor, str)
        or echoed_category != category_code
        or echoed_product_name != product_name
    ):
        raise IssuerMarkupChanged("Shinhan mobile disclosure query binding is invalid")

    records: list[_MobileListingRecord] = []
    for raw in raw_records:
        if not isinstance(raw, dict):
            raise IssuerMarkupChanged("Shinhan mobile disclosure record is invalid")
        product_code = raw.get("CRD_PD_GUI_N")
        current_product_name = raw.get("CRD_PD_GUI_NM")
        effective_raw = raw.get("CRD_PD_GUI_BUL_D")
        file_token = raw.get("CRD_PD_GUI_FIL_NM")
        if not (
            isinstance(product_code, str)
            and product_code.strip()
            and isinstance(current_product_name, str)
            and current_product_name.strip()
            and isinstance(effective_raw, str)
            and effective_raw.strip()
            and isinstance(file_token, str)
            and file_token.strip()
        ):
            raise IssuerMarkupChanged("Shinhan mobile disclosure record is invalid")
        try:
            effective = parse_source_date(effective_raw)
        except ValueError as exc:
            raise IssuerMarkupChanged("Shinhan mobile disclosure date is invalid") from exc
        records.append(
            _MobileListingRecord(
                product_code=product_code.strip(),
                product_name=clean_text(current_product_name),
                effective_date=effective,
                source_version=effective.strftime("%Y%m%d"),
                file_token=file_token.strip(),
            )
        )
    return records, next_cursor


def _validate_download_source_identity(source: SourceRecord) -> None:
    download_name = source.metadata.get(DOWNLOAD_NAME_METADATA_KEY)
    expected_file_name = (
        download_name.strip()
        if isinstance(download_name, str) and download_name.casefold().strip().endswith(".pdf")
        else f"{download_name.strip()}.pdf"
        if isinstance(download_name, str)
        else ""
    )
    if (
        not isinstance(download_name, str)
        or not download_name.strip()
        or dict(source.metadata) != {DOWNLOAD_NAME_METADATA_KEY: download_name}
        or source.source_url != BASE_URL + DOWNLOAD_PATH
        or source.source_post_id != f"{source.category}:{source.product_code}"
        or source.file_name != expected_file_name
        or source.document_type != "product_description"
        or source.source_version != source.effective_date.strftime("%Y%m%d")
    ):
        raise ValueError("source does not satisfy the Shinhan discovery identity")


def _bounded_mobile_search_term(product_name: str) -> str:
    """Fit the primary query to Shinhan's legacy EUC-KR search boundary."""

    prefix: list[str] = []
    byte_count = 0
    for character in product_name:
        try:
            character_bytes = len(character.encode("euc-kr"))
        except UnicodeEncodeError as exc:
            raise IssuerMarkupChanged(
                "Shinhan mobile disclosure search term is not EUC-KR encodable"
            ) from exc
        if byte_count + character_bytes > REFRESH_SEARCH_MAXIMUM_BYTES:
            break
        prefix.append(character)
        byte_count += character_bytes
    search_term = "".join(prefix).rstrip()
    if not search_term:
        raise IssuerMarkupChanged("Shinhan mobile disclosure search term is empty")
    return search_term


def _matching_mobile_records(
    records: list[_MobileListingRecord], source: SourceRecord
) -> list[_MobileListingRecord]:
    return [
        row
        for row in records
        if row.product_code == source.product_code
        and row.product_name == source.product_name
        and row.effective_date == source.effective_date
        and row.source_version == source.source_version
    ]


class ShinhanAdapter:
    spec = SPEC
    parser_version = "shinhan.current.v2"

    def __init__(self, *, base_url: str = BASE_URL, minimum_records: int | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.minimum_records = minimum_records or self.spec.minimum_records

    async def _post(self, client: httpx.AsyncClient, data: dict[str, str]) -> str:
        response = await client.post(
            self.base_url + LIST_PATH,
            data=data,
            headers={"Referer": self.base_url + NOTICE_PATH, "X-Requested-With": "XMLHttpRequest"},
        )
        response.raise_for_status()
        return response.content.decode("euc-kr", "replace")

    async def _query_current(
        self,
        client: httpx.AsyncClient,
        *,
        category: str,
        product_name: str,
        discovered_at: datetime,
        maximum_pages: int | None = None,
        maximum_records: int | None = None,
    ) -> list[_ListingRecord]:
        category_code = SHINHAN_CATEGORIES.get(category)
        if category_code is None:
            raise ValueError("unknown Shinhan disclosure category")
        records: list[_ListingRecord] = []
        cursor = ""
        seen: set[str] = set()
        page_count = 0
        while True:
            page_count += 1
            html = await self._post(
                client,
                {"nxtQyKey": cursor, "crdTcd": category_code, "crdPdGuiNm": product_name},
            )
            batch, next_cursor, done = _parse_listing_records(
                html,
                category=category,
                discovered_at=discovered_at,
            )
            records.extend(batch)
            if maximum_records is not None and len(records) > maximum_records:
                raise IssuerMarkupChanged("Shinhan filtered disclosure lookup exceeded its record limit")
            if done or not next_cursor:
                return records
            if maximum_pages is not None and page_count >= maximum_pages:
                raise IssuerMarkupChanged("Shinhan filtered disclosure lookup exceeded its page limit")
            if next_cursor in seen:
                raise IssuerMarkupChanged("Shinhan pagination cursor repeated")
            seen.add(next_cursor)
            cursor = next_cursor

    async def _post_mobile(
        self,
        client: httpx.AsyncClient,
        *,
        category: str,
        product_name: str,
        cursor: str,
        referer: str,
    ) -> tuple[list[_MobileListingRecord], str]:
        category_code = SHINHAN_CATEGORIES.get(category)
        if category_code is None:
            raise ValueError("unknown Shinhan disclosure category")
        response = await client.post(
            self.base_url + MOBILE_LIST_PATH,
            data={
                "nxtQyKey": cursor,
                "crdTcd": category_code,
                "crdPdGuiNm": product_name,
            },
            headers={
                "Accept": "application/json",
                "Referer": referer,
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            raise IssuerMarkupChanged("Shinhan mobile disclosure response is not JSON") from exc
        return _parse_mobile_listing(payload, category=category, product_name=product_name)

    async def _query_mobile_current(
        self,
        client: httpx.AsyncClient,
        *,
        category: str,
        product_name: str,
        referer: str,
        maximum_pages: int,
        maximum_records: int,
    ) -> list[_MobileListingRecord]:
        records: list[_MobileListingRecord] = []
        cursor = ""
        seen: set[str] = set()
        for page_count in range(1, maximum_pages + 1):
            batch, next_cursor = await self._post_mobile(
                client,
                category=category,
                product_name=product_name,
                cursor=cursor,
                referer=referer,
            )
            records.extend(batch)
            if len(records) > maximum_records:
                raise IssuerMarkupChanged("Shinhan filtered disclosure lookup exceeded its record limit")
            if not next_cursor:
                return records
            if page_count >= maximum_pages:
                raise IssuerMarkupChanged("Shinhan filtered disclosure lookup exceeded its page limit")
            if next_cursor in seen:
                raise IssuerMarkupChanged("Shinhan pagination cursor repeated")
            seen.add(next_cursor)
            cursor = next_cursor
        raise IssuerMarkupChanged("Shinhan filtered disclosure lookup exceeded its page limit")

    async def discover_current(self, client: httpx.AsyncClient) -> SourceSnapshot:
        started = datetime.now(UTC)
        landing = await client.get(self.base_url + NOTICE_PATH)
        landing.raise_for_status()
        records: list[SourceRecord] = []
        for category in self.spec.categories:
            records.extend(
                row.source
                for row in await self._query_current(
                    client,
                    category=category,
                    product_name="",
                    discovered_at=started,
                )
            )
        unique: dict[tuple[str, str, date, str, str], SourceRecord] = {}
        for record in records:
            key = (
                record.product_code,
                record.document_type,
                record.effective_date,
                record.source_version,
                record.source_post_id,
            )
            previous = unique.get(key)
            if previous is not None and previous.discovery_payload != record.discovery_payload:
                raise IssuerMarkupChanged("Shinhan discovery returned a conflicting stable source")
            unique[key] = record
        require_minimum(records, label="Shinhan current PDF disclosure", minimum=self.minimum_records)
        return snapshot_from_records(
            issuer=self.spec.code,
            source_url=self.base_url + NOTICE_PATH,
            parser_version=self.parser_version,
            records=records,
            started_at=started,
        )

    async def prepare_download(self, client: httpx.AsyncClient, source: SourceRecord) -> DownloadRequest:
        if source.issuer != self.spec.code:
            raise ValueError("source issuer does not match adapter")
        if source.category not in SHINHAN_CATEGORIES:
            raise ValueError("unknown Shinhan disclosure category")
        _validate_download_source_identity(source)
        mobile_page = SHINHAN_MOBILE_PAGES[source.category]
        landing_url = str(httpx.URL(self.base_url + MOBILE_NOTICE_PATH, params={"page": mobile_page}))
        landing = await client.get(landing_url)
        landing.raise_for_status()
        primary_search_term = _bounded_mobile_search_term(source.product_name)
        current = await self._query_mobile_current(
            client,
            category=source.category,
            product_name=primary_search_term,
            referer=landing_url,
            maximum_pages=REFRESH_MAXIMUM_PAGES,
            maximum_records=REFRESH_MAXIMUM_RECORDS,
        )
        matches = _matching_mobile_records(current, source)
        if len(matches) != 1:
            # A few official rows cannot be found by their own display name.
            # Fall back only after a structurally valid primary response, and
            # keep the full-list request bounded. Selection remains an exact
            # singleton match across every stable source identity field.
            current = await self._query_mobile_current(
                client,
                category=source.category,
                product_name="",
                referer=landing_url,
                maximum_pages=REFRESH_FULL_LIST_MAXIMUM_PAGES,
                maximum_records=REFRESH_FULL_LIST_MAXIMUM_RECORDS,
            )
            matches = _matching_mobile_records(current, source)
        if len(matches) != 1:
            raise IssuerMarkupChanged(
                "Shinhan current disclosure did not return exactly one stable source match"
            )
        match = matches[0]
        return DownloadRequest(
            url=str(
                httpx.URL(
                    self.base_url + MOBILE_DOWNLOAD_PATH,
                    params={"pbnNm": match.product_name, "aFilenm": match.file_token},
                )
            ),
            headers={"Referer": landing_url},
        )
