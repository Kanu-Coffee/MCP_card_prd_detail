"""Read-only legacy inventory and deterministic pilot reuse.

The migrator never opens legacy SQLite databases writable and never writes
under its source root.  Missing raw paths are resolved only by the trusted
manifest SHA-256 against a prebuilt file inventory.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import uuid
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cardrag.storage import ContentAddressedObjectStore


class LegacyFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    relative_path: str
    sha256: str
    size: int = Field(ge=0)


class LegacyInventory(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "cardrag-legacy-inventory.v1"
    source_root: str
    created_at: datetime
    files: tuple[LegacyFile, ...]

    @model_validator(mode="after")
    def paths_unique(self) -> Self:
        paths = [item.relative_path for item in self.files]
        if len(paths) != len(set(paths)):
            raise ValueError("legacy inventory contains duplicate paths")
        return self


class MigrationException(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    document_key: str
    code: str
    detail: str


class LegacyDocumentMapping(BaseModel):
    """Validated, portable lineage for one accepted legacy document."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    document_key: str
    issuer: str
    product_code: str
    mapping_method: str
    legacy_pdf_path: str | None
    pdf_sha256: str
    pdf_object_path: str
    legacy_ocr_path: str
    ocr_sha256: str
    ocr_object_path: str
    metadata_path: str | None
    metadata_sha256: str | None
    metadata_schema: str
    page_count: int = Field(gt=0)
    warnings: tuple[str, ...] = ()


class LegacyMigrationReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "cardrag-legacy-migration-report.v1"
    inventory_sha256: str
    selected_documents: int
    migrated_documents: int
    unique_pdf_objects: int
    unique_ocr_objects: int
    raw_path_missing_resolved_by_hash: int
    quarantined: int
    master_ocr_chars_drift: int
    metadata_ocr_hash_verified: int
    historical_embeddings_imported: int = 0
    excluded_runtime_kinds: tuple[str, ...] = (
        "legacy_archive",
        "historical_embedding",
        "email_job",
        "report_mailbox",
        "rendered_page_png",
        "temporary_ocr",
    )
    mappings: tuple[LegacyDocumentMapping, ...]
    exceptions: tuple[MigrationException, ...]
    source_writes: int = 0


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _inventory_content_hash(inventory: LegacyInventory) -> str:
    """Hash only portable file facts, not scan time or host-specific root."""

    body = json.dumps(
        [item.model_dump(mode="json") for item in inventory.files],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(body).hexdigest()


def _declared_sha256(value: object) -> str:
    raw = str(value or "").strip().lower()
    if raw.startswith("sha256:"):
        raw = raw.removeprefix("sha256:")
    return raw if re.fullmatch(r"[0-9a-f]{64}", raw) else ""


class LegacyMigrator:
    def __init__(self, source_root: Path, target_store: ContentAddressedObjectStore) -> None:
        self.source_root = source_root.resolve(strict=True)
        self.target_store = target_store

    def _source_file(self, relative_path: str) -> Path:
        candidate = (self.source_root / relative_path).resolve(strict=True)
        if not candidate.is_relative_to(self.source_root) or not candidate.is_file():
            raise ValueError("legacy path escaped read-only source root")
        return candidate

    def inventory(self, *, suffixes: frozenset[str] | None = None) -> LegacyInventory:
        selected = suffixes or frozenset({".pdf", ".md", ".json", ".sqlite3", ".sqlite"})
        files: list[LegacyFile] = []
        for path in sorted(self.source_root.rglob("*")):
            if not path.is_file() or path.suffix.casefold() not in selected:
                continue
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(self.source_root):
                raise ValueError("legacy source contains symlink escape")
            files.append(
                LegacyFile(
                    relative_path=resolved.relative_to(self.source_root).as_posix(),
                    sha256=_sha256(resolved),
                    size=resolved.stat().st_size,
                )
            )
        return LegacyInventory(
            source_root=str(self.source_root), created_at=datetime.now(UTC), files=tuple(files)
        )

    def migrate_manifest(
        self,
        manifest_path: Path,
        *,
        limit: int | None = None,
        dry_run: bool = False,
    ) -> LegacyMigrationReport:
        manifest_path = manifest_path.resolve(strict=True)
        if not manifest_path.is_relative_to(self.source_root):
            raise ValueError("manifest must be under legacy source root")
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        entries = list(payload.get("items") or payload.get("entries") or payload.get("documents") or [])
        if limit is not None:
            entries = entries[:limit]
        inventory = self.inventory(suffixes=frozenset({".pdf", ".md", ".json"}))
        by_hash: dict[str, list[LegacyFile]] = defaultdict(list)
        for item in inventory.files:
            by_hash[item.sha256].append(item)
        exceptions: list[MigrationException] = []
        missing_resolved = 0
        drifts = 0
        verified_ocr_hashes = 0
        stored_pdf_hashes: set[str] = set()
        stored_ocr_hashes: set[str] = set()
        mappings: list[LegacyDocumentMapping] = []
        for raw in entries:
            key = str(
                raw.get("doc_version_id") or raw.get("document_id") or raw.get("productCode") or "unknown"
            )
            expected = _declared_sha256(raw.get("pdf_sha256"))
            metadata_rel = str(raw.get("metadata_remote_rel") or raw.get("metadata_rel_path") or "")
            metadata: dict[str, object] = {}
            metadata_path: Path | None = None
            metadata_hash: str | None = None
            if metadata_rel:
                try:
                    metadata_path = self._source_file(metadata_rel)
                    metadata_hash = _sha256(metadata_path)
                    loaded = json.loads(metadata_path.read_text(encoding="utf-8"))
                    if not isinstance(loaded, dict):
                        raise ValueError("metadata is not an object")
                    metadata = loaded
                except (FileNotFoundError, UnicodeError, ValueError, json.JSONDecodeError):
                    exceptions.append(
                        MigrationException(
                            document_key=key,
                            code="metadata_unreadable",
                            detail="quarantined",
                        )
                    )
                    continue
            candidate: Path | None = None
            rel = str(raw.get("raw_pdf_rel_path") or metadata.get("raw_pdf_rel_path") or "")
            mapping_method = "direct_path_and_hash"
            if rel:
                try:
                    candidate = self._source_file(rel)
                except (FileNotFoundError, ValueError):
                    candidate = None
            if candidate is None and expected and by_hash.get(expected):
                pdf_matches = [
                    item for item in by_hash[expected] if item.relative_path.casefold().endswith(".pdf")
                ]
                if pdf_matches:
                    candidate = self._source_file(pdf_matches[0].relative_path)
                    mapping_method = "hash_lookup"
                    missing_resolved += 1
            if candidate is None:
                exceptions.append(
                    MigrationException(
                        document_key=key, code="raw_pdf_unresolved", detail="no path/hash match"
                    )
                )
                continue
            actual = _sha256(candidate)
            if expected and actual != expected:
                exceptions.append(
                    MigrationException(document_key=key, code="pdf_hash_mismatch", detail="quarantined")
                )
                continue
            ocr_rel = str(
                raw.get("ocr_rel_path") or raw.get("ocr_remote_rel") or metadata.get("ocr_md_rel_path") or ""
            )
            if not ocr_rel:
                exceptions.append(
                    MigrationException(document_key=key, code="ocr_unresolved", detail="quarantined")
                )
                continue
            try:
                ocr_path = self._source_file(ocr_rel)
                ocr_actual = _sha256(ocr_path)
                expected_ocr_hash = _declared_sha256(
                    metadata.get("ocr_md_sha256") or raw.get("ocr_md_sha256")
                )
                if expected_ocr_hash and ocr_actual != expected_ocr_hash:
                    exceptions.append(
                        MigrationException(
                            document_key=key,
                            code="ocr_hash_mismatch",
                            detail="quarantined",
                        )
                    )
                    continue
                if expected_ocr_hash:
                    verified_ocr_hashes += 1
                ocr_text = ocr_path.read_text(encoding="utf-8")
            except (FileNotFoundError, UnicodeError, ValueError):
                exceptions.append(
                    MigrationException(document_key=key, code="ocr_unreadable", detail="quarantined")
                )
                continue

            warnings: list[str] = []
            recorded = raw.get("ocr_chars")
            if isinstance(recorded, int) and recorded != len(ocr_text):
                drifts += 1
                warnings.append("master_ocr_chars_drift")
            metadata_chars = metadata.get("ocr_md_chars")
            if isinstance(metadata_chars, int) and metadata_chars != len(ocr_text):
                exceptions.append(
                    MigrationException(
                        document_key=key,
                        code="metadata_ocr_chars_mismatch",
                        detail="quarantined",
                    )
                )
                continue
            page_markers = [int(value) for value in re.findall(r"^## Page (\d+)\s*$", ocr_text, re.M)]
            page_count_raw = metadata.get("page_count", metadata.get("pages", raw.get("pages")))
            if page_count_raw is None and page_markers:
                page_count_raw = len(page_markers)
            if not isinstance(page_count_raw, int) or page_count_raw < 1:
                exceptions.append(
                    MigrationException(document_key=key, code="page_count_missing", detail="quarantined")
                )
                continue
            if page_markers != list(range(1, page_count_raw + 1)):
                exceptions.append(
                    MigrationException(
                        document_key=key,
                        code="ocr_page_coverage_mismatch",
                        detail="quarantined",
                    )
                )
                continue

            if not dry_run:
                pdf_ref = self.target_store.put_file(candidate)
                ocr_ref = self.target_store.put_file(ocr_path)
                if pdf_ref.sha256 != actual or ocr_ref.sha256 != ocr_actual:
                    raise RuntimeError("content-addressed copy hash changed")
                pdf_object_path = pdf_ref.relative_path.as_posix()
                ocr_object_path = ocr_ref.relative_path.as_posix()
            else:
                pdf_object_path = self.target_store.relative_path_for(actual).as_posix()
                ocr_object_path = self.target_store.relative_path_for(ocr_actual).as_posix()
            stored_pdf_hashes.add(actual)
            stored_ocr_hashes.add(ocr_actual)
            mappings.append(
                LegacyDocumentMapping(
                    document_key=key,
                    issuer={
                        "wooricard": "woori",
                        "kbcard": "kb",
                        "shinhancard": "shinhan",
                    }.get(
                        str(raw.get("cardCompany") or raw.get("card_company") or "unknown"),
                        str(raw.get("cardCompany") or raw.get("card_company") or "unknown"),
                    ),
                    product_code=str(raw.get("productCode") or "unknown"),
                    mapping_method=mapping_method,
                    legacy_pdf_path=(
                        candidate.relative_to(self.source_root).as_posix()
                        if mapping_method == "direct_path_and_hash"
                        else None
                    ),
                    pdf_sha256=actual,
                    pdf_object_path=pdf_object_path,
                    legacy_ocr_path=ocr_path.relative_to(self.source_root).as_posix(),
                    ocr_sha256=ocr_actual,
                    ocr_object_path=ocr_object_path,
                    metadata_path=(
                        metadata_path.relative_to(self.source_root).as_posix()
                        if metadata_path is not None
                        else None
                    ),
                    metadata_sha256=metadata_hash,
                    metadata_schema=str(metadata.get("schema_version") or "unknown"),
                    page_count=page_count_raw,
                    warnings=tuple(warnings),
                )
            )
        return LegacyMigrationReport(
            inventory_sha256=_inventory_content_hash(inventory),
            selected_documents=len(entries),
            migrated_documents=len(mappings),
            unique_pdf_objects=len(stored_pdf_hashes),
            unique_ocr_objects=len(stored_ocr_hashes),
            raw_path_missing_resolved_by_hash=missing_resolved,
            quarantined=len(exceptions),
            master_ocr_chars_drift=drifts,
            metadata_ocr_hash_verified=verified_ocr_hashes,
            mappings=tuple(mappings),
            exceptions=tuple(exceptions),
        )

    def snapshot_source_metadata(self) -> dict[str, tuple[int, int]]:
        """Cheap proof that a pilot did not write the legacy root (size, mtime_ns)."""
        return {
            path.relative_to(self.source_root).as_posix(): (path.stat().st_size, path.stat().st_mtime_ns)
            for path in self.source_root.rglob("*")
            if path.is_file()
        }

    @staticmethod
    def create_pilot_root(build_root: Path, pilot_id: str | None = None) -> Path:
        """Create an explicitly marked, disposable migration staging root."""

        root = build_root.resolve(strict=True)
        identifier = pilot_id or f"pilot-{uuid.uuid4().hex[:12]}"
        if re.fullmatch(r"pilot-[0-9a-f]{12}", identifier) is None:
            raise ValueError("invalid pilot ID")
        pilots = root / "legacy-pilots"
        pilots.mkdir(mode=0o750, exist_ok=True)
        target = pilots / identifier
        target.mkdir(mode=0o750, exist_ok=False)
        marker = {
            "schema_version": "cardrag-legacy-pilot.v1",
            "pilot_id": identifier,
            "created_at": datetime.now(UTC).isoformat(),
        }
        (target / ".cardrag-legacy-pilot.json").write_text(
            json.dumps(marker, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return target

    @staticmethod
    def rollback_pilot(build_root: Path, pilot_root: Path) -> str:
        """Remove only an operator-selected, marked pilot under the build root."""

        parent = build_root.resolve(strict=True) / "legacy-pilots"
        target = pilot_root.resolve(strict=True)
        if not target.is_relative_to(parent) or target.parent != parent:
            raise ValueError("pilot rollback target escaped the staging root")
        if re.fullmatch(r"pilot-[0-9a-f]{12}", target.name) is None:
            raise ValueError("pilot rollback target has an invalid name")
        marker_path = target / ".cardrag-legacy-pilot.json"
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("pilot rollback marker is absent or invalid") from exc
        if marker.get("schema_version") != "cardrag-legacy-pilot.v1" or marker.get("pilot_id") != target.name:
            raise ValueError("pilot rollback marker does not match the target")
        shutil.rmtree(target)
        return target.name
