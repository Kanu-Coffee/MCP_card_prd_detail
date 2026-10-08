from __future__ import annotations

import json
from pathlib import Path

import pytest

from cardrag_mcp.summary_fields import bounded_source_text, summary_candidates


def node(text, *, kind="PARAGRAPH", parent=None, heading=None, node_id="n"):
    return {
        "node_id": node_id,
        "node_type": kind,
        "display_text": text,
        "parent_id": parent,
        "raw_heading": heading,
        "major_class": "MIXED",
        "table_role": None,
    }


@pytest.mark.parametrize(
    ("text", "benefit", "condition"),
    [
        ("조건, 한도 없이 든든한 1.2% 할인", True, False),
        ("전월 실적 조건 없음(할인한도 제한 없음)", False, True),
        ("전월 30만원 이상 이용 시 1.2% 할인", True, True),
        ("실적 조건 없이 월 1만원까지 할인", True, True),
        ("공항 라운지 연 2회 무료 제공", True, True),
        ("전월 실적 제외 항목", False, True),
        ("월 할인한도: 2만원", False, True),
        ("통합 월 캐시백 한도", False, True),
        ("이용금액은 캐시백 대상에서 제외됩니다", False, True),
        ("캐시백 금액은 당사 매출접수일 이후 지급됩니다", False, False),
        ("생활 5% 캐시백 서비스", True, False),
        ("무이자 할부 이용건은 할인 혜택 제공하지 않음", False, False),
        ("무이자 할부 거래 연체 시: 유이자 할부금리 적용", False, False),
        ("상품 출시일 및 부가서비스 변경 안내", False, False),
        ("'할인 제공 시 유의사항' 참고", False, False),
    ],
)
def test_semantic_classification(text, benefit, condition):
    fields = {c.field for c in summary_candidates([node(text)])}
    assert ("benefit" in fields) is benefit
    assert ("condition" in fields) is condition


def test_notice_parent_fee_table_and_cyclic_parent():
    nodes = [
        node(
            "상품 출시일 및 부가서비스 변경 안내",
            kind="MAJOR_SECTION",
            heading="상품 출시일 및 부가서비스 변경 안내",
            node_id="h",
        ),
        node("다른 부가서비스 제공이 불가한 경우", parent="h"),
        node("각종 수수료 및 이자, 연회비", node_id="bad"),
        node("연회비 안내", kind="MAJOR_SECTION", heading="연회비 안내", node_id="fee"),
        node("| 본인카드 | 15,000원 |", kind="TABLE_ROW", parent="fee", node_id="good"),
        node("국내외 1.2% 할인", kind="FOOTNOTE", parent="cycle", node_id="cycle"),
    ]
    candidates = summary_candidates(nodes)
    assert not any(c.node_id in {"h", "n", "bad"} for c in candidates)
    assert any(c.node_id == "good" and c.field == "annual_fee" for c in candidates)
    assert any(c.node_id == "cycle" and c.field == "benefit" for c in candidates)


def test_real_solid_nodes():
    path = (
        Path(__file__).resolve().parents[3]
        / ".handoff/009_product-summary-field-classification/evidence/current-summary-sample.json"
    )
    nodes = json.loads(path.read_text())["target_nodes"]
    candidates = summary_candidates(nodes)
    benefits = [c.text for c in candidates if c.field == "benefit"]
    assert any("1.2%" in text for text in benefits)
    assert not any("변경" in text for text in benefits)
    assert any("15,000원" in c.text for c in candidates if c.field == "annual_fee")
    assert not any(
        c.text == "조건, 한도 없이 든든한 1.2% 할인" for c in candidates if c.field == "condition"
    )


def test_long_sentence_does_not_lose_tail_negation():
    assert bounded_source_text("할인 " + "설명 " * 100 + "제외", 180) is None
