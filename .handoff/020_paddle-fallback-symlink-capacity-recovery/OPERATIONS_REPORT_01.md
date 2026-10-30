# 020 운영 준비 — 배포 후보와 sudo 실행 대기

2026-10-28 00시대 KST. 사용자는 운영 배포와 링크 정리를 요청했고, 다음 실운영 관측 대상이 **2026-10-28 10:00 KST**임을 확인했다. `sudo -n true`는 `interactive authentication is required`로 실패하여 현재 세션은 systemd timer 중지·활성 release 교체에 필요한 root 권한을 얻지 못했다. 아래 단일 적용 스크립트는 사용자 sudo 인증으로 실행할 수 있게 준비했다. 작성 시점에는 운영 전환과 링크 삭제가 완료되지 않았다.

## 후보와 검증

- 현재 운영 release: `/opt/cardrag/019-fix33-7d2e39edbf59`; image ID `sha256:71eb18e6538de2e0b00fd3c5173045263980322c623b17e3debee7d782d51450`.
- 후보 release: `/opt/cardrag/020-paddle-20261028`.
- 후보 image: `cardrag-worker:020-paddle-2bfed5c195e3`; image ID `sha256:943becf993a6cc8348e7a25526b5b5d6cce7a54385542bf8f6be4970965df93a`.
- 운영 이미지 위에 인수된 Worker 6개 파일만 network 없는 빌드로 overlay했다. 파일별 SHA와 input SHA `2bfed5c195e37bd5c2f66ec5a5415a87a551bfc2f43898940fa262af57d5f242`는 후보 `operations/020-build-input.json`에 기록했다. 나머지 이미지 코드·의존성은 기존 운영 이미지다.
- 후보와 현 운영의 Compose 실효 environment, volume, tmpfs, user, read-only, security options, capabilities, entrypoint, command를 비교해 동일함을 확인했다. image만 교체한다. OCR은 `codex-exec`/`gpt-6.1-terra`/`high`, 폴백은 `local-paddleocr`/`PaddleOCR-VL-1.6`, 순회는 `round_robin`이다.
- 후보 이미지 import와 격리된 tmpfs의 capacity 거부 → dry-run → 링크 정리 → capacity 통과 및 `/tmp` 임시 alias 생성·정리 smoke가 통과했다. 별도의 격리 SQLite fixture에서 두 lock fence 보유, online snapshot, recovery-point 검증이 통과했다.
- 후보 Compose를 운영 state에 연결한 **dry-run**은 예상 run의 `.paddle-input-2788.pdf` 1건만 `would_remove`로 반환했다. `--apply`는 실행하지 않았다.
- 후보 이미지에서 추출한 실제 Worker 코드 조합으로 Worker/Core/runtime 전체 테스트를 실행했다. **1,969 passed, 1 failed**. 유일한 실패 `test_workspace_has_three_independent_packages_at_one_version`은 운영 이미지 Worker의 `__version__=1.0.36`과 현재 저장소 패키지 버전 `1.0.37`의 기존 차이에 대한 정적 버전 일치 검사다. 코드 조합이 저장소의 모든 파일과 동일하다고 주장하지 않는다. 인수된 전체 저장소 코드의 검증은 `REVIEW_01.md`의 1,970 passed다.
- 후보 적용 스크립트의 `shellcheck`, `bash -n`은 통과했다. 실제 root/service-account gate, 새 snapshot, 실운영 startup-check는 아직 실행하지 않았다.

## 준비된 실제 적용

```bash
sudo /opt/cardrag/020-paddle-20261028/operations/apply-020-paddle-20261028.sh
```

스크립트 SHA-256 `67367ef2f90fe3a2588c88619776b122ab74237a01fce7e29accd260cb036e6f`. 이 일회성 스크립트는 **10월 28일 10:00 KST 전**에만 실행한다. 실행 전에 이전 release·두 image ID·후보 파일 hash·실효 설정·서비스 계정 접근·timer·실행 중 Worker/backup과 여유 공간을 검사한다.

검사 통과 시 timer를 disable하고 Worker/backup lock을 함께 보유한 상태에서 두 SQLite DB의 새 online snapshot과 quick_check·SHA·stable pointer를 검증한다. 그 상태에서 active release를 전환하고, fence 해제 후 대상 링크가 정확히 1건인 것을 dry-run으로 검증해 정리한다. 정리 전후 CAS SHA를 확인하고 실제 서비스 계정으로 startup-check를 실행한다. 마지막으로 stable pointer가 보존됐는지 확인하고 단일 10:00 KST/Persistent=no timer를 재활성화한다. 수동 배치는 시작하지 않는다.

오류가 발생하면 스크립트는 snapshot fence를 해제하고 timer를 disabled로 남긴다. active release가 이미 바뀌었거나 링크 정리가 부분적으로 완료됐을 수 있으므로 출력과 상태를 확인해야 한다. 새 snapshot을 검증한 뒤에도 종전 snapshot은 삭제하지 않으며 현재/rollback recovery guidance는 새 보호 snapshot을 가리킨다. 이전 snapshot 정리는 별도 검토 대상이다.

## 남은 검증 및 GitHub 릴리스

실제 명령 종료 후 active release/image, startup-check JSON, alias 정리 및 CAS 증거, recovery-point와 stable pointer, timer의 다음 실행을 독립 확인해야 한다. 10:00 배치는 journal, terminal 결과, 실제 게시 generation 및 해당 scope 백업으로 확인한다. timer 활성화나 startup-check 성공만으로 배치 완주를 판정하지 않는다.

이번 변경에는 Worker 코드 수정이 포함되며 현재 Git 작업 트리에 미커밋 상태로 남아 있다. 로컬 운영 이미지 전환에 GitHub 릴리스 발행이 필수는 아니다. 다만 이 패치를 외부에서 추적·재현하려면 운영 검증 뒤 별도의 commit/PR·CI 및 새 소스 릴리스 판단이 필요하다. 현재 후보는 기존 1.0.36 운영 이미지에 로컬 patch를 적용한 이미지이며 기존 v1.0.37 GitHub 릴리스나 공개 registry image를 갱신하지 않았다.
