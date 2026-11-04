# Satellite repository contents

Each satellite owns its source, contracts, maintained documentation and tests. Change it in its own repository,
let its CI pass, then move the OS pin with `scripts/ops/bump_satellite.py`
([runbook](SATELLITE_FEDERATION_RUNBOOK.md)). The current tactical engine is `tare.tools.agent-runtime`; the
retired harness stays frozen and is never imported, copied or resumed.

## What a satellite tree holds

- Maintained code, tests, contracts and guides, in English. The only Portuguese text is a `README.pt-BR.md` that
  mirrors `README.md`, plus test data that deliberately exercises Portuguese.
- A README that lets someone who clones the repository install it, run its tests and see how the OS pins it.
- No obsolete history: archived editions, dated execution records, snapshots and imported chat transcripts are
  deleted, not kept or translated. Git history still has them. All six satellites went through this pass on
  2026-11-03.
- No machine-specific data: machine addresses and paths come from operator configuration or portable examples,
  and tests use bounded fixtures and disposable storage instead of real records or operator databases.

The Library's own research is its product. The OS does not ground Work on it: specs and ADRs are read from the
repositories that own them. The Node's fleet, state, credentials and model weights are private
installation data; its code license grants no rights to third-party model weights.

## Retired records

The Kernel, the Library and the Backlog Graph list records that were removed from Git in
`docs/RECORDS_ARCHIVE.json`, by original commit, path, SHA-256 and size, without distributing their bodies. Passing
tests or metadata checks over that list create no new qualification, ratification or authority to settle Work.

## Publication

A clean current tree is not a public-history clearance. Git history, pull-request refs, repository visibility and
the hosted CI policy are separate decisions for the owner.
