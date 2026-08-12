"""Shinhan personal credit/check disclosure adapter (current and history)."""

from __future__ import annotations

import re
from datetime import UTC, datetime

import httpx
from bs4 import BeautifulSoup
from pydantic import AnyHttpUrl

from cardrag.domain import Issuer

from .base import DiscoveryMode, SourceRecord, SourceSnapshot, UnsupportedCategory
from .common import absolute_https_url, canonical_snapshot, clean_text, parse_source_date, require_nonempty

BASE_URL = "https://www.shinhancard.com"
NOTICE_PATH = "/hpp/HPPCARDN/HPPPdPbnA01C.shc?creChkCcd=2"
LIST_PATH = "/hpp/HPPCUSTMN/CrdPdPbn02.ahtml"
HISTORY_PATH = "/hpp/HPPCUSTMN/CrdPdPbn03.ahtml"
DOWNLOAD_PATH = "/hpp/HPPCUSTMN/CrdPdPbn01FileDn.shc"
SHINHAN_CATEGORIES = {"credit": "0", "check": "1"}


def _decode(response: httpx.Response) -> str:
    return response.content.decode("euc-kr", "replace")


def parse_listing(
    page_html: str,
    *,
    category: str,
    discovered_at: datetime,
) -> tuple[list[SourceRecord], str | None, bool]:
    if category not in SHINHAN_CATEGORIES:
        raise UnsupportedCategory(category)
    soup = BeautifulSoup(page_html, "lxml")
    records: list[SourceRecord] = []
    for row in soup.select("tr[name='pdPbnRow'], tr"):
        download = row.select_one("[onclick*='fnRetrieveFile']")
        if download is None:
            continue
        call = str(download.get("onclick") or "")
        file_match = re.search(r"fnRetrieveFile\(\s*'([^']+)'\s*,\s*'([^']+)'", call)
        history = row.select_one("[onclick*='openSvPifPop']")
        history_match = re.search(
            r"openSvPifPop\(\s*'([^']+)'\s*,\s*'([^']*)'",
            str(history.get("onclick") if history else ""),
        )
        date_match = re.search(r"20\d{2}\D{1,3}\d{1,2}\D{1,3}\d{1,2}", row.get_text(" ", strip=True))
        if not file_match or not date_match or not history_match:
            continue
        file_token, file_name = file_match.groups()
        product_code, fallback_name = history_match.groups()
        name_node = row.find(["th", "td"])
        product_name = clean_text(name_node.get_text(" ", strip=True) if name_node else fallback_name)
        effective = parse_source_date(date_match.group(0))
        records.append(
            SourceRecord(
                issuer=Issuer.SHINHAN,
                product_code=product_code,
                product_name=product_name,
                effective_date=effective,
                source_version=effective.strftime("%Y%m%d"),
                source_url=AnyHttpUrl(
                    absolute_https_url(BASE_URL, DOWNLOAD_PATH, frozenset({"www.shinhancard.com"}))
                ),
                source_post_id=file_token,
                file_name=file_name if file_name.casefold().endswith(".pdf") else file_name + ".pdf",
                category=category,
                is_current=True,
                discovered_at=discovered_at,
                metadata={"file_token": file_token, "history_product_name": fallback_name},
            )
        )
    more_match = re.search(r"<!--\s*MORE\(([^)]+)\)\s*-->", page_html, flags=re.I)
    done = bool(re.search(r"<!--\s*DONE\s*-->", page_html, flags=re.I))
    return records, (more_match.group(1) if more_match else None), done


