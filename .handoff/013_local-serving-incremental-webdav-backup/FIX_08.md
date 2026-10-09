# 013 FIX_08 — native 5건 provenance 반환 보완

작성: 2026-10-09, Reviewer / Codex. 기준 commit `c34eef0`.

## 판정

FIX_07의 주요 수정은 통과했다. 남은 인수 보완은 **알려진 native manifest 5건의 provider/model 반환**이다. 신규 기능이나 전체 Worker 검증을 늘리지 않는다. 이 항목 및 정확한 검증 보고를 완료하면 최종 구현 인수할 수 있다.

독립 검증:
- 지정5파일 pytest: **52 passed, 1 warning / 3.19s**. 이전 백업 실패 테스트와 commit failure 후 verified receipt 재사용 테스트가 통과했다.
- 기존 reviewer-fix06-repro.py: provider0, 기존variant 일치, Paddle provider/model 일치, 반복variant 안정, epoch1 miss 확인.
- 수정4파일 mypy: Success.
- 실제 영향328건(구 seed에 없는 문서만 선별): **328 hits / provider calls0 / 동일OCR328 / 원variant328**. 323content와5native를 실제 OCRResolver로 모두 실행했다. 그러나5native의 반환 provider/model은 generation-only/unrecorded였다.

근거: `evidence/reviewer-fix07-actual-inventory.py`, `evidence/reviewer-fix07-actual-inventory.json`. 실제 운영 volume은 `/state:ro`, repo는 read-only, Docker network none, WorkerState와 출력은 임시 디렉터리였다. 호스트 소스 접근 권한 때문에 검증용 컨테이너 UID0을 사용했지만 운영 volume은 읽기 전용이다. 실제 MCP/Worker 설정 및 운영 자료는 변경하지 않았다.

## 수정할 코드

`apps/cardrag-worker/src/cardrag_worker/ocr.py::_lookup_prior_local_sealed_ocr`

현재 prior.provider가 없고 native-manifest.json이 있으면 파일을 읽지만 `p_man.get("provenance", {})`를 조회한다. 실제 OCRArtifactManifest에는 provider/model이 **contract** 아래에 있다. 따라서 정상 운영에서 pipeline이 provider/model=None으로 전달하는5native의 알려진 OpenCode provenance가 사라진다.

요구사항:
1. native manifest의 올바른 contract 필드로부터 provider/model을 가져온다. 가능한 기존 OCRArtifactManifest 검증/읽기 경로를 활용한다. 기존 source PDF와 OCR identity 확인을 유지한다.
2. prior에 명시된 provider/model은 보존한다. source에 정보가 없는 content 자료는 generation-only/unrecorded를 유지할 수 있다. 알려진 정보를 새 모델 이름으로 재표기하지 않는다.
3. 기존 provider0/cache hit/원OCR/variant 보존 결과를 유지한다.

## 제출 검증의 표본 오류

Executor의 `preflight_volume_328.py`는 native 파일이 없는 전체 문서 중 **앞쪽323건**을 선택했다. 원래 영향대상은 seed에 없고 native 파일도 없는323건이다. 같은 개수라고 같은 표본은 아니다. 또한 native5건은 원파일 metadata를 읽기만 했고 OCRResolver 반환값을 확인하지 않아 위 결함을 놓쳤다.

이 사실 때문에 대규모 재검증을 새로 요구하지 않는다. Reviewer가 올바른328건의 resolver를 실행했고 실제 재OCR 방지는 확인했다. Executor는 새 검증 script/evidence를 추가하거나 Reviewer의 새 script를 활용하여 native5건 반환값까지 검증하면 된다. **기존 handoff 문서/증거를 덮어쓰지 않고** 새 evidence 이름과 FIX_08_REPORT에 수정·검증 범위를 정확히 적는다.

## 완료 기준과 명령

- 실제323content: provider0, 같은 OCR SHA/variant 모두 유지.
- 실제5native: provider0, 같은 OCR SHA/variant, 반환 provider=opencode / model=alibaba-token-plan/qwen3.8-flash를 엄격 assert한다. pipeline과 같이 prior.provider/model을 미리 채우지 않은 입력에서 검증한다.
- 기존52개 관련 테스트와4파일 mypy가 통과한다. 작은 회귀 테스트를 추가해도 되지만 유료 OCR/embedding/full Worker 반복은 필요 없다.

실물 검증은 다음 방식으로 재현 가능하다(원래script는 이전 리뷰 결과로 보존하고 강화된 검증이 필요하면 새script를 작성):

```sh
docker run --rm --network none --user 0:0   -v cardrag-worker-state:/state:ro   -v /home/lee/projects/MCP_card_prd_detail:/workspace:ro   -v /home/lee/projects/MCP_card_prd_detail/.handoff/013_local-serving-incremental-webdav-backup/evidence/reviewer-fix07-actual-inventory.py:/review.py:ro   --entrypoint python cardrag-worker:013-3eaa35d /review.py
```

이 명령은 이전 runtime의 의존 라이브러리와 /workspace의 최신 소스를 사용한다. 네트워크는 차단되고 volume은 read-only다. 이 오프라인 확인에 새 공개 이미지 빌드는 필요하지 않다.

## 운영 상태와 검증 한계

운영 MCP는 healthy이며 Worker/backup은 정지 상태다. Reviewer는 재기동하지 않았다. 백업 예산 예약과 actual_* 미측정 표시는 관련 테스트를 통과했다. body_requests/control_requests는 helper 호출 단위이며 실제 HTTP 전체 횟수로 해석하지 않는다. 30초 item cap 및 짧은 예산에서도 최소10초 commit 허용은 운영상 제약으로 기록하되 이번 추가 개발 조건으로 확대하지 않는다.

5건의 metadata 보완 후 기존 승인에 따라 운영 반영/Worker 재기동으로 진행할 수 있다. 기동 시 startup과 최초 진행만 확인하고 장시간 모니터링하지 않는다. 사용자가 완료를 통보한다. 이번 코드 리뷰를 새 운영 배치 완료나 공개 release 완료로 보고하지 않는다.
