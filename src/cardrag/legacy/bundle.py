"""Build and verify portable, deterministic legacy PDF/OCR bundles.

The legacy tree is an input archive, never a staging directory.  A bundle is
assembled in a sibling temporary directory, verified, made read-only, and
renamed into place only after its ``READY`` marker has been written.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cardrag.domain import DocumentIdentity, Issuer, canonical_json_bytes, canonical_sha256
from cardrag.pdf import PDFEngineError, open_pdf

BUNDLE_SCHEMA_VERSION: Literal["cardrag.legacy-bundle.v1"] = "cardrag.legacy-bundle.v1"
ADOPTION_POLICY_VERSION: Literal["cardrag.legacy-ocr-adoption.v1"] = (
    "cardrag.legacy-ocr-adoption.v1"
)
_ISSUER_MAP = {"wooricard": Issuer.WOORI, "kbcard": Issuer.KB, "shinhancard": Issuer.SHINHAN}
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")
_PRODUCT_CODE = re.compile(r"^[A-Za-z0-9_-]+$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PAGE_MARKER = re.compile(r"^## Page (\d+)\s*$", re.MULTILINE)


class BundleIntegrityError(RuntimeError):
    """The bundle or legacy source does not satisfy the fail-closed contract."""


class BundleObject(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["pdf", "ocr", "metadata"]
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    path: str


class LegacyBundleDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    document_key: str
    document_id: str
    issuer: Issuer
    product_code: str
    product_name: str
    document_type: str
    effective_date: str
    source_version: str
    version_sort_key: tuple[tuple[int, int | str], ...]
    source_url: str
    source_post_id: str
    file_name: str
    discovered_at: datetime
    is_latest: bool
    pdf_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    pdf_size_bytes: int = Field(gt=0)
    pdf_page_count: int = Field(gt=0)
    pdf_object_path: str
    ocr_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    ocr_size_bytes: int | None = Field(default=None, ge=0)
    ocr_object_path: str | None = None
    metadata_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    metadata_object_path: str | None = None
    metadata_schema: str
    adoption_status: Literal["adopted", "reocr"]
    adoption_reason: str
    mapping_method: Literal["direct_path_and_hash", "hash_lookup"]
    source_pdf_path: str | None
    source_ocr_path: str | None
    source_metadata_path: str | None
    warnings: tuple[str, ...] = ()

    @model_validator(mode="after")
    def adopted_ocr_is_complete(self) -> Self:
        if self.adoption_status == "adopted" and (
            self.ocr_sha256 is None or self.ocr_object_path is None or self.ocr_size_bytes is None
        ):
            raise ValueError("adopted OCR requires a complete object reference")
        return self


class LegacyBundleManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["cardrag.legacy-bundle.v1"] = BUNDLE_SCHEMA_VERSION
    bundle_id: str = Field(pattern=r"^bundle-[0-9a-f]{12}$")
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    document_count: int = Field(ge=0)
    adopted_count: int = Field(ge=0)
    reocr_count: int = Field(ge=0)
    unique_pdf_objects: int = Field(ge=0)
    unique_ocr_objects: int = Field(ge=0)
    unique_metadata_objects: int = Field(ge=0)
    payload_bytes: int = Field(ge=0)
    documents_manifest: str = "manifests/documents.jsonl"
    source_files_manifest: str = "manifests/source-files.jsonl"
    exceptions_manifest: str = "manifests/exceptions.jsonl"
    adoption_policy: Literal["cardrag.legacy-ocr-adoption.v1"] = ADOPTION_POLICY_VERSION

    @model_validator(mode="after")
    def counts_balance(self) -> Self:
        if self.adopted_count + self.reocr_count != self.document_count:
            raise ValueError("bundle document disposition counts do not balance")
        if self.bundle_id != f"bundle-{self.content_sha256[:12]}":
            raise ValueError("bundle ID does not match its content digest")
        return self


class LegacyPrepareResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    manifest: LegacyBundleManifest
    bundle_path: str | None
    dry_run: bool
    source_writes: int = 0


def _sha256_file(path: Path, *, progress: Callable[[int], None] | None = None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
            if progress is not None:
                progress(len(chunk))
    return digest.hexdigest()


def _declared_sha256(value: object) -> str:
    raw = str(value or "").strip().casefold()
    if raw.startswith("sha256:"):
        raw = raw.removeprefix("sha256:")
    return raw if _SHA256.fullmatch(raw) else ""


def _json_line(value: object) -> bytes:
    return canonical_json_bytes(value) + b"\n"


def _safe_version_segment(value: str) -> str:
    if _SAFE_SEGMENT.fullmatch(value) and value not in {".", ".."}:
        return value
    return f"id-{hashlib.sha256(value.encode()).hexdigest()[:16]}"


def _parse_datetime(value: object) -> datetime:
    raw = str(value or "").strip()
    if raw:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                return parsed.astimezone(UTC)
        except ValueError:
            pass
    return datetime(1970, 1, 1, tzinfo=UTC)


def _source_version(raw: dict[str, Any], document_key: str) -> str:
    declared = str(raw.get("gdccVer") or raw.get("source_version") or "").strip()
    if declared:
        return declared.removeprefix("v")
    key_parts = document_key.rsplit(":", 1)
    if len(key_parts) == 2 and key_parts[1]:
        return key_parts[1].removeprefix("v")
    return "legacy"


def _effective_date(raw: dict[str, Any], document_key: str) -> str:
    value = str(raw.get("beginDt") or raw.get("effective_date") or "").replace(".", "-")
    try:
        return datetime.strptime(value, "%Y-%m-%d").date().isoformat()
    except ValueError:
        parts = document_key.split(":")
        if len(parts) >= 5:
            return datetime.strptime(parts[-2], "%Y-%m-%d").date().isoformat()
        raise BundleIntegrityError(f"invalid effective date for {document_key}") from None


class LegacyBundlePreparer:
    """Normalize a master manifest without ever mutating its source tree."""

    def __init__(self, source_root: Path) -> None:
        if source_root.is_symlink():
            raise BundleIntegrityError("legacy source root must not be a symlink")
        self.source_root = source_root.resolve(strict=True)
        if not self.source_root.is_dir():
            raise BundleIntegrityError("legacy source must be a regular directory")

    def _source_file(self, relative_path: str) -> Path:
        if not relative_path or Path(relative_path).is_absolute():
            raise BundleIntegrityError("legacy paths must be non-empty and relative")
        candidate = self.source_root / relative_path
        if candidate.is_symlink():
            raise BundleIntegrityError("legacy source symlinks are forbidden")
        try:
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError as exc:
            raise BundleIntegrityError(f"legacy source file is missing: {relative_path}") from exc
        if not resolved.is_relative_to(self.source_root) or not resolved.is_file():
            raise BundleIntegrityError("legacy path escaped the source root")
        if not stat.S_ISREG(resolved.stat().st_mode):
            raise BundleIntegrityError("legacy source object is not a regular file")
        return resolved

    def _pdf_inventory(self) -> dict[str, list[tuple[str, Path, int]]]:
        by_hash: dict[str, list[tuple[str, Path, int]]] = defaultdict(list)
        for path in sorted(self.source_root.rglob("*")):
            if path.is_symlink():
                raise BundleIntegrityError("legacy source contains a symlink escape or alias")
            if not path.is_file() or path.suffix.casefold() != ".pdf":
                continue
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(self.source_root):
                raise BundleIntegrityError("legacy PDF escaped the source root")
            digest = _sha256_file(resolved)
            by_hash[digest].append(
                (resolved.relative_to(self.source_root).as_posix(), resolved, resolved.stat().st_size)
            )
        return by_hash

    def prepare(
        self,
        manifest_path: Path,
        output_root: Path,
        *,
        dry_run: bool = False,
    ) -> LegacyPrepareResult:
        manifest_path = manifest_path.resolve(strict=True)
        if not manifest_path.is_relative_to(self.source_root):
            raise BundleIntegrityError("master manifest must be inside the source root")
        requested_output = output_root.absolute()
        if requested_output.is_symlink():
            raise BundleIntegrityError("bundle output must not be a symlink")
        try:
            output = requested_output.resolve(strict=True)
        except FileNotFoundError:
            parent = requested_output.parent.resolve(strict=True)
            output = parent / requested_output.name
        if output == self.source_root or output.is_relative_to(self.source_root):
            raise BundleIntegrityError("bundle output must be outside the read-only source root")
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise BundleIntegrityError("master manifest is not valid UTF-8 JSON") from exc
        if not isinstance(payload, dict):
            raise BundleIntegrityError("master manifest must be a JSON object")
        raw_entries = payload.get("entries") or payload.get("items") or payload.get("documents")
        if not isinstance(raw_entries, list):
            raise BundleIntegrityError("master manifest has no document list")
        entries: list[dict[str, Any]] = []
        for item in raw_entries:
            if not isinstance(item, dict):
                raise BundleIntegrityError("master manifest document is not an object")
            entries.append(item)

        pdf_inventory = self._pdf_inventory()
        documents, source_files, exceptions, object_sources = self._normalize_entries(
            entries, pdf_inventory
        )
        documents = self._mark_latest(documents)
        documents_body = b"".join(_json_line(item.model_dump(mode="json")) for item in documents)
        source_files_body = b"".join(_json_line(item) for item in source_files)
        exceptions_body = b"".join(_json_line(item) for item in exceptions)
        source_manifest_sha256 = _sha256_file(manifest_path)
        content_spec: dict[str, object] = {
            "adoption_policy": ADOPTION_POLICY_VERSION,
            "documents_sha256": hashlib.sha256(documents_body).hexdigest(),
            "exceptions_sha256": hashlib.sha256(exceptions_body).hexdigest(),
            "objects": [
                {"kind": kind, "sha256": digest, "size_bytes": size}
                for (kind, digest), (_, size) in sorted(object_sources.items())
            ],
            "schema_version": BUNDLE_SCHEMA_VERSION,
            "source_files_sha256": hashlib.sha256(source_files_body).hexdigest(),
            "source_manifest_sha256": source_manifest_sha256,
        }
        content_sha256 = canonical_sha256(content_spec)
        object_counts = {
            kind: len({digest for object_kind, digest in object_sources if object_kind == kind})
            for kind in ("pdf", "ocr", "metadata")
        }
        bundle_manifest = LegacyBundleManifest(
            bundle_id=f"bundle-{content_sha256[:12]}",
            content_sha256=content_sha256,
            source_manifest_sha256=source_manifest_sha256,
            document_count=len(documents),
            adopted_count=sum(item.adoption_status == "adopted" for item in documents),
            reocr_count=sum(item.adoption_status == "reocr" for item in documents),
            unique_pdf_objects=object_counts["pdf"],
            unique_ocr_objects=object_counts["ocr"],
            unique_metadata_objects=object_counts["metadata"],
            payload_bytes=sum(size for _, size in object_sources.values()),
        )
        if dry_run:
            return LegacyPrepareResult(manifest=bundle_manifest, bundle_path=None, dry_run=True)

        output.mkdir(mode=0o750, parents=True, exist_ok=True)
        final = output / bundle_manifest.bundle_id
        if final.exists():
            existing = verify_bundle(final)
            if existing.content_sha256 != bundle_manifest.content_sha256:
                raise BundleIntegrityError("existing bundle ID has different content")
            return LegacyPrepareResult(
                manifest=existing, bundle_path=str(final), dry_run=False
            )
        temporary = Path(tempfile.mkdtemp(prefix=f".{bundle_manifest.bundle_id}.", dir=output))
        try:
            self._write_bundle(
                temporary,
                bundle_manifest,
                documents,
                documents_body,
                source_files_body,
                exceptions_body,
                object_sources,
            )
            os.replace(temporary, final)
            _fsync_directory(output)
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        verified = verify_bundle(final)
        return LegacyPrepareResult(manifest=verified, bundle_path=str(final), dry_run=False)

    def _normalize_entries(
        self,
        entries: list[dict[str, Any]],
        pdf_inventory: dict[str, list[tuple[str, Path, int]]],
    ) -> tuple[
        list[LegacyBundleDocument],
        list[dict[str, object]],
        list[dict[str, object]],
        dict[tuple[str, str], tuple[Path, int]],
    ]:
        documents: list[LegacyBundleDocument] = []
        source_facts: dict[str, dict[str, object]] = {}
        exceptions: list[dict[str, object]] = []
        objects: dict[tuple[str, str], tuple[Path, int]] = {}
        pdf_page_counts: dict[str, int] = {}
        seen_keys: set[str] = set()
        for raw in sorted(entries, key=lambda value: str(value.get("doc_version_id") or "")):
            key = str(raw.get("doc_version_id") or raw.get("document_id") or "").strip()
            if not key or key in seen_keys:
                raise BundleIntegrityError("legacy document keys must be non-empty and unique")
            seen_keys.add(key)
            issuer_raw = str(raw.get("cardCompany") or raw.get("card_company") or "").casefold()
            try:
                issuer = _ISSUER_MAP[issuer_raw]
            except KeyError as exc:
                raise BundleIntegrityError(f"unsupported legacy issuer for {key}") from exc
            product_code = str(raw.get("productCode") or raw.get("product_code") or "").strip()
            if not _PRODUCT_CODE.fullmatch(product_code):
                raise BundleIntegrityError(f"unsafe product code for {key}")
            document_type = str(raw.get("docType") or raw.get("document_type") or "product_description")
            effective_date = _effective_date(raw, key)
            source_version = _source_version(raw, key)
            identity_without_hash = DocumentIdentity(
                issuer=issuer,
                product_code=product_code,
                document_type=document_type,
                effective_date=effective_date,
                version=source_version,
            )
            expected_pdf = _declared_sha256(raw.get("pdf_sha256") or raw.get("pdf_fingerprint"))
            if not expected_pdf:
                raise BundleIntegrityError(f"missing trusted PDF hash for {key}")

            metadata_rel = str(raw.get("metadata_remote_rel") or raw.get("metadata_rel_path") or "")
            metadata: dict[str, Any] = {}
            metadata_path: Path | None = None
            metadata_digest: str | None = None
            if metadata_rel:
                metadata_path = self._source_file(metadata_rel)
                try:
                    loaded = json.loads(metadata_path.read_text(encoding="utf-8"))
                except (UnicodeError, json.JSONDecodeError) as exc:
                    raise BundleIntegrityError(f"invalid metadata JSON for {key}") from exc
                if not isinstance(loaded, dict):
                    raise BundleIntegrityError(f"metadata is not an object for {key}")
                metadata = loaded
                metadata_digest = _sha256_file(metadata_path)
                objects[("metadata", metadata_digest)] = (metadata_path, metadata_path.stat().st_size)
                self._remember_source(source_facts, metadata_path, metadata_digest)

            direct_rel = str(raw.get("raw_pdf_rel_path") or metadata.get("raw_pdf_rel_path") or "")
            pdf_path: Path | None = None
            mapping_method: Literal["direct_path_and_hash", "hash_lookup"] = "hash_lookup"
            if direct_rel:
                try:
                    candidate = self._source_file(direct_rel)
                    if _sha256_file(candidate) == expected_pdf:
                        pdf_path = candidate
                        mapping_method = "direct_path_and_hash"
                except BundleIntegrityError:
                    pdf_path = None
            if pdf_path is None:
                matches = sorted(pdf_inventory.get(expected_pdf, []), key=lambda value: value[0])
                if not matches:
                    raise BundleIntegrityError(f"PDF hash cannot be resolved for {key}")
                pdf_path = matches[0][1]
            actual_pdf = _sha256_file(pdf_path)
            if actual_pdf != expected_pdf:
                raise BundleIntegrityError(f"resolved PDF hash changed for {key}")
            pdf_size = pdf_path.stat().st_size
            objects[("pdf", actual_pdf)] = (pdf_path, pdf_size)
            self._remember_source(source_facts, pdf_path, actual_pdf)

            if actual_pdf not in pdf_page_counts:
                try:
                    with open_pdf(pdf_path) as document:
                        document.validate_all_pages()
                        actual_page_count = document.page_count
                except PDFEngineError as exc:
                    raise BundleIntegrityError(f"PDF cannot be validated for {key}") from exc
                if actual_page_count < 1:
                    raise BundleIntegrityError(f"PDF has no pages for {key}")
                pdf_page_counts[actual_pdf] = actual_page_count
            actual_page_count = pdf_page_counts[actual_pdf]
            declared_page_count = metadata.get(
                "page_count", metadata.get("pages", raw.get("pages"))
            )
            page_count_issue: str | None = None
            if not isinstance(declared_page_count, int) or declared_page_count < 1:
                page_count_issue = "pdf_page_count_missing"
            elif declared_page_count != actual_page_count:
                page_count_issue = "pdf_page_count_mismatch"

            ocr_rel = str(
                raw.get("ocr_rel_path")
                or raw.get("ocr_remote_rel")
                or metadata.get("ocr_md_rel_path")
                or ""
            )
            ocr_path: Path | None = None
            ocr_digest: str | None = None
            ocr_size: int | None = None
            status: Literal["adopted", "reocr"] = "reocr"
            reason = "ocr_missing"
            warnings: list[str] = []
            if ocr_rel:
                try:
                    ocr_path = self._source_file(ocr_rel)
                    ocr_text = ocr_path.read_text(encoding="utf-8")
                    ocr_digest = _sha256_file(ocr_path)
                    ocr_size = ocr_path.stat().st_size
                    objects[("ocr", ocr_digest)] = (ocr_path, ocr_size)
                    self._remember_source(source_facts, ocr_path, ocr_digest)
                    expected_ocr = _declared_sha256(
                        metadata.get("ocr_md_sha256") or raw.get("ocr_md_sha256")
                    )
                    metadata_chars = metadata.get("ocr_md_chars")
                    recorded_chars = raw.get("ocr_chars")
                    markers = [int(value) for value in _PAGE_MARKER.findall(ocr_text)]
                    if not expected_ocr:
                        reason = "ocr_hash_missing"
                    elif expected_ocr != ocr_digest:
                        reason = "ocr_hash_mismatch"
                    elif isinstance(metadata_chars, int) and metadata_chars != len(ocr_text):
                        reason = "metadata_ocr_chars_mismatch"
                    elif page_count_issue is not None:
                        reason = page_count_issue
                    elif markers != list(range(1, actual_page_count + 1)):
                        reason = "ocr_page_coverage_mismatch"
                    else:
                        status = "adopted"
                        reason = "validated"
                    if isinstance(recorded_chars, int) and recorded_chars != len(ocr_text):
                        warnings.append("master_ocr_chars_drift")
                    if page_count_issue is not None:
                        warnings.append(page_count_issue)
                except (BundleIntegrityError, UnicodeError):
                    reason = "ocr_unreadable"
            if status == "reocr":
                exceptions.append(
                    {
                        "code": reason,
                        "document_key": key,
                        "disposition": "reocr",
                    }
                )

            document_identity = identity_without_hash.model_copy(update={"source_sha256": actual_pdf})
            pdf_object_path = f"objects/pdf/sha256/{actual_pdf[:2]}/{actual_pdf}.pdf"
            ocr_object_path = (
                f"objects/ocr/sha256/{ocr_digest[:2]}/{ocr_digest}.md" if ocr_digest else None
            )
            metadata_object_path = (
                f"objects/metadata/sha256/{metadata_digest[:2]}/{metadata_digest}.json"
                if metadata_digest
                else None
            )
            documents.append(
                LegacyBundleDocument(
                    document_key=key,
                    document_id=document_identity.stable_id,
                    issuer=issuer,
                    product_code=product_code,
                    product_name=str(raw.get("productName") or raw.get("product_name") or product_code),
                    document_type=document_type,
                    effective_date=effective_date,
                    source_version=source_version,
                    version_sort_key=document_identity.version_sort_key,
                    source_url=str(raw.get("sourceUrl") or raw.get("source_url") or "https://legacy.invalid/"),
                    source_post_id=str(raw.get("sourcePostId") or raw.get("source_post_id") or key),
                    file_name=str(raw.get("fileNm") or raw.get("file_name") or f"{product_code}.pdf"),
                    discovered_at=_parse_datetime(raw.get("completed_at") or raw.get("discovered_at")),
                    is_latest=False,
                    pdf_sha256=actual_pdf,
                    pdf_size_bytes=pdf_size,
                    pdf_page_count=actual_page_count,
                    pdf_object_path=pdf_object_path,
                    ocr_sha256=ocr_digest,
                    ocr_size_bytes=ocr_size,
                    ocr_object_path=ocr_object_path,
                    metadata_sha256=metadata_digest,
                    metadata_object_path=metadata_object_path,
                    metadata_schema=str(metadata.get("schema_version") or "unknown"),
                    adoption_status=status,
                    adoption_reason=reason,
                    mapping_method=mapping_method,
                    source_pdf_path=(
                        pdf_path.relative_to(self.source_root).as_posix()
                        if mapping_method == "direct_path_and_hash"
                        else None
                    ),
                    source_ocr_path=(
                        ocr_path.relative_to(self.source_root).as_posix() if ocr_path else None
                    ),
                    source_metadata_path=metadata_rel or None,
                    warnings=tuple(sorted(warnings)),
                )
            )
        return (
            documents,
            [source_facts[path] for path in sorted(source_facts)],
            sorted(exceptions, key=lambda value: str(value["document_key"])),
            objects,
        )

    def _remember_source(
        self, facts: dict[str, dict[str, object]], path: Path, digest: str
    ) -> None:
        relative = path.relative_to(self.source_root).as_posix()
        facts[relative] = {
            "relative_path": relative,
            "sha256": digest,
            "size_bytes": path.stat().st_size,
        }

    @staticmethod
    def _mark_latest(documents: list[LegacyBundleDocument]) -> list[LegacyBundleDocument]:
        grouped: dict[tuple[Issuer, str, str], list[LegacyBundleDocument]] = defaultdict(list)
        for document in documents:
            grouped[(document.issuer, document.product_code, document.document_type)].append(document)
        latest_ids = {
            max(items, key=lambda item: (item.effective_date, item.version_sort_key, item.document_id)).document_id
            for items in grouped.values()
        }
        return [
            item.model_copy(update={"is_latest": item.document_id in latest_ids})
            for item in sorted(documents, key=lambda value: value.document_key)
        ]

    def _write_bundle(
        self,
        root: Path,
        manifest: LegacyBundleManifest,
        documents: list[LegacyBundleDocument],
        documents_body: bytes,
        source_files_body: bytes,
        exceptions_body: bytes,
        object_sources: dict[tuple[str, str], tuple[Path, int]],
    ) -> None:
        (root / "manifests").mkdir(mode=0o750)
        (root / "records").mkdir(mode=0o750)
        (root / "reports").mkdir(mode=0o750)
        _write_file(root / "manifests/documents.jsonl", documents_body)
        _write_file(root / "manifests/source-files.jsonl", source_files_body)
        _write_file(root / "manifests/exceptions.jsonl", exceptions_body)
        for (kind, digest), (source, _) in sorted(object_sources.items()):
            suffix = {"pdf": ".pdf", "ocr": ".md", "metadata": ".json"}[kind]
            target = root / "objects" / kind / "sha256" / digest[:2] / f"{digest}{suffix}"
            target.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            if _sha256_file(target) != digest:
                raise BundleIntegrityError("bundle object hash changed during copy")
            with target.open("rb") as stream:
                os.fsync(stream.fileno())
            target.chmod(0o440)
        record_paths: set[Path] = set()
        for document in documents:
            version = _safe_version_segment(document.source_version)
            relative = (
                Path("records")
                / document.issuer.value
                / document.product_code
                / document.document_type
                / document.effective_date
                / version
                / "record.json"
            )
            if relative in record_paths:
                relative = (
                    Path("records")
                    / document.issuer.value
                    / document.product_code
                    / document.document_type
                    / document.effective_date
                    / f"{version}--{document.document_id[-12:]}"
                    / "record.json"
                )
            if relative in record_paths:
                raise BundleIntegrityError("normalized record path collision")
            record_paths.add(relative)
            target = root / relative
            target.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
            _write_file(target, _json_line(document.model_dump(mode="json")))
        _write_file(root / "bundle-manifest.json", _json_line(manifest.model_dump(mode="json")))
        report = {
            "adopted": manifest.adopted_count,
            "documents": manifest.document_count,
            "payload_bytes": manifest.payload_bytes,
            "reocr": manifest.reocr_count,
            "schema_version": "cardrag.legacy-prepare-report.v1",
            "source_writes": 0,
            "unique_metadata_objects": manifest.unique_metadata_objects,
            "unique_ocr_objects": manifest.unique_ocr_objects,
            "unique_pdf_objects": manifest.unique_pdf_objects,
        }
        _write_file(root / "reports/prepare-report.json", _json_line(report))
        checksum_rows: list[str] = []
        for path in sorted(root.rglob("*")):
            if path.is_file() and path.name not in {"checksums.sha256", "READY"}:
                checksum_rows.append(f"{_sha256_file(path)}  {path.relative_to(root).as_posix()}\n")
        _write_file(root / "checksums.sha256", "".join(checksum_rows).encode())
        # Flush every child directory before the readiness commit. A crash can
        # leave an incomplete temporary tree, but never a final bundle whose
        # READY marker precedes its payload and checksum entries.
        for directory in sorted((path for path in root.rglob("*") if path.is_dir()), reverse=True):
            _fsync_directory(directory)
            directory.chmod(0o550)
        _write_file(
            root / "READY",
            _json_line(
                {
                    "bundle_id": manifest.bundle_id,
                    "bundle_manifest_sha256": _sha256_file(root / "bundle-manifest.json"),
                    "checksums_sha256": _sha256_file(root / "checksums.sha256"),
                    "content_sha256": manifest.content_sha256,
                    "schema_version": "cardrag.legacy-bundle-ready.v1",
                }
            ),
        )
        _fsync_directory(root)
        root.chmod(0o550)


def _write_file(path: Path, payload: bytes) -> None:
    with path.open("wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    path.chmod(0o440)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def inspect_bundle_control(bundle_root: Path) -> LegacyBundleManifest:
    """Read the small READY-bound identity before the full payload scan.

    This is intentionally not a substitute for :func:`verify_bundle`.  It lets
    an importer persist and print durable operation IDs before hashing a large
    bundle, while still binding those IDs to an immutable manifest identity.
    """
    if bundle_root.is_symlink():
        raise BundleIntegrityError("bundle root must not be a symlink")
    root = bundle_root.resolve(strict=True)
    if not root.is_dir():
        raise BundleIntegrityError("bundle root must be a regular directory")
    ready_path = root / "READY"
    manifest_path = root / "bundle-manifest.json"
    checksums_path = root / "checksums.sha256"
    for control in (ready_path, manifest_path, checksums_path):
        if control.is_symlink():
            raise BundleIntegrityError("bundle control files must not be symlinks")
    if not ready_path.is_file() or not manifest_path.is_file() or not checksums_path.is_file():
        raise BundleIntegrityError("bundle is not sealed with READY, manifest and checksums")
    try:
        manifest = LegacyBundleManifest.model_validate_json(manifest_path.read_bytes())
        ready = json.loads(ready_path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise BundleIntegrityError("bundle control file is invalid") from exc
    if ready.get("bundle_id") != manifest.bundle_id or ready.get("content_sha256") != manifest.content_sha256:
        raise BundleIntegrityError("READY marker does not match bundle manifest")
    if ready.get("bundle_manifest_sha256") != _sha256_file(manifest_path):
        raise BundleIntegrityError("READY marker does not bind the bundle manifest")
    if ready.get("checksums_sha256") != _sha256_file(checksums_path):
        raise BundleIntegrityError("READY marker does not bind the checksum manifest")
    return manifest


def verify_bundle(
    bundle_root: Path,
    *,
    progress: Callable[[int, int, int, int], None] | None = None,
) -> LegacyBundleManifest:
    """Verify a sealed bundle completely before exposing any object to import."""

    manifest = inspect_bundle_control(bundle_root)
    root = bundle_root.resolve(strict=True)
    _audit_bundle_tree(root)
    allowed_top = {
        "READY",
        "bundle-manifest.json",
        "checksums.sha256",
        "manifests",
        "objects",
        "records",
        "reports",
    }
    if {path.name for path in root.iterdir()} != allowed_top:
        raise BundleIntegrityError("bundle has an unexpected or missing top-level entry")
    checksums_path = root / "checksums.sha256"
    expected_paths: set[str] = set()
    checksum_lines = checksums_path.read_text(encoding="utf-8").splitlines()
    resolved_rows: list[tuple[str, str, Path]] = []
    for line in checksum_lines:
        parts = line.split("  ", 1)
        if len(parts) != 2 or not _SHA256.fullmatch(parts[0]):
            raise BundleIntegrityError("invalid checksums manifest row")
        relative = parts[1]
        candidate = root / relative
        if candidate.is_symlink():
            raise BundleIntegrityError("bundle symlinks are forbidden")
        try:
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError as exc:
            raise BundleIntegrityError(f"bundle file is missing: {relative}") from exc
        if not resolved.is_relative_to(root) or not resolved.is_file():
            raise BundleIntegrityError("bundle checksum path escaped its root")
        if relative in expected_paths:
            raise BundleIntegrityError(f"bundle checksum mismatch: {relative}")
        expected_paths.add(relative)
        resolved_rows.append((relative, parts[0], resolved))
    total_bytes = sum(path.stat().st_size for _, _, path in resolved_rows)
    checked_bytes = 0
    for index, (relative, expected, resolved) in enumerate(resolved_rows, 1):
        completed_before = index - 1

        def advance(size: int, *, completed_files: int = completed_before) -> None:
            nonlocal checked_bytes
            checked_bytes += size
            if progress is not None:
                progress(completed_files, len(resolved_rows), checked_bytes, total_bytes)

        if _sha256_file(resolved, progress=advance) != expected:
            raise BundleIntegrityError(f"bundle checksum mismatch: {relative}")
        if progress is not None:
            progress(index, len(resolved_rows), checked_bytes, total_bytes)
    actual_paths = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path.name not in {"checksums.sha256", "READY"}
    }
    if actual_paths != expected_paths:
        raise BundleIntegrityError("bundle contains unchecked or unlisted files")
    expected_directories = {"manifests", "objects", "records", "reports"}
    for relative in expected_paths:
        parent = Path(relative).parent
        while parent != Path("."):
            expected_directories.add(parent.as_posix())
            parent = parent.parent
    actual_directories = {
        path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_dir()
    }
    if actual_directories != expected_directories:
        raise BundleIntegrityError("bundle contains an unlisted or missing directory")
    documents_path = root / manifest.documents_manifest
    documents = load_bundle_documents(root, manifest=manifest)
    if len(documents) != manifest.document_count:
        raise BundleIntegrityError("bundle document count differs from manifest")
    source_rows = [
        json.loads(line)
        for line in (root / manifest.source_files_manifest).read_text(encoding="utf-8").splitlines()
        if line
    ]
    source_paths = [str(item.get("relative_path")) for item in source_rows]
    if source_paths != sorted(source_paths) or len(source_paths) != len(set(source_paths)):
        raise BundleIntegrityError("source file manifest paths are not sorted and unique")
    exception_rows = [
        json.loads(line)
        for line in (root / manifest.exceptions_manifest).read_text(encoding="utf-8").splitlines()
        if line
    ]
    reocr_ids = {item.document_key for item in documents if item.adoption_status == "reocr"}
    if {str(item.get("document_key")) for item in exception_rows} != reocr_ids:
        raise BundleIntegrityError("exception manifest does not account for every re-OCR document")
    content_spec: dict[str, object] = {
        "adoption_policy": manifest.adoption_policy,
        "documents_sha256": _sha256_file(documents_path),
        "exceptions_sha256": _sha256_file(root / manifest.exceptions_manifest),
        "objects": [],
        "schema_version": manifest.schema_version,
        "source_files_sha256": _sha256_file(root / manifest.source_files_manifest),
        "source_manifest_sha256": manifest.source_manifest_sha256,
    }
    object_rows: list[dict[str, object]] = []
    for kind, suffix in (("metadata", ".json"), ("ocr", ".md"), ("pdf", ".pdf")):
        for path in sorted((root / "objects" / kind / "sha256").glob(f"*/*{suffix}")):
            digest = path.stem
            if not _SHA256.fullmatch(digest) or path.parent.name != digest[:2]:
                raise BundleIntegrityError("bundle object path does not match its content address")
            object_rows.append({"kind": kind, "sha256": digest, "size_bytes": path.stat().st_size})
    content_spec["objects"] = sorted(
        object_rows, key=lambda value: (str(value["kind"]), str(value["sha256"]))
    )
    if canonical_sha256(content_spec) != manifest.content_sha256:
        raise BundleIntegrityError("bundle content identity is not reproducible")
    return manifest


def _audit_bundle_tree(root: Path) -> None:
    """Reject links and special nodes, including otherwise-unlisted directories."""

    for current, directory_names, file_names in os.walk(root, followlinks=False):
        parent = Path(current)
        for name in [*directory_names, *file_names]:
            candidate = parent / name
            metadata = candidate.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise BundleIntegrityError("bundle symlinks are forbidden")
            if name in directory_names:
                if not stat.S_ISDIR(metadata.st_mode):
                    raise BundleIntegrityError("bundle directory entry is not a directory")
            elif not stat.S_ISREG(metadata.st_mode):
                raise BundleIntegrityError("bundle contains a special file")


def load_bundle_documents(
    bundle_root: Path, *, manifest: LegacyBundleManifest | None = None
) -> tuple[LegacyBundleDocument, ...]:
    root = bundle_root.resolve(strict=True)
    bundle_manifest = manifest or LegacyBundleManifest.model_validate_json(
        (root / "bundle-manifest.json").read_bytes()
    )
    documents: list[LegacyBundleDocument] = []
    seen: set[str] = set()
    for line in (root / bundle_manifest.documents_manifest).read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        document = LegacyBundleDocument.model_validate_json(line)
        if document.document_id in seen:
            raise BundleIntegrityError("bundle contains duplicate document IDs")
        for relative, digest in (
            (document.pdf_object_path, document.pdf_sha256),
            (document.ocr_object_path, document.ocr_sha256),
            (document.metadata_object_path, document.metadata_sha256),
        ):
            if relative is None or digest is None:
                continue
            target = (root / relative).resolve(strict=True)
            if not target.is_relative_to(root) or target.is_symlink() or not target.is_file():
                raise BundleIntegrityError("document object reference escaped the bundle")
            if _sha256_file(target) != digest:
                raise BundleIntegrityError("document object reference hash mismatch")
        seen.add(document.document_id)
        documents.append(document)
    record_files = tuple(sorted((root / "records").rglob("record.json")))
    if len(record_files) != len(documents):
        raise BundleIntegrityError("normalized record file count differs from document manifest")
    record_ids: set[str] = set()
    expected_record_payloads = {
        document.document_id: _json_line(document.model_dump(mode="json")) for document in documents
    }
    for path in record_files:
        record = LegacyBundleDocument.model_validate_json(path.read_bytes())
        if record.document_id in record_ids:
            raise BundleIntegrityError("normalized record files contain a collision")
        if path.read_bytes() != expected_record_payloads.get(record.document_id):
            raise BundleIntegrityError("normalized record is not canonical or differs from manifest")
        version = _safe_version_segment(record.source_version)
        base = (
            Path("records")
            / record.issuer.value
            / record.product_code
            / record.document_type
            / record.effective_date
        )
        allowed = {
            (base / version / "record.json").as_posix(),
            (base / f"{version}--{record.document_id[-12:]}" / "record.json").as_posix(),
        }
        if path.relative_to(root).as_posix() not in allowed:
            raise BundleIntegrityError("normalized record is stored at an unexpected path")
        record_ids.add(record.document_id)
    if record_ids != seen:
        raise BundleIntegrityError("normalized records differ from document manifest")
    return tuple(documents)
