from __future__ import annotations

import hashlib
import json
import os
import stat
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

import cardrag.state_transfer as state_transfer
from cardrag.generation import CurrentPointer, GenerationFile, GenerationManifest
from cardrag.state_transfer import (
    ARCHIVE_SENTINEL_CONTENT,
    ARCHIVE_SENTINEL_NAME,
    ARCHIVE_SOURCE_NAME,
    ArchiveSentinelError,
    DatabaseStateSnapshot,
    ExportRequest,
    PackageVerification,
    PortableStateService,
    PostgresPortableDatabaseRestorer,
    PostgresToolConfig,
    PostgresToolError,
    PostgresToolRunner,
    ProcessResult,
    QuiescenceReport,
    RestoreRequest,
    RolePasswordSecret,
    RuntimeCompatibility,
    StateIntegrityError,
    StateManifest,
    StateProgress,
    StateQuiescenceError,
    current_schema_migrations,
    validate_archive_mount_identity,
    verify_state_package,
    verify_state_package_with_progress,
)


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _reseal_deployment(root: Path) -> None:
    payload = {
        "schema_version": "cardrag-deployment-set.v1",
        "files": {
            name: _sha((root / name).read_bytes())
            for name in ("stack-redacted.yaml", "image-digests.json", "release-manifest.json")
        },
    }
    (root / "deployment-set.json").write_text(json.dumps(payload), encoding="utf-8")


def _reseal_package_database_state(
    package: Path,
    database_state: DatabaseStateSnapshot,
) -> StateManifest:
    package.chmod(0o750)
    for path in package.rglob("*"):
        path.chmod(0o750 if path.is_dir() else 0o640)

    manifest_path = package / "state-manifest.json"
    manifest = StateManifest.model_validate_json(manifest_path.read_bytes())
    reference_path = package / "reports/reference-check.json"
    reference = state_transfer.ReferenceCheckReport.model_validate_json(
        reference_path.read_bytes()
    ).model_copy(update={"database_epoch_sha256": database_state.epoch_sha256})
    reference_path.write_bytes(state_transfer.canonical_json_bytes(reference) + b"\n")
    reference_entry = state_transfer._file_entry(reference_path, package, "report")
    files = tuple(
        reference_entry if item.path == reference_entry.path else item
        for item in manifest.files
    )
    manifest = manifest.model_copy(
        update={
            "database_epoch_sha256": database_state.epoch_sha256,
            "database_state": database_state,
            "files": files,
        }
    )
    manifest_path.write_bytes(manifest.canonical_bytes())
    checksum_body = state_transfer._checksum_body(
        files + (state_transfer._file_entry(manifest_path, package, "report"),)
    )
    (package / "checksums.sha256").write_bytes(checksum_body)
    ready = state_transfer.StateReady(
        export_id=manifest.export_id,
        state_manifest_sha256=_sha(manifest_path.read_bytes()),
        checksums_sha256=_sha(checksum_body),
    )
    (package / "READY").write_bytes(ready.canonical_bytes())
    state_transfer._seal_tree(package)
    return manifest


@dataclass
class FakeInspector:
    states: list[DatabaseStateSnapshot]
    report: QuiescenceReport = field(
        default_factory=lambda: QuiescenceReport(
            pipeline_runs=0,
            jobs=0,
            legacy_imports=0,
            other_database_sessions=0,
            status="passed",
        )
    )
    snapshot_calls: int = 0

    def quiescence(self, *, database_names: Sequence[str]) -> QuiescenceReport:
        assert tuple(database_names) == ("cardrag", "keycloak")
        return self.report

    def snapshot(self) -> DatabaseStateSnapshot:
        state = self.states[min(self.snapshot_calls, len(self.states) - 1)]
        self.snapshot_calls += 1
        return state


@dataclass
class FakeExecutor:
    version: str = "17.11"
    server_version_num: str = "170011"
    databases_empty: bool = True
    fail_dump_count: int = 0
    dump_delay_seconds: float = 0.0
    commands: list[tuple[str, ...]] = field(default_factory=list)
    password_values: list[str | None] = field(default_factory=list)

    inputs: list[bytes | None] = field(default_factory=list)

    def __call__(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        input_bytes: bytes | None = None,
    ) -> ProcessResult:
        command = tuple(argv)
        self.commands.append(command)
        self.password_values.append(env.get("PGPASSWORD"))
        self.inputs.append(input_bytes)
        executable = Path(command[0]).name
        if "--version" in command:
            return ProcessResult(0, f"{executable} (PostgreSQL) {self.version}\n", "")
        if executable == "psql":
            if "--command" not in command:
                return ProcessResult(0)
            statement = command[command.index("--command") + 1]
            if statement == "SHOW server_version_num":
                return ProcessResult(0, f"{self.server_version_num}\n", "")
            if "FROM pg_database WHERE datname=" in statement:
                return ProcessResult(0, "1\n", "")
            return ProcessResult(0, "0\n" if self.databases_empty else "1\n", "")
        if executable == "pg_dump":
            if self.dump_delay_seconds:
                time.sleep(self.dump_delay_seconds)
            if self.fail_dump_count:
                self.fail_dump_count -= 1
                return ProcessResult(1, "", "deliberate failure")
            destination = Path(command[command.index("--file") + 1])
            destination.write_bytes(b"PGDMP\x01portable-test-dump")
            return ProcessResult(0)
        if executable == "pg_restore":
            return ProcessResult(0)
        return ProcessResult(1, "", "unexpected")


@dataclass
class FakeDatabaseRestorer:
    calls: int = 0
    fail_preflight: bool = False

    def preflight(
        self,
        *,
        package: Path,
        manifest: StateManifest,
        role_password_secrets: Sequence[RolePasswordSecret],
    ) -> object:
        del package
        if self.fail_preflight:
            raise PostgresToolError("deliberate database preflight failure")
        assert {item.role for item in role_password_secrets} == {
            "cardrag",
            "cardrag_worker",
            "cardrag_mcp",
            "keycloak",
        }
        return manifest.database_state

    def execute(self, preflight: object) -> DatabaseStateSnapshot:
        assert isinstance(preflight, DatabaseStateSnapshot)
        self.calls += 1
        return preflight


@dataclass(frozen=True)
class Fixture:
    archive: Path
    objects: Path
    generations: Path
    imports: Path
    password_file: Path
    deployment: Path
    object_key: str


