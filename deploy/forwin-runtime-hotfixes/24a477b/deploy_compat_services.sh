#!/usr/bin/env bash
# Deliberately narrow release helper for the approved compatibility image.
# It never invokes docker compose, stack deploy, or a service update outside
# the three recovery consumers listed below.
set -euo pipefail

readonly IMAGE='forwin-forwin:compat-24a477b'
readonly BUILD_DOCKER_CONTEXT="${FORWIN_BUILD_DOCKER_CONTEXT:-colima-swarmbridged}"
readonly SERVICE_DOCKER_CONTEXT="${FORWIN_SERVICE_DOCKER_CONTEXT:-swarm-manager-150}"
readonly LAYER_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly TARGET_NODE_HOSTNAME='colima-swarmbridged'
readonly -a TARGET_SERVICES=(
  forwin-app-swarm
  forwin-mcp-swarm
  forwin-generation-worker-swarm
)
readonly -a PROTECTED_SERVICES=(
  forwin-publisher-worker-swarm
  forwin-publisher-browser-swarm
  forwin-outbox-worker-swarm
)
readonly MAX_WAIT_SECONDS=300
readonly POLL_SECONDS=5

if [[ "${FORWIN_COMPAT_DEPLOY_APPROVED:-}" != '1' ]]; then
  printf '%s\n' \
    'Refusing deployment: set FORWIN_COMPAT_DEPLOY_APPROVED=1 after review approval.' >&2
  exit 64
fi

service_docker_cmd() {
  docker --context "$SERVICE_DOCKER_CONTEXT" "$@"
}

service_image() {
  service_docker_cmd service inspect --format '{{.Spec.TaskTemplate.ContainerSpec.Image}}' "$1"
}

service_replicas() {
  service_docker_cmd service inspect --format '{{if .Spec.Mode.Replicated}}{{.Spec.Mode.Replicated.Replicas}}{{else}}0{{end}}' "$1"
}

require_service() {
  service_docker_cmd service inspect "$1" >/dev/null
}

require_manager_context() {
  local control_available
  control_available="$(service_docker_cmd info --format '{{.Swarm.ControlAvailable}}')"
  if [[ "$control_available" != 'true' ]]; then
    printf 'service Docker context %s is not a Swarm manager\n' "$SERVICE_DOCKER_CONTEXT" >&2
    return 1
  fi
}

require_target_placement() {
  local service constraints normalized expected
  service="$1"
  expected="node.hostname==${TARGET_NODE_HOSTNAME}"
  constraints="$(service_docker_cmd service inspect --format '{{range .Spec.TaskTemplate.Placement.Constraints}}{{printf \"%s\\n\" .}}{{end}}' "$service")"
  normalized="$(printf '%s\n' "$constraints" | tr -d '[:space:]')"
  if ! printf '%s\n' "$normalized" | grep -Fxq "$expected"; then
    printf 'target %s is not constrained to %s\n' "$service" "$expected" >&2
    return 1
  fi
}

is_expected_image() {
  [[ "$1" == "$IMAGE" || "$1" == "$IMAGE@"* ]]
}

same_image_ref() {
  [[ "$1" == "$2" ]]
}

wait_for_convergence() {
  local service="$1"
  local expected running elapsed=0
  expected="$(service_replicas "$service")"
  if [[ ! "$expected" =~ ^[0-9]+$ || "$expected" -lt 1 ]]; then
    printf 'invalid expected replica count for %s: %s\n' "$service" "$expected" >&2
    return 1
  fi
  while (( elapsed <= MAX_WAIT_SECONDS )); do
    running="$(service_docker_cmd service ps --filter desired-state=running --format '{{.CurrentState}}' "$service" | awk '/^Running/{count++} END{print count+0}')"
    if [[ "$running" -eq "$expected" ]]; then
      return 0
    fi
    if service_docker_cmd service ps --no-trunc "$service" | grep -Eq 'Rejected|Failed'; then
      printf 'service %s has a failed task:\n' "$service" >&2
      service_docker_cmd service ps --no-trunc "$service" >&2
      return 1
    fi
    sleep "$POLL_SECONDS"
    ((elapsed += POLL_SECONDS))
  done
  printf 'service %s did not converge within %ss:\n' "$service" "$MAX_WAIT_SECONDS" >&2
  service_docker_cmd service ps --no-trunc "$service" >&2
  return 1
}

