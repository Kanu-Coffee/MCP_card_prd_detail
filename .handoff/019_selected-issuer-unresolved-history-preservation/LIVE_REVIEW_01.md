# 019 우리카드 첫 라이브 실행 검토 및 상태 도구 권한 수정

2026-10-19 Codex. 사용자 요청: 배치 완주, 우리카드 실행에 의한 전체 목록 덮어쓰기 여부, status/summary 오류, 실행 시간 단축 여부를 점검한다.

## 판정

**수집·OCR·서빙 게시는 성공했다. 비선택 7사 덮어쓰기/재처리는 확인되지 않았다. 백업은 메타파일 2건이 남은 degraded 상태이므로 전체 운영 검증 완료로 판정하지 않는다.**

- 컨테이너: `cardrag-live-woori-20261019T112845Z`, exited/exit0, OOM 없음.
- 컨테이너 실제 시간: 20:28:45.734–21:59:19.363 KST, **90분 33.629초**.
- run: `41ce678e665b47179f8bfb2d82273882`, succeeded.
- run DB 시간: 20:29:05.184–21:46:50.328 KST, 77분45.145초. 최종 백업 등 후속 단계는 이 DB finished_at 이후에도 진행되므로 이를 컨테이너 총 시간으로 사용하지 않는다.
- 새 generation: `g-41ce678e665b47179f8bfb2d-e884484ab66b`.
- `quality_status=passed`, `collection_status=complete`, `backup_status=degraded`.

## 전체 목록 및 비선택 보존 검사

실행 직전 baseline manifest와 신규 manifest를 비교하고, 이전/신규 sealed SQLite의 `product_lineages JOIN contract_revisions` 전체 행을 document_id로 대조했다.

| 카드사 | 실행 전 문서 | 실행 후 문서 |
|---|---:|---:|
| 우리 | 979 | 980 |
| KB | 751 | 751 |
| 신한 | 931 | 931 |
| 삼성 | 766 | 766 |
| 현대 | 605 | 605 |
| 하나 | 735 | 735 |
| 롯데 | 647 | 647 |
| BC | 122 | 122 |
| 합계 | 5,536 | 5,537 |

- 비선택 7사 manifest document 변경 **0건**, DB 계약/상품 행 변경 **0건**. 행 비교에는 상품명/issuer 및 모든 contract_revision 필드를 포함했다.
- 변경은 우리 신규 current 문서 **KG이니시스 우리카드(product_code=500111)** 1건뿐이다. PDF 7페이지, OCR 본문 생성 확인.
- 비선택 stage 기록 **0건**, issuer HTTP metrics도 우리카드 요청만 존재한다. 나머지 7사는 not_scheduled이며 해당 카드사의 기존 freshness가 유지됐다.
- 실제 scope selected는 `[woori]`다. scope 파일의 `mode=all` 표시는 기존 부수 이슈지만 전체 실행의 증거가 아니다. selected, HTTP, stage 및 게시 DB를 함께 확인했다.
- 우리 목록 920건 수집, adopted920, 실패 issuer0. PDF cache hit916, 다운로드3, 재검증3, revision 변경0. 다운로드 전체 재수행이 아니다.
- 우리 문서 처리980건 중 OCR 재사용979, provider OCR1, OCR 실패0. embedding provider call1, 새 view embedding37건, 나머지는 캐시 사용이다.
- 미해결 과거 ledger22건은 final inventory에서 past_excluded21/updated_to_latest1로 정리됐고 새 ledger0건이다. 단순 수치만 보지 않고 inventory를 확인했다.
- unsupported DRM은 기존과 같은 총8건(우리4/KB3/현대1), ZIP unsupported_attachment0건이다. collection complete는 DRM이 모두 복원됐다는 뜻은 아니다.

## status/summary 권한 오류와 수정

준비한 도구의 live 디렉터리에 setgid 그룹 상속 설정이 빠져 있었다. 호스트 lee가 만든 `woori.json`과 baseline 파일이 UID/GID1000, mode0640이었고, 컨테이너는 UID/GID10001(cardrag)이어서 읽지 못했다. 배치 자체의 실패가 아니라 조회용 bind mount 파일 권한 문제다.

- 기존 live 파일의 group을 cardrag로 조정하고 group read를 부여했다. live 디렉터리는 mode2750으로 바꿨다. world-read 허용이나 컨테이너 root 실행으로 우회하지 않았다.
- `run-issuer.sh`에 `install -d -m 2750 -g cardrag "$OPS_DIR/live"`를 추가해 다음 실행의 메타데이터에도 그룹 상속을 보장했다.
- `status-issuer.sh`는 종료 컨테이너 로그의 최종 JSON에서 backup/quality/unsupported 결과를 저장하도록 보완했다.
- `summary-issuers.sh`는 backup_recorded와 별개로 backup_status 및 pending 수를 표시한다. 현재 `woori succeeded / other_changes0 / backup_status degraded / pending2 / backup_recorded True`로 나온다.
- 이전 결과 스냅샷은 `woori.result.before-status-enhancement.json`으로 보존했다.
- helpers shellcheck 통과, 두 명령 실제 재실행 성공, 호스트에서 새로 생성한 mode0640 파일을 컨테이너 UID/GID10001이 읽는 테스트도 통과했다.