def _fixture(tmp_path: Path) -> Fixture:
    tmp_path.mkdir(parents=True, exist_ok=True)
    archive = tmp_path / "archive"
    archive.mkdir()
    (archive / ARCHIVE_SENTINEL_NAME).write_text(f"{ARCHIVE_SENTINEL_CONTENT}\n", encoding="utf-8")
    source_record = archive / ARCHIVE_SOURCE_NAME
    source_record.write_text("fixture-archive\n", encoding="utf-8")
    source_record.chmod(0o440)
    objects = tmp_path / "objects"
    (objects / ".incoming").mkdir(parents=True)
    payload = b"immutable object"
    digest = _sha(payload)
    object_path = objects / "sha256" / digest[:2] / digest
    object_path.parent.mkdir(parents=True)
    object_path.write_bytes(payload)
    generations = tmp_path / "generations"
    (generations / "generations").mkdir(parents=True)
    (generations / ".publish.lock").write_bytes(b"")
    imports = tmp_path / "imports"
    imports.mkdir()
    (imports / "bundle-a").mkdir()
    (imports / "bundle-a/READY").write_text("sealed", encoding="utf-8")
    password_file = tmp_path / "postgres-password"
    password_file.write_text("not-logged\n", encoding="utf-8")
    password_file.chmod(0o600)
    deployment = tmp_path / "deployment"
    deployment.mkdir()
    image_name = "example.invalid/cardrag"
    images = {
        "admin": f"{image_name}@sha256:{'a' * 64}",
        "worker": f"{image_name}@sha256:{'b' * 64}",
        "mcp": f"{image_name}@sha256:{'c' * 64}",
    }
    (deployment / "stack-redacted.yaml").write_text(
        "services:\n"
        + "".join(f"  {role}:\n    image: {reference}\n" for role, reference in images.items()),
        encoding="utf-8",
    )
    (deployment / "image-digests.json").write_text(
        json.dumps({"schema_version": "cardrag-image-digests.v1", "images": images}),
        encoding="utf-8",
    )
    roles = {
        role: {
            "schema": "cardrag.container-release-part.v3",
            "role": role,
            "image": image_name,
            "digest": reference.rsplit("@", 1)[1],
            "version": "0.2.0",
            "git_sha": "f" * 40,
        }
        for role, reference in images.items()
    }
    (deployment / "release-manifest.json").write_text(
        json.dumps(
            {
                "schema": "cardrag.container-release.v3",
                "version": "0.2.0",
                "git_sha": "f" * 40,
                "roles": roles,
            }
        ),
        encoding="utf-8",
    )
    _reseal_deployment(deployment)
    return Fixture(
        archive=archive.resolve(),
        objects=objects.resolve(),
        generations=generations.resolve(),
        imports=imports.resolve(),
        password_file=password_file.resolve(),
        deployment=deployment.resolve(),
        object_key=f"sha256/{digest[:2]}/{digest}",
    )


def _tools(fixture: Fixture, executor: FakeExecutor | None = None) -> tuple[PostgresToolRunner, FakeExecutor]:
    fake = executor or FakeExecutor()
    config = PostgresToolConfig(
        host="postgres",
        port=5432,
        user="postgres",
        password_file=fixture.password_file,
    )
    return PostgresToolRunner(config, executor=fake), fake


def _role_secrets(fixture: Fixture) -> tuple[RolePasswordSecret, ...]:
    return tuple(
        RolePasswordSecret(role=role, password_file=fixture.password_file)  # type: ignore[arg-type]
        for role in ("cardrag", "cardrag_worker", "cardrag_mcp", "keycloak")
    )


def _request(fixture: Fixture, *, include_imports: bool = False) -> ExportRequest:
    return ExportRequest(
        archive_root=fixture.archive,
        object_root=fixture.objects,
        generation_root=fixture.generations,
        imports_root=fixture.imports,
        include_imports=include_imports,
        export_id="012345abcdef",
        now=datetime(2026, 8, 13, 1, 2, 3, tzinfo=UTC),
        compatibility=RuntimeCompatibility(
            application_version="0.2.0",
            image_revision="fixture",
            embedding_provider="openrouter",
            embedding_model="openai/text-embedding-3-small",
            embedding_dimension=1536,
            image_digests={
                "admin": "example.invalid/cardrag@sha256:" + "a" * 64,
                "worker": "example.invalid/cardrag@sha256:" + "b" * 64,
                "mcp": "example.invalid/cardrag@sha256:" + "c" * 64,
            },
        ),
        deployment_root=fixture.deployment,
    )


def _service(
    fixture: Fixture,
    *,
    states: list[DatabaseStateSnapshot] | None = None,
    executor: FakeExecutor | None = None,
    report: QuiescenceReport | None = None,
) -> tuple[PortableStateService, FakeInspector, FakeExecutor]:
    database_state = DatabaseStateSnapshot(
        schema_migrations=current_schema_migrations(),
        pgvector_version="0.8.6",
        object_keys=(fixture.object_key,),
    )
    inspector = FakeInspector(states or [database_state])
    if report is not None:
        inspector.report = report
    tools, fake = _tools(fixture, executor)
    return PortableStateService(inspector, tools), inspector, fake


def _export(tmp_path: Path) -> tuple[Fixture, PackageVerification, PortableStateService, FakeExecutor]:
    fixture = _fixture(tmp_path)
    service, _, fake = _service(fixture)
    verification = service.export(_request(fixture))
    return fixture, verification, service, fake


def test_export_is_sealed_complete_and_idempotently_verifiable(tmp_path: Path) -> None:
    fixture, verification, service, fake = _export(tmp_path)
    package = verification.package_path

    assert package.name == "cardrag-state-20260813T010203Z-012345abcdef"
    assert (package / "READY").is_file()
    assert not (package / "objects/.incoming").exists()
    assert not (package / "generations/.publish.lock").exists()
    assert not (package / "imports").exists()
    assert verification.manifest.object_count == 1
    assert verification.manifest.database_state.object_keys == (fixture.object_key,)
    assert {dump.database for dump in verification.manifest.database_dumps} == {
        "cardrag",
        "keycloak",
    }
    assert verify_state_package(fixture.archive, package).manifest == verification.manifest

    # A retry with the same fixed export identity returns the already verified package.
    assert service.export(_request(fixture)).package_path == package
    assert (
        sum(Path(command[0]).name == "pg_dump" and "--version" not in command for command in fake.commands)
        == 2
    )
    assert "not-logged" not in json.dumps(fake.commands)
    assert all(value in {None, "not-logged"} for value in fake.password_values)


