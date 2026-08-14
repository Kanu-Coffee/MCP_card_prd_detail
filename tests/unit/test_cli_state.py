from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from cardrag import cli
from cardrag.state_transfer import RuntimeCompatibility


def test_state_commands_are_publicly_registered() -> None:
    result = CliRunner().invoke(cli.app, ["state", "--help"])

    assert result.exit_code == 0
    for command in ("export", "verify", "restore", "verify-restored"):
        assert command in result.stdout


def test_restore_does_not_connect_to_the_empty_target_cardrag_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "cardrag-state-20260813T010203Z-012345abcdef"
    package.mkdir()
    archive = tmp_path / "archive"
    archive.mkdir()
    (archive / ".cardrag-archive-root").write_text("cardrag-archive-v1\n", encoding="utf-8")
    source_record = archive / ".cardrag-archive-mount-source"
    source_record.write_text("fixture-archive\n", encoding="utf-8")
    source_record.chmod(0o440)
    objects = tmp_path / "runtime/objects"
    generations = tmp_path / "runtime/generations"
    objects.mkdir(parents=True)
    generations.mkdir()
    deployment = tmp_path / "deployment"
    deployment.mkdir()
    admin_password = tmp_path / "postgres-password"
    admin_password.write_text("fixture", encoding="utf-8")
    role_files: dict[str, Path] = {}
    for variable in (
        "CARDRAG_DB_PASSWORD_FILE",
        "CARDRAG_WORKER_DB_PASSWORD_FILE",
        "CARDRAG_MCP_DB_PASSWORD_FILE",
        "KEYCLOAK_DB_PASSWORD_FILE",
    ):
        path = tmp_path / variable.casefold()
        path.write_text("fixture", encoding="utf-8")
        role_files[variable] = path
        monkeypatch.setenv(variable, str(path))
    monkeypatch.setenv("CARDRAG_POSTGRES_ADMIN_PASSWORD_FILE", str(admin_password))
    monkeypatch.setenv("CARDRAG_DEPLOYMENT_METADATA_ROOT", str(deployment))
    monkeypatch.setenv("CARDRAG_ARCHIVE_ROOT", str(archive))
    monkeypatch.setenv("CARDRAG_ARCHIVE_EXPECTED_SOURCE", "fixture-archive")
    monkeypatch.setenv("CARDRAG_MINIMUM_FREE_GIB", "0")
    monkeypatch.setenv("CARDRAG_MINIMUM_FREE_PERCENT", "0")
    monkeypatch.setenv("CARDRAG_MAXIMUM_USED_PERCENT", "100")

    settings = SimpleNamespace(
        storage_root=objects,
        generation_root=generations,
    )
    compatibility = RuntimeCompatibility(
        application_version="0.2.0",
        image_revision="a" * 40,
        image_digests={
            "admin": "example/admin@sha256:" + "a" * 64,
            "worker": "example/worker@sha256:" + "b" * 64,
            "mcp": "example/mcp@sha256:" + "c" * 64,
        },
    )
    captured: list[object] = []

    class FakeService:
        def __init__(self, inspector: object, tools: object, **kwargs: object) -> None:
            assert inspector is None

        def restore(self, request: object) -> object:
            captured.append(request)
            return SimpleNamespace(model_dump=lambda **kwargs: {"status": "passed"})

    monkeypatch.setattr(cli, "_settings", lambda: settings)
    monkeypatch.setattr(cli, "_runtime_compatibility", lambda *_: compatibility)
    monkeypatch.setattr(cli, "PortableStateService", FakeService)
    monkeypatch.setattr(
        cli,
        "_database",
        lambda *_: (_ for _ in ()).throw(AssertionError("target DB must not be opened")),
    )

    result = CliRunner().invoke(
        cli.app,
        ["state", "restore", "--source", str(package), "--empty-target"],
    )

    assert result.exit_code == 0, result.output
    assert captured
    assert '"status": "passed"' in result.stdout