declare -a target_before=()
declare -a protected_before=()
declare -a attempted_indexes=()

rollback_attempted_targets() {
  local failed_status="$1"
  local index service actual rollback_failed=0
  trap - ERR
  if ((${#attempted_indexes[@]} == 0)); then
    exit "$failed_status"
  fi
  printf 'compat deployment failed; restoring attempted targets: %s\n' \
    "${attempted_indexes[*]}" >&2
  for index in "${attempted_indexes[@]}"; do
    service="${TARGET_SERVICES[$index]}"
    if ! service_docker_cmd service update --detach=false --resolve-image never --image "${target_before[$index]}" "$service"; then
      printf 'rollback update failed for %s\n' "$service" >&2
      rollback_failed=1
      continue
    fi
    if ! wait_for_convergence "$service"; then
      printf 'rollback convergence failed for %s\n' "$service" >&2
      rollback_failed=1
      continue
    fi
    actual="$(service_image "$service")"
    if ! same_image_ref "$actual" "${target_before[$index]}"; then
      printf 'rollback image mismatch for %s: expected %s, got %s\n' \
        "$service" "${target_before[$index]}" "$actual" >&2
      rollback_failed=1
    fi
  done
  if ((rollback_failed)); then
    printf 'one or more rollback checks failed; manual incident response is required\n' >&2
  fi
  exit "$failed_status"
}

on_error() {
  rollback_attempted_targets "$?"
}

trap on_error ERR

# Build and inspect the image only in the worker context. Service inspection
# and every mutation use the separate Swarm-manager context.
FORWIN_BUILD_DOCKER_CONTEXT="$BUILD_DOCKER_CONTEXT" "$LAYER_DIR/build_compat_image.sh"
require_manager_context
for index in "${!TARGET_SERVICES[@]}"; do
  service="${TARGET_SERVICES[$index]}"
  require_service "$service"
  require_target_placement "$service"
  target_before[$index]="$(service_image "$service")"
done
for index in "${!PROTECTED_SERVICES[@]}"; do
  service="${PROTECTED_SERVICES[$index]}"
  require_service "$service"
  protected_before[$index]="$(service_image "$service")"
done

for index in "${!TARGET_SERVICES[@]}"; do
  service="${TARGET_SERVICES[$index]}"
  # Include a target before attempting its update: Swarm may have accepted a
  # partial spec change even when the client reports an error.
  attempted_indexes+=("$index")
  service_docker_cmd service update --detach=false --resolve-image never --image "$IMAGE" "$service"
  actual="$(service_image "$service")"
  if ! is_expected_image "$actual"; then
    printf 'unexpected image for %s: %s\n' "$service" "$actual" >&2
    rollback_attempted_targets 1
  fi
  wait_for_convergence "$service"
done

for index in "${!PROTECTED_SERVICES[@]}"; do
  service="${PROTECTED_SERVICES[$index]}"
  actual="$(service_image "$service")"
  if [[ "$actual" != "${protected_before[$index]}" ]]; then
    printf 'protected service image changed for %s: %s -> %s\n' \
      "$service" "${protected_before[$index]}" "$actual" >&2
    rollback_attempted_targets 1
  fi
done

trap - ERR
printf 'compat deployment converged for: %s\n' "${TARGET_SERVICES[*]}"
printf 'publisher/outbox image references unchanged: %s\n' "${PROTECTED_SERVICES[*]}"
