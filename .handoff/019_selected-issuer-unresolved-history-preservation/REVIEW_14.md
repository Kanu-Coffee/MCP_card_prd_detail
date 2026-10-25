# 019 REVIEW_14 — FIX_26 정리 및 FIX_27 예약·프록시 변경 검토

2026-10-22 Codex, Reviewer. PLAN/변경계획, FIX_26/REPORT, FIX_27/REPORT, REVIEW_13과 현재 Git·운영 파일·systemd·container/image·snapshot 및 MCP 증거를 대조했다. 삭제나 운영 설정 변경은 추가 수행하지 않았다. 기존 handoff를 보존한다.

## 판단

**FIX_27 예약·프록시 변경은 검증 통과. FIX_26의 실제 정리/데이터 보호는 확인했으나 복구 안내 P2 보완1건을 FIX_28로 발행한다.** 오늘10시 예약을 중단할 사유는 확인하지 못했다. 그 시각의 첫 자동 배치 성공은 아직 미검증이다.

### P2 — 보존된 롤백 release의 snapshot 경로가 삭제된 복구본을 가리킴

`/opt/cardrag/019-fix23-e9b9cdca1a22/operations/deployment.json`은 snapshot_directory를 `/state/deployment-backups/019-fix23-e9b9cdca1a22`로 유지하지만 실제 해당 폴더는 삭제됐다. active metadata만 새 protected snapshot으로 바뀌었다. rollback release를 다시 활성화한 뒤 metadata를 복구 지침으로 사용하면 없는 경로를 참조할 수 있다. 역사 metadata 자체는 보존할 수 있으나 현재 복구점 안내와 구분이 필요하다. FIX_28에서 단일 최신 복구점의 발견/호환성 확인 경로를 명시하고 실제 rollback/DB 복원 없이 보완한다.

### 절차 관찰 — 공유 build cache prune 범위

Executor는 명시적으로 CardRAG 이미지를 제거한 것과 별개로 호스트 `docker builder prune --all --force`도 실행했다. 현재 서비스 장애나 운영 volume 손실은 관찰하지 않았으나 공유 builder의 타 프로젝트 cache도 범위에 포함될 수 있다. 과거 cache 재생성은 요구하지 않는다. 향후 표적/기간/보존량을 한정하는 안내를 FIX_28에 포함했고 전역 prune을 반복하는 자동 정책은 승인하지 않는다.

## Reviewer 직접 확인

- df 현재 가용110,419,279,872bytes(약102.8GiB), 사용73%. 보존된 before/after 측정의 순확보24,687,685,632bytes(약22.99GiB)와 현재 근소한 사용 차이는 모순되지 않는다. Docker 보고의 공유 layer 합을 디스크 순회수량으로 대체하지 않았다.
- active image ID `sha256:d08b61a6f3cd3809be3ed46aaa560f48bf632f4d987685b2640bfeb1ea339597`, rollback image ID `sha256:15124ebcc9147de54db5ac238bb488f9a31eead2e5cd1b0a89c334f60550972e` 실제 존재.
- protected snapshot은 `019-fix24-current-20261022`1개다. 두 DB를 직접 읽어 전체 SHA256/크기를 snapshot.json과 대조했고 SQLite immutable read-only quick_check가 모두ok였다. worker-state WAL은0bytes이며 SHM32768bytes로 offline main DB 검사 조건을 확인했다. worker-state7,403,823,104bytes/backup-ledger24,416,256bytes다.
- 삭제된8개 container의 gzip log8개를 끝까지 읽어 무결성을 확인했고 inspect JSON8개도 parse 통과했다. 최신8사 summary는 정상 succeeded7사/no_change1사, 비선택 변경0을 유지한다.
- 실제 운영 MCP source page2개 조회 성공. MCP current와 Worker stable은 `g-666c2d2ee9e24d04acf76eed-d6ffd9a927e9`로 일치한다. snapshot/로그 읽기와 별도로 원격 백업 전체 본문 재감사는 하지 않았다. 보존된 cleanup scoped status는 필수 결손0/정상 out-of-scope pending3개다.
- resolved compose는 동일FIX_24 image/round_robin/프록시 빈 값이다. FIX_27의 직접 연결 실측은 전체 목록 신한887/롯데531 및 PDF표본200/magic 확인으로, 배치 전체 성공이나 OCR 품질 검사와 구별돼 있다. URL/인증정보를 출력하지 않는 방식도 확인했다. 이번 리뷰에서 원본 전체 목록 호출을 반복하지 않았다.
- repo timer와 실제 설치본 cmp 일치, systemd-analyze verify 및 git diff --check 통과. timer active/enabled/Persistent=no, next10/22 10:00KST다. 예전 service failed는 이전 후보 이미지 실행 기록이며 수정 경로의 새 실패로 보지 않는다.

이번 범위에는 Python 구현 변경이 없어 전체 suite를 반복하지 않았다. 실제 복구 snapshot 검증·MCP·unit/설정·아카이브 검증을 수행했고 그 결과를 배치 실운영 성공과 혼동하지 않는다. 자동 첫 배정/실행/백업 종료를 확인할 때까지019 최종 인수는 열어 둔다. FIX_28은 운영 복구 안내 보완이며 새 image나 오늘 배치 선행 재처리를 요구하지 않는다.
