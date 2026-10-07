# Executor cleanup report — 2026-10-08

## 요청 및 결과

개발·릴리스 종료 후 불필요한 로컬 잔여물을 정리했다. 실제 루트 파일시스템 가용 공간은 **104.70 GiB → 170.59 GiB**, 순증가 **65.90 GiB**다. 정리 도중 예약 Worker가 실행 중이므로 이 수치는 동시 운영 쓰기를 포함한 관측값이다. Docker의 논리적 정리 용량을 합산해 회수량으로 제시하지 않았다.

## 실행 내역

- 진행 중인 `docker build`/`buildx build`가 없고 공개 v1.0.33 릴리스가 완료된 상태를 확인했다.
- `docker buildx use default` 후 `docker buildx rm cardrag-release-v1026`: 요청한 `buildx_buildkit_cardrag-release-v10260` 컨테이너와 전용 state 볼륨을 제거했다. Buildx 목록은 default만 남았다.
- `docker builder prune --all --force`: 사용되지 않는 기본 빌드 캐시를 정리했다(명령 보고값 28.08GB). 현재 실행 이미지 레이어는 Docker 참조 보호가 적용된다.
- 모든 컨테이너의 image ID와 대조한 후, 미사용 과거 CardRAG 후보 이미지와 BuildKit 이미지 참조 **23개**를 `docker image rm`으로 제거했다. 강제 제거를 사용하지 않았다.
- 종료된 `cardrag-prod-008-resume01`, `cardrag-prod-008-first`, `cardrag-prod-007-fix01`, `cardrag-007-content-verify-fix01`의 상태와 로그를 보관한 뒤 컨테이너 **4개**를 제거했다.
- Docker 사용량 0B 및 컨테이너 참조 없음이 확인된 이전 후보 볼륨 **3개** 제거: `cardrag-worker-v122-candidate-state`, `cardrag-mcp-v122-candidate-state`, `cardrag-mcp-v130-candidate-state`.
- 현재 deployment/서비스 설정에서 참조하지 않는 `/opt/cardrag/v1.0.29`, 공개 릴리스에 증거가 보존된 `/tmp/cardrag133-*` 임시 자료, 저장소 테스트·정적 분석 캐시를 정리했다.

## 운영 및 롤백 보존

- 현재 `/opt/cardrag/current` → `/opt/cardrag/008-6b42a1a`, Worker 이미지 `cardrag-worker:008-6b42a1a` 유지.
- 직전 롤백 이미지·설정 **1세트**: `/opt/cardrag/007-31edb1d`, Worker `cardrag-worker:007-31edb1d`, MCP `cardrag-mcp:007-31edb1d` 유지. 008 안의 OpenCode 설정 원복 스크립트도 유지했다.
- 실제 Worker/MCP state, OCR/PDF 데이터, 인증, 모델, WebDAV 원본 및 다른 실행 서비스 볼륨을 보존했다. 실행 중인 state 내부의 generation/run 파일을 직접 삭제하지 않았다. 데이터 보존 정책·pin을 우회하는 수동 GC는 수행하지 않았다.
- 다른 서비스의 데이터 및 용도를 확정할 수 없는 볼륨을 전역 prune하지 않았다. 남은 Docker의 reclaimable 수치는 직전 롤백 이미지 등 보존 대상도 포함하므로 추가 전역 image prune의 근거가 아니다.

## 검증

- 종료 시 예약 Worker `cardrag-worker-worker-run-020ef677b667`은 **running**, image ID `sha256:2eed6edbeb4f1d57d69481bd0d576dfbfcc957d29cb3bd89a5591619f5e9d121`로 정리 전과 동일했다.
- `curl -fsS --max-time 10 http://127.0.0.1:18015/health/ready` → `{"ready":true}`.
- `systemctl is-active cardrag-worker.timer` → `active`.
- 다른 실행 서비스 health도 기존 healthy 상태를 유지했다. 서비스를 재시작하거나 장기 Worker 완료를 기다리지 않았다.
- v1.0.33 태그/공개 릴리스 및 handoff 과거 문서를 삭제하거나 변경하지 않았다. 코드 변경은 없어 전체 테스트를 재실행하지 않았다.

상세 전후 측정, 제거 image/path 목록, 컨테이너 로그: `/opt/cardrag/008-6b42a1a/operations/cleanup-20261008/`.
