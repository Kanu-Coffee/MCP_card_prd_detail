# 2026-10-17 기본 홈페이지 직접 호출 및 Crawl4AI 점검

- 요청: 롯데 모바일 `m.lottecard.co.kr`, 신한 `www.shinhancard.com`의 기본 홈페이지도 실패하는지 일반 호출 및 Crawl4AI로 확인.
- 관측: 07:15–07:18 KST, 현재 운영 호스트. 제품 소스/운영 설정 변경 없음.
- 결론: **이 호스트에서는 기본 홈페이지도 정상 페이지를 확보하지 못했다. 공시실 URL이나 PDF parser에 한정된 실패가 아니다.** 카드사 전체 장애나 특정 WAF/IP 차단으로 단정할 근거는 없다.

| 대상 | 일반 HTTPS GET | 브라우저 UA·HTTP/1.1 GET | Crawl4AI 0.9.5 / Chromium |
|---|---|---|---|
| `https://m.lottecard.co.kr/` | 8.02초 connect timeout, HTTP 응답 없음 | 8.00초 connect timeout | 단독 순차 재검증 25.01초 navigation timeout, HTTP status 없음 |
| `https://www.lottecard.co.kr/` | 8.02초 connect timeout | 8.00초 connect timeout | 첫 병렬 시도 ERR_NETWORK_CHANGED; 이에 근거해 timeout을 확정하지 않고 모바일 단독 결과로 보완 |
| `https://www.shinhancard.com/` | TCP/TLS 연결 후 약 4.06초 connection reset, HTTP 응답 없음 | 동일 reset | 약 4.06초 `net::ERR_CONNECTION_RESET`, HTML 0 |

HTTP(80) 기본 주소로도 롯데는 connect timeout, 신한은 reset이었다. DNS는 각각 모바일 롯데 124.243.36.46, PC 롯데 124.243.36.21, 신한 210.112.177.1로 정상 해석됐다. 신한 curl 측정은 TCP 약 0.014초/TLS 약 0.029초로, 롯데의 TCP 연결 자체 timeout과 다르다.

배포된 Worker 이미지의 httpx로도 별도 임시 컨테이너에서 롯데 ConnectTimeout 약 8.09초, 신한 ReadError 약 4.05초를 재현했다. 이 테스트는 Docker 기본 bridge에서 실행했으며 생산 Compose network에서 직접 테스트했다고 확대하지 않는다.

## Crawl4AI 검증 방법 및 한계

프로젝트 의존성을 변경하지 않고 `uv run --no-project --with crawl4ai`의 임시 환경에서 실행했다. 버전 0.9.5, 기존 Chromium Chrome/153.0.8010.12를 임시 무인증 profile/CDP로 사용했다. HTTPS 인증서 검증 유지, cache bypass, domcontentloaded, navigation timeout 25초, 재시도 0회였다.

비교용 `https://example.com/`는 첫 브라우저 시도 및 새 브라우저 단독 검증에서 모두 HTTP **200**, HTML 1,989 chars, 약 0.81–0.95초로 성공했다. 첫 병렬 롯데 요청은 NETWORK_CHANGED를 기록해 순차 재시도로 확인했다. 순차 실행 마지막 비교 요청에는 앞선 실패 후 chrome-error navigation 간섭이 있었으므로 그 비교 결과는 제외하고 새 브라우저 단독 200으로 보완했다. Crawl4AI 종료 시 subprocess transport/임시 profile cleanup 경고가 발생했지만 navigation 결과는 이미 저장됐다. 태스크 임시 브라우저와 profile은 후속 정리했다.

웹 검색 도구의 원격 fetch는 두 주소에 HTML 타입만 반환하고 본문 0 lines였으므로 이를 다른 네트워크에서의 정상 접속 증거로 취급하지 않는다.

## 해석 및 다음 조사

롯데는 DNS 이후 TCP 연결 경계, 신한은 TCP/TLS 이후 HTTP 응답 이전 reset 경계를 먼저 조사해야 한다. 일반 클라이언트/브라우저에서 같은 범주의 실패가 재현돼 Crawl4AI 전환만으로 현재 문제가 해결되지는 않았다. 서버 outbound 경로·방화벽 및 목적지 접근 정책 가능성이 있으나 현재 테스트로 어느 쪽인지 확정할 수 없다. 다른 공인 IP/정상 사용자 회선에서 동일 주소를 비교하고 방화벽/라우팅 로그와 대조하는 것이 다음 진단 단계다. 이번 요청에서 비공식 proxy나 TLS 검증 해제는 사용하지 않았다.

원본 증거: `/opt/cardrag/016-evidence/operations/connectivity-20261017/` 아래 `curl-basic.json`, `curl-browser-headers.json`, `crawl4ai-results.json`, `crawl4ai-sequential-results.json`, `crawl4ai-fresh-control.json`, `worker-httpx-results.json`.
