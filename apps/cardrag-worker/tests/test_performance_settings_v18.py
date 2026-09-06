from __future__ import annotations

from typing import Any

import pytest

import cardrag_worker.webdav as webdav_module
from cardrag_worker.settings import PublicationResumeSettings, WorkerSettings

_SETTINGS = (
    ("CARDRAG_PDF_CONCURRENCY", "pdf_concurrency", 8, 1, 32),
    ("CARDRAG_PDF_CONCURRENCY_PER_ISSUER", "pdf_concurrency_per_issuer", 2, 1, 8),
    ("CARDRAG_LOCAL_PROCESSING_WORKERS", "local_processing_workers", 4, 1, 8),
    ("CARDRAG_STATE_SQLITE_CACHE_MIB", "sqlite_cache_mib", 256, 1, 1024),
    ("CARDRAG_STATE_SQLITE_MMAP_MIB", "sqlite_mmap_mib", 2048, 0, 4096),
    ("CARDRAG_WEBDAV_UPLOAD_CHUNK_MIB", "webdav_upload_chunk_mib", 8, 1, 16),
)


@pytest.mark.parametrize(("name", "field", "default", "minimum", "maximum"), _SETTINGS)
def test_performance_settings_defaults_and_bounds(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    field: str,
    default: int,
    minimum: int,
    maximum: int,
) -> None:
    monkeypatch.delenv(name, raising=False)
    assert getattr(WorkerSettings.from_env(), field) == default
    for value in (minimum, maximum):
        monkeypatch.setenv(name, str(value))
        assert getattr(WorkerSettings.from_env(), field) == value
    for value in (str(minimum - 1), str(maximum + 1), "1.5", "true", ""):
        monkeypatch.setenv(name, value)
        with pytest.raises(ValueError, match=name):
            WorkerSettings.from_env()


@pytest.mark.parametrize(("name", "field", "default", "minimum", "maximum"), _SETTINGS[3:])
def test_publication_resume_uses_same_local_transfer_settings(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    field: str,
    default: int,
    minimum: int,
    maximum: int,
) -> None:
    monkeypatch.delenv(name, raising=False)
    assert getattr(PublicationResumeSettings.from_env(), field) == default
    for value in (minimum, maximum):
        monkeypatch.setenv(name, str(value))
        assert getattr(PublicationResumeSettings.from_env(), field) == value
    monkeypatch.setenv(name, str(maximum + 1))
    with pytest.raises(ValueError, match=name):
        PublicationResumeSettings.from_env()


@pytest.mark.parametrize(
    ("configured", "override", "expected"), [(None, None, 8), ("3", None, 3), ("3", 2, 2)]
)
def test_worker_webdav_forwards_chunk_size_to_both_publishers(
    monkeypatch: pytest.MonkeyPatch,
    configured: str | None,
    override: int | None,
    expected: int,
) -> None:
    calls: list[int] = []

    class Core:
        def performance_snapshot(self) -> dict[str, int | float]:
            return {"put_requests": 7}

    core = Core()

    def publisher(client: Any, *, upload_chunk_size_bytes: int) -> object:
        assert client is core
        calls.append(upload_chunk_size_bytes)
        return object()

    monkeypatch.setattr(webdav_module, "CoreWebDAVClient", lambda _settings: core)
    monkeypatch.setattr(webdav_module.WebDAVSettings, "from_env", lambda: object())
    monkeypatch.setattr(webdav_module, "ImmutablePublisher", publisher)
    monkeypatch.setattr(webdav_module, "CASPublisher", publisher)
    if configured is None:
        monkeypatch.delenv("CARDRAG_WEBDAV_UPLOAD_CHUNK_MIB", raising=False)
    else:
        monkeypatch.setenv("CARDRAG_WEBDAV_UPLOAD_CHUNK_MIB", configured)
    client = webdav_module.WebDAVClient.from_env(
        upload_chunk_size_bytes=None if override is None else override * 1024 * 1024,
    )
    assert calls == [expected * 1024 * 1024] * 2
    assert client.performance_snapshot() == {"put_requests": 7}
