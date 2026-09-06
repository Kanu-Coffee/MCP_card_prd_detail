"""BC's current product leaflets and labelled guide variants."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, date, datetime
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

from .common import IssuerMarkupChanged, absolute_https_url, clean_text, parse_source_date, require_minimum

BASE_URL = "https://www.bccard.com"
NOTICE_PATH = "/app/card/ContentsLinkActn.do?pgm_id=ind0836"
CATEGORY = "personal"
SPEC = IssuerSpec(
    code="bc",
    display_name="BC카드",
    sort_order=80,
    allowed_hosts=frozenset({"www.bccard.com"}),
    categories=(CATEGORY,),
    minimum_records=25,
)
_HEADINGS = ("No.", "상품명", "약관", "상품안내장", "개정이력", "상품출시일", "발급중단일")
_GUIDE_LABEL = re.compile(r"안내장\s*받기(?:\s*\(([^)]+)\))?")


@dataclass(frozen=True, slots=True)
class _ParsedListing:
    records: tuple[SourceRecord, ...]
    product_rows: int
    excluded_application_forms: int
    excluded_corporate_guides: int


def _normalized_identity(value: str) -> str:
    return clean_text(unicodedata.normalize("NFKC", value)).casefold()


def _root_product_code(name: str, launch_date: date) -> str:
    identity = f"{_normalized_identity(name)}\n{launch_date.isoformat()}"
    return "p-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _variant_key(label: str) -> str:
    identity = re.sub(r"[\s_]+", " ", _normalized_identity(label))
    return "v-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def _date(value: str, *, label: str) -> date:
    if not re.fullmatch(r"\d{4}\.\d{2}\.\d{2}", value):
        raise IssuerMarkupChanged(f"BC disclosure {label} is invalid")
    try:
        return parse_source_date(value)
    except ValueError as exc:
        raise IssuerMarkupChanged(f"BC disclosure {label} is invalid") from exc


def _issuance_end_date(value: str) -> str:
    if value in {"", "-"}:
        return ""
    if "개인" in value:
        match = re.search(r"개인\s*:\s*(\d{4}\.\d{2}\.\d{2})", value)
        if match is None:
            raise IssuerMarkupChanged("BC disclosure personal issuance end date is invalid")
        value = match.group(1)
    return _date(value, label="issuance end date").isoformat()


def parse_listing(page_html: str, *, discovered_at: datetime) -> _ParsedListing:
    soup = BeautifulSoup(page_html, "lxml")
    tables = [
        table
        for table in soup.select("table.tbColAc")
        if tuple(re.sub(r"\s+", "", cell.get_text()) for cell in table.select("thead th")) == _HEADINGS
    ]
    if len(tables) != 1:
        raise IssuerMarkupChanged("BC disclosure product guide table is missing or ambiguous")
    records: list[SourceRecord] = []
    product_roots: set[str] = set()
    product_rows = 0
    excluded_application_forms = 0
    excluded_corporate_guides = 0
    for row in tables[0].select("tr"):
        cells = row.find_all("td", recursive=False)
        if not cells:
            continue
        if len(cells) != len(_HEADINGS):
            raise IssuerMarkupChanged("BC disclosure product row columns changed")
        product_rows += 1
        name = clean_text(cells[1].get_text(" ", strip=True))
        if not name:
            raise IssuerMarkupChanged("BC disclosure product name is empty")
        launch_date = _date(clean_text(cells[5].get_text(" ", strip=True)), label="product launch date")
        end_status = clean_text(cells[6].get_text(" ", strip=True))
        end_date = _issuance_end_date(end_status)
        root_code = _root_product_code(name, launch_date)
        if root_code in product_roots:
            raise IssuerMarkupChanged("BC disclosure repeated a product identity")
        product_roots.add(root_code)
        variants: set[str] = set()
        row_records: list[SourceRecord] = []
        for link in cells[3].select("a[href]"):
            label = clean_text(unicodedata.normalize("NFKC", link.get_text(" ", strip=True)))
            if label == "신청서 받기":
                excluded_application_forms += 1
                continue
            match = _GUIDE_LABEL.fullmatch(label)
            if match is None:
                raise IssuerMarkupChanged("BC disclosure guide label changed")
            semantic_label = clean_text(match.group(1) or "")
            if semantic_label in {"법인", "기업"}:
                excluded_corporate_guides += 1
                continue
            variant = _variant_key(semantic_label)
            if variant in variants:
                raise IssuerMarkupChanged("BC disclosure repeated a conflicting guide label")
            variants.add(variant)
            try:
                source_url = absolute_https_url(BASE_URL, str(link.get("href")), SPEC.allowed_hosts)
            except ValueError as exc:
                raise IssuerMarkupChanged("BC disclosure guide URL is outside the HTTPS allowlist") from exc
            # httpx's canonical URL representation preserves Korean filenames
            # and percent-encodes spaces without treating them as separators.
            source_url = str(httpx.URL(source_url))
            path = urlparse(source_url).path
            file_name = unquote(PurePosixPath(path).name)
            if (
                not path.startswith("/down/individual/customer/")
                or not file_name.casefold().endswith(".pdf")
                or PurePosixPath(file_name).name != file_name
                or "\\" in file_name
                or any(ord(character) < 32 for character in file_name)
            ):
                raise IssuerMarkupChanged("BC disclosure guide PDF path is invalid")
            # Always include the semantic variant key so adding a sibling PDF
            # cannot change an existing product's identity.
            product_code = f"{root_code}--{variant}"
            row_records.append(
                SourceRecord(
                    issuer=SPEC.code,
                    product_code=product_code,
                    product_name=f"{name} ({semantic_label})" if semantic_label else name,
                    effective_date=launch_date,
                    source_version=path,
                    source_url=source_url,
                    source_post_id=product_code,
                    file_name=file_name,
                    category=CATEGORY,
                    discovered_at=discovered_at,
                    metadata={
                        "date_basis": "product_launch",
                        "product_launch_date": launch_date.isoformat(),
                        "root_product_code": root_code,
                        "guide_label": label,
                        "variant_key": variant,
                        "issuance_end_status": end_status,
                        "issuance_end_date": end_date,
                        "issuance_ended": bool(end_date) if end_status else None,
                    },
                )
            )
        if not row_records:
            raise IssuerMarkupChanged(f"BC disclosure {name} has no personal product guide")
        records.extend(row_records)
    return _ParsedListing(
        records=tuple(records),
        product_rows=product_rows,
        excluded_application_forms=excluded_application_forms,
        excluded_corporate_guides=excluded_corporate_guides,
    )


class BCAdapter:
    spec = SPEC
    parser_version = "bc.current.v1"

    def __init__(self, *, base_url: str = BASE_URL, minimum_records: int | None = None) -> None:
        self.base_url = absolute_https_url(BASE_URL, base_url, self.spec.allowed_hosts).rstrip("/")
        self.minimum_records = minimum_records or self.spec.minimum_records

    async def discover_current(self, client: httpx.AsyncClient) -> SourceSnapshot:
        started = datetime.now(UTC)
        notice_url = self.base_url + NOTICE_PATH
        response = await client.get(notice_url)
        response.raise_for_status()
        listing = parse_listing(response.text, discovered_at=started)
        require_minimum(
            list({record.metadata["root_product_code"] for record in listing.records}),
            label="BC current PDF disclosure products",
            minimum=self.minimum_records,
        )
        return snapshot_from_records(
            issuer=self.spec.code,
            source_url=notice_url,
            parser_version=self.parser_version,
            records=listing.records,
            started_at=started,
            warnings=(
                f"BC disclosure parsed {listing.product_rows} product rows; selected {len(listing.records)} "
                f"guides; excluded {listing.excluded_application_forms} application forms and "
                f"{listing.excluded_corporate_guides} corporate guides",
            ),
        )

    async def prepare_download(self, client: httpx.AsyncClient, source: SourceRecord) -> DownloadRequest:
        del client
        if source.issuer != self.spec.code:
            raise ValueError("source issuer does not match adapter")
        if source.category not in self.spec.categories:
            raise ValueError("source category does not match adapter")
        url = absolute_https_url(BASE_URL, source.source_url, self.spec.allowed_hosts)
        path = urlparse(url).path
        if (
            not path.startswith("/down/individual/customer/")
            or path != source.source_version
            or unquote(PurePosixPath(path).name) != source.file_name
        ):
            raise ValueError("source does not satisfy the BC discovery identity")
        return DownloadRequest(url=url)
