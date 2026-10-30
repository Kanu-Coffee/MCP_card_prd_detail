# 022 운영 리뷰 및 디스크 정리 결과

작성일: 2026-10-31 KST  
역할: Reviewer  
판정: **022 운영 배포·우리카드 재개·게시 범위 인수. 누적 복구본 보존 문제는 이번 사용자 정리 지시로 현장에서 교정함.**

## 검토 범위와 확인

FIX_05 및 FIX_05_REPORT, REVIEW_01과 이전 correction 기록, 현재 Git 상태와 실제 active release/image, 실행 container 로그, 배포 증거, serving manifest, MCP current, backup 상태를 확인했다. 코드 변경 없이 운영을 검증하고 승인된 용량 정리를 수행했다. 기존 handoff 보고서는 보존한다.

- active `/opt/cardrag/022-woori-session-auth-20261030`.
- 실행 image ID `sha256:b0b765d9e1083fe5eb1649ee4d1918de6a87b54c656458285a68cc664f2064c0`.
- 실image에서 Python 모듈 파일 SHA-256을 계산해 인수된 woori/downloader/pipeline 3개 hash와 각각 일치함을 확인했다.
- `cardrag-worker-022-woori-resume-20261030`: exited/0, 종료 UTC `2026-10-30T15:19:02.700909476Z` (10/31 00:19:02 KST).
- 동일 run `df29d8e5dbb34048942a5296bb2d6ac8`의 최종 JSON: `succeeded`, `quality_status=passed`, `collection_status=complete`, `published=true`.
- PDF 성공 918 / skip 4, 실제 원본 download 220, OCR cache 재사용 981 / provider 대상 1건. skip 4건을 모든 PDF 취득 완료로 표현하지 않는다.
- 게시 generation `g-df29d8e5dbb34048942a5296-5a3a99224f92`가 현재 Worker stable 및 MCP current와 일치한다. manifest SHA가 pointer와 일치한다.
- 이전 BC generation `g-ec574ddd3cda4570adcf1124-2777b78df9fd`와 새 generation의 manifest documents를 document_id별로 대조한 결과, 비선택 7사 문서 레코드 변경 **0건**이다. 단순 문서 수 비교보다 강한 확인이다.
- 기존 `test-live-mcp.py`를 실제 MCP 안에서 실행해 초기화·tool 목록 및 OCR page 2회 읽기에 성공했고 MCP/Worker generation 일치를 확인했다.
- run 종료 JSON은 `backup_scope_complete=true`. Reviewer의 `backup status --run ... --issuer woori`는 in-scope remaining/conflicts/missing/lost_source/unbound 모두 0, out-of-scope pending 4건 / 300,444 bytes다. 이 status 명령 자체의 `backup_scope_complete`는 null이므로 그 값만으로 true라 단정하지 않고 종료 기록과 함께 판단했다. 이번 리뷰에서 추가 원격 flush는 수행하지 않았다.
- timer active/enabled 유지, Worker/backup 실행 container 없음, MCP 및 고객 web/edge healthy. systemd worker의 failed 표시는 10/30 예약 실행의 과거 실패이며 성공한 수동 resume의 실패가 아니다.

## 기존 보고서의 사실 교정

다음은 실제 파일·로그와 맞지 않는 표현을 바로잡는 것이며, 기존 보고서 내용을 덮어쓰지 않는다.

1. FIX_05_REPORT의 snapshot evidence SHA `bb1baa...` 및 worker DB 크기 `7,467,991,040`은 실제 보호 복구본과 다르다. 실제 evidence SHA는 `ffc259e197fd284cfe6f5d97bc7486ac8d53aa5621527f7542e66279e62cac33`, worker DB는 `7,463,845,888` bytes, SHA `bac2083c3d9918526a5829037e69533856e1d366a0f12d77f0b8cae98e87eb6b`이다. backup DB는 `27,185,152` bytes, SHA `3fe27cf479158ef1cf2b9a10fa48ba015bfbedac34b7b4f4722578ff5982673a`다.
2. skip을 단순 부속서류 오류로 설명하면 부정확하다. `unsupported_attachment_count=0`이며 로그에는 보호 문서(SCDSA004) skip이 있다. 상품코드 500061/500062의 보호 문서 경고도 명시돼 있다. 보호 해제나 HTML 오류 우회는 수행하지 않았다.
3. 배포 보고서의 다중 snapshot 보존은 docs/OPERATIONS.md의 단일 독립 복구본 정책과 어긋난다. 이전 snapshot은 코드/image rollback을 위해 반드시 함께 필요한 DB가 아니다. 이번 사용자 용량 정리 지시에 따라 아래처럼 교정했다.
4. 보호 snapshot은 **배포 직전** 실패 run 및 이전 BC serving을 담은 복구점이다. verify 출력의 latest_run=failed/이전 stable은 그 역사적 snapshot 내용이며, 현재 resume 성공·현재 generation과 혼동하지 않는다.

