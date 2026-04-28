# PR Automation

ForWin uses two machines for the normal development loop:

- Production: `10.0.0.150`, `/home/taiwei/ForWin`, branch `master`
- Development: `10.0.0.246` WSL, `/home/kikuhiko/ForWin`, development branches

Production should stay on `master`. It evaluates candidate branches in a
temporary git worktree, so pre-PR checks do not switch or dirty the running
production checkout.

## Development Flow

1. Discuss the design with GPT or a collaborator.
2. Create a design document from `docs/designs/TEMPLATE.md`.
3. Commit the design document on the development branch.
4. Implement the change on the same branch.
5. Push the branch to GitHub.
6. On production, run `scripts/pre_pr_eval.sh`.
7. Review and merge the PR.
8. On production, pull `master` and redeploy.

## Example

On the development machine:

```bash
cd /home/kikuhiko/ForWin
git checkout codex/dev
cp docs/designs/TEMPLATE.md docs/designs/2026-04-28-example-feature.md
git add docs/designs/2026-04-28-example-feature.md
git commit -m "docs: design example feature"
# implement the feature, commit it, then push
git push origin codex/dev
```

On the production machine:

```bash
cd /home/taiwei/ForWin
scripts/pre_pr_eval.sh \
  --base master \
  --head codex/dev \
  --design docs/designs/2026-04-28-example-feature.md \
  --create-pr
```

## What The Evaluator Checks

`scripts/pre_pr_eval.sh` checks that:

- the base and head branches exist on `origin`
- the head branch has changes compared with the base branch
- a design document exists under `docs/designs/`
- the design document includes the required sections
- likely secret files such as `.env` are not part of the diff
- `docker compose config --quiet` succeeds when Docker is available
- `scripts/check_codex_operator_ready.py` succeeds
- the configured pytest command succeeds unless `--skip-tests` is used

With `--create-pr`, the script creates a draft PR if none exists, or updates the
existing PR body if one already exists for the head branch.
