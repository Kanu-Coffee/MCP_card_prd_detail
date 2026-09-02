# CardRAG v1.0.12 SIGBUS incident

## 결과

v1.0.12 candidate Worker는 2026-09-02 18:53:38 KST에 `SIGBUS`로
종료되었습니다. Docker가 core dump 작성을 기다린 뒤 18:55:06 KST에 최종
`exited` 상태를 기록했습니다.

| 항목 | 확인값 |
|---|---|
| container | `cardrag-v112-candidate-worker-acceptance` |
| application version | `1.0.12` |
| source revision | `e40dc2577541438ab9a87db7b2d18801fae1b24f` |
| exit | `135` (`128 + SIGBUS`) |
| OOMKilled | `false` |
| restart count | `0` |
| 실제 fault | 2026-09-02 18:53:38 KST |
| Docker terminal timestamp | 2026-09-02 18:55:06 KST |

메모리 부족이나 embedding provider 오류가 원인이 아닙니다. 원본 v1.0.12
container, state volume과 host core dump는 재현 및 감사 증거로 변경하지 않고
보존합니다.

## 직접 원인

`WorkerState`는 `sqlite3.connect()`로 WAL database를 연 뒤 main DB, WAL과 SHM의
nofollow/identity 검증을 위해 같은 inode의 별도 file descriptor를 열었다가
닫았습니다. POSIX `fcntl()` locking에서는 한 process가 같은 inode를 가리키는
**어떤** descriptor라도 닫으면 그 process가 해당 inode에 가진 lock이 모두
해제될 수 있습니다. 따라서 검증 자체는 성공했지만 SQLite connection이 보유해야 할
DB/SHM lock은 조용히 사라졌습니다.

그 상태에서 외부 read-only progress monitor가 live state DB에 접속했습니다. Monitor는
자신을 첫 SHM client로 판단해 `worker-state.sqlite3-shm`을 32 KiB로
truncate/rebuild했습니다. Worker는 이전에 만든 두 번째 SHM mapping을 계속 보유하고
있었습니다. WAL frame 4063을 wal-index에 추가하면서 SQLite `walIndexAppend()`가
두 번째 32 KiB region 시작을 `memset()`했고, 축소된 파일 끝을 건드려
`BUS_ADRERR`/`SIGBUS`가 발생했습니다.

Core dump와 정확한 container binary를 결합한 결과는 다음과 같습니다.

- fault address는 SHM file offset `0x8000`, 즉 두 번째 32 KiB region의 첫 byte입니다.
- program counter는 libc `memset`, destination은 fault address, length는 32 KiB입니다.
- WAL은 4,629 frame을 포함했습니다. 첫 wal-index region에는 4,062 frame이 들어가므로
  다음 frame부터 두 번째 region이 필요합니다.
- 마지막 정상 WAL 진행 log와 DB/WAL mtime은 fault 직전까지 writer가 동작했음을
  보여 줍니다.
- Host와 container 모두 OOM 증거가 없고 Docker도 `OOMKilled=false`를 기록했습니다.

이 동작은 SQLite가 설명하는 POSIX close/locking 제약과
[WAL-index 형식](https://sqlite.org/walformat.html)의 32 KiB region 경계에
일치합니다.

## v1.0.13 수정

v1.0.13은 identity 검증용 descriptor의 lifetime을 SQLite connection과 결속합니다.

- main DB, WAL, SHM 검증 descriptor를 connection이 살아 있는 동안 닫지 않습니다.
- 정상 종료는 SQLite connection을 먼저 닫고 검증 descriptor를 그 다음 닫습니다.
- 초기화 도중 실패해도 connection-first 순서로 정리합니다.
- path swap, symlink, inode 변경 검증은 유지합니다.
- process-local path/inode registry가 두 번째 `WorkerState`를 DB open 전에 거부합니다.
- Linux [`unix-excl` VFS](https://sqlite.org/vfs.html)로 WAL을 열어 file-backed SHM을
  생성하거나 mmap하지 않으며,
  live state DB를 여는 두 번째 process는 `database is locked`로 거부합니다.
- WAL 4,063-frame 경계를 넘는 회귀 시험에서 SHM 파일/mapping이 없고 writer가 계속
  commit할 수 있는지 검증합니다.

문제를 유발한 cron direct-DB monitor는 disable했습니다. 운영 관측은 container/journal의
진행 log와 terminal result를 사용하며, live Worker SQLite 파일을 직접 여는 monitor는
다시 활성화하지 않습니다. 이는 code fix와 별개의 운영 방어선입니다. 외부 reader가
장시간 transaction을 유지하면 WAL checkpoint를 방해할 수도 있으므로 lock fix가
적용된 뒤에도 direct-DB monitoring은 금지합니다.

## 복구 경계

SIGBUS 이후의 SHM은 durable state가 아닙니다. v1.0.12 source를 직접 재시작하거나
수정하지 않고, stopped source를 새 v1.0.13 전용 volume에 offline copy합니다. Main DB와
WAL은 한 쌍으로 보존하고 destination에서만 stale SHM을 제외한 뒤 SQLite recovery,
checkpoint와 integrity 검사를 수행합니다. 그 다음 동일 run ID
`1f1763a9cd474a81952a6eb6ffb6e397`을 v1.0.13 exact image로 resume합니다. 상세 절차는
[v1.0.13 migration](V1_0_13_MIGRATION.md)에 있습니다.

v1.0.13도 data/publication channel `candidate-v1.0.11`을 유지하는 runtime reliability
patch입니다. Stable v1.0.9 image, volume, WebDAV stable pointer와 소비 경로는 candidate
acceptance 및 별도 cutover 승인 전까지 변경하지 않습니다.
