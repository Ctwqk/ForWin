# Canon Replay Operator Guide

## When To Use

Use canon replay after legacy canon migration, schema-version upgrades, LLM re-validation, or targeted re-audit of an accepted chapter. Do not use it to regenerate chapter prose, plans, drafts, world-model projections, Obsidian exports, or generation tasks.

## Recommended Workflow

1. Estimate cost.
2. Dry-run a narrow range.
3. Run diff mode against existing form-sourced rows.
4. Persist only after reviewing the dry-run and diff output.

## Examples

### Post-Migration Backfill

```bash
python3 scripts/canon_replay.py --project-id PROJECT_ID --from-chapter 1 --to-chapter 30 --estimate-only
python3 scripts/canon_replay.py --project-id PROJECT_ID --from-chapter 1 --to-chapter 30 --dry-run --cost-cap-usd 5
python3 scripts/canon_replay.py --project-id PROJECT_ID --from-chapter 1 --to-chapter 30 --diff-mode --cost-cap-usd 5
python3 scripts/canon_replay.py --project-id PROJECT_ID --from-chapter 1 --to-chapter 30 --persist --cost-cap-usd 5
```

### Schema Version Upgrade

```bash
python3 scripts/canon_replay.py --project-id PROJECT_ID --from-chapter 1 --to-chapter 60 --schema-version chapter_review_form.v2 --diff-mode --cost-cap-usd 10
```

### LLM Upgrade Re-Validation

```bash
python3 scripts/canon_replay.py --project-id PROJECT_ID --from-chapter 10 --to-chapter 12 --llm-profile env-deepseek --diff-mode --cost-cap-usd 2
```

### Targeted Re-Audit

```bash
python3 scripts/canon_replay.py --project-id PROJECT_ID --from-chapter 7 --to-chapter 7 --dry-run --cost-cap-usd 1
```

## Resume

State files live under `data/artifacts/canon_replay/<project_id>/<from>-<to>.state.json`.

```bash
python3 scripts/canon_replay.py --project-id PROJECT_ID --from-chapter 1 --to-chapter 30 --resume --persist --cost-cap-usd 5
```

## Troubleshooting

- `missing_accepted_draft`: regenerate or accept the chapter through the normal writer workflow first.
- `missing_cost_cap`: pass `--cost-cap-usd <N>` or `--no-cost-cap`.
- `state file already exists`: pass `--resume` to continue or `--force-restart` to start a new state.
- `cost_cap`: inspect the state file, raise the cap, then resume.
