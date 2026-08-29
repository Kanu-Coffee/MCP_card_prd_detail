from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from helpers import pdf_bytes

from cardrag_worker.contracts import SourceRecord
from cardrag_worker.downloader import DownloadedPDF, PDFValidationError
from cardrag_worker.pdf_cache import (
    PDFCache,
    PDFCachePruneError,
    PDFCacheSecurityError,
    PDFSourceIdentity,
)
from cardrag_worker.pipeline import WorkerPipeline
from cardrag_worker.settings import WorkerSettings
from cardrag_worker.state import WorkerState


def source_record(
    *,
    product_code: str = "CARD-1",
    source_version: str = "1",
    source_url: str = "https://cards.example/guides/card-1.pdf",
    source_post_id: str = "post-1",
) -> SourceRecord:
    return SourceRecord(
        issuer="issuer",
        product_code=product_code,
        product_name="Test Card",
        effective_date=date(2026, 1, 1),
        source_version=source_version,
        source_url=source_url,
        source_post_id=source_post_id,
        file_name="guide.pdf",
        category="credit",
        discovered_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def write_pdf(path: Path, *, width: float = 612, pages: int = 1) -> tuple[str, bytes]:
    body = pdf_bytes(width=width, pages=pages)
    path.write_bytes(body)
    return hashlib.sha256(body).hexdigest(), body


def test_cache_hit_revalidates_and_exposes_source_validators(tmp_path: Path) -> None:
    source_path = tmp_path / "download.pdf"
    digest, body = write_pdf(source_path, pages=2)
    source = source_record()
    identity = PDFSourceIdentity.from_source_record(source)

    with WorkerState(tmp_path / "worker-state.sqlite3") as state:
        cache = PDFCache(tmp_path, state)
        hit = cache.ingest_download(
            identity,
            DownloadedPDF(
                path=source_path,
                sha256=digest,
                size_bytes=len(body),
                page_count=2,
                final_url="https://cdn.cards.example/current.pdf",
                etag='"revision-1"',
                last_modified="Wed, 01 Jan 2026 00:00:00 GMT",
            ),
        )

        assert hit.path == tmp_path / "pdf-cache" / "objects" / "sha256" / digest[:2] / digest
        assert hit.path.read_bytes() == body
        assert (hit.size_bytes, hit.page_count, hit.etag) == (len(body), 2, '"revision-1"')
        assert hit.as_downloaded_pdf().etag == '"revision-1"'
        assert hit.origin_checked_at.tzinfo is not None
        reused = cache.lookup(identity)
        assert reused == hit
        binding = state.pdf_cache_source_binding(source.source_id)
        assert binding is not None
        assert binding.discovery_sha256 == source.source_id.removeprefix("source_")
        assert binding.verified_at <= state.pdf_cache_object(digest).last_verified_at  # type: ignore[union-attr]


def test_not_modified_observation_refreshes_origin_time_without_a_revision(tmp_path: Path) -> None:
    source_path = tmp_path / "download.pdf"
    digest, body = write_pdf(source_path)
    source = source_record()
    identity = PDFSourceIdentity.from_source_record(source)
    first_seen = datetime(2026, 1, 1, tzinfo=UTC)
    refreshed = first_seen + timedelta(days=7)

    with WorkerState(tmp_path / "worker-state.sqlite3") as state:
        cache = PDFCache(tmp_path, state)
        initial = cache.ingest_download(
            identity,
            DownloadedPDF(
                source_path,
                digest,
                len(body),
                1,
                source.source_url,
                '"revision-1"',
                "Wed, 01 Jan 2026 00:00:00 GMT",
            ),
            observed_at=first_seen,
            verified_at=first_seen,
        )
        observed = cache.observe_not_modified(
            identity,
            initial,
            final_url=source.source_url,
            observed_at=refreshed,
            verified_at=refreshed,
        )

        assert observed.origin_checked_at == refreshed
        assert observed.etag == '"revision-1"'
        history = state.pdf_cache_source_history(source.source_id)
        assert len(history) == 1
        assert history[0].revision_last_observed_at == refreshed.isoformat()


def test_successful_download_replaces_disappeared_origin_validators(tmp_path: Path) -> None:
    source_path = tmp_path / "download.pdf"
    digest, body = write_pdf(source_path)
    identity = PDFSourceIdentity.from_source_record(source_record())

    with WorkerState(tmp_path / "worker-state.sqlite3") as state:
        cache = PDFCache(tmp_path, state)
        cache.ingest_download(
            identity,
            DownloadedPDF(
                source_path,
                digest,
                len(body),
                1,
                identity.source_url,
                '"revision-1"',
                "Wed, 01 Jan 2026 00:00:00 GMT",
            ),
        )
        refreshed = cache.ingest_download(
            identity,
            DownloadedPDF(source_path, digest, len(body), 1, identity.source_url),
        )

        assert refreshed.etag is None
        assert refreshed.last_modified is None
        assert len(state.pdf_cache_source_history(identity.source_id)) == 1


def test_cache_deduplicates_identical_bytes_across_sources_and_ingestions(tmp_path: Path) -> None:
    first_path = tmp_path / "first.pdf"
    second_path = tmp_path / "second.pdf"
    digest, body = write_pdf(first_path)
    second_path.write_bytes(body)

    with WorkerState(tmp_path / "worker-state.sqlite3") as state:
        cache = PDFCache(tmp_path, state)
        first = cache.ingest_and_bind(
            PDFSourceIdentity.from_source_record(source_record()),
            first_path,
            final_url="https://cards.example/guides/card-1.pdf",
        )
        inode = first.path.stat().st_ino
        second = cache.ingest_and_bind(
            PDFSourceIdentity.from_source_record(
                source_record(
                    product_code="CARD-2",
                    source_url="https://cards.example/guides/card-2.pdf",
                    source_post_id="post-2",
                )
            ),
            second_path,
            final_url="https://cards.example/guides/card-2.pdf",
        )

        assert second.sha256 == digest
        assert second.path == first.path
        assert second.path.stat().st_ino == inode
        assert state.connection.execute("SELECT count(*) FROM pdf_cache_object").fetchone()[0] == 1
        object_files = [path for path in cache.objects_root.rglob("*") if path.is_file()]
        assert object_files == [first.path]


def test_same_url_changed_bytes_creates_revision_history_idempotently(tmp_path: Path) -> None:
    source = source_record()
    identity = PDFSourceIdentity.from_source_record(source)
    source_path = tmp_path / "download.pdf"
    first_digest, _ = write_pdf(source_path, width=612)
    first_seen = datetime(2026, 1, 1, tzinfo=UTC)
    second_seen = first_seen + timedelta(days=1)

    with WorkerState(tmp_path / "worker-state.sqlite3") as state:
        cache = PDFCache(tmp_path, state)
        first_object = cache.ingest(source_path)
        cache.bind(
            identity,
            first_object,
            final_url=source.source_url,
            etag='"one"',
            observed_at=first_seen,
            verified_at=first_seen,
        )
        # Rebinding the same bytes is an observation update, not a new revision.
        cache.bind(
            identity,
            first_object,
            final_url=source.source_url,
            observed_at=first_seen + timedelta(hours=1),
            verified_at=first_seen + timedelta(hours=1),
        )
        assert len(state.pdf_cache_source_history(source.source_id)) == 1

        second_digest, _ = write_pdf(source_path, width=613)
        second_object = cache.ingest(source_path)
        cache.bind(
            identity,
            second_object,
            final_url=source.source_url,
            etag='"two"',
            observed_at=second_seen,
            verified_at=second_seen,
        )

        history = state.pdf_cache_source_history(source.source_id)
        assert [row.pdf_sha256 for row in history] == [first_digest, second_digest]
        assert history[0].superseded_at == second_seen.isoformat()
        assert history[1].previous_revision_id == history[0].revision_id
        assert history[1].superseded_at is None
        assert cache.lookup(identity).sha256 == second_digest  # type: ignore[union-attr]


def test_new_discovery_identity_supersedes_prior_product_source(tmp_path: Path) -> None:
    source_path = tmp_path / "download.pdf"
    write_pdf(source_path)
    old = source_record()
    renewed = source_record(
        source_version="2",
        source_url="https://cards.example/guides/card-1-v2.pdf",
        source_post_id="post-2",
    )

    with WorkerState(tmp_path / "worker-state.sqlite3") as state:
        cache = PDFCache(tmp_path, state)
        cached = cache.ingest(source_path)
        cache.bind(
            PDFSourceIdentity.from_source_record(old),
            cached,
            final_url=old.source_url,
        )
        cache.bind(
            PDFSourceIdentity.from_source_record(renewed),
            cached,
            final_url=renewed.source_url,
        )

        old_binding = state.pdf_cache_source_binding(old.source_id)
        renewed_binding = state.pdf_cache_source_binding(renewed.source_id)
        assert old_binding is not None and renewed_binding is not None
        assert old_binding.superseded_by_source_id == renewed.source_id
        assert old_binding.source_superseded_at is not None
        assert renewed_binding.superseded_by_source_id is None


@pytest.mark.parametrize("remove", [False, True])
def test_corrupt_or_missing_object_is_a_safe_miss_and_can_be_repaired(
    tmp_path: Path,
    remove: bool,
) -> None:
    source_path = tmp_path / "download.pdf"
    digest, original = write_pdf(source_path, width=612)
    identity = PDFSourceIdentity.from_source_record(source_record())

    with WorkerState(tmp_path / "worker-state.sqlite3") as state:
        cache = PDFCache(tmp_path, state)
        hit = cache.ingest_and_bind(identity, source_path, final_url=identity.source_url)
        if remove:
            hit.path.unlink()
        else:
            hit.path.write_bytes(pdf_bytes(width=700))
        assert cache.lookup(identity) is None

        source_path.write_bytes(original)
        repaired = cache.ingest_and_bind(
            identity,
            source_path,
            final_url=identity.source_url,
            expected_sha256=digest,
        )
        assert repaired.sha256 == digest
        assert cache.lookup(identity) is not None


def test_ingestion_rejects_symlink_and_lookup_never_follows_cache_symlink(tmp_path: Path) -> None:
    real_source = tmp_path / "real.pdf"
    write_pdf(real_source)
    source_link = tmp_path / "source-link.pdf"
    source_link.symlink_to(real_source)

    with WorkerState(tmp_path / "worker-state.sqlite3") as state:
        cache = PDFCache(tmp_path, state)
        with pytest.raises(PDFCacheSecurityError, match="non-symlink"):
            cache.ingest(source_link)

        identity = PDFSourceIdentity.from_source_record(source_record())
        hit = cache.ingest_and_bind(identity, real_source, final_url=identity.source_url)
        outside = tmp_path / "outside.pdf"
        outside_body = pdf_bytes(width=700)
        outside.write_bytes(outside_body)
        hit.path.unlink()
        hit.path.symlink_to(outside)
        assert cache.lookup(identity) is None
        assert outside.read_bytes() == outside_body


def test_ingestion_is_recoverable_when_metadata_write_fails_and_cleans_temporaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = tmp_path / "download.pdf"
    digest, _ = write_pdf(source_path)

    with WorkerState(tmp_path / "worker-state.sqlite3") as state:
        cache = PDFCache(tmp_path, state)
        original_record = state.record_pdf_cache_object
        calls = 0

        def fail_once(**kwargs: object) -> object:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("simulated state failure")
            return original_record(**kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(state, "record_pdf_cache_object", fail_once)
        with pytest.raises(RuntimeError, match="simulated state failure"):
            cache.ingest(source_path)
        assert cache.object_path(digest).is_file()
        assert state.pdf_cache_object(digest) is None

        cached = cache.ingest(source_path)
        assert cached.sha256 == digest
        assert state.pdf_cache_object(digest) is not None
        assert not list(cache.objects_root.rglob(".ingest-*.pdf"))


def test_invalid_pdf_is_never_published_and_managed_download_path_is_cleaned(tmp_path: Path) -> None:
    with WorkerState(tmp_path / "worker-state.sqlite3") as state:
        cache = PDFCache(tmp_path, state)
        with cache.temporary_download_path() as destination:
            assert not destination.exists()
            destination.write_bytes(b"not a PDF")
            with pytest.raises(PDFValidationError, match="PDF signature"):
                cache.ingest(destination)
        assert not destination.exists()
        assert state.connection.execute("SELECT count(*) FROM pdf_cache_object").fetchone()[0] == 0
        assert not [path for path in cache.objects_root.rglob("*") if path.is_file()]


def test_prune_removes_only_unprotected_bytes_and_preserves_revision_history(tmp_path: Path) -> None:
    source_path = tmp_path / "download.pdf"
    identity = PDFSourceIdentity.from_source_record(source_record())
    first_digest, _ = write_pdf(source_path, width=612)

    with WorkerState(tmp_path / "worker-state.sqlite3") as state:
        cache = PDFCache(tmp_path, state)
        first = cache.ingest_and_bind(identity, source_path, final_url=identity.source_url)
        second_digest, _ = write_pdf(source_path, width=613)
        second = cache.ingest_and_bind(identity, source_path, final_url=identity.source_url)

        result = cache.prune({second_digest})

        assert result.scanned_objects == 2
        assert result.protected_objects == 1
        assert result.deleted_objects == 1
        assert result.deleted_bytes == first.size_bytes
        assert not cache.object_path(first_digest).exists()
        assert second.path.is_file()
        assert [row.pdf_sha256 for row in state.pdf_cache_source_history(identity.source_id)] == [
            first_digest,
            second_digest,
        ]
        assert cache.lookup(identity) is not None
        assert cache.prune({second_digest}).deleted_objects == 0


def test_pruned_current_binding_becomes_a_miss_and_repopulates_without_losing_history(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "download.pdf"
    digest, _ = write_pdf(source_path)
    identity = PDFSourceIdentity.from_source_record(source_record())

    with WorkerState(tmp_path / "worker-state.sqlite3") as state:
        cache = PDFCache(tmp_path, state)
        cache.ingest_and_bind(identity, source_path, final_url=identity.source_url)
        assert cache.prune(set()).deleted_objects == 1
        assert cache.lookup(identity) is None
        assert [row.pdf_sha256 for row in state.pdf_cache_source_history(identity.source_id)] == [digest]

        repaired = cache.ingest_and_bind(identity, source_path, final_url=identity.source_url)

        assert repaired.sha256 == digest
        assert cache.lookup(identity) is not None
        assert [row.pdf_sha256 for row in state.pdf_cache_source_history(identity.source_id)] == [digest]


@pytest.mark.parametrize("unsafe_kind", ["prefix-symlink", "leaf-symlink", "leaf-directory"])
def test_prune_preflight_rejects_unsafe_tree_without_deleting_any_object(
    tmp_path: Path,
    unsafe_kind: str,
) -> None:
    first_path = tmp_path / "first.pdf"
    second_path = tmp_path / "second.pdf"
    first_digest, _ = write_pdf(first_path, width=612)
    second_digest, _ = write_pdf(second_path, width=613)

    with WorkerState(tmp_path / "worker-state.sqlite3") as state:
        cache = PDFCache(tmp_path, state)
        first = cache.ingest(first_path)
        second = cache.ingest(second_path)
        outside = tmp_path / "outside"
        outside.mkdir()
        if unsafe_kind == "prefix-symlink":
            (cache.objects_root / "ff").symlink_to(outside, target_is_directory=True)
        elif unsafe_kind == "leaf-symlink":
            (cache.objects_root / first_digest[:2] / (first_digest[:2] + "0" * 62)).symlink_to(first.path)
        else:
            (cache.objects_root / first_digest[:2] / (first_digest[:2] + "0" * 62)).mkdir()

        with pytest.raises(PDFCacheSecurityError, match="unsafe"):
            cache.prune(set())

        assert first.path.is_file()
        assert second.path.is_file()


def test_prune_rejects_invalid_protected_identity(tmp_path: Path) -> None:
    with WorkerState(tmp_path / "worker-state.sqlite3") as state:
        cache = PDFCache(tmp_path, state)
        with pytest.raises(ValueError, match="protected PDF cache sha256"):
            cache.prune({"../outside"})


def test_prune_reports_known_deleted_bytes_when_post_unlink_fsync_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = tmp_path / "download.pdf"
    _, body = write_pdf(source_path)

    with WorkerState(tmp_path / "worker-state.sqlite3") as state:
        cache = PDFCache(tmp_path, state)
        cached = cache.ingest(source_path)

        def fail_fsync(_path: Path) -> None:
            raise OSError("RAW_FSYNC_PATH_SECRET")

        monkeypatch.setattr("cardrag_worker.pdf_cache._fsync_directory", fail_fsync)
        with pytest.raises(PDFCachePruneError) as captured:
            cache.prune(set())

        assert captured.value.deleted_objects == 1
        assert captured.value.deleted_bytes == len(body)
        assert not cached.path.exists()
        assert "RAW_FSYNC_PATH_SECRET" not in str(captured.value)


def test_pdf_cache_refresh_setting_defaults_and_rejects_nonpositive_or_nonfinite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CARDRAG_PDF_CACHE_REFRESH_HOURS", raising=False)
    assert WorkerSettings.from_env().pdf_cache_refresh_hours == 168

    monkeypatch.setenv("CARDRAG_PDF_CACHE_REFRESH_HOURS", "12.5")
    assert WorkerSettings.from_env().pdf_cache_refresh_hours == 12.5

    for invalid in ("0", "-1", "nan", "inf", "-inf"):
        monkeypatch.setenv("CARDRAG_PDF_CACHE_REFRESH_HOURS", invalid)
        with pytest.raises(ValueError, match="CARDRAG_PDF_CACHE_REFRESH_HOURS"):
            WorkerSettings.from_env()


@pytest.mark.parametrize("invalid", [0, -1, float("nan"), float("inf"), 1e300])
def test_pipeline_rejects_unsafe_pdf_cache_refresh_interval(
    tmp_path: Path,
    invalid: float,
) -> None:
    with pytest.raises(ValueError, match="PDF cache refresh hours"):
        WorkerPipeline(
            state=None,  # type: ignore[arg-type]
            state_dir=tmp_path,
            adapters=(object(),),  # type: ignore[arg-type]
            ocr=None,  # type: ignore[arg-type]
            embeddings=SimpleNamespace(dimension=1536),  # type: ignore[arg-type]
            webdav=None,  # type: ignore[arg-type]
            pdf_cache_refresh_hours=invalid,
        )
