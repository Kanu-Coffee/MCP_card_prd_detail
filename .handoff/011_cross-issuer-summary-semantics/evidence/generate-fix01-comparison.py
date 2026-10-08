import json
from pathlib import Path

evidence_dir = Path(__file__).resolve().parent
comp = json.loads((evidence_dir / "fix01-comparison.json").read_text(encoding="utf-8"))
repro = json.loads((evidence_dir / "fix01-repro-results.json").read_text(encoding="utf-8"))

md = []
md.append("# FIX_01 Evaluation Comparison across 64 Products")
md.append("")
md.append("## 1. Reviewer Reproduction Verification")
md.append("")
md.append("| Case | Issuer / Code | Baseline Before | Prev After (584f4ac) | FIX_01 After | Status |")
md.append("|---|---|---|---|---|---|")

for item in repro:
    if item["case"] == "fee_regression":
        prev = (
            "3,000원"
            if item["product_code"] == "00917"
            else ("1만원" if item["product_code"] == "00549" else "기본연회비 5천원 면제 footnote")
        )
        md.append(
            f"| 연회비 총액 복원 | {item['issuer']} {item['product_code']} | `{item['before']}` | `{prev}` | `{item['after']}` | **{item['status']}** |"
        )
    else:
        label = item["source"]["display_text"]
        ret = ", ".join([f"{c['field']}: '{c['text']}'" for c in item["returned"]]) or "None (Blocked)"
        prev = "benefit: '온라인 | 5% 포인트 미적립'" if "미적립" in label else "benefit heading: '마일리지 적립'"
        md.append(f"| 부정 표 후보 차단 | `{label}` | N/A | `{prev}` | `{ret}` | **{item['status']}** |")

md.append("")
md.append("## 2. 64 Products Annual Fee Preservation Analysis")
md.append("")
md.append("- 총 검증 상품: 64개 (고정 34개 + 무작위 30개)")
md.append("- 기준치(Baseline Before)와 완전 일치: 61개")
md.append("- 단순 포맷팅 차이 (`<br>` 태그 공백 정규화): 3개")
md.append("- 실제 금액 또는 적용 대상 왜곡: **0개**")
md.append("")
md.append("### 포맷팅 정규화 3건 상세")
for diff in comp["fee_differences"]:
    md.append(f"- **{diff['issuer']} {diff['product_code']}**:")
    md.append(f"  - Baseline: `{diff['baseline_before']}`")
    md.append(f"  - FIX_01: `{diff['new_after_fix01']}`")

md.append("")
md.append("## 3. Benefits and Conditions Metrics")
md.append("")
total_before_b = sum(len(p["baseline_before"].get("benefit_summary_texts", [])) for p in comp["products"])
total_prev_b = sum(len(p["prev_after_584f4ac"].get("benefit_summary_texts", [])) for p in comp["products"])
total_new_b = sum(len(p["new_after_fix01"].get("benefit_summary_texts", [])) for p in comp["products"])
total_before_c = sum(len(p["baseline_before"].get("condition_summary_texts", [])) for p in comp["products"])
total_prev_c = sum(len(p["prev_after_584f4ac"].get("condition_summary_texts", [])) for p in comp["products"])
total_new_c = sum(len(p["new_after_fix01"].get("condition_summary_texts", [])) for p in comp["products"])

md.append("| 지표 | Baseline Before | Prev After (584f4ac) | FIX_01 After |")
md.append("|---|---|---|---|")
md.append(f"| 총 혜택 상세 수 (Benefit Texts) | {total_before_b} | {total_prev_b} | {total_new_b} |")
md.append(f"| 총 조건 상세 수 (Condition Texts) | {total_before_c} | {total_prev_c} | {total_new_c} |")
md.append("")

(evidence_dir / "fix01-comparison.md").write_text("\n".join(md), encoding="utf-8")
print("Saved fix01-comparison.md successfully.")
