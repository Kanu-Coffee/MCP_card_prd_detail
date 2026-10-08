from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from cardrag_core.derived_metadata import LaunchDateResolution

from cardrag_mcp.catalog import CatalogRepository
from cardrag_mcp.summary_fields import summary_candidates

ROOT = Path(__file__).resolve().parents[3] / ".handoff/011_cross-issuer-summary-semantics/evidence"
CASES = json.loads((ROOT / "reported-four-baseline.json").read_text())["products"]


def summary(case):
    row = case["summary"]
    pages = {}
    for span in case["spans"]:
        pages.setdefault((row["contract_revision_id"], span["node_id"]), set()).add(span["page"])
    return CatalogRepository._summary(
        SimpleNamespace(generation_id=row["generation_id"]),
        row,
        case["nodes"],
        pages,
        LaunchDateResolution(None, "missing"),
        (),
        (),
    ).model_dump(mode="json")


@pytest.mark.parametrize("case", CASES, ids=["airport", "miles", "billing-discount", "tier-table"])
def test_real_catalog_semantics_and_source_evidence(case):
    result = summary(case)
    benefits, conditions = result["benefit_summary_texts"], result["condition_summary_texts"]
    assert not set(benefits) & set(conditions)
    assert result["benefit_headings"]
    assert all(
        len(result[field]) <= 5
        for field in ["benefit_headings", "benefit_summary_texts", "condition_summary_texts"]
    )
    nodes = {n["node_id"]: n for n in case["nodes"]}
    for evidence in result["evidence"]:
        source = " ".join(nodes[evidence["node_id"]]["display_text"].split())
        assert evidence["excerpt"] in source
        assert evidence["pages"]
    code = case["summary"]["product_code"]
    if code == "01208":
        assert any("10~15%" in b for b in benefits)
        assert any("1일 1회" in c for c in conditions)
        assert any("2인까지" in c for c in conditions)
    elif code == "00917":
        assert any("1,000원당 1마일" in b for b in benefits)
        assert not any("1마일" in c for c in conditions)
        assert any("무이자" in c and "제외" in c for c in conditions)
    elif code == "09063":
        assert any("주유" in b and "60원" in b for b in benefits)
        assert not any("구간" in b for b in benefits)
        assert any("30만원 이상" in c for c in conditions)
        assert not any("예시" in h for h in result["benefit_headings"])
    else:
        assert any("페이북" in b and "1천원 청구할인" in b for b in benefits)
        assert not any(b == "### 1천원 할인" for b in benefits)
        assert any("1만원 이상" in c for c in conditions)


def node(text, kind="PARAGRAPH", **values):
    return dict(
        dict(node_id="n", node_type=kind, display_text=text, parent_id=None, raw_heading=None),
        **values,
    )


@pytest.mark.parametrize(
    "text", ["[예시: 10% 할인]", "※ 예시) 2천원 캐시백", "30,000원 X 12개월 = 360,000포인트 적립"]
)
def test_examples_are_not_real_offers(text):
    assert not summary_candidates([node(text)])


def test_mixed_offer_preserves_source_limits_without_repeating_whole_offer():
    text = "전월 실적 30만원 이상 이용 시 2% 할인(무이자 할부 제외), 월 2회"
    candidates = summary_candidates([node(text)])
    assert any(c.field == "benefit" and c.text == text for c in candidates)
    conditions = [c.text for c in candidates if c.field == "condition"]
    assert any("30만원 이상" in t for t in conditions)
    assert "월 2회" in conditions and "무이자 할부 제외" in conditions
    assert text not in conditions


def test_column_roles_negative_status_and_malformed_metadata():
    row = node(
        "| 식당 | × |",
        kind="TABLE_ROW",
        table_headers_json='["서비스","혜택"]',
        table_cells_json='["식당","×"]',
        table_role="BODY",
    )
    assert not any(c.field == "benefit" for c in summary_candidates([row]))
    row.update(
        display_text="| 커피 | 5% |", table_headers_json="bad JSON", table_cells_json="bad JSON"
    )
    assert isinstance(summary_candidates([row]), list)
    row.update(table_role="HEADER")
    assert not summary_candidates([row])


def test_exclusion_list_context_does_not_invent_interest_free_service():
    intro = node("서비스 제외 대상은 아래와 같습니다.", node_id="intro")
    item = node("- 무이자 할부 이용금액", kind="LIST_ITEM")
    candidates = summary_candidates([intro, item])
    assert not any(c.field == "benefit" for c in candidates)
    assert any(c.field == "condition" and "무이자" in c.text for c in candidates)


def test_mileage_offer_without_provide_verb_is_not_only_a_condition():
    text = "전 가맹점 이용금액 1,500원당 1마일 - 전월 실적 30만원 이상 이용 시"
    candidates = summary_candidates([node(text)])
    assert any(c.field == "benefit" and "1마일" in c.text for c in candidates)
    assert any(c.field == "condition" and "30만원 이상" in c.text for c in candidates)
    assert not any(c.field == "condition" and c.text == text for c in candidates)
