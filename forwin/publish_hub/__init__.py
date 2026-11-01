"""ForWin's side of the publish-hub integration.

client    HTTP client for the producer API (POST /v1/jobs, lookups, changes, destinations).
refs      source_ref and idempotency-key helpers shared by import, submit and sync.
payloads  request-body builders for chapter, create-book and cover jobs.
state     the destinations snapshot the sync process caches for the release gate.
gate      what the release gate reads (read-only; the production planner uses it too).
submit    submitting released jobs, the release gate's writes, and recovery.
importer  the cutover export file publish-hub's import-forwin loads (read-only on ForWin's database).

This package root stays empty on purpose: forwin.publisher_runtime.service imports the client,
and payloads imports publisher_runtime, so importing payloads from here would be a cycle.
"""
