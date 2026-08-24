#!/bin/sh
set -eu

repository_root=$(CDPATH='' cd -- "$(dirname -- "$0")/../../.." && pwd)
transfer=$repository_root/deploy/portainer/cardrag-legacy-transfer.sh
temporary_root=$(mktemp -d)
cleanup() {
    chmod -R u+rwx "$temporary_root" 2>/dev/null || true
    rm -rf "$temporary_root"
}
trap cleanup EXIT HUP INT TERM

python=$repository_root/.venv/bin/python
cardrag=$repository_root/.venv/bin/cardrag
[ -x "$python" ] && [ -x "$cardrag" ] || {
    echo "locked test environment is required" >&2
    exit 69
}

source_root=$temporary_root/legacy-source
bundle_root=$temporary_root/prepared
import_root=$temporary_root/imports
mkdir -p "$source_root/raw" "$source_root/ocr" "$import_root"
chmod 0750 "$source_root" "$source_root/raw" "$source_root/ocr" "$import_root"

"$python" - "$source_root" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

from tests.support_pdf import synthetic_text_pdf_bytes

root = Path(sys.argv[1])
pdf = synthetic_text_pdf_bytes(["legacy transfer fixture"])
ocr = "## Page 1\n레거시 이전 시험\n".encode()
(root / "raw/card.pdf").write_bytes(pdf)
(root / "ocr/ocr.md").write_bytes(ocr)
(root / "ocr/metadata.json").write_text(json.dumps({
    "schema_version": "ocr_result_manifest.v2",
    "raw_pdf_rel_path": "raw/card.pdf",
    "ocr_md_rel_path": "ocr/ocr.md",
    "ocr_md_sha256": hashlib.sha256(ocr).hexdigest(),
    "ocr_md_chars": len(ocr.decode()),
    "page_count": 1,
}), encoding="utf-8")
(root / "master.json").write_text(json.dumps({"entries": [{
    "cardCompany": "wooricard",
    "doc_version_id": "wooricard:100001:product_description:2025-01-01:v1",
    "productCode": "100001",
    "productName": "레거시 이전 시험 카드",
    "docType": "product_description",
    "beginDt": "2025-01-01",
    "gdccVer": "1",
    "fileNm": "card.pdf",
    "sourceUrl": "https://example.invalid/card.pdf",
    "sourcePostId": "fixture-one",
    "pdf_sha256": hashlib.sha256(pdf).hexdigest(),
    "ocr_remote_rel": "ocr/ocr.md",
    "metadata_remote_rel": "ocr/metadata.json",
    "pages": 1,
    "ocr_chars": len(ocr.decode()),
    "completed_at": "2026-01-01T00:00:00Z",
}]}), encoding="utf-8")
PY

source_before=$(find "$source_root" -mindepth 1 -printf '%P|%m|%s|%T@\n' | LC_ALL=C sort | sha256sum)
common_env="CARDRAG_LEGACY_CLI=$cardrag CARDRAG_RUNTIME_UID=$(id -u) CARDRAG_RUNTIME_GID=$(id -g) CARDRAG_MINIMUM_FREE_GIB=0 CARDRAG_MINIMUM_FREE_PERCENT=0 CARDRAG_MAXIMUM_USED_PERCENT=100 CARDRAG_WARNING_USED_PERCENT=99"

# shellcheck disable=SC2086
env $common_env "$transfer" prepare-install \
    --source "$source_root" --manifest "$source_root/master.json" \
    --output "$bundle_root" --import-root "$import_root" \
    >"$temporary_root/prepare-install.log" 2>"$temporary_root/prepare-install.err"
source_after=$(find "$source_root" -mindepth 1 -printf '%P|%m|%s|%T@\n' | LC_ALL=C sort | sha256sum)
test "$source_before" = "$source_after"

