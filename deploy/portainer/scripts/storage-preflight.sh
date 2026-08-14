#!/bin/sh
set -eu

minimum_free_gib=${CARDRAG_MINIMUM_FREE_GIB:-50}
minimum_free_percent=${CARDRAG_MINIMUM_FREE_PERCENT:-20}
maximum_used_percent=${CARDRAG_MAXIMUM_USED_PERCENT:-85}
warning_used_percent=${CARDRAG_WARNING_USED_PERCENT:-70}
unset CARDRAG_MINIMUM_FREE_GIB CARDRAG_MINIMUM_FREE_PERCENT \
    CARDRAG_MAXIMUM_USED_PERCENT CARDRAG_WARNING_USED_PERCENT

for value in "$minimum_free_gib" "$minimum_free_percent" \
             "$maximum_used_percent" "$warning_used_percent"; do
    case "$value" in
        ''|*[!0-9]*)
            echo "storage thresholds must be non-negative integers" >&2
            exit 64
            ;;
    esac
done
if [ "$minimum_free_percent" -gt 100 ] || \
   [ "$maximum_used_percent" -gt 100 ] || \
   [ "$warning_used_percent" -gt 100 ]; then
    echo "storage percentages must not exceed 100" >&2
    exit 64
fi
if [ "$warning_used_percent" -ge "$maximum_used_percent" ]; then
    echo "storage warning threshold must precede the blocking threshold" >&2
    exit 64
fi

minimum_free_kib=$((minimum_free_gib * 1024 * 1024))
paths_file=/tmp/cardrag-storage-preflight.$$
trap 'rm -f "$paths_file"' EXIT HUP INT TERM
: >"$paths_file"

while [ "$#" -gt 0 ] && [ "$1" != -- ]; do
    path=$1
    shift
    if [ ! -d "$path" ] || [ -L "$path" ]; then
        echo "storage path must be an existing non-symlink directory: $path" >&2
        exit 78
    fi
    case "$path" in
        *'
'*)
            echo "storage path must not contain a newline" >&2
            exit 78
            ;;
    esac
    printf '%s\n' "$path" >>"$paths_file"
done
if [ "$#" -eq 0 ] || [ "$1" != -- ]; then
    echo "usage: storage-preflight.sh PATH... -- COMMAND [ARG...]" >&2
    exit 64
fi
shift
if [ ! -s "$paths_file" ] || [ "$#" -eq 0 ]; then
    echo "storage preflight requires at least one path and a command" >&2
    exit 64
fi

check_filesystems() {
    phase=$1
    checked_mounts=''
    while IFS= read -r path; do
        block_row=$(df -Pk "$path" | awk 'NR == 2 {print $2, $3, $4, $5, $6}')
        inode_row=$(df -Pi "$path" | awk 'NR == 2 {print $2, $3, $4, $5, $6}')
        block_total=$(printf '%s\n' "$block_row" | awk '{print $1}')
        block_used=$(printf '%s\n' "$block_row" | awk '{print $2}')
        block_available=$(printf '%s\n' "$block_row" | awk '{print $3}')
        block_used_label=$(printf '%s\n' "$block_row" | awk '{print $4}')
        mountpoint=$(printf '%s\n' "$block_row" | awk '{print $5}')
        case "$checked_mounts" in
            *"|$mountpoint|"*) continue ;;
        esac
        checked_mounts=$checked_mounts\|$mountpoint\|

        inode_total=$(printf '%s\n' "$inode_row" | awk '{print $1}')
        inode_used=$(printf '%s\n' "$inode_row" | awk '{print $2}')
        inode_available=$(printf '%s\n' "$inode_row" | awk '{print $3}')
        inode_used_label=$(printf '%s\n' "$inode_row" | awk '{print $4}')
        inode_mountpoint=$(printf '%s\n' "$inode_row" | awk '{print $5}')
        if [ "$inode_mountpoint" != "$mountpoint" ]; then
            echo "block and inode filesystem identity differs for $path" >&2
            return 78
        fi

        for number in "$block_total" "$block_used" "$block_available" \
                      "$inode_total" "$inode_used" "$inode_available"; do
            case "$number" in
                ''|*[!0-9]*)
                    echo "storage preflight received invalid filesystem counters" >&2
                    return 70
                    ;;
            esac
        done
        block_used_percent=${block_used_label%%%}
        inode_used_percent=${inode_used_label%%%}
        case "$block_used_percent:$inode_used_percent" in
            *[!0-9:]*|:*|*:)
                echo "storage preflight received invalid usage percentages" >&2
                return 70
                ;;
        esac
        if [ "$block_total" -eq 0 ] || [ "$inode_total" -eq 0 ]; then
            echo "storage preflight cannot measure filesystem capacity" >&2
            return 70
        fi
        block_free_percent=$((block_available * 100 / block_total))
        inode_free_percent=$((inode_available * 100 / inode_total))

        rejected=false
        if [ "$block_free_percent" -lt "$minimum_free_percent" ] || \
           [ "$inode_free_percent" -lt "$minimum_free_percent" ]; then
            rejected=true
        fi
        if [ "$phase" = preflight ] && { \
           [ "$block_available" -lt "$minimum_free_kib" ] || \
           [ "$block_used_percent" -ge "$maximum_used_percent" ] || \
           [ "$inode_used_percent" -ge "$maximum_used_percent" ]; \
        }; then
            rejected=true
        fi
        if [ "$rejected" = true ]; then
            echo "storage $phase rejected filesystem at $mountpoint" >&2
            echo "available_kib=$block_available block_free_percent=$block_free_percent inode_free_percent=$inode_free_percent" >&2
            return 75
        fi
        if [ "$block_used_percent" -ge "$warning_used_percent" ] || \
           [ "$inode_used_percent" -ge "$warning_used_percent" ]; then
            echo "storage warning: phase=$phase mount=$mountpoint block_used_percent=$block_used_percent inode_used_percent=$inode_used_percent" >&2
        fi
        echo "storage $phase passed: mount=$mountpoint available_kib=$block_available block_free_percent=$block_free_percent inode_free_percent=$inode_free_percent"
    done <"$paths_file"
}

check_filesystems preflight
set +e
"$@"
command_status=$?
set -e
set +e
check_filesystems postflight
postflight_status=$?
set -e
if [ "$postflight_status" -ne 0 ]; then
    echo "storage postflight failed after command status $command_status" >&2
    exit "$postflight_status"
fi
exit "$command_status"
