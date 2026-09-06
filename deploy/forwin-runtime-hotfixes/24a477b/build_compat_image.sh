#!/usr/bin/env bash
# Build this exact compatibility layer in the same Docker context used by its
# narrowly scoped deployment helper. This script does not update any service.
set -euo pipefail

readonly IMAGE='forwin-forwin:compat-24a477b'
readonly DOCKER_CONTEXT="${FORWIN_DOCKER_CONTEXT:-colima-swarmbridged}"
readonly LAYER_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

docker --context "$DOCKER_CONTEXT" build --no-cache \
  -f "$LAYER_DIR/Dockerfile" \
  -t "$IMAGE" \
  "$LAYER_DIR"
docker --context "$DOCKER_CONTEXT" image inspect "$IMAGE" >/dev/null
printf 'built %s in Docker context %s\n' "$IMAGE" "$DOCKER_CONTEXT"
