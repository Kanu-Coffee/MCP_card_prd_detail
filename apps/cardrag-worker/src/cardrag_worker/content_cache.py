"""Immutable, run-snapshotted content OCR variant access.

The flat append-only index permits one WebDAV PROPFIND at run start. It is a
discovery aid only: every selected variant must pass manifest, READY, CAS and
OCR-byte verification before it can be used.
"""

from __future__ import annotations

import json
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from cardrag_core import (
    ContentOCRArtifactManifest,
    ContentOCRReady,
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
    def __init__(self, *, webdav: WebDAVClient, state_root: Path) -> None:
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
            try:
                entries = await self.webdav.list_children(CONTENT_INDEX_ROOT)
            except WebDAVHTTPError as exc:
                if exc.status_code != 404:
                    raise
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
        index_body = await self.webdav.get_bytes(entry, max_bytes=CONTROL_OBJECT_MAX_BYTES)
        manifest_body = await self.webdav.get_bytes(
            root / "manifest.json", max_bytes=CONTROL_OBJECT_MAX_BYTES
        )
        ready_body = await self.webdav.get_bytes(root / "READY.json", max_bytes=CONTROL_OBJECT_MAX_BYTES)
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
        body = await self.webdav.get_bytes(manifest.output.path, max_bytes=manifest.output.size_bytes)
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

    async def lookup(
        self,
        *,
        run_id: str,
        source: OCRInput,
        cache_epoch: int,
        expected_ocr_identity: tuple[str, int] | None = None,
        document_id: str | None = None,
    ) -> ContentOCRVariantHit | None:
        await self.freeze(run_id)
        key = content_addressed_ocr_reuse_key(source, cache_epoch=cache_epoch)
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
            return None
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

    async def publish(self, manifest: ContentOCRArtifactManifest, body: bytes) -> None:
        """Commit CAS, manifest, READY, then the discoverable index marker."""

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
        root = manifest.variant_root
        await self.webdav.put_bytes(
            root / "manifest.json", manifest.canonical_bytes(), content_type="application/json"
        )
        ready = ContentOCRReady.for_manifest(manifest)
        await self.webdav.put_bytes(
            root / "READY.json", ready.canonical_bytes(), content_type="application/json"
        )
        index = {
            "schema_version": "cardrag.ocr-content-index.v1",
            "reuse_key": manifest.reuse_key,
            "variant_id": manifest.variant_id,
            "manifest_sha256": manifest.manifest_sha256,
            "variant_label": manifest.variant_label,
        }
        await self.webdav.put_bytes(
            content_index_path(manifest), canonical_json_bytes(index), content_type="application/json"
        )