## 용량 진단

사용자 요청 직후 root 가용량은 `81,246,425,088` bytes였다. FIX_05 발행 전 관측 `91,841,871,872` bytes보다 약 10.60GB 적다. 주요 측정값(du의 GiB 기준):

- worker state 약 72.2GiB, 이 중 deployment-backups 20.9GiB (019/021/022 약 7GiB씩 3개).
- worker runs 39.8GiB, PDF cache 4.3GiB.
- MCP state 47.4GiB, 이 중 generation 3개 43.1GiB. 실제 운영 설정 `CARDRAG_MCP_RETAIN_GENERATIONS=3`과 일치한다.
- serving 33.2GiB, 이 중 generation 28.8GiB.
- Docker build cache 12.3GB, 기본 prune 회수 가능분 약 2.2GB.

새 022 snapshot 자체의 두 DB 크기는 약 7.49GB다. 새 복구본과 빌드 캐시 누적을 확인했으나, 다른 프로젝트의 동시 변경까지 포함한 전체 감소분을 하나의 원인으로 단정하지 않는다. 현재/이전 serving과 MCP generation, 원본 PDF/OCR, 모든 운영·롤백 image와 다른 프로젝트 local image는 이번 정리에서 보존했다.

## 수행한 정리와 보호 절차

1. 실행 중 build가 없는지 확인한 후 기본 `docker builder prune -f`로 미사용 빌드 cache **2.193GB** 회수. `--all`, image/volume 전역 prune은 사용하지 않았다. `uv cache prune`은 미사용 항목 0이었다.
2. 보호 대상 022 snapshot을 기존 `verify-recovery-point.py`로 **정리 전과 후 모두** 검증했다. 두 DB SHA·크기·SQLite quick_check 모두 정상, `verified=true`, `state_written=false`다. snapshot DB는 regular file이며 nlink=1, 이전 snapshot에 대한 파일 참조가 없는 독립 복구본임을 확인했다.
3. deployment lock, active/rollback manual-start lock, worker.lock, backup.lock을 비차단 획득해 유지한 상태에서 정확히 아래 두 오래된 디렉터리만 정리했다. timer는 유지했다.
   - `019-fix33-current-20261022`: allocated `7,429,242,880` bytes.
   - `021-ocr-logging-20261029`: allocated `7,487,500,288` bytes.
   - 합계 **14,916,743,168 bytes**.
4. active/rollback 및 삭제 대상 snapshot을 현재 복구점으로 안내하던 보존 release들의 recovery guidance/retention을 022로 통일했다. 배포 당시 snapshot 경로는 역사값으로 남기고 해당 옛 deployment에는 `historical_snapshot_status=pruned`를 표시했다. DB restore는 하지 않았다. 기존의 “FIX_33 이전 snapshot”이라는 잘못된 설명도 “복원 호환성 별도 검증 필요”로 교정했다.
5. 보호 snapshot은 022 하나만 남았다. active와 rollback의 guidance가 정확히 동일하며, 021 코드/image rollback은 보존했다.

정리 전 시도 중 파일 소유권과 컨테이너 mount 경로 검사에서 실패 폐쇄한 호출들이 있었다. 삭제 단계 전 중단됐으며, 이후 필요한 경로를 명시한 격리 maintenance container에서 성공했다. 호스트 또는 daemon 전역 설정은 바꾸지 않았다.

사전 metadata 사본, 명시적 정리 script 및 완료 JSON:
`/opt/cardrag/022-woori-session-auth-20261030/operations/live/review-20261031/`

정리 후 root 가용량은 **98,115,805,184 bytes (약 98.12GB / 91.38GiB)**, 실제 순증 **16,869,380,096 bytes**였다. background 사용량 변동 때문에 파일 allocated 합과 df 변화가 정확히 같을 필요는 없다.

## 판정과 남은 범위

운영 코드·우리카드 resume 완주·게시·비선택 7사 보존·대상 backup 범위를 인수한다. snapshot 누적 문제는 사용자 승인된 이번 작업에서 직접 교정했으므로 별도 Executor FIX를 발행하지 않는다. 전체 과거 backup pending 4건은 남아 있으나 이번 run의 backup 미완료가 아니며, 삭제로 숨기지 않았다.

새벽 리뷰 시점에는 10/31 10:00 다음 정규 예약의 실제 실행 결과는 아직 관측하지 않았다. 현재 timer 상태 확인과 다음 일배치 완주 관측은 구별한다. MCP generation 보존 수 축소나 운영 DB/VACUUM·원본 cache 정리 등 추가 정책 변경은 수행하지 않았다.
