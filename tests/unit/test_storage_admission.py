from __future__ import annotations

import os
from pathlib import Path

import pytest

from cardrag.storage_admission import (
    StorageAdmissionError,
    StorageThresholds,
    enforce_storage_admission,
)


def test_admission_rejects_capacity_below_absolute_reserve(tmp_path: Path) -> None:
    with pytest.raises(StorageAdmissionError, match="rejected"):
        enforce_storage_admission(
            [tmp_path],
            phase="preflight",
            thresholds=StorageThresholds(minimum_free_bytes=10**30),
        )


def test_postflight_requires_percent_reserve_but_not_initial_absolute_headroom(
    tmp_path: Path,
) -> None:
    result = enforce_storage_admission(
        [tmp_path],
        phase="postflight",
        thresholds=StorageThresholds(
            minimum_free_bytes=10**30,
            minimum_free_percent=0,
            warning_used_percent=0,
            maximum_used_percent=1,
        ),
    )

    assert len(result) == 1


def test_admission_deduplicates_filesystem_and_emits_sanitized_warning(tmp_path: Path) -> None:
    warnings: list[str] = []
    result = enforce_storage_admission(
        [tmp_path, tmp_path / "not-created-yet"],
        phase="preflight",
        thresholds=StorageThresholds(
            minimum_free_bytes=0,
            minimum_free_percent=0,
            warning_used_percent=0,
            maximum_used_percent=100,
        ),
        warning=warnings.append,
    )

    assert len(result) == 1
    assert len(warnings) == 1
    assert str(tmp_path) not in warnings[0]


def test_environment_thresholds_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CARDRAG_MINIMUM_FREE_GIB", "not-a-number")
    with pytest.raises(StorageAdmissionError, match="non-negative integer"):
        StorageThresholds.from_environment()


def test_admission_rejects_symlink_target(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    alias = tmp_path / "alias"
    os.symlink(target, alias)

    with pytest.raises(StorageAdmissionError, match="non-symlink"):
        enforce_storage_admission(
            [alias],
            phase="preflight",
            thresholds=StorageThresholds(minimum_free_bytes=0, minimum_free_percent=0),
        )