def test_export_without_imports_never_leaks_excluded_runtime_kinds(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    (tmp_path / "build").mkdir()
    (tmp_path / "build/secret.txt").write_text("must-not-export", encoding="utf-8")
    service, _, _ = _service(fixture)

    verification = service.export(_request(fixture, include_imports=False))
    paths = {entry.path for entry in verification.manifest.files}

    assert not any(path.startswith("imports/") for path in paths)
    assert not any("secret" in path for path in paths)
    assert set(verification.manifest.exclusions) == {
        "build_workspace",
        "page_cache",
        "codex_auth",
        "secrets",
    }


def test_archive_sentinel_is_mandatory_and_fail_closed(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    (fixture.archive / ARCHIVE_SENTINEL_NAME).write_text("wrong", encoding="utf-8")
    service, inspector, fake = _service(fixture)

    with pytest.raises(ArchiveSentinelError):
        service.export(_request(fixture))

    assert inspector.snapshot_calls == 0
    assert fake.commands == []


def test_export_rejects_active_work_and_nonempty_incoming(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    active = QuiescenceReport(
        pipeline_runs=1,
        jobs=0,
        legacy_imports=0,
        other_database_sessions=0,
        status="failed",
    )
    service, _, fake = _service(fixture, report=active)
    with pytest.raises(StateQuiescenceError, match="no active runs"):
        service.export(_request(fixture))
    assert fake.commands == []

    (fixture.objects / ".incoming/partial").write_bytes(b"partial")
    quiet_service, _, quiet_fake = _service(fixture)
    with pytest.raises(StateQuiescenceError, match="incoming"):
        quiet_service.export(_request(fixture))
    assert quiet_fake.commands == []


@pytest.mark.parametrize(
    ("database_state", "message"),
    (
        (
            DatabaseStateSnapshot(
                schema_migrations=current_schema_migrations()[:-1],
                pgvector_version="0.8.6",
            ),
            "schema migrations differ",
        ),
        (
            DatabaseStateSnapshot(
                schema_migrations=current_schema_migrations(),
                pgvector_version="0.8.2",
            ),
            "pgvector version must be 0.8.6",
        ),
    ),
)
def test_export_rejects_database_from_another_release_before_dumping(
    tmp_path: Path,
    database_state: DatabaseStateSnapshot,
    message: str,
) -> None:
    fixture = _fixture(tmp_path)
    state = database_state.model_copy(update={"object_keys": (fixture.object_key,)})
    service, _, fake = _service(fixture, states=[state])

    with pytest.raises(StateIntegrityError, match=message):
        service.export(_request(fixture))

    assert fake.commands == []
    assert not any(fixture.archive.rglob("*.dump"))
    assert not any(fixture.archive.rglob("READY"))


def test_export_rejects_symlink_and_database_epoch_change(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside")
    (fixture.generations / "escaped").symlink_to(outside)
    service, _, _ = _service(fixture)
    with pytest.raises(StateIntegrityError, match="symlink"):
        service.export(_request(fixture))

    (fixture.generations / "escaped").unlink()
    first = DatabaseStateSnapshot(
        schema_migrations=current_schema_migrations(),
        pgvector_version="0.8.6",
        object_keys=(fixture.object_key,),
    )
    second = DatabaseStateSnapshot(
        schema_migrations=current_schema_migrations(),
        pgvector_version="0.8.6",
        object_keys=(),
    )
    changed_service, _, _ = _service(fixture, states=[first, second])
    with pytest.raises(StateQuiescenceError, match="changed during export"):
        changed_service.export(_request(fixture))


def test_cli_archive_identity_record_is_exact_and_mode_bound(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)

    assert validate_archive_mount_identity(fixture.archive, "fixture-archive") == fixture.archive
    with pytest.raises(ArchiveSentinelError, match="does not match"):
        validate_archive_mount_identity(fixture.archive, "different-mount")

    record = fixture.archive / ARCHIVE_SOURCE_NAME
    record.chmod(0o640)
    with pytest.raises(ArchiveSentinelError, match="0440"):
        validate_archive_mount_identity(fixture.archive, "fixture-archive")


def test_export_rejects_special_files_partial_bundles_and_unredacted_deployment(
    tmp_path: Path,
) -> None:
    fifo_fixture = _fixture(tmp_path / "fifo")
    os.mkfifo(fifo_fixture.objects / "unexpected-fifo")
    service, _, _ = _service(fifo_fixture)
    with pytest.raises(StateIntegrityError, match="special file"):
        service.export(_request(fifo_fixture))

    bundle_fixture = _fixture(tmp_path / "bundle")
    bundle_service, _, _ = _service(bundle_fixture)
    with pytest.raises(StateIntegrityError, match="legacy import bundle is invalid"):
        bundle_service.export(_request(bundle_fixture, include_imports=True))

    secret_fixture = _fixture(tmp_path / "secret")
    (secret_fixture.deployment / "stack-redacted.yaml").write_text(
        "services:\n  app:\n    api_key: live-secret-value\n", encoding="utf-8"
    )
    _reseal_deployment(secret_fixture.deployment)
    secret_service, inspector, fake = _service(secret_fixture)
    with pytest.raises(StateIntegrityError, match="secret value"):
        secret_service.export(_request(secret_fixture))
    assert inspector.snapshot_calls == 0
    assert fake.commands == []

    database_url_fixture = _fixture(tmp_path / "database-url-secret")
    (database_url_fixture.deployment / "stack-redacted.yaml").write_text(
        "services:\n  app:\n    CARDRAG_DATABASE_URL: postgresql://cardrag:plain-password@postgres/cardrag\n",
        encoding="utf-8",
    )
    _reseal_deployment(database_url_fixture.deployment)
    database_url_service, _, _ = _service(database_url_fixture)
    with pytest.raises(StateIntegrityError, match="credential-bearing URI"):
        database_url_service.export(_request(database_url_fixture))

    generic_uri_fixture = _fixture(tmp_path / "generic-uri-secret")
    (generic_uri_fixture.deployment / "stack-redacted.yaml").write_text(
        "services:\n  app:\n    endpoint: postgresql://cardrag:plain-password@postgres/cardrag\n",
        encoding="utf-8",
    )
    _reseal_deployment(generic_uri_fixture.deployment)
    generic_uri_service, _, _ = _service(generic_uri_fixture)
    with pytest.raises(StateIntegrityError, match="credential-bearing URI"):
        generic_uri_service.export(_request(generic_uri_fixture))

    list_secret_fixture = _fixture(tmp_path / "list-env-secret")
    (list_secret_fixture.deployment / "stack-redacted.yaml").write_text(
        "services:\n  app:\n    environment:\n      - DATABASE_PASSWORD=supersecret\n",
        encoding="utf-8",
    )
    _reseal_deployment(list_secret_fixture.deployment)
    with pytest.raises(StateIntegrityError, match="secret environment value"):
        _service(list_secret_fixture)[0].export(_request(list_secret_fixture))

    quoted_list_secret_fixture = _fixture(tmp_path / "quoted-list-env-secret")
    (quoted_list_secret_fixture.deployment / "stack-redacted.yaml").write_text(
        'services:\n  app:\n    environment:\n      - "DATABASE_PASSWORD=supersecret" # no\n',
        encoding="utf-8",
    )
    _reseal_deployment(quoted_list_secret_fixture.deployment)
    with pytest.raises(StateIntegrityError, match="secret environment value"):
        _service(quoted_list_secret_fixture)[0].export(_request(quoted_list_secret_fixture))

    default_secret_fixture = _fixture(tmp_path / "default-secret")
    (default_secret_fixture.deployment / "stack-redacted.yaml").write_text(
        "services:\n  app:\n    DATABASE_PASSWORD: ${DB_PASSWORD:-supersecret}\n",
        encoding="utf-8",
    )
    _reseal_deployment(default_secret_fixture.deployment)
    with pytest.raises(StateIntegrityError, match="secret value"):
        _service(default_secret_fixture)[0].export(_request(default_secret_fixture))

    json_secret_fixture = _fixture(tmp_path / "json-secret")
    release_path = json_secret_fixture.deployment / "release-manifest.json"
    release_path.write_text(
        json.dumps({"authorization": "Bearer plaintext", "private_key": "plaintext"}),
        encoding="utf-8",
    )
    _reseal_deployment(json_secret_fixture.deployment)
    json_secret_service, _, _ = _service(json_secret_fixture)
    with pytest.raises(StateIntegrityError, match="contain a secret"):
        json_secret_service.export(_request(json_secret_fixture))

    json_list_fixture = _fixture(tmp_path / "json-list-secret")
    release_path = json_list_fixture.deployment / "release-manifest.json"
    release_path.write_text(json.dumps({"env": ["API_TOKEN=supersecret"]}), encoding="utf-8")
    _reseal_deployment(json_list_fixture.deployment)
    with pytest.raises(StateIntegrityError, match="contain a secret"):
        _service(json_list_fixture)[0].export(_request(json_list_fixture))


def test_interrupted_export_has_no_ready_and_same_id_retry_is_safe(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    fake = FakeExecutor(fail_dump_count=1)
    service, _, _ = _service(fixture, executor=fake)
    request = _request(fixture)

    with pytest.raises(PostgresToolError):
        service.export(request)

    staging = fixture.archive / ".cardrag-state-20260813T010203Z-012345abcdef.incoming"
    assert staging.is_dir()
    assert not (staging / "READY").exists()
    retry = replace(request, now=datetime(2026, 8, 14, 2, 3, 4, tzinfo=UTC))
    verification = service.export(retry)
    assert verification.status == "passed"
    assert verification.package_path.name == "cardrag-state-20260813T010203Z-012345abcdef"
    assert not staging.exists()


def test_progress_payload_is_path_and_secret_free(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    inspector = FakeInspector(
        [
            DatabaseStateSnapshot(
                schema_migrations=current_schema_migrations(),
                pgvector_version="0.8.6",
                object_keys=(fixture.object_key,),
            )
        ]
    )
    tools, _ = _tools(fixture)
    progress: list[StateProgress] = []
    service = PortableStateService(inspector, tools, progress=progress.append)

    service.export(_request(fixture))

    assert progress[0].phase == "started"
    assert progress[-1].phase == "completed"
    payload = json.dumps([item.model_dump(mode="json") for item in progress])
    assert "012345abcdef" in payload
    assert str(tmp_path) not in payload
    assert "not-logged" not in payload


def test_database_dump_phase_keeps_a_bounded_heartbeat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path)
    inspector = FakeInspector(
        [
            DatabaseStateSnapshot(
                schema_migrations=current_schema_migrations(),
                pgvector_version="0.8.6",
                object_keys=(fixture.object_key,),
            )
        ]
    )
    fake = FakeExecutor(dump_delay_seconds=0.03)
    tools, _ = _tools(fixture, fake)
    progress: list[StateProgress] = []
    monkeypatch.setattr("cardrag.state_transfer._OperationProgress._TIME_INTERVAL_SECONDS", 0.01)
    service = PortableStateService(inspector, tools, progress=progress.append)

    service.export(_request(fixture))

    heartbeats = [item for item in progress if item.phase == "databases_dumping"]
    assert len(heartbeats) >= 2


@dataclass
class StepClock:
    step: float
    value: float = 0.0

    def __call__(self) -> float:
        current = self.value
        self.value += self.step
        return current


def test_copy_progress_is_emitted_each_100_files_with_rate_and_eta(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    for index in range(204):
        payload = f"additional-object-{index}".encode()
        digest = _sha(payload)
        path = fixture.objects / "sha256" / digest[:2] / digest
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    inspector = FakeInspector(
        [
            DatabaseStateSnapshot(
                schema_migrations=current_schema_migrations(),
                pgvector_version="0.8.6",
                object_keys=(fixture.object_key,),
            )
        ]
    )
    tools, _ = _tools(fixture)
    progress: list[StateProgress] = []
    service = PortableStateService(
        inspector,
        tools,
        progress=progress.append,
        clock=StepClock(0.001),
    )

    service.export(_request(fixture))

    copying = [item for item in progress if item.phase == "objects_copying"]
    assert [item.files_completed for item in copying] == [100, 200, 205]
    assert all(item.total_files == 205 for item in copying)
    assert all(item.total_bytes is not None for item in copying)
    assert all(item.bytes_per_second > 0 for item in copying)
    assert all(item.eta_seconds is not None for item in copying)


def test_copy_progress_is_emitted_after_30_seconds_within_one_file(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    inspector = FakeInspector(
        [
            DatabaseStateSnapshot(
                schema_migrations=current_schema_migrations(),
                pgvector_version="0.8.6",
                object_keys=(fixture.object_key,),
            )
        ]
    )
    tools, _ = _tools(fixture)
    progress: list[StateProgress] = []
    service = PortableStateService(
        inspector,
        tools,
        progress=progress.append,
        clock=StepClock(31.0),
    )

    service.export(_request(fixture))

    within_file = next(
        item
        for item in progress
        if item.phase == "objects_copying" and item.files_completed == 0 and item.bytes_completed > 0
    )
    assert within_file.total_files == 1
    assert within_file.total_bytes is not None
    assert within_file.bytes_per_second > 0
    assert within_file.eta_seconds is not None


def test_restore_started_is_emitted_before_a_full_package_verification_failure(
    tmp_path: Path,
) -> None:
    fixture, verification, _, _ = _export(tmp_path / "source")
    object_path = next((verification.package_path / "objects/sha256").glob("*/*"))
    object_path.chmod(0o640)
    object_path.write_bytes(b"tampered")
    target = tmp_path / "target"
    target.mkdir()
    objects = target / "objects"
    generations = target / "generations"
    objects.mkdir()
    generations.mkdir()
    tools, _ = _tools(fixture)
    progress: list[StateProgress] = []
    service = PortableStateService(
        None,
        tools,
        database_restorer=FakeDatabaseRestorer(),
        progress=progress.append,
    )

    with pytest.raises(StateIntegrityError, match="checksum mismatch"):
        service.restore(
            RestoreRequest(
                archive_root=fixture.archive,
                package_path=verification.package_path,
                object_root=objects.resolve(),
                generation_root=generations.resolve(),
                expected_compatibility=verification.manifest.compatibility,
                role_password_secrets=_role_secrets(fixture),
            )
        )

    assert progress[0].phase == "started"
    assert progress[0].operation_id == verification.manifest.export_id
    payload = json.dumps([item.model_dump(mode="json") for item in progress])
    assert str(tmp_path) not in payload
    assert "not-logged" not in payload


@pytest.mark.parametrize(
    ("database_update", "message"),
    (
        ({"schema_migrations": current_schema_migrations()[:-1]}, "schema migrations differ"),
        ({"pgvector_version": "0.8.2"}, "pgvector version must be 0.8.6"),
    ),
)
def test_restore_rejects_another_database_release_before_any_target_mutation(
    tmp_path: Path,
    database_update: dict[str, object],
    message: str,
) -> None:
    fixture, verification, _, _ = _export(tmp_path / "source")
    bad_state = verification.manifest.database_state.model_copy(update=database_update)
    manifest = _reseal_package_database_state(verification.package_path, bad_state)
    target = tmp_path / "target"
    objects = target / "objects"
    generations = target / "generations"
    objects.mkdir(parents=True)
    generations.mkdir()
    tools, fake = _tools(fixture)
    restorer = FakeDatabaseRestorer()
    service = PortableStateService(None, tools, database_restorer=restorer)

    with pytest.raises(StateIntegrityError, match=message):
        service.restore(
            RestoreRequest(
                archive_root=fixture.archive,
                package_path=verification.package_path,
                object_root=objects.resolve(),
                generation_root=generations.resolve(),
                expected_compatibility=manifest.compatibility,
                role_password_secrets=_role_secrets(fixture),
            )
        )

    assert list(objects.iterdir()) == []
    assert list(generations.iterdir()) == []
    assert restorer.calls == 0
    assert fake.commands == []


def test_standalone_package_verify_emits_bounded_progress(tmp_path: Path) -> None:
    fixture, verification, _, _ = _export(tmp_path)
    progress: list[StateProgress] = []

    result = verify_state_package_with_progress(
        fixture.archive,
        verification.package_path,
        progress.append,
    )

    assert result.manifest == verification.manifest
    assert progress[0].operation == "verify"
    assert progress[0].phase == "started"
    assert progress[-1].phase == "completed"
    assert any(item.phase == "package_checksums_verifying" for item in progress)
    payload = json.dumps([item.model_dump(mode="json") for item in progress])
    assert str(tmp_path) not in payload
    assert "not-logged" not in payload


def test_verification_rejects_tampered_or_untracked_bytes(tmp_path: Path) -> None:
    fixture, verification, _, _ = _export(tmp_path)
    package = verification.package_path
    object_path = next((package / "objects/sha256").glob("*/*"))
    os.chmod(object_path, 0o640)
    object_path.write_bytes(b"tampered")
    with pytest.raises(StateIntegrityError, match="checksum mismatch"):
        verify_state_package(fixture.archive, package)

    # Build another clean package and inject an untracked file.
    second = tmp_path / "second"
    second.mkdir()
    second_fixture, second_verification, _, _ = _export(second)
    injected = second_verification.package_path / "objects/untracked"
    os.chmod(
        second_verification.package_path / "objects",
        0o750,  # noqa: S103 - fixture deliberately makes sealed archive writable
    )
    injected.write_bytes(b"injected")
    with pytest.raises(StateIntegrityError, match="exact package file set"):
        verify_state_package(second_fixture.archive, second_verification.package_path)


def test_postgres_runner_enforces_exact_1711_and_uses_password_only_in_environment(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    incompatible = FakeExecutor(version="17.10")
    runner, _ = _tools(fixture, incompatible)
    with pytest.raises(PostgresToolError, match="exactly 17.11"):
        runner.validate()

    incompatible_server = FakeExecutor(server_version_num="170010")
    runner, _ = _tools(fixture, incompatible_server)
    with pytest.raises(PostgresToolError, match="server must be exactly 17.11"):
        runner.validate()

    fake = FakeExecutor()
    runner, _ = _tools(fixture, fake)
    runner.validate()
    destination = tmp_path / "cardrag.dump"
    runner.dump("cardrag", destination)
    runner.restore("cardrag", destination)

    flattened = " ".join(part for command in fake.commands for part in command)
    assert "not-logged" not in flattened
    assert fake.password_values[:3] == [None, None, None]
    assert all(value == "not-logged" for value in fake.password_values[3:])
    restore_command = next(
        command
        for command in fake.commands
        if Path(command[0]).name == "pg_restore" and "--version" not in command
    )
    assert "--single-transaction" in restore_command
    assert "--exit-on-error" in restore_command


def test_runtime_role_passwords_use_stdin_only_and_require_safe_regular_files(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    fake = FakeExecutor()
    runner, _ = _tools(fixture, fake)
    secrets: list[RolePasswordSecret] = []
    secret_values: list[str] = []
    for index, role in enumerate(("cardrag", "cardrag_worker", "cardrag_mcp", "keycloak")):
        value = f"runtime-password-{index}'quoted"
        path = tmp_path / f"{role}.secret"
        path.write_text(value, encoding="utf-8")
        path.chmod(0o440)
        secrets.append(RolePasswordSecret(role=role, password_file=path))  # type: ignore[arg-type]
        secret_values.append(value)

    runner.rotate_role_passwords(secrets)

    argv_and_env = json.dumps(
        {
            "commands": fake.commands,
            "password_environment": fake.password_values,
        }
    )
    assert all(value not in argv_and_env for value in secret_values)
    stdin = fake.inputs[-1]
    assert stdin is not None
    assert all(value.replace("'", "''").encode() in stdin for value in secret_values)

    secrets[0].password_file.chmod(0o666)
    with pytest.raises(PostgresToolError, match="unsafe permissions"):
        runner.rotate_role_passwords(secrets)


def test_restore_provenance_stamp_is_idempotent_for_multi_hop_archives(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    runner, fake = _tools(fixture)

    runner.stamp_restore_provenance(
        "cardrag",
        export_id="012345abcdef",
        dump_sha256="a" * 64,
    )

    statement = fake.inputs[-1]
    assert statement is not None
    assert b"CREATE TABLE IF NOT EXISTS" in statement
    assert b"ON CONFLICT (singleton) DO UPDATE" in statement


class FakeRestartSafeDatabaseTools:
    def __init__(self, config: PostgresToolConfig) -> None:
        self.config = config
        self.exists = {"cardrag", "keycloak"}
        self.nonempty: set[str] = set()
        self.comments: dict[str, str] = {}
        self.provenance: dict[str, tuple[str, str]] = {}
        self.restore_calls: list[str] = []
        self.fail_cardrag_once = True
        self.rotations = 0
        self.mutations: list[str] = []

    def database_exists(self, database: str) -> bool:
        return database in self.exists

    def restore_provenance(self, database: str) -> tuple[str, str] | None:
        return self.provenance.get(database)

    def database_is_empty(self, database: str) -> bool:
        return database not in self.nonempty

    def database_comment(self, database: str) -> str | None:
        return self.comments.get(database)

    def drop_database(self, database: str) -> None:
        self.mutations.append(f"drop:{database}")
        self.exists.discard(database)
        self.nonempty.discard(database)
        self.comments.pop(database, None)
        self.provenance.pop(database, None)

    def create_restore_database(
        self,
        database: str,
        *,
        owner: str,
        owner_comment: str,
    ) -> None:
        assert owner in {"cardrag", "keycloak"}
        self.mutations.append(f"create:{database}")
        self.exists.add(database)
        self.comments[database] = owner_comment

    def restore(
        self,
        logical_name: str,
        source: Path,
        *,
        target_database: str,
        require_empty: bool,
    ) -> None:
        del source, require_empty
        self.restore_calls.append(logical_name)
        self.nonempty.add(target_database)
        if logical_name == "cardrag" and self.fail_cardrag_once:
            self.fail_cardrag_once = False
            raise PostgresToolError("deliberate cardrag interruption")

    def schema_migrations(self, database: str) -> tuple[tuple[int, str], ...]:
        del database
        return current_schema_migrations()

    def pgvector_version(self, database: str) -> str:
        del database
        return "0.8.6"

    def stamp_restore_provenance(self, database: str, *, export_id: str, dump_sha256: str) -> None:
        self.provenance[database] = (export_id, dump_sha256)

    def rename_database(self, source: str, destination: str) -> None:
        self.mutations.append(f"rename:{source}:{destination}")
        self.exists.discard(source)
        self.exists.add(destination)
        if source in self.nonempty:
            self.nonempty.discard(source)
            self.nonempty.add(destination)
        if source in self.comments:
            self.comments[destination] = self.comments.pop(source)
        if source in self.provenance:
            self.provenance[destination] = self.provenance.pop(source)

    def validate_role_password_secrets(
        self,
        secrets: Sequence[RolePasswordSecret],
    ) -> object:
        assert len(secrets) == 4
        return tuple((item.role, item.password_file) for item in secrets)

    def rotate_validated_role_passwords(self, secrets: object) -> None:
        assert isinstance(secrets, tuple)
        self.rotations += 1


def test_two_database_restore_stages_cardrag_before_keycloak_and_resumes(
    tmp_path: Path,
) -> None:
    fixture, verification, _, _ = _export(tmp_path)
    config = PostgresToolConfig(
        host="postgres",
        port=5432,
        user="postgres",
        password_file=fixture.password_file,
    )
    fake = FakeRestartSafeDatabaseTools(config)
    restorer = PostgresPortableDatabaseRestorer(fake)  # type: ignore[arg-type]

    with pytest.raises(PostgresToolError, match="interruption"):
        restorer.restore(
            package=verification.package_path,
            manifest=verification.manifest,
            role_password_secrets=_role_secrets(fixture),
        )
    assert fake.restore_calls == ["cardrag"]
    assert not fake.provenance
    assert not any(item.startswith("create:keycloak_restore_") for item in fake.mutations)

    restored = restorer.restore(
        package=verification.package_path,
        manifest=verification.manifest,
        role_password_secrets=_role_secrets(fixture),
    )

    assert restored == verification.manifest.database_state
    assert fake.restore_calls == ["cardrag", "cardrag", "keycloak"]
    assert set(fake.provenance) == {"keycloak", "cardrag"}
    assert fake.rotations == 1


def test_restore_refuses_predictable_unowned_empty_staging_database(tmp_path: Path) -> None:
    fixture, verification, _, _ = _export(tmp_path)
    config = PostgresToolConfig(
        host="postgres",
        port=5432,
        user="postgres",
        password_file=fixture.password_file,
    )
    fake = FakeRestartSafeDatabaseTools(config)
    staging = f"keycloak_restore_{verification.manifest.export_id}"
    fake.exists.add(staging)
    restorer = PostgresPortableDatabaseRestorer(fake)  # type: ignore[arg-type]

    with pytest.raises(PostgresToolError, match="not owned"):
        restorer.restore(
            package=verification.package_path,
            manifest=verification.manifest,
            role_password_secrets=_role_secrets(fixture),
        )

    assert staging in fake.exists
    assert fake.mutations == []


def test_cardrag_staging_collision_is_found_before_keycloak_mutation(tmp_path: Path) -> None:
    fixture, verification, _, _ = _export(tmp_path)
    config = PostgresToolConfig(
        host="postgres",
        port=5432,
        user="postgres",
        password_file=fixture.password_file,
    )
    fake = FakeRestartSafeDatabaseTools(config)
    cardrag_staging = f"cardrag_restore_{verification.manifest.export_id}"
    fake.exists.add(cardrag_staging)
    restorer = PostgresPortableDatabaseRestorer(fake)  # type: ignore[arg-type]

    with pytest.raises(PostgresToolError, match="not owned"):
        restorer.restore(
            package=verification.package_path,
            manifest=verification.manifest,
            role_password_secrets=_role_secrets(fixture),
        )

    assert cardrag_staging in fake.exists
    assert fake.mutations == []
    assert not any(item.startswith("create:keycloak_restore_") for item in fake.mutations)


def test_database_state_change_after_preflight_is_refused_before_mutation(tmp_path: Path) -> None:
    fixture, verification, _, _ = _export(tmp_path)
    config = PostgresToolConfig(
        host="postgres",
        port=5432,
        user="postgres",
        password_file=fixture.password_file,
    )
    fake = FakeRestartSafeDatabaseTools(config)
    restorer = PostgresPortableDatabaseRestorer(fake)  # type: ignore[arg-type]
    preflight = restorer.preflight(
        package=verification.package_path,
        manifest=verification.manifest,
        role_password_secrets=_role_secrets(fixture),
    )
    cardrag_staging = f"cardrag_restore_{verification.manifest.export_id}"
    fake.exists.add(cardrag_staging)
    fake.comments[cardrag_staging] = "not-owned-by-this-export"

    with pytest.raises(PostgresToolError, match="changed after preflight"):
        restorer.execute(preflight)

    assert fake.mutations == []


def test_all_role_secret_files_are_validated_before_database_mutation(tmp_path: Path) -> None:
    fixture, verification, _, _ = _export(tmp_path)
    config = PostgresToolConfig(
        host="postgres",
        port=5432,
        user="postgres",
        password_file=fixture.password_file,
    )
    fake = FakeRestartSafeDatabaseTools(config)
    runner = PostgresToolRunner(config)
    restorer = PostgresPortableDatabaseRestorer(fake)  # type: ignore[arg-type]
    secrets = list(_role_secrets(fixture))
    invalid = tmp_path / "invalid-keycloak-secret"
    invalid.write_text("", encoding="utf-8")
    invalid.chmod(0o440)
    secrets[-1] = RolePasswordSecret(role="keycloak", password_file=invalid)

    # Use the production secret validator while retaining mutation-recording
    # fake database operations.
    fake.validate_role_password_secrets = runner.validate_role_password_secrets  # type: ignore[method-assign]
    with pytest.raises(PostgresToolError, match="empty or invalid"):
        restorer.restore(
            package=verification.package_path,
            manifest=verification.manifest,
            role_password_secrets=secrets,
        )

    assert fake.mutations == []


def test_restore_installs_verified_files_and_both_database_dumps(tmp_path: Path) -> None:
    fixture, verification, _, _ = _export(tmp_path / "source")
    restore_objects = (tmp_path / "target/objects").resolve()
    restore_generations = (tmp_path / "target/generations").resolve()
    restore_objects.parent.mkdir(parents=True)
    (restore_objects.parent / "build").mkdir()
    (restore_objects.parent / "build/keep").write_text("untouched", encoding="utf-8")
    (restore_objects.parent / "page-cache").mkdir()
    (restore_objects.parent / "page-cache/keep").write_text("untouched", encoding="utf-8")
    restore_objects.mkdir()
    restore_generations.mkdir()
    restored_state = verification.manifest.database_state
    restore_inspector = FakeInspector([restored_state])
    restore_tools, fake = _tools(fixture)
    database_restorer = FakeDatabaseRestorer()
    service = PortableStateService(
        restore_inspector,
        restore_tools,
        database_restorer=database_restorer,
    )
    request = RestoreRequest(
        archive_root=fixture.archive,
        package_path=verification.package_path,
        object_root=restore_objects,
        generation_root=restore_generations,
        expected_compatibility=verification.manifest.compatibility,
        role_password_secrets=_role_secrets(fixture),
    )

    report = service.restore(request)

    assert report.status == "passed"
    restored_object = restore_objects / fixture.object_key
    assert restored_object.read_bytes() == b"immutable object"
    assert stat.S_IMODE(restored_object.stat().st_mode) == 0o444
    assert stat.S_IMODE(restore_objects.stat().st_mode) == 0o750
    assert stat.S_IMODE(restore_generations.stat().st_mode) == 0o750
    assert (restore_objects.parent / "build/keep").read_text(encoding="utf-8") == "untouched"
    assert (restore_objects.parent / "page-cache/keep").read_text(encoding="utf-8") == "untouched"
    assert database_restorer.calls == 1
    assert service.verify_restored(request).status == "passed"


def test_database_preflight_finishes_before_filesystem_restore_mutation(tmp_path: Path) -> None:
    fixture, verification, _, _ = _export(tmp_path / "source")
    target = (tmp_path / "target").resolve()
    target.mkdir()
    objects = target / "objects"
    generations = target / "generations"
    tools, _ = _tools(fixture)
    service = PortableStateService(
        None,
        tools,
        database_restorer=FakeDatabaseRestorer(fail_preflight=True),
    )

    with pytest.raises(PostgresToolError, match="preflight failure"):
        service.restore(
            RestoreRequest(
                archive_root=fixture.archive,
                package_path=verification.package_path,
                object_root=objects,
                generation_root=generations,
                expected_compatibility=verification.manifest.compatibility,
                role_password_secrets=_role_secrets(fixture),
            )
        )

    assert not objects.exists()
    assert not generations.exists()
    assert not any(target.iterdir())


def _add_active_generation(fixture: Fixture) -> DatabaseStateSnapshot:
    generation_id = "gen-20260813T010203Z-012345abcdef"
    generation_path = fixture.generations / "generations" / generation_id
    (generation_path / "reports").mkdir(parents=True)
    payload = b"quality"
    quality_path = generation_path / "reports/quality.json"
    quality_path.write_bytes(payload)
    manifest = GenerationManifest(
        generation_id=generation_id,
        created_at=datetime(2026, 8, 13, 1, 2, 3, tzinfo=UTC),
        source_snapshot_ids=(),
        document_count=0,
        latest_document_count=0,
        latest_pdf_count=0,
        latest_ocr_count=0,
        latest_structure_count=0,
        latest_embedding_count=0,
        latest_index_count=0,
        historical_quarantine_count=0,
        embedding_provider="openrouter",
        embedding_model="fixture",
        embedding_dimension=1536,
        chunk_policy="fixture",
        taxonomy_version="fixture",
        files=(
            GenerationFile(
                path="reports/quality.json",
                sha256=_sha(payload),
                size=len(payload),
            ),
        ),
        quality_report_sha256=_sha(payload),
        retrieval_report_sha256=_sha(payload),
    )
    (generation_path / "manifest.json").write_bytes(manifest.canonical_bytes())
    (generation_path / "READY").write_text(
        json.dumps({"generation_id": generation_id, "manifest_sha256": manifest.sha256}),
        encoding="utf-8",
    )
    pointer = CurrentPointer(
        generation_id=generation_id,
        manifest_sha256=manifest.sha256,
        published_at=datetime(2026, 8, 13, 1, 3, tzinfo=UTC),
    )
    (fixture.generations / "current.json").write_text(
        json.dumps(pointer.model_dump(mode="json")),
        encoding="utf-8",
    )
    (fixture.generations / "publication-history.jsonl").write_text(
        json.dumps(pointer.model_dump(mode="json")) + "\n",
        encoding="utf-8",
    )
    return DatabaseStateSnapshot(
        active_generation_id=generation_id,
        active_manifest_sha256=manifest.sha256,
        active_root_key=f"generations/{generation_id}",
        schema_migrations=current_schema_migrations(),
        pgvector_version="0.8.6",
        object_keys=(fixture.object_key,),
    )


def test_exact_restore_retry_and_verify_restored_reapply_runtime_modes(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path / "source")
    state = _add_active_generation(fixture)
    export_service, _, _ = _service(fixture, states=[state])
    verification = export_service.export(_request(fixture))
    target = tmp_path / "target"
    target.mkdir()
    objects = target / "objects"
    generations = target / "generations"
    objects.mkdir()
    generations.mkdir()
    inspector = FakeInspector([state])
    tools, _ = _tools(fixture)
    database_restorer = FakeDatabaseRestorer()
    service = PortableStateService(
        inspector,
        tools,
        database_restorer=database_restorer,
    )
    request = RestoreRequest(
        archive_root=fixture.archive,
        package_path=verification.package_path,
        object_root=objects.resolve(),
        generation_root=generations.resolve(),
        expected_compatibility=verification.manifest.compatibility,
        role_password_secrets=_role_secrets(fixture),
    )
    service.restore(request)

    restored_object = objects.joinpath(*fixture.object_key.split("/"))
    generation_id = state.active_generation_id
    assert generation_id is not None
    sealed = generations / "generations" / generation_id
    nested = sealed / "reports"
    quality = nested / "quality.json"
    current = generations / "current.json"
    history = generations / "publication-history.jsonl"
    object_parent = restored_object.parent

    for path in (objects, object_parent, generations, generations / "generations"):
        path.chmod(0o777)
    for path in (sealed, nested):
        path.chmod(0o700)
    for path in (restored_object, quality, sealed / "manifest.json", sealed / "READY"):
        path.chmod(0o600)
    for path in (current, history):
        path.chmod(0o600)

    # Exact-content retry repairs modes before resuming the database restore.
    assert service.restore(request).status == "passed"
    assert stat.S_IMODE(objects.stat().st_mode) == 0o750
    assert stat.S_IMODE(object_parent.stat().st_mode) == 0o750
    assert stat.S_IMODE(restored_object.stat().st_mode) == 0o444
    assert stat.S_IMODE(generations.stat().st_mode) == 0o750
    assert stat.S_IMODE((generations / "generations").stat().st_mode) == 0o750
    assert stat.S_IMODE(sealed.stat().st_mode) == 0o550
    assert stat.S_IMODE(nested.stat().st_mode) == 0o550
    assert stat.S_IMODE(quality.stat().st_mode) == 0o440
    assert stat.S_IMODE((sealed / "manifest.json").stat().st_mode) == 0o440
    assert stat.S_IMODE((sealed / "READY").stat().st_mode) == 0o440
    assert stat.S_IMODE(current.stat().st_mode) == 0o640
    assert stat.S_IMODE(history.stat().st_mode) == 0o640

    restored_object.chmod(0o600)
    sealed.chmod(0o700)
    quality.chmod(0o600)
    current.chmod(0o600)
    history.chmod(0o600)

    # Operator verification also repairs drift, then validates every contract mode.
    assert service.verify_restored(request).status == "passed"
    assert stat.S_IMODE(restored_object.stat().st_mode) == 0o444
    assert stat.S_IMODE(sealed.stat().st_mode) == 0o550
    assert stat.S_IMODE(quality.stat().st_mode) == 0o440
    assert stat.S_IMODE(current.stat().st_mode) == 0o640
    assert stat.S_IMODE(history.stat().st_mode) == 0o640


def test_restore_staging_remains_owned_and_retryable_after_verification_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture, verification, _, _ = _export(tmp_path / "source")
    target = tmp_path / "target"
    objects = target / "objects"
    generations = target / "generations"
    target.mkdir()
    objects.mkdir()
    generations.mkdir()
    inspector = FakeInspector([verification.manifest.database_state])
    tools, _ = _tools(fixture)
    service = PortableStateService(
        inspector,
        tools,
        database_restorer=FakeDatabaseRestorer(),
    )
    request = RestoreRequest(
        archive_root=fixture.archive,
        package_path=verification.package_path,
        object_root=objects.resolve(),
        generation_root=generations.resolve(),
        expected_compatibility=verification.manifest.compatibility,
        role_password_secrets=_role_secrets(fixture),
    )
    original = state_transfer._apply_runtime_restore_modes
    failed = False

    def fail_once(root: Path, *, package_prefix: str) -> None:
        nonlocal failed
        original(root, package_prefix=package_prefix)
        if package_prefix == "objects" and not failed:
            failed = True
            raise OSError("injected crash before atomic install")

    monkeypatch.setattr(state_transfer, "_apply_runtime_restore_modes", fail_once)
    with pytest.raises(OSError, match="injected crash"):
        service.restore(request)

    staging = target / f".objects.{verification.manifest.export_id}.restore-incoming"
    marker = target / f".objects.{verification.manifest.export_id}.restore-owner.json"
    assert staging.is_dir()
    assert marker.is_file()

    monkeypatch.setattr(state_transfer, "_apply_runtime_restore_modes", original)
    assert service.restore(request).status == "passed"
    assert not staging.exists()
    assert not marker.exists()


def test_atomic_restore_install_never_exposes_a_missing_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "objects"
    target.mkdir()
    staging = tmp_path / ".objects.abcdef123456.restore-incoming"
    staging.mkdir()
    (staging / "payload").write_bytes(b"durable")
    marker = tmp_path / ".objects.abcdef123456.restore-owner.json"
    marker.write_text(
        '{"export_id":"abcdef123456","schema_version":'
        '"cardrag-state-restore-owner.v1","target":"objects"}\n',
        encoding="utf-8",
    )
    marker.chmod(0o600)
    original_replace = state_transfer.os.replace

    def observe_atomic_replace(source: Path, destination: Path) -> None:
        assert Path(destination).is_dir()
        original_replace(source, destination)

    monkeypatch.setattr(state_transfer.os, "replace", observe_atomic_replace)
    state_transfer._install_restore_staging(staging, target, "abcdef123456")
    assert (target / "payload").read_bytes() == b"durable"
    assert not marker.exists()


def test_verify_restored_rejects_a_different_runtime_contract(tmp_path: Path) -> None:
    fixture, verification, _, _ = _export(tmp_path / "source")
    target = tmp_path / "target"
    objects = target / "objects"
    generations = target / "generations"
    target.mkdir()
    objects.mkdir()
    generations.mkdir()
    state = verification.manifest.database_state
    inspector = FakeInspector([state])
    tools, _ = _tools(fixture)
    service = PortableStateService(
        inspector,
        tools,
        database_restorer=FakeDatabaseRestorer(),
    )
    request = RestoreRequest(
        archive_root=fixture.archive,
        package_path=verification.package_path,
        object_root=objects.resolve(),
        generation_root=generations.resolve(),
        expected_compatibility=verification.manifest.compatibility,
        role_password_secrets=_role_secrets(fixture),
    )
    service.restore(request)

    incompatible = replace(
        request,
        expected_compatibility=request.expected_compatibility.model_copy(
            update={"image_revision": "different-release"}
        ),
    )
    with pytest.raises(StateIntegrityError, match="incompatible"):
        service.verify_restored(incompatible)


def test_restore_refuses_nonempty_target_before_database_mutation(tmp_path: Path) -> None:
    fixture, verification, _, _ = _export(tmp_path / "source")
    target = tmp_path / "target"
    objects = target / "objects"
    generations = target / "generations"
    objects.mkdir(parents=True)
    generations.mkdir()
    (objects / "unrelated").write_text("do not overwrite", encoding="utf-8")
    service, _, fake = _service(fixture, states=[verification.manifest.database_state])

    with pytest.raises(StateIntegrityError, match="must be empty"):
        service.restore(
            RestoreRequest(
                archive_root=fixture.archive,
                package_path=verification.package_path,
                object_root=objects.resolve(),
                generation_root=generations.resolve(),
                expected_compatibility=verification.manifest.compatibility,
                role_password_secrets=_role_secrets(fixture),
            )
        )

    assert not any(Path(command[0]).name == "pg_restore" for command in fake.commands)
    assert (objects / "unrelated").read_text(encoding="utf-8") == "do not overwrite"


def test_active_generation_is_bound_to_db_pointer_manifest_and_portable_root_key(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    generation_id = "gen-20260813T010203Z-012345abcdef"
    generation_path = fixture.generations / "generations" / generation_id
    generation_path.mkdir()
    payload = b"quality"
    (generation_path / "quality.json").write_bytes(payload)
    manifest = GenerationManifest(
        generation_id=generation_id,
        created_at=datetime(2026, 8, 13, 1, 2, 3, tzinfo=UTC),
        source_snapshot_ids=(),
        document_count=0,
        latest_document_count=0,
        latest_pdf_count=0,
        latest_ocr_count=0,
        latest_structure_count=0,
        latest_embedding_count=0,
        latest_index_count=0,
        historical_quarantine_count=0,
        embedding_provider="openrouter",
        embedding_model="fixture",
        embedding_dimension=1536,
        chunk_policy="fixture",
        taxonomy_version="fixture",
        files=(GenerationFile(path="quality.json", sha256=_sha(payload), size=len(payload)),),
        quality_report_sha256=_sha(payload),
        retrieval_report_sha256=_sha(payload),
    )
    (generation_path / "manifest.json").write_bytes(manifest.canonical_bytes())
    (generation_path / "READY").write_text(
        json.dumps({"generation_id": generation_id, "manifest_sha256": manifest.sha256}),
        encoding="utf-8",
    )
    pointer = CurrentPointer(
        generation_id=generation_id,
        manifest_sha256=manifest.sha256,
        published_at=datetime(2026, 8, 13, 1, 3, tzinfo=UTC),
    )
    (fixture.generations / "current.json").write_text(
        json.dumps(pointer.model_dump(mode="json")), encoding="utf-8"
    )
    state = DatabaseStateSnapshot(
        active_generation_id=generation_id,
        active_manifest_sha256=manifest.sha256,
        active_root_key=f"generations/{generation_id}",
        schema_migrations=current_schema_migrations(),
        pgvector_version="0.8.6",
        object_keys=(fixture.object_key,),
    )
    service, _, _ = _service(fixture, states=[state])

    verification = service.export(_request(fixture))

    assert verification.manifest.database_state.active_root_key == f"generations/{generation_id}"

    wrong = state.model_copy(update={"active_root_key": f"/srv/cardrag/{generation_id}"})
    mismatched_service, _, _ = _service(fixture, states=[wrong])
    with pytest.raises(StateIntegrityError, match="reconcile"):
        mismatched_service.export(
            replace(
                _request(fixture),
                export_id="abcdef012345",
                now=datetime(2026, 8, 13, 1, 2, 4, tzinfo=UTC),
            )
        )

    bad_pointer = pointer.model_copy(update={"previous_generation_id": "gen-20260812T010203Z-fedcba987654"})
    (fixture.generations / "current.json").write_text(
        json.dumps(bad_pointer.model_dump(mode="json")), encoding="utf-8"
    )
    bad_previous_service, _, _ = _service(fixture, states=[state])
    with pytest.raises(StateIntegrityError, match="reconcile"):
        bad_previous_service.export(
            replace(
                _request(fixture),
                export_id="abcdef012346",
                now=datetime(2026, 8, 13, 1, 2, 5, tzinfo=UTC),
            )
        )
