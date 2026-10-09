"""Immutable, run-snapshotted content OCR variant access.

The flat append-only index permits one WebDAV PROPFIND at run start. It is a
discovery aid only: every selected variant must pass manifest, READY, CAS and
OCR-byte verification before it can be used.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

from cardrag_core import (
    ArtifactRef,
    ContentOCRArtifactManifest,
    ContentOCRImportedProvenance,
    ContentOCRReady,
    NativeOCRContract,
    OCRArtifactManifest,
    OCRInput,
    VerifiedOCR,
    WebDAVHTTPError,
    canonical_json_bytes,
    content_addressed_ocr_reuse_key,
    content_ocr_variant_root_path,
    verify_ocr_bytes,
)
from cardrag_core.paths import validate_identifier

from .providers import reject_credential_bearing_ocr
from .webdav import CONTROL_OBJECT_MAX_BYTES, WebDAVClient

CONTENT_INDEX_ROOT = PurePosixPath("v1", "ocr-cache", "content-index")
_INDEX_NAME = re.compile(
    r"^(?P<key>[0-9a-f]{64})-"
    r"(?P<label>[0-9]{8}T[0-9]{12}Z-[0-9a-f]{12})\.json$"
)
_MAX_INDEX_ENTRIES = 200_000
_MAX_SNAPSHOT_BYTES = 32 * 1024 * 1024


class ContentCacheValidationError(ValueError):
    """A listed content variant failed an immutable contract check."""


@dataclass(frozen=True, slots=True)
class ContentOCRVariantHit:
    manifest: ContentOCRArtifactManifest
    body: bytes
    verified: VerifiedOCR


def content_index_path(manifest: ContentOCRArtifactManifest) -> PurePosixPath:
    return CONTENT_INDEX_ROOT / f"{manifest.reuse_key}-{manifest.variant_label}.json"


def _indexed_variant_path(entry: PurePosixPath) -> tuple[str, PurePosixPath]:
    if entry.parent != CONTENT_INDEX_ROOT:
        raise ContentCacheValidationError("content index path escaped its root")
    match = _INDEX_NAME.fullmatch(entry.name)
    if match is None:
        raise ContentCacheValidationError("content index entry name is invalid")
    return match["key"], content_ocr_variant_root_path(match["key"], match["label"])


class ContentOCRVariantStore:
    def __init__(self, *, webdav: WebDAVClient | None = None, state_root: Path) -> None:
        self.webdav = webdav
        self.state_root = state_root
        self._snapshots: dict[str, dict[str, tuple[PurePosixPath, ...]]] = {}

    def _selection_path(self, run_id: str, document_id: str) -> Path:
        safe_run = validate_identifier(run_id, label="run_id")
        safe_document = validate_identifier(document_id, label="document_id")
        return self.state_root / "runs" / safe_run / "content-ocr-selections" / f"{safe_document}.json"

    @staticmethod
    def _atomic_write(path: Path, body: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        try:
            with temporary.open("xb") as handle:
                os.chmod(temporary, 0o600)
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    async def freeze(self, run_id: str) -> None:
        """Reuse a durable snapshot on resume; otherwise enumerate once."""

        safe_id = validate_identifier(run_id, label="run_id")
        if safe_id in self._snapshots:
            return
        snapshot_path = self.state_root / "runs" / safe_id / "content-ocr-index-snapshot.json"
        if snapshot_path.exists():
            if snapshot_path.is_symlink() or not snapshot_path.is_file():
                raise ContentCacheValidationError("content index snapshot is not a regular file")
            if snapshot_path.stat().st_size > _MAX_SNAPSHOT_BYTES:
                raise ContentCacheValidationError("content index snapshot is too large")
            body = snapshot_path.read_bytes()
            try:
                payload = json.loads(body)
            except (ValueError, UnicodeDecodeError) as exc:
                raise ContentCacheValidationError("content index snapshot JSON is invalid") from exc
            if (
                not isinstance(payload, dict)
                or set(payload) != {"schema_version", "entries"}
                or payload["schema_version"] != "cardrag.ocr-content-index-snapshot.v1"
                or not isinstance(payload["entries"], list)
                or len(payload["entries"]) > _MAX_INDEX_ENTRIES
                or canonical_json_bytes(payload) != body
            ):
                raise ContentCacheValidationError("content index snapshot contract is invalid")
            entries = tuple(PurePosixPath(value) for value in payload["entries"] if isinstance(value, str))
            if len(entries) != len(payload["entries"]):
                raise ContentCacheValidationError("content index snapshot entry is invalid")
        else:
            if self.webdav is not None:
                try:
                    entries = await self.webdav.list_children(CONTENT_INDEX_ROOT)
                except WebDAVHTTPError as exc:
                    if exc.status_code != 404:
                        raise
                    entries = ()
            else:
                retained_body: bytes | None = None
                runs_dir = self.state_root / "runs"
                if runs_dir.is_dir():
                    for other_run in sorted(runs_dir.iterdir(), reverse=True):
                        if other_run.name == safe_id or not other_run.is_dir():
                            continue
                        cand = other_run / "content-ocr-index-snapshot.json"
                        if cand.is_file() and not cand.is_symlink():
                            with suppress(Exception):
                                cand_bytes = cand.read_bytes()
                                p_cand = json.loads(cand_bytes)
                                if (
                                    isinstance(p_cand, dict)
                                    and p_cand.get("schema_version") == "cardrag.ocr-content-index-snapshot.v1"
                                    and isinstance(p_cand.get("entries"), list)
                                ):
                                    retained_body = cand_bytes
                                    break
                if retained_body is not None:
                    p_loaded = json.loads(retained_body)
                    entries = tuple(PurePosixPath(v) for v in p_loaded["entries"] if isinstance(v, str))
                else:
                    entries = ()
            if len(entries) > _MAX_INDEX_ENTRIES:
                raise ContentCacheValidationError("content index exceeds the run limit")
            entries = tuple(sorted(entries, key=lambda item: item.as_posix()))
            for entry in entries:
                _indexed_variant_path(entry)
            body = canonical_json_bytes(
                {
                    "schema_version": "cardrag.ocr-content-index-snapshot.v1",
                    "entries": [entry.as_posix() for entry in entries],
                }
            )
            if len(body) > _MAX_SNAPSHOT_BYTES:
                raise ContentCacheValidationError("content index snapshot is too large")
            self._atomic_write(snapshot_path, body)

        by_key: dict[str, list[PurePosixPath]] = {}
        for entry in entries:
            key, _ = _indexed_variant_path(entry)
            by_key.setdefault(key, []).append(entry)
        self._snapshots[safe_id] = {key: tuple(paths) for key, paths in by_key.items()}

    def _require_webdav(self) -> WebDAVClient:
        if self.webdav is None:
            raise ContentCacheValidationError("remote WebDAV client is not available in local mode")
        return self.webdav

    async def _read_variant(
        self,
        entry: PurePosixPath,
        *,
        source: OCRInput,
        reuse_key: str,
    ) -> ContentOCRVariantHit:
        key, root = _indexed_variant_path(entry)
        if key != reuse_key:
            raise ContentCacheValidationError("content index key does not match PDF")
        webdav = self._require_webdav()
        index_body = await webdav.get_bytes(entry, max_bytes=CONTROL_OBJECT_MAX_BYTES)
        manifest_body = await webdav.get_bytes(
            root / "manifest.json", max_bytes=CONTROL_OBJECT_MAX_BYTES
        )
        ready_body = await webdav.get_bytes(root / "READY.json", max_bytes=CONTROL_OBJECT_MAX_BYTES)
        if index_body is None or manifest_body is None or ready_body is None:
            raise ContentCacheValidationError("content variant control file is missing")
        try:
            index = json.loads(index_body)
            manifest = ContentOCRArtifactManifest.model_validate_json(manifest_body)
            ready = ContentOCRReady.model_validate_json(ready_body)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ContentCacheValidationError("content variant control JSON is invalid") from exc
        if (
            not isinstance(index, dict)
            or set(index) != {"schema_version", "reuse_key", "variant_id", "manifest_sha256", "variant_label"}
            or index["schema_version"] != "cardrag.ocr-content-index.v1"
            or canonical_json_bytes(index) != index_body
            or manifest.canonical_bytes() != manifest_body
            or ready.canonical_bytes() != ready_body
            or manifest.source != source
            or manifest.reuse_key != reuse_key
            or manifest.variant_root != root
            or not ready.verifies(manifest)
            or index["reuse_key"] != reuse_key
            or index["variant_id"] != manifest.variant_id
            or index["manifest_sha256"] != manifest.manifest_sha256
            or index["variant_label"] != manifest.variant_label
        ):
            raise ContentCacheValidationError("content variant identity is invalid")
        body = await webdav.get_bytes(manifest.output.path, max_bytes=manifest.output.size_bytes)
        if body is None:
            raise ContentCacheValidationError("content variant CAS object is missing")
        reject_credential_bearing_ocr(body)
        try:
            verified = verify_ocr_bytes(
                body,
                expected_page_count=source.page_count,
                expected_sha256=manifest.output.sha256,
                expected_size_bytes=manifest.output.size_bytes,
                expected_char_count=manifest.ocr_chars,
                expected_page_sha256=manifest.page_output_sha256,
            )
        except ValueError as exc:
            raise ContentCacheValidationError("content variant OCR bytes are invalid") from exc
        return ContentOCRVariantHit(manifest=manifest, body=body, verified=verified)

    async def _lookup_local(
        self,
        *,
        run_id: str,
        document_id: str | None,
        source: OCRInput,
        key: str,
        cache_epoch: int,
        expected_ocr_identity: tuple[str, int] | None = None,
    ) -> ContentOCRVariantHit | None:
        runs_dir = self.state_root / "runs"
        if not runs_dir.is_dir():
            return None

        for cand_run in sorted(runs_dir.iterdir(), reverse=True):
            if cand_run.name == run_id or not cand_run.is_dir():
                continue
            publish_path = cand_run / "sealed" / "publish.json"
            if not publish_path.is_file() or publish_path.is_symlink():
                continue
            documents: list[dict[str, Any]] = []
            manifest_data: dict[str, Any] = {}
            with suppress(Exception):
                seal_data = json.loads(publish_path.read_text(encoding="utf-8"))
                manifest_data = seal_data.get("manifest", {})
                documents = manifest_data.get("documents", [])

            if document_id is not None:
                documents = sorted(documents, key=lambda d: 0 if d.get("document_id") == document_id else 1)

            for doc in documents:
                if doc.get("availability") != "available":
                    continue
                # Strict PDF source match is mandatory
                pdf_info = doc.get("pdf", {})
                pdf_matches = (
                    pdf_info.get("sha256") == source.pdf_sha256
                    and int(pdf_info.get("size_bytes", 0)) == source.pdf_size_bytes
                    and int(doc.get("page_count", 0)) == source.page_count
                )
                if not pdf_matches:
                    continue

                # Cache epoch boundary / reuse key check
                doc_cache_kind = doc.get("ocr_cache_kind")
                doc_reuse_key = doc.get("ocr_reuse_key")
                if doc_cache_kind == "content":
                    # Content cache must strictly match the expected reuse key for this epoch
                    if doc_reuse_key != key:
                        continue
                else:
                    # Native or adopted document fallback to content cache is only permitted at epoch 0
                    if cache_epoch != 0:
                        continue

                ocr_info = doc.get("ocr", {})
                ocr_sha = ocr_info.get("sha256")
                ocr_size = int(ocr_info.get("size_bytes", 0))
                if not ocr_sha or ocr_size <= 0:
                    continue
                if expected_ocr_identity is not None and (ocr_sha, ocr_size) != expected_ocr_identity:
                    continue
                cand_doc_id = doc.get("document_id")
                if not cand_doc_id:
                    continue
                ocr_file = cand_run / "documents" / cand_doc_id / "ocr" / "ocr.md"
                if not ocr_file.is_file() or ocr_file.is_symlink() or ocr_file.stat().st_size != ocr_size:
                    continue
                body = ocr_file.read_bytes()
                if hashlib.sha256(body).hexdigest() != ocr_sha:
                    continue
                reject_credential_bearing_ocr(body)
                verified_opt: VerifiedOCR | None = None
                with suppress(Exception):
                    verified_opt = verify_ocr_bytes(
                        body,
                        expected_page_count=source.page_count,
                        expected_sha256=ocr_sha,
                        expected_size_bytes=ocr_size,
                    )
                if verified_opt is None:
                    continue
                verified = verified_opt

                created_at_val: datetime
                if isinstance(doc.get("created_at"), str):
                    try:
                        created_at_val = datetime.fromisoformat(doc["created_at"])
                    except ValueError:
                        created_at_val = datetime.fromtimestamp(publish_path.stat().st_mtime, tz=UTC)
                elif isinstance(manifest_data.get("created_at"), str):
                    try:
                        created_at_val = datetime.fromisoformat(manifest_data["created_at"])
                    except ValueError:
                        created_at_val = datetime.fromtimestamp(publish_path.stat().st_mtime, tz=UTC)
                else:
                    created_at_val = datetime.fromtimestamp(publish_path.stat().st_mtime, tz=UTC)

                cand_gen_id = str(manifest_data.get("generation_id") or cand_run.name)
                provenance: NativeOCRContract | ContentOCRImportedProvenance = (
                    ContentOCRImportedProvenance(
                        source_kind="generation-only",
                        provider="generation-only",
                        model="unrecorded",
                        generation_id=cand_gen_id,
                        document_id=cand_doc_id,
                    )
                )

                native_man_path = cand_run / "documents" / cand_doc_id / "ocr" / "native-manifest.json"
                if native_man_path.is_file() and not native_man_path.is_symlink():
                    with suppress(Exception):
                        n_man = OCRArtifactManifest.model_validate_json(native_man_path.read_bytes())
                        provenance = n_man.contract

                cand_variant_id = doc.get("ocr_variant_id")
                if isinstance(cand_variant_id, str) and re.fullmatch(r"[0-9a-f]{64}", cand_variant_id):
                    manifest = ContentOCRArtifactManifest(
                        schema_version="cardrag.ocr-content-artifact.v1",
                        reuse_key=key,
                        cache_epoch=cache_epoch,
                        source=source,
                        output=ArtifactRef.for_cas(
                            sha256=ocr_sha,
                            size_bytes=ocr_size,
                            media_type="text/markdown; charset=utf-8",
                        ),
                        ocr_chars=verified.char_count,
                        page_output_sha256=verified.page_sha256,
                        created_at=created_at_val,
                        provenance=provenance,
                        variant_id=cand_variant_id,
                    )
                else:
                    manifest = ContentOCRArtifactManifest.create(
                        source=source,
                        cache_epoch=cache_epoch,
                        output=ArtifactRef.for_cas(
                            sha256=ocr_sha,
                            size_bytes=ocr_size,
                            media_type="text/markdown; charset=utf-8",
                        ),
                        ocr_chars=verified.char_count,
                        page_output_sha256=verified.page_sha256,
                        created_at=created_at_val,
                        provenance=provenance,
                        reprocess_request_id=None,
                    )

                if document_id is not None:
                    sel_path = self._selection_path(run_id, document_id)
                    self._atomic_write(
                        sel_path,
                        canonical_json_bytes(
                            {
                                "schema_version": "cardrag.ocr-content-selection.v1",
                                "reuse_key": key,
                                "entry": f"local-sealed/{cand_run.name}/{cand_doc_id}",
                                "variant_id": manifest.variant_id,
                            }
                        ),
                    )

                return ContentOCRVariantHit(manifest=manifest, body=body, verified=verified)
        return None

    async def lookup(
        self,
        *,
        run_id: str,
        source: OCRInput,
        cache_epoch: int,
        expected_ocr_identity: tuple[str, int] | None = None,
        document_id: str | None = None,
    ) -> ContentOCRVariantHit | None:
        key = content_addressed_ocr_reuse_key(source, cache_epoch=cache_epoch)
        if self.webdav is None:
            return await self._lookup_local(
                run_id=run_id,
                document_id=document_id,
                source=source,
                key=key,
                cache_epoch=cache_epoch,
                expected_ocr_identity=expected_ocr_identity,
            )
        await self.freeze(run_id)
        selection_path = self._selection_path(run_id, document_id) if document_id is not None else None
        if selection_path is not None and selection_path.exists():
            if selection_path.is_symlink() or not selection_path.is_file():
                raise ContentCacheValidationError("content selection is not a regular file")
            if selection_path.stat().st_size > CONTROL_OBJECT_MAX_BYTES:
                raise ContentCacheValidationError("content selection is too large")
            selection_body = selection_path.read_bytes()
            try:
                selection = json.loads(selection_body)
            except (ValueError, UnicodeDecodeError) as exc:
                raise ContentCacheValidationError("content selection JSON is invalid") from exc
            if (
                not isinstance(selection, dict)
                or set(selection) != {"schema_version", "reuse_key", "entry", "variant_id"}
                or selection["schema_version"] != "cardrag.ocr-content-selection.v1"
                or selection["reuse_key"] != key
                or not isinstance(selection["entry"], str)
                or not isinstance(selection["variant_id"], str)
                or canonical_json_bytes(selection) != selection_body
            ):
                raise ContentCacheValidationError("content selection contract is invalid")
            entry = PurePosixPath(selection["entry"])
            if entry not in self._snapshots[run_id].get(key, ()):
                raise ContentCacheValidationError("content selection is outside the run snapshot")
            try:
                hit = await self._read_variant(entry, source=source, reuse_key=key)
            except ContentCacheValidationError as exc:
                raise ContentCacheValidationError("frozen content selection is no longer valid") from exc
            if hit.manifest.variant_id != selection["variant_id"]:
                raise ContentCacheValidationError("frozen content selection variant changed")
            if (
                expected_ocr_identity is not None
                and (
                    hit.manifest.output.sha256,
                    hit.manifest.output.size_bytes,
                )
                != expected_ocr_identity
            ):
                raise ContentCacheValidationError("frozen content selection conflicts with retained OCR")
            return hit
        verified: list[ContentOCRVariantHit] = []
        entries: dict[str, PurePosixPath] = {}
        for entry in self._snapshots[run_id].get(key, ()):
            try:
                hit = await self._read_variant(entry, source=source, reuse_key=key)
            except ContentCacheValidationError:
                continue
            if (
                expected_ocr_identity is not None
                and (
                    hit.manifest.output.sha256,
                    hit.manifest.output.size_bytes,
                )
                != expected_ocr_identity
            ):
                continue
            verified.append(hit)
            entries[hit.manifest.variant_id] = entry
        if not verified:
            return await self._lookup_local(
                run_id=run_id,
                document_id=document_id,
                source=source,
                key=key,
                cache_epoch=cache_epoch,
                expected_ocr_identity=expected_ocr_identity,
            )
        selected = max(verified, key=lambda hit: (hit.manifest.created_at, hit.manifest.manifest_sha256))
        if selection_path is not None:
            self._atomic_write(
                selection_path,
                canonical_json_bytes(
                    {
                        "schema_version": "cardrag.ocr-content-selection.v1",
                        "reuse_key": key,
                        "entry": entries[selected.manifest.variant_id].as_posix(),
                        "variant_id": selected.manifest.variant_id,
                    }
                ),
            )
        return selected

    async def lookup_request(
        self, *, source: OCRInput, cache_epoch: int, request_id: str
    ) -> ContentOCRVariantHit | None:
        """Find a previous attempt's committed variant, including after same-run resume."""

        if self.webdav is None:
            return None
        validate_identifier(request_id, label="reprocess_request_id")
        key = content_addressed_ocr_reuse_key(source, cache_epoch=cache_epoch)
        try:
            entries = await self.webdav.list_children(CONTENT_INDEX_ROOT)
        except WebDAVHTTPError as exc:
            if exc.status_code != 404:
                raise
            return None
        hits: list[ContentOCRVariantHit] = []
        for entry in entries:
            indexed_key, _root = _indexed_variant_path(entry)
            if indexed_key != key:
                continue
            try:
                hit = await self._read_variant(entry, source=source, reuse_key=key)
            except ContentCacheValidationError:
                continue
            if hit.manifest.reprocess_request_id == request_id:
                hits.append(hit)
        return (
            max(hits, key=lambda hit: (hit.manifest.created_at, hit.manifest.manifest_sha256))
            if hits
            else None
        )

    async def verified_variants(
        self, *, source: OCRInput, cache_epoch: int
    ) -> tuple[ContentOCRVariantHit, ...]:
        """List only fully verified variants for an explicit PDF."""

        if self.webdav is None:
            return ()
        key = content_addressed_ocr_reuse_key(source, cache_epoch=cache_epoch)
        try:
            entries = await self.webdav.list_children(CONTENT_INDEX_ROOT)
        except WebDAVHTTPError as exc:
            if exc.status_code != 404:
                raise
            return ()
        if len(entries) > _MAX_INDEX_ENTRIES:
            raise ContentCacheValidationError("content index exceeds the lookup limit")
        hits: list[ContentOCRVariantHit] = []
        for entry in entries:
            indexed_key, _ = _indexed_variant_path(entry)
            if indexed_key == key:
                hits.append(await self._read_variant(entry, source=source, reuse_key=key))
        return tuple(sorted(hits, key=lambda hit: (hit.manifest.created_at, hit.manifest.manifest_sha256)))

    async def has_verified_variant_after(
        self, *, run_id: str, source: OCRInput, cache_epoch: int, cutoff: datetime
    ) -> bool:
        """Detect a new variant since the served generation without reading old variants."""

        if self.webdav is None:
            return False
        await self.freeze(run_id)
        key = content_addressed_ocr_reuse_key(source, cache_epoch=cache_epoch)
        for entry in self._snapshots[run_id].get(key, ()):
            match = _INDEX_NAME.fullmatch(entry.name)
            if match is None:
                raise ContentCacheValidationError("content index entry name is invalid")
            label_time = datetime.strptime(match["label"].split("-", 1)[0], "%Y%m%dT%H%M%S%fZ").replace(
                tzinfo=UTC
            )
            if label_time <= cutoff:
                continue
            try:
                hit = await self._read_variant(entry, source=source, reuse_key=key)
            except ContentCacheValidationError:
                continue
            if hit.manifest.created_at > cutoff:
                return True
        return False

    async def restore_variant(
        self, *, source: OCRInput, cache_epoch: int, variant_id: str
    ) -> ContentOCRArtifactManifest:
        """Promote verified historical bytes as a new immutable latest variant."""

        validate_identifier(variant_id, label="variant_id")
        hits = await self.verified_variants(source=source, cache_epoch=cache_epoch)
        original = next((hit for hit in hits if hit.manifest.variant_id == variant_id), None)
        if original is None:
            raise ContentCacheValidationError("requested historical variant is not verified")
        latest = hits[-1].manifest.created_at
        created_at = max(datetime.now(UTC), latest + timedelta(microseconds=1))
        manifest = ContentOCRArtifactManifest.create(
            source=source,
            cache_epoch=cache_epoch,
            output=original.manifest.output,
            ocr_chars=original.manifest.ocr_chars,
            page_output_sha256=original.manifest.page_output_sha256,
            created_at=created_at,
            provenance=original.manifest.provenance,
            restored_from=original.manifest.variant_id,
        )
        await self.publish_existing(manifest)
        return manifest

    async def verify_all(self) -> tuple[ContentOCRArtifactManifest, ...]:
        """Read and verify every discoverable variant without changing remote state."""

        if self.webdav is None:
            return ()
        try:
            entries = await self.webdav.list_children(CONTENT_INDEX_ROOT)
        except WebDAVHTTPError as exc:
            if exc.status_code != 404:
                raise
            entries = ()
        if len(entries) > _MAX_INDEX_ENTRIES:
            raise ContentCacheValidationError("content index exceeds the verification limit")
        semaphore = asyncio.Semaphore(16)

        webdav = self._require_webdav()

        async def verify_one(entry: PurePosixPath) -> ContentOCRArtifactManifest:
            async with semaphore:
                key, root = _indexed_variant_path(entry)
                manifest_body = await webdav.get_bytes(
                    root / "manifest.json", max_bytes=CONTROL_OBJECT_MAX_BYTES
                )
                if manifest_body is None:
                    raise ContentCacheValidationError("indexed content manifest is missing")
                try:
                    manifest = ContentOCRArtifactManifest.model_validate_json(manifest_body)
                except ValueError as exc:
                    raise ContentCacheValidationError("indexed content manifest is invalid") from exc
                hit = await self._read_variant(entry, source=manifest.source, reuse_key=key)
                return hit.manifest

        return tuple(await asyncio.gather(*(verify_one(entry) for entry in entries)))

    async def publish(self, manifest: ContentOCRArtifactManifest, body: bytes) -> None:
        """Commit CAS, manifest, READY, then the discoverable index marker."""

        if self.webdav is None:
            return
        reject_credential_bearing_ocr(body)
        verify_ocr_bytes(
            body,
            expected_page_count=manifest.source.page_count,
            expected_sha256=manifest.output.sha256,
            expected_size_bytes=manifest.output.size_bytes,
            expected_char_count=manifest.ocr_chars,
            expected_page_sha256=manifest.page_output_sha256,
        )
        sha, path = await self.webdav.put_cas(body, media_type="text/markdown; charset=utf-8")
        if sha != manifest.output.sha256 or path != manifest.output.path:
            raise ContentCacheValidationError("content variant CAS publication identity differs")
        await self._publish_controls(manifest)

    async def publish_existing(self, manifest: ContentOCRArtifactManifest) -> None:
        """Migrate a verified existing CAS without uploading its OCR bytes again."""

        if self.webdav is None:
            return
        body = await self.webdav.get_bytes(manifest.output.path, max_bytes=manifest.output.size_bytes)
        if body is None:
            raise ContentCacheValidationError("migration OCR CAS object is missing")
        reject_credential_bearing_ocr(body)
        verify_ocr_bytes(
            body,
            expected_page_count=manifest.source.page_count,
            expected_sha256=manifest.output.sha256,
            expected_size_bytes=manifest.output.size_bytes,
            expected_char_count=manifest.ocr_chars,
            expected_page_sha256=manifest.page_output_sha256,
        )
        await self._publish_controls(manifest)

    async def _publish_controls(self, manifest: ContentOCRArtifactManifest) -> None:
        webdav = self._require_webdav()
        root = manifest.variant_root
        await webdav.put_bytes(
            root / "manifest.json", manifest.canonical_bytes(), content_type="application/json"
        )
        ready = ContentOCRReady.for_manifest(manifest)
        await webdav.put_bytes(
            root / "READY.json", ready.canonical_bytes(), content_type="application/json"
        )
        index = {
            "schema_version": "cardrag.ocr-content-index.v1",
            "reuse_key": manifest.reuse_key,
            "variant_id": manifest.variant_id,
            "manifest_sha256": manifest.manifest_sha256,
            "variant_label": manifest.variant_label,
        }
        await webdav.put_bytes(
            content_index_path(manifest), canonical_json_bytes(index), content_type="application/json"
        )
