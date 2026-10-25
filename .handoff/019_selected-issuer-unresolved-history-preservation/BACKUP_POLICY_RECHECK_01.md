# 백업 정책 재확인 — 설정에 따른 정상 pending과 이번 metadata 제외의 구분

2026-10-20 Codex. 사용자 지적에 따라, 최소 백업 설정 때문에 의도적으로 보류된 항목을 결함으로 잘못 판정했는지 재검토했다. 기존 LIVE_REVIEW/FIX_06은 보존한다.

## 현재 설정 직접 확인

운영 Compose와 동일한 설정/secret mount를 사용하는 컨테이너에서 `WorkerSettings.from_env(require_providers=False, require_webdav=False)`로 설정만 읽었다. 수집/추론/flush는 수행하지 않았다.

```text
backup_mode=immediate
backup_every_runs=7
backup_new_ocr_count=30
backup_new_bytes=1073741824
backup_max_pending_age_hours=168
backup_inline_budget_seconds=300
backup_derived_snapshot_enabled=False
```

## 정상 보류 정책은 존재함

- `hybrid`는 횟수/개수/bytes/age 조건 미달이면 정상 deferred/pending이다. 위7회/30건/1GiB/7일 값은 `evaluate_triggers`에서 hybrid에 적용된다.
- `manual`은 자동 전송하지 않는다. immediate에서도 시간 예산/일시적 전송 실패 등은 pending으로 남길 수 있으며 로컬 게시 성공을 뒤집지 않는다.
- 선택 실행은 비선택/관계없는 전역 cache와 pending을 자동 소진하지 않는다. 따라서 전역 pending이 있다는 사실만으로 선택 run의 백업 장애를 판정하면 안 된다.
- derived snapshot 제외는 최소 백업 정책이며, 기존 013 계획에서 재생성 가능한 serving DB·벡터의 백업 제외를 뜻한다.

## 이번 두 건에 적용한 확인 결과

이번 설정은 immediate이므로 hybrid의 횟수/크기 미달로 보류한 경우는 아니다. 이번 두 파일은 DB/벡터 snapshot이 아니라 신규 선택 우리카드 문서에 연결된 native OCR manifest1791bytes와 READY285bytes다.

실제 recorder는 이를 pending에 등록했다. 선택 문서의 seal에는 `ocr_reuse_key=None`이어서 `scope_remote_paths`가 metadata 경로를 허용 목록에 넣지 않았고, 두 pending의 `remote_path in allowed_paths`가 False임을 readonly 조회로 확인했다. manifest는 backup doc_info의 document_id/PDF/OCR hash로 신규 우리카드 문서에 연결되고 READY는 같은 reuse_key의 짝이다. 따라서 비선택 카드사의 무관한 pending이라고 볼 근거도 없다.

최소 백업의 원래 계약인 `.handoff/013_local-serving-incremental-webdav-backup/PLAN.md` 4.2는 OCR 본문과 함께 native/compatible/adopted manifest 및 필요한 READY/variant 정보를 포함하도록 명시한다. 같은 문서의 4.3은 derived snapshot 제외를 DB·벡터에 대해 규정한다. 현재 restore 코드도 native manifest를 읽어 OCR seed/cache와 복구 ledger를 구성한다.

**따라서 현재 확인된 설정·기존 계약만으로 이번 두 건을 의도된 정상 제외라고 정정할 수는 없다.** 확인된 원인은 threshold 미달/derived snapshot 제외가 아니라 선택 허용 경로와 실제 OCR metadata 연결의 불일치다. FIX_06의 해당 연결 검증 요구를 유지한다.

다만 **pending 그 자체를 장애로 단정하는 해석은 정정한다.** 이 run은 수집·OCR·게시 성공이며 실제 PDF/OCR 본문도 백업됐다. 로그의 backup_status=degraded는 현재 구현이 전역 잔여 pending까지 반영한 결과이므로, 그 문자열만으로 전송 장애/데이터 유실/배치 실패를 뜻한다고 해석하지 않는다. 정책상 정상 보류, 선택 범위 밖 보류, 선택 artifact 연결 누락과 실제 전송 실패를 구분하는 것이 FIX_06의 검증 기준이다. 정상 정책 pending을 없애려고 전체 업로드·강제 flush·queue 삭제를 해서는 안 된다.

원래 정책과 다르게 OCR manifest/READY까지 선택 백업에서 제외하는 사용자 결정/운영 근거가 추가로 확인되면, 그에 맞춰 복구 요구와 recorder/상태 표시/FIX 판정을 함께 조정해야 한다. 현재 확인된 설정에는 그 제외를 의미하는 항목이 없다.

이번 재확인에서는 설정/코드/기존 계획을 읽었고 제품 코드, pending, 원격 백업 및 timer를 변경하지 않았다.
