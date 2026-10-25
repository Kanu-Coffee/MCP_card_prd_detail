# 019 조사 기록 — 선택 실행과 기존 미해결 개정 이력의 호환

2026-10-19 Planner. 제품 수정·배포·origin 호출·Worker 재실행 없이 조사했다. HEAD `aaeb2c4`에 017 구현과 테스트 등이 미커밋으로 존재한다. Git status/diff를 확인했고 기존 변경은 보존했다. 사용자 제공 전역 AGENTS 지침을 적용했으며 현재 저장소와 상위 프로젝트 경로에서 별도 AGENTS.md/GEMINI.md는 발견하지 못했다.

## 확정 사실

- 018의 `LIVE_REVIEW_01.md`와 운영 컨테이너 로그/결과가 근거다. 실행 `6266b015df8c496586fe6a55362edd09`, 컨테이너 `cardrag-live-woori-20261019T063550Z`, 신규 이미지 `cardrag-worker:017-fca5ef313a0b`에서 exit=1, scope/issuer_scope_revision_source_missing으로 실패했다. 시작 검증 및 우리 목록 920건 수집은 끝났고 OCR/게시/이번 run 백업 성공 증거는 없다.
- 기존 stable generation은 `g-f1871a51eff549ea9aee95b7-76c89918e569`, 대응 baseline run은 `f1871a51eff549ea9aee95b73eaad99d`다. 8사 5536문서 유지, 우리 979문서. 비선택 문서 변경 0, backup_run_recorded=false. timer inactive.
- `issuer_scope.preserve_inputs`가 baseline seal의 historical_revision_unresolved_identities를 읽고 모든 entry에 대해 `_known_snapshot_sources`의 완전한 SourceRecord를 먼저 요구한다. lookup 실패가 선택 여부 판별보다 앞선다.
- `revision_history_v5.py`는 source_metadata_unresolved를 정상적으로 표현하는 미해결 사유로 정의한다. 미해결 이력은 source_id/pdf_sha256/reason_codes로 구성되며 issuer 필드는 없다. PDF cache 링크·시각만으로 과거 상품명·효력일을 만들어서는 안 된다는 기존 계약이 있다.

## 추가 운영 조사와 결과

네트워크 없는 임시 컨테이너에서 운영 이미지의 `_known_snapshot_sources`를 실제 호출했다. WorkerState 생성자 대신 `__new__`에 SQLite mode=ro 연결만 부여하여 초기화/migration을 피하고, 등록된 8개 adapter와 `load_state_seed_ledger`를 사용했다. adapter 생성만 했으며 discovery/download는 호출하지 않았다. state 볼륨은 WAL 접근을 위해 쓰기 가능한 mount지만 SQL은 read-only다. readonly rootfs에서 SQLite 임시 정렬이 disk I/O error로 실패한 첫 시도 뒤 `PRAGMA temp_store=MEMORY`로 재실행해 통과했다. 이 첫 오류는 Worker 운영 실패와 별개인 조사 컨테이너 제약이다.

- snapshot 288개, canonical snapshot/seed 복원으로 얻은 known source 5558개.
- baseline seal의 미해결 identity 22개 중 known에 없는 것 4개. 4개 사유 모두 source_metadata_unresolved.
- 같은 source_id의 pdf_cache_source 행에서는 아래 issuer/product_code가 확인된다. 이 조회는 원인 조사이며 행 존재만으로 신뢰 검증이 끝났다고 판단하지 않는다.

| source_id | issuer | product_code |
|---|---|---|
| source_45d13e85c71e1e8f8e9c3f8c8103a67c4d31da109568559e21729fef117b0bb4 | hana | 15758 |
| source_497c579933e03d3d413bdfafbf9c2e8f64e1c6c1865ca03a2b403d131b06f2d3 | hyundai | 12461 |
| source_61152a27ff24e87264c9ac93a0525ee3b68065fee833fa63fa1740513a73a545 | hyundai | 153805 |
| source_efd56847f7a60433b73c86fd9f67d1754a223faa36f453a2d32e980880321683 | hyundai | 153806 |

이는 특정 카드의 파일 오류가 아니라, 메타데이터가 없다는 이력을 보존하면서 그 메타데이터를 필수로 요구하는 모순이다. 우리와 무관한 하나/현대 이력 때문에 선택 실행 전체가 막혔다.

## 미검증 사항

- 위 4개 source의 최초 유입·이전 seed/migration 경로, cache identity와 revision 링크의 재계산 검증은 미완료다. metadata 유실 원인을 임의로 GC/과거 개발 잔여물로 단정하지 않는다.
- 나머지 18개 미해결 이력의 카드사별 구성, 새 코드로 하나/현대를 선택했을 때의 재산정 결과, resumed publication/collect-process에서의 호환은 아직 시험하지 않았다.
- 실제 새 PDF 1건의 상품명·OCR 결과는 이번 run에서 검증 완료되지 않았다. 신규 게시 0을 신규 원본 0으로 해석하지 않는다.
- 운영 배포 완료나 8사 정상 완료 판정은 할 수 없다.
