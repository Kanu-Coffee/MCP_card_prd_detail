from __future__ import annotations

from datetime import date

import pytest

from cardrag_mcp.launch_date import parse_launch_date, resolve_launch_date


@pytest.mark.parametrize(
    "text",
    [
        "상품 출시일: 2026.09.01",
        "상품 출시일자 : 2026년 09월 01일",
        "카드 신규 출시(2026년 9월 1일) 이후 3년 이상 유지됩니다.",
        "카드 신규출시 : 2026.9.1",
        "카드(2026년 09월 01일 출시)",
        "카드(2026.09.01 출시)",
        "2026.09.01 출시",
        "2026년 9월 1일 출시",
        "상품 출시일：2026.09.01",
    ],
)
def test_documented_launch_date_formats(text: str) -> None:
    assert parse_launch_date(text) == date(2026, 9, 1)


@pytest.mark.parametrize(
    "text",
    [
        "상품 출시일: 2026.09.99",
        "상품 출시일: 2026.09.101",
        "상품 출시일: 2026.09.001",
        "상품 출시일: 2026.009.01",
        "상품 출시일: 2026.02.31",
        "상품 출시일: 2026.00.01",
        "상품 출시일: 2026.13.01",
        "상품 출시일: 2026.09.00",
        "상품 출시일: 2026.09.01.5",
        "상품 출시일: 2026.09.01abc",
        "상품 출시일: 2026년 09월 99일",
        "상품 출시일: 2026년 09월 101일",
        "상품 출시일: 2026년 02월 31일",
        "상품 출시일: 2026년 13월 01일",
        "상품 출시일: 2026년 09월 001일",
        "상품 출시일: 2026년 09월 101",
        "카드(2026.09.99 출시)",
        "카드(2026.09.101 출시)",
        "카드(12026.09.01 출시)",
        "카드(12026년 09월 01일 출시)",
        "상품 출시일: 2026.02.29",
        "상품 출시일: 0000.01.01",
    ],
)
def test_invalid_dates_are_never_prefix_parsed(text: str) -> None:
    assert parse_launch_date(text) is None


def test_leap_day_requires_a_valid_calendar_date() -> None:
    assert parse_launch_date("상품 출시일: 2024.02.29") == date(2024, 2, 29)


@pytest.mark.parametrize(
    "text",
    ["", "국내외 모든 가맹점 0.8% 청구할인", "개정일: 2026.09.01", "2026.09.01"],
)
def test_missing_launch_date_is_unknown(text: str) -> None:
    assert parse_launch_date(text) is None


@pytest.mark.parametrize(
    "texts",
    [
        ["출시일: 2026.09.01 / 출시일: 2026.08.01"],
        ["출시일: 2026년 09월 01일 / 출시일: 2026.08.01"],
        ["출시일: 2026.09.01", "카드(2026년 08월 01일 출시)"],
    ],
)
def test_conflicting_dates_remain_unknown(texts: list[str]) -> None:
    assert resolve_launch_date(iter(texts)) is None
    assert parse_launch_date("\n".join(texts)) is None


def test_duplicate_dates_are_consistent_across_nodes() -> None:
    texts = [
        "출시일: 2026.09.01 / 출시일: 2026년 09월 01일",
        "카드(2026년 9월 1일 출시)",
        "개정일: 2026.09.09",
    ]
    assert resolve_launch_date(iter(texts)) == date(2026, 9, 1)
    assert parse_launch_date("\n".join(texts)) == date(2026, 9, 1)


@pytest.mark.parametrize(
    "invalid",
    ["출시일: 2026.09.99", "출시일: 2026.09.101", "출시일: 2026년 02월 31일"],
)
def test_invalid_candidate_is_not_overridden_by_another_date(invalid: str) -> None:
    valid = "출시일: 2026.09.01"
    assert resolve_launch_date([invalid, valid]) is None
    assert resolve_launch_date([valid, invalid]) is None
    assert parse_launch_date(f"{invalid} / {valid}") is None


def test_no_nodes_have_no_launch_date() -> None:
    assert resolve_launch_date([]) is None