bundle=$(find "$import_root" -mindepth 1 -maxdepth 1 -type d -name 'bundle-*' -print -quit)
test -n "$bundle"
bundle_id=${bundle##*/}
test -f "$bundle/READY"
test "$(stat -c '%a' "$bundle")" = 550
test "$(stat -c '%a' "$bundle/READY")" = 440
"$cardrag" legacy verify --bundle "$bundle" >/dev/null
grep -q "transfer_status=installed bundle_id=$bundle_id" "$temporary_root/prepare-install.log"
grep -q '^COMPOSE_PROFILES=legacy-import$' "$temporary_root/prepare-install.log"
grep -q "^CARDRAG_LEGACY_BUNDLE_NAME=$bundle_id$" "$temporary_root/prepare-install.log"
if grep -Fq "$source_root" "$temporary_root/prepare-install.log" \
   "$temporary_root/prepare-install.err"; then
    echo "legacy preparation output exposed the absolute source path" >&2
    exit 1
fi
if grep -Eqi '(password|secret|token|api_key)=' "$temporary_root/prepare-install.log"; then
    echo "Portainer environment output leaked a secret-shaped value" >&2
    exit 1
fi

# Reinstalling the same content is idempotent and does not replace the final.
before_inode=$(stat -c '%i' "$bundle")
# shellcheck disable=SC2086
env $common_env "$transfer" install --bundle "$bundle_root/$bundle_id" \
    --import-root "$import_root" >"$temporary_root/idempotent.log"
test "$(stat -c '%i' "$bundle")" = "$before_inode"
grep -q 'transfer_status=already-installed' "$temporary_root/idempotent.log"

# A dry-run performs verification and admission but writes nothing to its target.
dry_import=$temporary_root/dry-imports
mkdir "$dry_import"
chmod 0750 "$dry_import"
# shellcheck disable=SC2086
env $common_env "$transfer" install --bundle "$bundle_root/$bundle_id" \
    --import-root "$dry_import" --dry-run >"$temporary_root/dry-run.log"
test -z "$(find "$dry_import" -mindepth 1 -print -quit)"
grep -q 'transfer_status=dry-run' "$temporary_root/dry-run.log"
grep -q '^COMPOSE_PROFILES=$' "$temporary_root/dry-run.log"
grep -q '^CARDRAG_LEGACY_IMPORT_ENABLED=false$' "$temporary_root/dry-run.log"
if grep -q '^COMPOSE_PROFILES=legacy-import$\|^CARDRAG_LEGACY_IMPORT_ENABLED=true$' \
   "$temporary_root/dry-run.log"; then
    echo "legacy transfer dry-run emitted an enabled mutation profile" >&2
    exit 1
fi

dry_prepare_output=$temporary_root/dry-prepare-output
# shellcheck disable=SC2086
env $common_env "$transfer" prepare-install --source "$source_root" \
    --manifest "$source_root/master.json" --output "$dry_prepare_output" \
    --import-root "$dry_import" --dry-run >"$temporary_root/dry-prepare-install.log"
test ! -e "$dry_prepare_output"
grep -q '^COMPOSE_PROFILES=$' "$temporary_root/dry-prepare-install.log"
if grep -q '^COMPOSE_PROFILES=legacy-import$\|^CARDRAG_LEGACY_IMPORT_ENABLED=true$' \
   "$temporary_root/dry-prepare-install.log"; then
    echo "legacy prepare-install dry-run emitted an enabled mutation profile" >&2
    exit 1
fi

# Bundle and import roots must be disjoint in both directions, including equal.
for disjoint_root in "$bundle_root/$bundle_id" "$bundle_root" "$bundle_root/$bundle_id/objects"; do
    # shellcheck disable=SC2086
    if env $common_env "$transfer" install --bundle "$bundle_root/$bundle_id" \
        --import-root "$disjoint_root" --dry-run >/dev/null 2>&1; then
        echo "legacy transfer accepted overlapping bundle/import roots: $disjoint_root" >&2
        exit 1
    fi
done

# Every mkdir->marker crash window is retryable only through the external exact
# owner proof written before the directory is created.
mkdir_fake_bin=$temporary_root/mkdir-fake-bin
mkdir "$mkdir_fake_bin"
cat >"$mkdir_fake_bin/mkdir" <<'SH'
#!/bin/sh
target=
for argument do
    target=$argument
done
/bin/mkdir "$@" || exit $?
if [ "$target" = "${CARDRAG_TEST_MKDIR_CRASH_TARGET:-}" ]; then
    exit 91
fi
SH
chmod 0755 "$mkdir_fake_bin/mkdir"

crash_output=$temporary_root/crash-prepared
owner_count_before=$(find "$temporary_root" -maxdepth 1 -type f \
    -name '.cardrag-legacy-prepare-owner-*' | wc -l)
# shellcheck disable=SC2086
if env $common_env PATH="$mkdir_fake_bin:$PATH" \
    CARDRAG_TEST_MKDIR_CRASH_TARGET="$crash_output" \
    "$transfer" prepare --source "$source_root" --manifest "$source_root/master.json" \
    --output "$crash_output" >/dev/null 2>&1; then
    echo "prepare mkdir crash fixture unexpectedly succeeded" >&2
    exit 1
fi
test -d "$crash_output"
test -z "$(find "$crash_output" -mindepth 1 -print -quit)"
owner_count_after=$(find "$temporary_root" -maxdepth 1 -type f \
    -name '.cardrag-legacy-prepare-owner-*' | wc -l)
test "$owner_count_after" -eq $((owner_count_before + 1))
find "$temporary_root" -maxdepth 1 -type f -name '.cardrag-legacy-prepare-owner-*' \
    -exec sh -c 'test "$(stat -c %a "$1")" = 400' sh {} \;
# shellcheck disable=SC2086
env $common_env "$transfer" prepare --source "$source_root" \
    --manifest "$source_root/master.json" --output "$crash_output" >/dev/null
test -f "$crash_output/$bundle_id/READY"

incoming_crash_import=$temporary_root/incoming-crash-imports
mkdir "$incoming_crash_import"
chmod 0750 "$incoming_crash_import"
# shellcheck disable=SC2086
if env $common_env PATH="$mkdir_fake_bin:$PATH" \
    CARDRAG_TEST_MKDIR_CRASH_TARGET="$incoming_crash_import/.incoming" \
    "$transfer" install --bundle "$bundle_root/$bundle_id" \
    --import-root "$incoming_crash_import" >/dev/null 2>&1; then
    echo "incoming mkdir crash fixture unexpectedly succeeded" >&2
    exit 1
fi
test -d "$incoming_crash_import/.incoming"
test ! -e "$incoming_crash_import/.incoming/.cardrag-legacy-transfer-v1"
test -f "$incoming_crash_import/.cardrag-legacy-incoming-owner"
test "$(stat -c '%a' "$incoming_crash_import/.cardrag-legacy-incoming-owner")" = 400
# shellcheck disable=SC2086
env $common_env "$transfer" install --bundle "$bundle_root/$bundle_id" \
    --import-root "$incoming_crash_import" >/dev/null
test -f "$incoming_crash_import/$bundle_id/READY"

stage_crash_import=$temporary_root/stage-crash-imports
mkdir "$stage_crash_import"
chmod 0750 "$stage_crash_import"
# shellcheck disable=SC2086
if env $common_env PATH="$mkdir_fake_bin:$PATH" \
    CARDRAG_TEST_MKDIR_CRASH_TARGET="$stage_crash_import/.incoming/$bundle_id" \
    "$transfer" install --bundle "$bundle_root/$bundle_id" \
    --import-root "$stage_crash_import" >/dev/null 2>&1; then
    echo "stage mkdir crash fixture unexpectedly succeeded" >&2
    exit 1
fi
test -d "$stage_crash_import/.incoming/$bundle_id"
test -f "$stage_crash_import/.incoming/$bundle_id.owner"
test "$(stat -c '%a' "$stage_crash_import/.incoming/$bundle_id.owner")" = 400
test -z "$(find "$stage_crash_import/.incoming/$bundle_id" -mindepth 1 -print -quit)"
# shellcheck disable=SC2086
env $common_env "$transfer" install --bundle "$bundle_root/$bundle_id" \
    --import-root "$stage_crash_import" >/dev/null
test -f "$stage_crash_import/$bundle_id/READY"
test ! -e "$stage_crash_import/.incoming/$bundle_id.owner"

# A failed copy never exposes a final. The next run recognizes and replaces
# only marker-owned partial staging before completing the atomic publish.
partial_import=$temporary_root/partial-imports
fake_bin=$temporary_root/fake-bin
mkdir "$partial_import" "$fake_bin"
chmod 0750 "$partial_import"
cat >"$fake_bin/cp" <<'SH'
#!/bin/sh
for argument do
    case "$argument" in
        */records) exit 91 ;;
    esac
