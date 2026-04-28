#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

BASE_BRANCH="master"
HEAD_BRANCH="codex/dev"
DESIGN_DOC=""
CREATE_PR=false
DRAFT_PR=true
PR_TITLE=""
WORKTREE_DIR=""
KEEP_WORKTREE=false
SKIP_TESTS=false
ALLOW_EMPTY_DIFF=false
REMOTE="origin"
REPORT_FILE="${FORWIN_PR_EVAL_REPORT:-}"
PYTHON_BIN="${FORWIN_PR_EVAL_PYTHON:-}"
TEST_CMD="${FORWIN_PR_EVAL_TEST_CMD:-}"

usage() {
  cat <<'EOF'
Usage:
  scripts/pre_pr_eval.sh [options]

Options:
  --base <branch>        Base branch on origin. Default: master
  --head <branch>        Head branch on origin. Default: codex/dev
  --design <path>        Repo-relative design doc path
  --create-pr            Create a draft PR, or update the existing PR body
  --ready-pr             Create a non-draft PR when used with --create-pr
  --title <title>        PR title. Defaults to the design doc title
  --worktree-dir <path>  Use a specific temporary worktree path
  --keep-worktree        Leave the evaluation worktree in place
  --skip-tests           Skip the pytest command
  --allow-empty-diff     Allow base and head to have no diff. Never creates PRs
  --remote <name>        Git remote name. Default: origin
  -h, --help             Show this help

Environment:
  FORWIN_PR_EVAL_PYTHON      Python executable. Default: .venv/bin/python, then python3
  FORWIN_PR_EVAL_TEST_CMD    Test command. Default: python -m pytest tests/test_codex_operator_ready.py -q
  FORWIN_PR_EVAL_REPORT      Report output path
EOF
}

die() {
  echo "[FAIL] $*" >&2
  exit 1
}

note() {
  echo "[INFO] $*" >&2
}

ok() {
  echo "[OK] $*" >&2
}

normalize_branch() {
  local branch="$1"
  branch="${branch#refs/heads/}"
  branch="${branch#origin/}"
  printf '%s\n' "$branch"
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "missing required command: $1"
}

run_in_worktree() {
  local label="$1"
  shift

  {
    echo "## $label"
    printf '$'
    printf ' %q' "$@"
    echo
  } >>"$LOG_FILE"

  if (
    cd "$WORKTREE_DIR"
    PYTHONPATH="$WORKTREE_DIR${PYTHONPATH:+:$PYTHONPATH}" "$@"
  ) >>"$LOG_FILE" 2>&1; then
    ok "$label"
    CHECK_LINES+=("- [x] $label")
    return 0
  fi

  echo >&2
  echo "[FAIL] $label" >&2
  tail -n 80 "$LOG_FILE" >&2 || true
  exit 1
}

run_shell_in_worktree() {
  local label="$1"
  local command_text="$2"

  {
    echo "## $label"
    echo "$ $command_text"
  } >>"$LOG_FILE"

  if (
    cd "$WORKTREE_DIR"
    PYTHONPATH="$WORKTREE_DIR${PYTHONPATH:+:$PYTHONPATH}" bash -lc "$command_text"
  ) >>"$LOG_FILE" 2>&1; then
    ok "$label"
    CHECK_LINES+=("- [x] $label")
    return 0
  fi

  echo >&2
  echo "[FAIL] $label" >&2
  tail -n 80 "$LOG_FILE" >&2 || true
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --base)
      BASE_BRANCH="${2:?missing value for --base}"
      shift 2
      ;;
    --head)
      HEAD_BRANCH="${2:?missing value for --head}"
      shift 2
      ;;
    --design)
      DESIGN_DOC="${2:?missing value for --design}"
      shift 2
      ;;
    --create-pr)
      CREATE_PR=true
      shift
      ;;
    --ready-pr)
      DRAFT_PR=false
      shift
      ;;
    --title)
      PR_TITLE="${2:?missing value for --title}"
      shift 2
      ;;
    --worktree-dir)
      WORKTREE_DIR="${2:?missing value for --worktree-dir}"
      shift 2
      ;;
    --keep-worktree)
      KEEP_WORKTREE=true
      shift
      ;;
    --skip-tests)
      SKIP_TESTS=true
      shift
      ;;
    --allow-empty-diff)
      ALLOW_EMPTY_DIFF=true
      shift
      ;;
    --remote)
      REMOTE="${2:?missing value for --remote}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown argument: $1"
      ;;
  esac
