"""Hana PC disclosure cursor API, including discontinued products."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import quote

import httpx

from cardrag_worker.contracts import (
    DownloadRequest,
    IssuerSpec,
    SourceRecord,
    SourceSnapshot,
    canonical_sha256,
    snapshot_from_records,
)

from .common import IssuerMarkupChanged, absolute_https_url, clean_text, parse_source_date, require_minimum

BASE_URL = "https://www.hanacard.co.kr"
NOTICE_PATH = "/OSA95000000D.web?schID=pcd&mID=OSA95000000D&CT_ID=CDPD"
LIST_PATH = "/OSA95000000D.ajax"
MAXIMUM_PAGES = 1_000
SPEC = IssuerSpec(
    code="hana",
    display_name="하나카드",
    sort_order=60,
    allowed_hosts=frozenset({"www.hanacard.co.kr", "m.hanacard.co.kr"}),
    # The official disclosure list includes consumer and corporate products
    # without a reliable category discriminator; retain its complete scope.
    categories=("shared",),
    minimum_records=25,
)


@dataclass(frozen=True, slots=True)
class ListingPage:
    records: list[SourceRecord]
    row_fingerprints: tuple[str, ...]
    next_cursor: str
    has_more: bool | None
    total_count: int


def _required_text(raw: dict[str, object], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise IssuerMarkupChanged(f"Hana disclosure field {key} is missing or invalid")
    return value.strip()


def _more_flag(value: object, key: str) -> bool | None:
    if value is None:
        return None
    if value is True or value == "Y":
        return True
    if value is False or value == "N":
        return False
    raise IssuerMarkupChanged(f"Hana pagination field {key} is invalid")


def parse_listing(payload: object, *, discovered_at: datetime) -> ListingPage:
    if not isinstance(payload, dict) or payload.get("result") != "success":
        raise IssuerMarkupChanged("Hana disclosure API reported an error")
    body = payload.get("dataMap")
    if not isinstance(body, dict) or body.get("errorCode") not in (None, "", "0", "0000"):
        raise IssuerMarkupChanged("Hana disclosure body is invalid")
    result = body.get("RESULT_LIST")
    raw_records = result.get("data") if isinstance(result, dict) else None
    cursor = body.get("AMM_NEXT_KEY")
    raw_count = body.get("listCount")
    if (
        not isinstance(raw_records, list)
        or not isinstance(cursor, str)
        or isinstance(raw_count, bool)
        or not isinstance(raw_count, (str, int))
        or not str(raw_count).isdigit()
    ):
        raise IssuerMarkupChanged("Hana disclosure pagination is invalid")
    more = _more_flag(body.get("hasMore"), "hasMore")
    eof_more = _more_flag(body.get("AMM_EOF_SWITCH"), "AMM_EOF_SWITCH")
    if more is not None and eof_more is not None and more != eof_more:
        raise IssuerMarkupChanged("Hana pagination termination flags conflict")
    has_more = more if more is not None else eof_more
    if has_more is True and not cursor.strip():
        raise IssuerMarkupChanged("Hana pagination promises more rows without a cursor")
    records: list[SourceRecord] = []
    for raw in raw_records:
        if not isinstance(raw, dict):
            raise IssuerMarkupChanged("Hana disclosure record is invalid")
        product_code = _required_text(raw, "ADD_VAR3")
        name = _required_text(raw, "APN_FILE_NM")
        path = _required_text(raw, "APN_FILE_PH_NM")
        if not name.casefold().endswith(".pdf") or any(char in name for char in ("/", "\\", "\x00")):
            raise IssuerMarkupChanged("Hana PDF filename is invalid")
        try:
            effective = parse_source_date(_required_text(raw, "AN_SDT"))
            directory = absolute_https_url(BASE_URL, path, SPEC.allowed_hosts)
            url = absolute_https_url(directory.rstrip("/") + "/", quote(name, safe=""), SPEC.allowed_hosts)
        except ValueError as exc:
            raise IssuerMarkupChanged("Hana disclosure date or PDF URL is invalid") from exc
        issuance_flag = raw.get("ADD_VAR5")
        if issuance_flag not in (None, "Y", "N"):
            raise IssuerMarkupChanged("Hana issuance status is invalid")
        metadata: dict[str, object] = {"classification_basis": "official_shared_listing"}
        if issuance_flag is not None:
            metadata["issuance_discontinued"] = issuance_flag == "Y"
        records.append(
            SourceRecord(
                issuer=SPEC.code,
                product_code=product_code,
                product_name=clean_text(_required_text(raw, "AN_TIT_NM")),
                effective_date=effective,
                source_version=effective.strftime("%Y%m%d"),
                source_url=url,
                source_post_id=product_code,
                file_name=name,
                category="shared",
                discovered_at=discovered_at,
                metadata=metadata,
            )
        )
    return ListingPage(
        records,
        tuple(canonical_sha256(raw) for raw in raw_records),
        cursor.strip(),
        has_more,
        int(raw_count),
    )


class HanaAdapter:
    spec = SPEC
    parser_version = "hana.current.v1"

    def __init__(self, *, base_url: str = BASE_URL, minimum_records: int | None = None) -> None:
        self.base_url = absolute_https_url(BASE_URL, base_url, self.spec.allowed_hosts).rstrip("/")
        self.minimum_records = minimum_records if minimum_records is not None else self.spec.minimum_records

    async def discover_current(self, client: httpx.AsyncClient) -> SourceSnapshot:
        started = datetime.now(UTC)
        landing = await client.get(self.base_url + NOTICE_PATH)
        landing.raise_for_status()
        records: list[SourceRecord] = []
        seen_cursors: set[str] = set()
        seen_records: set[str] = set()
        cursor = ""
        total_count: int | None = None
        for _ in range(MAXIMUM_PAGES):
            response = await client.post(
                self.base_url + LIST_PATH,
                data={"CT_ID": "CDPD", "AMM_NEXT_KEY": cursor, "SEARCHKEY": ""},
                headers={"Referer": self.base_url + NOTICE_PATH, "X-Requested-With": "XMLHttpRequest"},
            )
            response.raise_for_status()
            try:
                payload = json.loads(response.content.decode("euc-kr"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise IssuerMarkupChanged("Hana response is not valid EUC-KR JSON") from exc
            page = parse_listing(payload, discovered_at=started)
            if total_count is None:
                total_count = page.total_count
            elif page.total_count not in (0, total_count):
                raise IssuerMarkupChanged("Hana disclosure total changed during pagination")
            # The API can publish the same product/PDF twice with distinct
            # registration dates. Count those raw rows, but reject a replay of
            # an identical raw row even when the rest of its page is new.
            page_ids = set(page.row_fingerprints)
            if len(page_ids) != len(page.row_fingerprints) or page_ids.intersection(seen_records):
                raise IssuerMarkupChanged("Hana pagination repeated an identical raw record")
            seen_records.update(page_ids)
            records.extend(page.records)
            if len(records) > total_count:
                raise IssuerMarkupChanged("Hana disclosure exceeded the reported total")
            if page.has_more is False or not page.next_cursor:
                if len(records) != total_count:
                    raise IssuerMarkupChanged("Hana pagination ended before the reported total")
                break
            if not page.records or len(records) == total_count:
                raise IssuerMarkupChanged("Hana pagination did not terminate at the reported total")
            if page.next_cursor in seen_cursors:
                raise IssuerMarkupChanged("Hana pagination cursor repeated")
            seen_cursors.add(page.next_cursor)
            cursor = page.next_cursor
        else:
            raise IssuerMarkupChanged("Hana disclosure exceeded its page limit")
        grouped: dict[str, list[SourceRecord]] = {}
        for record in records:
            grouped.setdefault(record.product_code, []).append(record)
        selected: list[SourceRecord] = []
        for product_code, history in grouped.items():
            latest_date = max(record.effective_date for record in history)
            latest = {record.source_id: record for record in history if record.effective_date == latest_date}
            if len(latest) != 1:
                raise IssuerMarkupChanged(f"Hana product {product_code} has conflicting latest documents")
            selected.append(next(iter(latest.values())))
        require_minimum(selected, label="Hana current PDF disclosure", minimum=self.minimum_records)
        return snapshot_from_records(
            issuer=self.spec.code,
            source_url=self.base_url + NOTICE_PATH,
            parser_version=self.parser_version,
            records=selected,
            started_at=started,
        )

    async def prepare_download(self, client: httpx.AsyncClient, source: SourceRecord) -> DownloadRequest:
        if source.issuer != self.spec.code or source.category not in self.spec.categories:
            raise ValueError("source does not match the Hana adapter")
        return DownloadRequest(url=absolute_https_url(BASE_URL, source.source_url, self.spec.allowed_hosts))
