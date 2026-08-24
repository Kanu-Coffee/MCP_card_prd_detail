# Portainer 간편 설치 가이드

이 가이드는 **비어 있는 Docker Standalone 서버**에 CardRAG를 처음 설치하는
최단 경로다. 설치기는 host bind 경로, external volume, file-backed secret,
digest 고정 이미지 설정을 준비하지만 Portainer 관리자 암호나 Docker socket을
외부 서비스로 전송하지 않는다.

기존 `v0.1.2` 서버 또는 데이터가 든 PostgreSQL volume에는 이 설치기를 실행하지
않는다. 그런 경우에는 [RUNBOOK.md](RUNBOOK.md)의 *기존 named volume 전환* 절차를
사용한다. 설치기에는 기존 데이터를 덮어쓰는 `--force` 옵션이 없다.

## 준비물

- 대상 Docker endpoint host의 root shell
- Portainer의 **Docker Standalone** environment
- `/opt/cardrag`에 설치할 release tag와 정확히 일치하는 source checkout
- 검증을 마친 `release-manifest.json`
- 외부에서 접근할 Keycloak HTTPS origin 한 개
- OpenRouter API key

release manifest를 받는 명령과 세 이미지의 Cosign 검증은
[RUNBOOK.md](RUNBOOK.md#download-and-verify-release-evidence)를 먼저 수행한다.
설치기는 그 manifest에서 admin, worker, MCP의 digest-qualified image를 자동으로
추출하므로 이미지 tag를 따로 입력하지 않는다.

## 1. 입력 파일 만들기

secret은 설정 파일이나 명령 인자에 넣지 않는다. OpenRouter key만 별도의
owner-only 파일에 입력한다.

```sh
sudo install -d -o root -g root -m 0700 /root/cardrag-install
sudo install -o root -g root -m 0600 /dev/null \
  /root/cardrag-install/openrouter-api-key.txt
sudoedit /root/cardrag-install/openrouter-api-key.txt

sudo install -o root -g root -m 0600 \
  /opt/cardrag/deploy/portainer/quick-setup.conf.example \
  /root/cardrag-install/quick-setup.conf
sudoedit /root/cardrag-install/quick-setup.conf
```

설정 파일에서는 최소한 다음 두 값을 실제 값으로 바꾼다.

```text
KEYCLOAK_PUBLIC_URL=https://auth.example.com
CARDRAG_RELEASE_MANIFEST=/root/cardrag-install/release-manifest.json
```

기본 저장 위치를 바꾸려면 `CARDRAG_STATE_ROOT`와 그 하위 root를 함께 바꾼다.
서로 겹치는 경로, symlink를 통과하는 경로, `/srv` 같은 광범위한 경로는
거부된다.

## 2. 읽기 전용 점검 후 설치

먼저 dry-run을 실행한다. 이 단계는 디렉터리, secret, Docker volume을 만들지 않는다.

```sh
cd /opt/cardrag
sudo deploy/portainer/cardrag-portainer-setup.sh \
  --non-interactive \
  --config /root/cardrag-install/quick-setup.conf \
  --dry-run
```

`no host data was changed`가 출력되면 실제 준비와 검증을 실행한다.

```sh
sudo deploy/portainer/cardrag-portainer-setup.sh \
  --non-interactive \
  --config /root/cardrag-install/quick-setup.conf

sudo deploy/portainer/cardrag-portainer-setup.sh \
  --non-interactive \
  --config /root/cardrag-install/quick-setup.conf \
  --check
```

설정 파일 대신 화면 안내를 따라 입력하려면 `--interactive --dry-run`으로 먼저
점검한 뒤 `--interactive`로 실행할 수 있다. 다만 중단 재개와 bootstrap 완료 때 같은
값을 정확히 다시 제공해야 하므로 운영 설치에는 위의 root-only 설정 파일 방식을
권장한다.

설치가 중단되면 같은 명령과 같은 설정 파일로 다시 실행한다. 이미 만든 secret을
재생성하지 않고 installer marker와 fingerprint가 일치하는 단계만 재개한다. 설정을
바꾸거나 marker 없는 기존 volume/path를 발견하면 아무것도 덮어쓰지 않고 종료한다.

설치기가 만드는 저장 경계는 다음과 같다.

| Host 또는 volume | Container | 용도 | Stack 삭제 후 |
|---|---|---|---|
| `${CARDRAG_DATA_ROOT}/objects` | `/var/lib/cardrag/objects` | PDF/OCR/구조화 CAS | 유지 |
| `${CARDRAG_DATA_ROOT}/generations` | `/var/lib/cardrag/generations` | 게시 세대와 pointer | 유지 |
| `${CARDRAG_DATA_ROOT}/build` | `/var/lib/cardrag-build` | 재생성 가능한 작업공간 | 유지 |
| `${CARDRAG_DATA_ROOT}/page-cache` | `/var/cache/cardrag-pages` | 재생성 가능한 PNG | 유지 |
| `${CARDRAG_IMPORT_ROOT}` | `/mnt/cardrag-imports` | sealed legacy bundle | 유지 |
| `cardrag-postgres-v1` | PostgreSQL PGDATA | catalog/search/Keycloak | external volume 유지 |
| `cardrag-codex-auth-v1` | `/run/cardrag-codex` | worker device login | external volume 유지 |

실제 secret은 `${CARDRAG_SECRETS_DIR}`의 `0444` 파일에만 있고, 생성된
`/etc/cardrag/stack.env`에는 public URL, 경로, volume 이름, image digest만 있다.

설치기는 PostgreSQL admin, CardRAG owner/worker/MCP, Keycloak DB와 일회용
Keycloak admin password를 각각 독립적으로 생성하고, 세 CardRAG DSN을 그 파일에서
파생한다. 운영자가 직접 제공하는 secret은 owner-only
`CARDRAG_OPENROUTER_KEY_FILE` 하나뿐이다. 어떤 secret 값도 설정 파일, 명령 인자,
Portainer environment 또는 정상 로그에 출력되지 않는다.

## 3. Portainer에서 두 Stack 배포

Portainer에서 `/etc/cardrag/stack.env`를 환경변수 파일로 불러온다.

1. `cardrag-bootstrap` Stack을
   `deploy/portainer/cardrag-bootstrap-stack.yaml`로 만든다.
2. Keycloak이 healthy가 되면 bootstrap 계정으로 로그인한다.
3. 이름이 다른 영구 관리자 계정을 만들고, 새 private browser에서 그 계정의 로그인을
   검증한다. 그 계정으로 bootstrap 관리자 계정을 비활성화·삭제하거나 credential을
   회전한 뒤, 기존 bootstrap credential으로 로그인이 거부되는 것도 확인한다.
4. `cardrag-bootstrap` Stack만 삭제한다. **PostgreSQL external volume은 삭제하지 않는다.**
5. 다음 명령으로 일회용 password를 폐기하고 완료 marker를 기록한다.

   ```sh
   sudo /opt/cardrag/deploy/portainer/cardrag-portainer-setup.sh \
     --non-interactive \
     --config /root/cardrag-install/quick-setup.conf \
     --bootstrap-complete \
     --confirmed-permanent-admin \
     --confirmed-bootstrap-admin-revoked
   ```

6. `cardrag` Stack을 `deploy/portainer/cardrag-stack.yaml`로 만든다.

Keycloak public URL의 DNS, TLS 인증서와 reverse proxy는 외부 운영 경계다. 이것이
준비되지 않았으면 설치를 완료 상태로 간주하지 않는다.

## 4. Worker의 Codex 로그인

Portainer에서 `cardrag-worker-1`의 Console을 열고 기본 사용자로 다음을 실행한다.

```sh
codex login --device-auth
codex login status
```

표시된 URL과 짧은 코드를 신뢰할 수 있는 별도 기기에서 승인한다. worker container만
재시작한 뒤 새 Console에서 `codex login status`를 다시 확인한다. 로그인 전에는 BULK,
daily, legacy import를 시작하지 않는다.

## 5. 성공 확인

- PostgreSQL, Keycloak, MCP가 healthy이고 MCP readiness가 200이다.
- `/srv/cardrag/runtime`과 `/srv/cardrag/imports`가 host에서 확인된다.
- Stack을 재생성해도 external volume과 host 파일이 그대로다.
- bootstrap secret은 폐기됐고 normal Stack에는 mount되지 않는다.
- worker 재시작 뒤에도 `codex login status`가 성공한다.

레거시 자료가 있으면 다음 단계는
[LEGACY_IMPORT_QUICKSTART.ko.md](LEGACY_IMPORT_QUICKSTART.ko.md)다. NAS state export,
다른 서버 restore, timer와 기존 `v0.1.2` 전환은 [RUNBOOK.md](RUNBOOK.md)의 고급
운영 절차로 분리되어 있다.
