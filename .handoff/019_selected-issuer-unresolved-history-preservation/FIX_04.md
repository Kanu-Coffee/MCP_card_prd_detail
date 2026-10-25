# 019 FIX_04 — FIX_03_REPORT 재검토 및 남은 실행 경계 수정

2026-10-19 Reviewer: Codex. **판정: 수정 필요. 아래 두 실행 경계를 수정한 뒤 재검토한다.**

## 검토 기준과 해결 확인

`PLAN.md`, 우선 적용되는 `PLAN_AMENDMENT_01.md`, `ANALYSIS.md`, 원본 REPORT, FIX_01–03 및 각 REPORT, 현재 Git status와 관련 코드를 대조했다. HEAD는 `aaeb2c4`이며 017–019 변경이 미커밋으로 공존한다. 기존 문서를 보존하고 다음 미작성 correction인 본 문서만 추가했다.

FIX_03의 DRM `.record` 접근은 `.source`로 수정됐고 일반 실행에서 정상 문서와 DRM이 섞인 경우의 회귀 테스트가 추가됐다. 일반 실행의 final ledger 변경 후 corpus SHA 재계산과 동일 입력 no-change도 개선됐다. 실제 `AcquisitionSource`를 사용하는 lifecycle 테스트로 바꾼 점도 확인했다. 다만 CLI actual serving 검증은 여전히 우회 가능하며, 새 replay hash 재구성에는 OCR 실패 문서 타입에 대한 회귀가 있다.

## 1. [P1] CLI가 잘못된 transport를 선택하고 serving 조회 실패를 무시해 영구 정비를 적용함

위치: `apps/cardrag-worker/src/cardrag_worker/cli.py:2931–2952`, `unresolved_remediation.py:135–151,246` 부근의 등록 seal 대조.

설정의 실제 필드는 `publication_transport: Literal['local','webdav']`다. 새 CLI는 존재하지 않는 `settings.local_serving`을 검사하므로 정상 local 설정에서도 WebDAV 경로로 들어간다. local 분기에 들어갈 경우에도 channel을 `candidate-009`로 고정해서 실제 설정의 `channel`을 따르지 않는다. 기존 CLI의 transport 선택(`cli.py:1511` 부근)은 `publication_transport`, `serving_dir`, `channel`을 사용한다.

그 상태에서 실제 serving 조회가 실패하거나 pointer가 없으면 예외를 삼키고 `current_serving_generation_id=None`으로 진행한다. validator는 이를 검증 실패로 보지 않고 다시 latest-ready DB 정렬로 대체한다. 따라서 FIX_03이 요구한 actual current 확인 없이도 apply가 가능하다. manifest와 등록 seal 대조도 각각 입력/파일이 있을 때만 검사하므로, 두 증거가 모두 없는 별도 JSON에 기존 publication ID와 자체 일관성 있는 ledger를 넣어도 통과한다.

임시 state와 CliRunner 재현:

```text
CARDRAG_PUBLICATION_TRANSPORT=local
현재 serving pointer: 없음
WebDAVClient.from_env: 테스트에서 즉시 예외를 던지도록 대체(실제 네트워크 없음)
DB: succeeded run, latest-ready publication, 역사 source/revision/object 연결 존재
입력: 별도 seal JSON, manifest 없음, 등록 runs/<run>/sealed/publish.json 없음
CLI --seal-path fabricated.json --apply
관측: local 설정인데 WebDAV 생성 시도, 예외 무시
exit=0, status=succeeded, applied_count=1
```

수정 요구:

- 기존 CLI와 같은 `publication_transport`, `serving_dir`, `channel` 설정으로 실제 publication transport를 선택하고 사용 후 자원을 정리한다. 별도 hardcoded channel을 사용하지 않는다.
- apply는 실제 current 또는 그에 결합된 검증 receipt를 확보해야 한다. 조회 예외/None을 latest-ready fallback으로 성공 처리하지 않는다. 오프라인 미검증 입력은 dry-run 진단과 적용 가능 여부를 분리한다.
- manifest/등록 seal/게시 DB 등 필요한 증거를 선택적 검사로 우회하지 못하게 한다. 원본을 확인할 수 없는 별도 파일은 영구 적용하지 않는다. 정상적인 구형 seal 지원이 필요하면 기존 호환 정책에 따른 검증 근거를 명시한다.
- **실제 CLI** 테스트에서 local stable, local candidate, WebDAV, pointer 없음/손상/조회 실패, rollback, manifest/등록 원본 부재를 검사한다. helper에 current generation 문자열을 직접 전달하는 테스트만으로 actual serving 연결이 검증됐다고 보고하지 않는다. 실패 시 remediation DB는 불변이어야 한다.

## 2. [P1] replay corpus 재계산이 OCR 실패 문서를 성공 문서와 같은 타입으로 다뤄 배치를 중단시킴

