"""Woori Card product-disclosure adapter without legacy script imports."""

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
from pydantic import AnyHttpUrl

from cardrag.domain import Issuer

from .base import DiscoveryMode, IssuerMarkupChanged, SourceRecord, SourceSnapshot
from .common import (
    absolute_https_url,
    canonical_snapshot,
    natural_version_key,
    parse_source_date,
    require_nonempty,
)

BASE_URL = "https://pc.wooricard.com"
NOTICE_PATH = "/dcpc/yh1/cct/cct11/prdntc/H1CCT211S09.do"
MAIN_PATH = "/dcpc/yh1/cct/cct11/prdntc/getMainDataList.pwkjson"
DETAIL_PATH = "/dcpc/yh1/cct/cct11/prdntc/getDetailDataList.pwkjson"
RAONK_PATH = "/dcpc/kupload/handler/raonkhandler.jsp"
RAONK_AES_KEY = b"2018.1548512.15".ljust(16, b"\0")
RAONK_STD_B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="
RAONK_CUSTOM_B64 = "hituvabcdejklmnopqrxyzsfgwBCDEANOPQRSFGHIJKLYZMTUVWX5890167234+/="


def _first(raw: dict[str, Any], *keys: str) -> str:
    for key in keys:
        if raw.get(key) is not None and str(raw[key]).strip():
            return str(raw[key]).strip()
    return ""


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
    current_only: bool,
    discovered_at: datetime,
) -> list[SourceRecord]:
    parsed: list[SourceRecord] = []
    for raw in _walk(payload):
        file_path = _first(raw, "filePath", "file", "s1File")
        file_name = _first(raw, "fileNm", "fileName", "s1Nm") or PurePosixPath(file_path).name
        if not file_name.casefold().endswith(".pdf"):
            continue
        begin = _first(raw, "beginDt", "beginDate", "effectiveDate")
        version = _first(raw, "gdccVer", "version")
        if not file_path or not begin or not version:
            continue
        url = absolute_https_url(BASE_URL, file_path, frozenset({"pc.wooricard.com"}))
        parsed.append(
            SourceRecord(
                issuer=Issuer.WOORI,
                product_code=product_code,
                product_name=product_name,
                effective_date=parse_source_date(begin),
                source_version=version.removeprefix("v"),
                source_url=AnyHttpUrl(url),
                source_post_id=PurePosixPath(file_path).stem,
                file_name=file_name,
                category="personal",
                is_current=False,
                discovered_at=discovered_at,
                metadata={"raonk_file_path": file_path},
            )
        )
    parsed.sort(
        key=lambda item: (item.effective_date, natural_version_key(item.source_version)),
        reverse=True,
    )
    if parsed:
        parsed[0] = parsed[0].model_copy(update={"is_current": True})
    return parsed[:1] if current_only else parsed


def raonk_plaintext(file_path: str, file_name: str, *, guid: str | None = None) -> str:
    if not file_path.startswith("/") or ".." in PurePosixPath(file_path).parts:
        raise ValueError("invalid RAONK file path")
    if PurePosixPath(file_name).name != file_name or not file_name.lower().endswith(".pdf"):
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
    issuer = Issuer.WOORI
    allowed_hosts = frozenset({"pc.wooricard.com"})
    parser_version = "woori.v2"

    def __init__(self, *, base_url: str = BASE_URL, expected_minimum: int = 1) -> None:
        self.base_url = base_url.rstrip("/")
        self.expected_minimum = expected_minimum

    async def _post_json(self, client: httpx.AsyncClient, path: str, body: dict[str, Any]) -> dict[str, Any]:
        response = await client.post(
            self.base_url + path,
            json=body,
            headers={"Proworks-Body": "Y", "X-Requested-With": "XMLHttpRequest"},
        )
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise IssuerMarkupChanged("Woori API response is not an object")
        header = result.get("elHeader") or {}
        if header.get("resSuc") is False:
            raise IssuerMarkupChanged(f"Woori API error {header.get('resCode')}")
        return result

    async def discover(
        self,
        client: httpx.AsyncClient,
        *,
        mode: DiscoveryMode,
        categories: frozenset[str] | None = None,
    ) -> SourceSnapshot:
        started = datetime.now(UTC)
        if categories and categories != frozenset({"personal"}):
            raise ValueError("Woori v1 adapter supports only the personal category")
        landing = await client.get(self.base_url + NOTICE_PATH)
        landing.raise_for_status()
        main = await self._post_json(
            client,
            MAIN_PATH,
            {"cct11PrdntcAgrmReqVo": {"part": 0, "pageIndex": "1", "pageSize": "1000"}},
        )
        products = list((main.get("mainDataList") or {}).get("cct11PrdntcAgrmMainVo") or [])
        require_nonempty(products, label="Woori product listing", expected_minimum=self.expected_minimum)
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
                    current_only=mode == DiscoveryMode.CURRENT,
                    discovered_at=started,
                )
            )
        require_nonempty(records, label="Woori PDF disclosure", expected_minimum=self.expected_minimum)
        return canonical_snapshot(
            issuer=self.issuer,
            mode=mode,
            source_url=self.base_url + NOTICE_PATH,
            parser_version=self.parser_version,
            records=records,
            started_at=started,
        )

    def download_request(self, source: SourceRecord) -> tuple[str, dict[str, str]]:
        path = str(source.metadata.get("raonk_file_path") or "")
        plain = raonk_plaintext(path, source.file_name)
        return self.base_url + RAONK_PATH, {"k01": encrypt_raonk(plain)}
