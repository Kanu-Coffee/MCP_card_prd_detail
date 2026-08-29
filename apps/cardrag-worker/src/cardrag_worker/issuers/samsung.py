"""Samsung Card product-condition disclosure interpretation."""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import PurePosixPath
from typing import Any

import httpx

from cardrag_worker.contracts import (
    DownloadRequest,
    IssuerSpec,
    SourceRecord,
    SourceSnapshot,
    snapshot_from_records,
)

from .common import IssuerMarkupChanged, absolute_https_url, clean_text, parse_source_date, require_minimum

BASE_URL = "https://www.samsungcard.com"
NOTICE_PATH = "/company/IR/announce/product-conditions/UHPPCI0261M0.jsp"
LIST_PATH = "/service/SHPPCC0247S01"
DOWNLOAD_PATH = "/filedownload.do"
PAGE_SIZE = 100
MAXIMUM_PAGES = 100
MAXIMUM_RECORDS = 1_000
CHANNEL_CODE = "01"
BOARD_TYPE_CODE = "19"
CATEGORY = "personal_credit"

OFFICIAL_PRODUCT_CODE_METADATA_KEY = "official_product_code"
ANNOUNCEMENT_POST_ID_METADATA_KEY = "announcement_post_id"
ANNOUNCEMENT_START_DATE_METADATA_KEY = "announcement_start_date"
ANNOUNCEMENT_WRITE_DATE_METADATA_KEY = "announcement_write_date"
REGISTERED_TIMESTAMP_METADATA_KEY = "registered_timestamp"
ANNOUNCEMENT_LAST_CHANGED_TIMESTAMP_METADATA_KEY = "announcement_last_changed_timestamp"
ATTACHMENT_GROUP_METADATA_KEY = "attachment_group_no"
ENCODED_ATTACHMENT_GROUP_METADATA_KEY = "encoded_attachment_group"
ATTACHMENT_SEQUENCE_METADATA_KEY = "attachment_sequence"
VARIANT_KEY_METADATA_KEY = "variant_key"
RAW_SEMANTIC_GUIDE_NAME_METADATA_KEY = "raw_semantic_guide_name"
SEMANTIC_GUIDE_NAME_METADATA_KEY = "semantic_guide_name"
LOGICAL_GUIDE_KEY_VERSION_METADATA_KEY = "logical_guide_key_version"
LOGICAL_GUIDE_KEY_VERSION = "samsung.logical-guide-key.v1"

SPEC = IssuerSpec(
    code="samsung",
    display_name="삼성카드",
    sort_order=40,
    allowed_hosts=frozenset({"www.samsungcard.com"}),
    categories=(CATEGORY,),
    minimum_records=25,
)

