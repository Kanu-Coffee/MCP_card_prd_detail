from __future__ import annotations

import os

import pytest

from cardrag.db import Postgres


@pytest.fixture(scope="session")
def integration_database_url() -> str:
    value = os.environ.get("CARDRAG_TEST_DATABASE_URL")
    if not value:
        pytest.skip("CARDRAG_TEST_DATABASE_URL is not configured")
    return value


@pytest.fixture(scope="session")
def migrated_database(integration_database_url: str) -> Postgres:
    database = Postgres(integration_database_url, min_size=1, max_size=16)
    database.open()
    database.migrate()
    yield database
    database.close()


@pytest.fixture()
def clean_database(migrated_database: Postgres) -> Postgres:
    with migrated_database.connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            TRUNCATE TABLE
                metric_rollups, audit_events, stage_checkpoints, job_attempts, jobs,
                legacy_import_documents, legacy_imports,
                run_issuer_status, pipeline_runs, scheduler_locks, generation_pins,
                issuer_rate_limits, generation_artifacts, generation_documents,
                generation_expected_documents, generation_snapshots,
                active_generation, evidence, generations,
                source_documents, source_snapshots
            RESTART IDENTITY CASCADE
            """
        )
        connection.commit()
    return migrated_database
