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
    generation_vectors_path,
    ocr_manifest_path,
    ocr_ready_path,
    validate_identifier,
)

from .state import WorkerState
from .webdav import WebDAVClient


class GCError(RuntimeError):
    """Fail-closed remote garbage-collection error."""


class GCPartialFailure(GCError):
    """A bounded terminal error after one or more remote DELETEs succeeded."""

    reason_code = "remote_gc_partial_failure"
    reason = "Remote garbage collection stopped after partial deletion."

    def __init__(self, *, deleted_count: int) -> None:
        if type(deleted_count) is not int or deleted_count < 1:
            raise ValueError("partial GC deleted_count must be a positive integer")
        self.deleted_count = deleted_count
        super().__init__(f"{self.reason_code}: {self.reason} (deleted_count={deleted_count})")


_HEX_PREFIX = re.compile(r"^[0-9a-f]{2}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_INCOMING_TEMP_LEAF = re.compile(r"^[0-9a-f]{32}\.tmp$")
_INCOMING_ROOT = PurePosixPath("v1", ".incoming")
_INCOMING_NAMESPACES = ("channels", "publish")
_KNOWN_UNCOLLECTED_INCOMING_NAMESPACES = ("objects",)


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
    pointer_path: PurePosixPath,
) -> tuple[bytes, tuple[GenerationManifest, ...]]:
    pointer_body = await _required_bytes(webdav, pointer_path)
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
        if manifest.schema_version == "cardrag.generation.v5":
            sidecar = manifest.vector_sidecar
            if sidecar is None or (
                ready.vector_sidecar_sha256 != sidecar.artifact.sha256
                or ready.vector_sidecar_size_bytes != sidecar.artifact.size_bytes
                or sidecar.artifact.path != generation_vectors_path(generation_id).as_posix()
            ):
                raise GCError(f"generation {generation_id} vector control hashes disagree")
        elif (
            manifest.vector_sidecar is not None
            or ready.vector_sidecar_sha256 is not None
            or ready.vector_sidecar_size_bytes is not None
        ):
            raise GCError(f"legacy generation {generation_id} declares a vector sidecar")
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
            {"index.sqlite3", "vectors.f32", "manifest.json", "READY.json"}
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


async def _optional_children(
    webdav: WebDAVClient,
    path: PurePosixPath,
) -> tuple[PurePosixPath, ...]:
    try:
        return await webdav.list_children(path)
    except WebDAVHTTPError as exc:
        if exc.status_code == 404:
            return ()
        raise


def _is_incoming_temp_leaf(path: PurePosixPath | str) -> bool:
    candidate = PurePosixPath(path)
    return bool(
        len(candidate.parts) == 4
        and candidate.parts[:2] == _INCOMING_ROOT.parts
        and candidate.parts[2] in _INCOMING_NAMESPACES
        and _INCOMING_TEMP_LEAF.fullmatch(candidate.name)
    )


async def _incoming_leaf_exists_and_is_safe(
    webdav: WebDAVClient,
    path: PurePosixPath,
) -> bool:
    """Revalidate one exact temp leaf immediately before DELETE.

    A 404 means the publisher already moved or removed the temp object. Any
    descendant returned for the path makes its resource shape ambiguous and
    therefore blocks the sweep.
    """

    if not _is_incoming_temp_leaf(path):
        raise GCError("unsafe incoming temporary deletion candidate")
    try:
        descendants = await webdav.list_children(path)
    except WebDAVHTTPError as exc:
        if exc.status_code == 404:
            return False
        raise
    if descendants:
        raise GCError("incoming temporary deletion candidate is not a leaf")
    return True


