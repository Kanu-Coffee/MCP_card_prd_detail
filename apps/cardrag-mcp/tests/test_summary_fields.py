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


@pytest.mark.parametrize("text", ["1.2% 할인 대상에서 제외", "5% 적립 미적용", "2% 캐시백 불가"])
def test_negated_rate_is_not_a_benefit(text):
    assert not any(c.field == "benefit" for c in summary_candidates([node(text)]))


@pytest.mark.parametrize(
    "text", ["국내 1.2% 할인(무이자 할부 제외)", "전월 30만원 이상 5% 캐시백", "실적 없이 2% 적립"]
)
def test_positive_rate_survives_conditions_and_exceptions(text):
    assert any(c.field == "benefit" for c in summary_candidates([node(text)]))


@pytest.mark.parametrize(
    "heading,expected",
    [
        ("국내외 할인", True),
        ("포인트 적립", True),
        ("캐시백 혜택", True),
        ("할부금리", False),
        ("할인금리", False),
        ("수수료 안내", False),
        ("통계", False),
    ],
)
def test_rate_only_table_uses_offer_context(heading, expected):
    nodes = [
        node(heading, kind="MAJOR_SECTION", heading=heading, node_id="h"),
        node("| 국내 | 1.2% |", kind="TABLE_ROW", parent="h"),
    ]
    assert (
        any(c.node_id == "n" and c.field == "benefit" for c in summary_candidates(nodes))
        is expected
    )


_EVIDENCE = (
    Path(__file__).resolve().parents[3]
    / ".handoff/009_product-summary-field-classification/evidence"
)
_REAL_FIXTURES = json.loads((_EVIDENCE / "fix01-product-fixtures.json").read_text())["fixtures"]
_REAL_SUMMARIES = json.loads((_EVIDENCE / "fix01-summary-sample.json").read_text())["summaries"]


@pytest.mark.parametrize(
    "fixture", _REAL_FIXTURES, ids=lambda f: f["issuer"] + ":" + f["product_code"]
)
def test_multiple_real_products_preserve_source_candidates_and_evidence(fixture):
    candidates = summary_candidates(fixture["nodes"])
    summary = next(
        s for s in _REAL_SUMMARIES if s["contract_revision_id"] == fixture["contract_revision_id"]
    )
    by_id = {n["node_id"]: n for n in fixture["nodes"]}
    for evidence in summary["evidence"]:
        if evidence["field"] == "launch_date":
            assert evidence["contract_revision_id"] in summary["launch_date_source_revision_ids"]
            continue
        assert evidence["contract_revision_id"] == fixture["contract_revision_id"]
        assert evidence["node_id"] in by_id
        assert evidence["pages"]
        assert " ".join(evidence["excerpt"].split()) in " ".join(
            by_id[evidence["node_id"]]["display_text"].split()
        )
    for text in summary["benefit_summary_texts"]:
        assert any(c.field == "benefit" and c.text == text for c in candidates)
    assert not any(
        "상품 출시일 및 부가서비스 변경 안내" in text for text in summary["benefit_headings"]
    )


@pytest.mark.parametrize(
    "issuer,code,expected",
    [
        ("woori", "500107", "1.2%"),
        ("woori", "104022", "0.8%"),
        ("woori", "104023", "5%"),
        ("hana", "15911", "캐시백"),
        ("hana", "15758", "바우처"),
        ("hyundai", "149298", "M포인트"),
        ("kb", "04404", "단체보험"),
        ("lotte", "1118", "5~7%"),
        ("samsung", "AAP1920--v-bea85425b2934e8f", "10%"),
        ("shinhan", "00368", "15%"),
    ],
)
def test_diverse_real_benefit_types(issuer, code, expected):
    fixture = next(f for f in _REAL_FIXTURES if (f["issuer"], f["product_code"]) == (issuer, code))
    assert any(
        c.field == "benefit" and expected in c.text for c in summary_candidates(fixture["nodes"])
    )
