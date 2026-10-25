# 019 OPERATIONS_REPORT_09 — FIX_22–23 이미지로 롯데 동일 run 재개

2026-10-21 Codex. 사용자 "재기동" 승인에 따라 기존 롯데 run을 명시 resume했다. 신규 run/전체 카드사 실행이 아니다. timer와 신한 배치는 시작하지 않았다.

- run `49845932e05849d3a0110a2bc82d6c1c` 유지.
- 새 container `cardrag-live-lotte-resume-20261021T053259Z`, 14:32:59 KST 시작.
- 활성 release `/opt/cardrag/019-fix23-e9b9cdca1a22`, image ID `sha256:15124ebcc9147de54db5ac238bb488f9a31eead2e5cd1b0a89c334f60550972e`.
- 명령: `worker-compose.sh run -d --no-deps --name cardrag-live-lotte-resume-20261021T053259Z worker run --resume 49845932e05849d3a0110a2bc82d6c1c --issuer lotte`.

manual-start.lock 아래 timer/service inactive·활성 Worker 없음·이전 시도 exit1을 확인했다. 의도된 이미지 업그레이드이므로 새 image는 REVIEW_11/FIX_22–23의 deployment image ID와 일치해야 하며, 이전 container는 해당 base image로 한정했다. provider/model/fallback/cache 설정 일치와 배포 전후 운영 OCR contract 일치 증거도 검사했다.

실행 전 read-only/offline immutable audit로 failed run, scope lotte, 완료330개 OCR body fingerprint 및97개 chunk SHA를 다시 검증하고 FIX_23 승인 상태와 대조했다. 가용88.63GiB ≥ gate+4GiB buffer72.92GiB였다. SQL/receipt/seal/checkpoint를 수작업 수정하지 않았다. 실제 resume이 새로운 finite attempt epoch 및 최신 discovery를 처리한다.

이전 resume container의 종료 결과를 status helper로 먼저 저장하여 시도별 .result.json을 보존했다. operations/live/lotte.json은 새 container로 원자 전환했고 원래 started_at/baseline_file을 유지했으며, 이전 실패를 가리키던 lotte.result.json 캐시만 제거했다. 새 metadata 및 preflight 증거는 새 container명 .json/.preflight.json에 저장했다. 이전 시도별 파일은 덮어쓰지 않았다.

기동 후 실제 로그:

```text
14:33:22 Worker startup completed elapsed_seconds=22.035
14:33:33 discovery completed issuer=lotte records=530 warnings=1
14:33:48 PDF progress completed=425 total=530 downloads=0
```

container running, 실제 command/image도 일치한다. 단계 진행을 확인했으나 아직 OCR 단계의 이전 실패 지점 통과·실제 Paddle 추론·배치 완주/백업/게시 완료를 선언하지 않는다. FIX_23의 좁은 Codex quota→로컬 Paddle 정책은 새 이미지에 적용돼 있다.

로그: `/opt/cardrag/current/operations/logs-issuer.sh lotte`. 종료 후 `check-issuer.sh lotte --flush`에서 run/컨테이너 성공·비선택 변경0·scoped backup 완료를 확인한다. 019/신한/전체8사 인수는 미완료다.
