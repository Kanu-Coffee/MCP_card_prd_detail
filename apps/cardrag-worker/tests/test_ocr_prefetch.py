from __future__ import annotations

import asyncio
import os
import sqlite3
import threading
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from cardrag_core import OCRInput, native_ocr_reuse_key
from test_ocr import (
    OCR_BODY,
    PDF_SHA,
    FakeProvider,
    FakeWebDAV,
    cache_native,
    make_resolver,
    write_document_local_native,
)

import cardrag_worker.ocr as ocr_module
from cardrag_worker.ocr import FailoverOCRResolver, OCRResolver, OCRValidationError, PriorLocalNativeSource
from cardrag_worker.state import WorkerState


@pytest.fixture
def native(tmp_path: Path) -> Iterator[tuple[OCRResolver, WorkerState, FakeProvider]]:
    provider = FakeProvider()
    resolver, state = make_resolver(tmp_path, provider, None)
    try:
        yield resolver, state, provider
    finally:
        state.close()


def _prefetch(
    resolver: OCRResolver | FailoverOCRResolver,
    output_dir: Path,
    prior: PriorLocalNativeSource | None = None,
) -> None:
    resolver.prefetch_local_native(
        document_id="doc",
        pdf_sha256=PDF_SHA,
        pdf_size_bytes=3,
        page_count=1,
        output_dir=output_dir,
        prior_local_native=prior,
    )


def _load(resolver: OCRResolver, output_dir: Path) -> Any:
    source = OCRInput(pdf_sha256=PDF_SHA, pdf_size_bytes=3, page_count=1)
    return resolver._load_local_native(
        output_dir=output_dir, source=source, reuse_key=native_ocr_reuse_key(resolver.contract, source)
    )


