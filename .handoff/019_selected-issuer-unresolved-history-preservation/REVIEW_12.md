# 019 REVIEW_12 — FIX_24 진단 보완 검증 통과

2026-10-21 Codex, Reviewer. FIX_24/REPORT, LIVE_REVIEW_09, 이전 FIX_23/REPORT 및 REVIEW_11의 정책과 현재 Git diff/source/tests를 대조했다. 기존 대규모 미커밋 변경을 보존했고 구현을 추가 수정하지 않았다.

## 판단

**FIX_24 검증 통과. 배포를 막는 추가 수정 사항은 발견하지 않았다.** 미분류 provider 종료의 관측성 보완으로 판단하며, 실제 upstream 원인 해결이나 롯데 완주 승인과 구별한다. 추가 FIX는 발행하지 않는다.

- Codex/Paddle nonzero 종료는 provider와 stdout/stderr 길이·해시, 정해진 분류 채널을 전달한다. Codex stdout을 일반 텍스트 검색하여 quota로 오인하지 않으며 exit0은 stderr의 오류 문구만으로 실패 처리하지 않는다.
- OpenCode는 기존 stdout 전체 검색을 error JSON event로 좁혔다. 일반 text event의 OCR 본문은 분류에서 제외한다. 현재 Codex 운영 경로와 별개인 이 변경도 관련 provider 회귀로 확인했다.
- OCRResolver의 문서/chunk/보정 호출에서 검증된 provider·target page 범위를 부착한다. FailoverOCRResolver는 fallback 실패의 phase를 fallback으로 바꾸며, 기존 quota 전용 Codex→로컬 Paddle 예외와 unknown 차단을 유지한다. fallback 실패/취소를 성공으로 숨기지 않는다.
- 진단값은 검증 후 canonical record/CLI로 전달하며, 선택 진단 손상 시 기존 reason/exit/stderr를 보존한다. 원시 출력·예외 text를 보고서로 옮기지 않는다. 신규 secret sentinel 테스트와 기존 회귀를 확인했다.
- 기존 선택 phase를 provider 요청에도 사용하는 것은 기존 cache publication phase와 분기상 충돌하지 않는다. 신규 필드가 없는 기존 record payload도 유지한다. 실제 record 위치가 pipeline.py인 점을 반영한 구현 차이는 타당하다.
- settings/model/cache epoch/OCR contract/체크포인트 형식 변경이나 DB migration은 없다. 최신517건을 예전 snapshot으로 되돌리는 처리는 포함되지 않는다.

## Reviewer 직접 검사

```text
uv run --no-sync pytest apps/cardrag-worker/tests/test_fix_24_provider_diagnostics.py apps/cardrag-worker/tests/test_fix_23_quota_fallback.py apps/cardrag-worker/tests/test_cli_settings_provider.py apps/cardrag-worker/tests/test_ocr.py -q
281 passed, 1 warning in 4.01s

uv run --no-sync pytest apps/cardrag-worker/tests/ packages/cardrag-core/tests/ -q
1819 passed, 11 warnings in 58.02s

uv run --no-sync ruff check apps/cardrag-worker/ packages/cardrag-core/
All checks passed!
uv run --no-sync ruff format --check apps/cardrag-worker/ packages/cardrag-core/
159 files already formatted
git diff --check
통과
```

경고는 손상 캐시/원격 충돌/의도된 fallback fixture의 RuntimeWarning이다. 검사에는 실제 외부 OCR/카드사 호출을 추가하지 않았다.

## 운영 경계 및 남은 제한

운영 current는 여전히 `/opt/cardrag/019-fix23-e9b9cdca1a22`이며 실행 중 Worker container는 없다. FIX_24는 아직 운영 이미지에 반영되지 않았다. 이번 Reviewer 턴에는 배포·resume·timer 활성화나 운영 DB/volume/serving 변경을 하지 않았다. 롯데517건/174개 checkpoint는 LIVE_REVIEW_09의 마지막 검증값이며 이번 코드 검사를 운영 데이터 재검증으로 표현하지 않는다.

FIX_24를 배포할 때 식별 가능한 source/image 일치, 동일 run `49845932e05849d3a0110a2bc82d6c1c`/contract/실제 운영 volume, 실행 lock·최신 독립 복구점·저장공간과 기존 완료 결과 보존을 확인한 뒤 재개한다. unknown 자체는 여전히 terminal이므로 재발 가능성이 남는다. 기존 실패의2512바이트 stderr를 되살리거나 원인을 확정한 변경이 아니다. 롯데/신한 운영 완주와019 최종 인수는 열린 상태다.
