"""Woori disclosure interpretation and RAONK request construction."""

from __future__ import annotations

import asyncio
import base64
import copy
import re
import uuid
import weakref
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.cookiejar import Cookie, CookieJar
from pathlib import PurePosixPath
from typing import Any

import httpx
from bs4 import BeautifulSoup
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
_SAFE_HEADER_VALUE = re.compile(r"^[A-Za-z0-9_.-]{1,128}\Z")

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
        if not file_path or not begin or not version or not file_name.casefold().endswith((".pdf", ".zip")):
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
                metadata={
                    "raonk_file_path": file_path,
                    **({"attachment_type": "zip"} if file_name.casefold().endswith(".zip") else {}),
                },
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
    if PurePosixPath(file_name).name != file_name or not file_name.casefold().endswith((".pdf", ".zip")):
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


def extract_raonk_auth_tokens(html: str) -> tuple[str, str]:
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception as exc:
        raise IssuerMarkupChanged("failed to parse Woori landing HTML") from exc

    key_inputs = soup.find_all("input", id=re.compile(r"^raonkAuthKey$", re.IGNORECASE))
    page_id_inputs = soup.find_all("input", id=re.compile(r"^raonkAuthPageId$", re.IGNORECASE))

    if not key_inputs:
        key_inputs = soup.find_all("input", attrs={"name": re.compile(r"^raonkAuthKey$", re.IGNORECASE)})
    if not page_id_inputs:
        page_id_inputs = soup.find_all(
            "input", attrs={"name": re.compile(r"^raonkAuthPageId$", re.IGNORECASE)}
        )

    keys = {str(inp.get("value") or "").strip() for inp in key_inputs if inp.get("value") is not None}
    page_ids = {str(inp.get("value") or "").strip() for inp in page_id_inputs if inp.get("value") is not None}

    if not keys or not page_ids:
        raise IssuerMarkupChanged("Woori landing page missing RAONK auth tokens")

    if len(keys) > 1 or len(page_ids) > 1:
        raise IssuerMarkupChanged("Woori landing page contains conflicting RAONK auth tokens")

    key = next(iter(keys))
    page_id = next(iter(page_ids))

    if not key or not page_id:
        raise IssuerMarkupChanged("Woori landing page contains empty RAONK auth tokens")

    if not _SAFE_HEADER_VALUE.fullmatch(key) or not _SAFE_HEADER_VALUE.fullmatch(page_id):
        raise IssuerMarkupChanged("Woori landing page contains invalid RAONK auth token format")

    return page_id, key


def _normalize_cookie_domain(domain: str) -> str:
    return domain.lstrip(".").lower()


def _normalize_cookie_path(path: str) -> str:
    return path or "/"


def _cookie_key(name: str, domain: str, path: str) -> tuple[str, str, str]:
    return (name, _normalize_cookie_domain(domain or ""), _normalize_cookie_path(path or "/"))


@dataclass(slots=True)
class _WooriSession:
    page_id: str
    auth_key: str
    cookies: dict[tuple[str, str, str], Cookie]
    landing_cookie_keys: set[tuple[str, str, str]] = field(default_factory=set)

    def is_expired(self) -> bool:
        if not self.cookies:
            return False
        return any(c.is_expired() for c in self.cookies.values())

    def update_cookie(self, cookie: Cookie) -> None:
        key = _cookie_key(cookie.name, cookie.domain or "", cookie.path or "/")
        self.cookies[key] = copy.copy(cookie)

    def remove_cookie(
        self,
        name: str | None = None,
        domain: str | None = None,
        path: str | None = None,
    ) -> None:
        if domain is None and path is None and name is None:
            # Full jar clearance (e.g. client.cookies.clear()) - do not delete session snapshot
            return
        norm_domain = _normalize_cookie_domain(domain) if domain is not None else None
        norm_path = _normalize_cookie_path(path) if path is not None else None
        to_del = []
        for c_name, c_domain, c_path in list(self.cookies.keys()):
            if name is not None and c_name != name:
                continue
            if norm_path is not None and c_path != norm_path:
                continue
            if norm_domain is not None and c_domain != norm_domain:
                continue
            to_del.append((c_name, c_domain, c_path))
        for k in to_del:
            self.cookies.pop(k, None)

    def is_valid(self) -> bool:
        if self.is_expired():
            return False
        return not self.landing_cookie_keys or self.landing_cookie_keys.issubset(self.cookies.keys())