수정은 host release의 operations 도구에 한정했다. worker/core 제품 코드 및 이미지를 바꾸거나 배치를 재실행하지 않았다.

## 실행 시간 비교

비교 기준은 직전 전체 배치 `f1871a51eff549ea9aee95b73eaad99d`다. 이 배치는 8사 scope였지만 실제 수집은 6사 성공/2사 carry였고, 신규 OCR은0건이었다. 코드 버전/캐시/원본 작업량도 달라 통제된 성능 벤치마크는 아니다.

| 같은 계측 항목 | 직전 전체 배치 | 이번 우리 단독 |
|---|---:|---:|
| performance elapsed(후속 작업 포함) | 99분47.622초 | 89분57.323초 |
| run DB 시작→완료 | 87분35.763초 | 77분45.145초 |
| OCR 및 local processing 계측 | 24분52.362초 | 6분1.798초 |
| export 계측 | 39분47.409초 | 41분33.494초 |

performance elapsed 기준 **약9.9%**, run DB 기준 **약11.2%** 짧았다. 선택 카드사 OCR/local 처리 비용은 크게 줄었지만 전체 실행이 1/8로 줄지는 않았다.

공통 병목의 직접 근거:

- 현재 export가 2,493.494초로 컨테이너 시간의 약46%를 차지한다.
- embedding cache 조회는 전체 배치395,137회→우리 단독395,174회, export cache 조회는619,092회→619,134회로 거의 동일하다.
- 새 generation을 만들면서 8사 전체 5,537문서와 약61.9만 evidence의 서빙 DB/벡터를 다시 조립·export하는 공통 비용이 남는다. 이는 비선택 카드사 origin/OCR/provider를 재호출했다는 의미는 아니다.
- 성능 보고서의 physical read_bytes도 직전289GB/이번299GB 수준이며 디스크/DB 재조립 부담이 여전히 크다. 이후 단축 목표는 선택 수집보다 전체 export 및 대량 캐시 조회의 재사용 경계다. 이번 점검에서 성능 관련 제품 코드를 임의 변경하지 않았다.
- accumulated_seconds는 중첩/누적 구간이므로 항목들을 단순 합산해 총 시간을 만들지 않는다.

## 백업 미완료 원인

- 해당 run은 backup_recorded_runs에 정상 등록됐다.
- 실제 업로드300,390bytes는 신규 PDF285,264bytes와 OCR 본문15,126bytes의 합이다. 설정된 canonical remote root의 backup_receipts에서 두 hash/크기와 이번 실행의 검증 시각을 확인했다.
- 남은 항목은 신규 KG이니시스 문서의 native OCR `manifest.json`1,791bytes와 `READY.json`285bytes, **총2,076bytes**다.
- 신규 manifest document의 `ocr_reuse_key`/`ocr_cache_kind`는 null인데, `record_document_ocr`는 native OCR metadata를 pending에 등록했다. 선택 실행의 `scope_remote_paths`는 document에 reuse_key가 있을 때만 native manifest/READY 경로를 허용하므로, 해당 문서가 우리카드임에도 이 두 경로가 scope filter에서 제외됐다. 백업 doc_info의 document_id/PDF/OCR hash가 신규 문서와 일치하는지 확인했다.
- 따라서 정상 게시와 별개로 backup_status는 degraded이고 pending2다. 캐시/백업 metadata의 연결 또는 scoped 허용 경로 보완이 필요하다. pending 행 삭제나 원격 immutable 파일 덮어쓰기로 성공 처리하지 않았다.
- monitor의 범위 receipt coverage(3660경로 중1896경로에 기존 receipt 관측)는 원격 재검증 결과가 아니며, 이 값만으로 기존 전체 백업이 유실됐다고 판정하지 않는다. 이번에 확인한 미완료는 실제 pending2건이다.

## 증거와 후속 경계

host `/opt/cardrag/current/operations/live/`에 컨테이너 전체 로그, `woori.result.json`, `woori.execution-result-from-log.json`, `woori.analysis.json`, `woori.serving-comparison.json` 및 원래 baseline을 보존했다. 타이머와 Worker service는 inactive로 유지했다.

현재 아래 명령은 권한 오류 없이 동작한다.

```bash
/opt/cardrag/current/operations/status-issuer.sh woori
/opt/cardrag/current/operations/summary-issuers.sh
```

이번 점검은 읽기 전용 운영 DB/서빙 조회와 host 도구 권한·표시 수정이다. 새 카드사 배치, provider 추론, backup flush/원격 쓰기, timer 활성화는 수행하지 않았다. 다음 운영 완료 판정에서는 백업 metadata2건 보완을 별도로 확인하고 나머지7사를 순차 검증한다.
