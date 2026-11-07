# Security policy

Please report vulnerabilities **privately** through GitHub's "Report a vulnerability" button on the Security tab (private vulnerability reporting). Don't open a public issue. You'll get an acknowledgement within a few days.

## In scope

- **Index ingestion:** anything that makes `deptime ingest` execute package code, escape the extraction directory, or accept a tampered artifact.
- **Index distribution:** bypassing the manifest checksum or schema checks in `deptime index pull`, or the automatic refresh, or getting an index accepted by `--verify` that this repository's build workflow didn't produce.
- **Prompt injection through mined text:** deprecation strings from third-party packages that could steer an agent (see docs/08).
- **The hosted server (`deptime serve`):** request handling, rate limits, transport security settings.
- **The Claude Code plugin hook** (`integrations/claude-code/scripts/suggest_deptime.py`).

## Design notes

- **Local mode** (`deptime mcp`) sends nothing anywhere. The only network request is downloading the prebuilt index from this repository's GitHub Releases.
- **Packages are never imported or executed** during indexing. Analysis is static, with griffe and `allow_inspection=False`.

## Verifying the index

Every published index has a signed [build provenance attestation](https://docs.github.com/en/actions/security-guides/using-artifact-attestations-to-establish-provenance-for-builds): `build-index.yml` signs `index.sqlite.xz`, `index.sqlite.gz` and `manifest.json` with Sigstore before uploading them, and uploads the signature bundle beside them as `index.sigstore.json`. The signature is recorded in Sigstore's public transparency log.

- **`deptime index pull --verify`** installs the index only when the attestation verifies. It needs the [GitHub CLI](https://cli.github.com) (`gh`) on `PATH`, but no GitHub login. `gh attestation verify` checks the signature, the certificate chain, the transparency-log entry and that the file is a subject, and DepTime then requires a certificate that names this repository's `.github/workflows/build-index.yml` on `refs/heads/main`, run on a GitHub-hosted runner. It compares these exactly itself, since `gh` before 2.102 matched `--signer-workflow` and `--source-ref` loosely. `deptime index status` shows the run and commit that built the installed file.
- **`DEPTIME_VERIFY_INDEX=1`** applies the same check to every download, including the automatic refresh of `deptime mcp` and `deptime serve`. A file that fails isn't installed, and the current index stays. An index installed earlier without the check stays in use until the next download, or until `deptime index pull --verify` checks it now.
- **By hand:**

  ```bash
  gh release download index -R cschmertz/deptime -p index.sqlite.xz -p index.sigstore.json
  gh attestation verify index.sqlite.xz -R cschmertz/deptime --bundle index.sigstore.json \
    --signer-workflow cschmertz/deptime/.github/workflows/build-index.yml --source-ref refs/heads/main
  ```

What it proves: the file was built by this repository's workflow from a commit on `main`, not uploaded by hand or by another workflow or branch. It doesn't protect against a malicious commit on `main` itself; such a build would still be signed, but the signature would be public in the transparency log and name the commit.
