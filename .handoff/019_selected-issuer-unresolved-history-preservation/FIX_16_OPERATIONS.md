# FIX_16 운영 준비·전환 절차 (Reviewer 통과 후 실행)

이 문서는 FIX_14/15 보고서의 배포 예시를 대체한다. **이번 Executor는 아래 빌드·snapshot·전환·삭제를 실행하지 않았다.** 현재 release는 `/opt/cardrag/019-e7fb8a0ba60c`다. 새 release는 검토된 고정 소스와 이미지 ID로 식별하며 activation과 BC 실행은 별도 단계다. 명령은 저장소 root의 같은 Bash 세션에서 수행한다.

## 1. 소스 고정 및 image 준비 (아직 활성화하지 않음)

기존 operations는 저장소 밖에 있으므로 명시적으로 복사한다. current의 live 결과와 비공개 설정은 새 release 안에만 복사하고 커밋하지 않는다. 소스 snapshot 이후 원본 작업 트리를 다시 복사하여 candidate 내용을 바꾸지 않는다.

```bash
set -euo pipefail
REPO=/home/lee/projects/MCP_card_prd_detail
PREV_REL=$(readlink -f /opt/cardrag/current)
STAGE=$(mktemp -d /opt/cardrag/.fix16-stage.XXXXXX)
rsync -a --exclude .git --exclude .venv --exclude __pycache__ --exclude .pytest_cache --exclude .ruff_cache "$REPO/" "$STAGE/"
mkdir -p "$STAGE/operations"
cp -a "$PREV_REL/operations/." "$STAGE/operations/"
# 불변 snapshot 안의 tracked/untracked 실제 제품 입력 모두 포함.
TREE_SHA=$(python3 - "$STAGE" <<'PY'
import hashlib,sys
from pathlib import Path
root=Path(sys.argv[1]); files=[]
for name in ('apps/cardrag-worker','apps/cardrag-mcp','packages/cardrag-core','deploy/worker'):
    files.extend(p for p in (root/name).rglob('*') if p.is_file())
files.extend(root/p for p in ('pyproject.toml','uv.lock','Dockerfile','README.md','.dockerignore'))
h=hashlib.sha256()
for p in sorted(set(files),key=lambda p:p.relative_to(root).as_posix()):
    if p.exists():h.update(p.relative_to(root).as_posix().encode()+b'\0'+p.read_bytes()+b'\0')
print(h.hexdigest())
PY
)
REL_TAG="019-fix16-${TREE_SHA:0:12}"
NEW_REL="/opt/cardrag/$REL_TAG"
test ! -e "$NEW_REL"
mv "$STAGE" "$NEW_REL"
IMAGE="cardrag-worker:$REL_TAG"
# Root Dockerfile을 사용한다. 네트워크/빌드 공간이 필요한 정규 빌드다.
docker build --target worker --label "io.cardrag.source-tree-sha256=$TREE_SHA" \
  --label "org.opencontainers.image.version=$REL_TAG" -t "$IMAGE" -f "$NEW_REL/Dockerfile" "$NEW_REL"
IMAGE_ID=$(docker image inspect "$IMAGE" --format '{{.Id}}')
```

offline 기존-runtime overlay를 대신 사용할 경우 dependency declarations/uv.lock이 기존 release와 동일함을 먼저 확인하고 base **image ID**와 설치 소스 대조를 기록한다. 과거 `code-image.Dockerfile`의 version/source digest/base ID label을 그대로 사용하지 않는다. 위 정규 빌드는 그 파일에 의존하지 않는다.

새 이미지 설치 소스와 snapshot의 worker/core `src/`를 파일별 SHA/bytes로 대조하고 `py.typed` 포함 여부, CLI version/import를 검증한다. 이미지 ID·TREE_SHA·base/dependency 근거를 deployment.json에 기록한다. worker가 기본 실행되지 않도록 조회 시에는 명시적으로 `--entrypoint`를 지정한다.

```bash
# private overlay의 release 내부 경로와 이미지 참조를 새 것으로 바꾼다.
# 내용이나 비밀 값은 터미널에 출력하지 않는다.
python3 - "$PREV_REL" "$NEW_REL" "$IMAGE" "$IMAGE_ID" "$TREE_SHA" <<'PY'
import json,sys
from pathlib import Path
old,new,image,image_id,digest=sys.argv[1:]; ops=Path(new)/'operations'
p=ops/'compose.runtime.private.yaml'
s=p.read_text().replace(old,new).replace('cardrag-worker:'+Path(old).name,image)
p.write_text(s)
(ops/'deployment.json').write_text(json.dumps({
  'release':new,'image':image,'image_id':image_id,'source_tree_sha256':digest,
  'previous_release':old,'activation':'prepared','review':'019 FIX_16',
  'method':'root Dockerfile worker target; frozen source snapshot'},indent=2)+'\n')
PY
chgrp -R cardrag "$NEW_REL/operations"
chmod 2750 "$NEW_REL/operations" "$NEW_REL/operations/live"
find "$NEW_REL/operations" -maxdepth 1 -name '*.sh' -exec chmod 750 {} +
"$NEW_REL/operations/worker-compose.sh" config --quiet
test "$("$NEW_REL/operations/worker-compose.sh" config --images | head -n 1)" = "$IMAGE"
test "$(docker image inspect "$IMAGE" --format '{{.Id}}')" = "$IMAGE_ID"
```

