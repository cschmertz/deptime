# Security policy

Please report vulnerabilities **privately** through GitHub's "Report a vulnerability" button on the Security tab (private vulnerability reporting). Don't open a public issue. You'll get an acknowledgement within a few days.

## In scope

- **Index ingestion:** anything that makes `deptime ingest` execute package code, escape the extraction directory, or accept a tampered artifact.
- **Index distribution:** bypassing the manifest checksum or schema checks in `deptime index pull`, or the automatic refresh.
- **Prompt injection through mined text:** deprecation strings from third-party packages that could steer an agent (see docs/08).
- **The hosted server (`deptime serve`):** request handling, rate limits, transport security settings.
- **The Claude Code plugin hook** (`integrations/claude-code/scripts/suggest_deptime.py`).

## Design notes

- **Local mode** (`deptime mcp`) sends nothing anywhere. The only network request is downloading the prebuilt index from this repository's GitHub Releases.
- **Packages are never imported or executed** during indexing. Analysis is static, with griffe and `allow_inspection=False`.