class WooriAdapter:
    spec = SPEC
    parser_version = "woori.current.v1"
    detail_inventory = None

    def __init__(self, *, base_url: str = BASE_URL, minimum_records: int | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.minimum_records = minimum_records or self.spec.minimum_records
        self._sessions: weakref.WeakKeyDictionary[Any, _WooriSession] = weakref.WeakKeyDictionary()
        self._session_locks: weakref.WeakKeyDictionary[Any, asyncio.Lock] = weakref.WeakKeyDictionary()

    @staticmethod
    def _resolve_target(client: Any) -> Any:
        if client is None:
            raise ValueError("client is required for Woori download session authentication")
        target = getattr(client, "client", client)
        try:
            weakref.ref(target)
        except TypeError as exc:
            raise TypeError(
                f"client {type(target).__name__} does not support weak references for session isolation"
            ) from exc
        return target

    def _get_lock(self, target: Any) -> asyncio.Lock:
        lock = self._session_locks.get(target)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[target] = lock
        return lock

    @staticmethod
    def _get_cookie_jar(target: Any) -> CookieJar | None:
        cookies = getattr(target, "cookies", None)
        if cookies is None:
            return None
        jar = getattr(cookies, "jar", cookies)
        return jar if isinstance(jar, CookieJar) else None

    def _capture_cookies(self, target: Any) -> dict[tuple[str, str, str], Cookie]:
        jar = self._get_cookie_jar(target)
        if jar is None:
            return {}
        return {_cookie_key(c.name, c.domain or "", c.path or "/"): copy.copy(c) for c in jar}

    def _restore_session_cookies_to_client(self, target: Any, session: _WooriSession) -> None:
        jar = self._get_cookie_jar(target)
        if jar is None:
            return
        # If client jar already has cookies, synchronize them to capture any server rotations
        for c in jar:
            session.update_cookie(c)
        # Restore non-expired session cookies preserving full attributes
        for c in session.cookies.values():
            if not c.is_expired():
                jar.set_cookie(copy.copy(c))

    def _attach_cookie_tracker(self, target: Any) -> None:
        jar = self._get_cookie_jar(target)
        if jar is not None and not getattr(jar, "_woori_tracked", False):
            orig_clear = jar.clear
            target_ref = weakref.ref(target)
            self_ref = weakref.ref(self)

            def _tracked_clear(
                domain: str | None = None,
                path: str | None = None,
                name: str | None = None,
            ) -> None:
                orig_clear(domain=domain, path=path, name=name)
                if domain is not None:
                    t = target_ref()
                    s = self_ref()
                    if t is not None and s is not None:
                        session = s._sessions.get(t)
                        if session is not None:
                            session.remove_cookie(name=name, domain=domain, path=path)

            jar.clear = _tracked_clear  # type: ignore[method-assign]
            jar._woori_tracked = True  # type: ignore[attr-defined]

        if not hasattr(target, "event_hooks") or not isinstance(target.event_hooks, dict):
            return
        response_hooks = target.event_hooks.setdefault("response", [])
        target_ref = weakref.ref(target)
        self_ref = weakref.ref(self)

        async def _track_response(response: httpx.Response) -> None:
            del response
            t = target_ref()
            s = self_ref()
            if t is not None and s is not None:
                s.sync_session(t)

        if not any(getattr(h, "_is_woori_cookie_tracker", False) for h in response_hooks):
            _track_response._is_woori_cookie_tracker = True  # type: ignore[attr-defined]
            response_hooks.append(_track_response)

    def sync_session(self, client: Any) -> None:
        try:
            target = self._resolve_target(client)
        except (ValueError, TypeError):
            return
        session = self._sessions.get(target)
        if session is not None:
            jar = self._get_cookie_jar(target)
            if jar is not None:
                for c in jar:
                    session.update_cookie(c)

    def invalidate_session(self, client: Any) -> None:
        target = self._resolve_target(client)
        self._sessions.pop(target, None)

    async def _ensure_session(self, client: Any) -> _WooriSession:
        target = self._resolve_target(client)
        self._attach_cookie_tracker(target)
        lock = self._get_lock(target)
        async with lock:
            cached = self._sessions.get(target)
            if cached is not None:
                if cached.is_valid():
                    self._restore_session_cookies_to_client(target, cached)
                    return cached
                self._sessions.pop(target, None)

            response = await client.get(self.base_url + NOTICE_PATH)
            response.raise_for_status()
            text = (
                response.text
                if hasattr(response, "text")
                else response.content.decode("utf-8", errors="replace")
            )
            page_id, auth_key = extract_raonk_auth_tokens(text)
            cookies = self._capture_cookies(target)
            session = _WooriSession(
                page_id=page_id,
                auth_key=auth_key,
                cookies=cookies,
                landing_cookie_keys=set(cookies.keys()),
            )
            self._sessions[target] = session
            return session

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
        await self._ensure_session(client)
        main = await self._post_json(
            client,
            MAIN_PATH,
            {"cct11PrdntcAgrmReqVo": {"part": 0, "pageIndex": "1", "pageSize": "1000"}},
        )
        products = list((main.get("mainDataList") or {}).get("cct11PrdntcAgrmMainVo") or [])
        # The official endpoint currently requests at most 1,000 rows. Refuse
        # a saturated response rather than declaring a truncated inventory complete.
        if len(products) >= 1000:
            raise IssuerMarkupChanged("Woori main listing requires pagination")
        require_minimum(products, label="Woori product listing", minimum=self.minimum_records)
        records: list[SourceRecord] = []
        for product in products:
            if product.get("goodsDescAt") != "Y":
                continue
            code = str(product.get("code") or "").strip()
            name = str(product.get("codeName") or "").strip()
            if not code or not name:
                continue
            from ..contracts import canonical_sha256

            fingerprint = canonical_sha256(product)
            if self.detail_inventory is not None:
                retained = self.detail_inventory.lookup(self.spec.code, code, fingerprint, now=started)
                if retained is not None:
                    records.extend(retained)
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
            parsed = parse_detail_records(detail, product_code=code, product_name=name, discovered_at=started)
            records.extend(parsed)
            if self.detail_inventory is not None:
                self.detail_inventory.put(self.spec.code, code, fingerprint, parsed, now=started)
        require_minimum(records, label="Woori current PDF disclosure", minimum=self.minimum_records)
        return snapshot_from_records(
            issuer=self.spec.code,
            source_url=self.base_url + NOTICE_PATH,
            parser_version=self.parser_version,
            records=records,
            started_at=started,
        )

    async def prepare_download(
        self, client: httpx.AsyncClient | Any, source: SourceRecord
    ) -> DownloadRequest:
        if source.issuer != self.spec.code:
            raise ValueError("source issuer does not match adapter")
        if client is None:
            raise ValueError("client is required for Woori download session authentication")
        path = str(source.metadata.get("raonk_file_path") or "")
        session = await self._ensure_session(client)
        return DownloadRequest(
            url=self.base_url + RAONK_PATH,
            method="POST",
            form={"k01": encrypt_raonk(raonk_plaintext(path, source.file_name))},
            headers={
                "Referer": self.base_url + NOTICE_PATH,
                "RAONK_AUTH_PAGE_ID": session.page_id,
                "RAONK_AUTH_KEY": session.auth_key,
            },
        )
