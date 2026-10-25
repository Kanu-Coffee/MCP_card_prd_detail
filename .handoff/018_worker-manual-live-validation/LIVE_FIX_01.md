# 운영 실행 도구 경로 정정

사용자의 활성화 실행은 성공했으나 `/opt/cardrag/current/operations/run-issuer.sh woori`가 활성화 선행 안내를 출력하고 종료했다. 원인은 운영 도구가 논리 경로 `pwd`로 current 심볼릭 링크를 유지하면서 실제 release 경로와 비교한 것이다. 이미지 태그 계산에도 동일한 문제가 있었다.

호스트 release `/opt/cardrag/017-fca5ef313a0b/operations/`의 activate, worker-compose, run-issuer, status-issuer, logs-issuer, summary-issuers 도구에서 경로 계산을 `pwd -P`로 정정했다. 제품 코드와 이미지는 변경하지 않았다.

검증: 전체 도구 shellcheck 통과, current 경유 compose config --quiet 통과, current 경유 activate 실행에서 이미 활성화된 실제 release 확인. run/status의 실제 경로 계산 부분을 실행해 release와 이미지 태그가 각각 `/opt/cardrag/017-fca5ef313a0b`, `cardrag-worker:017-fca5ef313a0b`임을 확인했다. Worker 실행부는 실행하지 않았다.

확인 시점 timer와 service는 모두 inactive이며 실행 중인 Worker 컨테이너가 없다. 사용자의 첫 우리카드 수동 실행은 아직 시작되지 않았다. 같은 run-issuer 명령으로 재시도하면 된다. 라이브 수집/OCR/백업 성공 여부는 별도 검증 대상이다.
