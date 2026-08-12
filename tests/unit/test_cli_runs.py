from __future__ import annotations

import json
from contextlib import nullcontext
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Self
from uuid import UUID

import pytest
from typer.testing import CliRunner

from cardrag import cli


class _Cursor:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows
        self.params: tuple[object, ...] | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, _query: str, params: tuple[object, ...]) -> None:
        self.params = params

    def fetchall(self) -> list[dict[str, object]]:
        return self.rows


class _Connection:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor

    def cursor(self) -> _Cursor:
        return self._cursor


class _Database:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor
        self.closed = False

    def connection(self) -> nullcontext[_Connection]:
        return nullcontext(_Connection(self._cursor))

    def close(self) -> None:
        self.closed = True


def test_run_list_prints_discoverable_running_run_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = UUID("00000000-0000-0000-0000-000000000123")
    cursor = _Cursor(
        [
            {
                "run_id": run_id,
                "run_type": "daily",
                "state": "running",
                "generation_id": "gen-20260812T000000Z-000000000123",
                "started_at": datetime(2026, 8, 12, tzinfo=UTC),
                "finished_at": None,
                "created_at": datetime(2026, 8, 12, tzinfo=UTC),
                "issuers": [{"issuer": "woori", "state": "running"}],
            }
        ]
    )
    database = _Database(cursor)
    monkeypatch.setattr(cli, "_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(cli, "_database", lambda _settings: database)

    result = CliRunner().invoke(cli.app, ["run", "list", "--state", "running", "--limit", "5"])

    assert result.exit_code == 0
    assert cursor.params == ("running", "running", 5)
    assert database.closed is True
    payload = json.loads(result.stdout)
    assert payload["runs"][0]["run_id"] == str(run_id)
    assert payload["runs"][0]["state"] == "running"


def test_run_list_rejects_unknown_state_before_opening_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened = False

    def fail_if_opened(_settings: object) -> object:
        nonlocal opened
        opened = True
        raise AssertionError("database should not be opened")

    monkeypatch.setattr(cli, "_database", fail_if_opened)

    result = CliRunner().invoke(cli.app, ["run", "list", "--state", "unknown"])

    assert result.exit_code == 2
    assert "state must be one of" in result.output
    assert opened is False
