# 016 FIX_04 Reviewer 인수 — 구현 수정 완료, 품질 정책 운영 적용 보류

- 작성: 2026-10-16, Reviewer Codex.
- 기준: HEAD `1764aef` + 현재 미커밋 7파일 변경, FIX_04/FIX_04_REPORT 및 기존 016 계획·실행·수정 이력.
- 판정: **FIX_04의 구현 수정 2건 인수. 이번 검토에서 추가 수정이 필요한 결함은 발견하지 않았다. FIX_05는 발행하지 않는다.**

## 1. 수정 사항 확인

### P1 — 품질 상세정보 출력 경계

`cli.py:_project_embedding_quality_payload`가 warning 상세를 출력 직전에 `validated_quality_details("repeat_cosine_warning", ...)`로 다시 검증한다. startup-only 및 일반 배치 반환부 모두 이 helper를 사용한다. Provider 생성자에도 상세 검증·방어적 복사가 추가됐고 startup history의 schema 검증은 유지된다.

신규 회귀에서 비허용 key/문자열, NaN/Inf, 부적합 hash 및 사후 변조 필터링을 확인했다. Reviewer도 helper에 비허용 필드, NaN, 부적합 hash와 생성 후 변조를 별도로 주입해 최종 출력에서 제거되고 정상 cosine/count는 유지됨을 확인했다. 이전 FIX_04에서 재현한 무검증 상세 출력 경로는 해소됐다.

### P2 — 일반 배치 종단 품질 경고

일반 `_run(startup_only=False)` 결과에도 공통 helper의 품질 필드가 병합된다. succeeded/no_change의 처리 상태와 warning 품질 상태가 별도로 유지되고, 정상 passed 결과에는 warning 상세가 붙지 않는다. mode/provider count/cross_evaluated 및 미측정 minimum cross=null이 최종 결과와 stage history에 연결됐다.

실제 `_run` 경로를 사용하고 pipeline/외부 호출을 mock하는 신규 succeeded/no_change/passed 회귀가 전체 테스트에 포함돼 통과했다. 단일 provider 미측정 cross 값은 helper 독립 재현에서도 None을 유지했다.

## 2. 독립 검증

- `uv run --no-sync pytest apps/cardrag-worker/tests/ -q`: **1313 passed, 10 warnings in 42.40s**.
- warnings는 OCR prior source/fallback/cache 충돌 테스트 경로이며 테스트 실패 없음.
- `uv run --no-sync ruff check apps/cardrag-worker/`: **All checks passed**.
- `uv run --no-sync ruff format --check apps/cardrag-worker/`: **116 files already formatted**.
- `git diff --check`: 통과.
- 무네트워크 helper 독립 재현: 비허용 상세/유한하지 않은 수치/부적합 hash/사후 변조 제거, warning 및 미측정 상태 보존.
- 현재 운영 Compose: `cardrag-worker:016-bb56d20`; MCP container health: **healthy**. 신규 코드가 운영에 배포됐다고 판단한 것이 아니다.

## 3. 인수 범위와 남은 작업

이번 인수는 **FIX_04 코드 결함 수정 및 검증 범위**다. 기존 FIX_03/FIX_04의 다음 보류 사항은 계속 유효하다.

1. **0.990 repeat hard cutoff와 0.980 semantic threshold는 후보/미인수 정책이다.** old-cache/new-vector anchor 및 업무 gold의 Recall/nDCG/MRR 평가가 없으므로 새 품질 정책 운영 적용은 보류한다. 문서의 후보 표기 및 기존 운영 이미지 유지 조건을 보존한다. 테스트 통과가 정책 calibration을 대체하지 않는다.
2. **롯데 fresh 목록 조회 장애는 미복구다.** 공식 PDF CDN 접근 가능과 기존 647개 carry 보호는 신규 상품 탐색·fresh 수집 복구의 증거가 아니다. 이전 dead helper 제거와 범위 정정에 대한 판정은 유지한다.
3. Worker/Paddle 운영 자료 보존과 VM 내용 보존 압축의 회수 결과는 `STORAGE_REVIEW_20261016.md`를 따른다. 이번 리뷰에서 추가 자원 삭제나 VM 부팅을 수행하지 않았다.

새 이미지 build/배포, 실제 유료 embedding 호출, full batch/OCR/게시/증분백업 재검증은 이번 Reviewer가 수행하지 않았다. 따라서 오늘 기존 성공 run과 별도로 새 코드의 실기동 성공이나 증분백업 완료를 주장하지 않는다. 사용자 미커밋 작업과 기존 handoff 문서를 모두 보존했다.