@pytest.mark.asyncio
async def test_prefetch_decodes_once_without_sqlite_filesystem_or_provider_mutation(
    tmp_path: Path,
    native: tuple[OCRResolver, WorkerState, FakeProvider],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver, state, provider = native
    state.start_run(run_id="run")
    output_dir = tmp_path / "ocr"
    write_document_local_native(resolver, output_dir)
    before = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in output_dir.iterdir()}
    calls = 0
    original = resolver._read_native_seal

    def read(**kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return original(**kwargs)

    monkeypatch.setattr(resolver, "_read_native_seal", read)
    sql_calls: list[int] = []

    def deny_sql(action: int, *_args: Any) -> int:
        sql_calls.append(action)
        return sqlite3.SQLITE_DENY

    state.connection.set_authorizer(deny_sql)
    try:
        await asyncio.to_thread(_prefetch, resolver, output_dir)
    finally:
        state.connection.set_authorizer(None)
    assert sql_calls == []
    assert calls == 1
    assert {
        path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in output_dir.iterdir()
    } == before
    result = await resolver.resolve(
        run_id="run",
        document_id="doc",
        pdf_path=tmp_path / "unused.pdf",
        pdf_sha256=PDF_SHA,
        pdf_size_bytes=3,
        page_count=1,
        output_dir=output_dir,
    )
    assert result.ocr_bytes == OCR_BODY
    assert result.provenance == "native-local"
    assert calls == 1
    assert provider.calls == []


@pytest.mark.parametrize("mutation", ("content", "manifest", "file-symlink", "directory-symlink"))
def test_prefetched_ocr_rejects_changed_content_and_unsafe_paths(
    tmp_path: Path,
    native: tuple[OCRResolver, WorkerState, FakeProvider],
    mutation: str,
) -> None:
    resolver, _, _ = native
    output_dir = tmp_path / "ocr"
    write_document_local_native(resolver, output_dir)
    _prefetch(resolver, output_dir)
    if mutation == "content":
        path = output_dir / "ocr.md"
        initial = path.stat()
        path.write_bytes(OCR_BODY.replace(b"Page 1", b"Page 2"))
        os.utime(path, ns=(initial.st_atime_ns, initial.st_mtime_ns))
    elif mutation == "manifest":
        (output_dir / "native-manifest.json").write_bytes(b"invalid")
    elif mutation == "file-symlink":
        original = output_dir / "ocr.md"
        target = output_dir / "other.md"
        original.rename(target)
        original.symlink_to(target)
    else:
        renamed = output_dir.with_name("renamed")
        output_dir.rename(renamed)
        output_dir.symlink_to(renamed, target_is_directory=True)
    # Optional prefetch swallows the failure, but the real resolver retains
    # its strict local-cache failure policy and must never return stale bytes.
    _prefetch(resolver, output_dir)
    with pytest.raises((OCRValidationError, OSError)):
        _load(resolver, output_dir)
    assert not resolver._local_prefetch.entries


def test_native_prefetch_binding_includes_source_contract_and_provenance(
    tmp_path: Path, native: tuple[OCRResolver, WorkerState, FakeProvider]
) -> None:
    resolver, _, _ = native
    output_dir = tmp_path / "ocr"
    key, _ = write_document_local_native(resolver, output_dir)
    _prefetch(resolver, output_dir)
    source = OCRInput(pdf_sha256=PDF_SHA, pdf_size_bytes=3, page_count=1)
    other_source = source.model_copy(update={"pdf_size_bytes": 4})
    assert (
        resolver._load_native_seal(
            output_dir=output_dir, source=other_source, reuse_key=key, provenance="native-local"
        )
        is None
    )
    sibling = resolver._load_native_seal(
        output_dir=output_dir, source=source, reuse_key=key, provenance="native-run-local"
    )
    assert sibling is not None
    assert sibling[0].provenance == "native-run-local"
    resolver.contract = resolver.contract.model_copy(update={"cache_epoch": 1})
    assert _load(resolver, output_dir) is None


def test_prefetch_rechecks_identity_on_both_sides_of_memo_hit(
    tmp_path: Path,
    native: tuple[OCRResolver, WorkerState, FakeProvider],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver, _, _ = native
    output_dir = tmp_path / "ocr"
    write_document_local_native(resolver, output_dir)
    _prefetch(resolver, output_dir)
    original = ocr_module._native_seal_snapshot
    calls = 0

    def snapshot(path: Path, *, state_root: Path) -> Any:
        nonlocal calls
        calls += 1
        if calls == 2:
            (output_dir / "ocr.md").write_bytes(OCR_BODY.replace(b"Page 1", b"Page 2"))
        return original(path, state_root=state_root)

    monkeypatch.setattr(ocr_module, "_native_seal_snapshot", snapshot)
    with pytest.raises(OCRValidationError, match="strict verification"):
        _load(resolver, output_dir)
    assert not resolver._local_prefetch.entries


def test_native_prefetch_enforces_entry_and_memory_bounds_and_clear(
    tmp_path: Path,
    native: tuple[OCRResolver, WorkerState, FakeProvider],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver, _, _ = native
    paths = [tmp_path / f"ocr-{index}" for index in range(10)]
    for path in paths:
        write_document_local_native(resolver, path)
    for path in paths:
        _prefetch(resolver, path)
    assert len(resolver._local_prefetch.entries) == ocr_module.LOCAL_OCR_PREFETCH_MAX_ENTRIES
    assert 0 < resolver._local_prefetch.size_bytes <= ocr_module.LOCAL_OCR_PREFETCH_MAX_BYTES
    resolver.clear_local_prefetch()
    assert not resolver._local_prefetch.entries
    assert resolver._local_prefetch.size_bytes == 0
    monkeypatch.setattr(ocr_module, "LOCAL_OCR_PREFETCH_MAX_BYTES", 1)
    _prefetch(resolver, paths[0])
    assert not resolver._local_prefetch.entries
    assert _load(resolver, paths[0]) is not None


@pytest.mark.asyncio
async def test_clearing_prefetch_discards_inflight_result(
    tmp_path: Path,
    native: tuple[OCRResolver, WorkerState, FakeProvider],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver, _, _ = native
    output_dir = tmp_path / "ocr"
    write_document_local_native(resolver, output_dir)
    started = threading.Event()
    release = threading.Event()
    original = resolver._read_native_seal

    def read(**kwargs: Any) -> Any:
        started.set()
        assert release.wait(timeout=5)
        return original(**kwargs)

    monkeypatch.setattr(resolver, "_read_native_seal", read)
    pending = asyncio.create_task(asyncio.to_thread(_prefetch, resolver, output_dir))
    assert await asyncio.to_thread(started.wait, 5)
    try:
        reserved = resolver._local_prefetch.reserved_bytes
        assert reserved > 0
        resolver.clear_local_prefetch()
        assert resolver._local_prefetch.reserved_bytes == reserved
    finally:
        release.set()
        await pending
    assert not resolver._local_prefetch.entries
    assert resolver._local_prefetch.reserved_bytes == 0


def test_prior_prefetch_reads_retained_seal_without_materializing_current_run(
    tmp_path: Path, native: tuple[OCRResolver, WorkerState, FakeProvider]
) -> None:
    resolver, _, provider = native
    prior_dir = tmp_path / "runs" / "prior-run" / "documents" / "doc" / "ocr"
    _, manifest = write_document_local_native(resolver, prior_dir)
    prior = PriorLocalNativeSource(
        runs_root=tmp_path / "runs",
        run_id="prior-run",
        generation_id="generation-prior",
        corpus_sha256="a" * 64,
        contract_sha256="b" * 64,
        document_id="doc",
        pdf_sha256=PDF_SHA,
        pdf_size_bytes=3,
        page_count=1,
        ocr_sha256=manifest.output.sha256,
        ocr_size_bytes=manifest.output.size_bytes,
    )
    current = tmp_path / "runs" / "current" / "documents" / "doc" / "ocr"
    _prefetch(resolver, current, prior)
    assert not current.exists()
    assert len(resolver._local_prefetch.entries) == 1
    assert provider.calls == []
    assert resolver.matches_prior_local_native(
        prior=prior, document_id="doc", pdf_sha256=PDF_SHA, pdf_size_bytes=3, page_count=1
    )
    assert not resolver.matches_prior_local_native(
        prior=replace(prior, ocr_sha256="c" * 64),
        document_id="doc",
        pdf_sha256=PDF_SHA,
        pdf_size_bytes=3,
        page_count=1,
    )
    resolver.clear_local_prefetch()
    _prefetch(resolver, current, replace(prior, ocr_sha256="c" * 64))
    assert not resolver._local_prefetch.entries
    assert not current.exists()


@pytest.mark.asyncio
async def test_failover_prefetch_preserves_primary_priority_and_shared_limits(tmp_path: Path) -> None:
    primary_provider, fallback_provider = FakeProvider(), FakeProvider()
    with WorkerState(tmp_path / "state.sqlite3") as state:
        state.start_run(run_id="run")
        primary = OCRResolver(provider=primary_provider, state=state, webdav=None)
        fallback = OCRResolver(provider=fallback_provider, state=state, webdav=None, cache_epoch=1)
        resolver = FailoverOCRResolver(primary, fallback)
        output_dir = tmp_path / "ocr"
        write_document_local_native(primary, output_dir / "primary")
        fallback_body = OCR_BODY.replace("카드".encode(), "특별".encode())
        write_document_local_native(fallback, output_dir / "fallback", body=fallback_body)
        await asyncio.to_thread(_prefetch, resolver, output_dir)
        assert primary._local_prefetch is fallback._local_prefetch
        assert len(primary._local_prefetch.entries) == 2
        result = await resolver.resolve(
            run_id="run",
            document_id="doc",
            pdf_path=tmp_path / "unused.pdf",
            pdf_sha256=PDF_SHA,
            pdf_size_bytes=3,
            page_count=1,
            output_dir=output_dir,
        )
        assert result.ocr_bytes == OCR_BODY
        assert primary_provider.calls == fallback_provider.calls == []
        resolver.clear_local_prefetch()
        assert not primary._local_prefetch.entries


@pytest.mark.asyncio
async def test_prefetch_preserves_remote_native_priority(tmp_path: Path) -> None:
    provider, webdav = FakeProvider(), FakeWebDAV()
    resolver, state = make_resolver(tmp_path, provider, webdav, cache_mode="read-only")
    try:
        state.start_run(run_id="run")
        output_dir = tmp_path / "ocr"
        write_document_local_native(resolver, output_dir)
        remote_body = OCR_BODY.replace("카드".encode(), "특별".encode())
        cache_native(resolver, webdav, body=remote_body)
        before = dict(webdav.objects)
        _prefetch(resolver, output_dir)
        assert webdav.objects == before
        assert webdav.publish_calls == {"cas": 0, "manifest": 0, "ready": 0}
        result = await resolver.resolve(
            run_id="run",
            document_id="doc",
            pdf_path=tmp_path / "unused.pdf",
            pdf_sha256=PDF_SHA,
            pdf_size_bytes=3,
            page_count=1,
            output_dir=output_dir,
        )
        assert result.ocr_bytes == remote_body
        assert provider.calls == []
    finally:
        state.close()


def test_oversized_prefetch_skips_body_reads_and_decoding_but_serial_read_still_works(
    tmp_path: Path,
    native: tuple[OCRResolver, WorkerState, FakeProvider],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver, _, provider = native
    output_dir = tmp_path / "ocr"
    body = b"## Page 1\n\n" + (b"valid OCR body text " * 2048).rstrip() + b"\n"
    write_document_local_native(resolver, output_dir, body=body)
    monkeypatch.setattr(ocr_module, "LOCAL_OCR_PREFETCH_MAX_BYTES", 256 * 1024)
    original_read = ocr_module._read_nofollow_regular
    original_verify = ocr_module.verify_ocr_bytes
    reads: list[str] = []
    decodes = 0

    def read(path: Path, **kwargs: Any) -> bytes:
        reads.append(path.name)
        return original_read(path, **kwargs)

    def verify(*args: Any, **kwargs: Any) -> Any:
        nonlocal decodes
        decodes += 1
        return original_verify(*args, **kwargs)

    monkeypatch.setattr(ocr_module, "_read_nofollow_regular", read)
    monkeypatch.setattr(ocr_module, "verify_ocr_bytes", verify)
    _prefetch(resolver, output_dir)
    assert reads == ["native-manifest.json"]
    assert decodes == 0
    assert resolver._local_prefetch.reserved_bytes == 0
    assert not resolver._local_prefetch.entries
    assert provider.calls == []
    assert _load(resolver, output_dir)[0].ocr_bytes == body
    assert decodes > 0


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_first", (False, True))
async def test_concurrent_prefetch_reserves_shared_decode_budget_and_always_releases(
    tmp_path: Path,
    native: tuple[OCRResolver, WorkerState, FakeProvider],
    monkeypatch: pytest.MonkeyPatch,
    fail_first: bool,
) -> None:
    resolver, state, _ = native
    fallback = OCRResolver(provider=FakeProvider(), state=state, webdav=None, cache_epoch=1)
    FailoverOCRResolver(resolver, fallback)
    first_dir, second_dir = tmp_path / "first", tmp_path / "second"
    body = b"## Page 1\n\n" + (b"valid OCR body text " * 2048).rstrip() + b"\n"
    write_document_local_native(resolver, first_dir, body=body)
    write_document_local_native(fallback, second_dir, body=body)
    monkeypatch.setattr(ocr_module, "LOCAL_OCR_PREFETCH_MAX_BYTES", 1500 * 1024)
    original_read = ocr_module._read_nofollow_regular
    entered = threading.Event()
    release = threading.Event()
    body_reads: list[Path] = []

    def read(path: Path, **kwargs: Any) -> bytes:
        if path.name == "ocr.md":
            body_reads.append(path)
            if path.parent == first_dir:
                entered.set()
                assert release.wait(timeout=5)
                if fail_first:
                    raise OSError("prefetch read failed")
        return original_read(path, **kwargs)

    monkeypatch.setattr(ocr_module, "_read_nofollow_regular", read)
    first = asyncio.create_task(asyncio.to_thread(_prefetch, resolver, first_dir))
    assert await asyncio.to_thread(entered.wait, 5)
    try:
        reserved = resolver._local_prefetch.reserved_bytes
        assert 0 < reserved <= ocr_module.LOCAL_OCR_PREFETCH_MAX_BYTES
        await asyncio.wait_for(asyncio.to_thread(_prefetch, fallback, second_dir), timeout=2)
        assert body_reads == [first_dir / "ocr.md"]
        assert resolver._local_prefetch.reserved_bytes == reserved
        assert (
            resolver._local_prefetch.size_bytes + resolver._local_prefetch.reserved_bytes
            <= ocr_module.LOCAL_OCR_PREFETCH_MAX_BYTES
        )
    finally:
        release.set()
        await first
    assert resolver._local_prefetch.reserved_bytes == 0
    assert bool(resolver._local_prefetch.entries) is not fail_first
    resolver.clear_local_prefetch()
    await asyncio.to_thread(_prefetch, fallback, second_dir)
    assert body_reads[-1] == second_dir / "ocr.md"
    assert len(resolver._local_prefetch.entries) == 1
    assert resolver._local_prefetch.reserved_bytes == 0


@pytest.mark.parametrize("leaf", ("native-manifest.json", "ocr.md"))
def test_prefetch_releases_admission_when_file_grows_after_snapshot(
    tmp_path: Path,
    native: tuple[OCRResolver, WorkerState, FakeProvider],
    monkeypatch: pytest.MonkeyPatch,
    leaf: str,
) -> None:
    resolver, _, _ = native
    output_dir = tmp_path / "ocr"
    write_document_local_native(resolver, output_dir)
    original = ocr_module._read_nofollow_regular
    decodes = 0

    def read(path: Path, **kwargs: Any) -> bytes:
        if path.name == leaf:
            path.write_bytes(path.read_bytes() + b"unexpected additional bytes")
        return original(path, **kwargs)

    def verify(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal decodes
        decodes += 1
        raise AssertionError("size mismatch must fail before decoding")

    monkeypatch.setattr(ocr_module, "_read_nofollow_regular", read)
    monkeypatch.setattr(ocr_module, "verify_ocr_bytes", verify)
    _prefetch(resolver, output_dir)
    assert decodes == 0
    assert resolver._local_prefetch.reserved_bytes == 0
    assert not resolver._local_prefetch.entries
