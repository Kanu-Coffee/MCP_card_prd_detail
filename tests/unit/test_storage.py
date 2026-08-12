from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cardrag.domain import ArtifactManifest, ArtifactType, Lineage, sha256_bytes
from cardrag.storage import (
    ContentAddressedObjectStore,
    ObjectIntegrityError,
    UnsafePathError,
    atomic_write_bytes,
    atomic_write_within_root,
    portable_relative_path,
    resolve_within_root,
    write_artifact_manifest,
)


@pytest.mark.parametrize(
    "value",
    ["", "/absolute", "../escape", "nested/../escape", "https://example.test/a", "C:/data/a", "a\\b"],
)
def test_portable_relative_path_rejects_unsafe_values(value: str) -> None:
    with pytest.raises(UnsafePathError):
        portable_relative_path(value)


def test_resolve_within_root_blocks_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)

    with pytest.raises(UnsafePathError):
        resolve_within_root(root, "escape/object.pdf")

    assert resolve_within_root(root, "safe/object.pdf") == root / "safe" / "object.pdf"


def test_atomic_write_has_explicit_overwrite_semantics(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    atomic_write_bytes(target, b"first")
    assert target.read_bytes() == b"first"

    with pytest.raises(FileExistsError):
        atomic_write_bytes(target, b"second")
    assert target.read_bytes() == b"first"

    atomic_write_bytes(target, b"second", overwrite=True)
    assert target.read_bytes() == b"second"
    assert not list(tmp_path.glob("*.tmp"))


def test_atomic_write_within_root_checks_containment(tmp_path: Path) -> None:
    root = tmp_path / "root"
    target = atomic_write_within_root(root, "catalog/doc.json", b"{}")
    assert target == root / "catalog" / "doc.json"
    with pytest.raises(UnsafePathError):
        atomic_write_within_root(root, "../outside.json", b"{}")


def test_content_addressed_store_deduplicates_and_verifies(tmp_path: Path) -> None:
    store = ContentAddressedObjectStore(tmp_path / "objects")
    payload = b"%PDF-1.7\nfixture"

    first = store.put_bytes(payload)
    second = store.put_stream((payload[:5], payload[5:]))

    assert first == second
    assert first.sha256 == sha256_bytes(payload)
    assert str(first.relative_path) == f"sha256/{first.sha256[:2]}/{first.sha256}"
    assert store.read_bytes(first.sha256) == payload
    assert store.verify(first.sha256).size_bytes == len(payload)
    assert store.path_for(first.sha256).stat().st_mode & 0o222 == 0


def test_content_addressed_store_refuses_tampered_existing_object(tmp_path: Path) -> None:
    store = ContentAddressedObjectStore(tmp_path / "objects")
    stored = store.put_bytes(b"canonical")
    path = store.path_for(stored.sha256)
    os.chmod(path, 0o644)
    path.write_bytes(b"tampered!")

    with pytest.raises(ObjectIntegrityError):
        store.verify(stored.sha256)
    with pytest.raises(ObjectIntegrityError):
        store.put_bytes(b"canonical")


def test_content_addressed_store_rejects_symlink_objects(tmp_path: Path) -> None:
    store = ContentAddressedObjectStore(tmp_path / "objects")
    digest = sha256_bytes(b"payload")
    target = resolve_within_root(store.root, store.relative_path_for(digest))
    target.parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.write_bytes(b"payload")
    target.symlink_to(outside)

    with pytest.raises((ObjectIntegrityError, UnsafePathError)):
        store.verify(digest)


def test_manifest_is_written_as_canonical_immutable_bytes(tmp_path: Path) -> None:
    lineage = Lineage(
        processor="fixture",
        processor_version="1",
        config_sha256=sha256_bytes(b"config"),
    )
    manifest = ArtifactManifest.for_bytes(
        artifact_type=ArtifactType.QUALITY_REPORT,
        payload=b"{}",
        media_type="application/json",
        created_at=datetime(2026, 8, 12, tzinfo=UTC),
        lineage=lineage,
    )
    target = write_artifact_manifest(tmp_path, "manifests/report.json", manifest)

    assert target.read_bytes() == manifest.canonical_bytes()
    assert target.stat().st_mode & 0o222 == 0
    with pytest.raises(FileExistsError):
        write_artifact_manifest(tmp_path, "manifests/report.json", manifest)
