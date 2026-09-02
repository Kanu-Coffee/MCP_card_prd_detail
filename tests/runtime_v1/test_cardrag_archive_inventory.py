from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import tools.cardrag_archive_inventory as archive_inventory  # noqa: E402
from tools.cardrag_archive_inventory import ArchiveInventoryError, build_inventory  # noqa: E402


def _entries(manifest: dict[str, object]) -> dict[str, dict[str, object]]:
    roots = manifest["roots"]
    assert isinstance(roots, list)
    root = roots[0]
    assert isinstance(root, dict)
    entries = root["entries"]
    assert isinstance(entries, list)
    return {str(entry["path"]): entry for entry in entries if isinstance(entry, dict)}


def test_inventory_hashes_files_and_records_symlink_without_following_it(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    root = allowed / "source"
    root.mkdir(parents=True)
    (root / "nested").mkdir()
    payload = b"immutable-cardrag-payload\n"
    (root / "nested" / "object.bin").write_bytes(payload)
    outside = tmp_path / "outside-secret"
    outside.write_bytes(b"must-not-be-read")
    os.symlink(outside, root / "external-link")

    manifest = build_inventory([root], [allowed])
    entries = _entries(manifest)

    assert set(entries) == {".", "external-link", "nested", "nested/object.bin"}
    assert entries["nested/object.bin"]["sha256"] == hashlib.sha256(payload).hexdigest()
    assert entries["external-link"]["kind"] == "symlink"
    assert entries["external-link"]["sha256"] == hashlib.sha256(os.fsencode(outside)).hexdigest()
    assert entries["external-link"]["target_size_bytes"] == len(os.fsencode(outside))
    assert entries["external-link"]["target_is_absolute"] is True
    assert "link_target" not in entries["external-link"]
    assert str(outside) not in entries

    roots = manifest["roots"]
    assert isinstance(roots, list)
    root_manifest = roots[0]
    assert isinstance(root_manifest, dict)
    summary = root_manifest["summary"]
    assert isinstance(summary, dict)
    assert summary["file_count"] == 1
    assert summary["directory_count"] == 2
    assert summary["symlink_count"] == 1
    assert summary["total_file_bytes"] == len(payload)
    assert len(str(summary["content_tree_sha256"])) == 64
    assert len(str(summary["identity_tree_sha256"])) == 64


def test_inventory_rejects_roots_outside_allowlist_overlap_and_symlink_root(
    tmp_path: Path,
) -> None:
    allowed = tmp_path / "allowed"
    first = allowed / "first"
    nested = first / "nested"
    outside = tmp_path / "outside"
    nested.mkdir(parents=True)
    outside.mkdir()

    with pytest.raises(ArchiveInventoryError, match="outside the explicit allowlist"):
        build_inventory([outside], [allowed])
    with pytest.raises(ArchiveInventoryError, match="must not overlap"):
        build_inventory([first, nested], [allowed])

    alias = allowed / "alias"
    os.symlink(first, alias)
    with pytest.raises(ArchiveInventoryError, match="without following symlinks"):
        build_inventory([alias], [allowed])


def test_inventory_fails_if_file_identity_changes_while_hashing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    subject = root / "subject.bin"
    subject.write_bytes(b"before")
    original_hash = archive_inventory._hash_descriptor

    def mutate_after_hash(descriptor: int) -> tuple[str, int]:
        result = original_hash(descriptor)
        subject.write_bytes(b"after-and-different-size")
        return result

    monkeypatch.setattr(archive_inventory, "_hash_descriptor", mutate_after_hash)
    with pytest.raises(ArchiveInventoryError, match="identity changed while hashing"):
        build_inventory([root], [tmp_path])


def test_cli_defaults_to_stdout_and_optional_output_never_overwrites(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "object").write_bytes(b"payload")

    assert archive_inventory.main(["--allow-root", str(tmp_path), "--root", str(root)]) == 0
    stdout_manifest = json.loads(capsys.readouterr().out)
    assert stdout_manifest["schema_version"] == archive_inventory.SCHEMA_VERSION
    assert stdout_manifest["mode"] == "read-only-local-filesystem-inventory"

    output = tmp_path / "manifest.json"
    assert (
        archive_inventory.main(
            [
                "--allow-root",
                str(tmp_path),
                "--root",
                str(root),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    receipt = json.loads(capsys.readouterr().out)
    raw = output.read_bytes()
    assert receipt == {
        "schema_version": archive_inventory.OUTPUT_RECEIPT_SCHEMA,
        "output": str(output),
        "size_bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    assert stat.S_IMODE(output.stat().st_mode) == 0o600

    original = raw
    assert (
        archive_inventory.main(
            [
                "--allow-root",
                str(tmp_path),
                "--root",
                str(root),
                "--output",
                str(output),
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "must be a new regular file" in captured.err
    assert output.read_bytes() == original


def test_inventory_rejects_special_nodes(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    fifo = root / "fifo"
    os.mkfifo(fifo)

    with pytest.raises(ArchiveInventoryError, match="special filesystem node"):
        build_inventory([root], [tmp_path])


def test_content_projection_excludes_physical_directory_and_symlink_sizes() -> None:
    common: dict[str, object] = {"path": "node", "mode": 0o755, "uid": 10001, "gid": 10001}
    directory = {**common, "kind": "directory", "size_bytes": 4096}
    regular = {**common, "kind": "file", "size_bytes": 7, "sha256": "a" * 64}
    symlink = {
        **common,
        "kind": "symlink",
        "size_bytes": 99,
        "target_size_bytes": 8,
        "target_is_absolute": True,
        "sha256": "b" * 64,
    }

    assert archive_inventory._content_projection(directory) == {**common, "kind": "directory"}
    assert archive_inventory._content_projection(regular) == regular
    assert archive_inventory._content_projection(symlink) == {
        **common,
        "kind": "symlink",
        "target_size_bytes": 8,
        "sha256": "b" * 64,
    }


def test_output_readback_mismatch_fails_without_unlinking_created_leaf(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "manifest.json"

    def mismatched_readback(_descriptor: int) -> tuple[str, int]:
        return "0" * 64, 8

    monkeypatch.setattr(archive_inventory, "_hash_descriptor", mismatched_readback)
    with pytest.raises(ArchiveInventoryError, match="readback changed"):
        archive_inventory._write_new_manifest(output, b"manifest", [])

    assert output.read_bytes() == b"manifest"


def test_output_rejects_same_inode_same_length_overwrite_after_readback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "manifest.json"
    original_open = archive_inventory._open_absolute_directory
    output_parent_opens = 0
    observed: list[tuple[os.stat_result, os.stat_result]] = []

    def open_parent_then_mutate(path: Path, *, field: str) -> tuple[int, os.stat_result]:
        nonlocal output_parent_opens
        result = original_open(path, field=field)
        if field == "output parent":
            output_parent_opens += 1
            if output_parent_opens == 2:
                before = output.stat()
                output.write_bytes(b"tampered")
                os.utime(
                    output,
                    ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000),
                )
                observed.append((before, output.stat()))
        return result

    monkeypatch.setattr(archive_inventory, "_open_absolute_directory", open_parent_then_mutate)
    with pytest.raises(ArchiveInventoryError, match="output path changed after readback"):
        archive_inventory._write_new_manifest(output, b"manifest", [])

    assert len(observed) == 1
    before, after = observed[0]
    assert before.st_ino == after.st_ino
    assert before.st_size == after.st_size == len(b"manifest")
    assert output.read_bytes() == b"tampered"
