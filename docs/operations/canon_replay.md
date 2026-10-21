# Canon Replay Operator Guide

## When To Use

Use canon replay for LLM re-validation or targeted re-audit of accepted chapters under the current schema. Review-form rows are reconstructed from BookState; answers produce quality signals only and do not rebuild canon ledger rows. Do not use it to regenerate chapter prose, plans, drafts, world-model projections, Obsidian exports, or generation tasks.

## Recommended Workflow

1. Estimate cost.
2. Dry-run a narrow range.
3. Review the candidate signals and errors in the JSON-line output.
4. Persist the replayed form signals only after reviewing that output. Dry-run writes no database rows.

## Examples

### Current-Schema Re-Audit

```bash
python3 scripts/canon_replay.py --project-id PROJECT_ID --from-chapter 1 --to-chapter 30 --estimate-only
python3 scripts/canon_replay.py --project-id PROJECT_ID --from-chapter 1 --to-chapter 30 --dry-run --cost-cap-usd 5
python3 scripts/canon_replay.py --project-id PROJECT_ID --from-chapter 1 --to-chapter 30 --persist --force-restart --cost-cap-usd 5
```

### Schema Version Check

```bash
python3 scripts/canon_replay.py --project-id PROJECT_ID --from-chapter 1 --to-chapter 60 --schema-version chapter_review_form.v2 --dry-run --cost-cap-usd 10
```

### LLM Upgrade Re-Validation

```bash
python3 scripts/canon_replay.py --project-id PROJECT_ID --from-chapter 10 --to-chapter 12 --llm-profile env-deepseek --dry-run --cost-cap-usd 2
```

### Targeted Re-Audit

```bash
python3 scripts/canon_replay.py --project-id PROJECT_ID --from-chapter 7 --to-chapter 7 --dry-run --cost-cap-usd 1
```

`--schema-version` reports a mismatch warning; the tool always uses its current form schema. It does not migrate database schemas or restore removed countdown / character-state ledgers.

## Resume

State files live under `<artifact_root>/canon_replay/<project_id>/<from>-<to>.state.json` (normally `data/artifacts`). When switching from a reviewed dry-run to persist, pass `--force-restart` to start a new replay state; `--resume` continues an interrupted run.

```bash
python3 scripts/canon_replay.py --project-id PROJECT_ID --from-chapter 1 --to-chapter 30 --resume --persist --cost-cap-usd 5
```

## Troubleshooting

- `missing_accepted_draft`: regenerate or accept the chapter through the normal writer workflow first.
- `missing_cost_cap`: pass `--cost-cap-usd <N>` or `--no-cost-cap`.
- `state file already exists`: pass `--resume` to continue or `--force-restart` to start a new state.
- `cost_cap`: inspect the state file, raise the cap, then resume.