done
exec /bin/cp "$@"
SH
chmod 0755 "$fake_bin/cp"
# shellcheck disable=SC2086
if env $common_env PATH="$fake_bin:$PATH" "$transfer" install \
    --bundle "$bundle_root/$bundle_id" --import-root "$partial_import" >/dev/null 2>&1; then
    echo "legacy transfer unexpectedly accepted an interrupted copy" >&2
    exit 1
fi
test ! -e "$partial_import/$bundle_id"
test -d "$partial_import/.incoming/$bundle_id"
test ! -e "$partial_import/.incoming/$bundle_id/READY"
# shellcheck disable=SC2086
env $common_env "$transfer" install --bundle "$bundle_root/$bundle_id" \
    --import-root "$partial_import" >/dev/null
test -f "$partial_import/$bundle_id/READY"
test ! -e "$partial_import/.incoming/$bundle_id"

# A corrupt existing target is rejected without overwriting its content.
collision_import=$temporary_root/collision-imports
mkdir "$collision_import"
chmod 0750 "$collision_import"
# shellcheck disable=SC2086
env $common_env "$transfer" install --bundle "$bundle_root/$bundle_id" \
    --import-root "$collision_import" >/dev/null
collision_ready=$collision_import/$bundle_id/READY
if [ ! -f "$collision_ready" ]; then
    echo "collision fixture installation did not publish its final bundle" >&2
    find "$collision_import" -mindepth 1 -maxdepth 3 -print >&2
    exit 1
