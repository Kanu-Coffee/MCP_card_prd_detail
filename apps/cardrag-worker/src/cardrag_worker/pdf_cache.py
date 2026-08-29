"""Validated local PDF content-addressed storage and durable source bindings.

The cache is intentionally independent from finite run directories.  Objects
are immutable by SHA-256 identity; SQLite records which discovered issuer
source currently resolves to each object and preserves every superseded byte
revision for audit and reuse.
"""

from __future__ import annotations

import os
import stat
import tempfile
import uuid
from collections.abc import Collection, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .contracts import SourceRecord, canonical_sha256
from .downloader import DownloadedPDF, PDFValidationError, validate_pdf
from .state import PDFSourceRevisionRow, WorkerState


class PDFCacheSecurityError(RuntimeError):
    """A cache or ingestion path contains a symlink or special node."""


class PDFCacheMetadataError(RuntimeError):
    """Durable cache metadata conflicts with validated object bytes."""


class PDFCachePruneError(RuntimeError):
    """A prune failed after a known number of object bytes were removed."""

    def __init__(self, *, deleted_objects: int, deleted_bytes: int) -> None:
        if deleted_objects < 1 or deleted_bytes < 0:
            raise ValueError("partial PDF cache prune counts are invalid")
        self.deleted_objects = deleted_objects
        self.deleted_bytes = deleted_bytes
        super().__init__("pdf_cache_prune_partial_failure: PDF cache pruning failed after partial deletion.")


