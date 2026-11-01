"""ForWin's side of the publish-hub integration.

client    HTTP client for the producer API (POST /v1/jobs, lookups, changes, destinations).
refs      source_ref and idempotency-key helpers shared by import, submit and sync.
payloads  request-body builders for chapter, create-book and cover jobs.

This package root stays empty on purpose: forwin.publisher_runtime.service imports the client,
and payloads imports publisher_runtime, so importing payloads from here would be a cycle.
"""
