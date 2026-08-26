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
DOWNLOAD_PATH = "/hpp/HPPCUSTMN/CrdPdPbn01FileDn.shc"
SHINHAN_CATEGORIES = {"credit": "0", "check": "1"}
DOWNLOAD_NAME_METADATA_KEY = "download_pbn_name"
REFRESH_MAXIMUM_PAGES = 5
REFRESH_MAXIMUM_RECORDS = 50
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
        landing = await client.get(self.base_url + NOTICE_PATH)
        landing.raise_for_status()
        current = await self._query_current(
            client,
            category=source.category,
            product_name=source.product_name,
            discovered_at=datetime.now(UTC),
            maximum_pages=REFRESH_MAXIMUM_PAGES,
            maximum_records=REFRESH_MAXIMUM_RECORDS,
        )
        matches = [row for row in current if row.source.discovery_payload == source.discovery_payload]
        if len(matches) != 1:
            raise IssuerMarkupChanged(
                "Shinhan current disclosure did not return exactly one stable source match"
            )
        match = matches[0]
        return DownloadRequest(
            url=self.base_url + DOWNLOAD_PATH,
            method="POST",
            form={"filNm": match.file_token, "pbnNm": match.download_name},
            headers={"Referer": self.base_url + NOTICE_PATH},
        )