class _InvalidCacheLeaf(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PDFSourceIdentity:
    source_id: str
    issuer: str
    product_code: str
    document_type: str
    source_url: str
    source_version: str
    source_post_id: str
    discovery_sha256: str

    @classmethod
    def from_source_record(cls, source: SourceRecord) -> PDFSourceIdentity:
        discovery_sha256 = canonical_sha256(source.discovery_payload)
        if source.source_id != f"source_{discovery_sha256}":  # pragma: no cover - contract invariant
            raise ValueError("source record has an inconsistent discovery identity")
        return cls(
            source_id=source.source_id,
            issuer=source.issuer,
            product_code=source.product_code,
            document_type=source.document_type,
            source_url=source.source_url,
            source_version=source.source_version,
            source_post_id=source.source_post_id,
            discovery_sha256=discovery_sha256,
        )


@dataclass(frozen=True, slots=True)
class CachedPDFObject:
    path: Path
    sha256: str
    size_bytes: int
    page_count: int


@dataclass(frozen=True, slots=True)
class PDFCacheHit:
    path: Path
    sha256: str
    size_bytes: int
    page_count: int
    final_url: str
    source_id: str
    etag: str | None
    last_modified: str | None
    origin_checked_at: datetime

    def as_downloaded_pdf(self) -> DownloadedPDF:
        """Adapt a revalidated cache hit to the pipeline's download contract."""

        return DownloadedPDF(
            path=self.path,
            sha256=self.sha256,
            size_bytes=self.size_bytes,
            page_count=self.page_count,
            final_url=self.final_url,
            etag=self.etag,
            last_modified=self.last_modified,
        )


@dataclass(frozen=True, slots=True)
class PDFCachePruneResult:
    scanned_objects: int
    protected_objects: int
    deleted_objects: int
    deleted_bytes: int


def _absolute_without_resolving(path: Path) -> Path:
    # ``abspath`` removes ``..`` components but, unlike ``resolve``, never
    # follows a symlink and therefore cannot hide an unsafe path component.
    return Path(os.path.abspath(os.fspath(path)))


def _components(path: Path) -> tuple[Path, ...]:
    absolute = _absolute_without_resolving(path)
    anchor = Path(absolute.anchor)
    current = anchor
    result = [anchor]
    for part in absolute.parts[1:]:
        current = current / part
        result.append(current)
    return tuple(result)


def _require_secure_directory(path: Path, *, create: bool) -> None:
    components = _components(path)
    for component in components:
        try:
            mode = component.lstat().st_mode
        except FileNotFoundError:
            if not create:
                raise
            break
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise PDFCacheSecurityError(f"cache directory is not a regular non-symlink directory: {path}")
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        for component in components:
            mode = component.lstat().st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                raise PDFCacheSecurityError(f"cache directory is not a regular non-symlink directory: {path}")


def _open_regular(path: Path, *, cache_leaf: bool) -> int:
    try:
        _require_secure_directory(path.parent, create=False)
    except FileNotFoundError:
        if cache_leaf:
            raise _InvalidCacheLeaf("cache object parent is missing") from None
        raise PDFCacheSecurityError("ingestion source parent does not exist") from None
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        if cache_leaf:
            raise _InvalidCacheLeaf("cache object is missing") from None
        raise
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        if cache_leaf:
            raise _InvalidCacheLeaf("cache object is not a regular non-symlink file")
        raise PDFCacheSecurityError("ingestion source must be a regular non-symlink file")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        if cache_leaf:
            raise _InvalidCacheLeaf("cache object cannot be opened safely") from exc
        raise PDFCacheSecurityError("ingestion source cannot be opened safely") from exc
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        if cache_leaf:
            raise _InvalidCacheLeaf("cache object changed to a special node")
        raise PDFCacheSecurityError("ingestion source changed to a special node")
    return descriptor


def _validate_descriptor(
    descriptor: int,
    *,
    expected_sha256: str | None = None,
) -> tuple[str, int, int]:
    before = os.fstat(descriptor)
    result = validate_pdf(Path(f"/proc/self/fd/{descriptor}"), expected_sha256=expected_sha256)
    after = os.fstat(descriptor)
    before_identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
    after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
    if before_identity != after_identity:
        raise PDFValidationError("PDF changed while it was being validated")
    return result


def _validate_regular_pdf(
    path: Path,
    *,
    expected_sha256: str | None = None,
    cache_leaf: bool,
) -> tuple[str, int, int]:
    descriptor = _open_regular(path, cache_leaf=cache_leaf)
    try:
        return _validate_descriptor(descriptor, expected_sha256=expected_sha256)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class PDFCache:
    """Local PDF CAS coupled to a :class:`WorkerState` metadata dictionary."""

    def __init__(self, state_dir: Path, state: WorkerState) -> None:
        self.state_dir = _absolute_without_resolving(state_dir)
        self.root = self.state_dir / "pdf-cache"
        self.objects_root = self.root / "objects" / "sha256"
        self.incoming_root = self.root / ".incoming"
        self.state = state
        _require_secure_directory(self.objects_root, create=True)
        _require_secure_directory(self.incoming_root, create=True)

    def object_path(self, pdf_sha256: str) -> Path:
        if len(pdf_sha256) != 64 or any(character not in "0123456789abcdef" for character in pdf_sha256):
            raise ValueError("PDF cache sha256 is invalid")
        return self.objects_root / pdf_sha256[:2] / pdf_sha256

    @staticmethod
    def relative_object_path(pdf_sha256: str) -> str:
        if len(pdf_sha256) != 64 or any(character not in "0123456789abcdef" for character in pdf_sha256):
            raise ValueError("PDF cache sha256 is invalid")
        return f"objects/sha256/{pdf_sha256[:2]}/{pdf_sha256}"

    @contextmanager
    def temporary_download_path(self) -> Iterator[Path]:
        """Yield an absent, cache-owned path suitable for ``download(..., destination)``."""

        _require_secure_directory(self.incoming_root, create=False)
        destination = self.incoming_root / f"download-{uuid.uuid4().hex}.pdf"
        try:
            yield destination
        finally:
            try:
                mode = destination.lstat().st_mode
            except FileNotFoundError:
                pass
            else:
                # Unlinking a leaf never follows a symlink.  A directory is
                # deliberately retained for inspection instead of recursively
                # deleting an unexpected tree.
                if not stat.S_ISDIR(mode):
                    destination.unlink(missing_ok=True)

    def _validated_cache_object(self, pdf_sha256: str) -> CachedPDFObject:
        path = self.object_path(pdf_sha256)
        try:
            digest, size_bytes, page_count = _validate_regular_pdf(
                path,
                expected_sha256=pdf_sha256,
                cache_leaf=True,
            )
        except (OSError, PDFValidationError, _InvalidCacheLeaf) as exc:
            raise _InvalidCacheLeaf("cache object failed PDF validation") from exc
        return CachedPDFObject(path, digest, size_bytes, page_count)

    def ingest(
        self,
        source_path: Path,
        *,
        expected_sha256: str | None = None,
        expected_size_bytes: int | None = None,
        expected_page_count: int | None = None,
    ) -> CachedPDFObject:
        """Validate and atomically ingest one PDF into its immutable CAS path."""

        source_path = _absolute_without_resolving(source_path)
        source_descriptor = _open_regular(source_path, cache_leaf=False)
        try:
            digest, size_bytes, page_count = _validate_descriptor(
                source_descriptor,
                expected_sha256=expected_sha256,
            )
            if expected_size_bytes is not None and size_bytes != expected_size_bytes:
                raise PDFValidationError("PDF size differs from the expected size")
            if expected_page_count is not None and page_count != expected_page_count:
                raise PDFValidationError("PDF page count differs from the expected count")

            target = self.object_path(digest)
            _require_secure_directory(target.parent, create=True)
            try:
                current = self._validated_cache_object(digest)
            except _InvalidCacheLeaf:
                current = None
            if current is not None:
                self.state.record_pdf_cache_object(
                    pdf_sha256=digest,
                    size_bytes=size_bytes,
                    page_count=page_count,
                    relative_path=self.relative_object_path(digest),
                )
                return current

            temporary_descriptor, temporary_name = tempfile.mkstemp(
                prefix=".ingest-",
                suffix=".pdf",
                dir=target.parent,
            )
            temporary = Path(temporary_name)
            try:
                os.fchmod(temporary_descriptor, 0o600)
                os.lseek(source_descriptor, 0, os.SEEK_SET)
                while block := os.read(source_descriptor, 1024 * 1024):
                    view = memoryview(block)
                    while view:
                        written = os.write(temporary_descriptor, view)
                        view = view[written:]
                os.fsync(temporary_descriptor)
                os.close(temporary_descriptor)
                temporary_descriptor = -1
                copied_digest, copied_size, copied_pages = _validate_regular_pdf(
                    temporary,
                    expected_sha256=digest,
                    cache_leaf=False,
                )
                if (copied_digest, copied_size, copied_pages) != (digest, size_bytes, page_count):
                    raise PDFValidationError("PDF changed while it was copied into cache")

                # Another process may have won the same digest race.  Reuse a
                # fully validated winner; otherwise replace only the leaf with
                # our already-fsynced regular file.
                try:
                    winner = self._validated_cache_object(digest)
                except _InvalidCacheLeaf:
                    winner = None
                if winner is None:
                    try:
                        target_mode = target.lstat().st_mode
                    except FileNotFoundError:
                        target_mode = None
                    if target_mode is not None and stat.S_ISDIR(target_mode):
                        raise PDFCacheSecurityError("cache object leaf cannot be a directory")
                    os.replace(temporary, target)
                    _fsync_directory(target.parent)
                    winner = self._validated_cache_object(digest)
                if (winner.size_bytes, winner.page_count) != (size_bytes, page_count):
                    raise PDFCacheMetadataError("validated CAS object metadata changed during ingestion")
            finally:
                if temporary_descriptor >= 0:
                    os.close(temporary_descriptor)
                temporary.unlink(missing_ok=True)

            self.state.record_pdf_cache_object(
                pdf_sha256=digest,
                size_bytes=size_bytes,
                page_count=page_count,
                relative_path=self.relative_object_path(digest),
            )
            return winner
        finally:
            os.close(source_descriptor)

    def bind(
        self,
        identity: PDFSourceIdentity,
        cached: CachedPDFObject,
        *,
        final_url: str,
        etag: str | None = None,
        last_modified: str | None = None,
        replace_validators: bool = False,
        observed_at: datetime | None = None,
        verified_at: datetime | None = None,
    ) -> PDFCacheHit:
        """Bind validated CAS bytes to a discovery identity and preserve history."""

        validated = self._validated_cache_object(cached.sha256)
        if (
            cached.path != validated.path
            or cached.size_bytes != validated.size_bytes
            or cached.page_count != validated.page_count
        ):
            raise PDFCacheMetadataError("cache binding input does not match its validated CAS object")
        self.state.record_pdf_cache_object(
            pdf_sha256=validated.sha256,
            size_bytes=validated.size_bytes,
            page_count=validated.page_count,
            relative_path=self.relative_object_path(validated.sha256),
            verified_at=verified_at,
        )
        binding = self.state.bind_pdf_cache_source(
            source_id=identity.source_id,
            issuer=identity.issuer,
            product_code=identity.product_code,
            document_type=identity.document_type,
            source_url=identity.source_url,
            source_version=identity.source_version,
            source_post_id=identity.source_post_id,
            discovery_sha256=identity.discovery_sha256,
            pdf_sha256=validated.sha256,
            pdf_size_bytes=validated.size_bytes,
            page_count=validated.page_count,
            final_url=final_url,
            etag=etag,
            last_modified=last_modified,
            replace_validators=replace_validators,
            observed_at=observed_at,
            verified_at=verified_at,
        )
        return self._hit(validated.path, binding)

    def ingest_and_bind(
        self,
        identity: PDFSourceIdentity,
        source_path: Path,
        *,
        final_url: str,
        expected_sha256: str | None = None,
        expected_size_bytes: int | None = None,
        expected_page_count: int | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
        replace_validators: bool = False,
        observed_at: datetime | None = None,
        verified_at: datetime | None = None,
    ) -> PDFCacheHit:
        cached = self.ingest(
            source_path,
            expected_sha256=expected_sha256,
            expected_size_bytes=expected_size_bytes,
            expected_page_count=expected_page_count,
        )
        return self.bind(
            identity,
            cached,
            final_url=final_url,
            etag=etag,
            last_modified=last_modified,
            replace_validators=replace_validators,
            observed_at=observed_at,
            verified_at=verified_at,
        )

    def ingest_download(
        self,
        identity: PDFSourceIdentity,
        downloaded: DownloadedPDF,
        *,
        observed_at: datetime | None = None,
        verified_at: datetime | None = None,
    ) -> PDFCacheHit:
        """Atomically adopt a downloader result and persist all response metadata."""

        return self.ingest_and_bind(
            identity,
            downloaded.path,
            final_url=downloaded.final_url,
            expected_sha256=downloaded.sha256,
            expected_size_bytes=downloaded.size_bytes,
            expected_page_count=downloaded.page_count,
            etag=downloaded.etag,
            last_modified=downloaded.last_modified,
            replace_validators=True,
            observed_at=observed_at,
            verified_at=verified_at,
        )

    def observe_not_modified(
        self,
        identity: PDFSourceIdentity,
        cached: PDFCacheHit,
        *,
        final_url: str,
        etag: str | None = None,
        last_modified: str | None = None,
        observed_at: datetime | None = None,
        verified_at: datetime | None = None,
    ) -> PDFCacheHit:
        """Record a successful conditional origin check without creating a revision."""

        if cached.source_id != identity.source_id:
            raise PDFCacheMetadataError("not-modified observation belongs to a different source")
        validated = self._validated_cache_object(cached.sha256)
        if (
            validated.path != cached.path
            or validated.size_bytes != cached.size_bytes
            or validated.page_count != cached.page_count
        ):
            raise PDFCacheMetadataError("not-modified cache input no longer matches CAS bytes")
        return self.bind(
            identity,
            validated,
            final_url=final_url,
            etag=etag,
            last_modified=last_modified,
            observed_at=observed_at,
            verified_at=verified_at,
        )

    def prune(self, protected_sha256s: Collection[str]) -> PDFCachePruneResult:
        """Delete unreferenced object bytes after a full fail-closed tree preflight.

        Durable object/source/revision rows are deliberately retained.  A later
        lookup therefore becomes a safe miss and can repopulate the same CAS
        identity without losing its audit history.
        """

        protected = set(protected_sha256s)
        for digest in protected:
            if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
                raise ValueError("protected PDF cache sha256 is invalid")

        _require_secure_directory(self.objects_root, create=False)
        candidates: list[tuple[Path, int, int, int]] = []
        seen: set[str] = set()
        for prefix in sorted(self.objects_root.iterdir(), key=lambda path: path.name):
            prefix_mode = prefix.lstat().st_mode
            if (
                len(prefix.name) != 2
                or any(character not in "0123456789abcdef" for character in prefix.name)
                or stat.S_ISLNK(prefix_mode)
                or not stat.S_ISDIR(prefix_mode)
            ):
                raise PDFCacheSecurityError("PDF cache object tree contains an unsafe prefix node")
            for leaf in sorted(prefix.iterdir(), key=lambda path: path.name):
                leaf_stat = leaf.lstat()
                digest = leaf.name
                if (
                    len(digest) != 64
                    or any(character not in "0123456789abcdef" for character in digest)
                    or digest[:2] != prefix.name
                    or digest in seen
                    or stat.S_ISLNK(leaf_stat.st_mode)
                    or not stat.S_ISREG(leaf_stat.st_mode)
                ):
                    raise PDFCacheSecurityError("PDF cache object tree contains an unsafe object node")
                seen.add(digest)
                candidates.append((leaf, leaf_stat.st_dev, leaf_stat.st_ino, leaf_stat.st_size))

        deleted_objects = 0
        deleted_bytes = 0
        protected_objects = 0
        touched_directories: set[Path] = set()
        try:
            for leaf, device, inode, size_bytes in candidates:
                if leaf.name in protected:
                    protected_objects += 1
                    continue
                current = leaf.lstat()
                if (
                    stat.S_ISLNK(current.st_mode)
                    or not stat.S_ISREG(current.st_mode)
                    or (current.st_dev, current.st_ino, current.st_size) != (device, inode, size_bytes)
                ):
                    raise PDFCacheSecurityError("PDF cache object changed during prune")
                leaf.unlink()
                touched_directories.add(leaf.parent)
                deleted_objects += 1
                deleted_bytes += size_bytes
            for directory in sorted(touched_directories, key=os.fspath):
                _fsync_directory(directory)
            if touched_directories:
                _fsync_directory(self.objects_root)
        except Exception:
            if deleted_objects:
                raise PDFCachePruneError(
                    deleted_objects=deleted_objects,
                    deleted_bytes=deleted_bytes,
                ) from None
            raise
        return PDFCachePruneResult(
            scanned_objects=len(candidates),
            protected_objects=protected_objects,
            deleted_objects=deleted_objects,
            deleted_bytes=deleted_bytes,
        )

    def lookup(self, source: PDFSourceIdentity | str) -> PDFCacheHit | None:
        """Return a fully revalidated hit, or a safe miss for absent/corrupt bytes."""

        source_id = source.source_id if isinstance(source, PDFSourceIdentity) else source
        binding = self.state.pdf_cache_source_binding(source_id)
        if binding is None:
            return None
        if isinstance(source, PDFSourceIdentity):
            durable_identity = (
                binding.source_id,
                binding.issuer,
                binding.product_code,
                binding.document_type,
                binding.source_url,
                binding.source_version,
                binding.source_post_id,
                binding.discovery_sha256,
            )
            requested_identity = (
                source.source_id,
                source.issuer,
                source.product_code,
                source.document_type,
                source.source_url,
                source.source_version,
                source.source_post_id,
                source.discovery_sha256,
            )
            if durable_identity != requested_identity:
                raise PDFCacheMetadataError("source identity conflicts with its durable cache binding")
        if binding.relative_path != self.relative_object_path(binding.pdf_sha256):
            raise PDFCacheMetadataError("durable PDF object path is not content-addressed")
        try:
            cached = self._validated_cache_object(binding.pdf_sha256)
        except _InvalidCacheLeaf:
            return None
        if (cached.size_bytes, cached.page_count) != (binding.pdf_size_bytes, binding.page_count):
            return None
        if not self.state.mark_pdf_cache_verified(
            source_id=source_id,
            pdf_sha256=binding.pdf_sha256,
        ):
            return None
        return self._hit(cached.path, binding)

    def lookup_revision(self, revision: PDFSourceRevisionRow) -> PDFCacheHit | None:
        """Revalidate one immutable historical revision without rebinding history."""

        if revision.relative_path != self.relative_object_path(revision.pdf_sha256):
            raise PDFCacheMetadataError("historical PDF object path is not content-addressed")
        try:
            cached = self._validated_cache_object(revision.pdf_sha256)
        except _InvalidCacheLeaf:
            return None
        if (cached.size_bytes, cached.page_count) != (
            revision.pdf_size_bytes,
            revision.page_count,
        ):
            return None
        return self._hit(cached.path, revision)

    @staticmethod
    def _hit(path: Path, binding: PDFSourceRevisionRow) -> PDFCacheHit:
        return PDFCacheHit(
            path=path,
            sha256=binding.pdf_sha256,
            size_bytes=binding.pdf_size_bytes,
            page_count=binding.page_count,
            final_url=binding.final_url,
            source_id=binding.source_id,
            etag=binding.etag,
            last_modified=binding.last_modified,
            origin_checked_at=datetime.fromisoformat(binding.revision_last_observed_at),
        )
