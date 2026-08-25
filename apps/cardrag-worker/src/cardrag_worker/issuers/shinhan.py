"""Shinhan personal credit/check disclosure interpretation."""

from __future__ import annotations

import re
from datetime import UTC, datetime

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

BASE_URL = "https://www.shinhancard.com"
NOTICE_PATH = "/hpp/HPPCARDN/HPPPdPbnA01C.shc?creChkCcd=2"
LIST_PATH = "/hpp/HPPCUSTMN/CrdPdPbn02.ahtml"
DOWNLOAD_PATH = "/hpp/HPPCUSTMN/CrdPdPbn01FileDn.shc"
SHINHAN_CATEGORIES = {"credit": "0", "check": "1"}
SPEC = IssuerSpec(
    code="shinhan",
    display_name="신한카드",
    sort_order=30,
    allowed_hosts=frozenset({"www.shinhancard.com"}),
    categories=tuple(SHINHAN_CATEGORIES),
    minimum_records=25,
)


def parse_listing(
    page_html: str, *, category: str, discovered_at: datetime
) -> tuple[list[SourceRecord], str | None, bool]:
    soup = BeautifulSoup(page_html, "lxml")
    rows: list[SourceRecord] = []
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
        token, file_name = file_match.groups()
        product_code, fallback_name = product_match.groups()
        name_node = row.find(["th", "td"])
        product_name = clean_text(name_node.get_text(" ", strip=True) if name_node else fallback_name)
        effective = parse_source_date(date_match.group(0))
        rows.append(
            SourceRecord(
                issuer=SPEC.code,
                product_code=product_code,
                product_name=product_name,
                effective_date=effective,
                source_version=effective.strftime("%Y%m%d"),
                source_url=absolute_https_url(BASE_URL, DOWNLOAD_PATH, SPEC.allowed_hosts),
                source_post_id=token,
                file_name=file_name if file_name.casefold().endswith(".pdf") else file_name + ".pdf",
                category=category,
                discovered_at=discovered_at,
                metadata={"file_token": token},
            )
        )
    more = re.search(r"<!--\s*MORE\(([^)]+)\)\s*-->", page_html, flags=re.I)
    return rows, (more.group(1) if more else None), bool(re.search(r"<!--\s*DONE\s*-->", page_html, re.I))


class ShinhanAdapter:
    spec = SPEC
    parser_version = "shinhan.current.v1"

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

    async def discover_current(self, client: httpx.AsyncClient) -> SourceSnapshot:
        started = datetime.now(UTC)
        landing = await client.get(self.base_url + NOTICE_PATH)
        landing.raise_for_status()
        records: list[SourceRecord] = []
        for category in self.spec.categories:
            cursor = ""
            seen: set[str] = set()
            while True:
                html = await self._post(
                    client,
                    {"nxtQyKey": cursor, "crdTcd": SHINHAN_CATEGORIES[category], "crdPdGuiNm": ""},
                )
                batch, next_cursor, done = parse_listing(html, category=category, discovered_at=started)
                records.extend(batch)
                if done or not next_cursor:
                    break
                if next_cursor in seen:
                    raise RuntimeError("Shinhan pagination cursor repeated")
                seen.add(next_cursor)
                cursor = next_cursor
        require_minimum(records, label="Shinhan current PDF disclosure", minimum=self.minimum_records)
        return snapshot_from_records(
            issuer=self.spec.code,
            source_url=self.base_url + NOTICE_PATH,
            parser_version=self.parser_version,
            records=records,
            started_at=started,
        )

    async def prepare_download(self, client: httpx.AsyncClient, source: SourceRecord) -> DownloadRequest:
        del client
        if source.issuer != self.spec.code:
            raise ValueError("source issuer does not match adapter")
        token = str(source.metadata.get("file_token") or source.source_post_id)
        return DownloadRequest(
            url=self.base_url + DOWNLOAD_PATH,
            method="POST",
            form={"filNm": token, "pbnNm": source.file_name},
            headers={"Referer": self.base_url + NOTICE_PATH},
        )
