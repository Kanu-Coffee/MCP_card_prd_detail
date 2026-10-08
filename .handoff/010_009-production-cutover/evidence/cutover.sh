#!/bin/bash
set -euo pipefail
export PATH=/usr/local/bin:/usr/bin:/bin
cardrag_new=/opt/cardrag/009-b544a80
cardrag_old=/opt/cardrag/008-6b42a1a
cardrag_switched=0
[ "$(id -u)" -eq 0 ] || { echo 'Run this script with sudo.'; exit 1; }
[ "$(readlink -f /opt/cardrag/current)" = "$cardrag_old" ] || { echo 'Current snapshot changed; stop and recheck.'; exit 1; }
systemctl is-active --quiet cardrag-worker.timer || { echo 'Timer state changed; stop and recheck.'; exit 1; }
for cardrag_image in cardrag-worker:009-b544a80 cardrag-mcp:009-b544a80 cardrag-worker:008-6b42a1a cardrag-mcp:007-31edb1d; do
    docker image inspect "$cardrag_image" >/dev/null
 done
"$cardrag_new/operations/worker-compose.sh" config --quiet
"$cardrag_new/operations/mcp-compose.sh" config --quiet
cardrag_restore() {
    cardrag_exit=$?
    trap - EXIT
    if [ "$cardrag_exit" -ne 0 ]; then
        if [ "$cardrag_switched" -eq 1 ]; then
            python3 - <<'PY'
import os
p='/opt/cardrag/current.restore-'+str(os.getpid())
os.symlink('/opt/cardrag/008-6b42a1a',p)
os.replace(p,'/opt/cardrag/current')
PY
            "$cardrag_old/operations/mcp-compose.sh" up -d --no-build --pull never mcp || true
        fi
        systemctl start cardrag-worker.timer || true
        echo 'Cutover failed; previous snapshot and timer restoration attempted. Check readiness.'
    fi
    exit "$cardrag_exit"
}
trap cardrag_restore EXIT
systemctl stop cardrag-worker.timer
cardrag_service_status=$(systemctl is-active cardrag-worker.service || true)
[ "$cardrag_service_status" = inactive ] || { echo 'Worker service is not inactive; cutover cancelled.'; exit 1; }
cardrag_active_worker=$(docker ps --filter label=com.docker.compose.service=worker --format '{{.ID}}')
[ -z "$cardrag_active_worker" ] || { echo 'A Worker container is running; cutover cancelled.'; exit 1; }
python3 - <<'PY'
import os
p='/opt/cardrag/current.switch-'+str(os.getpid())
os.symlink('/opt/cardrag/009-b544a80',p)
os.replace(p,'/opt/cardrag/current')
PY
cardrag_switched=1
"$cardrag_new/operations/mcp-compose.sh" up -d --no-build --pull never mcp
cardrag_ready=0
for ((cardrag_attempt=1;cardrag_attempt<=90;cardrag_attempt++)); do
    if curl --max-time 5 -fsS http://127.0.0.1:18015/health/ready >/dev/null 2>&1; then
        cardrag_ready=1
        break
    fi
    sleep 2
 done
[ "$cardrag_ready" -eq 1 ] || { echo 'New MCP did not become ready; restoring previous deployment.'; exit 1; }
systemctl start cardrag-worker.timer
systemctl is-active --quiet cardrag-worker.timer
python3 - <<'PY'
import json
from datetime import datetime,timezone
from pathlib import Path
Path('/opt/cardrag/009-b544a80/operations/cutover-status.json').write_text(json.dumps({'status':'deployed','snapshot':'/opt/cardrag/009-b544a80','readiness':True,'timer':'active','completed_at':datetime.now(timezone.utc).isoformat()},indent=2)+'\n')
PY
trap - EXIT
curl -fsS http://127.0.0.1:18015/health/ready
systemctl list-timers cardrag-worker.timer --no-pager
