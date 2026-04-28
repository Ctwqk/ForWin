# Design Documents

Design documents are the stable input for development and pre-PR review.
Every non-trivial change should add one design document on the same branch as
the implementation before a PR is opened.

## Naming

Use this path format:

```text
docs/designs/YYYY-MM-DD-short-feature-name.md
```

Example:

```text
docs/designs/2026-04-28-pr-automation.md
```

## Required Sections

Each design document must include these headings:

```text
## Goal
## Scope
## Design
## Risk
## Verification
## Rollback
```

The production pre-PR evaluator checks for these headings before it creates or
updates a PR.

## Workflow

1. Copy `docs/designs/TEMPLATE.md` to a dated feature file.
2. Commit the design document on the development branch.
3. Implement the change on the same branch.
4. Push the branch.
5. Run `scripts/pre_pr_eval.sh` from production to evaluate the branch and
   create or update a draft PR.
