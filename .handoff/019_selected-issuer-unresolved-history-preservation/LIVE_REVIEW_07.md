# 019 LIVE_REVIEW_07 — 롯데 Codex 한도 중단 및 재개 가능성 점검

2026-10-21 Codex. 사용자 요청은 충전 이후 안전 재개 가능 여부, Paddle fallback 등록, 처리/잔여 OCR 건수 점검이다. 현재 활성 FIX_21 source와 종료 컨테이너/동일 live state 볼륨을 읽었다. 운영 SQL/설정/원장/체크포인트를 수정하거나 카드사/OCR/provider 호출·실제 재개를 수행하지 않았다. 별도 환경 확인용 compose Python 컨테이너만 실행했으며 배치는 기동하지 않았다.

## 실제 중단

- container `cardrag-live-lotte-20261021T005408Z`, run `49845932e05849d3a0110a2bc82d6c1c`, exit1/failed. OOMKilled=false.
- container 시작09:54:09 KST, 종료10:59:09 KST. run DB 실패시각10:58:05 KST.
- failed OCR document `doc_72c4fdc631147f0f70e1c7ec34fa9fdcc94d56ea413add05ce9849259328de2b`, product1576.
- report `runs/RUN_ID/reports/ocr-systemic-failure.json`: provider_process_rate_limited / error_kind=rate_limit / retryable=true, stage attempt_count=4/max_attempts=4. 충전 여부를 확인하려고 유료 OCR 호출을 추가하지 않았다. 사용자 충전 완료 사실을 전제로 재개 경로를 검토했다.
- 선택 scope는 lotte 하나. collection complete, 최신 목록530건 중 download succeeded529/skipped1; PDF 신규 다운로드60, cache hit469. 목록의 기존404 한 건은 별도 unavailable 처리이며 이번 중단 원인은 provider 한도다.

## 처리/잔여 수치와 무결성

performance.json과 state stage/checkpoint, acquisition.v1.json을 대조했다.

| 항목 | 건수 |
|---|---:|
| 롯데 OCR 전체 작업 대상(개정 포함) | 707 |
| OCR·structure·views 각각 succeeded | 330 |
| 미완료(실패1 포함) | 377 |
| 완료 중 이번 run Codex chunk checkpoint가 있는 문서 | 26 |
| 완료 중 이번 run provider chunk 없는 재사용 문서 | 304 |

체크포인트는97개/27문서로, 완료26문서95개 + 중단1문서2개(chunk index0,1)다. **97개 모두 DB의 output_sha256과 실제 파일 바이트가 일치**한다. 완료330건의 ocr/ocr.md가 모두 존재하고, primary native manifest312건 또는 직전 sealed generation의 문서 OCR18건에 대해 SHA/size가 모두 일치한다. 312개 manifest가 있다고312건을 신규 provider 처리로 계산하지 않았다(기존 결과의 manifest materialization 포함).

acquisition checkpoint payload hash도 정상이다. 전체 acquired5605=비선택4898+롯데707. 현재 live baseline의 롯데647개 document ID와 비교한 **신규 ID60개** 중 완료29/잔여31, 기존ID는 완료301/잔여346이다. 신규 ID 일부도 content/cache를 재사용할 수 있으므로 신규ID29와 Codex 신규호출 완료26은 서로 다른 지표다. 잔여377 전체가 새 Codex OCR 과금 문서라는 의미가 아니다. 향후 최신 목록 재확인으로 실제 잔여 수치가 바뀔 수 있다.

## 안전 재개 경로

기존 run을 `worker run --resume 49845932e05849d3a0110a2bc82d6c1c`로 명시 재개하는 것이 적합하다. 현재 설정은 중단 컨테이너와 동일한 codex-exec/gpt-6.1-sol, fallback local-paddleocr/PaddleOCR-VL-1.6, cache read-write다. 모델/contract를 임의 변경하거나 새 run으로 시작하지 않는다.

state.assert_resumable은 failed stage의 유한 시도 epoch만 새로 열고 성공 stage·다운로드·chunk checkpoint는 보존한다. explicit resume은 롯데 최신 discovery를 다시 확인한다(현재 코드의 latest-only 정책). OCRResolver는 입력/출력 hash가 같은 checkpoint를 재사용하므로 저장된 완료 결과와 실패 문서의 유효 chunk도 재활용할 수 있다. 입력/contract/hash가 달라진 결과는 재사용을 강제하지 않고 재검증/재처리한다. 따라서 "330건의 provider 호출이 무조건0" 같은 절대 보장은 하지 않는다.

기존 host run-issuer.sh는 신규 run용이며 --resume 지원이 없다. 향후 사용자가 재개를 요청하면 같은 run ID의 resume 명령을 사용하고, 별도 resume 컨테이너를 만드는 경우 live/lotte.json의 container 추적과 실패 캐시 결과를 함께 정리하여 status/check helper가 이전 exit1 컨테이너를 보지 않도록 해야 한다. 이 점검에서는 해당 metadata도 변경하지 않았다.

## Paddle fallback 실제 상태와 한계

종료 컨테이너 환경과 현재 compose에서 모두 다음을 확인했다:

- primary codex-exec / gpt-6.1-sol.
- fallback local-paddleocr / PaddleOCR-VL-1.6.
- `/var/lib/cardrag-paddle`에 `cardrag-worker-paddleocr-models` 볼륨 RW mount, 설치된 paddleocr Python 모듈, 모델 cache 디렉터리 존재.

CLI는 FailoverOCRResolver(primary,fallback)를 구성한다. 다만 FailoverOCRResolver는 ProviderDocumentError/OCRValidationError/timeout 및 일부 HTTP 오류를 fallback 대상으로 삼고 **ProviderSystemicError는 통과시켜 배치를 중단**한다. Codex process 한도 오류가 ProviderSystemicError(provider_process_rate_limited)이므로 이번에는 Paddle로 전환되지 않았다. 설정 누락이 아니라 현재 오류 분류/전환 정책이며 기존 테스트가 이 systemic 차단을 명시한다. Paddle 실제 추론은 이번에 실행하지 않았으므로 모델 로딩/추론 완주까지 검증했다고 주장하지 않는다. 한도 오류에도 자동 전환하려면 해당 reason만 제한적으로 허용하는 별도 정책 보완과 검토가 필요하다(인증/무결성 등 systemic 전체를 삼키는 변경은 부적합).

## 검사와 현재 운영 상태

```text
uv run --no-sync pytest apps/cardrag-worker/tests/test_ocr.py -k 'chunk_checkpoint_resumes_after_provider_failure or twenty_four_page_logo_only_chunk_reuses_first_ten_checkpoints_on_resume or failover_does_not_swallow_provider_systemic_failure' -q
3 passed,119 deselected
uv run --no-sync pytest apps/cardrag-worker/tests/test_state.py::test_state_uses_wal_and_resume_resets_attempts_and_refreshes_discovery -q
1 passed
```

Live stable은 여전히 현대 generation `g-10f236331a2744f78980f2b8-9d16cfdbae66`다. 실패한 롯데 중간 결과를 live에 게시하지 않았다. timer/service inactive, 롯데 배치 exited1 상태다. 안전 재개가 가능하지만 이번 요청은 점검이므로 실제 재개는 하지 않았다. 019와 신한/롯데 최종 인수는 미완료다.
