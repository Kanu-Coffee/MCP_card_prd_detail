#!/bin/sh
# Install the Worker service/timer and leave scheduled execution disabled.
# Run as root after the verified release is active and /etc/cardrag/worker.env
# and the active release operations helper exist.
set -eu
ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd -P)
# Reject inaccessible releases before changing installed units or timer state.
"$ROOT/deploy/worker/verify-service-access.sh" /opt/cardrag/current
install -m 0644 "$ROOT/deploy/worker/cardrag-worker.service" /etc/systemd/system/cardrag-worker.service
install -m 0644 "$ROOT/deploy/worker/cardrag-worker.timer" /etc/systemd/system/cardrag-worker.timer
systemctl daemon-reload
systemctl disable --now cardrag-worker.timer
printf 'Installed. Timer remains disabled until an explicit enable after validation.\n'
systemctl show cardrag-worker.timer -p UnitFileState -p ActiveState -p Persistent
systemctl show cardrag-worker.service -p UnitFileState -p ActiveState
