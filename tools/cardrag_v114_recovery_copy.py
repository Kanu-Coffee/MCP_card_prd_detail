#!/usr/bin/env python3
"""Fail-closed offline copy helper for the CardRAG v1.0.14 recovery.

The v1.0.13 acceptance Worker stopped after sealing its local generation.  This
tool verifies the exact stopped container/image evidence and copies its state
or bounded Codex credential into fresh v1.0.14 volumes.  Source filesystems
must be mounted read-only.  The stale root SQLite SHM file is the only omitted
state entry; the incident has no WAL.

The implementation deliberately reuses the descriptor-based, race-resistant
copy primitives from the audited v1.0.13 recovery helper.  Both scripts must
therefore be mounted together when this file is run outside the repository.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path
from typing import Final, NoReturn

try:
    from tools import cardrag_v113_recovery_copy as _base
except ModuleNotFoundError:  # pragma: no cover - standalone recovery mount
    import cardrag_v113_recovery_copy as _base  # type: ignore[import-not-found,no-redef]


SCHEMA_VERSION: Final = "cardrag.v114-recovery-copy.v1"
PRODUCTION_UID: Final = _base.PRODUCTION_UID
PRODUCTION_GID: Final = _base.PRODUCTION_GID
MAXIMUM_AUTH_BYTES: Final = _base.MAXIMUM_AUTH_BYTES
MAXIMUM_INSPECT_BYTES: Final = 4 * 1024 * 1024

SOURCE_CONTAINER_NAME: Final = "/cardrag-v113-candidate-worker-acceptance"
SOURCE_COMPOSE_PROJECT: Final = "cardrag-v113-candidate"
SOURCE_APP_VERSION: Final = "1.0.13"
SOURCE_REVISION: Final = "03a24f5e549e5466dfe99db61e9ebbf6b58f8410"
SOURCE_IMAGE_ID: Final = "sha256:9703eddeb5e4b1f3423b250fd13978121b24ec4a5a2c3e8064db4a76bdbe0be9"
SOURCE_IMAGE_REPO_DIGEST: Final = (
    "ghcr.io/kanu-coffee/mcp-card-prd-detail-candidate"
    "@sha256:9703eddeb5e4b1f3423b250fd13978121b24ec4a5a2c3e8064db4a76bdbe0be9"
)
SOURCE_RUNTIME_IMAGE: Final = SOURCE_IMAGE_REPO_DIGEST
SOURCE_STATE_VOLUME: Final = "cardrag-worker-v113-candidate-state"
SOURCE_CODEX_VOLUME: Final = "cardrag-worker-v113-candidate-codex-home"

MAIN_DATABASE: Final = _base.MAIN_DATABASE
INCIDENT_WAL: Final = _base.INCIDENT_WAL
INCIDENT_SHM: Final = _base.INCIDENT_SHM
SQLITE_HEADER: Final = _base.SQLITE_HEADER

INCIDENT_MAIN_DATABASE_BYTES: Final = 3_900_289_024
INCIDENT_SHM_BYTES: Final = 32_768
INCIDENT_MAIN_DATABASE_SHA256: Final = "f4941cc73f15a021f6606d837829dc96c90bc6eda2faad6f4fa33577265e04df"
INCIDENT_SHM_SHA256: Final = "31125591d630ebf62822a27764a37a81fdc5a8482334f462f7e93fdecec6ecd4"
INCIDENT_SOURCE_FILE_COUNT: Final = 15_883
INCIDENT_SOURCE_DIRECTORY_ENTRY_COUNT: Final = 10_427
INCIDENT_SOURCE_TOTAL_FILE_BYTES: Final = 16_697_468_245

RecoveryCopyError = _base.RecoveryCopyError


def _fail(reason: str) -> NoReturn:
    raise RecoveryCopyError(reason)


def _one_inspect_object(value: object, *, field: str) -> dict[str, object]:
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        _fail(f"{field}_shape_invalid")
    return value[0]


def _mapping(value: object, *, field: str) -> dict[str, object]:
    if not isinstance(value, dict):
        _fail(f"{field}_invalid")
    return value


def _exact_bool(value: object, expected: bool, *, field: str) -> None:
    if type(value) is not bool or value is not expected:
        _fail(f"{field}_invalid")


def verify_source_inspection(
    container_inspect: object,
    image_inspect: object,
) -> dict[str, object]:
    """Verify exact, stopped v1.0.13 incident container and image metadata."""

    container = _one_inspect_object(container_inspect, field="container_inspect")
    image = _one_inspect_object(image_inspect, field="image_inspect")
    state = _mapping(container.get("State"), field="container_state")
    config = _mapping(container.get("Config"), field="container_config")
    labels = _mapping(config.get("Labels"), field="container_labels")
    host_config = _mapping(container.get("HostConfig"), field="container_host_config")
    restart = _mapping(host_config.get("RestartPolicy"), field="container_restart_policy")

    if container.get("Name") != SOURCE_CONTAINER_NAME:
        _fail("source_container_name_mismatch")
    if container.get("Image") != SOURCE_IMAGE_ID or config.get("Image") != SOURCE_RUNTIME_IMAGE:
        _fail("source_container_image_mismatch")
    if (
        state.get("Status") != "exited"
        or type(state.get("ExitCode")) is not int
        or state.get("ExitCode") != 1
    ):
        _fail("source_container_terminal_state_mismatch")
    if type(container.get("RestartCount")) is not int or container.get("RestartCount") != 0:
        _fail("source_container_restart_count_mismatch")
    for key, expected in (
        ("Running", False),
        ("Paused", False),
        ("Restarting", False),
        ("OOMKilled", False),
        ("Dead", False),
    ):
        _exact_bool(state.get(key), expected, field=f"source_container_{key.lower()}")
    if state.get("Error") not in {None, ""}:
        _fail("source_container_runtime_error_present")
    if restart != {"Name": "no", "MaximumRetryCount": 0}:
        _fail("source_container_restart_policy_mismatch")
    if labels.get("org.opencontainers.image.version") != SOURCE_APP_VERSION:
        _fail("source_container_version_mismatch")
    if labels.get("org.opencontainers.image.revision") != SOURCE_REVISION:
        _fail("source_container_revision_mismatch")
    if labels.get("com.docker.compose.project") != SOURCE_COMPOSE_PROJECT:
        _fail("source_container_project_mismatch")

    mounts = container.get("Mounts")
    if not isinstance(mounts, list):
        _fail("source_container_mounts_invalid")
    expected_mounts = {
        "/var/lib/cardrag-worker": SOURCE_STATE_VOLUME,
        "/var/lib/cardrag-codex-home": SOURCE_CODEX_VOLUME,
    }
    found: dict[str, str] = {}
    for value in mounts:
        mount = _mapping(value, field="source_container_mount")
        destination = mount.get("Destination")
        if destination not in expected_mounts:
            continue
        if (
            destination in found
            or mount.get("Type") != "volume"
            or type(mount.get("RW")) is not bool
            or mount.get("RW") is not True
        ):
            _fail("source_container_mount_mismatch")
        name = mount.get("Name")
        if not isinstance(name, str):
            _fail("source_container_mount_mismatch")
        found[destination] = name
    if found != expected_mounts:
        _fail("source_container_mount_mismatch")

    image_config = _mapping(image.get("Config"), field="image_config")
    image_labels = _mapping(image_config.get("Labels"), field="image_labels")
    repo_digests = image.get("RepoDigests")
    if image.get("Id") != SOURCE_IMAGE_ID:
        _fail("source_image_id_mismatch")
    if not isinstance(repo_digests, list) or SOURCE_IMAGE_REPO_DIGEST not in repo_digests:
        _fail("source_image_repo_digest_mismatch")
    if image_labels.get("org.opencontainers.image.version") != SOURCE_APP_VERSION:
        _fail("source_image_version_mismatch")
    if image_labels.get("org.opencontainers.image.revision") != SOURCE_REVISION:
        _fail("source_image_revision_mismatch")

    return {
        "exit_code": 1,
        "image_id": SOURCE_IMAGE_ID,
        "image_repo_digest": SOURCE_IMAGE_REPO_DIGEST,
        "mode": "inspect",
        "oom_killed": False,
        "schema_version": SCHEMA_VERSION,
        "source_container": SOURCE_CONTAINER_NAME.removeprefix("/"),
        "source_revision": SOURCE_REVISION,
        "source_version": SOURCE_APP_VERSION,
        "status": "passed",
    }


def _load_inspect(path_raw: str | Path, *, field: str) -> object:
    path = _base._normalize_absolute(path_raw, field=field)
    parent_descriptor, parent_identity = _base._open_absolute_directory(path.parent, field=field)
    descriptor: int | None = None
    try:
        try:
            named = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
        except OSError as exc:
            raise RecoveryCopyError(f"{field}_open_failed") from exc
        identity = _base._Identity.from_stat(named)
        if (
            not stat.S_ISREG(identity.mode)
            or identity.link_count != 1
            or not 1 <= identity.size_bytes <= MAXIMUM_INSPECT_BYTES
        ):
            _fail(f"{field}_invalid")
        descriptor = _base._open_source_regular(parent_descriptor, path.name, identity, field=field)
        remaining = identity.size_bytes
        content = bytearray()
        while remaining:
            block = os.read(descriptor, min(64 * 1024, remaining))
            if not block:
                _fail(f"{field}_short_read")
            content.extend(block)
            remaining -= len(block)
        if os.read(descriptor, 1):
            _fail(f"{field}_grew_during_read")
        named_after = _base._Identity.from_stat(
            os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
        )
        if (
            named_after != identity
            or _base._Identity.from_stat(os.fstat(descriptor)) != identity
            or _base._Identity.from_stat(os.fstat(parent_descriptor)) != parent_identity
        ):
            _fail(f"{field}_changed_during_read")
        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            raise RecoveryCopyError(f"{field}_invalid") from exc
    except OSError as exc:
        raise RecoveryCopyError(f"{field}_read_failed") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_descriptor)


def verify_source_inspection_files(
    container_path: str | Path,
    image_path: str | Path,
) -> dict[str, object]:
    return verify_source_inspection(
        _load_inspect(container_path, field="container_inspect"),
        _load_inspect(image_path, field="image_inspect"),
    )


def _require_source_filesystem_read_only(source: Path) -> None:
    try:
        flags = os.statvfs(source).f_flag
    except OSError as exc:
        raise RecoveryCopyError("source_filesystem_stat_failed") from exc
    if not flags & getattr(os, "ST_RDONLY", 1):
        _fail("source_filesystem_not_read_only")


def _validate_v113_inventory(
    entries: list[_base._Entry],
    *,
    source: bool,
    enforce_exact_incident: bool,
) -> None:
    by_path = _base._entry_map(entries)
    main = by_path.get(MAIN_DATABASE)
    if main is None or main.kind != "file":
        _fail("required_worker_state_sqlite3_missing")
    if main.identity.size_bytes <= 0:
        _fail("main_database_empty")
    if INCIDENT_WAL in by_path:
        _fail("incident_wal_present")
    if source:
        shm = by_path.get(INCIDENT_SHM)
        if shm is None or shm.kind != "file":
            _fail("required_worker_state_sqlite3_shm_missing")
    elif INCIDENT_SHM in by_path:
        _fail("destination_shm_present")
    if source and enforce_exact_incident:
        files = [entry for entry in entries if entry.kind == "file"]
        directories = [entry for entry in entries if entry.kind == "directory" and entry.relative]
        if main.identity.size_bytes != INCIDENT_MAIN_DATABASE_BYTES:
            _fail("incident_main_database_size_mismatch")
        if by_path[INCIDENT_SHM].identity.size_bytes != INCIDENT_SHM_BYTES:
            _fail("incident_shm_size_mismatch")
        if len(files) != INCIDENT_SOURCE_FILE_COUNT:
            _fail("incident_source_file_count_mismatch")
        if len(directories) != INCIDENT_SOURCE_DIRECTORY_ENTRY_COUNT:
            _fail("incident_source_directory_count_mismatch")
        if sum(entry.identity.size_bytes for entry in files) != INCIDENT_SOURCE_TOTAL_FILE_BYTES:
            _fail("incident_source_total_bytes_mismatch")


def copy_state(
    source_raw: str | Path,
    destination_raw: str | Path,
    *,
    expected_uid: int,
    expected_gid: int,
    enforce_exact_incident: bool = True,
    require_read_only_source: bool = True,
) -> dict[str, object]:
    """Copy the exact stopped v1.0.13 state into one pristine v1.0.14 tree."""

    source = _base._normalize_absolute(source_raw, field="source")
    destination = _base._normalize_absolute(destination_raw, field="destination")
    if require_read_only_source:
        _require_source_filesystem_read_only(source)
    source_descriptor, source_root_identity = _base._open_absolute_directory(source, field="source")
    try:
        destination_descriptor, destination_root_identity = _base._open_absolute_directory(
            destination, field="destination"
        )
    except BaseException:
        os.close(source_descriptor)
        raise
    pinned: dict[tuple[str, ...], int] = {}
    try:
        _base._require_distinct_roots(source, source_root_identity, destination, destination_root_identity)
        _base._require_state_root(
            source_root_identity,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
            destination=False,
        )
        _base._require_state_root(
            destination_root_identity,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
            destination=True,
        )
        if _base._list_names(destination_descriptor, field="destination"):
            _fail("destination_not_empty")

        source_entries = _base._scan_state_tree(
            source_descriptor,
            source_root_identity,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
            allow_incident_shm=True,
        )
        _validate_v113_inventory(source_entries, source=True, enforce_exact_incident=enforce_exact_incident)
        source_by_path = _base._entry_map(source_entries)
        if (destination_root_identity.device, destination_root_identity.inode) in {
            (entry.identity.device, entry.identity.inode) for entry in source_entries
        }:
            _fail("source_destination_inode_overlap")

        for relative in (MAIN_DATABASE, INCIDENT_SHM):
            pinned[relative] = _base._open_source_regular(
                source_descriptor,
                relative[0],
                source_by_path[relative].identity,
                field="incident_sqlite_file",
            )
        if os.pread(pinned[MAIN_DATABASE], len(SQLITE_HEADER), 0) != SQLITE_HEADER:
            _fail("main_database_header_invalid")
        shm_digest = _base._hash_regular_descriptor(
            pinned[INCIDENT_SHM], source_by_path[INCIDENT_SHM].identity, field="incident_shm"
        )

        os.fchmod(destination_descriptor, 0o700)
        os.fsync(destination_descriptor)
        sealed_destination_root = _base._Identity.from_stat(os.fstat(destination_descriptor))
        _base._require_state_root(
            sealed_destination_root,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
            destination=False,
        )

        children: dict[tuple[str, ...], list[_base._Entry]] = {}
        for entry in source_entries[1:]:
            children.setdefault(entry.relative[:-1], []).append(entry)
        file_hashes: dict[tuple[str, ...], str] = {}

        def copy_directory(relative: tuple[str, ...], source_parent: int, destination_parent: int) -> None:
            directory_entry = source_by_path[relative]
            if _base._Identity.from_stat(os.fstat(source_parent)) != directory_entry.identity:
                _fail("state_source_directory_changed_during_copy")
            for entry in children.get(relative, []):
                name = entry.relative[-1]
                named = _base._Identity.from_stat(os.stat(name, dir_fd=source_parent, follow_symlinks=False))
                if named != entry.identity:
                    _fail("state_source_entry_changed_before_copy")
                if entry.relative == INCIDENT_SHM:
                    continue
                if entry.kind == "file":
                    file_hashes[entry.relative] = _base._copy_regular_file(
                        source_parent,
                        destination_parent,
                        entry,
                        pinned_source=pinned.get(entry.relative),
                        expected_uid=expected_uid,
                        expected_gid=expected_gid,
                    )
                    continue
                source_child, opened_identity = _base._open_child_directory(
                    source_parent, name, field="state_source"
                )
                destination_child: int | None = None
                try:
                    if opened_identity != entry.identity:
                        _fail("state_source_directory_identity_changed")
                    try:
                        os.mkdir(name, 0o700, dir_fd=destination_parent)
                        destination_child, _ = _base._open_child_directory(
                            destination_parent, name, field="state_destination"
                        )
                    except OSError as exc:
                        raise RecoveryCopyError("state_destination_directory_create_failed") from exc
                    copy_directory(entry.relative, source_child, destination_child)
                    os.fchmod(destination_child, stat.S_IMODE(entry.identity.mode))
                    os.fsync(destination_child)
                    destination_child_identity = _base._Identity.from_stat(os.fstat(destination_child))
                    _base._require_owner(
                        destination_child_identity,
                        expected_uid=expected_uid,
                        expected_gid=expected_gid,
                        field="state_destination_directory",
                    )
                    if destination_child_identity.device != sealed_destination_root.device or stat.S_IMODE(
                        destination_child_identity.mode
                    ) != stat.S_IMODE(entry.identity.mode):
                        _fail("state_destination_directory_metadata_invalid")
                finally:
                    if destination_child is not None:
                        os.close(destination_child)
                    os.close(source_child)
            os.fsync(destination_parent)
            if _base._Identity.from_stat(os.fstat(source_parent)) != directory_entry.identity:
                _fail("state_source_directory_changed_during_copy")

        copy_directory((), source_descriptor, destination_descriptor)
        os.fsync(destination_descriptor)
        if enforce_exact_incident:
            if file_hashes[MAIN_DATABASE] != INCIDENT_MAIN_DATABASE_SHA256:
                _fail("incident_main_database_hash_mismatch")
            if shm_digest != INCIDENT_SHM_SHA256:
                _fail("incident_shm_hash_mismatch")

        source_after = _base._scan_state_tree(
            source_descriptor,
            source_root_identity,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
            allow_incident_shm=True,
        )
        if source_after != source_entries:
            _fail("source_tree_changed_during_copy")
        for pinned_relative, descriptor in pinned.items():
            if _base._Identity.from_stat(os.fstat(descriptor)) != source_by_path[pinned_relative].identity:
                _fail("incident_sqlite_file_changed_during_copy")
        _base._verify_absolute_directory(source, source_descriptor, source_root_identity, field="source")

        destination_after_identity = _base._Identity.from_stat(os.fstat(destination_descriptor))
        destination_entries = _base._scan_state_tree(
            destination_descriptor,
            destination_after_identity,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
            allow_incident_shm=False,
        )
        _validate_v113_inventory(destination_entries, source=False, enforce_exact_incident=False)
        if {(entry.identity.device, entry.identity.inode) for entry in source_entries} & {
            (entry.identity.device, entry.identity.inode) for entry in destination_entries
        }:
            _fail("source_destination_inode_overlap")
        expected_canonical = _base._canonical_tree(
            source_entries, file_hashes, excluded=frozenset({INCIDENT_SHM})
        )
        destination_canonical = _base._canonical_tree(destination_entries, file_hashes, excluded=frozenset())
        if destination_canonical != expected_canonical:
            _fail("destination_tree_mismatch")
        _base._verify_absolute_directory(
            destination,
            destination_descriptor,
            destination_after_identity,
            field="destination",
        )

        return {
            "bytes_copied": sum(
                entry.identity.size_bytes
                for entry in source_entries
                if entry.kind == "file" and entry.relative != INCIDENT_SHM
            ),
            "content_tree_sha256": _base._tree_digest(expected_canonical),
            "directory_count": sum(entry.kind == "directory" for entry in destination_entries),
            "excluded_entries": [INCIDENT_SHM[0]],
            "file_count": len(file_hashes),
            "incident_source_directory_entry_count": sum(
                entry.kind == "directory" and bool(entry.relative) for entry in source_entries
            ),
            "incident_source_file_count": sum(entry.kind == "file" for entry in source_entries),
            "incident_source_total_file_bytes": sum(
                entry.identity.size_bytes for entry in source_entries if entry.kind == "file"
            ),
            "main_database_sha256": file_hashes[MAIN_DATABASE],
            "main_database_size_bytes": source_by_path[MAIN_DATABASE].identity.size_bytes,
            "mode": "state",
            "schema_version": SCHEMA_VERSION,
            "shm_excluded_sha256": shm_digest,
            "shm_excluded_size_bytes": source_by_path[INCIDENT_SHM].identity.size_bytes,
            "status": "passed",
            "wal_present": False,
        }
    except OSError as exc:
        raise RecoveryCopyError("state_filesystem_operation_failed") from exc
    finally:
        for descriptor in pinned.values():
            os.close(descriptor)
        os.close(destination_descriptor)
        os.close(source_descriptor)


def copy_codex_auth(
    source_raw: str | Path,
    destination_raw: str | Path,
    *,
    expected_uid: int,
    expected_gid: int,
    maximum_auth_bytes: int = MAXIMUM_AUTH_BYTES,
    require_read_only_source: bool = True,
) -> dict[str, object]:
    """Copy only bounded ``auth.json`` into a pristine v1.0.14 Codex home."""

    source = _base._normalize_absolute(source_raw, field="source")
    if require_read_only_source:
        _require_source_filesystem_read_only(source)
    receipt = _base.copy_codex_auth(
        source,
        destination_raw,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
        maximum_auth_bytes=maximum_auth_bytes,
    )
    return {**receipt, "schema_version": SCHEMA_VERSION}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="mode", required=True)
    for mode in ("inspect", "state", "codex"):
        subparser = subparsers.add_parser(mode)
        subparser.add_argument("--container-inspect", required=True)
        subparser.add_argument("--image-inspect", required=True)
        if mode != "inspect":
            subparser.add_argument("--source", required=True)
            subparser.add_argument("--destination", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        inspection_receipt = verify_source_inspection_files(args.container_inspect, args.image_inspect)
        if args.mode == "inspect":
            receipt = inspection_receipt
        elif args.mode == "state":
            receipt = copy_state(
                args.source,
                args.destination,
                expected_uid=PRODUCTION_UID,
                expected_gid=PRODUCTION_GID,
            )
        else:
            receipt = copy_codex_auth(
                args.source,
                args.destination,
                expected_uid=PRODUCTION_UID,
                expected_gid=PRODUCTION_GID,
            )
    except RecoveryCopyError as exc:
        print(f"cardrag_v114_recovery_copy_failed:{exc}", file=sys.stderr)
        return 2
    print(json.dumps(receipt, ensure_ascii=True, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
