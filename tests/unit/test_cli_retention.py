from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from cardrag import cli


def test_owner_retention_one_shot_runs_generation_audit_and_metric_pruning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[str] = []

    class Database:
        closed = False

        def close(self) -> None:
            self.closed = True

    database = Database()

    class Builder:
        def __init__(self, candidate_database: object, store: object) -> None:
            assert candidate_database is database
            assert store is not None

        def prune(self) -> list[str]:
            calls.append("generation")
            return ["gen-20260801T000000Z-000000000001"]

    def prune_metadata(candidate_database: object) -> tuple[int, int]:
        assert candidate_database is database
        calls.append("metadata")
        return 2, 3

    settings = SimpleNamespace(
        generation_root=tmp_path / "generations",
        build_root=tmp_path / "build",
    )
    monkeypatch.setattr(cli, "_settings", lambda: settings)
    monkeypatch.setattr(cli, "_database", lambda _: database)
    monkeypatch.setattr(cli, "GenerationBuilder", Builder)
    monkeypatch.setattr(cli, "prune_database_retention", prune_metadata)

    cli.retention_prune()

    assert calls == ["generation", "metadata"]
    assert database.closed is True
    assert json.loads(capsys.readouterr().out) == {
        "audit_rows_removed": 2,
        "generation_ids_removed": ["gen-20260801T000000Z-000000000001"],
        "metric_rows_removed": 3,
    }
