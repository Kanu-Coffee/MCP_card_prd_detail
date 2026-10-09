"""Local serving volume publication and head inspection transport."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import shutil
import time
import uuid
from collections.abc import Awaitable, Callable, Coroutine, Iterable, Mapping
from contextlib import suppress
from pathlib import Path, PurePosixPath
from typing import Any

from cardrag_core import (
    GenerationManifest,
    GenerationPointer,
    GenerationReady,
    canonical_json_bytes,
    channel_pointer_path,
    generation_manifest_path,
    generation_ready_path,
    generation_root_path,
    object_path,
    sha256_bytes,
    validate_relative_path,
)

from .async_utils import to_thread_fenced
from .webdav import PublishedBundle, RemoteGenerationIdentity


def _fsync_file(path: Path) -> None:
    with path.open("rb") as source:
        os.fsync(source.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


class LocalServingTransport:
    """Local filesystem publication transport matching WebDAV serving contract."""

    def __init__(self, serving_dir: Path, *, channel: str = "stable") -> None:
        self.serving_dir = serving_dir.resolve()
        self.channel = channel
        self.pointer_path = channel_pointer_path(channel)
        self._full_pointer_path = self.serving_dir / self.pointer_path

    def _resolve_safe_path(self, relative: str | PurePosixPath) -> Path:
        rel = validate_relative_path(relative)
        full = (self.serving_dir / rel).resolve()
        if full != self.serving_dir and self.serving_dir not in full.parents:
            raise RuntimeError(f"artifact path escapes serving directory: {relative}")
        return full

    async def validated_current_generation(
        self, *, force_refresh: bool = False
    ) -> RemoteGenerationIdentity | None:
        return await to_thread_fenced(self._validated_current_generation_sync)

    def _validated_current_generation_sync(self) -> RemoteGenerationIdentity | None:
        if not self._full_pointer_path.exists():
            return None
        try:
            pointer_bytes = self._full_pointer_path.read_bytes()
            pointer = GenerationPointer.model_validate_json(pointer_bytes)
        except Exception:
            return None
        if pointer.canonical_bytes() != pointer_bytes:
            return None

        ready_path = self._resolve_safe_path(generation_ready_path(pointer.generation_id))
        if not ready_path.exists():
            return None
        try:
            ready_bytes = ready_path.read_bytes()
            if sha256_bytes(ready_bytes) != pointer.ready_sha256:
                return None
            ready = GenerationReady.model_validate_json(ready_bytes)
        except Exception:
            return None
        if ready.generation_id != pointer.generation_id or ready.canonical_bytes() != ready_bytes:
            return None
        if ready.manifest_sha256 != pointer.manifest_sha256:
            return None

        manifest_path = self._resolve_safe_path(generation_manifest_path(pointer.generation_id))
        if not manifest_path.exists():
            return None
        try:
            manifest_bytes = manifest_path.read_bytes()
            if sha256_bytes(manifest_bytes) != ready.manifest_sha256:
                return None
            manifest = GenerationManifest.model_validate_json(manifest_bytes)
        except Exception:
            return None
        if manifest.generation_id != pointer.generation_id or manifest.canonical_bytes() != manifest_bytes:
            return None

        database = manifest.serving_database
        if (
            ready.serving_database_sha256 != database.sha256
            or ready.serving_database_size_bytes != database.size_bytes
        ):
            return None
        vector_sidecar = manifest.vector_sidecar
        if manifest.schema_version in {"cardrag.generation.v5", "cardrag.generation.v6"}:
            if vector_sidecar is None:
                return None
            if (
                ready.vector_sidecar_sha256 != vector_sidecar.artifact.sha256
                or ready.vector_sidecar_size_bytes != vector_sidecar.artifact.size_bytes
            ):
                return None

        return RemoteGenerationIdentity(
            generation_id=manifest.generation_id,
            corpus_sha256=manifest.corpus_sha256,
            contract_sha256=manifest.contract_sha256,
            generation_schema=manifest.schema_version,
            serving_schema=manifest.serving_schema,
            ocr_failed_document_count=sum(
                document.availability == "ocr_failed" for document in manifest.documents
            ),
        )

    async def observed_pointer_bytes(self) -> bytes | None:
        return await asyncio.to_thread(self._observed_pointer_bytes_sync)

    def _observed_pointer_bytes_sync(self) -> bytes | None:
        if not self._full_pointer_path.exists():
            return None
        try:
            return self._full_pointer_path.read_bytes()
        except OSError:
            return None

    async def get_bytes(self, path: str | PurePosixPath, *, max_bytes: int | None = None) -> bytes:
        return await asyncio.to_thread(self._get_bytes_sync, path, max_bytes=max_bytes)

    def _get_bytes_sync(self, path: str | PurePosixPath, *, max_bytes: int | None = None) -> bytes:
        full = self._resolve_safe_path(path)
        if not full.is_file():
            raise FileNotFoundError(f"serving file does not exist: {path}")
        size = full.stat().st_size
        if max_bytes is not None and size > max_bytes:
            raise RuntimeError(f"serving file exceeds maximum byte bound: {size} > {max_bytes}")
        return full.read_bytes()

    async def get_json(self, path: str | PurePosixPath) -> dict[str, Any]:
        body = await self.get_bytes(path)
        payload = json.loads(body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"expected JSON object at {path}")
        return payload

    async def publish(
        self,
        *,
        generation_id: str,
        database: Path,
        manifest: Mapping[str, Any],
        vectors: Path | None = None,
        unique_objects: Iterable[tuple[Path, str, str, int]] = (),
        before_pointer_replace: Callable[[], Awaitable[None]] | None = None,
    ) -> PublishedBundle:
        return await to_thread_fenced(
            self._publish_sync,
            generation_id=generation_id,
            database=database,
            manifest=manifest,
            vectors=vectors,
            unique_objects=tuple(unique_objects),
            before_pointer_replace=before_pointer_replace,
        )

    def _publish_sync(
        self,
        *,
        generation_id: str,
        database: Path,
        manifest: Mapping[str, Any],
        vectors: Path | None = None,
        unique_objects: tuple[tuple[Path, str, str, int], ...],
        before_pointer_replace: Callable[[], Awaitable[None]] | None = None,
    ) -> PublishedBundle:
        self.serving_dir.mkdir(parents=True, exist_ok=True)
        # 0. Preflight disk usage check
        needed_bytes = (
            sum(size for _, _, _, size in unique_objects)
            + database.stat().st_size * 2
            + (vectors.stat().st_size * 2 if vectors is not None else 0)
            + 10 * 1024 * 1024
        )
        usage = shutil.disk_usage(self.serving_dir)
        if usage.free < needed_bytes:
            raise RuntimeError(
                f"insufficient disk space on serving volume: {usage.free} bytes free, {needed_bytes} required"
            )

        # 1. Publish referenced CAS objects (PDFs, etc.)
        for local_path, _media_type, declared_sha, declared_size in unique_objects:
            rel = object_path(declared_sha)
            target = self._resolve_safe_path(rel)
            if target.is_file():
                actual_sha, actual_size = _sha256_file(target)
                if actual_sha == declared_sha and actual_size == declared_size:
                    continue
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = target.parent / f".tmp-{target.name}-{uuid.uuid4().hex[:8]}"
            try:
                shutil.copyfile(local_path, temp)
                actual_sha, actual_size = _sha256_file(temp)
                if actual_sha != declared_sha or actual_size != declared_size:
                    raise RuntimeError("CAS object identity mismatch during local publication")
                _fsync_file(temp)
                temp.replace(target)
                _fsync_directory(target.parent)
            finally:
                if temp.exists():
                    with suppress(OSError):
                        temp.unlink()

        # 2. Stage generation directory
        staging_root = self.serving_dir / "staging"
        staging_root.mkdir(parents=True, exist_ok=True)
        staging_dir = staging_root / f"incoming-{generation_id}-{uuid.uuid4().hex[:8]}"
        staging_dir.mkdir(parents=True, exist_ok=False)

        try:
            # Stage database
            staged_db = staging_dir / "index.sqlite3"
            shutil.copyfile(database, staged_db)
            db_sha, db_size = _sha256_file(staged_db)
            _fsync_file(staged_db)

            # Stage vectors
            vector_sha: str | None = None
            vector_size: int | None = None
            if vectors is not None:
                staged_vec = staging_dir / "vectors.f32"
                shutil.copyfile(vectors, staged_vec)
                vector_sha, vector_size = _sha256_file(staged_vec)
                _fsync_file(staged_vec)

            # Stage manifest
            manifest_body = canonical_json_bytes(dict(manifest))
            validated_manifest = GenerationManifest.model_validate_json(manifest_body)
            if validated_manifest.generation_id != generation_id:
                raise ValueError("manifest generation_id does not match target")

            # Validate database matches manifest before proceeding
            if (
                db_sha != validated_manifest.serving_database.sha256
                or db_size != validated_manifest.serving_database.size_bytes
            ):
                raise RuntimeError(
                    f"staged database ({db_sha}, {db_size}B) does not match manifest "
                    f"({validated_manifest.serving_database.sha256}, {validated_manifest.serving_database.size_bytes}B)"
                )

            # Validate vector sidecar matches manifest if specified
            if validated_manifest.vector_sidecar is not None:
                expected_vec_sha = validated_manifest.vector_sidecar.artifact.sha256
                expected_vec_size = validated_manifest.vector_sidecar.artifact.size_bytes
                if vector_sha != expected_vec_sha or vector_size != expected_vec_size:
                    raise RuntimeError("staged vector sidecar does not match manifest")

            manifest_sha = hashlib.sha256(manifest_body).hexdigest()
            staged_manifest = staging_dir / "manifest.json"
            staged_manifest.write_bytes(manifest_body)
            _fsync_file(staged_manifest)

            # Stage READY
            ready = GenerationReady(
                generation_id=generation_id,
                manifest_sha256=manifest_sha,
                serving_database_sha256=db_sha,
                serving_database_size_bytes=db_size,
                vector_sidecar_sha256=vector_sha,
                vector_sidecar_size_bytes=vector_size,
            )
            ready_body = ready.canonical_bytes()
            ready_sha = hashlib.sha256(ready_body).hexdigest()
            staged_ready = staging_dir / "READY.json"
            staged_ready.write_bytes(ready_body)
            _fsync_file(staged_ready)

            _fsync_directory(staging_dir)

            # 3. Atomically move staged generation to final directory (idempotent if identical)
            final_gen_dir = self._resolve_safe_path(generation_root_path(generation_id))
            final_gen_dir.parent.mkdir(parents=True, exist_ok=True)
            if final_gen_dir.exists():
                existing_ready = final_gen_dir / "READY.json"
                if existing_ready.is_file() and existing_ready.read_bytes() == ready_body:
                    # Idempotent reuse: identical generation already finalized
                    pass
                else:
                    raise RuntimeError(
                        f"generation directory {generation_id} already exists with conflicting contents; refusing to overwrite"
                    )
            else:
                staging_dir.replace(final_gen_dir)
                _fsync_directory(final_gen_dir.parent)

            # Record previous generation ID for retention policy
            prev_gen_id: str | None = None
            if self._full_pointer_path.exists():
                with suppress(Exception):
                    cur_ptr = GenerationPointer.model_validate_json(self._full_pointer_path.read_bytes())
                    prev_gen_id = cur_ptr.generation_id

            # 4. Predecessor fence
            if before_pointer_replace is not None:
                res = before_pointer_replace()
                if inspect.isawaitable(res):
                    coro: Coroutine[Any, Any, Any] = res  # type: ignore[assignment]
                    try:
                        loop = asyncio.get_running_loop()
                        future: Any = asyncio.run_coroutine_threadsafe(coro, loop)
                        future.result()
                    except RuntimeError:
                        asyncio.run(coro)

            # 5. Atomic pointer replacement
            pointer = GenerationPointer(
                generation_id=generation_id,
                manifest_sha256=manifest_sha,
                ready_sha256=ready_sha,
            )
            pointer_body = pointer.canonical_bytes()
            pointer_target = self._full_pointer_path
            pointer_target.parent.mkdir(parents=True, exist_ok=True)
            temp_pointer = pointer_target.parent / f".tmp-{uuid.uuid4().hex[:8]}.json"
            try:
                temp_pointer.write_bytes(pointer_body)
                _fsync_file(temp_pointer)
                temp_pointer.replace(pointer_target)
                _fsync_directory(pointer_target.parent)
            finally:
                if temp_pointer.exists():
                    with suppress(OSError):
                        temp_pointer.unlink()

            # 6. Serving volume retention: retain current + previous generation(s)
            generations_root = self.serving_dir / "v1" / "generations"
            if generations_root.is_dir():
                retain_gens = {generation_id}
                if prev_gen_id is not None and prev_gen_id != generation_id:
                    retain_gens.add(prev_gen_id)
                else:
                    # When republishing the same current generation, preserve the newest other generation
                    other_gens = sorted(
                        [p for p in generations_root.iterdir() if p.is_dir() and p.name != generation_id],
                        key=lambda p: p.stat().st_mtime,
                        reverse=True,
                    )
                    if other_gens:
                        retain_gens.add(other_gens[0].name)
                for gen_path in generations_root.iterdir():
                    if gen_path.is_dir() and gen_path.name not in retain_gens:
                        with suppress(OSError):
                            shutil.rmtree(gen_path)

            # Clean up stale staging directories older than 10 minutes
            if staging_root.is_dir():
                now_ts = time.time()
                for old_stage in staging_root.iterdir():
                    if old_stage.is_dir() and old_stage != staging_dir:
                        try:
                            if now_ts - old_stage.stat().st_mtime > 600:
                                shutil.rmtree(old_stage)
                        except OSError:
                            pass

            return PublishedBundle(generation_id, db_sha, manifest_sha)
        finally:
            if staging_dir.exists():
                with suppress(OSError):
                    shutil.rmtree(staging_dir)

    async def close(self) -> None:
        pass
