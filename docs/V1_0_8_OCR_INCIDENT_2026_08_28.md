# v1.0.8 OCR Worker 종료 조사 기록 (2026-08-28)

이 문서는 2026-08-28 22:48 KST에 종료된 v1.0.8 Worker의 읽기 전용 조사 결과와
v1.0.9 후보의 재발 방지 합격 기준을 기록합니다. 조사 중 운영 Worker를 재시작하지
않았고, `/opt/cardrag/current`의 target `/opt/cardrag/v1.0.8`, 운영 상태 volume,
WebDAV 객체 및 `stable.json`을 변경하지 않았습니다.

## 판정

Worker는 OOM이나 외부 signal로 사라진 것이 아니라 OCR native cache의
`READY.json` 게시 경계에서 발생한 예외를 v1.0.8의 fail-closed 분기가 처리하면서
종료 코드 1로 끝났습니다. 다만 v1.0.8이 원래 예외를 일반화한 뒤 exception chain을
제거했으므로, `READY.json` 게시 내부의 temp PUT/readback/MOVE/final readback/cleanup
중 어느 세부 동작에서 어떤 HTTP 또는 socket 상태가 발생했는지는 복구할 수 없습니다.

따라서 확인된 직접 원인은 **READY publication 경계의 비문서 범위 오류와 그에 따른
배치 종료**이며, 그 아래의 네트워크·HTTP 예외 종류를 추정해서 확정하지 않습니다.
원예외를 지운 관측성 결함 자체도 재발 방지 대상입니다.

## 근거

- systemd는 `Result=exit-code`, `ExecMainCode=1`, `ExecMainStatus=1`을 기록했습니다.
  서비스는 2026-08-27 09:17:13 KST에 시작해 2026-08-28 22:48:30 KST에 끝났으며,
  마지막 traceback은 22:48:29 KST에 출력됐습니다.
- systemd service/main Docker CLI scope는 memory peak 22.2 MiB, swap peak
  5.1 MiB를 보고했고, journal의 transient container scope는 memory peak 2.8 GiB,
  swap peak 203.9 MiB를 보고했습니다. 어느 scope에도 OOM/signal/kernel kill 종료
  근거가 없으며 main process는 status 1로 종료됐습니다. 다음 날 발생해 다른
  프로세스를 종료한 OOM 기록은 이 사건의 근거로 사용하지 않습니다.
- 실패 run은 `5b7d7f49724f4a02b5f8bd5714a7c8e2`입니다. 그 run은 discovery 3건,
  download 성공 2,523건·skip 6건, OCR 성공 1,265건까지 진행했습니다. OCR 실패
  2건 중 한 건은 재시도 4회를 소진한 문서 오류였고, 신한카드 상품코드 `00870`
  문서는 첫 시도에서 비문서 범위 오류로 분류돼 배치를 끝냈습니다.
- 해당 신한 문서는 22:47:16 KST까지 local OCR, checkpoint와 native manifest가
  완성됐습니다. 동일 OCR CAS는 22:47:20, native manifest는 22:47:22 KST에 원격의
  정확한 bytes로 확인됐지만 해당 reuse key의 native `READY.json`은 없었습니다.
  그러므로 provider 인식이나 local seal 생성 이전이 아니라 READY publication
  경계에서 실패한 것으로 범위를 좁힐 수 있습니다.
- v1.0.8은 이 분기에서 원예외 대신
  `OCRSystemicFailureError: OCR failed with a non-document-scoped error`를
  `from None`으로 발생시켰고 상태 DB에도
  `non_document_scoped_error: OCR failed outside a document boundary.`만 남겼습니다.
  따라서 기존 journal과 상태 DB만으로 원래 exception class, HTTP status, errno 또는
  안전한 response 분류를 되살릴 수 없습니다.
- `docker compose run --rm` 방식 때문에 종료한 임시 컨테이너도 남지 않았습니다.
  컨테이너가 없다는 사실을 crash 증거로 해석하지 않습니다.

## v1.0.9 후보 합격 기준

v1.0.9 구현과 시험은 다음 불변조건을 만족해야 합니다.

1. provider가 만든 OCR bytes와 native manifest를 먼저 원자적으로 local seal로
   보존하고 엄격하게 다시 검증합니다. 이후 remote cache control 게시가 일시적으로
   실패해도 검증된 OCR 결과 자체를 잃지 않습니다.
2. OCR cache commit의 일시적 WebDAV 실패만 제한된 횟수로 재시도합니다. 재시도
   소진 뒤에는 cache binding을 주장하지 않는 generation-only OCR로 현재 run을
   계속할 수 있어야 하며, 부분 native `READY.json`을 성공으로 간주하지 않습니다.
3. 같은 run의 재시도는 검증된 local seal로 cache 게시를 복구합니다. 다음 run은
   원격에 남은 manifest와 OCR CAS의 source/contract/canonical bytes/hash를 엄격히
   검증한 뒤 READY만 복구합니다. 어느 경로도 같은 OCR을 만들기 위해 provider를
   다시 호출하지 않습니다.
4. 인증·권한 실패, canonical bytes/해시·manifest·READY 무결성 불일치, 안전하지 않은
   local seal은 일시 장애로 낮춰 보지 않고 fail-closed로 중단합니다.
5. 전역 OCR 오류는 원문 응답, credential, URL query나 provider stderr를 기록하지
   않는 구조화된 안전 보고서를 남깁니다. 보고서에는 run/document 식별자, 실패
   phase, 제한된 reason code, 안전한 error kind/status, retry 가능 여부와 실제 시도
   횟수가 포함돼야 합니다. CLI와 상태 DB도 이 보고서를 가리키되 원예외를 다시
   일반 문구 하나로 지우지 않습니다.
6. INFO 단계 진행 로그와 최종 구조화 결과가 journal에 즉시 남고, 운영자는 run과
   문서 단위 진행이 실제로 증가하는지 확인할 수 있어야 합니다.

확정된 보고서 schema/path, 3회 bounded retry와 partial cache 복구 계약은
[v1.0.9 후보 검증·전환 절차](V1_0_9_MIGRATION.md)에 기록했습니다. 이 기준은 후보
환경에만 적용하며 조사 완료를 이유로 v1.0.8 운영 자산을 수정하거나 삭제하지
않습니다.

## 읽기 전용 운영 확인

장애 조사 시작 시 먼저 다음 systemd 증거를 보존합니다.

```bash
systemctl show cardrag-worker.service \
  -p ActiveState -p SubState -p Result \
  -p ExecMainCode -p ExecMainStatus \
  -p ExecMainStartTimestamp -p ExecMainExitTimestamp \
  -p MemoryPeak -p MemorySwapPeak

journalctl -u cardrag-worker.service \
  --since '2026-08-28 22:40:00' --until '2026-08-28 22:50:00' \
  --no-pager -o short-iso-precise
```

그 다음 상태 volume과 WebDAV는 snapshot 또는 read-only mount에서만 확인합니다.
상태 DB를 직접 열 때는 SQLite URI의 `mode=ro`를 사용하고, 점검 전에 service 재시작,
`resume`, cache 재게시, pointer 수정이나 volume 정리를 실행하지 않습니다. 인증 파일,
provider 출력과 URL query는 사건 보고서나 terminal에 출력하지 않습니다.
