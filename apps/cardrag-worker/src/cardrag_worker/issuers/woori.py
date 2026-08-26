"""Woori disclosure interpretation and RAONK request construction."""

from __future__ import annotations

import base64
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

from cardrag_worker.contracts import (
    DownloadRequest,
    IssuerSpec,
    ProtectedSourceAllowance,
    SourceRecord,
    SourceSnapshot,
    snapshot_from_records,
)

from .common import (
    IssuerMarkupChanged,
    absolute_https_url,
    first,
    natural_version_key,
    parse_source_date,
    require_minimum,
)

BASE_URL = "https://pc.wooricard.com"
NOTICE_PATH = "/dcpc/yh1/cct/cct11/prdntc/H1CCT211S09.do"
MAIN_PATH = "/dcpc/yh1/cct/cct11/prdntc/getMainDataList.pwkjson"
DETAIL_PATH = "/dcpc/yh1/cct/cct11/prdntc/getDetailDataList.pwkjson"
RAONK_PATH = "/dcpc/kupload/handler/raonkhandler.jsp"
RAONK_AES_KEY = b"2018.1548512.15".ljust(16, b"\0")
RAONK_STD_B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="
RAONK_CUSTOM_B64 = "hituvabcdejklmnopqrxyzsfgwBCDEANOPQRSFGHIJKLYZMTUVWX5890167234+/="

SPEC = IssuerSpec(
    code="woori",
    display_name="우리카드",
    sort_order=10,
    allowed_hosts=frozenset({"pc.wooricard.com"}),
    categories=("personal",),
    minimum_records=25,
    protected_source_allowances=(
        ProtectedSourceAllowance(
            source_id="source_854d1b4effb9473acc693aacc76484de24bfb658fb31df126b908d093a4e815a",
            product_code="102958",
            source_version="2",
            source_url=(
                "https://pc.wooricard.com/upload/cardClause/2024/3/19/"
                "574d01a3-65d0-42f3-bec0-e82c77710f49.pdf"
            ),
            sha256="ea143c393bed26325e75ed55be266c07281678a1e37cac0fc53d6b248c3f6f46",
            size_bytes=2_132_864,
            magic="FASOO_DRMONE",
        ),
        ProtectedSourceAllowance(
            source_id="source_caa72dffbeafe404460e0a0a427467b5d99ee619c802a4b6db77719f666d0854",
            product_code="203988",
            source_version="11",
            source_url=(
                "https://pc.wooricard.com/upload/cardClause/2021/10/5/"
                "be9faab5-be15-4a2a-9db1-9189aafc7852.pdf"
            ),
            sha256="1fefe29375616a983941675b6394e5f8406f601cef362ffb799cd61f49c8f0e3",
            size_bytes=1_097_360,
            magic="FASOO_DRMONE",
        ),
        ProtectedSourceAllowance(
            source_id="source_8aea463c3fc9d8e43ae5dd894af4ca54d47686dc9a77326d1ab6576f34f074bc",
            product_code="832388",
            source_version="11",
            source_url=(
                "https://pc.wooricard.com/upload/cardClause/2026/8/11/"
                "f1f9d290-30e9-43ea-abe0-6f43e947eaa5.pdf"
            ),
            sha256="dfba0d8a61a0ec3783f133be6e45c2e465a6bcbd83f056b6b68048a0643e5c61",
            size_bytes=417_596,
            magic="SCDSA004",
        ),
    ),
)


