"""FIX_03: a channel pointer whose generation bundle was fully reclaimed is
treated as an absent current (GC residue heals on next publication), while
partial absence and non-404 failures stay fail-closed."""

from __future__ import annotations

from pathlib import PurePosixPath
from types import SimpleNamespace
from typing import Any

import pytest
from cardrag_core import GenerationPointer
from cardrag_core.webdav import WebDAVHTTPError

import cardrag_worker.webdav as webdav_module
from cardrag_worker.webdav import WebDAVClient


class _Policy:
    channel = "candidate-v1.0.11"

    def __init__(self) -> None:
        self.pointer_checked = False
        self.observed_pointer: bytes | None = None
        self.audit: dict[str, Any] = {}
        self.puts: list[tuple[str, ...]] = []
        self.memo = SimpleNamespace(clear=lambda: self.cleared.append(True))
        self.cleared: list[bool] = []

    def due(self) -> bool:
        return False

    def put(self, *args: object) -> None:
        self.puts.append(tuple(str(a) for a in args))

    def increment(self, *_args: object) -> None: ...

    def start_audit(self) -> None: ...

    def complete_audit(self, **_kw: object) -> None: ...


def _client(monkeypatch: pytest.MonkeyPatch, *, raise_status: int, manifest_exists: bool) -> WebDAVClient:
    generation_id = "g-0928dee8e6f04af9ae41fdb7-f916d1c475e0"
    pointer = GenerationPointer(
        generation_id=generation_id, manifest_sha256="a" * 64, ready_sha256="b" * 64
    )

    class Core:
        def read_only(self) -> object:
            return object()

    client = WebDAVClient(Core(), channel="candidate-v1.0.11")  # type: ignore[arg-type]
    client.verification = _Policy()

    async def get_bytes(path: PurePosixPath | str, *_a: object, **_k: object) -> bytes | None:
        if PurePosixPath(path) == client.pointer_path:
            return pointer.canonical_bytes()
        return None

    async def exists(path: PurePosixPath | str, *_a: object, **_k: object) -> bool:
        name = PurePosixPath(path).name
        if name == "manifest.json":
            return manifest_exists
        return False

    monkeypatch.setattr(client, "get_bytes", get_bytes)  # type: ignore[method-assign]
    monkeypatch.setattr(client, "exists", exists)  # type: ignore[method-assign]

    class Reader:
        def __init__(self, _core: object, *, channel: str) -> None:
            assert channel == "candidate-v1.0.11"

        def read_current_generation(self) -> object:
            raise WebDAVHTTPError("GET", PurePosixPath("v1/generations/x/READY.json"), raise_status)

    monkeypatch.setattr(webdav_module, "MCPArtifactReader", Reader)
    return client


@pytest.mark.asyncio
async def test_fully_reclaimed_generation_is_absent_current(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch, raise_status=404, manifest_exists=False)
    assert await client.validated_current_generation() is None
    policy = client.verification
    assert policy is not None
    assert policy.pointer_checked is True
    assert policy.observed_pointer is None
    assert policy.audit.get("pending") is True


@pytest.mark.asyncio
async def test_dangling_pointer_bytes_are_effectively_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch, raise_status=404, manifest_exists=False)
    assert await client.validated_current_generation() is None
    raw = await client.get_bytes(client.pointer_path)
    assert raw is not None
    assert await client.observed_pointer_bytes() is None
    # After the next publication rebinds the pointer, the new bytes surface.
    client._dangling_pointer_bytes = None
    assert await client.observed_pointer_bytes() == raw


@pytest.mark.asyncio
async def test_partially_present_generation_stays_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch, raise_status=404, manifest_exists=True)
    with pytest.raises(WebDAVHTTPError):
        await client.validated_current_generation()


@pytest.mark.asyncio
async def test_transient_http_status_stays_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch, raise_status=503, manifest_exists=False)
    with pytest.raises(WebDAVHTTPError):
        await client.validated_current_generation()
