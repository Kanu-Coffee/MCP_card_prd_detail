# 012 REPORT — v1.0.34 발행·운영 반영 완료

작성: 2026-10-08 Executor. **운영 인수 및 정식 공개 발행 완료.**

## Git·버전·릴리스

- 011 인수 runtime72c28ac를 포함하여 core/Worker/MCP 패키지·Worker 런타임 상수·lock·README·릴리스 문서를1.0.34로 맞췄다. 버전별 공급망 검증 gate를34로 맞추고 기존32/33 qualification 검증 호환성은 보존했다.
- [PR #43](https://github.com/Kanu-Coffee/MCP_card_prd_detail/pull/43) main 병합 완료: d8598686a31e858336e8db8d65bb8e84fa68bdad. 병합 브랜치의 원격/로컬 참조를 삭제했고 열린 PR은0개다. 기존 공개 Release/tag는 보존했다.
- 공개 이미지 source: `3a3cb60abfdbacb64cd55f1a4a196e1093e3f246`. [정확한 source CI](https://github.com/Kanu-Coffee/MCP_card_prd_detail/actions/runs/37764278147) 성공, **2,480 passed /9 expected runtime warnings**. 이 테스트는 live OCR/embedding 실행이 아니다.
- evidence만 추가한 봉인 commit/annotated tag v1.0.34: `9f842af028543cc1441d909b243202a9e6c7b49e`. [봉인 CI](https://github.com/Kanu-Coffee/MCP_card_prd_detail/actions/runs/37765174767) 성공. source→tag diff는 qualification JSON1개뿐이다.
- qualification SHA256: `993d5a6891729cfb80ce70a863eccb3657f134ab2fe3165f376e4d29ec66247a`.
- [공식 발행 workflow](https://github.com/Kanu-Coffee/MCP_card_prd_detail/actions/runs/37765993435) 전 job success. [GitHub Release v1.0.34](https://github.com/Kanu-Coffee/MCP_card_prd_detail/releases/tag/v1.0.34)는 draft/prerelease가 아닌 Latest 공개본이다.

| 공개 역할 | immutable digest |
|---|---|
| MCP | `sha256:371895b40861244405a7ee5e6c23def3aa460e1d5da647a729afc405e4139d80` |
| Worker | `sha256:65f066e115b621fdc901e9176d703e88678a63f6b7082dcbfe20f3767740ca94` |

Docker Hub `ymtop59/mcp-card-prd-detail`의 `1.0.34-mcp`, `1.0.34-worker` 및 각 `-sha-3a3cb60abfdb` alias4개를 익명 원격 manifest 해시로 다시 대조했다. GitHub 자산24개 다운로드, SHA256SUMS에 실린23개 모두 일치. qualification bytes/source/seal/digest와 manifest 결속도 확인했다. `evidence/release-verification.json`.

## 수행 검증과 시행착오

- 011의 고정34+층화무작위30, 총64개/8개 카드사 인수는 ACCEPTANCE.md에 근거한다. Reviewer 집중118건 및 Executor MCP883건 인수 증거를 이번 배포 때문에 반복 생성하지 않았다.
- 발행 준비의 qualification/workflow 계약 테스트25건, 추가 버전/OCI 계약 테스트29건 통과. actionlint/uv lock 검사 통과. 실제 후보2개에 익명 접근·OCI 구조·provenance/SBOM 검증기5종·버전/source 라벨을 독립 대조했다.
- 첫 CI는 이전버전33을 기대하는 테스트 fixture 때문에5건 실패했다. fixture와 Worker 런타임 상수·후보 허용버전을 맞춘 뒤 최종 source/봉인 CI를 통과했다. 실패한 초기 source로 공개하지 않았다.
- 후보 빌드는 정확한 원격 Git context, BuildKit0.32.2, 고정 Syft scanner, provenance v0.2였다. 공식 workflow의 strict filesystem/image 검사·tag 충돌 preflight·cosign 서명·자산 검증을 우회하지 않았다.
- 소스 archive 갱신이 기본 Compose overlay를 복원해 첫 전환 사전 image 검사에서 중단됐다. 기존 MCP를 교체하기 전이었으며 운영 데이터/컨테이너 변화는 없었다. 현행 host overlay를 재적용하고 **렌더링된 Worker runtime 전체 동일성**, MCP image/container/external volume을 다시 검사 후 전환했다.

## 운영 반영 결과

- `/opt/cardrag/current` → `/opt/cardrag/v1.0.34`. 컨테이너/project `cardrag-mcp`, 기존 `cardrag-mcp-state` 및 `cardrag-mcp_default` 유지. 대형 state 복제나 volume 이름 변경을 하지 않았다.
- **실제 운영 MCP는 공개 v1.0.34 digest/source와 동일**하며 healthy/readiness=true다. 신규 MCP 초기 준비는 약7분5초였고 최대15분 timeout/이전009 복원 경로를 준비했다. 성공/준비 실패 rollback 경로는 격리 mock으로 사전 확인했다.
- `2026-10-08T20:02:39.718367` 인증 HTTP 확인: 우리500107, 신한01208, KB00917, BC BD001 별칭, KB09063, 신한00549, 신한00157 **7상품**의 generation/revision/연회비/혜택제목/혜택상세/조건이 인수된 FIX_01 값과 동일했다. 각 상품의 같은 revision benefit contract bundle도 확인했다. BC는 canonical code가 긴 identifier인 상태를 정상으로 검사했다.
- LibreChat 중계 nginx reload 후 proxy/LibreChat readiness 및 LibreChat 컨테이너에서 인증된500107 실제 호출 통과. 두 컨테이너 healthy.
- Worker는 인수된 `cardrag-worker:009-b544a80` 유지. OpenCode / alibaba-token-plan/qwen3.8-flash / medium, fallback 빈 값도 보존. 공개 Worker34는 발행했지만 운영 Worker 교체는 이번 MCP 요약 반영에 필요하지 않아 실행하지 않았다.
- Worker service inactive, timer active, 다음 **2026-10-09 03:00 Asia/Seoul**. PDF/OCR/embedding/Worker 전량 실행 및 WebDAV 신규 generation 발행은 하지 않았다. serving generation은 g-eba5ca0d13924abdb1f36937-71a5fd98d58b 유지.

상세 증거: `evidence/cutover-status.json`, `evidence/operating-state.json`.

### sudo·권한과 계획 조정

systemctl 쓰기는 대화형 sudo 인증이 거절됐다. 이번 반영은 운영 Worker 설정/이미지/볼륨을 바꾸지 않는 MCP 단독 교체이므로 타이머를 변경하지 않았다. 전환 직전 Worker service/실행 컨테이너 없음, 다음 예약까지1시간 이상을 guard로 확인했고 atomic current 전환은 준비·인증 smoke 완료 후 수행했다. timer 전후 active다. root 권한 우회나 privileged host 작업을 하지 않았으며 사용자의 추가 sudo 실행은 필요 없었다.

기존009 snapshot의700 경로/600 Worker overlay가 systemd의 cardrag 계정 접근을 막는 상태도 확인했다. 새 source 디렉터리의 탐색 권한과 공개 경로 참조 overlay의 읽기 권한을 맞추고, 직전009 rollback Worker 경로/파일에도 cardrag 접근 ACL을 부여했다. UID/GID10001로 실제 Compose/env 파일 읽기를 확인했다. `/etc/cardrag`의 비밀 파일 내용/권한은 변경하지 않았다. systemd의 Documentation 진입점인 SIMPLE_RUNTIME.md도 복원하고 운영 문서에 현재 canonical 이름·배포 범위를 기록했다.

## 정리·롤백

- 정상 전환/실사용 smoke 후 이전008 source와 Worker008/Worker007/MCP007 이미지3개를 전체 컨테이너 참조/bind 없음 확인 후 삭제했다. 관측된 추가 여유 공간 **약7.40GB**. 운영/state/auth/Paddle volume은 삭제하지 않았다.
- 이번 임시 BuildKit builder/container/state는 후보 빌드 후 제거했다.
- rollback은 **009 snapshot + MCP009/Worker009 이미지1세트**만 보존한다. Worker009는 현재 예약 실행 이미지이기도 하므로 삭제하지 않았다. `evidence/cleanup.json`.
- 재전환 스크립트/상태는 `/opt/cardrag/v1.0.34/operations`에 보존했다. 이미 성공한 cutover script를 다시 실행하면 current guard가 거절한다. 롤백은 Worker 정지 상태를 확인하고009 MCP overlay로 up한 뒤 최대15분 readiness·LibreChat reload/인증 호출을 확인하고 current를009로 복원한다. 공통 state의 별도 과거 복제본은 보유하지 않는다.

## 남은 운영 관찰과 한계

현재 기능은 인수 가능하고 공개 릴리스/운영 반영을 완료했다. 다음03시 정상 예약은 운영 관찰 대상이며 추가2일 대기가 완료 조건이 아니다. 후보 전체 Worker/후보12도구/gold 평가는 이번 발행 gate에서 미수행으로 명시한다. immutable qualification의 production_cutover 미수행 기록은 **공개 발행 당시 gate 상태**이며, 이 보고서의 MCP 단독 운영 전환은 발행 후 수행한 별도 결과다.

새 상품의 예시·단독 금액·빈 제목·5개 제한 등011 ACCEPTANCE의 허용 한계는 유지된다. 보고서·운영 문서/evidence의 마지막 커밋은 tag 이후 문서 정리이며 공개 이미지/runtime source를 바꾸지 않는다. 토큰·전체 env·비공개 운영 로그를 공개 evidence에 포함하지 않았다.
