#!/bin/bash
set -euo pipefail

usage() {
    echo "Usage: $0 [--ref <sha>] [--data-stores-only]"
}

ref=origin/master
data_stores_only=0
while (($#)); do
    case "$1" in
        --ref)
            if (($# < 2)) || [[ ! "$2" =~ ^[0-9a-fA-F]{4,40}$ ]]; then
                echo "--ref requires a commit SHA." >&2
                exit 1
            fi
            ref=$2
            shift 2
            ;;
        --data-stores-only) data_stores_only=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 1 ;;
    esac
done

cd "$(dirname "${BASH_SOURCE[0]}")/.."
tree_status=$(git status --porcelain --untracked-files=no)
if [[ -n "$tree_status" ]]; then
    echo "Refusing to deploy a dirty tree; commit or stash changes first." >&2
    exit 1
fi
git fetch origin master
sha=$(git rev-parse --verify "${ref}^{commit}")
if ! git merge-base --is-ancestor "$sha" origin/master; then
    echo "Refusing to deploy a commit outside origin/master history." >&2
    exit 1
fi

# Push through SSH to magi3:~/forwin/repo.git; no GitHub credentials on magi3.
ssh magi3 'if [ ! -d ~/forwin/repo.git ]; then mkdir -p ~/forwin; git init --bare -q ~/forwin/repo.git; fi'
git push ssh://magi3/~/forwin/repo.git "$sha:refs/heads/deploy" --force
ssh magi3 bash -s -- "$sha" "$data_stores_only" <<'REMOTE'
set -euo pipefail
export PATH="/opt/homebrew/bin:$PATH"
sha=$1
data_stores_only=$2

if [[ ! -d "$HOME/forwin/src" ]]; then
    git clone --no-checkout "$HOME/forwin/repo.git" "$HOME/forwin/src"
fi
cd "$HOME/forwin/src"
git fetch origin deploy
git checkout --detach "$sha"
if [[ ! -f deploy/magi3/.env ]]; then
    echo "Create deploy/magi3/.env on magi3 before deploying." >&2
    exit 1
fi

compose() {
    docker compose -p forwin -f deploy/magi3/docker-compose.yml \
        --env-file deploy/magi3/.env "$@"
}
compose build
if ((data_stores_only)); then
    compose up -d postgres qdrant
    exit 0
fi
compose --profile tools run --rm -T migrate </dev/null
compose up -d --remove-orphans

deadline=$((SECONDS + 180))
health_ok() {
    local remaining=$((deadline - SECONDS))
    ((remaining > 0)) || return 1
    if ((remaining > 5)); then remaining=5; fi
    curl -fsS --max-time "$remaining" "http://127.0.0.1:$1/health" >/dev/null 2>&1
}
while ((SECONDS < deadline)); do
    if health_ok 8899 && health_ok 8896; then
        printf '%s\n' "$sha" > .deploy-commit
        printf 'Deployed %s on magi3.\n' "$sha"
        exit 0
    fi
    if ((SECONDS < deadline)); then sleep 1; fi
done
echo "App and MCP health checks did not both pass within 180 seconds." >&2
exit 1
REMOTE
