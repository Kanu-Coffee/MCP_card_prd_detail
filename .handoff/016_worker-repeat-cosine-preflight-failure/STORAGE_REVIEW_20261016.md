# 016 저장소 Reviewer 실사 및 회수 결과 — 2026-10-16

## 결론

Worker state는 이 프로젝트의 **현재 운영 상태**이고 Paddle은 운영 OCR fallback 모델이다. 두 볼륨에서 즉시 삭제해도 안전한 대형 잔여물은 확인하지 못했다. libvirt VM은 다른 프로젝트의 개발·검증 환경이지만 최근 사용 이력이 있으므로 삭제하지 않았다. 대신 종료된 VM 디스크를 내용 보존형 qcow2 압축으로 교체하여 **9,016,520,704 bytes = 9.02 GB / 8.40 GiB**를 실제 회수했다.

이 문서는 기존 handoff를 보존하는 신규 Reviewer 운영 기록이다. 제품 코드 변경·배포는 하지 않았다. 코드 리뷰 수정 요청은 `FIX_03.md`에 있다.

## 생성 주체와 사용 목적

| 자원 | 생성·소유 근거 | 현재 용도 / 판정 |
|---|---|---|
| `cardrag-worker-state` | Docker 생성 시각 10/08 14:43:46 KST. 010 운영 전환 REPORT가 candidate-state를 안정 운영 이름으로 이동한 이력을 기록. 현재 `/opt/cardrag/current/deploy/worker/compose.secrets.yaml` external 참조 | `MCP_card_prd_detail` 운영 Worker의 증분 상태, PDF/OCR/임베딩 캐시, 현재·직전 결과 및 rollback 자료. 이름에 candidate였던 이력만으로 개발 잔여물이라고 볼 수 없음 |
| Paddle 모델 볼륨 | 생성 시각 09/20 12:23:58 KST. 운영 Compose 및 OCR 설정 참조 | Codex primary 장애 시 local-paddleocr/PaddleOCR-VL-1.6 fallback. 삭제하면 다음 사용 시 재다운로드·초기화·네트워크 의존 발생 |
| HAOS VM | `/home/lee/projects/antigravity_for_HA/tools/development/haos-vm`, checkout `4e4fc806baf5`; 등록 domain `antigravity-haos-4e4fc806baf5` | HAOS 18.2 개발·검증 환경. persistent, shut off, autostart disabled, managed save/내부 snapshot 없음. 디스크/NVRAM 최근 사용 흔적 10/14. 불필요 확정 근거 부족 |

## Worker state 45.9 GB 상세

아래는 파일시스템 실제 할당량(bytes)이다. 표시 단위 및 파일시스템 반올림으로 논리 크기와 차이가 있다.

| 항목 | 할당 bytes | 의미 |
|---|---:|---|
| runs | 34,101,248,000 | 오늘 run `9270ff0a…`와 직전 ready `ec1255d9…`. native retention=2 보호 대상 |
| worker-state.sqlite3 | 7,226,638,336 | stage/snapshot 및 임베딩 캐시 |
| pdf-cache | 4,507,963,392 | 4,858개 원본 PDF 재사용 캐시 |
| ocr-seed | 104,595,456 | OCR 재사용 자료 |
| backup-ledger.sqlite3 | 24,420,352 | 백업 처리 이력 |
| audit-reports / contracts | 13,557,760 / 11,423,744 | 검사·계약 자료 |

두 run의 sealed 결과가 각각 약 15.04/15.02 GB이며 current documents 약 3.95 GB, previous documents 약 95.7 MB다. 이전 정리로 누락됐다가 FIX_01에서 복구한 previous seal을 재삭제하지 않았다. 대형 미보호 run은 발견되지 않았다.

SQLite read-only 점검에서 `embedding_cache_v5`가 **6,749,876,224 bytes**를 차지했다. `freelist_count=0`, `page_size=4096`으로 단순 VACUUM을 통한 빈 페이지 회수 여지가 없다. 전체 볼륨/DB/cache 삭제는 증분 실행·복구·재처리 비용과 연결되므로 수행하지 않았다. 종료 후 제거되는 oneshot Worker 때문에 dangling으로 보여도 실제 미사용 볼륨이 아니다.

Paddle 실제 모델 할당량은 VL-1.6 **1,930,518,528**, DocLayoutV3 **132,014,080**, doc orientation **6,885,376 bytes**다. 구버전 중복 모델은 없었으며 다른 cache/temp 폴더는 비어 있다. 대형 정리 대상이 아니다.

