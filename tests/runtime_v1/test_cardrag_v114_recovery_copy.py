from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import sys
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
TOOL = ROOT / "tools/cardrag_v114_recovery_copy.py"

import tools.cardrag_v114_recovery_copy as recovery_copy  # noqa: E402
from tools.cardrag_v114_recovery_copy import RecoveryCopyError  # noqa: E402


def _owner() -> tuple[int, int]:
    return os.getuid(), os.getgid()


def _container_inspect() -> list[dict[str, object]]:
    return [
        {
            "Name": recovery_copy.SOURCE_CONTAINER_NAME,
            "Image": recovery_copy.SOURCE_IMAGE_ID,
            "RestartCount": 0,
            "State": {
                "Status": "exited",
                "Running": False,
                "Paused": False,
                "Restarting": False,
                "OOMKilled": False,
                "Dead": False,
                "ExitCode": 1,
                "Error": "",
            },
            "Config": {
                "Image": recovery_copy.SOURCE_RUNTIME_IMAGE,
                "Labels": {
                    "com.docker.compose.project": recovery_copy.SOURCE_COMPOSE_PROJECT,
                    "org.opencontainers.image.revision": recovery_copy.SOURCE_REVISION,
                    "org.opencontainers.image.version": recovery_copy.SOURCE_APP_VERSION,
                },
            },
            "HostConfig": {"RestartPolicy": {"Name": "no", "MaximumRetryCount": 0}},
            "Mounts": [
                {
                    "Type": "volume",
                    "Name": recovery_copy.SOURCE_STATE_VOLUME,
                    "Destination": "/var/lib/cardrag-worker",
                    "RW": True,
                },
                {
                    "Type": "volume",
                    "Name": recovery_copy.SOURCE_CODEX_VOLUME,
                    "Destination": "/var/lib/cardrag-codex-home",
                    "RW": True,
                },
                {
                    "Type": "bind",
                    "Source": "/redacted/secret",
                    "Destination": "/run/secrets/webdav_password",
                    "RW": False,
                },
            ],
        }
    ]


def _image_inspect() -> list[dict[str, object]]:
    return [
        {
            "Id": recovery_copy.SOURCE_IMAGE_ID,
            "RepoDigests": [recovery_copy.SOURCE_IMAGE_REPO_DIGEST],
            "Config": {
                "Labels": {
                    "org.opencontainers.image.revision": recovery_copy.SOURCE_REVISION,
                    "org.opencontainers.image.version": recovery_copy.SOURCE_APP_VERSION,
                }
            },
        }
    ]


def test_recovery_tool_is_readable_by_the_unprivileged_production_container_user() -> None:
    assert stat.S_IMODE(TOOL.stat().st_mode) == 0o644


def test_source_inspection_accepts_only_the_exact_stopped_v113_incident() -> None:
    receipt = recovery_copy.verify_source_inspection(_container_inspect(), _image_inspect())

    assert receipt == {
        "exit_code": 1,
        "image_id": recovery_copy.SOURCE_IMAGE_ID,
        "image_repo_digest": recovery_copy.SOURCE_IMAGE_REPO_DIGEST,
        "mode": "inspect",
        "oom_killed": False,
        "schema_version": recovery_copy.SCHEMA_VERSION,
        "source_container": recovery_copy.SOURCE_CONTAINER_NAME.removeprefix("/"),
        "source_revision": recovery_copy.SOURCE_REVISION,
        "source_version": "1.0.13",
        "status": "passed",
    }


@pytest.mark.parametrize(
    ("mutation", "expected_error"),
    [
        (lambda container, _image: container[0]["State"].update(Status="running"), "terminal_state"),
        (lambda container, _image: container[0]["State"].update(ExitCode=0), "terminal_state"),
        (lambda container, _image: container[0]["State"].update(OOMKilled=True), "oomkilled"),
        (
            lambda container, _image: container[0]["Config"]["Labels"].update(  # type: ignore[index]
                {"org.opencontainers.image.version": "1.0.12"}
            ),
            "version_mismatch",
        ),
        (
            lambda _container, image: image[0].update(Id="sha256:" + "0" * 64),
            "image_id_mismatch",
        ),
        (
            lambda _container, image: image[0].update(RepoDigests=["invalid@sha256:" + "0" * 64]),
            "repo_digest_mismatch",
        ),
        (
            lambda container, _image: container[0]["HostConfig"].update(  # type: ignore[index]
                RestartPolicy={"Name": "always", "MaximumRetryCount": 0}
            ),
            "restart_policy_mismatch",
        ),
        (
            lambda container, _image: container[0].update(RestartCount=1),
            "restart_count_mismatch",
        ),
    ],
)
def test_source_inspection_rejects_nonincident_metadata(
    mutation: Callable[[list[dict[str, object]], list[dict[str, object]]], object],
    expected_error: str,
) -> None:
    container = deepcopy(_container_inspect())
    image = deepcopy(_image_inspect())
    mutation(container, image)

    with pytest.raises(RecoveryCopyError, match=expected_error):
        recovery_copy.verify_source_inspection(container, image)


