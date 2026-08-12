from __future__ import annotations

from uuid import UUID

import pytest

from cardrag.cli import _list_runs
from cardrag.db import Postgres

pytestmark = pytest.mark.integration


def test_running_run_is_discoverable_for_durable_finalize(clean_database: Postgres) -> None:
    running_id = UUID("00000000-0000-0000-0000-000000000101")
    succeeded_id = UUID("00000000-0000-0000-0000-000000000102")
    with clean_database.connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO pipeline_runs(run_id, run_type, state, generation_id, started_at)
            VALUES (%s, 'daily', 'running', 'gen-running', now()),
                   (%s, 'daily', 'succeeded', 'gen-succeeded', now())
            """,
            (running_id, succeeded_id),
        )
        for sequence, issuer in enumerate(("woori", "kb", "shinhan"), 1):
            cursor.execute(
                """
                INSERT INTO run_issuer_status(run_id, issuer, sequence_no, state)
                VALUES (%s, %s, %s, %s)
                """,
                (running_id, issuer, sequence, "running" if sequence == 1 else "queued"),
            )
        connection.commit()

    rows = _list_runs(clean_database, state="running", limit=20)

    assert [row["run_id"] for row in rows] == [running_id]
    assert [item["issuer"] for item in rows[0]["issuers"]] == ["woori", "kb", "shinhan"]
