"""Audit sync over a direct request channel between ForWin and its browser extension.

service   AuditRequestService: creating an audit request (one open request per book), the
          extension's claim with its lease, and applying the extension's observation.
routes    the two extension endpoints (claim, result) as FastAPI handlers.

Reading a book's review state is a read-only operation with a large answer, so it does not
go through publish-hub (D8), and it no longer rides on the upload-job runtime that is being
retired (D11); it works the way comment sync does.

This package root stays empty on purpose: forwin.publisher_runtime.service imports the
service, and routes imports the application layer.
"""
