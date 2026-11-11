---
id: ADR-075
title: Product releases are signed with a project key
status: ACCEPTED
admission: OPERATOR_AUTHORIZED
---

# ADR-075: Product releases are signed with a project key

Date: 2026-11-11. Authority: operator decision.

## Context

An installed copy of tare updates itself from the `product-*` releases of tare.tools.os
([ADR-074](ADR-074_PRODUCT_RELEASES_FROM_CI.md)). It takes the newest release whose notes contain
`CI passed on main: ` and installs its `bundle.zip`. The notes are text anyone who can create a release can write, so
a leaked token with `contents: write` is enough to push code to every installation within five minutes.

Verifying the Sigstore build attestation that the workflow already publishes was tried and set aside: it would put
a third-party root of trust, its key rotation and an observation mode inside the installer, which runs with a bare
Python before any dependency exists and must stay small. The attestation stays as provenance for people
(`gh attestation verify`).

## Decision

1. Releases are signed with an Ed25519 key of the project, the pattern of Sparkle and Tauri. The signature covers
   `SIGNED_PREFIX` (`tare.tools product bundle` and a newline) followed by the lowercase hex SHA-256 of `bundle.zip`.
   Signing the digest keeps the message tiny, so any Ed25519 tool can produce the signature (OpenSSL 3 in CI); the
   prefix keeps the key from signing anything another protocol could mistake for its message. The release carries the
   base64 of the 64-byte signature as `bundle.zip.sig`.
2. The private key lives only in the `RELEASE_SIGNING_KEY` secret of the `release-signing` GitHub environment, which
   only protected branches may use. The workflow's `sign` job uses it; that job runs no build: it receives the
   release's `bundle.zip` and signs it in a step that runs only OpenSSL and removes the key file when it ends. A later
   step, which never sees the key, checks the signature with the keys committed to `main`
   (`relay/native_product.py verify-signature`). `publish` creates the release as a draft, and the draft becomes
   visible only after the signature verifies. A draft left by a run whose signing failed is deleted by the next run.
3. The public keys ship inside the installed copy (`RELEASE_KEYS` in `relay/native_product.py`, standard library
   only, Ed25519 verification of RFC 8032 in pure Python). Any listed key is accepted, so a key is rotated by shipping
   a release that lists the old and the new key, then dropping the old one.
4. A second, recovery key is generated with the CI key (`scripts/ops/release_signing_key.py`) and listed from the
   start. Its private half stays offline and signs a release by hand when the CI key is lost or exposed, so such a
   loss does not strand installed copies.
5. It rolls out in two steps. The first change makes releases carry the signature and adds the verifier; installed
   copies still accept an unsigned release. The second makes the updater refuse a release without a valid signature,
   once a real signed release exists, so no installed copy is left waiting for one.

## Consequences

- A release created or altered by someone who can write releases but cannot run the protected workflow is not
  installed: it has no signature, or one that does not verify.
- Someone who can change `main` is not stopped: the workflow would build and sign their code. Protecting `main` (pull
  requests, CI, review) stays the control for that.
- Someone who can change the `sign` job's environment settings or the secret is not stopped either; those need
  repository admin rights.
- Losing both keys means installed copies cannot update and must be reinstalled by hand from a bundle that lists new
  keys.
- Until the second step ships, installed copies do not require the signature and the protection above is not in
  force.
