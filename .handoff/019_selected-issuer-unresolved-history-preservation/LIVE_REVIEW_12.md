# 019 LIVE_REVIEW_12 — 첫10시 자동 순회는 기동 성공, 우리 PDF origin HTML 응답으로 배치 실패

2026-10-22 Codex, Reviewer. 사용자 요청에 따라 journal/systemd, 실제 run 보고서·영속 stage/rotation과 기존 serving을 조사했다. 원인 확인을 위해 소수의 우리 공식 endpoint 요청만 수행했으며, 배치 재개·DB 교정·설정/예약 변경·백업 flush는 하지 않았다. 후속은 FIX_30으로 발행한다.

## 실패 단계와 운영 영향

- service10:00:00 시작/10:03:52 exit1. helper 경로로 실행됐고 OCR resolver/tokenizer/Qwen preflight를 통과했다. 이전 candidate image/volume 오선택 장애의 재발 증거는 없다.
- run `e81c17e69b5f428f8d6314ab2b8762d4`, selected=woori, round_robin. discovery922건/경고0,10:03:47 완료.
- 다운로드 도중 source `source_c363707aef7be75165e225bf96369ac88cd7784f3393116411535400083a3c0f`, product000000, `[상품안내장] BCTOP카드(2021.11)_00.pdf`에서 실패. stage attempt1/max4, last_error=issuer_source_invalid. collection 보고 exception_class=PDFValidationError다.
- 실제 download stage는 succeeded24/failed1/skipped897이다. `PDF progress completed=922`는 실패 후 quarantine으로 건너뛴897건도 집계한 것이며 다운로드922건 성공이 아니다. cache_hits26/revalidations2/downloads0이고 in-flight count와 영속 succeeded24는 의미가 다르다.
- origin_failure는 PDFValidationError를 issuer_source_invalid로 변환한다. 첫 실패 후 우리 전체가 quarantine되고, 선택사가 우리1사여서 successful_issuers가 비어 RuntimeError(all issuer collection attempts failed)로 종료됐다. CLI worker_unexpected_failure/worker-failure.json은 이 최종 generic wrapper이며 PDF 원문 진단을 담지 않는다. collection attempts2는 discovery/download 합산이고 실패 문서를2번 재시도했다는 뜻이 아니다.
- OCR/embedding/새 seal·게시 단계에 도달하지 않았다. MCP 실제 source page2개 및 current=Worker stable 일치를 확인했다. live는 신한 `g-666c2d2ee9e24d04acf76eed-d6ffd9a927e9`/기존5632건 유지다. 이번 실패를 WebDAV 백업 실패로 볼 근거는 없다.

## 직접 실패 재현과 미확정 부분

운영 image 네트워크에서 현재 snapshot의 정확한 SourceRecord를 읽어 같은 WooriAdapter.prepare_download 및 SecurePDFDownloader 경로를 실행했다. state volume은 추가 /state:ro로 읽었고 파일 다운로드는 TemporaryDirectory에서만 처리했다. 원본에 대한 제한된 진단 호출이며 WorkerPipeline/캐시 write/순회 배정을 실행하지 않았다.

1. 실패 문서 RAONK POST에 기존 ETag/Last-Modified 조건을 붙인 호출과 조건 없는 호출이 모두 HTTP200/text/html;charset=UTF-8로 PDFValidationError(response MIME type is not PDF-compatible)를 반환했다.
2. HTML2581bytes/title 우리카드, 표시 내용은 서비스가 원활하지 않다는 공식 Error 안내다. 전체 원문/암호화 request form/cookie는 증거에 저장하지 않았다.
3. 다른 기존 문서의 같은 RAONK 경로도200 HTML이었다. 공식 source URL 직접 접근은302이며 보안검증 downloader로 redirect를 따라가도 최종 PDF-compatible MIME 검사에 실패했다. direct URL로 우회하면 정상이라는 근거는 없다.
4. 공식 landing을 먼저 열어 세션을 만든 뒤 실패 문서를 요청해도 동일했다. 다른 최신 상품500108(효력일2026-10-21)도 동일 MIME 오류였다. 특정 구형 BCTOP 파일만의 손상/부재라고 단정하지 않는다.

**확정한 직접 원인은 PDF origin이 HTML 서비스 오류를 반환한다는 것이다.** 우리 origin의 일시 장애/파일 endpoint 정책·프로토콜 변화/네트워크 조건 중 어느 것이 근본 원인인지는 현재 증거로 확정하지 못했다. 조건부304 문제·landing cookie만의 문제로는 해결되지 않았다. 우리 host는 기존 선택 프록시 대상이 아니므로 신한·롯데 프록시 옵션 해제와 인과관계를 단정하지 않는다. HTML을 PDF로 허용하거나 cache를 fresh로 처리하는 것은 해결책이 아니다.

분산 origin audit는 round_robin에서 visit_days=len(adapters)=8일의 사전 점검 창을 사용한다. 따라서 감사 예정일10/23이 아직 오지 않았다는 이유만으로 오늘 재검증을 잘못된 강제 다운로드로 판단하지 않는다. 이 정책을 장애 우회 목적으로 바꾸지 않았다.

## 순회 상태와 재개 주의

RO SQLite의 WAL-aware mode=ro로 확인했다. WAL4,148,872bytes가 있어 immutable main-only 조사는 의도적으로 거부했고, 이후 정상 mode=ro로 DB+WAL을 읽었다. journal/sidecar를 지우거나 checkpoint하지 않았다.

오늘 daily/stable slot은 woori/run 위ID/result failed(rotation_execution_failed)로 영속 기록됐다. remaining queue는 kb/shinhan/samsung/hyundai/hana/lotte/bc다. 따라서 최초 자동 우리 배정 및 queue의 영속성은 확인됐으나 배치는 성공하지 않았다. 같은 날 기본 run을 반복하면 저장 실패 결과를 반환한다. 내일10/23 10시는 기존 정책대로 국민 차례다.

timer는 active/enabled, 다음10/23 10:00KST 유지했다. 우리 재처리는 원인 대응 후 명시 resume 절차로 해야 하며 cursor/result SQL reset·새 전체run으로 강제하지 않는다. 실제 재개 성공을 rotation/day 결과와 혼동하지 않도록 후속 보고에서 함께 확인한다.

## 검사와 증거

```text
uv run --no-sync pytest apps/cardrag-worker/tests/test_downloader.py apps/cardrag-worker/tests/test_017_scope.py -k 'downloader or rotation' -q
18 passed,52 deselected in1.58s
```

기존 HTML 거부/redirect/304/순회 회귀를 확인한 것이며 실제 우리 endpoint 복구를 뜻하지 않는다. 증거는 `/opt/cardrag/current/operations/live/auto-woori-failure-20261022/`의 service journal 및4개 진단 JSONL에0640으로 보존했다. 내용은 allowlisted 상태/MIME/해시·공개 파일 식별 수준이며 private proxy·인증값을 기록하지 않았다. 기존 snapshot/이미지/데이터를 변경하지 않았다. 019 자동운영 최종 인수는 미완료다.
