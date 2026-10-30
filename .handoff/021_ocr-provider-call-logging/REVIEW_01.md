# 021 REVIEW_01 — FIX_01·FIX_02 및 코드 구현 인수

2026-10-28 KST Reviewer Codex. PLAN, REPORT, FIX_01/02와 각 REPORT, 현재 Git status/diff, 실제 구현 및 관련 테스트를 대조했다.

## 판정

**FIX_01과 FIX_02의 P2는 해결됐다. 현재 021 코드 구현을 인수하며 추가 correction은 없다.** 이는 로컬 소스 코드 인수다. 운영 배포나 실제 provider 호출에서의 신규 로그 관측, 계획의 운영 종료를 판정한 것은 아니다.

## 수정 확인

- whole-document와 chunk 결합 후 최종 credential/byte 검증 실패가 `ocr_output_validation_failed`로 기록된다. `OCRVerificationError`는 고정 `final_ocr_verification_failed`로 분류되고 원래 예외가 전파된다. provider 반환 집계와 validation 실패를 구별하며 storage/publication 경계를 변경하지 않는다.
- `final_chunk_combine`은 `validation_scope=document`, 전체 페이지 범위, `call_id=null`, `chunk_index=null`로 기록한다. 직전 호출은 `last_call_id` 참고 필드로만 남긴다. 첫 청크만 잘못되고 마지막 청크는 정상인 회귀 fixture에서 정상 청크에 오류를 귀속하지 않는 것을 확인했다.
- checkpoint-only 최종 검증 실패는 새 provider 호출/순번을 만들지 않으며 원인 call_id를 부여하지 않는다. 단일 응답 및 whole-document 단일 호출의 기존 연결은 유지된다.
- 기존 문맥 격리·실행별 순번·fallback/corrective·OpenRouter 내부 요청·안전한 오류 분류와 020 Paddle 취소/reap/alias 테스트도 전체 회귀에서 통과했다. 오류 원문·credential을 보관하지 않는 경계와 기존 retry/fallback 정책을 대조했다.

## Reviewer 재검증

```text
uv run --no-sync pytest apps/cardrag-worker/tests/ packages/cardrag-core/tests/ tests/runtime_v1/ -q
1990 passed, 11 warnings in 65.58s

uv run --no-sync ruff check apps/cardrag-worker/ packages/cardrag-core/
All checks passed!

uv run --no-sync ruff format --check apps/cardrag-worker/src/cardrag_worker/ocr_logging.py apps/cardrag-worker/src/cardrag_worker/ocr.py apps/cardrag-worker/src/cardrag_worker/pipeline.py apps/cardrag-worker/src/cardrag_worker/providers.py apps/cardrag-worker/tests/test_ocr_provider_logging.py
5 files already formatted

uv run --no-sync mypy apps/cardrag-worker/src/cardrag_worker/ocr_logging.py apps/cardrag-worker/src/cardrag_worker/ocr.py apps/cardrag-worker/src/cardrag_worker/pipeline.py apps/cardrag-worker/src/cardrag_worker/providers.py
Success: no issues found in 4 source files

git diff --check
통과
```

11개 warning은 손상된 prior OCR 거부, 원격 캐시 충돌 및 예상 fallback fixture의 기존 경고다. gitleaks 정책 테스트도 이번 전체 실행에 포함돼 통과했다. 실제 외부 API나 Paddle 모델 추론·systemd 배포를 이번 검사로 검증했다고 주장하지 않는다.

검토한 파일 SHA-256:

| 파일 | SHA-256 |
|---|---|
| `ocr_logging.py` | `008dcedd56e5ae9aae62f3d62480e139832016b1264049793da7a951f01b0d3f` |
| `ocr.py` | `99de7f101091ef1f13be452a7c03be508fa7ae73f31fa008fd19359b052998cd` |
| `pipeline.py` | `bf20e3094b6e7b25fa709e3c699955e5dc81cf6bb4c11167a3ef8c6c2a3f6c25` |
| `providers.py` | `d79353ef4fdaf61ad91408c54bcc247eefdae43f52d35fa0bb4d74bc4d3c52d8` |
| `test_ocr_provider_logging.py` | `f605950453fad1653296574672ebcc32b1194f9ae294b5aea83098c8a419cd11` |

## 인계 및 한계

- HEAD는 `06af791b6db3fa250b8b543deed46f77f3a4a7d7`이며 020/021 및 기존 사용자 문서/timer 변경은 미커밋 상태다. Reviewer는 production 코드·운영 설정·데이터를 수정하지 않고 본 문서만 추가했다.
- provider attempt와 고유 호출 문서 수는 별개이며, 과금 수·SDK의 숨은 retry·OpenRouter 실제 upstream/model을 증명하지 않는다. unknown/null 및 `model_source=artifact_contract`의 의미를 유지한다.
- 운영에서 새 이벤트를 보려면 새 로깅 모듈을 포함한 검증된 이미지 배포가 필요하다. 현재 운영 020 이미지에 이 소스 변경이 적용됐다고 주장하지 않는다. 배포 시 실제 이미지/소스 차이와 서비스 계정·로그 출력·예약 상태를 별도 확인해야 한다.