_OFFICIAL_PRODUCT_CODE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_POSITIVE_INTEGER = re.compile(r"^[1-9][0-9]*$")
_ATTACHMENT_GROUP = re.compile(r"^[0-9]{1,32}$")
_ATTACHMENT_GROUP_TOKEN = re.compile(r"^(?:[A-Za-z0-9]|%2[BbFf]|%3[Dd])+$")
_REGISTERED_TIMESTAMP = re.compile(r"^[0-9]{14,20}$")
_LAST_CHANGED_TIMESTAMP = re.compile(r"^[0-9]{20}$")
_GENERIC_GUIDE_SUFFIX = re.compile(
    r"(?:[\s._/\-]*)"
    r"(?:(?:상품\s*)?(?:이용\s*)?안내장)\s*$",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class _SelectedAttachment:
    official_product_code: str
    product_name: str
    effective_date: date
    start_date: str
    write_date: str
    registered_timestamp: str
    last_changed_timestamp: str
    post_id: str
    file_name: str
    attachment_group: str
    attachment_group_token: str
    attachment_sequence: str
    raw_semantic_guide_name: str
    semantic_guide_name: str

    @property
    def stable_variant_key(self) -> str:
        digest = hashlib.sha256(self.semantic_guide_name.encode("utf-8")).hexdigest()
        return f"v-{digest[:16]}"


@dataclass(frozen=True, slots=True)
class _ParsedPage:
    total: int
    post_ids: tuple[str, ...]
    attachments: tuple[_SelectedAttachment, ...]


def _required_string(raw: dict[str, Any], key: str, *, label: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise IssuerMarkupChanged(f"Samsung {label} is invalid")
    return value.strip()


def _parse_date(value: str, *, label: str) -> date:
    try:
        return parse_source_date(value)
    except (TypeError, ValueError) as exc:
        raise IssuerMarkupChanged(f"Samsung {label} is invalid") from exc


def _parse_last_changed_timestamp(value: str) -> datetime:
    if not _LAST_CHANGED_TIMESTAMP.fullmatch(value):
        raise IssuerMarkupChanged("Samsung announcement last-changed timestamp is invalid")
    try:
        parsed = datetime.strptime(value, "%Y%m%d%H%M%S%f")
    except ValueError as exc:
        raise IssuerMarkupChanged("Samsung announcement last-changed timestamp is invalid") from exc
    if parsed.strftime("%Y%m%d%H%M%S%f") != value:
        raise IssuerMarkupChanged("Samsung announcement last-changed timestamp is invalid")
    return parsed


def _semantic_guide_identity(file_name: str) -> tuple[str, str]:
    """Return the auditable raw name and its stable logical-key input.

    Revision fields such as post, attachment group, and URL are deliberately
    excluded.  Unicode compatibility forms, case, spacing, and punctuation do
    not create a new logical variant, while meaningful letters and numbers do.
    """

    normalized_file_name = unicodedata.normalize("NFKC", clean_text(file_name))
    if not normalized_file_name.casefold().endswith(".pdf"):
        raise IssuerMarkupChanged("Samsung guide filename is invalid")
    stem = normalized_file_name[:-4].strip()
    raw_semantic_name = _GENERIC_GUIDE_SUFFIX.sub("", stem).strip(" ._/-")
    if not raw_semantic_name:
        raise IssuerMarkupChanged("Samsung guide semantic name is empty")

    folded = unicodedata.normalize("NFKC", raw_semantic_name).casefold()
    canonical_characters: list[str] = []
    for character in folded:
        category = unicodedata.category(character)
        canonical_characters.append(
            " " if character.isspace() or category.startswith(("P", "Z")) else character
        )
    semantic_name = " ".join("".join(canonical_characters).split())
    if not semantic_name:
        raise IssuerMarkupChanged("Samsung guide semantic name is empty")
    return raw_semantic_name, semantic_name


def _selected_pdf_attachments(raw: dict[str, Any], *, post_id: str) -> list[dict[str, Any]]:
    upload_files = raw.get("uploadFileList")
    if not isinstance(upload_files, list) or not upload_files:
        raise IssuerMarkupChanged(f"Samsung announcement {post_id} has no attachment list")

    pdfs: list[dict[str, Any]] = []
    for attachment in upload_files:
        if not isinstance(attachment, dict):
            raise IssuerMarkupChanged(f"Samsung announcement {post_id} has an invalid attachment")
        file_name = attachment.get("apnFileNm")
        if not isinstance(file_name, str):
            raise IssuerMarkupChanged(f"Samsung announcement {post_id} has an invalid attachment name")
        cleaned_name = clean_text(file_name)
        if cleaned_name.casefold().endswith(".pdf"):
            pdfs.append({**attachment, "apnFileNm": cleaned_name})

    guides = [attachment for attachment in pdfs if "안내장" in str(attachment["apnFileNm"])]
    if guides:
        return guides
    if len(pdfs) == 1:
        return pdfs
    raise IssuerMarkupChanged(f"Samsung announcement {post_id} does not have an unambiguous guide PDF")


def _parse_attachment(
    raw: dict[str, Any],
    *,
    official_product_code: str,
    product_name: str,
    effective_date: date,
    start_date: str,
    write_date: str,
    registered_timestamp: str,
    last_changed_timestamp: str,
    post_id: str,
) -> _SelectedAttachment:
    file_name = _required_string(raw, "apnFileNm", label="attachment filename")
    attachment_group = _required_string(raw, "apnFileGrpNo", label="attachment group")
    attachment_group_token = _required_string(
        raw,
        "apnFileGrpNoE",
        label="encrypted attachment group",
    )
    attachment_sequence = _required_string(raw, "apnFileSn", label="attachment sequence")
    if (
        PurePosixPath(file_name).name != file_name
        or "\\" in file_name
        or not file_name.casefold().endswith(".pdf")
        or not _ATTACHMENT_GROUP.fullmatch(attachment_group)
        or not _ATTACHMENT_GROUP_TOKEN.fullmatch(attachment_group_token)
        or not _POSITIVE_INTEGER.fullmatch(attachment_sequence)
    ):
        raise IssuerMarkupChanged(f"Samsung announcement {post_id} attachment identity is invalid")
    raw_semantic_guide_name, semantic_guide_name = _semantic_guide_identity(file_name)
    return _SelectedAttachment(
        official_product_code=official_product_code,
        product_name=product_name,
        effective_date=effective_date,
        start_date=start_date,
        write_date=write_date,
        registered_timestamp=registered_timestamp,
        last_changed_timestamp=last_changed_timestamp,
        post_id=post_id,
        file_name=file_name,
        attachment_group=attachment_group,
        attachment_group_token=attachment_group_token,
        attachment_sequence=attachment_sequence,
        raw_semantic_guide_name=raw_semantic_guide_name,
        semantic_guide_name=semantic_guide_name,
    )


def parse_listing_page(payload: object, *, discovered_at: datetime, page_size: int) -> _ParsedPage:
    del discovered_at
    if not isinstance(payload, dict):
        raise IssuerMarkupChanged("Samsung disclosure lookup failed")
    common = payload.get("common")
    if not isinstance(common, dict) or common.get("procsRsDvC") != "0":
        raise IssuerMarkupChanged("Samsung disclosure lookup failed")
    message = payload.get("message")
    if not isinstance(message, dict) or message.get("msgMarkDvC") != "00":
        raise IssuerMarkupChanged("Samsung disclosure lookup returned an invalid result binding")
    raw_total = payload.get("totInqrCt")
    rows = payload.get("blbdInqrRsList")
    if (
        not isinstance(raw_total, str)
        or not raw_total.isdigit()
        or not isinstance(rows, list)
        or len(rows) > page_size
    ):
        raise IssuerMarkupChanged("Samsung disclosure response is invalid")
    total = int(raw_total)

    post_ids: list[str] = []
    selected: list[_SelectedAttachment] = []
    for raw in rows:
        if not isinstance(raw, dict):
            raise IssuerMarkupChanged("Samsung disclosure record is invalid")
        if raw.get("itgBlbdChnlDvC") != CHANNEL_CODE or raw.get("itgBlbdTpDvC") != BOARD_TYPE_CODE:
            raise IssuerMarkupChanged("Samsung disclosure record binding is invalid")
        official_product_code = _required_string(raw, "bgdAlncPdC", label="product code")
        product_name = clean_text(_required_string(raw, "bltnbmTitNm", label="product name"))
        post_id = _required_string(raw, "itgBlbdSn", label="announcement post id")
        start_date = _required_string(raw, "bltnStrtdt", label="announcement start date")
        write_date = _required_string(raw, "bltnbmWrteDt", label="announcement write date")
        registered_timestamp = _required_string(raw, "sysFstRgTs", label="registered timestamp")
        last_changed_timestamp = _required_string(
            raw,
            "sysLtChTs",
            label="announcement last-changed timestamp",
        )
        if (
            not _OFFICIAL_PRODUCT_CODE.fullmatch(official_product_code)
            or not _POSITIVE_INTEGER.fullmatch(post_id)
            or not _REGISTERED_TIMESTAMP.fullmatch(registered_timestamp)
            or not _LAST_CHANGED_TIMESTAMP.fullmatch(last_changed_timestamp)
        ):
            raise IssuerMarkupChanged(f"Samsung announcement {post_id} identity is invalid")
        effective_date = _parse_date(start_date, label="announcement start date")
        _parse_date(write_date, label="announcement write date")
        _parse_date(registered_timestamp[:8], label="registered timestamp")
        _parse_last_changed_timestamp(last_changed_timestamp)

        post_ids.append(post_id)
        for attachment in _selected_pdf_attachments(raw, post_id=post_id):
            selected.append(
                _parse_attachment(
                    attachment,
                    official_product_code=official_product_code,
                    product_name=product_name,
                    effective_date=effective_date,
                    start_date=start_date,
                    write_date=write_date,
                    registered_timestamp=registered_timestamp,
                    last_changed_timestamp=last_changed_timestamp,
                    post_id=post_id,
                )
            )
    return _ParsedPage(total=total, post_ids=tuple(post_ids), attachments=tuple(selected))


def _download_url(base_url: str, attachment_group_token: str, attachment_sequence: str) -> str:
    return absolute_https_url(
        base_url,
        f"{DOWNLOAD_PATH}?grpNo={attachment_group_token}&sn={attachment_sequence}",
        SPEC.allowed_hosts,
    )


def _variant_product_code(attachment: _SelectedAttachment) -> tuple[str, str]:
    key = attachment.stable_variant_key
    return f"{attachment.official_product_code}--{key}", key


def _source_records(
    attachments: list[_SelectedAttachment],
    *,
    discovered_at: datetime,
    base_url: str,
) -> list[SourceRecord]:
    records: list[SourceRecord] = []
    seen_product_codes: set[str] = set()
    seen_source_keys: set[tuple[str, str, str]] = set()
    seen_logical_keys: set[tuple[str, str]] = set()
    for attachment in attachments:
        source_key = (
            attachment.post_id,
            attachment.attachment_group,
            attachment.attachment_sequence,
        )
        if source_key in seen_source_keys:
            raise IssuerMarkupChanged("Samsung disclosure repeated an attachment identity")
        seen_source_keys.add(source_key)
        product_code, variant_key = _variant_product_code(attachment)
        logical_key = (attachment.official_product_code, variant_key)
        if logical_key in seen_logical_keys:
            raise IssuerMarkupChanged(
                "Samsung disclosure repeated a logical guide identity for one official product"
            )
        seen_logical_keys.add(logical_key)
        if product_code in seen_product_codes:
            raise IssuerMarkupChanged("Samsung disclosure produced a duplicate product identity")
        seen_product_codes.add(product_code)
        metadata = {
            OFFICIAL_PRODUCT_CODE_METADATA_KEY: attachment.official_product_code,
            ANNOUNCEMENT_POST_ID_METADATA_KEY: attachment.post_id,
            ANNOUNCEMENT_START_DATE_METADATA_KEY: attachment.start_date,
            ANNOUNCEMENT_WRITE_DATE_METADATA_KEY: attachment.write_date,
            REGISTERED_TIMESTAMP_METADATA_KEY: attachment.registered_timestamp,
            ANNOUNCEMENT_LAST_CHANGED_TIMESTAMP_METADATA_KEY: attachment.last_changed_timestamp,
            ATTACHMENT_GROUP_METADATA_KEY: attachment.attachment_group,
            ENCODED_ATTACHMENT_GROUP_METADATA_KEY: attachment.attachment_group_token,
            ATTACHMENT_SEQUENCE_METADATA_KEY: attachment.attachment_sequence,
            VARIANT_KEY_METADATA_KEY: variant_key,
            RAW_SEMANTIC_GUIDE_NAME_METADATA_KEY: attachment.raw_semantic_guide_name,
            SEMANTIC_GUIDE_NAME_METADATA_KEY: attachment.semantic_guide_name,
            LOGICAL_GUIDE_KEY_VERSION_METADATA_KEY: LOGICAL_GUIDE_KEY_VERSION,
        }
        records.append(
            SourceRecord(
                issuer=SPEC.code,
                product_code=product_code,
                product_name=attachment.product_name,
                effective_date=attachment.effective_date,
                source_version=attachment.last_changed_timestamp,
                source_url=_download_url(
                    base_url,
                    attachment.attachment_group_token,
                    attachment.attachment_sequence,
                ),
                source_post_id=(
                    f"{attachment.post_id}:{attachment.attachment_group}:{attachment.attachment_sequence}"
                ),
                file_name=attachment.file_name,
                category=CATEGORY,
                discovered_at=discovered_at,
                metadata=metadata,
            )
        )
    return records


def _validate_download_source_identity(source: SourceRecord, *, base_url: str) -> None:
    metadata = dict(source.metadata)
    expected_keys = {
        OFFICIAL_PRODUCT_CODE_METADATA_KEY,
        ANNOUNCEMENT_POST_ID_METADATA_KEY,
        ANNOUNCEMENT_START_DATE_METADATA_KEY,
        ANNOUNCEMENT_WRITE_DATE_METADATA_KEY,
        REGISTERED_TIMESTAMP_METADATA_KEY,
        ANNOUNCEMENT_LAST_CHANGED_TIMESTAMP_METADATA_KEY,
        ATTACHMENT_GROUP_METADATA_KEY,
        ENCODED_ATTACHMENT_GROUP_METADATA_KEY,
        ATTACHMENT_SEQUENCE_METADATA_KEY,
        VARIANT_KEY_METADATA_KEY,
        RAW_SEMANTIC_GUIDE_NAME_METADATA_KEY,
        SEMANTIC_GUIDE_NAME_METADATA_KEY,
        LOGICAL_GUIDE_KEY_VERSION_METADATA_KEY,
    }
    if set(metadata) != expected_keys or any(not isinstance(value, str) for value in metadata.values()):
        raise ValueError("source does not satisfy the Samsung discovery identity")
    official_product_code = str(metadata[OFFICIAL_PRODUCT_CODE_METADATA_KEY])
    post_id = str(metadata[ANNOUNCEMENT_POST_ID_METADATA_KEY])
    start_date = str(metadata[ANNOUNCEMENT_START_DATE_METADATA_KEY])
    write_date = str(metadata[ANNOUNCEMENT_WRITE_DATE_METADATA_KEY])
    registered_timestamp = str(metadata[REGISTERED_TIMESTAMP_METADATA_KEY])
    last_changed_timestamp = str(metadata[ANNOUNCEMENT_LAST_CHANGED_TIMESTAMP_METADATA_KEY])
    attachment_group = str(metadata[ATTACHMENT_GROUP_METADATA_KEY])
    attachment_group_token = str(metadata[ENCODED_ATTACHMENT_GROUP_METADATA_KEY])
    attachment_sequence = str(metadata[ATTACHMENT_SEQUENCE_METADATA_KEY])
    variant_key = str(metadata[VARIANT_KEY_METADATA_KEY])
    raw_semantic_guide_name = str(metadata[RAW_SEMANTIC_GUIDE_NAME_METADATA_KEY])
    semantic_guide_name = str(metadata[SEMANTIC_GUIDE_NAME_METADATA_KEY])
    logical_guide_key_version = str(metadata[LOGICAL_GUIDE_KEY_VERSION_METADATA_KEY])
    try:
        expected_effective_date = parse_source_date(start_date)
        parse_source_date(write_date)
        parse_source_date(registered_timestamp[:8])
        _parse_last_changed_timestamp(last_changed_timestamp)
        expected_url = _download_url(base_url, attachment_group_token, attachment_sequence)
        expected_raw_semantic_name, expected_semantic_name = _semantic_guide_identity(source.file_name)
    except (IssuerMarkupChanged, ValueError) as exc:
        raise ValueError("source does not satisfy the Samsung discovery identity") from exc
    expected_stable_variant_key = (
        "v-" + hashlib.sha256(expected_semantic_name.encode("utf-8")).hexdigest()[:16]
    )
    expected_product_code = f"{official_product_code}--{expected_stable_variant_key}"
    if (
        source.issuer != SPEC.code
        or source.category != CATEGORY
        or source.document_type != "product_description"
        or not _OFFICIAL_PRODUCT_CODE.fullmatch(official_product_code)
        or not _POSITIVE_INTEGER.fullmatch(post_id)
        or not _ATTACHMENT_GROUP.fullmatch(attachment_group)
        or not _ATTACHMENT_GROUP_TOKEN.fullmatch(attachment_group_token)
        or not _POSITIVE_INTEGER.fullmatch(attachment_sequence)
        or not _REGISTERED_TIMESTAMP.fullmatch(registered_timestamp)
        or not _LAST_CHANGED_TIMESTAMP.fullmatch(last_changed_timestamp)
        or logical_guide_key_version != LOGICAL_GUIDE_KEY_VERSION
        or raw_semantic_guide_name != expected_raw_semantic_name
        or semantic_guide_name != expected_semantic_name
        or variant_key != expected_stable_variant_key
        or source.product_code != expected_product_code
        or source.effective_date != expected_effective_date
        or source.source_version != last_changed_timestamp
        or source.source_url != expected_url
        or source.source_post_id != f"{post_id}:{attachment_group}:{attachment_sequence}"
        or PurePosixPath(source.file_name).name != source.file_name
        or "\\" in source.file_name
    ):
        raise ValueError("source does not satisfy the Samsung discovery identity")


class SamsungAdapter:
    spec = SPEC
    parser_version = "samsung.current.v1"

    def __init__(
        self,
        *,
        base_url: str = BASE_URL,
        minimum_records: int | None = None,
        page_size: int = PAGE_SIZE,
        maximum_pages: int = MAXIMUM_PAGES,
        maximum_records: int = MAXIMUM_RECORDS,
    ) -> None:
        if page_size < 1 or maximum_pages < 1 or maximum_records < 1 or page_size > maximum_records:
            raise ValueError("Samsung pagination limits must be positive and internally consistent")
        self.base_url = base_url.rstrip("/")
        self.minimum_records = minimum_records or self.spec.minimum_records
        self.page_size = page_size
        self.maximum_pages = maximum_pages
        self.maximum_records = maximum_records

    def _conditions(self, page_number: int) -> dict[str, str]:
        return {
            "no1PgeSize": str(self.page_size),
            "pgeNo": str(page_number),
            "itgBlbdSn": "",
            "itgBlbdChnlDvC": CHANNEL_CODE,
            "itgBlbdTpDvC": BOARD_TYPE_CODE,
            "bltnbmTitNm": "",
            "bltnbmCn": "",
            "seaKeywCn": "",
            "seaKeywNm": "",
            "aryCriCn": "sysFstRgTs",
            "aryDvCn": "DESC",
        }

    async def _post_page(
        self,
        client: httpx.AsyncClient,
        *,
        page_number: int,
        discovered_at: datetime,
    ) -> _ParsedPage:
        response = await client.post(
            self.base_url + LIST_PATH,
            json={"cndt": self._conditions(page_number)},
            headers={
                "Accept": "application/json",
                "Referer": self.base_url + NOTICE_PATH,
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            raise IssuerMarkupChanged("Samsung disclosure response is not JSON") from exc
        return parse_listing_page(payload, discovered_at=discovered_at, page_size=self.page_size)

    async def discover_current(self, client: httpx.AsyncClient) -> SourceSnapshot:
        started = datetime.now(UTC)
        landing = await client.get(self.base_url + NOTICE_PATH)
        landing.raise_for_status()

        first_page = await self._post_page(client, page_number=1, discovered_at=started)
        if first_page.total > self.maximum_records:
            raise IssuerMarkupChanged("Samsung disclosure exceeded its record limit")
        required_pages = max(1, math.ceil(first_page.total / self.page_size))
        if required_pages > self.maximum_pages:
            raise IssuerMarkupChanged("Samsung disclosure exceeded its page limit")

        pages = [first_page]
        for page_number in range(2, required_pages + 1):
            page = await self._post_page(client, page_number=page_number, discovered_at=started)
            if page.total != first_page.total:
                raise IssuerMarkupChanged("Samsung disclosure total changed during pagination")
            pages.append(page)

        post_ids = [post_id for page in pages for post_id in page.post_ids]
        if len(post_ids) != first_page.total or len(set(post_ids)) != len(post_ids):
            raise IssuerMarkupChanged(
                "Samsung disclosure pagination did not produce a stable complete listing"
            )
        attachments = [attachment for page in pages for attachment in page.attachments]
        if len(attachments) > self.maximum_records:
            raise IssuerMarkupChanged("Samsung disclosure selected PDFs exceeded its record limit")
        records = _source_records(attachments, discovered_at=started, base_url=self.base_url)
        require_minimum(
            records,
            label="Samsung current PDF disclosure",
            minimum=self.minimum_records,
        )
        return snapshot_from_records(
            issuer=self.spec.code,
            source_url=self.base_url + NOTICE_PATH,
            parser_version=self.parser_version,
            records=records,
            started_at=started,
        )

    async def prepare_download(self, client: httpx.AsyncClient, source: SourceRecord) -> DownloadRequest:
        del client
        _validate_download_source_identity(source, base_url=self.base_url)
        return DownloadRequest(
            url=source.source_url,
            headers={"Referer": self.base_url + NOTICE_PATH},
        )
