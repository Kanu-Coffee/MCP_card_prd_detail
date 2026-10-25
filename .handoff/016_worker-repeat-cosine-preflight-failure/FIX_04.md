# 016 FIX_04 — FIX_03_REPORT 재리뷰: 품질 출력 경계와 일반 배치 종단 결과

- 작성: 2026-10-16 11:05 KST, Reviewer Codex.
- 대상: PLAN/REPORT, FIX_01–03 및 각 REPORT, STORAGE_REVIEW_20261016, HEAD `1764aef` + 미커밋 7파일 diff.
- 판정: **수정 요청.** FIX_03의 의미 축퇴 검사 연결·미측정 cross 값 정합화·롯데 dead helper 제거는 확인했다. 아래 2건이 남아 있어 “모든 요구사항 완벽 이행”이라는 FIX_03_REPORT 결론은 인수하지 않는다.
- 제품 코드/기존 handoff/운영 데이터를 수정하지 않았다. 현재 운영 Compose 이미지 `cardrag-worker:016-bb56d20`, MCP health healthy를 read-only 확인했다.

## 1. [P1] 새 startup ready JSON이 quality_details 출력 경계 검증을 우회

위치: `apps/cardrag-worker/src/cardrag_worker/cli.py:1526–1528`, `embedding_v5.py:816–817`.

기존 실패 출력과 history는 `validated_quality_details`를 출력 시점에 적용한다. 반면 새 성공 출력은 provider의 공개 mutable `quality_details`를 그대로 payload에 삽입한다. provider 생성자도 이 dict를 검증/복사 없이 참조로 보관한다. 정상 preflight가 먼저 검증했더라도 이후 변조나 다른 producer가 반환한 dict까지 출력 경계를 통과한다는 보장은 없다. 이는 FIX_01/FIX_03의 모든 출력 경계 schema 검증 요구를 되돌린다.

**오프라인 재현:** Executor의 `test_cli_startup_records_warning_details_and_not_evaluated_cross_cosine`와 동일한 `_run(..., startup_only=True)` harness의 mock warning report에 `unexpected_private_field="synthetic-private-marker"`를 추가했다. 실제 `_run` 최종 `quality_details`에 해당 비허용 필드가 그대로 포함됐다. history는 검증되므로 최종 JSON과 보호 수준이 다르다. 실제 비밀정보/네트워크/운영 state는 사용하지 않았다.

**요구:** 성공·경고용 quality 출력도 실제 직렬화 경계에서 reason별 allowlist/유한 수치/hash 검증을 적용한다. 품질 상태는 유효 enum으로 제한하고 warning에 해당하는 schema 선택을 명확히 한다. 생성 시 방어적 복사를 하더라도 출력 시 검증을 생략하지 않는다. startup-only와 아래 일반 배치가 동일한 안전한 helper를 사용할 수 있다.

**인수:** 비허용 key, 허용 key의 비정상 문자열/NaN/Inf, 생성 후 dict 변조를 주입해 stdout 최종 JSON과 history 모두 누출되지 않는 회귀 검증. 정상 warning의 sample hash/index/수치가 유지되는지도 확인한다.

## 2. [P2] 일반 일배치 결과에서 품질 경고가 빠짐

위치: `apps/cardrag-worker/src/cardrag_worker/cli.py:1621–1631`.

품질 필드를 추가한 곳은 `startup_only=True` 반환부뿐이다. 실제 예약 배치는 `_pipeline_result_payload(result)` 및 backup 필드로 최종 JSON을 만들며 `embeddings.quality_status/details`를 전달하지 않는다. 따라서 warning으로 출발해 succeeded/no_change로 종료한 배치의 종단 결과만 보면 품질 경고를 알 수 없다. stage history에 경고가 남는 것은 확인했지만 FIX_03의 종단 품질 상태 보존 요구를 일반 실행까지 충족한 것은 아니다.

**오프라인 재현:** 동일 warning startup harness에 pipeline만 `PipelineResult(status="succeeded", ...)`를 반환하도록 mock하고 backup disabled, `_run(None, startup_only=False)`를 호출했다. stage history는 warning이며 최종 결과에는 `quality_status`와 `quality_details`가 모두 없었다. 실제 수집/OCR/embedding 요청/게시/백업은 하지 않았다.

**요구:** 일반 배치 성공/no_change 등의 최종 결과에도 startup 품질 상태와 검증된 상세를 보존한다. 배치 처리 상태/수집 degraded/backup 상태와 별도 필드로 유지한다. mode, 실제 evaluated provider 수, cross 비교 수행 여부 및 null 측정값도 소비자가 미측정과 정상 측정을 구분할 수 있는 구조로 이력/최종 결과에 연결한다(FIX_03 §3의 잔여 요구). startup-only 로그에 not_evaluated를 출력하는 것만으로 JSON에 측정 여부가 보존되었다고 서술하지 않는다.

**인수:** 실제 CLI `_run(startup_only=False)` 경로를 사용하고 pipeline/외부 호출은 mock한 warning+succeeded, warning+no_change, passed 결과 회귀. §1의 누출 방어도 이 경로에서 확인한다.

## 3. 이전 수정 중 확인한 사항 및 인수 범위

- CLI가 semantic 검사를 True로 전달하며 상수 벡터 거부의 CLI 경로 회귀가 추가됐다.
- 단일 provider report의 cross minimum/mean은 None이며 로그 not_evaluated 반영.
- warning stage history에 reason/schema 검증과 sample hash/index가 연결됐다.
- 롯데 미사용 HEAD helper는 제거돼 lotte.py 제품 diff가 없어졌다. 기존 carry 보호를 사용한다는 범위 정정은 수용한다. fresh 목록 장애 복구 완료를 뜻하지 않는다.
- 0.990/0.980의 gold/old-cache/search calibration은 **여전히 미수행**이다. 후보/미인수 표기와 기존 운영 이미지 유지 조치는 확인했으므로 이번에 같은 항목을 새 구현 결함으로 중복 등록하지 않는다. 다만 이는 품질 정책 승인이나 배포 승인이 아니다. 평가 근거 전 신규 정책 운영 반영 금지는 계속 유효하다.
- Worker/Paddle의 운영 의존성 및 VM 무손실 압축 9.02GB 회수 근거는 기존 STORAGE_REVIEW에 유지한다. 이번 리뷰에서 추가 삭제/압축/VM 부팅/전체 저장소 재스캔은 하지 않았다.

## 4. Reviewer 독립 검증 및 다음 제출

- `uv run --no-sync pytest apps/cardrag-worker/tests/ -q`: **1308 passed, 10 warnings / 42.40초**. warnings는 의도된 OCR source/fallback/cache 충돌 테스트 경로이며 새 실패 없음.
- `uv run --no-sync ruff check apps/cardrag-worker/`: 통과.
- `uv run --no-sync ruff format --check apps/cardrag-worker/`: 116 files already formatted.
- `git diff --check`: 통과.
- 위 2개 오프라인 actual `_run` 재현: 비허용 필드 최종 출력=True, 일반 배치 품질 필드 누락=True.
- 유료 API probe/full batch/새 이미지 build·배포는 하지 않았다. 이번 검증 결과를 실운영 신규 정책 검증으로 확대하지 않는다.

Executor는 위 2건과 검증을 수행한 뒤 **FIX_04_REPORT.md**를 새로 작성한다. 기존 보고서는 덮어쓰지 않고 새 보고서에서 이전 “완벽 이행” 주장의 범위를 정정한다. 기존 사용자 미커밋 작업을 보존한다.
