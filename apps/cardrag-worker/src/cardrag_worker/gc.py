"""Fail-closed, grace-period mark-and-sweep for remote immutable artifacts."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath

from cardrag_core import (
    STABLE_POINTER_PATH,
    AdoptedOCRArtifactManifest,
    GenerationManifest,
    GenerationPointer,
    GenerationReady,
    OCRArtifactManifest,
    OCRReady,
    WebDAVHTTPError,
    generation_database_path,
    generation_manifest_path,
    generation_ready_path,
    ocr_manifest_path,
    ocr_ready_path,
    validate_identifier,
)

from .state import WorkerState
from .webdav import WebDAVClient


class GCError(RuntimeError):
    """Control-plane uncertainty: no remote deletion was attempted."""


_HEX_PREFIX = re.compile(r"^[0-9a-f]{2}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class GCResult:
    retained_generations: tuple[str, ...]
    marked_objects: int
    candidates: tuple[str, ...]
    eligible: tuple[str, ...]
    deleted: tuple[str, ...]
    dry_run: bool


async def _required_bytes(webdav: WebDAVClient, path: PurePosixPath) -> bytes:
    body = await webdav.get_bytes(path)
    if body is None:
        raise GCError(f"required control object is missing: {path}")
    return body


async def _generation_chain(
    webdav: WebDAVClient,
    *,
    retain: int,
) -> tuple[bytes, tuple[GenerationManifest, ...]]:
    pointer_body = await _required_bytes(webdav, STABLE_POINTER_PATH)
    try:
        pointer = GenerationPointer.model_validate_json(pointer_body)
    except Exception as exc:
        raise GCError("stable pointer is invalid") from exc
    if pointer.canonical_bytes() != pointer_body:
        raise GCError("stable pointer is not canonical JSON")
    manifests: list[GenerationManifest] = []
    seen_generation_ids: set[str] = set()
    generation_id: str | None = pointer.generation_id
    for index in range(retain):
        if generation_id is None:
            break
        if generation_id in seen_generation_ids:
            raise GCError(f"generation predecessor chain contains a cycle at {generation_id}")
        seen_generation_ids.add(generation_id)
        manifest_body = await _required_bytes(webdav, generation_manifest_path(generation_id))
        ready_body = await _required_bytes(webdav, generation_ready_path(generation_id))
        try:
            manifest = GenerationManifest.model_validate_json(manifest_body)
            ready = GenerationReady.model_validate_json(ready_body)
        except Exception as exc:
            raise GCError(f"generation {generation_id} control JSON is invalid") from exc
        if manifest.canonical_bytes() != manifest_body or ready.canonical_bytes() != ready_body:
            raise GCError(f"generation {generation_id} control JSON is not canonical")
        if (
            manifest.generation_id != generation_id
            or ready.generation_id != generation_id
            or hashlib.sha256(manifest_body).hexdigest() != ready.manifest_sha256
            or manifest.serving_database.sha256 != ready.serving_database_sha256
            or manifest.serving_database.size_bytes != ready.serving_database_size_bytes
        ):
            raise GCError(f"generation {generation_id} control hashes disagree")
        if index == 0 and (
            pointer.manifest_sha256 != ready.manifest_sha256
            or pointer.ready_sha256 != hashlib.sha256(ready_body).hexdigest()
        ):
            raise GCError("stable pointer does not bind current generation READY/manifest")
        manifests.append(manifest)
        generation_id = manifest.previous_generation_id
    return pointer_body, tuple(manifests)


async def _list_generation_ids(webdav: WebDAVClient) -> tuple[str, ...]:
    children = await webdav.list_children(PurePosixPath("v1", "generations"))
    ids: list[str] = []
    for path in children:
        if path.parent != PurePosixPath("v1", "generations"):
            raise GCError("unexpected generations PROPFIND shape")
        try:
            validate_identifier(path.name, label="generation_id")
        except ValueError as exc:
            raise GCError("unsafe generation ID in PROPFIND") from exc
        generation_children = await webdav.list_children(path)
        names = {child.name for child in generation_children if child.parent == path}
        if len(names) != len(generation_children) or not names.issubset(
            {"index.sqlite3", "manifest.json", "READY.json"}
        ):
            raise GCError(f"unexpected object in generation {path.name}")
        ids.append(path.name)
    return tuple(sorted(ids))


async def _list_cas_objects(webdav: WebDAVClient) -> tuple[PurePosixPath, ...]:
    root = PurePosixPath("v1", "objects", "sha256")
    prefixes = await webdav.list_children(root)
    objects: list[PurePosixPath] = []
    for prefix in prefixes:
        if prefix.parent != root or _HEX_PREFIX.fullmatch(prefix.name) is None:
            raise GCError("unexpected CAS prefix in PROPFIND")
        for path in await webdav.list_children(prefix):
            if path.parent != prefix or _SHA256.fullmatch(path.name) is None or path.name[:2] != prefix.name:
                raise GCError("unexpected CAS object in PROPFIND")
            objects.append(path)
    return tuple(sorted(objects, key=lambda item: item.as_posix()))


async def _mark_ocr_caches(
    webdav: WebDAVClient,
    *,
    retained_references: Mapping[tuple[str, str], tuple[str, int, str]],
) -> tuple[set[str], set[str]]:
    marked: set[str] = set()
    inactive: set[str] = set()
    found_references: set[tuple[str, str]] = set()
    root = PurePosixPath("v1", "ocr-cache")
    for kind in ("native", "adopted"):
        kind_root = root / kind
        try:
            prefixes = await webdav.list_children(kind_root)
        except WebDAVHTTPError as exc:
            if exc.status_code == 404:
                prefixes = ()
            else:
                raise
        for prefix in prefixes:
            if prefix.parent != kind_root or _HEX_PREFIX.fullmatch(prefix.name) is None:
                raise GCError("unexpected OCR cache prefix")
            for reuse_root in await webdav.list_children(prefix):
                reuse_key = reuse_root.name
                if (
                    reuse_root.parent != prefix
                    or _SHA256.fullmatch(reuse_key) is None
                    or reuse_key[:2] != prefix.name
                ):
                    raise GCError("unexpected OCR cache reuse directory")
                manifest_path = ocr_manifest_path(reuse_key, kind=kind)
                ready_path = ocr_ready_path(reuse_key, kind=kind)
                reference_key = (kind, reuse_key)
                retained = retained_references.get(reference_key)
                children = await webdav.list_children(reuse_root)
                child_names = {path.name for path in children if path.parent == reuse_root}
                if len(child_names) != len(children) or not child_names.issubset(
                    {manifest_path.name, ready_path.name}
                ):
                    raise GCError(f"unexpected object in {kind} OCR cache {reuse_key}")
                manifest_body = await webdav.get_bytes(manifest_path)
                ready_body = await webdav.get_bytes(ready_path)
                if ready_body is None:
                    # No READY means this was never committed. It is a grace-period
                    # candidate even when empty or manifest-only.
                    inactive.add(reuse_root.as_posix())
                    continue
                if manifest_body is None:
                    if retained is not None:
                        raise GCError(f"retained {kind} OCR cache {reuse_key} has no manifest")
                    inactive.add(reuse_root.as_posix())
                    continue
                try:
                    manifest = (
                        OCRArtifactManifest.model_validate_json(manifest_body)
                        if kind == "native"
                        else AdoptedOCRArtifactManifest.model_validate_json(manifest_body)
                    )
                    ready = OCRReady.model_validate_json(ready_body)
                except Exception as exc:
                    if retained is not None:
                        raise GCError(f"invalid retained {kind} OCR cache {reuse_key}") from exc
                    inactive.add(reuse_root.as_posix())
                    continue
                if manifest.canonical_bytes() != manifest_body or ready.canonical_bytes() != ready_body:
                    if retained is not None:
                        raise GCError(f"non-canonical retained {kind} OCR cache {reuse_key}")
                    inactive.add(reuse_root.as_posix())
                    continue
                if (
                    manifest.reuse_key != reuse_key
                    or ready.reuse_key != reuse_key
                    or ready.manifest_sha256 != hashlib.sha256(manifest_body).hexdigest()
                    or ready.ocr_sha256 != manifest.output.sha256
                ):
                    if retained is not None:
                        raise GCError(f"unbound retained {kind} OCR cache {reuse_key}")
                    inactive.add(reuse_root.as_posix())
                    continue
                if retained is not None:
                    if retained != (
                        manifest.output.sha256,
                        manifest.output.size_bytes,
                        manifest.output.path,
                    ):
                        raise GCError(f"retained generation/cache binding differs for {kind}/{reuse_key}")
                    found_references.add(reference_key)
                    marked.update(
                        {
                            manifest_path.as_posix(),
                            ready_path.as_posix(),
                            manifest.output.path,
                        }
                    )
                else:
                    inactive.add(reuse_root.as_posix())
    missing = set(retained_references).difference(found_references)
    if missing:
        raise GCError(f"retained generation references missing OCR caches: {sorted(missing)}")
    return marked, inactive


async def collect_garbage(
    *,
    webdav: WebDAVClient,
    state: WorkerState,
    apply: bool = False,
    retain_generations: int = 3,
    grace_days: int = 30,
    now: datetime | None = None,
) -> GCResult:
    if retain_generations < 1 or grace_days < 1:
        raise ValueError("retention and grace must be positive")
    observed_at = (now or datetime.now(UTC)).astimezone(UTC)

    # Complete every parse/list/hash decision before considering DELETE.
    pointer_body, manifests = await _generation_chain(webdav, retain=retain_generations)
    retained = tuple(manifest.generation_id for manifest in manifests)
    marked: set[str] = {STABLE_POINTER_PATH.as_posix()}
    retained_cache_references: dict[tuple[str, str], tuple[str, int, str]] = {}
    for manifest in manifests:
        generation_id = manifest.generation_id
        marked.update(
            {
                generation_database_path(generation_id).as_posix(),
                generation_manifest_path(generation_id).as_posix(),
                generation_ready_path(generation_id).as_posix(),
            }
        )
        for document in manifest.documents:
            marked.add(document.pdf.path)
            if document.ocr is not None:
                marked.add(document.ocr.path)
                if document.ocr_cache_kind is not None and document.ocr_reuse_key is not None:
                    cache_key = (document.ocr_cache_kind, document.ocr_reuse_key)
                    binding = (
                        document.ocr.sha256,
                        document.ocr.size_bytes,
                        document.ocr.path,
                    )
                    previous = retained_cache_references.setdefault(cache_key, binding)
                    if previous != binding:
                        raise GCError(f"retained generations disagree on OCR cache {cache_key}")
    cache_marks, inactive_caches = await _mark_ocr_caches(
        webdav,
        retained_references=retained_cache_references,
    )
    marked.update(cache_marks)
    all_generations = await _list_generation_ids(webdav)
    all_objects = await _list_cas_objects(webdav)
    candidates = {
        f"v1/generations/{generation_id}"
        for generation_id in all_generations
        if generation_id not in retained
    }
    candidates.update(path.as_posix() for path in all_objects if path.as_posix() not in marked)
    candidates.update(inactive_caches)

    first_seen = {path: state.note_unreferenced(path, observed_at=observed_at) for path in sorted(candidates)}
    state.clear_unreferenced_except(candidates)
    threshold = observed_at - timedelta(days=grace_days)

    def deletion_order(path: str) -> tuple[int, str]:
        if path.startswith("v1/generations/"):
            return (0, path)
        if path.startswith("v1/ocr-cache/"):
            return (1, path)
        if path.startswith("v1/objects/"):
            return (2, path)
        raise GCError(f"unexpected deletion candidate {path}")

    eligible = tuple(
        sorted(
            (path for path, seen in first_seen.items() if seen <= threshold),
            key=deletion_order,
        )
    )
    deleted: list[str] = []
    if apply and eligible:
        if await _required_bytes(webdav, STABLE_POINTER_PATH) != pointer_body:
            raise GCError("stable pointer changed during GC; deleted 0 objects")
        for path in eligible:
            # Re-fence before each deletion. A changed head aborts the remaining
            # sweep; every earlier candidate was proven unreferenced by the head
            # observed immediately before its DELETE.
            if await _required_bytes(webdav, STABLE_POINTER_PATH) != pointer_body:
                raise GCError(f"stable pointer changed during GC after {len(deleted)} deletions")
            await webdav.delete(path, missing_ok=True)
            state.clear_unreferenced(path)
            deleted.append(path)
    return GCResult(
        retained_generations=retained,
        marked_objects=sum(path.startswith("v1/objects/") for path in marked),
        candidates=tuple(sorted(candidates)),
        eligible=eligible,
        deleted=tuple(deleted),
        dry_run=not apply,
    )
