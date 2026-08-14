#!/bin/sh
set -eu

repository_root=$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)
temporary_root=$(mktemp -d)
cleanup() {
    chmod -R u+rwx "$temporary_root" 2>/dev/null || true
    rm -rf "$temporary_root"
}
trap cleanup EXIT HUP INT TERM

source_objects=$temporary_root/source-objects
source_generations=$temporary_root/source-generations
target_objects=$temporary_root/target-objects
target_generations=$temporary_root/target-generations
reports=$temporary_root/reports
mkdir -p \
    "$source_objects/.incoming" \
    "$source_objects/sha256" \
    "$source_generations/generations" \
    "$target_objects" \
    "$target_generations" \
    "$reports"
chmod 0750 "$reports"
chmod 0750 "$target_objects" "$target_generations"

printf 'portable object\n' >"$temporary_root/object"
object_sha=$(sha256sum "$temporary_root/object" | awk '{print $1}')
mkdir -p "$source_objects/sha256/${object_sha%${object_sha#??}}"
cp "$temporary_root/object" "$source_objects/sha256/${object_sha%${object_sha#??}}/$object_sha"

python3 - "$source_generations" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
generation_id = "gen-20260813T000000Z-0123456789ab"
generation = root / "generations" / generation_id
generation.mkdir()
payload = b"generation payload\n"
(generation / "quality.json").write_bytes(payload)
manifest = {
    "generation_id": generation_id,
    "files": [{
        "path": "quality.json",
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size": len(payload),
    }],
}
canonical = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
manifest_sha = hashlib.sha256(canonical).hexdigest()
(generation / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
(generation / "READY").write_text(json.dumps({
    "generation_id": generation_id,
    "manifest_sha256": manifest_sha,
}), encoding="utf-8")
(root / "current.json").write_text(json.dumps({
    "generation_id": generation_id,
    "manifest_sha256": manifest_sha,
}), encoding="utf-8")
PY

python3 "$repository_root/deploy/portainer/scripts/verify-runtime-storage.py" \
    --objects-root "$source_objects" \
    --generations-root "$source_generations" >/dev/null

unsafe_mode_objects=$temporary_root/unsafe-mode-objects
unsafe_mode_generations=$temporary_root/unsafe-mode-generations
unsafe_mode_reports=$temporary_root/unsafe-mode-reports
mkdir -p "$unsafe_mode_objects" "$unsafe_mode_generations" "$unsafe_mode_reports"
chmod 0770 "$unsafe_mode_objects"
chmod 0750 "$unsafe_mode_generations" "$unsafe_mode_reports"
if CARDRAG_LEGACY_OBJECTS_VOLUME=fixture-objects \
   CARDRAG_LEGACY_GENERATIONS_VOLUME=fixture-generations \
   CARDRAG_STORAGE_MIGRATION_ID=unsafe-mode \
   CARDRAG_RUNTIME_UID=$(id -u) \
   CARDRAG_RUNTIME_GID=$(id -g) \
   CARDRAG_MIGRATION_SOURCE_OBJECTS_ROOT=$source_objects \
   CARDRAG_MIGRATION_SOURCE_GENERATIONS_ROOT=$source_generations \
   CARDRAG_MIGRATION_TARGET_OBJECTS_ROOT=$unsafe_mode_objects \
   CARDRAG_MIGRATION_TARGET_GENERATIONS_ROOT=$unsafe_mode_generations \
   CARDRAG_MIGRATION_REPORTS_ROOT=$unsafe_mode_reports \
   CARDRAG_STORAGE_VERIFIER=$repository_root/deploy/portainer/scripts/verify-runtime-storage.py \
       "$repository_root/deploy/portainer/scripts/storage-migrate.sh" >/dev/null 2>&1; then
    echo "storage migration accepted an unsafe target root mode" >&2
    exit 1
fi
test -z "$(find "$unsafe_mode_objects" "$unsafe_mode_generations" -mindepth 1 -print -quit)"

generation_path=$(find "$source_generations/generations" -mindepth 1 -maxdepth 1 -type d)
printf 'unlisted\n' >"$generation_path/unlisted.txt"
if python3 "$repository_root/deploy/portainer/scripts/verify-runtime-storage.py" \
    --objects-root "$source_objects" \
    --generations-root "$source_generations" >/dev/null 2>&1; then
    echo "generation verifier accepted a file absent from its manifest" >&2
    exit 1
fi
rm -f "$generation_path/unlisted.txt"
mkdir "$generation_path/unlisted-directory"
if python3 "$repository_root/deploy/portainer/scripts/verify-runtime-storage.py" \
    --objects-root "$source_objects" \
    --generations-root "$source_generations" >/dev/null 2>&1; then
    echo "generation verifier accepted an unlisted empty directory" >&2
    exit 1
fi
rmdir "$generation_path/unlisted-directory"

python3 - "$source_generations" <<'PY'
import json
import sys
from pathlib import Path

pointer = Path(sys.argv[1]) / "current.json"
payload = json.loads(pointer.read_text())
payload["previous_generation_id"] = "gen-20260812T010203Z-fedcba987654"
pointer.write_text(json.dumps(payload))
PY
if python3 "$repository_root/deploy/portainer/scripts/verify-runtime-storage.py" \
    --objects-root "$source_objects" \
    --generations-root "$source_generations" >/dev/null 2>&1; then
    echo "generation verifier accepted a missing previous generation" >&2
    exit 1
fi
python3 - "$source_generations" <<'PY'
import json
import sys
from pathlib import Path

pointer = Path(sys.argv[1]) / "current.json"
payload = json.loads(pointer.read_text())
payload.pop("previous_generation_id")
pointer.write_text(json.dumps(payload))
PY

printf 'do not truncate\n' >"$temporary_root/report-target"
ln -s "$temporary_root/report-target" "$reports/unsafe-report.json"
if python3 "$repository_root/deploy/portainer/scripts/verify-runtime-storage.py" \
    --objects-root "$source_objects" \
    --generations-root "$source_generations" \
    --output "$reports/unsafe-report.json" >/dev/null 2>&1; then
    echo "storage verifier accepted a symlink report destination" >&2
    exit 1
fi
test "$(cat "$temporary_root/report-target")" = 'do not truncate'

blocked_objects=$temporary_root/blocked-objects
blocked_generations=$temporary_root/blocked-generations
mkdir -p "$blocked_objects" "$blocked_generations"
chmod 0750 "$blocked_objects" "$blocked_generations"
printf 'completion victim\n' >"$temporary_root/completion-victim"
ln -s "$temporary_root/completion-victim" "$reports/blocked-complete.json"
if CARDRAG_LEGACY_OBJECTS_VOLUME=fixture-objects \
   CARDRAG_LEGACY_GENERATIONS_VOLUME=fixture-generations \
   CARDRAG_STORAGE_MIGRATION_ID=blocked \
   CARDRAG_RUNTIME_UID=$(id -u) \
   CARDRAG_RUNTIME_GID=$(id -g) \
   CARDRAG_MIGRATION_SOURCE_OBJECTS_ROOT=$source_objects \
   CARDRAG_MIGRATION_SOURCE_GENERATIONS_ROOT=$source_generations \
   CARDRAG_MIGRATION_TARGET_OBJECTS_ROOT=$blocked_objects \
   CARDRAG_MIGRATION_TARGET_GENERATIONS_ROOT=$blocked_generations \
   CARDRAG_MIGRATION_REPORTS_ROOT=$reports \
   CARDRAG_STORAGE_VERIFIER=$repository_root/deploy/portainer/scripts/verify-runtime-storage.py \
       "$repository_root/deploy/portainer/scripts/storage-migrate.sh" >/dev/null 2>&1; then
    echo "storage migration accepted a symlink completion report" >&2
    exit 1
fi
test "$(cat "$temporary_root/completion-victim")" = 'completion victim'
test -z "$(find "$blocked_objects" "$blocked_generations" -mindepth 1 -print -quit)"

CARDRAG_LEGACY_OBJECTS_VOLUME=fixture-objects \
CARDRAG_LEGACY_GENERATIONS_VOLUME=fixture-generations \
CARDRAG_STORAGE_MIGRATION_ID=fixture \
CARDRAG_RUNTIME_UID=$(id -u) \
CARDRAG_RUNTIME_GID=$(id -g) \
CARDRAG_MIGRATION_SOURCE_OBJECTS_ROOT=$source_objects \
CARDRAG_MIGRATION_SOURCE_GENERATIONS_ROOT=$source_generations \
CARDRAG_MIGRATION_TARGET_OBJECTS_ROOT=$target_objects \
CARDRAG_MIGRATION_TARGET_GENERATIONS_ROOT=$target_generations \
CARDRAG_MIGRATION_REPORTS_ROOT=$reports \
CARDRAG_STORAGE_VERIFIER=$repository_root/deploy/portainer/scripts/verify-runtime-storage.py \
    "$repository_root/deploy/portainer/scripts/storage-migrate.sh" >/dev/null

test -f "$reports/fixture-complete.json"
test -f "$target_objects/sha256/${object_sha%${object_sha#??}}/$object_sha"
test -f "$target_generations/current.json"

# Simulate a crash after the durable completion report was published but before
# either commit marker was removed. The same-ID explicit resume must verify the
# complete target and finalize only those owned markers.
owner_body=$(printf '%s\n%s\n%s\n%s' \
    cardrag-storage-migration-owner.v1 \
    migration_id=fixture \
    objects_volume=fixture-objects \
    generations_volume=fixture-generations)
printf '%s\n' "$owner_body" >"$target_objects/.cardrag-storage-migration-commit"
printf '%s\n' "$owner_body" >"$target_generations/.cardrag-storage-migration-commit"
chmod 0600 "$target_objects/.cardrag-storage-migration-commit" \
    "$target_generations/.cardrag-storage-migration-commit"
CARDRAG_LEGACY_OBJECTS_VOLUME=fixture-objects \
CARDRAG_LEGACY_GENERATIONS_VOLUME=fixture-generations \
CARDRAG_STORAGE_MIGRATION_ID=fixture \
CARDRAG_STORAGE_MIGRATION_RESUME=true \
CARDRAG_RUNTIME_UID=$(id -u) \
CARDRAG_RUNTIME_GID=$(id -g) \
CARDRAG_MIGRATION_SOURCE_OBJECTS_ROOT=$source_objects \
CARDRAG_MIGRATION_SOURCE_GENERATIONS_ROOT=$source_generations \
CARDRAG_MIGRATION_TARGET_OBJECTS_ROOT=$target_objects \
CARDRAG_MIGRATION_TARGET_GENERATIONS_ROOT=$target_generations \
CARDRAG_MIGRATION_REPORTS_ROOT=$reports \
CARDRAG_STORAGE_VERIFIER=$repository_root/deploy/portainer/scripts/verify-runtime-storage.py \
    "$repository_root/deploy/portainer/scripts/storage-migrate.sh" >/dev/null
test ! -e "$target_objects/.cardrag-storage-migration-commit"
test ! -e "$target_generations/.cardrag-storage-migration-commit"

python3 "$repository_root/deploy/portainer/scripts/verify-runtime-storage.py" \
    --objects-root "$target_objects" \
    --generations-root "$target_generations" >/dev/null

chmod 0644 "$target_objects/sha256/${object_sha%${object_sha#??}}/$object_sha"
printf 'tampered\n' >"$target_objects/sha256/${object_sha%${object_sha#??}}/$object_sha"
if python3 "$repository_root/deploy/portainer/scripts/verify-runtime-storage.py" \
    --objects-root "$target_objects" \
    --generations-root "$target_generations" >/dev/null 2>&1; then
    echo "tampered CAS object was accepted" >&2
    exit 1
fi

# A failed run retains marker-owned, migration-ID-specific staging. It cannot be
# reclaimed accidentally; an explicit retry with the same ID is required.
partial_objects=$temporary_root/partial-objects
partial_generations=$temporary_root/partial-generations
partial_reports=$temporary_root/partial-reports
mkdir -p "$partial_objects" "$partial_generations" "$partial_reports"
chmod 0750 "$partial_reports"
chmod 0750 "$partial_objects" "$partial_generations"
if CARDRAG_LEGACY_OBJECTS_VOLUME=fixture-objects \
   CARDRAG_LEGACY_GENERATIONS_VOLUME=fixture-generations \
   CARDRAG_STORAGE_MIGRATION_ID=partial \
   CARDRAG_RUNTIME_UID=$(id -u) \
   CARDRAG_RUNTIME_GID=$(id -g) \
   CARDRAG_MIGRATION_SOURCE_OBJECTS_ROOT=$source_objects \
   CARDRAG_MIGRATION_SOURCE_GENERATIONS_ROOT=$source_generations \
   CARDRAG_MIGRATION_TARGET_OBJECTS_ROOT=$partial_objects \
   CARDRAG_MIGRATION_TARGET_GENERATIONS_ROOT=$partial_generations \
   CARDRAG_MIGRATION_REPORTS_ROOT=$partial_reports \
   CARDRAG_STORAGE_VERIFIER=/bin/false \
       "$repository_root/deploy/portainer/scripts/storage-migrate.sh" >/dev/null 2>&1; then
    echo "forced partial migration unexpectedly succeeded" >&2
    exit 1
fi
test -f "$partial_objects/.migration-incoming-partial/.cardrag-storage-migration-owner"
if CARDRAG_LEGACY_OBJECTS_VOLUME=fixture-objects \
   CARDRAG_LEGACY_GENERATIONS_VOLUME=fixture-generations \
   CARDRAG_STORAGE_MIGRATION_ID=partial \
   CARDRAG_RUNTIME_UID=$(id -u) \
   CARDRAG_RUNTIME_GID=$(id -g) \
   CARDRAG_MIGRATION_SOURCE_OBJECTS_ROOT=$source_objects \
   CARDRAG_MIGRATION_SOURCE_GENERATIONS_ROOT=$source_generations \
   CARDRAG_MIGRATION_TARGET_OBJECTS_ROOT=$partial_objects \
   CARDRAG_MIGRATION_TARGET_GENERATIONS_ROOT=$partial_generations \
   CARDRAG_MIGRATION_REPORTS_ROOT=$partial_reports \
   CARDRAG_STORAGE_VERIFIER=$repository_root/deploy/portainer/scripts/verify-runtime-storage.py \
       "$repository_root/deploy/portainer/scripts/storage-migrate.sh" >/dev/null 2>&1; then
    echo "partial migration was reclaimed without explicit resume" >&2
    exit 1
fi
CARDRAG_LEGACY_OBJECTS_VOLUME=fixture-objects \
CARDRAG_LEGACY_GENERATIONS_VOLUME=fixture-generations \
CARDRAG_STORAGE_MIGRATION_ID=partial \
CARDRAG_STORAGE_MIGRATION_RESUME=true \
CARDRAG_RUNTIME_UID=$(id -u) \
CARDRAG_RUNTIME_GID=$(id -g) \
CARDRAG_MIGRATION_SOURCE_OBJECTS_ROOT=$source_objects \
CARDRAG_MIGRATION_SOURCE_GENERATIONS_ROOT=$source_generations \
CARDRAG_MIGRATION_TARGET_OBJECTS_ROOT=$partial_objects \
CARDRAG_MIGRATION_TARGET_GENERATIONS_ROOT=$partial_generations \
CARDRAG_MIGRATION_REPORTS_ROOT=$partial_reports \
CARDRAG_STORAGE_VERIFIER=$repository_root/deploy/portainer/scripts/verify-runtime-storage.py \
    "$repository_root/deploy/portainer/scripts/storage-migrate.sh" >/dev/null
test -f "$partial_reports/partial-complete.json"

# A host-wide advisory lock rejects a concurrent migration before it touches a
# target. The lock process is bounded to five seconds even if the assertion fails.
locked_objects=$temporary_root/locked-objects
locked_generations=$temporary_root/locked-generations
locked_reports=$temporary_root/locked-reports
mkdir -p "$locked_objects" "$locked_generations" "$locked_reports"
chmod 0750 "$locked_reports"
chmod 0750 "$locked_objects" "$locked_generations"
flock "$locked_reports/.storage-migrate.lock" -c 'sleep 5' &
lock_pid=$!
sleep 0.1
if CARDRAG_LEGACY_OBJECTS_VOLUME=fixture-objects \
   CARDRAG_LEGACY_GENERATIONS_VOLUME=fixture-generations \
   CARDRAG_STORAGE_MIGRATION_ID=locked \
   CARDRAG_RUNTIME_UID=$(id -u) \
   CARDRAG_RUNTIME_GID=$(id -g) \
   CARDRAG_MIGRATION_SOURCE_OBJECTS_ROOT=$source_objects \
   CARDRAG_MIGRATION_SOURCE_GENERATIONS_ROOT=$source_generations \
   CARDRAG_MIGRATION_TARGET_OBJECTS_ROOT=$locked_objects \
   CARDRAG_MIGRATION_TARGET_GENERATIONS_ROOT=$locked_generations \
   CARDRAG_MIGRATION_REPORTS_ROOT=$locked_reports \
   CARDRAG_STORAGE_VERIFIER=$repository_root/deploy/portainer/scripts/verify-runtime-storage.py \
       "$repository_root/deploy/portainer/scripts/storage-migrate.sh" >/dev/null 2>&1; then
    kill "$lock_pid" 2>/dev/null || true
    echo "concurrent storage migration was accepted" >&2
    exit 1
fi
kill "$lock_pid" 2>/dev/null || true
wait "$lock_pid" 2>/dev/null || true
test -z "$(find "$locked_objects" "$locked_generations" -mindepth 1 -print -quit)"

echo "storage migration tests passed"
