#!/bin/sh
# Listen on :18930 in the compose network and forward to 127.0.0.1:18930 on the VPS, where
# magi2's reverse tunnel ends. Idles until the target and the key are set up (README).
set -eu
dir=${PUBLISH_HUB_TUNNEL_DIR:-/run/publish-hub-tunnel}
target=${PUBLISH_HUB_TUNNEL_TARGET:-}
if [ -z "$target" ] || [ ! -r "$dir/id_ed25519" ] || [ ! -r "$dir/known_hosts" ]; then
    echo "publish-hub-tunnel: FORWIN_PUBLISH_HUB_TUNNEL_TARGET or $dir/{id_ed25519,known_hosts} missing; idle" >&2
    exec sleep 2147483647
fi
exec ssh -NT -F /dev/null -i "$dir/id_ed25519" \
    -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes \
    -o UserKnownHostsFile="$dir/known_hosts" -o HostKeyAlgorithms=ssh-ed25519 -o UpdateHostKeys=no \
    -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ConnectTimeout=15 \
    -L 0.0.0.0:18930:127.0.0.1:18930 "$target"
