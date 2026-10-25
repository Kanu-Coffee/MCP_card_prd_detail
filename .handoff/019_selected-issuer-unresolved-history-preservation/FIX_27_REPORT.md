# 019 FIX_27_REPORT — KST10시 예약 활성화 및 추가 승인된 프록시 해제

2026-10-22 Codex. 사용자의 매일10시 자동운영 전환 요청과 첫 실행을 **오늘10시 예약**으로 하겠다는 응답에 따라 진행했다. 사용자가 관리자 적용 명령을 실행한 뒤 실제 systemd 상태를 재검증했다. 추가로 사용자가 IP 변경 후 직접 연결 가능성을 알리고 간단한 옵션 변경이면 프록시 해제를 승인하여 아래 연결 검사 후 반영했다. Worker 실제 배치는 지금 시작하지 않았다.

## 예약 적용 완료

- repo timer Description/OnCalendar를 KST10시로 바꾸고 docs/OPERATIONS.md의 현재 예약 시각을 일치시켰다. Persistent=false 유지.
- 직접 sudo/systemd 요청은 관리자 인증 부족으로 거부됐다. 준비한 timer install/daemon-reload/enable --now 명령을 사용자가 실행했다. 이 권한 제약을 우회하지 않았다.
- 현재 설치된 timer는 repo 수정본과 cmp 일치, systemd-analyze verify 통과. calendar 검사에서10/22와10/23 각각10:00 KST를 확인했다. shellcheck 및 diff check도 통과했다.
- 실제 systemctl 결과: ActiveState=active, UnitFileState=enabled, Persistent=no, NextElapseUSecRealtime=Thu 2026-10-22 10:00:00 KST.
- service는 검증된 active release helper를 사용한다. image=cardrag-worker:019-fix24-11d1321cc9e6, mode=round_robin, pool=woori,kb,shinhan,samsung,hyundai,hana,lotte,bc, state=cardrag-worker-state다. 새 image는 만들지 않았다.
- 아직 자동 rotation table/배정이 없다는 REVIEW_13 관찰을 유지하며, 오늘 수동 slot을 생성하지 않았다. 첫 정상 배정은 우리부터이고, 이후 실제 다른 날짜 실행에서 다음사로 진행한다. 오늘10시 실제 시작·완주는 그 시각 이후 runtime 증거로 확인해야 하며 현재 성공을 미리 선언하지 않는다.

## 추가 요청: 신한·롯데 프록시 옵션 해제

create_issuer_client는 proxy_url=None이면 proxy transport를 생성하지 않고 trust_env=False를 유지한다. 별도 코드 수정 없이 CARDRAG_ISSUER_PROXY_URL을 빈 값으로 만들면 된다. OCR/embedding/WebDAV는 기존부터 이 선택 프록시 대상이 아니며 이번에 그 설정을 바꾸지 않았다.

먼저 실제 운영 image/container 네트워크에서 proxy_url=None을 명시한 client로 두 adapter의 전체 discover_current와 대표 PDF 준비·GET 첫 chunk를 실행했다. WorkerPipeline/DB/캐시/순회는 사용하지 않았다. PDF는 200과 시작 %PDF- magic을 확인하는 표본이며 전체 파일 해시나 OCR 품질 검증은 아니다.

```text
lotte: listing_records531, warnings1, PDF200/header valid
       HTTP200 8회/404 1회, elapsed0.861s
shinhan: listing_records887, warnings0, PDF200/header valid
         HTTP200 94회, elapsed4.071s
proxy_used=false, state_modified=false (양쪽)
```

롯데404는 adapter의 기존 검토된 source unavailable 경로에서 처리된 경고다. 홈페이지200만으로 성공 판정하지 않고 실제 목록 parser/pagination 및 다운로드 경로도 점검했다. 롯데531은 이전 수동530과 다른 현재 원본 관측이며 새 배치에 반영됐다고 주장하지 않는다. 프록시 사용 대비 동시 비교 벤치마크는 없으므로 정확한 속도 향상 비율은 제시하지 않는다.

수동 시작 lock 아래 실행 중 Worker가 없음을 확인하고 활성 release의 private runtime overlay에서 **CARDRAG_ISSUER_PROXY_URL 한 줄만 빈 문자열로 원자 교체**했다. image/모델/volume/순회/backup 옵션은 유지했다. 원본은 같은 operations 폴더의 compose.runtime.private.proxy-before-20261022.yaml에 권한0640으로 보존했다. 비공개 endpoint·파일 내용은 handoff/출력에 남기지 않는다.

before overlay SHA256=a2076f93890afb46e9f5228f5efdedce8b8b4b5bdd38945c966f076887219f64
after overlay SHA256=9bba5313c9fa3d93a970adfc7cd6b6b63a2c27fa1f5a717d7eb8f4edbf64d25e

resolved compose에서 프록시 빈 값 및 동일 image/round_robin을 확인했다. 별도 일회성 container의 실제 WorkerSettings.from_env에서 issuer_proxy_url=None과 settings parse 통과를 확인했다. 이 container도 pipeline을 시작하지 않았다. 프록시 기능은 삭제하지 않아 향후 직접 연결이 다시 차단되면 승인된 설정을 복구할 수 있다. 이후 release 준비 시 비공개 운영 설정을 유지해야 하며 오래된 proxy overlay를 다시 복사하지 않는다.

## 남은 범위

공간 정리는 요청대로 FIX_26을 발행했으며 삭제 실행은 아직 하지 않았다. 오늘10시 자동 우리 첫 실행·첫 자동 순회 DB 기록·배치 종료/백업은 후속 실운영 검증이다. 현재 timer 활성화와 프록시 직접 연결 전환은 완료했다. Python 구현 변경이 없는 옵션/예약 변경이므로 전체 suite를 재실행하지 않았고 실제 연결·설정·unit/calendar 검사로 검증했다. 이전1819건 통과를 새 예약 실제 성공 근거로 사용하지 않는다.