def _state_source(tmp_path: Path) -> tuple[Path, Path, bytes, bytes]:
    source = tmp_path / "source-state"
    source.mkdir(mode=0o700)
    objects = source / "objects"
    objects.mkdir(mode=0o755)
    objects.chmod(0o755)
    payload = objects / "payload.bin"
    payload.write_bytes(b"sealed-payload\n")
    payload.chmod(0o600)

    database = source / "worker-state.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE run (run_id TEXT PRIMARY KEY, status TEXT NOT NULL)")
    connection.execute("INSERT INTO run VALUES ('preserved', 'failed')")
    connection.commit()
    connection.close()
    database.chmod(0o644)

    shm_bytes = b"stale-shm-wal-index"
    (source / "worker-state.sqlite3-shm").write_bytes(shm_bytes)
    return source, payload, database.read_bytes(), shm_bytes


def _empty_state_destination(tmp_path: Path, name: str = "destination-state") -> Path:
    destination = tmp_path / name
    destination.mkdir(mode=0o755)
    return destination


def _copy_state(source: Path, destination: Path) -> dict[str, object]:
    uid, gid = _owner()
    return recovery_copy.copy_state(
        source,
        destination,
        expected_uid=uid,
        expected_gid=gid,
        enforce_exact_incident=False,
        require_read_only_source=False,
    )


def test_state_copy_preserves_failed_run_and_excludes_only_stale_shm(tmp_path: Path) -> None:
    source, _payload, database_bytes, shm_bytes = _state_source(tmp_path)
    destination = _empty_state_destination(tmp_path)
    source_inventory = sorted(
        (str(path.relative_to(source)), path.stat().st_size) for path in source.rglob("*") if path.is_file()
    )

    receipt = _copy_state(source, destination)

    assert receipt["schema_version"] == recovery_copy.SCHEMA_VERSION
    assert receipt["status"] == "passed"
    assert receipt["mode"] == "state"
    assert receipt["wal_present"] is False
    assert receipt["excluded_entries"] == ["worker-state.sqlite3-shm"]
    assert receipt["main_database_sha256"] == hashlib.sha256(database_bytes).hexdigest()
    assert receipt["shm_excluded_sha256"] == hashlib.sha256(shm_bytes).hexdigest()
    assert (destination / "worker-state.sqlite3").read_bytes() == database_bytes
    assert not (destination / "worker-state.sqlite3-shm").exists()
    assert not (destination / "worker-state.sqlite3-wal").exists()
    assert (destination / "objects" / "payload.bin").read_bytes() == b"sealed-payload\n"
    assert stat.S_IMODE(destination.stat().st_mode) == 0o700
    assert (
        sorted(
            (str(path.relative_to(source)), path.stat().st_size)
            for path in source.rglob("*")
            if path.is_file()
        )
        == source_inventory
    )
    connection = sqlite3.connect(f"file:{destination / 'worker-state.sqlite3'}?mode=ro", uri=True)
    try:
        assert connection.execute("SELECT status FROM run WHERE run_id='preserved'").fetchone() == ("failed",)
    finally:
        connection.close()


def test_state_copy_requires_a_read_only_source_mount_by_default(tmp_path: Path) -> None:
    source, _payload, _database_bytes, _shm_bytes = _state_source(tmp_path)
    destination = _empty_state_destination(tmp_path)
    uid, gid = _owner()

    with pytest.raises(RecoveryCopyError, match="source_filesystem_not_read_only"):
        recovery_copy.copy_state(
            source,
            destination,
            expected_uid=uid,
            expected_gid=gid,
            enforce_exact_incident=False,
        )

    assert list(destination.iterdir()) == []


def test_state_copy_rejects_wal_even_when_otherwise_valid(tmp_path: Path) -> None:
    source, _payload, _database_bytes, _shm_bytes = _state_source(tmp_path)
    destination = _empty_state_destination(tmp_path)
    (source / "worker-state.sqlite3-wal").write_bytes(b"unexpected-wal")

    with pytest.raises(RecoveryCopyError, match="incident_wal_present"):
        _copy_state(source, destination)