done

cd "$REPO_ROOT"
BASE_BRANCH="$(normalize_branch "$BASE_BRANCH")"
HEAD_BRANCH="$(normalize_branch "$HEAD_BRANCH")"

[[ -n "$BASE_BRANCH" ]] || die "base branch is empty"
[[ -n "$HEAD_BRANCH" ]] || die "head branch is empty"
[[ "$DESIGN_DOC" != /* ]] || die "--design must be repo-relative"

require_command git
if [[ "$CREATE_PR" == true ]]; then
  require_command gh
  gh auth status >/dev/null 2>&1 || die "gh is not authenticated on this machine"
fi

if [[ -z "$PYTHON_BIN" ]]; then
  if [[ -x "$REPO_ROOT/.venv/bin/python" ]]; then
    PYTHON_BIN="$REPO_ROOT/.venv/bin/python"
  elif command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3)"
  else
    die "missing python; set FORWIN_PR_EVAL_PYTHON"
  fi
fi

if [[ -z "$TEST_CMD" ]]; then
  TEST_CMD="'$PYTHON_BIN' -m pytest tests/test_codex_operator_ready.py -q"
fi

if [[ -z "$REPORT_FILE" ]]; then
  REPORT_FILE="$(mktemp -t forwin-pr-eval-report.XXXXXX.md)"
fi
LOG_FILE="${REPORT_FILE%.md}.log"
: >"$LOG_FILE"
CHECK_LINES=()

note "fetching $REMOTE/$BASE_BRANCH and $REMOTE/$HEAD_BRANCH"
git fetch --prune "$REMOTE" "$BASE_BRANCH" "$HEAD_BRANCH" >/dev/null

BASE_REF="$REMOTE/$BASE_BRANCH"
HEAD_REF="$REMOTE/$HEAD_BRANCH"
git rev-parse --verify "$BASE_REF^{commit}" >/dev/null || die "cannot resolve $BASE_REF"
git rev-parse --verify "$HEAD_REF^{commit}" >/dev/null || die "cannot resolve $HEAD_REF"

if git diff --quiet "$BASE_REF...$HEAD_REF"; then
  if [[ "$CREATE_PR" == true ]]; then
    die "refusing to create a PR because $HEAD_REF has no diff against $BASE_REF"
  fi
  [[ "$ALLOW_EMPTY_DIFF" == true ]] || die "$HEAD_REF has no diff against $BASE_REF"
  note "$HEAD_REF has no diff against $BASE_REF; continuing because --allow-empty-diff was set"
else
  ok "$HEAD_REF has changes against $BASE_REF"
  CHECK_LINES+=("- [x] Head branch has changes against base")
fi

CREATED_WORKTREE=false
if [[ -z "$WORKTREE_DIR" ]]; then
  WORKTREE_DIR="$(mktemp -d -t forwin-pr-eval-worktree.XXXXXX)"
  rmdir "$WORKTREE_DIR"
  CREATED_WORKTREE=true
fi

cleanup() {
  if [[ "$KEEP_WORKTREE" == true ]]; then
    return 0
  fi
  if [[ -n "${WORKTREE_DIR:-}" ]]; then
    git -C "$REPO_ROOT" worktree remove --force "$WORKTREE_DIR" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

if [[ "$CREATED_WORKTREE" == false && -e "$WORKTREE_DIR" ]]; then
  die "worktree path already exists: $WORKTREE_DIR"
fi

git worktree add --detach --quiet "$WORKTREE_DIR" "$HEAD_REF"
ok "created evaluation worktree: $WORKTREE_DIR"

if [[ -z "$DESIGN_DOC" ]]; then
  mapfile -t changed_designs < <(
    git -C "$WORKTREE_DIR" diff --name-only "$BASE_REF...HEAD" -- 'docs/designs/*.md' \
      | grep -Ev '^docs/designs/(README|TEMPLATE)\.md$' || true
  )
  case "${#changed_designs[@]}" in
    0)
      die "no changed design doc found under docs/designs; pass --design"
      ;;
    1)
      DESIGN_DOC="${changed_designs[0]}"
      ;;
    *)
      printf '%s\n' "${changed_designs[@]}" >&2
      die "multiple design docs changed; pass --design"
      ;;
  esac
fi

DESIGN_PATH="$WORKTREE_DIR/$DESIGN_DOC"
[[ -f "$DESIGN_PATH" ]] || die "design doc does not exist on $HEAD_REF: $DESIGN_DOC"

for heading in "## Goal" "## Scope" "## Design" "## Risk" "## Verification" "## Rollback"; do
  grep -Eq "^${heading}([[:space:]]|$)" "$DESIGN_PATH" || die "design doc is missing required heading: $heading"
done
ok "design doc validated: $DESIGN_DOC"
CHECK_LINES+=("- [x] Design doc exists and has required sections: \`$DESIGN_DOC\`")

bad_secret_paths=()
while IFS= read -r path; do
  case "$path" in
    .env|.env.*|*/.env|*/.env.*)
      case "$path" in
        .env.example|*/.env.example) ;;
        *) bad_secret_paths+=("$path") ;;
      esac
      ;;
    *.pem|*.key|*.p12|*.pfx)
      bad_secret_paths+=("$path")
      ;;
  esac