def test_restore_requires_explicit_empty_target_flag(tmp_path: Path) -> None:
    package = tmp_path / "cardrag-state-20260813T010203Z-012345abcdef"
    package.mkdir()

    result = CliRunner().invoke(cli.app, ["state", "restore", "--source", str(package)])

    assert result.exit_code != 0
    assert "--empty-target" in result.output


def test_restore_verify_flag_opens_database_inspector_only_after_activation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = tmp_path / "cardrag-state-20260813T010203Z-012345abcdef"
    package.mkdir()
    settings = SimpleNamespace(storage_root=tmp_path / "objects", generation_root=tmp_path / "generations")
    compatibility = RuntimeCompatibility(application_version="0.2.0", image_revision="a" * 40)
    events: list[str] = []
    monkeypatch.setenv("CARDRAG_MINIMUM_FREE_GIB", "0")
    monkeypatch.setenv("CARDRAG_MINIMUM_FREE_PERCENT", "0")
    monkeypatch.setenv("CARDRAG_MAXIMUM_USED_PERCENT", "100")

    class FakeRestoreService:
        def __init__(self, inspector: object, tools: object, **kwargs: object) -> None:
            del tools, kwargs
            assert inspector is None

        def restore(self, request: object) -> object:
            del request
            events.append("restore")
            return SimpleNamespace(model_dump=lambda **kwargs: {"status": "restore-passed"})

    class FakeVerificationService:
        def verify_restored(self, request: object) -> object:
            del request
            events.append("verify-restored")
            return SimpleNamespace(model_dump=lambda **kwargs: {"status": "passed"})

    class FakeDatabase:
        def close(self) -> None:
            events.append("database-close")

    def open_database(_: object) -> FakeDatabase:
        assert events == ["restore"]
        events.append("database-open")
        return FakeDatabase()

    monkeypatch.setattr(cli, "_settings", lambda: settings)
    monkeypatch.setattr(cli, "_runtime_compatibility", lambda *_: compatibility)
    monkeypatch.setattr(cli, "_postgres_tool_config", lambda: object())
    monkeypatch.setattr(cli, "PostgresToolRunner", lambda _: object())
    monkeypatch.setattr(cli, "PortableStateService", FakeRestoreService)
    monkeypatch.setattr(cli, "_database", open_database)
    monkeypatch.setattr(
        cli,
        "_restore_request",
        lambda *args, **kwargs: (args, kwargs),
    )
    monkeypatch.setattr(
        cli,
        "_state_service",
        lambda *_: (FakeVerificationService(), tmp_path, compatibility),
    )

    result = CliRunner().invoke(
        cli.app,
        [
            "state",
            "restore",
            "--source",
            str(package),
            "--empty-target",
            "--verify-restored",
        ],
    )

    assert result.exit_code == 0, result.output
    assert events == ["restore", "database-open", "verify-restored", "database-close"]
    assert '"status": "passed"' in result.stdout


@pytest.mark.parametrize(
    "arguments",
    (
        ("run", "bulk"),
        ("legacy", "import", "--bundle", "{payload}"),
        ("state", "export", "--destination", "{payload}"),
    ),
)
def test_mutating_cli_rejects_low_disk_before_opening_database(
    arguments: tuple[str, ...],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = tmp_path / "payload"
    payload.mkdir()
    settings = SimpleNamespace(
        storage_root=tmp_path,
        generation_root=tmp_path,
        build_root=tmp_path,
    )
    monkeypatch.setenv("CARDRAG_MINIMUM_FREE_GIB", "999999999")
    monkeypatch.setattr(cli, "_settings", lambda: settings)
    monkeypatch.setattr(
        cli,
        "_database",
        lambda *_: (_ for _ in ()).throw(AssertionError("database must not be opened")),
    )
    rendered = tuple(str(payload) if item == "{payload}" else item for item in arguments)

    result = CliRunner().invoke(cli.app, list(rendered))

    assert result.exit_code != 0
    assert isinstance(result.exception, Exception)
    assert "storage preflight rejected" in str(result.exception)
