from __future__ import annotations

from datetime import date

import pytest

from cardrag_core.derived_metadata import (
    DerivedTextSegment,
    LaunchDateResolution,
    resolve_launch_date_segments,
    resolve_launch_date_texts,
    resolve_lineage_launch_date,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("상품 출시일 2026.08.12", date(2026, 8, 12)),
        ("출시일자: 2026-8-12", date(2026, 8, 12)),
        ("발매일 2026/08/12", date(2026, 8, 12)),
        ("판매 개시일 2026년 8월 12일", date(2026, 8, 12)),
        ("2026.08.12 출시", date(2026, 8, 12)),
    ],
)
def test_supported_launch_date_grammar(text: str, expected: date) -> None:
    resolution = resolve_launch_date_texts((text,))
    assert (resolution.launch_date, resolution.status) == (expected, "confirmed")


def test_label_and_date_can_cross_adjacent_source_segments() -> None:
    resolution = resolve_launch_date_segments(
        (
            DerivedTextSegment("상품 출시일", node_id="label", page=1, group_id="item"),
            DerivedTextSegment("2026. 08. 12", node_id="value", page=1, group_id="item"),
        )
    )
    assert resolution.launch_date == date(2026, 8, 12)
    assert {segment.node_id for segment in resolution.evidence[0].segments} == {"label", "value"}


@pytest.mark.parametrize("label", ["개정일", "시행일", "효력일", "서비스 시작일"])
def test_non_launch_dates_are_not_promoted(label: str) -> None:
    assert resolve_launch_date_texts((f"{label}: 2026.08.12",)).status == "missing"


def test_two_digit_year_is_invalid_instead_of_century_guessed() -> None:
    resolution = resolve_launch_date_texts(("상품 출시일: '26/08/12",))
    assert (resolution.launch_date, resolution.status) == (None, "invalid")


def test_distinct_valid_dates_conflict() -> None:
    resolution = resolve_launch_date_texts(("출시일 2026.08.12", "발매일 2026.08.13"))
    assert (resolution.launch_date, resolution.status) == (None, "conflicting")


def test_historical_confirmation_survives_current_omission() -> None:
    resolution = resolve_lineage_launch_date(
        (
            LaunchDateResolution(date(2024, 5, 13), "confirmed"),
            LaunchDateResolution(None, "missing"),
        )
    )
    assert (resolution.launch_date, resolution.status) == (date(2024, 5, 13), "confirmed")
