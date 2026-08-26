"""KB disclosure interpretation; direct PDF GETs use the common downloader."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import PurePosixPath
from urllib.parse import unquote, urlparse

import httpx
from bs4 import BeautifulSoup

from cardrag_worker.contracts import (
    DownloadRequest,
    IssuerSpec,
    SourceRecord,
    SourceSnapshot,
    snapshot_from_records,
)

from .common import absolute_https_url, clean_text, parse_source_date, require_minimum

BASE_URL = "https://card.kbcard.com"
LIST_PATH = "/SVC/DVIEW/HSHMCXCRSZZC0002"
KB_CATEGORIES = {
    "0": "personal_credit",
    "1": "personal_check",
    "2": "corporate_credit",
    "3": "corporate_check",
    "4": "international_brand",
}
SPEC = IssuerSpec(
    code="kb",
    display_name="KB국민카드",
    sort_order=20,
    allowed_hosts=frozenset({"card.kbcard.com", "img2.kbcard.com"}),
    categories=tuple(KB_CATEGORIES),
    minimum_records=25,
)

_DETAIL_CALL = re.compile(
    r"^\s*(?:javascript:\s*)?goDetail\(\s*'([^']+)'\s*,\s*'([^']*)'"
    r"(?:\s*,\s*'[^']*')?\s*\)\s*;?\s*$",
    flags=re.I,
)
_PAGE_CALL = re.compile(
    r'^\s*(?:javascript:\s*)?doSearchSpider\(\s*["\']HSHMCXCRSZZC0002["\']'
    r'\s*,\s*["\'](\d+)["\']\s*\)\s*;?\s*$',
    flags=re.I,
)


def _pdf_date(url: str) -> str:
    match = re.search(r"(20\d{6}|19\d{6})(?=\.pdf(?:$|\?))", url, flags=re.I)
    return match.group(1) if match else ""


def parse_listing(page_html: str, *, category_code: str, discovered_at: datetime) -> list[SourceRecord]:
    soup = BeautifulSoup(page_html, "lxml")
    records: list[SourceRecord] = []
    for row in soup.select("tr"):
        pdf = row.select_one('a[href*="/obj/card/download/"][href$=".pdf"]')
        detail = row.select_one("[onclick*='goDetail'], a[href*='goDetail']")
        if pdf is None or detail is None:
            continue
        onclick = detail.get("onclick")
        href = detail.get("href")
        detail_call = str(onclick or href or "")
        if (
            href is not None
            and onclick is None
            and not detail_call.lstrip().lower().startswith("javascript:")
        ):
            continue
        match = _DETAIL_CALL.fullmatch(detail_call)
        cells = row.select("td")
        if not match or not cells:
            continue
        code = unquote(match.group(1)).strip()
        name = clean_text(cells[0].get_text(" ", strip=True)) or unquote(match.group(2)).strip()
        url = absolute_https_url(BASE_URL, str(pdf.get("href")), SPEC.allowed_hosts)
        date_text = _pdf_date(url)
        if not date_text:
            continue
        records.append(
            SourceRecord(
                issuer=SPEC.code,
                product_code=code,
                product_name=name,
                effective_date=parse_source_date(date_text),
                source_version=date_text,
                source_url=url,
                source_post_id=PurePosixPath(urlparse(url).path).stem,
                file_name=PurePosixPath(urlparse(url).path).name,
                category=KB_CATEGORIES[category_code],
                discovered_at=discovered_at,
                metadata={"category_code": category_code},
            )
        )
    return records


def _last_page(page_html: str) -> int:
    soup = BeautifulSoup(page_html, "lxml")
    pages: list[int] = []
    for node in soup.select("[onclick*='doSearchSpider'], a[href*='doSearchSpider']"):
        onclick = node.get("onclick")
        href = node.get("href")
        page_call = str(onclick or href or "")
        if href is not None and onclick is None and not page_call.lstrip().lower().startswith("javascript:"):
            continue
        match = _PAGE_CALL.fullmatch(page_call)
        if match:
            pages.append(int(match.group(1)))
    return max(pages, default=1)


class KBAdapter:
    spec = SPEC
    parser_version = "kb.current.v2"

    def __init__(self, *, base_url: str = BASE_URL, minimum_records: int | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.minimum_records = minimum_records or self.spec.minimum_records

    async def _post(self, client: httpx.AsyncClient, data: dict[str, str]) -> str:
        response = await client.post(
            self.base_url + LIST_PATH,
            data=data,
            headers={"X-Requested-With": "XMLHttpRequest", "Referer": self.base_url + LIST_PATH},
        )
        response.raise_for_status()
        return response.text

    async def discover_current(self, client: httpx.AsyncClient) -> SourceSnapshot:
        started = datetime.now(UTC)
        landing = await client.get(self.base_url + LIST_PATH)
        landing.raise_for_status()
        records: list[SourceRecord] = []
        for category_code in self.spec.categories:
            body = {"카드분류코드": category_code, "카드검색그룹코드": "", "pageCount": "1", "카드명": ""}
            first = await self._post(client, body)
            for page in range(1, _last_page(first) + 1):
                html = first if page == 1 else await self._post(client, {**body, "pageCount": str(page)})
                records.extend(parse_listing(html, category_code=category_code, discovered_at=started))
        require_minimum(records, label="KB current PDF disclosure", minimum=self.minimum_records)
        return snapshot_from_records(
            issuer=self.spec.code,
            source_url=self.base_url + LIST_PATH,
            parser_version=self.parser_version,
            records=records,
            started_at=started,
        )

    async def prepare_download(self, client: httpx.AsyncClient, source: SourceRecord) -> DownloadRequest:
        del client
        if source.issuer != self.spec.code:
            raise ValueError("source issuer does not match adapter")
        return DownloadRequest(url=source.source_url)