operations의 예전 code-image.Dockerfile/과거 deployment 증거는 새 build 입력이 아니다. 새 deployment.json은 prepared이며 source 검증·아래 snapshot 증거를 추가하기 전 activate하지 않는다. 운영 helper에서 상속된 old digest를 새 검증 결과로 표시하지 않는다. private overlay가 다른 이미지 표기 방식을 쓰면 위 마지막 비교가 실패하므로 내용 출력 없이 원래 image 필드만 수정해 재확인한다.

## 2. 정지 경계·복구 snapshot·activation

timer/service는 inactive여야 한다. `docker ps -q --filter label=com.docker.compose.project=cardrag-worker --filter label=com.docker.compose.service=worker`로 수동 run까지 없음을 확인한다. 이어 UID10001로 state volume의 `worker.lock` 및 `backup.lock`을 `fcntl.flock(LOCK_EX|LOCK_NB)`로 모두 획득하는 전용 network-none 컨테이너를 유지한다. 어느 lock이라도 실패하면 전환을 중단한다. **조회 후 바로 lock을 해제하고 복사하는 방식은 금지한다.** lock holder의 PID/컨테이너 ID와 ready를 확인하고, snapshot 및 포인터 전환이 끝날 때까지 유지한다. timer와 수동 기동도 정지 구간 동안 금지한다.

lock holder 안에서 `sqlite3.Connection.backup()`으로 worker/backup DB를 새 `deployment-backups/$REL_TAG/`에 보존한다. 읽는 DB에는 RO connection, 새 snapshot만 RW다. 두 DB는 모든 writer가 같은 lock을 준수하고 정지한 구간에서 연속 snapshot한다. main DB 단순 복사나 WAL 파일 무시는 하지 않는다. snapshot마다 quick_check=ok, SHA256·크기·UTC시각·latest run·stable generation을 기록하고 비교 대상 복구본과의 의존성도 manifest로 기록한다. 현재 두 DB가 바뀌었으므로 이번 배포는 새 snapshot이 필요하다. 기존 snapshot과 내용 동일성이 증명된 파일만 재사용하며 의존 대상 삭제를 금지한다.

activation 직전 snapshot 증거와 이미지 ID를 다시 확인한다. 기존 `activate.sh`의 timer/service/Worker-container 검사와 물리 경로 확인을 유지하고, lock holder를 가진 채 `"$NEW_REL/operations/activate.sh"`를 실행한다. lock holder는 **Worker 서비스 label 없이** 실행해야 실제 Worker로 오인되지 않는다. 실패/성공 모두 holder를 종료하여 lock을 해제한다. 이 과정은 helper config 검증·원자적 current 포인터 전환만 수행하며 `up -d`, `run`, timer start를 실행하지 않는다.

구체적인 snapshot/fence 도구는 새 snapshot에 포함된 `deploy/worker/snapshot-lock.py`다. 두 lock 획득 실패 시 ready를 만들지 않고 종료한다. snapshot 실패 디렉터리는 partial이며 검증된 복구점으로 표시하지 않는다.