위치: `apps/cardrag-worker/src/cardrag_worker/partial_execution.py:820–838`, `pipeline.py:821`의 `_OCRFailedDocument` 정의.

새 hash 재계산은 ledger가 변경되면 `(*processed, *failed)`를 합쳐 모든 항목에 `doc.source`, `doc.temporal_status`, `doc.supersedes_document_id`가 있다고 가정한다. 그러나 `_OCRFailedDocument`는 `record`, `pdf_path`, `is_historical`만 갖는다. OCR 실패가 기존 허용 범위 안에서 격리됐어도 최종 ledger 정리가 발생하면 `doc.source` 접근에서 중단된다.

실제 collect→`AcquisitionSource`→process 재현:

1. mock 카드사에 현재 문서 20건을 만들고 검증된 baseline을 게시한다.
2. 같은 lineage의 과거 source/revision을 추가한다. source identity는 `PDFSourceIdentity.from_source_record`로 계산하며, 과거 metadata 부재 unresolved entry 1건을 재현한다.
3. collect 후 실제 `AcquisitionSource`로 process한다. 현재 문서 1건만 isolatable `OCRValidationError`로 실패시키고 나머지 19건은 처리한다. 역사 문서 실패가 아니며 19/20=95%로 기존 허용 기준을 통과한다.
4. 별도의 정상 상품에 대한 과거 unresolved entry가 정비돼 ledger 변경이 발생하면 hash 재계산에서 다음 예외가 난다.

```text
AttributeError: '_OCRFailedDocument' object has no attribute 'source'
외부 노출: WorkerUnexpectedFailureError
```

수정 요구:

- 실패 문서의 SourceRecord/temporal/supersession 정보는 검증된 source revision/receipt에서 복원하거나, 기존 획득 입력을 보존해 corpus를 재계산한다. 실패 record에서 임의 metadata를 만들어내지 않는다.
- 성공/실패 항목 모두를 기존 canonical corpus 순서와 동일한 순서로 결합하고, 실패 PDF를 corpus에서 누락시키지 않는다.
- 실제 collect-process와 reprocess에서 ledger 변경 + 95% 허용 OCR 실패 조합을 검증한다. 정상 문서 처리/실패 기록/현재 오류/manifest corpus SHA가 유지되고 unexpected failure가 없어야 한다. threshold 미달 및 역사 문서 실패의 기존 차단 계약도 유지한다.

## Reviewer 검증

```text
uv run --no-sync pytest apps/cardrag-worker/tests/ -q
1385 passed, 10 warnings in 51.77s

uv run --no-sync ruff check apps/cardrag-worker/
All checks passed!

uv run --no-sync ruff format --check apps/cardrag-worker/
125 files already formatted

git diff --check
통과
```

추가 process 재현은 `/tmp/cardrag019-review4.Qs3u0X/test_review4.py`에서 기존 corpus fixture/mock provider를 사용했다. 초기 테스트의 receipt 필드 조회 오류를 수정한 뒤 다음 결과를 확인했다.

```text
PYTHONPATH=apps/cardrag-worker/tests:apps/cardrag-worker/src:packages/cardrag-core/src uv run --no-sync pytest /tmp/cardrag019-review4.Qs3u0X/test_review4.py -q -s -o asyncio_mode=auto
REPRO: 19/20 OCR success plus remediated ledger crashes during corpus reconstruction
1 passed in 2.47s
```

이 pass는 결함 동작을 확인하는 재현 결과이며 승인 테스트가 아니다. CLI 우회는 별도 임시 SQLite/JSON과 lookup 실패 mock으로 재현했다. 실제 카드사/운영 DB/serving에 접근하거나 배포·timer 변경을 수행하지 않았다.

보고서도 실제 근거에 맞춰 보완한다. FIX_03_REPORT의 rollback 검증은 helper 호출이며 실제 CLI transport/예외 경계를 검증하지 않는다. 운영 명령 예시의 `run --selected-issuers woori`는 현재 CLI의 `--issuer woori`와 맞지 않으므로 수정 결과 보고서에 올바른 사용법을 남긴다. 정상 DRM test는 문서 수 외에 unsupported/degraded 상태도 직접 검사하도록 보완한다.

## Executor 인계

기존 PLAN/REPORT/FIX를 덮어쓰지 않고 본 correction의 구현·명령·실제 결과·편차와 운영 미검증 항목을 `FIX_04_REPORT.md`에 작성한다. 017–018 공존 변경과 비선택 격리, 공유 CAS/현재 serving/마지막 정상본 보호 계약을 유지한다. 위 경계 테스트, 관련 테스트 및 Worker 전체 테스트·ruff를 실행한다. 검토 전 배포·실수집·timer 활성화를 하지 않으며, 이후 사용자 수동 우리 실행 → 나머지 7사 → 백업/degraded 확인 → timer 조정의 운영 순서를 유지한다.