def test_state_copy_detects_source_mutation_and_destination_cannot_be_reused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, payload, _database_bytes, _shm_bytes = _state_source(tmp_path)
    destination = _empty_state_destination(tmp_path)
    original_copy_stream = recovery_copy._base._copy_stream
    mutated = False

    def mutate_after_stream(source_descriptor: int, destination_descriptor: int, size: int) -> str:
        nonlocal mutated
        digest = original_copy_stream(source_descriptor, destination_descriptor, size)
        if not mutated:
            payload.write_bytes(b"mutated-after-copy")
            mutated = True
        return digest

    monkeypatch.setattr(recovery_copy._base, "_copy_stream", mutate_after_stream)

    with pytest.raises(RecoveryCopyError, match="source_file_changed_during_copy"):
        _copy_state(source, destination)
    assert mutated is True
    assert any(destination.iterdir())
    with pytest.raises(RecoveryCopyError, match="destination_not_empty"):
        _copy_state(source, destination)


def test_state_exact_incident_contract_seals_counts_sizes_and_hashes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, _payload, database_bytes, shm_bytes = _state_source(tmp_path)
    destination = _empty_state_destination(tmp_path)
    files = [path for path in source.rglob("*") if path.is_file()]
    directories = [path for path in source.rglob("*") if path.is_dir()]
    monkeypatch.setattr(recovery_copy, "INCIDENT_MAIN_DATABASE_BYTES", len(database_bytes))
    monkeypatch.setattr(recovery_copy, "INCIDENT_SHM_BYTES", len(shm_bytes))
    monkeypatch.setattr(
        recovery_copy,
        "INCIDENT_MAIN_DATABASE_SHA256",
        hashlib.sha256(database_bytes).hexdigest(),
    )
    monkeypatch.setattr(recovery_copy, "INCIDENT_SHM_SHA256", hashlib.sha256(shm_bytes).hexdigest())
    monkeypatch.setattr(recovery_copy, "INCIDENT_SOURCE_FILE_COUNT", len(files))
    monkeypatch.setattr(recovery_copy, "INCIDENT_SOURCE_DIRECTORY_ENTRY_COUNT", len(directories))
    monkeypatch.setattr(
        recovery_copy,
        "INCIDENT_SOURCE_TOTAL_FILE_BYTES",
        sum(path.stat().st_size for path in files),
    )
    uid, gid = _owner()

    receipt = recovery_copy.copy_state(
        source,
        destination,
        expected_uid=uid,
        expected_gid=gid,
        require_read_only_source=False,
    )

    assert receipt["incident_source_file_count"] == len(files)
    assert receipt["incident_source_directory_entry_count"] == len(directories)
    assert receipt["incident_source_total_file_bytes"] == sum(path.stat().st_size for path in files)


def _codex_pair(tmp_path: Path, credential: bytes) -> tuple[Path, Path]:
    source = tmp_path / "source-codex"
    destination = tmp_path / "destination-codex"
    source.mkdir(mode=0o700)
    destination.mkdir(mode=0o755)
    (destination / "home").mkdir(mode=0o700)
    auth = source / "auth.json"
    auth.write_bytes(credential)
    auth.chmod(0o600)
    (source / "runtime-cache").mkdir(mode=0o700)
    return source, destination


def test_codex_copy_is_bounded_and_copies_no_runtime_cache(tmp_path: Path) -> None:
    credential = b'{"token":"never-emit-this"}'
    source, destination = _codex_pair(tmp_path, credential)
    uid, gid = _owner()

    receipt = recovery_copy.copy_codex_auth(
        source,
        destination,
        expected_uid=uid,
        expected_gid=gid,
        require_read_only_source=False,
    )

    assert receipt["schema_version"] == recovery_copy.SCHEMA_VERSION
    assert receipt["mode"] == "codex"
    assert credential.decode() not in json.dumps(receipt, sort_keys=True)
    assert {entry.name for entry in destination.iterdir()} == {"auth.json", "home"}
    assert (destination / "auth.json").read_bytes() == credential
    assert list((destination / "home").iterdir()) == []


def test_inspection_files_reject_symlinks(tmp_path: Path) -> None:
    container = tmp_path / "container.json"
    image = tmp_path / "image.json"
    container.write_text(json.dumps(_container_inspect()), encoding="utf-8")
    image.write_text(json.dumps(_image_inspect()), encoding="utf-8")
    linked = tmp_path / "linked-container.json"
    linked.symlink_to(container)

    with pytest.raises(RecoveryCopyError, match="container_inspect_invalid"):
        recovery_copy.verify_source_inspection_files(linked, image)
