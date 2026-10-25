# 019 LIVE_REVIEW_05 — 재실행 BC는 OCR 통과, 용량 gate에서 중단

2026-10-20 Reviewer: Codex. 사용자 “bc 다시 failed” 요청에 따라 실제 새 컨테이너·로그·state·게시·복구본을 읽기 전용으로 조사했다. **기존 OCR 결함 재발이 아니라 filesystem free-space gate에 의한 정상 차단**이다. 운영 준비의 용량 검증이 불충분하여 FIX_17을 발행한다. 이번 조사에서 삭제·threshold 완화·재배포·배치 재실행·운영 DB/원격 쓰기를 하지 않았다.

## 실행 결과와 원인

- 컨테이너 `cardrag-live-bc-20261020T072112Z`, image `cardrag-worker:019-fix16-4108e3dac5f5`.
- run `d4fe5f3f85d344e7b981da0adb35a556`, KST 16:21:14–16:47:10, 약25분57초, exit1, OOMKilled=False.
- 목록122, PDF cache122 / 다운로드0. OCR122·structure122·views122 모두 succeeded. 이전 variant ID 오류는 발생하지 않았다.
- embedding-v5의 용량 preflight가 `capacity_free_space`로 실패했다. 새 embedding miss 처리와 export/publish 이전이다. pipeline 사전 점검 provider 호출과 실제 새 embedding 생성은 구분한다.

```text
available_bytes      69418844160  = 64.651 GiB
required_free_bytes  73971883584  = 68.892 GiB
deficit_bytes         4553039424  =  4.240 GiB
peak_growth_bytes    71824399936  = 66.892 GiB
reserved_free_bytes   2147483648  =  2.000 GiB
derived_views=619676, unique_cache_misses=6, database_rows=12666406
```

`capacity_v5.py:preflight_v5_capacity`의 peak growth+reserve 계산과 실제 기록이 일치한다. 선택 BC 실행도 현재 구조에서는 전체 corpus의 DB/sidecar를 생성하므로 카드사가 한 개라는 이유로 필요한 게시 공간이 그 비율로 감소하지 않는다. 이번 실패는 물리 ENOSPC로 쓰다가 손상된 상황이 아니라, 예상 peak를 확보하지 못해 위험한 작업을 시작하지 않은 것이다. gate를 낮추거나 reserve를 제거할 근거는 없다.

시작 로그에도 이미 free73364160512 bytes가 직전 성공 required73971502728 bytes보다 적다는 warning이 있었다. minimum-start 2GiB만 통과하고 warning은 실행을 막지 않아 약26분 후 실패했다. 배포 단계에서 새 복구본을 만든 뒤 이 기준과 전처리 증가분까지 확인하지 못한 것은 운영 준비 누락이다. 기존 OPERATIONS_REPORT_04의 “여유69GiB”는 관측값이며, BC 실행 가능 공간을 검증한 수치가 아니었다.

## 게시·백업 영향

- 새 run의 publish row 없음, 새 seal/게시 없음. stable은 삼성 성공 세대 `g-44d55df9db8645c8b9060dc4-a7a46a5bb5e6` 그대로다.
- 전체5543, 하나735·삼성766·롯데647·우리980·신한931·현대605·KB757·BC122 유지. 비선택 문서 변경0.
- 실패 run의 성공 백업 등록 없음, 전역 pending0. 이를 백업 완료나 신규 백업 누락으로 판정하지 않는다. 성공한 새 run이 아니므로 check-issuer.sh --flush가 차단하는 것이 맞다.
- status 결과는 run failed를 표시하지만 execution_result=null이다. 최종 capacity 오류 JSON에 run_id가 없는 현재 파서 조건 때문에 분류·부족량이 status/summary에 직접 드러나지 않아 로그 조사가 필요했다.

## 공간과 복구본 조사

조사 시 filesystem 약65GiB 여유,83% 사용. 새 배포 복구본은 worker7305965568+backup ledger24416256 bytes, 약6.827GiB다. 배포 전 약76GiB→복구본 후69GiB→이번 실패 run 산출물 등 증가 후65GiB라는 변화와 부합한다. snapshot은 필요한 최신 복구점이므로 이를 제거하는 방식은 적절하지 않다.

보호 집합:

1. 현재 `019-fix16-4108e3dac5f5`의 새 일관된 snapshot.
2. 직전 release `019-e7fb8a0ba60c`의 복구본.
3. 직전 복구본이 참조하는 `019-ba273106158e/worker-state.sqlite3` 및 해당 검증 근거.

구형 후보 `017-fca5ef313a0b`, `019-4dba44664659`의 파일은 nlink1·symlink 없음·별도 inode다. 각 DB와 부속 파일의 allocated block 합은 약13.624GiB다. 현재/직전 복구의 **DB 의존성**에는 포함되지 않는다. previous_release라는 역사 링크를 무제한 복구 지원 약속으로 해석하지 않는다. 실제 정리 시에는 파일 사용 여부와 보호 집합을 다시 확인해야 한다. 이번에는 후보 조사만 했으며 Docker 이미지/볼륨 global prune, serving/MCP 세대 삭제, 실패 run 삭제를 하지 않았다.

약4.24GiB만 확보하고 곧바로 재시도하면 다음 전처리 산출물 때문에 또 부족할 수 있다. 현재 required68.9GiB+관측된 전처리 증가 약3.7GiB+여유를 실행 전에 확보한다. 후보13.6GiB의 안전한 정리를 실제 수행하면 약78GiB 수준이 예상되나, 실측 후 판단해야 한다. 새 대형 복구본을 또 만드는 행위를 무조건 반복하지 않는다.

## 증거와 후속

`/tmp/cardrag-bc-fix16.log`, `/opt/cardrag/current/operations/live/bc.result.json`, `/tmp/bc-fix16-capacity-audit.txt`를 확인했다. state/serving volume은 RO, network-none으로 조회했고 현재 run·publish·capacity observation·복구본 inode/allocated blocks·배포 metadata의 의존성을 대조했다. 소스 무수정이라 전체 테스트를 반복하지 않았다.

FIX_17은 운영 공간 확보 절차·시작 전 조기 점검·명확한 capacity 실패 진단으로 한정한다. 전체 export 최적화/GC 재설계는 현재 실패 복구의 필수 조건이 아니므로 새 PLAN을 만들지 않는다. 019와 자동 timer 비활성 상태를 유지한다.
