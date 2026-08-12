"""KB Kookmin Card disclosure adapter using structural HTML parsing."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import PurePosixPath
from urllib.parse import unquote, urlparse

import httpx
from bs4 import BeautifulSoup
from pydantic import AnyHttpUrl

from cardrag.domain import Issuer

from .base import DiscoveryMode, SourceRecord, SourceSnapshot, UnsupportedCategory
from .common import absolute_https_url, canonical_snapshot, clean_text, parse_source_date, require_nonempty

BASE_URL = "https://card.kbcard.com"
LIST_PATH = "/SVC/DVIEW/HSHMCXCRSZZC0002"
DETAIL_PATH = "/SVC/DVIEW/HSHMCXCRSZZC0003?isAjax=Y&layer=Y&isNoFrame=Y"
KB_CATEGORIES = {
    "0": "personal_credit",
    "1": "personal_check",
    "2": "corporate_credit",
    "3": "corporate_check",
    "4": "international_brand",
}


def _pdf_date(url: str, fallback: str = "") -> str:
    match = re.search(r"(20\d{6}|19\d{6})(?=\.pdf(?:$|\?))", url, flags=re.I)
    return match.group(1) if match else re.sub(r"\D", "", fallback)


def parse_listing(
    page_html: str,
    *,
    category_code: str,
    discovered_at: datetime,
) -> list[SourceRecord]:
    if category_code not in KB_CATEGORIES:
        raise UnsupportedCategory(category_code)
    soup = BeautifulSoup(page_html, "lxml")
    records: list[SourceRecord] = []
    for row in soup.select("tr"):
        pdf = row.select_one('a[href*="/obj/card/download/"][href$=".pdf"]')
        detail = row.select_one("[onclick*='goDetail']")
        if pdf is None or detail is None:
            continue
        onclick = str(detail.get("onclick") or "")
        match = re.search(r"goDetail\(\s*'([^']+)'\s*,\s*'([^']*)'", onclick)
        cells = row.select("td")
        if not match or len(cells) < 1:
            continue
        code = unquote(match.group(1)).strip()
        name = clean_text(cells[0].get_text(" ", strip=True)) or unquote(match.group(2)).strip()
        url = absolute_https_url(
            BASE_URL, str(pdf.get("href")), frozenset({"card.kbcard.com", "img2.kbcard.com"})
        )
        date_text = _pdf_date(url)
        if not date_text:
            continue
        records.append(
            SourceRecord(
                issuer=Issuer.KB,
                product_code=code,
                product_name=name,
                effective_date=parse_source_date(date_text),
                source_version=date_text,
                source_url=AnyHttpUrl(url),
                source_post_id=PurePosixPath(urlparse(url).path).stem,
                file_name=PurePosixPath(urlparse(url).path).name,
                category=KB_CATEGORIES[category_code],
                is_current=True,
                discovered_at=discovered_at,
                metadata={"category_code": category_code},
            )
        )
    return records


def parse_history(
    detail_html: str,
    *,
    current: SourceRecord,
) -> list[SourceRecord]:
    soup = BeautifulSoup(detail_html, "lxml")
    records: list[SourceRecord] = []
    for row in soup.select("tr"):
        pdf = row.select_one('a[href*="/obj/card/download/"][href$=".pdf"]')
        if pdf is None:
            continue
        url = absolute_https_url(
            BASE_URL, str(pdf.get("href")), frozenset({"card.kbcard.com", "img2.kbcard.com"})
        )
        date_match = re.search(r"\d{4}[.-]\d{2}[.-]\d{2}", row.get_text(" ", strip=True))
        date_text = _pdf_date(url, date_match.group(0) if date_match else "")
        if not date_text:
            continue
        records.append(
            current.model_copy(
                update={
                    "effective_date": parse_source_date(date_text),
                    "source_version": date_text,
                    "source_url": AnyHttpUrl(url),
                    "source_post_id": PurePosixPath(urlparse(url).path).stem,
                    "file_name": PurePosixPath(urlparse(url).path).name,
                    "is_current": url == str(current.source_url),
                }
            )
        )
    return records


def _last_page(page_html: str) -> int:
    soup = BeautifulSoup(page_html, "lxml")
    found: list[int] = []
    for node in soup.select("[onclick*='doSearchSpider']"):
        match = re.search(r'HSHMCXCRSZZC0002["\']\s*,\s*["\'](\d+)', str(node.get("onclick")))
        if match:
            found.append(int(match.group(1)))
    return max(found, default=1)


class KBAdapter:
    issuer = Issuer.KB
    allowed_hosts = frozenset({"card.kbcard.com", "img2.kbcard.com"})
    parser_version = "kb.v2"

    def __init__(self, *, base_url: str = BASE_URL, expected_minimum: int = 1) -> None:
        self.base_url = base_url.rstrip("/")
        self.expected_minimum = expected_minimum

    async def _post(self, client: httpx.AsyncClient, path: str, data: dict[str, str]) -> str:
        response = await client.post(
            self.base_url + path,
            data=data,
            headers={"X-Requested-With": "XMLHttpRequest", "Referer": self.base_url + LIST_PATH},
        )
        response.raise_for_status()
        return response.text

    async def discover(
        self,
        client: httpx.AsyncClient,
        *,
        mode: DiscoveryMode,
        categories: frozenset[str] | None = None,
    ) -> SourceSnapshot:
        started = datetime.now(UTC)
        codes = categories or frozenset(KB_CATEGORIES)
        invalid = set(codes).difference(KB_CATEGORIES)
        if invalid:
            raise UnsupportedCategory(",".join(sorted(invalid)))
        landing = await client.get(self.base_url + LIST_PATH)
        landing.raise_for_status()
        records: list[SourceRecord] = []
        for code in sorted(codes):
            first = await self._post(
                client,
                LIST_PATH,
                {"카드분류코드": code, "카드검색그룹코드": "", "pageCount": "1", "카드명": ""},
            )
            for page in range(1, _last_page(first) + 1):
                html = (
                    first
                    if page == 1
                    else await self._post(
                        client,
                        LIST_PATH,
                        {"카드분류코드": code, "카드검색그룹코드": "", "pageCount": str(page), "카드명": ""},
                    )
                )
                current = parse_listing(html, category_code=code, discovered_at=started)
                for item in current:
                    if mode == DiscoveryMode.HISTORY:
                        detail = await self._post(
                            client,
                            DETAIL_PATH,
                            {
                                "카드명": item.product_name,
                                "카드제휴코드": item.product_code,
                                "카드분류코드_search": "0",
                            },
                        )
                        history = parse_history(detail, current=item)
                        # A history endpoint is allowed to omit the current row.
                        # BULK still promises the complete current+history set.
                        records.extend([item, *history])
                    else:
                        records.append(item)
        require_nonempty(records, label="KB PDF disclosure", expected_minimum=self.expected_minimum)
        return canonical_snapshot(
            issuer=self.issuer,
            mode=mode,
            source_url=self.base_url + LIST_PATH,
            parser_version=self.parser_version,
            records=records,
            started_at=started,
        )
