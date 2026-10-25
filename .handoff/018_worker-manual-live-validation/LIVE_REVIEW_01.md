# 우리카드 첫 라이브 실행 검토 — 정상 완료 아님

2026-10-19 사용자 수동 실행 `cardrag-live-woori-20261019T063550Z` 검토. 컨테이너 exit=1, run `6266b015df8c496586fe6a55362edd09` status=failed. 15:36:03–15:41:04 KST 실행.

## 확인 사실

- orphan 안내의 `cardrag-eval-016-d8a9018`는 10월 16일 종료(exit=0)한 이전 release 평가용 one-off 컨테이너다. 현재 실패 원인이 아니며 정리하지 않아도 수집 실행에 영향 없다. `--remove-orphans`로 일괄 제거하지 않았다.
- 시작 용량/OCR resolver/tokenizer/Qwen endpoint/embedding 검증은 통과했다.
- selected=[woori]. 우리 목록 920건, discovery warnings=0. 비선택 7사는 not_scheduled.
- PDF 최종 로그: cache_hits=915, cache_misses=5, downloads=1, revisions=0, revalidations=0, unsupported=4. 전체 920건을 재다운로드한 것이 아니다. 다운로드 바이트는 이 로그로 확인 불가.
- 수집 이후 scope 단계에서 `issuer_scope_revision_source_missing`으로 blocked. 단계 기록에는 discovery/download만 있으며 OCR·publication·이번 run 백업 완료 근거가 없다.
- 기존 stable generation `g-f1871a51eff549ea9aee95b7-76c89918e569` 유지. 8사 총 5536문서(우리 979), 비선택 문서 변경 0. 신규 게시 문서 0. 이번 실행 backup_run_recorded=false, 전역 backup_pending=[]. pending이 없다는 사실은 이번 실행의 백업 성공을 의미하지 않는다.

## 수정 필요 — 운영 기준 baseline과 선택 실행 보존 경로 호환

`issuer_scope.py:preserve_inputs`는 기준 seal의 `v5_metrics.historical_revision_unresolved_identities`를 순회하면서 `_known_snapshot_sources`에서 source_id를 찾지 못하면 위 오류를 발생시킨다. 해당 lookup은 선택 카드사 여부를 판별하기 전이다. 현재 운영 baseline에서 이 조건이 실제 발생했다. 누락 identity의 카드사/유래 및 seed·snapshot history와 baseline seal의 차이는 추가 조사 대상이며, 단순히 skip하거나 검증을 제거해서는 안 된다.

Executor는 실제 baseline의 실패 identity를 읽기 전용으로 재현하고, 보존에 필요한 source identity를 검증 가능한 sealed/영속 자료에서 복구하는 정책을 설계해야 한다. 선택·비선택 양쪽 미해결 이력과 기존 운영 seal 호환성을 테스트하고, 비선택 serving/이력 보존과 비선택 OCR·provider 미호출을 확인해야 한다. 제품 수정·이미지 재적용 후 우리카드부터 재검증한다. 이번 리뷰에서 제품 코드 변경이나 재실행은 하지 않았다.

라이브 성공 판정과 다음 7사 검증은 보류한다. timer는 중지 상태를 유지한다. 실행 및 결과 증거는 host release operations/live의 우리 metadata/result JSON과 Docker 로그에 남아 있다.
