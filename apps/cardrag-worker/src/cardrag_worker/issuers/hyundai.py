"""Hyundai's official product guide listing and attachment history."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from urllib.parse import quote

import httpx
from bs4 import BeautifulSoup

from cardrag_worker.contracts import (
    DownloadRequest,
    IssuerSpec,
    ProtectedSourceAllowance,
    SourceRecord,
    SourceSnapshot,
    snapshot_from_records,
)

from .common import IssuerMarkupChanged, absolute_https_url, clean_text, parse_source_date, require_minimum

BASE_URL = "https://www.hyundaicard.com"
NOTICE_PATH = "/cpu/ug/CPUUG2001_04.hc"
DETAIL_PATH = "/cpu/ug/apiCPUUG2001_0404.hc"
PDF_PATH = "/upload/card/"
SPEC = IssuerSpec(
    code="hyundai",
    display_name="현대카드",
    sort_order=50,
    allowed_hosts=frozenset({"www.hyundaicard.com"}),
    # The official page mixes consumer and corporate guides without a
    # machine-readable discriminator. Preserve its full published coverage.
    categories=("shared",),
    minimum_records=25,
    protected_source_allowances=(
        # Official latest LG U+-Hyundai M guide observed 2026-09-06. Keep the
        # exact source and byte binding so an origin change fails validation.
        ProtectedSourceAllowance(
            source_id="source_1841d83805f532fc31aa69249609448312dbb2b2a210e253d610b4d3a9ca9472",
            product_code="12396",
            source_version="20180822",
            source_url="https://www.hyundaicard.com/upload/card/210205_00687.pdf",
            sha256="605b2437087fb22300c9f060d72a1bc437b05373feff2e5196de22e356f228e6",
            size_bytes=317_916,
            magic="SCDSA004",
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class ListingProduct:
    product_code: str
    product_name: str
    launch_date: date | None
    discontinued_date: date | None


class UnavailableEffectiveDate(ValueError):
    """The official attachment uses its explicit unknown-date sentinel."""


def _listing_date(text: str, label: str) -> date | None:
    if label not in text:
        return None
    match = re.search(rf"{label}\s*:\s*(\d{{4}})\.\s*(\d{{1,2}})\.\s*(\d{{1,2}})", text)
    if match is None:
        raise IssuerMarkupChanged(f"Hyundai {label} is invalid")
    try:
        return date(*(int(part) for part in match.groups()))
    except ValueError as exc:
        raise IssuerMarkupChanged(f"Hyundai {label} is invalid") from exc


def parse_listing(page_html: str) -> list[ListingProduct]:
    soup = BeautifulSoup(page_html, "lxml")
    products: dict[str, ListingProduct] = {}
    for node in soup.select("[onclick*='getDownloadList(']"):
        code = str(node.get("sqno") or "").strip()
        name = clean_text(str(node.get("webblbdtitl") or ""))
        match = re.search(r"getDownloadList\(\s*(\d+)\s*,", str(node.get("onclick") or ""))
        if not code.isdigit() or not name or match is None or match.group(1) != code:
            raise IssuerMarkupChanged("Hyundai listing product identity is invalid")
        row_text = node.parent.get_text(" ", strip=True) if node.parent else ""
        product = ListingProduct(
            product_code=code,
            product_name=name,
            launch_date=_listing_date(row_text, "상품출시일"),
            discontinued_date=_listing_date(row_text, "발급중단일"),
        )
        if code in products and products[code] != product:
            raise IssuerMarkupChanged(f"Hyundai listing contains conflicting product {code}")
        products[code] = product
    return list(products.values())


def parse_detail(payload: object, *, product: ListingProduct, discovered_at: datetime) -> SourceRecord | None:
    if not isinstance(payload, dict):
        raise IssuerMarkupChanged("Hyundai attachment response is not an object")
    header = payload.get("hdr")
    if header is not None and (not isinstance(header, dict) or header.get("rsltCd") != "0000"):
        raise IssuerMarkupChanged("Hyundai attachment API reported an error")
    body = payload.get("bdy")
    if not isinstance(body, dict) or body.get("rsltCode") not in (None, "", "0000"):
        raise IssuerMarkupChanged("Hyundai attachment body is invalid")
    result = body.get("result")
    attachments = result.get("cpuug2001DAO") if isinstance(result, dict) else None
    if not isinstance(attachments, list):
        raise IssuerMarkupChanged(f"Hyundai product {product.product_code} attachment history is invalid")
    if not attachments:
        return None
    dated_attachments: list[tuple[date, str]] = []
    unavailable_date = False
    for raw in attachments:
        if not isinstance(raw, dict):
            raise IssuerMarkupChanged("Hyundai attachment is invalid")
        name = raw.get("apndFileNm")
        effective_raw = raw.get("agmRrfmDt")
        parent_code = raw.get("webPsnlBlbdSqno")
        if (
            not isinstance(name, str)
            or not isinstance(effective_raw, str)
            or (parent_code not in (None, "", product.product_code))
        ):
            raise IssuerMarkupChanged(f"Hyundai product {product.product_code} attachment is invalid")
        if effective_raw == "99999999":
            unavailable_date = True
            continue
        try:
            effective = parse_source_date(effective_raw)
        except ValueError as exc:
            raise IssuerMarkupChanged(f"Hyundai product {product.product_code} date is invalid") from exc
        dated_attachments.append((effective, name.strip()))
    if unavailable_date:
        raise UnavailableEffectiveDate(
            f"Hyundai product {product.product_code}: source effective date unavailable (99999999)"
        )
    # Historical rows can deliberately omit a withdrawn file. Date and parent
    # identity remain required for every row so a missing current file never
    # causes a silent fallback to an older document.
    newest_date = max(effective for effective, _ in dated_attachments)
    records: list[SourceRecord] = []
    for effective, name in dated_attachments:
        if effective != newest_date:
            continue
        if not name.casefold().endswith(".pdf") or any(char in name for char in ("/", "\\", "\x00")):
            raise IssuerMarkupChanged(f"Hyundai product {product.product_code} latest attachment is invalid")
        metadata: dict[str, object] = {
            "classification_basis": "official_shared_listing",
            "issuance_discontinued": product.discontinued_date is not None,
        }
        if product.launch_date is not None:
            metadata["product_launch_date"] = product.launch_date.isoformat()
        if product.discontinued_date is not None:
            metadata["issuance_discontinued_date"] = product.discontinued_date.isoformat()
        records.append(
            SourceRecord(
                issuer=SPEC.code,
                product_code=product.product_code,
                product_name=product.product_name,
                effective_date=effective,
                source_version=effective.strftime("%Y%m%d"),
                source_url=absolute_https_url(
                    BASE_URL, PDF_PATH + quote(name.strip(), safe=""), SPEC.allowed_hosts
                ),
                source_post_id=product.product_code,
                file_name=name.strip(),
                category="shared",
                discovered_at=discovered_at,
                metadata=metadata,
            )
        )
    newest = {row.source_id: row for row in records}
    if len(newest) != 1:
        raise IssuerMarkupChanged(
            f"Hyundai product {product.product_code} has conflicting latest attachments"
        )
    return next(iter(newest.values()))


class HyundaiAdapter:
    spec = SPEC
    parser_version = "hyundai.current.v1"

    def __init__(self, *, base_url: str = BASE_URL, minimum_records: int | None = None) -> None:
        self.base_url = absolute_https_url(BASE_URL, base_url, self.spec.allowed_hosts).rstrip("/")
        self.minimum_records = minimum_records if minimum_records is not None else self.spec.minimum_records

    async def discover_current(self, client: httpx.AsyncClient) -> SourceSnapshot:
        started = datetime.now(UTC)
        landing = await client.get(self.base_url + NOTICE_PATH)
        landing.raise_for_status()
        products = parse_listing(landing.text)
        require_minimum(products, label="Hyundai product listing", minimum=self.minimum_records)
        records: list[SourceRecord] = []
        warnings: list[str] = []
        for product in products:
            response = await client.post(
                self.base_url + DETAIL_PATH,
                data={"sqno": product.product_code},
                headers={"Referer": self.base_url + NOTICE_PATH, "X-Requested-With": "XMLHttpRequest"},
            )
            response.raise_for_status()
            try:
                payload = response.json()
            except ValueError as exc:
                raise IssuerMarkupChanged("Hyundai attachment response is not JSON") from exc
            try:
                record = parse_detail(payload, product=product, discovered_at=started)
            except UnavailableEffectiveDate as exc:
                warnings.append(str(exc))
                continue
            if record is None:
                warnings.append(f"Hyundai product {product.product_code}: no published PDF attachment")
            else:
                records.append(record)
        require_minimum(records, label="Hyundai current PDF disclosure", minimum=self.minimum_records)
        return snapshot_from_records(
            issuer=self.spec.code,
            source_url=self.base_url + NOTICE_PATH,
            parser_version=self.parser_version,
            records=records,
            started_at=started,
            warnings=warnings,
        )

    async def prepare_download(self, client: httpx.AsyncClient, source: SourceRecord) -> DownloadRequest:
        if source.issuer != self.spec.code or source.category not in self.spec.categories:
            raise ValueError("source does not match the Hyundai adapter")
        return DownloadRequest(url=absolute_https_url(BASE_URL, source.source_url, self.spec.allowed_hosts))
