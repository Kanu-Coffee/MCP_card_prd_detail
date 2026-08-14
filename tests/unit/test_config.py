from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from cardrag.config import Settings


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "database_url": "postgresql://cardrag:test@postgres/cardrag",
        "storage_root": Path("/var/lib/cardrag"),
        "generation_root": Path("/var/lib/cardrag/generations"),
        "build_root": Path("/var/lib/cardrag-build"),
        "page_cache_root": Path("/var/cache/cardrag-pages"),
        "mcp_server_url": "http://localhost:8000/mcp",
        "oidc_issuer": "http://cardrag-keycloak.localhost:8080/realms/cardrag",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def test_runtime_dimension_is_locked_to_the_database_vector_schema() -> None:
    assert _settings().embedding_dimension == 1536
    with pytest.raises(ValidationError):
        _settings(embedding_dimension=4096)


def test_runtime_paths_must_be_explicit_and_absolute() -> None:
    with pytest.raises(ValidationError):
        _settings(storage_root=Path("relative"))


def test_database_url_can_be_loaded_directly_from_a_secret_file(tmp_path: Path) -> None:
    secret = tmp_path / "cardrag_database_url"
    secret.write_text("postgresql://cardrag:secret@postgres/cardrag\n", encoding="utf-8")

    settings = _settings(database_url=None, database_url_file=secret)

    assert settings.database_url_value() == "postgresql://cardrag:secret@postgres/cardrag"
    assert "secret" not in repr(settings.database_url)


def test_database_url_secret_file_is_required_when_url_is_unset(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="database_url or database_url_file is required"):
        _settings(database_url=None)

    empty = tmp_path / "empty"
    empty.write_text("\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="secret file is empty"):
        _settings(database_url=None, database_url_file=empty)


def test_online_postgres_timeouts_are_ordered_inside_the_request_deadline() -> None:
    settings = _settings()
    assert settings.postgres_lock_timeout_seconds == 5
    assert settings.postgres_statement_timeout_seconds == 40
    assert (
        settings.postgres_lock_timeout_seconds
        < settings.postgres_statement_timeout_seconds
        < settings.request_timeout_seconds
    )

    with pytest.raises(ValidationError, match="statement timeout must be below"):
        _settings(request_timeout_seconds=30, postgres_statement_timeout_seconds=30)
    with pytest.raises(ValidationError, match="lock timeout must be below"):
        _settings(postgres_statement_timeout_seconds=5, postgres_lock_timeout_seconds=5)
