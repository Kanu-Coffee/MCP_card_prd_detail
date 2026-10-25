# 019 LIVE_REVIEW_09 — 롯데 재개 중 Codex 미분류 프로세스 종료

2026-10-21 Codex, Reviewer. 사용자 요청에 따라 FIX_22–23 적용 후 롯데의 세 번째 시도를 조사했다. 구현/설정/DB/체크포인트/serving을 수정하거나 배치를 재시작하지 않았다. 읽기 조사 외에 실패 문서 첫 페이지의 실제 primary OCR 진단 호출을 1회 수행했다. 기존 handoff는 보존한다.

## 종료와 처리 결과

- run `49845932e05849d3a0110a2bc82d6c1c`, container `cardrag-live-lotte-resume-20261021T053259Z`.
- 활성 release `/opt/cardrag/019-fix23-e9b9cdca1a22`. 14:32:59 KST 시작, 18:01:10 종료, exit1/OOMKilled=false. Worker 로그 elapsed_seconds=12444. 약 3시간28분 시도다.
- PDF530/530, cache_hits529, downloads0, revalidations1/not_modified1. collection barrier 통과. 이전 local OCR647 재사용 후보를 찾았으며 이전346건 지점의 cache identity 오류는 재발하지 않았다.
- 영속 OCR/structure/views 각각 **517건 성공**, 전체707 중 **190건 미완료**다. 이전330개 OCR body fingerprint는 변경/누락0. 현재517개 본문은 해당 manifest 또는 검증된 이전 generation의 source/PDF/page/body SHA/size와 대조했다.
- chunk checkpoint **174개**, 파일 hash 불일치0. 완료 건수는 새 provider 호출 수와 다르다. baseline 대비 신규 document ID60개 중47개 완료,13개 미완료이며, 미완료190개 전체를 신규 OCR 호출190개로 해석하지 않는다.
- 새 게시 없음. MCP live는 기존 현대 generation `g-10f236331a2744f78980f2b8-9d16cfdbae66`, 전체5545건 유지. 롯데 신규 결과가 라이브에 게시됐다고 판단하지 않는다.

## 이번 직접 실패

현재 run의 `reports/ocr-systemic-failure.json`과 DB 실패가 일치한다.

```text
occurred_at=2026-10-21T09:00:23.678366+00:00
document_id=doc_aea398c8f4b36346652bd4765f4f9eeaa4e223d9ae8d2d7a66a0ec1da4f69d5f
product_code=1765
pdf_sha256=f358967fe2f016240623c61e25c020a1d9704962ed5debf344ae56fae01a8761
reason_code=provider_process_exit_unknown
error_kind=process_exit exit_code=1 retryable=false attempt=1
stderr_size_bytes=2512
stderr_sha256=ff760ea407b2e452166a65e0a64059c2f4efe19a0abe7e223571aad6570b9aa9
```

해당 문서는 primary/rendered 5페이지만 있고 fallback 결과/chunk/full OCR은 없다. Codex primary의 미분류 프로세스 종료로 판단한다. 이전 cache 재구성 오류나 Paddle 모델 로딩 실패 증거가 아니다. unknown은 nonretryable이므로 max_attempts4가 있어도 이번 문서는 attempt1에서 중단됐다.

CodexOCRProvider는 nonzero exit에서 stderr를 분류하고 길이/해시만 보존한다. 원시 stderr는 저장하지 않으며, 확인한 Codex 로그 DB에도 해당 원인이 남아 있지 않다. 따라서 **실제 upstream 오류·새 한도 메시지·일시적 장애 중 어느 것인지는 확정할 수 없다.** 현재 코드가 stdout 오류 채널을 분류하지 않는 점은 관찰 사항이며, 이번 오류가 그 채널에 있었다는 증거는 없다.

FIX_23은 codex-exec → local-paddleocr 조합의 typed `provider_process_rate_limited`만 systemic fallback으로 허용한다. unknown을 전파한 것은 명시된 정책과 일치한다. unknown을 모두 quota로 간주하거나 인증/설정/무결성 오류까지 fallback시키는 수정은 권장하지 않는다.

## 실제 fallback 및 진단 호출

다른 문서 `doc_5da34711132e1f245ff8985b403f78eec5a30cd374e0c9c4d0ecee043fc2352e`에는 fallback/native-manifest가 있다. provider=local-paddleocr, model=PaddleOCR-VL-1.6, **9페이지 전체 문서 완료**, 생성17:59:39 KST. 로컬 Paddle fallback은 등록만 된 상태가 아니라 이번 시도에 실제 성공했다.

15:28:02의475건부터17:59:44의500건까지 약2시간32분 간격이 있다. CPU Paddle 처리 부담이 의심되지만 generic fallback warning에는 시작시각이 없어 그 전체를 정확한 Paddle 소요시간으로 단정하지 않는다. 상속된 native manifest 수를 이번 신규 Paddle 호출 수로 세지 않는다.

실패 문서의 page-0001 이미지를 동일 primary 설정/기본 OCR prompt로 한 차례 호출했다. exit0, OCR2812문자 반환. stage/checkpoint는 갱신하지 않았다. 관찰 파일은 `/opt/cardrag/current/operations/live/lotte.latest-provider-probe.jsonl`이다. 해당 파일의 classification 필드는 성공 프로세스에도 분류기를 적용한 진단 도구의 기본값이며, sandbox_or_permission boolean도 정상 CLI header와 매치될 수 있다. **이번 성공 호출의 오류나 원래 실패 원인으로 해석하면 안 된다.** 원래 다중 페이지 요청과 동일한 재현이 아니므로 원인 해결 또는 전체 재개 완주를 증명하지 않는다.

## 판단 및 후속

데이터 보존 상태는 양호하며 같은 run을 resume하여 이어갈 수 있다. 다만 미분류 종료의 재발 가능성은 남는다. 최신 처리517건을 예전330건 snapshot으로 되돌리거나 새 run으로 교체할 필요는 없다.

필요 보완은 현재019 안에서 국소 진단 개선으로 처리할 크기다. 다음 `FIX_24.md`에 안전한 프로세스 종료 진단 보완을 발행한다. 근거 없는 unknown 자동 fallback/재시도 변경은 포함하지 않는다. 이번 턴에는 코드 변경·배포·resume을 하지 않았다. timer/service는 비활성 유지, 롯데/신한 및019 최종 인수는 미완료다. 코드 무변경 조사이므로 전체 테스트를 다시 실행하지 않았으며, 이전1808건 통과를 이번 운영 완주 증거로 사용하지 않는다.