done < <(git -C "$WORKTREE_DIR" diff --name-only "$BASE_REF...HEAD")

if [[ "${#bad_secret_paths[@]}" -gt 0 ]]; then
  printf '%s\n' "${bad_secret_paths[@]}" >&2
  die "diff includes likely secret files"
fi
ok "no likely secret files in diff"
CHECK_LINES+=("- [x] No likely secret files in diff")

if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  compose_config_args=(docker compose)
  if [[ -f "$REPO_ROOT/.env" ]]; then
    compose_config_args+=(--env-file "$REPO_ROOT/.env")
  fi
  compose_config_args+=(config --quiet)
  run_in_worktree "docker compose config" "${compose_config_args[@]}"
else
  note "docker compose is unavailable; skipping compose config check"
  CHECK_LINES+=("- [ ] Docker compose config skipped; docker compose unavailable")
fi

run_in_worktree "operator readiness" "$PYTHON_BIN" scripts/check_codex_operator_ready.py

if [[ "$SKIP_TESTS" == false ]]; then
  run_shell_in_worktree "pytest" "$TEST_CMD"
else
  note "pytest skipped by --skip-tests"
  CHECK_LINES+=("- [ ] Pytest skipped by --skip-tests")
fi

DESIGN_TITLE="$(sed -n 's/^# //p' "$DESIGN_PATH" | head -n 1)"
if [[ -z "$PR_TITLE" ]]; then
  if [[ -n "$DESIGN_TITLE" ]]; then
    PR_TITLE="$DESIGN_TITLE"
  else
    PR_TITLE="Evaluate $HEAD_BRANCH"
  fi
fi

{
  echo "# Pre-PR Evaluation"
  echo
  echo "- Base: \`$BASE_BRANCH\`"
  echo "- Head: \`$HEAD_BRANCH\`"
  echo "- Design: \`$DESIGN_DOC\`"
  echo "- Evaluated from: \`$(hostname)\`"
  echo "- Evaluated at: \`$(date -u '+%Y-%m-%dT%H:%M:%SZ')\`"
  echo
  echo "## Checks"
  printf '%s\n' "${CHECK_LINES[@]}"
  echo
  echo "## Diff Stat"
  echo
  echo '```text'
  git -C "$WORKTREE_DIR" diff --stat "$BASE_REF...HEAD" || true
  echo '```'
  echo
  echo "## Commands"
  echo
  echo "- \`docker compose config --quiet\`"
  echo "- \`scripts/check_codex_operator_ready.py\`"
  if [[ "$SKIP_TESTS" == false ]]; then
    echo "- \`$TEST_CMD\`"
  fi
} >"$REPORT_FILE"

ok "wrote evaluation report: $REPORT_FILE"

if [[ "$CREATE_PR" == true ]]; then
  PR_URL="$(
    gh pr list \
      --head "$HEAD_BRANCH" \
      --base "$BASE_BRANCH" \
      --state open \
      --json url \
      --jq '.[0].url // ""' 2>/dev/null || true
  )"
  if [[ -n "$PR_URL" ]]; then
    gh pr edit "$PR_URL" --title "$PR_TITLE" --body-file "$REPORT_FILE" >/dev/null
    ok "updated PR: $PR_URL"
  else
    pr_args=(pr create --base "$BASE_BRANCH" --head "$HEAD_BRANCH" --title "$PR_TITLE" --body-file "$REPORT_FILE")
    if [[ "$DRAFT_PR" == true ]]; then
      pr_args+=(--draft)
    fi
    PR_URL="$(gh "${pr_args[@]}")"
    ok "created PR: $PR_URL"
  fi
fi

echo "$REPORT_FILE"
