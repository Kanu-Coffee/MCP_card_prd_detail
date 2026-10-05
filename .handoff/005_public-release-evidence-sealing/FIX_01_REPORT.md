# FIX_01 — Executor REPORT (task 005, 2026-10-06 KST)

## 결과 판정

FIX_01의 **릴리스 계약 재설계(§1~§3, §5)와 후보 OCI 발행 전 검증(§4의 빌드·공급망부는 완료**했다. 그러나 **격리 후보 기능 실사(§4 후반)가 저장소 밖 운영 데이터의 구조적 결함으로 착수 불가**가 확인되어, §실행순서 3~4(증거 봉인·tag·dispatch·발행)는 **미실행**이다. FIX_01 §31에 따라 완료로 쓰지 않고 정확한 잔여 입력과 차기 행동을 아래에 기재한다. 공개 표면(GitHub Release `v1.0.29` 최신 유지, `v1.0.30`/`v1.0.31` tag 불변, Docker Hub `1.0.32*` 404, annotated `v1.0.32` tag 미생성)에는 변경이 0건이다.

## 완료 항목과 근거

### 1. 릴리스 계약 재설계 (push·CI 통과)

| commit | 내용 | CI run |
|---|---|---|
| `e6ccc82` | gold dispatch 입력 3종·56 portable 경로·`cardrag_mcp.evaluation`/`gold_capture`/`aggregation_profile` 필수 호출을 발행 경로에서 제거. readiness 번들 스키마(`cardrag.release-readiness-evidence.v1`, `release-readiness-manifest.json`, `readiness-evidence--` 자산 접두)로 일관改名(validate→publish 재검증→release 자산 조립/SHA256SUMS 전체). Release notes에 수행 검증/미수행(300~500 gold, `v109_baseline`, 5-lane, 통계 우위, 답변 품질) 구분 명시. | 37273732727 success |
| `435a99f` | **협은 release-readiness receipt 스키마** 신설: `cardrag.release-readiness-receipt.v1` + `cardrag.release-readiness-effective-config.v1`(v4의 70개 운영 필드 그대로, 연구 산출물 3필드 `document_aggregation_profile_sha256`·`document_aggregation_policy`·`retrieval_policy_sha256`만 제외) + `verify_release_readiness`(기타 12결속 불변식 전부 유지) + `cardrag_mcp.release_readiness` CLI(candidate_smoke 응답 모델 리플레이 재사용) + core 동작 테스트 6종. evidence root allowlist 강제(수락 목록 외 추가 regular file·symlink tree 거부). dispatch 입력 `release_readiness_sha256`. | 37378742323 success |

- narrow schema 착수 근거(FIX_01 §2 후문 절): 실측 운영 runtime은 profileless다. `/etc/cardrag/worker.env`에 `CARDRAG_DOCUMENT_AGGREGATION_PROFILE_*` 주입 없음, host 전체에 프로파일 산출물 없음(find), `compose.yaml`은 optional(all-or-nothing). 반면 `verify_candidate_acceptance`는 `manifest.sealed_profile_sha256` 비NULL을 강제(`generation_profile_unsealed`)하여 **실측 실행을 receipt로 봉인 불가**. gold 없이 profile을 만드는 것은 허위 작성이고, 허위 봉인 금지 조항이 이를 거부 → FIX_01 §2의 "별도 좁은 release-readiness schema/validator" 분기 발동. (과정에서 한 차례 재사용 설계로 되돌렸다가 위 실측 근거로 narrow 설계를 복원했다 — 이 경위는 본 문서에 남긴다.)
- 로컬 검증: `ruff check`/`format --check`(CI 경로) 통과, `mypy` 95 파일 통과, pytest **2282 passed**(신규 6 포함), release.yml 6 job YAML 파싱+내장 python AST 전량 통과.
- `1.0.32` 전수 일치 확인: `rg` 부재로 grep 전수 — 잔존 `1.0.31`은 `candidate_acceptance.py` 호환 Literal 3곳과 release_version 분기 1곳뿐(과거 receipt 재검증 가능 유지, 의도적).

### 2. 후보 OCI 이미지 발행 전 검증 (격리 패키지, 공개 surface 아님)

source commit `435a99f78fb48f1936bcedf7c7fd315920a64ccd`, 빌더 `cardrag-release-v1026`(BuildKit v0.32.2), 원격 git context, `--platform linux/amd64`, `--attest type=provenance,mode=max,version=v0.2`, SBOM generator `docker/buildkit-syft-scanner:stable-1@sha256:ae4f3b55…`(1회차 push는 generator purl에 digest가 빠져 검증기 거부 → 고정 인자로 재빌드; 1회차 산출물은 동일 tag에 덮어쓰기됨, 판정 전에 폐기 확인).

| role | index digest | platform manifest | platform config | attestation manifest |
|---|---|---|---|---|
| worker | `sha256:ebfc5fec228b715b3ec72e7c6168357d9dc6822670c36eb72c8feb3ed92cd566` | `sha256:dd40b143…f635` | `sha256:89d17271…12b2` | `sha256:3712776d…19e8` |
| mcp | `sha256:e8334fa5e970e5491be19e214826e44e72304973902f048ffd011a1f520ef40b` | `sha256:f72efc5e…dde0` | `sha256:fe7df685…1996` | `sha256:9eb01f5b…879d` |

- 저장소 jq 검증기 5종(`validate-candidate-oci-index`/`platform-manifest`/`attestation-manifest`/`provenance`/`sbom`)을 고정 crane v0.22.0(checksum 일치) + `validate-strict-json.py`으로 익명 재현: **worker/mcp 전부 PASS**. provenance subject·빌드인자 9종·materials 불변성·git secret 선언/무마운트 검증 통과.
- 라벨 실측: `org.opencontainers.image.revision=435a99f…`, `version=1.0.32`, entrypoint `cardrag-worker`/`cardrag-mcp`, user `10001:10001`, `linux/amd64` 확인.
- 이미지는 공개 후보 GHCR 저장소 `ghcr.io/kanu-coffee/mcp-card-prd-detail-candidate`의 `candidate-v1.0.32-<role>-<40hex>` tag로 존재(후보 전용 namespace; Docker Hub·Release 미触及).

### 3. 격리 자원 준비와 되돌림

- 신규 볼륨 3종(빈 볼륨 정책 준수 확인용) 생성 → 시드 dry-run 후 **전량 삭제**, compose 네트워크도 삭제. 운영 볼륨(`cardrag-worker-v130-candidate-state` 등)에는 `mode=ro&immutable=1` SELECT 쿼리만 수행(쓰기 0건).

## 차단 사항 (정확한 사실과 재현)

`seed-state-v122`는 선택 generation의 **모든 manifest document에 대해 discovery snapshot 바인딩**을 요구한다(state_seed_v122.py:864 `source_record_missing`). 2026-10-06 03:00 KST 운영 실행의 최신 generation `g-03fbc4f18a3c450bb017e2fd-36bae25dd8cd`로 격리 후보 대상 볼륨에 dry-run 시:

```
status=blocked  reason_code=source_record_missing:
source_16452578404de2315a5cf908418f954f4b98cbff241cc5e930432e65b0155fd2
```

진단: 해당 source는 woori 835848 `product_description`의 **superseded 이전 버전**(2026-09-01 `superseded_by_source_id` 기록)으로 manifest의 historical revision 근거 문서를 구성한다. 반면 003 rolling baseline_compaction 이후 운영 `snapshot` 테이블은 run별 집계 payload **173행(19 run)**만 보존하고 문서별 discovery 바인딩을 보유하지 않는다. 001 era 시드 성공 사례(accepted_sources 5,203)는 compaction 이전 상태 기준이며, 현재 호스트에는 compaction 이전 완전 상태 볼륨이 존재하지 않는다(002/003 정리 이력).

금지된 우회(불실행): 수동 SQLite 복사·임의 스냅샷 행 작성으로 시드 검사 무력화(RELEASING 55~57·FIX_01 §1·§2), 시드 검사 완화 코드 변경을 승인 없이단행(커밋된 검증 도구의 신뢰 모델 변경 — Reviewer 판단 사항).

따라서 §실행순서 2 후반(실Run·12도구 실측 receipt)→3(봉인)→4(tag·dispatch)는 정当한 입력 결여로 도달 불가. **v1.0.32 tag 생성 0건, release.yml dispatch 0건, Docker Hub 게시 0건, GitHub Release 변경 0건.**

## 운영 불변 재확인 (2026-10-06 08:1x KST)

- `cardrag-worker.timer`: 마지막 10-06 03:00 KST(성공), 다음 10-07 03:00 — 본 작업과 무관하게 정상.
- `cardrag-stable-v1026-mcp-1` Up 8 days (healthy), host-proxy healthy.
- WebDAV stable/candidate 채널 포인터 2종: 본 세션 최초 프로브와 **바이트 동일**(generation·manifest·ready sha 일치), 쓰기 0건. 공유 OCR cache·epoch 무접근(시드 dry-run 단계에서 중단).
- 디스크 `/` 104G 여유(buildx cache +2.7G는 재빌드 가속용 유지, 001 §FIX_03 판단 이월 관례).
- 저장소 표면: `main == origin/main == 435a99f`, working tree clean.

## Secret/권한 점검 한계

- `dockerhub-public` 환경 존재 확인(`can_admins_bypass=false`). 다만 현재 `gh` OAuth 토큰으로는 환경 secret 목록 API가 404를 반환해 `DOCKERHUB_USERNAME`·`DOCKERHUB_TOKEN` 이름 등록을 **API로 재확인하지 못했다**(FIX_01 §7 "현재 API 재확인 가능" 전제와 불일치 — 토큰 scope 한계로 추정). 실제 게시 권한의 최종 실증 수단은 공식 publish job이며,本轮では 미도달.

## 미실행 항목

1. 격리 후보 Worker 1회 terminal 실행·게시 READY·OCR 재사용/공급자 호출 실측
2. MCP 12도구 discovery+실호출·rollback 5단계·baseline 복귀·stable/OCR cache 불변 감사
3. `release-evidence/v1.0.32/` 13파일(receipt 1 + bound evidence 12) 생산·검증·봉인 commit
4. 발행 전 readiness preflight(workflow 동일 검증기 실데이터 실행)
5. annotated tag `v1.0.32` 생성·게시
6. 공식 `release.yml` dispatch와 Docker Hub·GitHub Release 발행·대조
7. 운영 이미지 전환(본 과제 범위 밖 — 자동 수행하지 않음)

## 차기 행동 (Planner/Reviewer 판단 필요)

정렬 우선순위 없이, 선택지:

- **A. 시드 도구 확장(권장 검토 대상)**: `seed-state-v122`의 discovery 바인딩 요구를 durable 근거(`pdf_cache_source`+`pdf_cache_source_revision`+manifest+corpus-baseline)로 재해석하는 변경. 신뢰 모델 변경이므로 PLAN/FIX 발행 후 새 source commit으로 진행 필요 — 이 경우 본 세션의 후보 이미지는 폐기되고 동일 commit 패턴으로 재빌드·재검증된다.
- **B. 격리 후보 state의 별도 무결성 근거 승인**: 커밋된 시드 경로 대신 offline copy를 명시 승인(RELEASING 개정 포함). 임베딩 캐시는 현행 규정이 커밋된 명령 외 경로를 금지하므로 seed-embedding-cache-v122의 동일 문제 재발 여부 선행 확인 필요(미조사).
- **C. 발행 연기**: 1.0.32 게이트 계약을 현행 narrow schema로 동결하고, 실사 가능한 시드 공급처가 확보되는 다음 릴리스에 적용.

어느 경로든 §22-24 절차(후보 1회 실Run→readiness 증거→preflight→봉인 commit→tag 1회→dispatch)와 본 FIX가 확정한 narrow 스키마·allowlist는 그대로 재사용 가능하다.

## 잔여 위험

- 후보 GHCR tag `candidate-v1.0.32-*`는 mutable tag에 재push된 이력이 있다(1회차 폐기). Docker Hub 발행은 0건이므로 공개 불변성 위반은 없다. 공식 workflow의 익명 재검증은 최종 digest만 참조해 영향 없음.
- 시드 도구·운영 compaction 간 불일치는 **차기 모든 릴리스에도 동일하게 재발**한다(상태는 계속 compaction되기 때문) — A/B 중 하나의 항구 결정이 필요하다.
- 환경 secret 실명 재확인 불가(토큰 scope)는 dispatch 직전까지 미해결로 남는다.
