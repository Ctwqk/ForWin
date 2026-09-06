#!/usr/bin/env bash
# Build this exact compatibility layer in the worker Docker context. This
# script does not inspect or update any Swarm service.
set -euo pipefail

readonly IMAGE='forwin-forwin:compat-24a477b'
readonly BUILD_DOCKER_CONTEXT="${FORWIN_BUILD_DOCKER_CONTEXT:-colima-swarmbridged}"
readonly LAYER_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

docker --context "$BUILD_DOCKER_CONTEXT" build --no-cache \
  -f "$LAYER_DIR/Dockerfile" \
  -t "$IMAGE" \
  "$LAYER_DIR"
docker --context "$BUILD_DOCKER_CONTEXT" image inspect "$IMAGE" >/dev/null
printf 'built %s in worker Docker context %s\n' "$IMAGE" "$BUILD_DOCKER_CONTEXT"