def parse_history(detail_html: str, *, current: SourceRecord) -> list[SourceRecord]:
    parsed, _, _ = parse_listing(
        detail_html,
        category=current.category,
        discovered_at=current.discovered_at,
    )
    if parsed:
        return [
            item.model_copy(
                update={
                    "product_code": current.product_code,
                    "product_name": current.product_name,
                    "is_current": item.source_post_id == current.source_post_id,
                }
            )
            for item in parsed
        ]
    # Some historical pages use links rather than a full listing row.
    soup = BeautifulSoup(detail_html, "lxml")
    records: list[SourceRecord] = []
    for node in soup.select("[onclick*='fnRetrieveFile']"):
        call = str(node.get("onclick") or "")
        file_match = re.search(r"fnRetrieveFile\(\s*'([^']+)'\s*,\s*'([^']+)'", call)
        container = node.find_parent("tr") or node.parent
        if container is None:
            continue
        date_match = re.search(r"20\d{2}\D{1,3}\d{1,2}\D{1,3}\d{1,2}", container.get_text(" ", strip=True))
        if not file_match or not date_match:
            continue
        token, file_name = file_match.groups()
        effective = parse_source_date(date_match.group(0))
        records.append(
            current.model_copy(
                update={
                    "effective_date": effective,
                    "source_version": effective.strftime("%Y%m%d"),
                    "source_post_id": token,
                    "file_name": file_name if file_name.casefold().endswith(".pdf") else file_name + ".pdf",
                    "is_current": token == current.source_post_id,
                    "metadata": {"file_token": token},
                }
            )
        )
    return records


class ShinhanAdapter:
    issuer = Issuer.SHINHAN
    allowed_hosts = frozenset({"www.shinhancard.com"})
    parser_version = "shinhan.v1"

    def __init__(self, *, base_url: str = BASE_URL, expected_minimum: int = 1) -> None:
        self.base_url = base_url.rstrip("/")
        self.expected_minimum = expected_minimum

    async def _post(self, client: httpx.AsyncClient, path: str, data: dict[str, str]) -> str:
        response = await client.post(
            self.base_url + path,
            data=data,
            headers={"Referer": self.base_url + NOTICE_PATH, "X-Requested-With": "XMLHttpRequest"},
        )
        response.raise_for_status()
        return _decode(response)

    async def discover(
        self,
        client: httpx.AsyncClient,
        *,
        mode: DiscoveryMode,
        categories: frozenset[str] | None = None,
    ) -> SourceSnapshot:
        started = datetime.now(UTC)
        selected = categories or frozenset(SHINHAN_CATEGORIES)
        invalid = set(selected).difference(SHINHAN_CATEGORIES)
        if invalid:
            raise UnsupportedCategory("Shinhan v1 excludes corporate/prepaid: " + ",".join(sorted(invalid)))
        landing = await client.get(self.base_url + NOTICE_PATH)
        landing.raise_for_status()
        all_records: list[SourceRecord] = []
        warnings: list[str] = []
        for category in sorted(selected):
            cursor = ""
            seen_cursors: set[str] = set()
            current_records: list[SourceRecord] = []
            while True:
                page = await self._post(
                    client,
                    LIST_PATH,
                    {"nxtQyKey": cursor, "crdTcd": SHINHAN_CATEGORIES[category], "crdPdGuiNm": ""},
                )
                rows, next_cursor, done = parse_listing(page, category=category, discovered_at=started)
                current_records.extend(rows)
                if done or not next_cursor:
                    break
                if next_cursor in seen_cursors:
                    raise RuntimeError("Shinhan pagination cursor repeated")
                seen_cursors.add(next_cursor)
                cursor = next_cursor
            require_nonempty(
                current_records,
                label=f"Shinhan {category} PDF disclosure",
                expected_minimum=self.expected_minimum,
            )
            if mode == DiscoveryMode.HISTORY:
                for current in current_records:
                    history_html = await self._post(
                        client,
                        HISTORY_PATH,
                        {
                            "nxtQyKey": "",
                            "crdPdGuiNm": str(
                                current.metadata.get("history_product_name") or current.product_name
                            ),
                            "crdPdGuiN": current.product_code,
                        },
                    )
                    history = parse_history(history_html, current=current)
                    # Some history responses list only superseded notices.  Keep
                    # the listing's authoritative current record as well.
                    all_records.extend([current, *history])
                    if not history:
                        warnings.append(f"no history rows for {current.product_code}")
            else:
                all_records.extend(current_records)
        return canonical_snapshot(
            issuer=self.issuer,
            mode=mode,
            source_url=self.base_url + NOTICE_PATH,
            parser_version=self.parser_version,
            records=all_records,
            started_at=started,
            warnings=warnings,
        )

    def download_form(self, source: SourceRecord) -> dict[str, str]:
        token = str(source.metadata.get("file_token") or source.source_post_id)
        return {"filNm": token, "pbnNm": source.file_name}
