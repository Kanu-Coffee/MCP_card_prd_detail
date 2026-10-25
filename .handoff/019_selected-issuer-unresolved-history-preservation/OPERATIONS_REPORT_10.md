# 019 OPERATIONS_REPORT_10 — FIX_24 배포 및 롯데 동일 run 재개

2026-10-21 Executor. REVIEW_12 승인 후 FIX_24를 새 release/image로 반영하고 기존 롯데 run을 resume했다. 완주를 기다리지 않고 마지막 확인 시점의 상태만 기록한다. timer/service는 inactive다.

## 배포

- 활성 release: `/opt/cardrag/019-fix24-11d1321cc9e6`
- 이전 release: `/opt/cardrag/019-fix23-e9b9cdca1a22`
- image: `cardrag-worker:019-fix24-11d1321cc9e6`
- image ID: `sha256:d08b61a6f3cd3809be3ed46aaa560f48bf632f4d987685b2640bfeb1ea339597`
- base image ID: `sha256:15124ebcc9147de54db5ac238bb488f9a31eead2e5cd1b0a89c334f60550972e`
- source tree SHA-256: `11d1321cc9e6156af433e6f1aea0708e2123941d38c892a962444323e5a39141`
- build input SHA-256: `cc893ca905e63a925fe5f6a7385c730bfee85f4c7ea4eb3ca2acfb70b63a354a`
- 설치 source 81개를 snapshot과 대조했고 UID10001 CLI help, startup-check, primary/fallback OCR contract before/after 일치를 확인했다.
- 새 복구점: `cardrag-worker-state:/deployment-backups/019-fix24-11d1321cc9e6`
  - worker-state: 7,315,312,640 bytes, SHA-256 `342962de6185990b3fbaec88edaece2e608f85fda18e4998a369414caa0e1b7a`
  - backup-ledger: 24,416,256 bytes, SHA-256 `394fdb1937cad67d69190d03f1f21585be226e3e6f47db1eb4ab771d52c80a2b`
  - quick_check=ok, latest run=failed, stable generation 유지.
- 이전 fix23 snapshot은 resume 완주/실패 결과 확인 전까지 임시 보존했다. 단일 복구본 정리와 snapshot-retention.json 갱신은 후속 운영 단계다.
- 전환 전 timer/service inactive, Worker container 0, WAL 0을 확인했다. 전환 중 worker.lock/backup.lock을 유지했다.

## Resume

- run 유지: `49845932e05849d3a0110a2bc82d6c1c`
- container: `cardrag-live-lotte-resume-20261021T104220Z`
- command: `worker-compose.sh run -d --no-deps --name cardrag-live-lotte-resume-20261021T104220Z worker run --resume 49845932e05849d3a0110a2bc82d6c1c --issuer lotte`
- `--phase process`/`--skip-stage pdf`는 사용하지 않았다. 현재 failed run에는 `execution-plan.json`/`acquisition.json`/seal이 없어 valid collection source가 아니며, 억지로 만들면 수작업 상태 생성이 된다.
- 대신 정상 resume의 PDF cache 경로를 사용했다. PDF 530/530, cache_hits 529, downloads 0, cache_misses 0, revalidations 0으로 origin PDF 다운로드 없이 통과했고 collection barrier 후 OCR 단계로 진행했다.
- 이전 완료 517건/174 checkpoint는 보존됐고 647개 local OCR artifact를 재사용했다.
- 마지막 확인 시 OCR 600/707 succeeded=600, failed=0, structure_failed=0, cache_publication_deferred=0. 이전 실패 문서 구간을 지났고 container는 running이다.
- 새 게시/백업/scoped flush/최종 인수는 아직 완료로 판정하지 않는다.

## 모니터링

```bash
docker logs -f cardrag-live-lotte-resume-20261021T104220Z
/opt/cardrag/current/operations/logs-issuer.sh lotte
docker inspect cardrag-live-lotte-resume-20261021T104220Z \
  --format 'status={{.State.Status}} exit={{.State.ExitCode}} finished={{.State.FinishedAt}} oom={{.State.OOMKilled}}'
```

종료 후:

```bash
/opt/cardrag/current/operations/status-issuer.sh lotte
/opt/cardrag/current/operations/check-issuer.sh lotte --flush
```

실패 시 `runs/49845932e05849d3a0110a2bc82d6c1c/reports/ocr-systemic-failure.json`에 FIX_24의 provider/phase/stdout·stderr hash/분류 근거가 남는다. unknown은 여전히 terminal이므로 자동 재시도하지 않는다.