```bash
cat > "$NEW_REL/operations/verify-source.py" <<'PYVERIFY'
import importlib,json
from pathlib import Path
count=0
for component,module in [('apps/cardrag-worker','cardrag_worker'),('packages/cardrag-core','cardrag_core')]:
    installed=Path(importlib.import_module(module).__file__).parent
    src=Path('/snapshot')/component/'src'/module
    for p in src.rglob('*'):
        if p.is_file() and '__pycache__' not in p.parts:
            if p.read_bytes()!=(installed/p.relative_to(src)).read_bytes():
                raise RuntimeError('installed source mismatch: '+str(p.relative_to(src)))
            count+=1
print(json.dumps({'verified_installed_source_files':count}))
PYVERIFY
docker run --rm --network none --read-only --entrypoint /opt/cardrag/bin/python \
  -v "$NEW_REL:/snapshot:ro" "$IMAGE_ID" /snapshot/operations/verify-source.py
for UNIT in cardrag-worker.timer cardrag-worker.service; do
  if systemctl is-active --quiet "$UNIT"; then echo 'Worker/timer active: abort' >&2; exit 1; fi
done
if test -n "$(docker ps -q --filter label=com.docker.compose.project=cardrag-worker --filter label=com.docker.compose.service=worker)"; then
  echo 'Worker container active: abort' >&2; exit 1
fi
HOLDER=$(docker run -d --network none --read-only --init --tmpfs /tmp:rw,nosuid,size=16m \
  --entrypoint /opt/cardrag/bin/python -v cardrag-worker-state:/state \
  -v cardrag-serving:/serving:ro -v "$NEW_REL/deploy/worker/snapshot-lock.py:/hold.py:ro" \
  "$IMAGE_ID" /hold.py /state /serving "$REL_TAG")
trap 'docker rm -f "$HOLDER" >/dev/null' EXIT
READY=0
for ((attempt=0;attempt<600;attempt++)); do
  if docker exec "$HOLDER" test -f /tmp/ready; then READY=1; break; fi
  test "$(docker inspect "$HOLDER" --format '{{.State.Running}}')" = true || break
  sleep 1
done
test "$READY" = 1 || { echo 'Snapshot/lock failed: abort' >&2; exit 1; }
docker exec "$HOLDER" cat /tmp/evidence.json > "$NEW_REL/operations/backup-verified.json"
python3 - "$NEW_REL/operations" <<'PYMETA'
import json,sys
from pathlib import Path
ops=Path(sys.argv[1]);p=ops/'deployment.json';d=json.loads(p.read_text())
d['state_backup']=json.loads((ops/'backup-verified.json').read_text())
p.write_text(json.dumps(d,indent=2)+'\n')
PYMETA
# activate.sh가 timer/service/Worker 컨테이너를 재확인한다. holder는 계속 lock을 보유한다.
"$NEW_REL/operations/activate.sh"
docker rm -f "$HOLDER" >/dev/null
trap - EXIT
```

source 검증·snapshot·activation 중 실패하면 복귀/정리 판단 전에 오류와 ready/evidence를 확인한다. host에는 cardrag 그룹 권한이 필요하다. 위 도구는 운영 실행에 사용하는 명령이며 이번 Executor는 fixture 검증만 수행했다.

## 3. 복귀

위와 같은 timer/service/수동 Worker 비실행 검사와 두 lock을 유지한 상태에서 previous-release.txt의 **검증된 실제 디렉터리**로 임시 symlink를 만들고 `mv -Tf`로 current를 교체한다. 기존 release 이미지/config를 확인한다. `up -d`나 timer start는 없다. 스키마/데이터가 바뀐 상태에서 DB 복구가 필요하면 보존된 snapshot 두 개의 시점·호환성을 먼저 검증한다. 코드 포인터 복귀와 DB restore를 혼동하지 않는다. old DB로 무조건 교체하는 rollback 명령은 제공하지 않는다.

## 4. 사용자 수동 BC 검증 (새 배포 Reviewer 통과 후)

```bash
/opt/cardrag/current/operations/run-issuer.sh bc
/opt/cardrag/current/operations/status-issuer.sh bc
/opt/cardrag/current/operations/summary-issuers.sh
```

BC exit0/succeeded 및 자체 seal/새 generation, 비선택 변경0을 먼저 확인한다. 실패 run에는 백업 완료를 요구하지 않는다. 성공한 **새 RUN_ID**로 다음을 사용한다.

```bash
OPS=/opt/cardrag/current/operations
RUN_ID=여기에_성공한_BC_RUN_ID
"$OPS/worker-compose.sh" run --rm --no-deps worker backup status --run "$RUN_ID" --issuer bc
"$OPS/worker-compose.sh" run --rm --no-deps worker backup flush --run "$RUN_ID" --issuer bc
```

status는 RO 진단으로 complete=null이다. flush 후 원격 receipt/index까지 일치해야 complete=True다. 정상 정책상 제외와 required/unbound·lost_source를 구분한다. 삼성 과거 run은 별도로 `--issuer samsung --run 44d55df9db8645c8b9060dc4631ff463`로 진단한다.

배포/실행 전과 실행 후 `df -h /`, Worker state·serving·MCP volume 용량 및 Docker usage를 기록한다. 실행 중에는 같은 RO 용량 조회로 증가/피크를 관측한다. 최소2GiB gate만으로 충분한 임시 공간을 주장하지 않는다. 현재 보존 후보 2개의 약13.6GiB는 의존성·hardlink/shared block 검증 전의 추정값이며 자동 삭제 승인이 아니다.