## 실제 수행: VM 디스크 무손실 압축

- 실행: **10:11:07–10:22:51 KST**.
- 대상: `/var/tmp/antigravity-haos-vm-1000-4e4fc806baf5/instances/antigravity-haos-4e4fc806baf5/system.qcow2`.
- 원본 할당: **20,459,876,352 bytes** → 압축본 할당: **11,443,355,648 bytes**.
- 가상 디스크 크기 **34,359,738,368 bytes (32 GiB)**와 guest 내용은 동일하다.
- backing base `/var/tmp/antigravity-haos-vm-1000-4e4fc806baf5/cache/18.2/haos_ova-18.2.qcow2`는 overlay가 실제 참조하므로 삭제하지 않았다.

프로젝트 helper와 같은 exclusive manager lock을 획득하고 종료 상태·snapshot 없음·원본 regular file/clean 상태를 확인했다. 별도 임시 경로에 `qemu-img convert -c`로 qcow2 zlib 압축본을 생성했다. 전체 가상 내용에 대한 `qemu-img compare`는 **Images are identical.**, 압축본 `qemu-img check`는 **check-errors=0**이었다. 비교 후 원본과 base가 외부 변경되지 않았는지 확인하고 소유자·권한·시간을 보존했다. 원본 복구용 hardlink를 유지한 상태로 atomic replacement와 재검사를 수행한 뒤 원본 임시 링크를 제거했다. 교체 후에도 `check-errors=0`, 동일 backing/virtual size 및 종료 상태를 확인했다. 변환 임시 파일은 남아 있지 않다.

이 방식은 guest 파일이나 VM 등록을 삭제하지 않는다. 이후 guest가 데이터를 쓰면 qcow2 공간이 다시 증가할 수 있고 압축 블록 읽기에 CPU 비용이 생길 수 있다. **VM 부팅 검사는 수행하지 않았다.** 디스크 전체 내용 비교, 무결성 및 등록 상태 검증까지 완료했다.

## 사후 검증 및 증거

- 소유 프로젝트 helper `status --json`: configured=true, checkout 일치, HAOS 18.2, domain_state=shut off. 종료된 VM의 ready=false는 예상 상태다.
- libvirt domain 등록/UUID, autostart disabled 및 기존 disk 경로 보존.
- Docker 컨테이너 **15개** ID/이름/상태/health와 볼륨 **40개** 이름은 기존 baseline 대비 동일. Worker/Paddle 볼륨 삭제 없음.
- MCP `/health/ready`: **ready=true**. Worker timer active, 다음 10/17 03:00 KST.
- 최종 파일시스템 available 약 **127,358,554,112 bytes (127.36 GB)**, 사용률 **69%**. 실행 중 임시 압축본 공간은 완료 후 정리됐다.
- 이번 회수량은 위 VM **9.02 GB**다. 앞선 BuildKit 정리 회수량 **33,971,699,712 bytes**는 별도 기존 작업이며 이번 수치에 중복 포함하지 않는다.

운영 증거 디렉터리: `/opt/cardrag/016-evidence/operations/reviewer-storage-20261016/`:

- `vm-lossless-compaction.json`: 시작/종료, 원본/압축본 할당, 전체 내용 비교, 사전·사후 무결성 결과.
- `vm-status-after.json`: 소유 프로젝트 helper의 비밀정보 제외 상태.
- `health-after.json`: 컨테이너/볼륨 baseline 비교, MCP readiness, 최종 여유 공간, 임시 파일 없음.

## 후속 최적화 과제

Worker sealed DB/vector의 Worker/serving 중복 저장은 큰 절감 후보다. 현재 regular-file/seal/rollback 계약을 조사하고 immutable 공유 저장·retention·안전한 삭제 순서를 설계한 뒤 별도 제품 변경으로 검증해야 한다. 당장 임의 symlink/hardlink나 보호 run 삭제로 대체하지 않는다. 임베딩 캐시 역시 cache-hit/재생성 비용과 active profile 사용률을 계측한 뒤 범위가 명확한 GC 정책을 검토한다. 해당 개발 VM을 폐기할지는 최근 사용과 다른 프로젝트 의존성을 감안한 수명주기 판단이 필요하며 이번에는 내용 보존 방식으로 회수했다.
