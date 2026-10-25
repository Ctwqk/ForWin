#!/bin/bash
set -euo pipefail
umask 077

with_data=0
while (($#)); do
    case "$1" in
        --with-data) with_data=1; shift ;;
        -h|--help) echo "Usage: $0 [--with-data]"; exit 0 ;;
        *) echo "Usage: $0 [--with-data]" >&2; exit 1 ;;
    esac
done

backup_dir="$HOME/backups/forwin-magi3"
mkdir -p "$backup_dir"
timestamp=$(date -u +%Y%m%dT%H%M%SZ)
dump="$backup_dir/forwin-$timestamp.dump"
data_archive="$backup_dir/forwin-data-$timestamp.tgz"
partial=
cleanup() {
    if [[ -n "$partial" ]]; then rm -f "$partial"; fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Stage each stream privately; a failed SSH or dump never becomes a backup.
partial=$(mktemp "$dump.partial.XXXXXX")
ssh magi3 'export PATH="/opt/homebrew/bin:$PATH"; exec docker compose -p forwin -f ~/forwin/src/deploy/magi3/docker-compose.yml --env-file ~/forwin/src/deploy/magi3/.env exec -T postgres pg_dump -U forwin -Fc forwin' > "$partial"
if (( $(wc -c < "$partial") < 1024 )); then
    echo "PostgreSQL dump is smaller than 1024 bytes; backup rejected." >&2
    exit 1
fi
mv "$partial" "$dump"
partial=

# Sunday is evaluated in magi1's local timezone, matching the launchd schedule.
if ((with_data)) || [[ "$(date +%u)" == 7 ]]; then
    partial=$(mktemp "$data_archive.partial.XXXXXX")
    ssh magi3 'export PATH="/opt/homebrew/bin:$PATH"; exec docker run --rm -v forwin_forwin-data:/data alpine tar czf - -C /data .' > "$partial"
    mv "$partial" "$data_archive"
    partial=
fi

# Only remove this job's completed backups, older than 14 * 24 hours.
find "$backup_dir" -type f \( -name 'forwin-*.dump' -o -name 'forwin-data-*.tgz' \) \
    -mmin +20160 -delete
printf 'Backup completed: %s\n' "$dump"
