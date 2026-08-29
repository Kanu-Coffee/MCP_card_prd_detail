from __future__ import annotations

import math
import sqlite3
import struct
from pathlib import Path

import pytest

from cardrag_worker.state import WorkerState


def test_v5_embedding_cache_is_profile_bound_normalized_and_separate_from_v4(
    tmp_path: Path,
) -> None:
    cache_key = "1" * 64
    input_sha256 = "2" * 64
    with WorkerState(tmp_path / "state.sqlite3") as state:
        state.put_embedding(
            cache_key=cache_key,
            contract_sha256="3" * 64,
            text_sha256=input_sha256,
            embedding=b"\x00" * 6144,
        )
        assert (
            state.get_embedding_v5(
                cache_key,
                profile_id="profile-deepinfra",
                input_sha256=input_sha256,
                dimension=4,
            )
            is None
        )

        cached = state.put_embedding_v5(
            cache_key=cache_key,
            profile_id="profile-deepinfra",
            input_sha256=input_sha256,
            dimension=4,
            values=[3.0, 4.0, 0.0, 0.0],
        )
        assert cached.profile_id == "profile-deepinfra"
        assert cached.dtype == "float32"
        assert cached.normalization == "l2"
        values = struct.unpack("<4f", cached.embedding)
        assert values == pytest.approx((0.6, 0.8, 0.0, 0.0))
        assert math.isclose(sum(value * value for value in values), 1.0, rel_tol=2e-5)

        loaded = state.get_embedding_v5(
            cache_key,
            profile_id="profile-deepinfra",
            input_sha256=input_sha256,
            dimension=4,
        )
        assert loaded == cached
        with pytest.raises(RuntimeError, match="different profile or input"):
            state.get_embedding_v5(
                cache_key,
                profile_id="profile-nebius",
                input_sha256=input_sha256,
                dimension=4,
            )
        with pytest.raises(RuntimeError, match="collision"):
            state.put_embedding_v5(
                cache_key=cache_key,
                profile_id="profile-deepinfra",
                input_sha256=input_sha256,
                dimension=4,
                values=[1.0, 0.0, 0.0, 0.0],
            )


@pytest.mark.parametrize(
    "values",
    (
        [0.0, 0.0],
        [math.inf, 0.0],
        [math.nan, 1.0],
        [1.0],
    ),
)
def test_v5_embedding_cache_rejects_invalid_vectors(tmp_path: Path, values: list[float]) -> None:
    with WorkerState(tmp_path / "state.sqlite3") as state, pytest.raises(ValueError):
        state.put_embedding_v5(
            cache_key="a" * 64,
            profile_id="profile",
            input_sha256="b" * 64,
            dimension=2,
            values=values,
        )


def test_v5_embedding_cache_sql_check_binds_blob_length_to_dimension(tmp_path: Path) -> None:
    with WorkerState(tmp_path / "state.sqlite3") as state, pytest.raises(sqlite3.IntegrityError):
        state.connection.execute(
            """INSERT INTO embedding_cache_v5
               (cache_key,profile_id,input_sha256,dimension,dtype,normalization,embedding,created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (
                "c" * 64,
                "profile",
                "d" * 64,
                4096,
                "float32",
                "l2",
                b"wrong-size",
                "2026-01-01T00:00:00+00:00",
            ),
        )


def test_v5_embedding_cache_detects_non_normalized_persisted_bytes(tmp_path: Path) -> None:
    with WorkerState(tmp_path / "state.sqlite3") as state:
        state.connection.execute(
            """INSERT INTO embedding_cache_v5
               (cache_key,profile_id,input_sha256,dimension,dtype,normalization,embedding,created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (
                "e" * 64,
                "profile",
                "f" * 64,
                2,
                "float32",
                "l2",
                struct.pack("<2f", 2.0, 0.0),
                "2026-01-01T00:00:00+00:00",
            ),
        )
        with pytest.raises(RuntimeError, match="non-normalized"):
            state.get_embedding_v5(
                "e" * 64,
                profile_id="profile",
                input_sha256="f" * 64,
                dimension=2,
            )