fi
chmod 0750 "$collision_import/$bundle_id"
chmod 0640 "$collision_ready"
printf '%s\n' 'different-content' >"$collision_ready"
# shellcheck disable=SC2086
if env $common_env "$transfer" install --bundle "$bundle_root/$bundle_id" \
    --import-root "$collision_import" >/dev/null 2>&1; then
    echo "legacy transfer overwrote a corrupt existing bundle" >&2
    exit 1
fi
test "$(cat "$collision_ready")" = different-content

# A pre-existing foreign output directory is never chmodded or populated.
foreign_output=$temporary_root/foreign-output
mkdir "$foreign_output"
chmod 0710 "$foreign_output"
printf '%s\n' keep >"$foreign_output/foreign.txt"
# shellcheck disable=SC2086
if env $common_env "$transfer" prepare --source "$source_root" \
    --manifest "$source_root/master.json" --output "$foreign_output" >/dev/null 2>&1; then
    echo "legacy transfer accepted an unowned existing output directory" >&2
    exit 1
fi
test "$(stat -c '%a' "$foreign_output")" = 710
test "$(cat "$foreign_output/foreign.txt")" = keep

# Source links, special nodes, and lexical traversal paths fail before output.
ln -s /etc/passwd "$source_root/unsafe-link"
# shellcheck disable=SC2086
if env $common_env "$transfer" prepare --source "$source_root" \
    --manifest "$source_root/master.json" --output "$temporary_root/unsafe-output" --dry-run >/dev/null 2>&1; then
    echo "legacy transfer accepted a source symlink" >&2
    exit 1
fi
rm "$source_root/unsafe-link"
mkfifo "$source_root/unsafe-fifo"
# shellcheck disable=SC2086
if env $common_env "$transfer" prepare --source "$source_root" \
    --manifest "$source_root/master.json" --output "$temporary_root/unsafe-output" --dry-run >/dev/null 2>&1; then
    echo "legacy transfer accepted a source special file" >&2
    exit 1
fi
rm "$source_root/unsafe-fifo"
# shellcheck disable=SC2086
if env $common_env "$transfer" install --bundle "$bundle_root/$bundle_id" \
    --import-root "$temporary_root/path/../escape" --dry-run >/dev/null 2>&1; then
    echo "legacy transfer accepted a traversal path" >&2
    exit 1
fi

# Status and Portainer import/resume/status/disable values are copy-paste safe.
# shellcheck disable=SC2086
env $common_env "$transfer" transfer-status --bundle "$bundle_id" --import-root "$import_root" \
    | grep -q 'transfer_status=ready'
import_id=123e4567-e89b-42d3-a456-426614174000
"$transfer" portainer-env resume --bundle "$bundle" --import-id "$import_id" \
    >"$temporary_root/resume.env"
grep -q '^CARDRAG_LEGACY_OPERATION=resume$' "$temporary_root/resume.env"
grep -q "^CARDRAG_LEGACY_IMPORT_ID=$import_id$" "$temporary_root/resume.env"
"$transfer" portainer-env status --import-id "$import_id" >"$temporary_root/status.env"
grep -q '^CARDRAG_ADMIN_OPERATION=legacy-status$' "$temporary_root/status.env"
"$transfer" portainer-env finalize --import-id "$import_id" >"$temporary_root/finalize.env"
grep -q '^CARDRAG_ADMIN_OPERATION=legacy-finalize$' "$temporary_root/finalize.env"
grep -q 'portainer-env disable' "$temporary_root/finalize.env"
"$transfer" portainer-env disable >"$temporary_root/disable.env"
grep -q '^COMPOSE_PROFILES=$' "$temporary_root/disable.env"
grep -q '^CARDRAG_LEGACY_IMPORT_ENABLED=false$' "$temporary_root/disable.env"

non_gnu_bin=$temporary_root/non-gnu-bin
mkdir "$non_gnu_bin"
cat >"$non_gnu_bin/find" <<'SH'
#!/bin/sh
if [ "${1:-}" = --version ]; then
    echo 'BusyBox find'
    exit 0
fi
exec /usr/bin/find "$@"
SH
chmod 0755 "$non_gnu_bin/find"
if PATH="$non_gnu_bin:$PATH" "$transfer" portainer-env disable >/dev/null 2>&1; then
    echo "legacy transfer did not reject a non-GNU find implementation upfront" >&2
    exit 1
fi

echo "legacy transfer tests passed"
