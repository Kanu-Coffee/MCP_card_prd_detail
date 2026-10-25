# 019 OPERATIONS_REPORT_07 — 롯데 실패 run 명시 재개 기동

2026-10-21 Codex. 사용자 "재기동하고 알려줘" 승인에 따라 LIVE_REVIEW_07에서 점검한 실패 run을 동일 image/설정으로 재개했다. 새 run 생성이나 전체 카드사 실행이 아니다. 기존 handoff·실패 컨테이너·시도별 metadata/result를 보존했다. 코드 배포/모델/프록시/fallback 정책 변경은 없다.

## 재개 기동

- 기존/재개 run `49845932e05849d3a0110a2bc82d6c1c`.
- 이전 container `cardrag-live-lotte-20261021T005408Z` exited1 보존.
- 새 container **cardrag-live-lotte-resume-20261021T021643Z**, 2026-10-21 11:16:43 KST 기동.
- 명령: `/opt/cardrag/current/operations/worker-compose.sh run -d --no-deps --name cardrag-live-lotte-resume-20261021T021643Z worker run --resume 49845932e05849d3a0110a2bc82d6c1c --issuer lotte`.
- 동일 image ID `sha256:f1df6c1c9e8a95634c54bb40b66f138a55445333a265ed6c2b4385226e3bf7e5`로 실행. codex-exec/gpt-6.1-sol, local-paddleocr/PaddleOCR-VL-1.6, cache read-write가 이전 container와 현재 compose에서 일치함을 확인했다.

manual-start.lock을 보유하여 timer/service inactive·활성 Worker 없음·기존 container exit1·scope lotte를 확인했다. state를 읽기 전용으로 점검하여 완료 OCR330건과 checkpoint97개 SHA를 검증하고 완료 OCR body fingerprint도 보관했다. 가용75.19GiB ≥ 최근 gate+4GiB buffer72.92GiB로 시작 준비 검사를 통과했다. 소스·DB·checkpoint를 수작업 변경하지 않았다. 실제 Worker의 resume 경로가 attempt epoch/최신 목록/재사용을 처리한다.

## 관찰 및 검증 한계

새 container running, 실제 command의 --resume/--issuer를 확인했다. 11:17:21 KST `Worker startup completed elapsed_seconds=36.743`, pipeline entry 통과. 이어 lotte discovery와 PDF 단계가 진행되고 11:17:46 시점 400/530, cache_hits400, downloads0이었다. 따라서 새 container 생성만 확인한 것이 아니라 실제 pipeline 진행을 확인했다.

Embedding preflight는 samples24 중 cosine warning1(min0.997209, quality_status=warning)을 기록했지만 gate는 통과했다. 이번 기록은 배치 완주나 새 Codex OCR 호출 성공의 선언이 아니다. 이후 OCR/provider/백업/게시 결과는 배치 종료 후 별도로 확인한다.

실행 중 state의 stock SQLite read-only 연결은 Worker의 DB 잠금 때문에 `database is locked`로 거부됐다. 이를 우회하거나 lock을 풀지 않았고, 기동 후 진행 확인은 실제 container command·startup·pipeline 로그를 사용했다. 재개 전330/97 검증은 Worker 시작 전에 잠금 충돌 없이 완료했다.

## host helper 추적

operations/live/lotte.json은 새 resume container를 가리키도록 원자 전환했다. 같은 run의 원래 started_at 및 원래 baseline_file을 유지하여 run 조회·비선택 문서 비교가 가능하게 했으며 resume_run_id/resumed_at/previous_container를 추가했다. 이전 시도별 `cardrag-live-lotte-20261021T005408Z.json`/`.result.json`은 보존하고, 이전 실패를 캐시한 `lotte.result.json`만 제거했다. 새 시도별 metadata와 `.preflight.json`을 live 디렉터리에 기록했다.

로그 추적은 기존 helper 그대로 가능하다:

```bash
/opt/cardrag/current/operations/logs-issuer.sh lotte
```

종료 후 `check-issuer.sh lotte --flush`로 새 container exit·동일 run 성공·비선택 변경0·scoped backup 완료를 확인한다. timer/service는 계속 inactive, 신한 배치는 기동하지 않았다. 019/롯데/전체8사 인수는 아직 완료 처리하지 않는다.