def _walk(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def parse_detail_records(
    payload: dict[str, Any] | list[Any],
    *,
    product_code: str,
    product_name: str,
    discovered_at: datetime,
) -> list[SourceRecord]:
    rows: list[SourceRecord] = []
    for raw in _walk(payload):
        file_path = first(raw, "filePath", "file", "s1File")
        file_name = first(raw, "fileNm", "fileName", "s1Nm") or PurePosixPath(file_path).name
        begin = first(raw, "beginDt", "beginDate", "effectiveDate")
        version = first(raw, "gdccVer", "version")
        if not file_path or not begin or not version or not file_name.casefold().endswith(".pdf"):
            continue
        rows.append(
            SourceRecord(
                issuer=SPEC.code,
                product_code=product_code,
                product_name=product_name,
                effective_date=parse_source_date(begin),
                source_version=version.removeprefix("v"),
                source_url=absolute_https_url(BASE_URL, file_path, SPEC.allowed_hosts),
                source_post_id=PurePosixPath(file_path).stem,
                file_name=file_name,
                category="personal",
                discovered_at=discovered_at,
                metadata={"raonk_file_path": file_path},
            )
        )
    rows.sort(
        key=lambda row: (row.effective_date, natural_version_key(row.source_version)),
        reverse=True,
    )
    return rows[:1]


def raonk_plaintext(file_path: str, file_name: str, *, guid: str | None = None) -> str:
    if not file_path.startswith("/") or ".." in PurePosixPath(file_path).parts:
        raise ValueError("invalid RAONK file path")
    if PurePosixPath(file_name).name != file_name or not file_name.casefold().endswith(".pdf"):
        raise ValueError("invalid RAONK file name")
    return (
        "kc\fc11"
        "\vk01\f2"
        f"\vk12\f{guid or uuid.uuid4().hex}"
        "\vk05\f0"
        f"\vk26\f/data/upload/uploadfiles{file_path}"
        f"\vk31\f{file_name}"
        f"\vk21\f{file_path}"
    )


def encrypt_raonk(plaintext: str) -> str:
    padder = PKCS7(128).padder()
    padded = padder.update(plaintext.encode()) + padder.finalize()
    encryptor = Cipher(algorithms.AES(RAONK_AES_KEY), modes.CBC(RAONK_AES_KEY)).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    encoded = base64.b64encode(ciphertext).decode()
    return encoded.translate(str.maketrans(RAONK_STD_B64, RAONK_CUSTOM_B64)).replace("+", "%2B")


class WooriAdapter:
    spec = SPEC
    parser_version = "woori.current.v1"

    def __init__(self, *, base_url: str = BASE_URL, minimum_records: int | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.minimum_records = minimum_records or self.spec.minimum_records

    async def _post_json(self, client: httpx.AsyncClient, path: str, body: dict[str, Any]) -> dict[str, Any]:
        response = await client.post(
            self.base_url + path,
            json=body,
            headers={"Proworks-Body": "Y", "X-Requested-With": "XMLHttpRequest"},
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise IssuerMarkupChanged("Woori API response is not an object")
        header = payload.get("elHeader") or {}
        if not isinstance(header, dict) or header.get("resSuc") is False:
            code = header.get("resCode") if isinstance(header, dict) else "invalid-header"
            raise IssuerMarkupChanged(f"Woori API error {code}")
        return payload

    async def discover_current(self, client: httpx.AsyncClient) -> SourceSnapshot:
        started = datetime.now(UTC)
        landing = await client.get(self.base_url + NOTICE_PATH)
        landing.raise_for_status()
        main = await self._post_json(
            client,
            MAIN_PATH,
            {"cct11PrdntcAgrmReqVo": {"part": 0, "pageIndex": "1", "pageSize": "1000"}},
        )
        products = list((main.get("mainDataList") or {}).get("cct11PrdntcAgrmMainVo") or [])
        require_minimum(products, label="Woori product listing", minimum=self.minimum_records)
        records: list[SourceRecord] = []
        for product in products:
            if product.get("goodsDescAt") != "Y":
                continue
            code = str(product.get("code") or "").strip()
            name = str(product.get("codeName") or "").strip()
            if not code or not name:
                continue
            detail = await self._post_json(
                client,
                DETAIL_PATH,
                {
                    "cct11PrdntcAgrmReqVo": {
                        "part": 0,
                        "searchPrdCd": code,
                        "searchCrdSe": "01",
                        "guideTypeCd": "01",
                        "pageIndex": "1",
                        "pageSize": "100",
                    }
                },
            )
            records.extend(
                parse_detail_records(
                    detail,
                    product_code=code,
                    product_name=name,
                    discovered_at=started,
                )
            )
        require_minimum(records, label="Woori current PDF disclosure", minimum=self.minimum_records)
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
        path = str(source.metadata.get("raonk_file_path") or "")
        return DownloadRequest(
            url=self.base_url + RAONK_PATH,
            method="POST",
            form={"k01": encrypt_raonk(raonk_plaintext(path, source.file_name))},
            headers={"Referer": self.base_url + NOTICE_PATH},
        )
