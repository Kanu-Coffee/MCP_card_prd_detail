"""Three durable automatic runs per KST day, and single CLI terminal output."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from typer.testing import CliRunner

from cardrag_worker import cli
from cardrag_worker.partial_execution import PartialExecutionError
from cardrag_worker.rotation import Rotation, scope_session
from cardrag_worker.state import WorkerState

DAY = datetime(2026, 10, 22, 13, tzinfo=ZoneInfo("Asia/Seoul"))
POOL = ("woori", "kb", "shinhan")


@pytest.mark.parametrize("status", ["succeeded", "no_change", "failed"])
def test_three_runs_then_fourth_blocked_across_reopen_and_next_day(tmp_path, status):
    path = tmp_path / "state.sqlite3"
    runs = []
    for number in range(1, 4):
        with WorkerState(path) as state:
            rotation = Rotation(state, channel="stable")
            rotation.check_limit(now=DAY)
            assignment = rotation.claim(POOL, now=DAY)
            assert assignment.scope.selected == ("woori",)
            assert assignment.attempt == number and assignment.result is None
            assert assignment.run_id is None
            run = state.start_run()
            runs.append(run)
            rotation.bind(assignment, run)
            state.finish_run(run, status)
            rotation.complete(assignment, {"status": status, "run_id": run})
    assert len(set(runs)) == 3
    with WorkerState(path) as state:
        rotation = Rotation(state, channel="stable")
        before = list(state.connection.execute("SELECT * FROM issuer_rotation_day"))
        for operation in (rotation.check_limit, lambda **kw: rotation.claim(POOL, **kw)):
            with pytest.raises(PartialExecutionError) as error:
                operation(now=DAY)
            assert error.value.reason_code == "rotation_daily_attempt_limit"
        assert before == list(state.connection.execute("SELECT * FROM issuer_rotation_day"))
        history = state.connection.execute(
            "SELECT attempt,run_id,result_json FROM issuer_rotation_attempt ORDER BY attempt"
        ).fetchall()
        assert [r[1] for r in history] == runs
        assert [json.loads(r[2])["status"] for r in history] == [status] * 3
        next_day = rotation.claim(POOL, now=DAY + timedelta(days=1))
        assert next_day.scope.selected == ("kb",) and next_day.attempt == 1


def test_legacy_day_migrates_as_one_real_attempt(tmp_path):
    with WorkerState(tmp_path / "state.sqlite3") as state:
        state.connection.executescript("""
            CREATE TABLE issuer_rotation_day (
                schedule TEXT,channel TEXT,day TEXT,issuer TEXT,run_id TEXT,result_json TEXT,
                PRIMARY KEY(schedule,channel,day)
            );
        """)
        old_result = json.dumps({"status": "failed", "run_id": "original"})
        with state.transaction() as db:
            db.execute(
                "INSERT INTO issuer_rotation_day VALUES (?,?,?,?,?,?)",
                ("daily", "stable", "2026-10-22", "woori", "original", old_result),
            )
        rotation = Rotation(state, channel="stable")
        assignment = rotation.claim(POOL, now=DAY)
        assert assignment.attempt == 2 and assignment.scope.selected == ("woori",)
        history = state.connection.execute(
            "SELECT run_id,result_json FROM issuer_rotation_attempt WHERE attempt=1"
        ).fetchone()
        assert tuple(history) == ("original", old_result)
        Rotation(state, channel="stable")
        assert state.connection.execute("SELECT COUNT(*) FROM issuer_rotation_attempt").fetchone()[0] == 2


def test_unfinished_run_reuses_attempt_and_unrecorded_terminal_is_reconciled(tmp_path):
    with WorkerState(tmp_path / "state.sqlite3") as state:
        rotation = Rotation(state, channel="stable")
        first = rotation.claim(POOL, now=DAY)
        run = state.start_run()
        rotation.bind(first, run)
        resumed = rotation.claim(POOL, now=DAY)
        assert resumed.attempt == 1 and resumed.run_id == run
        state.finish_run(run, "failed")
        second = rotation.claim(POOL, now=DAY)
        assert second.attempt == 2 and second.run_id is None
        saved = state.connection.execute(
            "SELECT result_json FROM issuer_rotation_attempt WHERE attempt=1"
        ).fetchone()[0]
        assert json.loads(saved)["status"] == "failed"


@pytest.mark.asyncio
async def test_cancelled_run_keeps_attempt_and_manual_run_does_not_consume_it(tmp_path):
    with WorkerState(tmp_path / "state.sqlite3") as state:
        options = dict(selected=None, mode="round_robin", channel="stable", resume=None)
        with pytest.raises(asyncio.CancelledError):
            async with scope_session(state, tmp_path, POOL, **options) as (assignment, bind):
                run = state.start_run()
                bind(run)
                raise asyncio.CancelledError()
        async with scope_session(state, tmp_path, POOL, **options) as (assignment, bind):
            assert assignment.run_id == run and assignment.attempt == 1
        async with scope_session(
            state, tmp_path, POOL, selected=("kb",), mode="selected", channel="stable", resume=None
        ) as (assignment, bind):
            assert bind is None
        assert state.connection.execute("SELECT COUNT(*) FROM issuer_rotation_attempt").fetchone()[0] == 1


def test_stale_assignment_cannot_overwrite_new_attempt(tmp_path):
    with WorkerState(tmp_path / "state.sqlite3") as state:
        rotation = Rotation(state, channel="stable")
        old = rotation.claim(POOL, now=DAY)
        rotation.complete(old, {"status": "failed"})
        current = rotation.claim(POOL, now=DAY)
        with pytest.raises(ValueError, match="completion changed"):
            rotation.complete(old, {"status": "succeeded"})
        assert current.attempt == 2


@pytest.mark.parametrize(
    ("status", "exit_code"),
    [("failed", 1), ("interrupted", 1), ("blocked", 1), ("succeeded", 0), ("no_change", 0)],
)
def test_terminal_payload_is_one_json_with_original_reason(monkeypatch, status, exit_code):
    async def result(*args, **kwargs):
        return {"status": status, "reason_code": "original_reason", "run_id": "original"}

    monkeypatch.setattr(cli, "_run_with_signal_shutdown", result)
    output = CliRunner().invoke(cli.app, ["run", "--round-robin"])
    assert output.exit_code == exit_code
    assert json.loads(output.stdout) == {
        "status": status,
        "reason_code": "original_reason",
        "run_id": "original",
    }


def test_limit_is_one_blocked_json_and_real_exception_still_reports(monkeypatch):
    async def blocked(*args, **kwargs):
        raise PartialExecutionError("rotation_daily_attempt_limit", "scope")

    monkeypatch.setattr(cli, "_run_with_signal_shutdown", blocked)
    output = CliRunner().invoke(cli.app, ["run", "--round-robin"])
    assert output.exit_code == 1
    assert json.loads(output.stdout)["reason_code"] == "rotation_daily_attempt_limit"

    async def unexpected(*args, **kwargs):
        raise RuntimeError("private error sentinel")

    monkeypatch.setattr(cli, "_run_with_signal_shutdown", unexpected)
    output = CliRunner().invoke(cli.app, ["run"])
    assert output.exit_code == 1
    assert json.loads(output.stdout)["reason_code"] == "worker_unexpected_failure"
    assert "private error sentinel" not in output.stdout
