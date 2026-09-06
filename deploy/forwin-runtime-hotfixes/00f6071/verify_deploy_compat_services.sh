#!/usr/bin/env bash
# Shell-level regression for the narrow deploy helper. It substitutes Docker
# with a disposable stateful stub and proves an image-ref mismatch restores all
# attempted target services before the helper returns failure.
set -euo pipefail

readonly LAYER_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
test_dir="$(mktemp -d /tmp/forwin-compat-deploy-test.XXXXXX)"
trap 'rm -rf "$test_dir"' EXIT
mkdir -p "$test_dir/bin" "$test_dir/state"

for service in \
  forwin-app-swarm \
  forwin-mcp-swarm \
  forwin-generation-worker-swarm \
  forwin-publisher-worker-swarm \
  forwin-publisher-browser-swarm \
  forwin-outbox-worker-swarm; do
  printf 'old-%s\n' "$service" >"$test_dir/state/$service"
done

cat >"$test_dir/bin/docker" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
context=''
if [[ "${1:-}" == '--context' ]]; then
  context="$2"
  shift 2
fi
case "${1:-}" in
  build)
    [[ "$context" == 'fake-worker' ]]
    exit 0
    ;;
  image)
    [[ "$context" == 'fake-worker' ]]
    [[ "${2:-}" == 'inspect' ]]
    exit 0
    ;;
  info)
    [[ "$context" == 'fake-manager' ]]
    [[ "${2:-}" == '--format' ]]
    printf 'true\n'
    exit 0
    ;;
  service)
    [[ "$context" == 'fake-manager' ]]
    subcommand="${2:-}"
    shift 2
    case "$subcommand" in
      inspect)
        if [[ "${1:-}" == '--format' ]]; then
          format="$2"
          service="$3"
          if [[ "$format" == *'Replicas'* ]]; then
            printf '1\n'
          elif [[ "$format" == *'Placement.Constraints'* ]]; then
            printf 'node.hostname==colima-swarmbridged\n'
          elif [[ "$format" == *'ContainerSpec.Image'* ]]; then
            image="$(cat "$FORWIN_FAKE_STATE/$service")"
            if [[ "$service" == 'forwin-mcp-swarm' && "$image" == 'forwin-forwin:compat-00f6071' ]]; then
              printf 'unexpected-mcp-image\n'
            else
              printf '%s\n' "$image"
            fi
          fi
        fi
        exit 0
        ;;
      update)
        shift 0
        image=''
        service=''
        while (($#)); do
          case "$1" in
            --detach=false|--no-resolve-image)
              shift
              ;;
            --image)
              image="$2"
              shift 2
              ;;
            *)
              service="$1"
              shift
              ;;
          esac
        done
        printf '%s:%s\n' "$service" "$image" >>"$FORWIN_FAKE_STATE/updates"
        printf '%s\n' "$image" >"$FORWIN_FAKE_STATE/$service"
        exit 0
        ;;
      ps)
        printf 'Running 1 second ago\n'
        exit 0
        ;;
    esac
    ;;
esac
printf 'unexpected fake docker invocation: %s\n' "$*" >&2
exit 97
EOF
chmod 755 "$test_dir/bin/docker"

set +e
PATH="$test_dir/bin:$PATH" \
FORWIN_FAKE_STATE="$test_dir/state" \
FORWIN_BUILD_DOCKER_CONTEXT='fake-worker' \
FORWIN_SERVICE_DOCKER_CONTEXT='fake-manager' \
FORWIN_COMPAT_DEPLOY_APPROVED=1 \
"$LAYER_DIR/deploy_compat_services.sh" >"$test_dir/output" 2>&1
status=$?
set -e

if [[ "$status" -eq 0 ]]; then
  printf 'expected deploy helper to fail on fake MCP image mismatch\n' >&2
  exit 1
fi
if [[ ! -f "$test_dir/state/updates" ]]; then
  cat "$test_dir/output" >&2
  printf 'fake mismatch did not reach a target update\n' >&2
  exit 1
fi
grep -Fx 'forwin-app-swarm:old-forwin-app-swarm' "$test_dir/state/updates" >/dev/null
grep -Fx 'forwin-mcp-swarm:old-forwin-mcp-swarm' "$test_dir/state/updates" >/dev/null
[[ "$(cat "$test_dir/state/forwin-app-swarm")" == 'old-forwin-app-swarm' ]]
[[ "$(cat "$test_dir/state/forwin-mcp-swarm")" == 'old-forwin-mcp-swarm' ]]
printf 'deploy_rollback_on_image_mismatch=ok\n'