async def _list_incoming_temp_leaves(webdav: WebDAVClient) -> tuple[PurePosixPath, ...]:
    """List only publisher-created UUID temp leaves under the closed namespace."""

    namespace_children = await _optional_children(webdav, _INCOMING_ROOT)
    if len(namespace_children) != len(set(namespace_children)):
        raise GCError("duplicate incoming namespace path in PROPFIND")
    collected_roots = {_INCOMING_ROOT / name for name in _INCOMING_NAMESPACES}
    allowed_roots = collected_roots | {
        _INCOMING_ROOT / name for name in _KNOWN_UNCOLLECTED_INCOMING_NAMESPACES
    }
    for path in namespace_children:
        if path.parent != _INCOMING_ROOT or path not in allowed_roots:
            raise GCError("unexpected incoming namespace path")

    leaves: list[PurePosixPath] = []
    for namespace_root in sorted(
        collected_roots.intersection(namespace_children),
        key=lambda item: item.as_posix(),
    ):
        children = await webdav.list_children(namespace_root)
        if len(children) != len(set(children)):
            raise GCError("duplicate incoming temporary path in PROPFIND")
        for path in children:
            if path.parent != namespace_root or not _is_incoming_temp_leaf(path):
                raise GCError("unexpected incoming temporary path")
            if await _incoming_leaf_exists_and_is_safe(webdav, path):
                leaves.append(path)
    return tuple(sorted(leaves, key=lambda item: item.as_posix()))


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
    retain_generations: int = 2,
    grace_days: int = 30,
    now: datetime | None = None,
    pointer_path: PurePosixPath = STABLE_POINTER_PATH,
) -> GCResult:
    if retain_generations < 1 or grace_days < 1:
        raise ValueError("retention and grace must be positive")
    if pointer_path != STABLE_POINTER_PATH:
        raise GCError("remote garbage collection is restricted to the stable channel")
    observed_at = (now or datetime.now(UTC)).astimezone(UTC)

    # Complete every parse/list/hash decision before considering DELETE.
    pointer_body, manifests = await _generation_chain(
        webdav,
        retain=retain_generations,
        pointer_path=pointer_path,
    )
    retained = tuple(manifest.generation_id for manifest in manifests)
    marked: set[str] = {pointer_path.as_posix()}
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
        if manifest.vector_sidecar is not None:
            marked.add(manifest.vector_sidecar.artifact.path)
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
    incoming_temp_leaves = await _list_incoming_temp_leaves(webdav)
    candidates = {
        f"v1/generations/{generation_id}"
        for generation_id in all_generations
        if generation_id not in retained
    }
    candidates.update(path.as_posix() for path in all_objects if path.as_posix() not in marked)
    candidates.update(inactive_caches)
    candidates.update(path.as_posix() for path in incoming_temp_leaves)

    first_seen = {path: state.note_unreferenced(path, observed_at=observed_at) for path in sorted(candidates)}
    state.clear_unreferenced_except(candidates)
    threshold = observed_at - timedelta(days=grace_days)

    def deletion_order(path: str) -> tuple[int, str]:
        if _is_incoming_temp_leaf(path):
            return (0, path)
        if path.startswith("v1/generations/"):
            return (1, path)
        if path.startswith("v1/ocr-cache/"):
            return (2, path)
        if path.startswith("v1/objects/"):
            return (3, path)
        raise GCError(f"unexpected deletion candidate {path}")

    eligible = tuple(
        sorted(
            (path for path, seen in first_seen.items() if seen <= threshold),
            key=deletion_order,
        )
    )
    deleted: list[str] = []
    if apply and eligible:
        if await _required_bytes(webdav, pointer_path) != pointer_body:
            raise GCError("stable pointer changed during GC; deleted 0 objects")
        deletion_failure: Exception | None = None
        for path in eligible:
            try:
                # Re-fence before each deletion. A changed head aborts the
                # remaining sweep; every earlier candidate was proven
                # unreferenced by the head observed immediately before DELETE.
                if await _required_bytes(webdav, pointer_path) != pointer_body:
                    raise GCError("stable pointer changed during GC")
                candidate = PurePosixPath(path)
                if _is_incoming_temp_leaf(candidate) and not await _incoming_leaf_exists_and_is_safe(
                    webdav, candidate
                ):
                    state.clear_unreferenced(path)
                    continue
                await webdav.delete(path, missing_ok=True)
                deleted.append(path)
                state.clear_unreferenced(path)
            except Exception as exc:
                deletion_failure = exc
                break
        if deletion_failure is not None:
            if deleted:
                # Raise outside the source exception handler so raw WebDAV URL,
                # body, or credential text is not retained as implicit context.
                deleted_count = len(deleted)
                deletion_failure = None
                raise GCPartialFailure(deleted_count=deleted_count) from None
            raise deletion_failure
    return GCResult(
        retained_generations=retained,
        marked_objects=sum(path.startswith("v1/objects/") for path in marked),
        candidates=tuple(sorted(candidates)),
        eligible=eligible,
        deleted=tuple(deleted),
        dry_run=not apply,
    )
