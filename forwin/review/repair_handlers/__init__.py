"""Metadata repair handlers used by the signal-kind repair router.

Only `subworld` and `active_rules` live here because they are metadata-only
repairs that must run without invoking the writer. `draft` and `chapter_plan`
reuse the existing writer/plan rewrite path in the orchestrator. `operator`
uses the structured operator report path. `band_plan` is intentionally not a
runtime metadata handler; band contracts are selected during band-plan
generation/regeneration.
"""
