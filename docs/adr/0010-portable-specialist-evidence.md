# ADR 0010 — Portable specialist evidence is append-only admission proof

- **Status:** Accepted
- **Date:** 2026-09-29

## Context

Clockwork and EdgeLab can now emit self-contained JSON evidence artifacts, but Conductor previously
accepted only configured evidence pointers such as `producer`, `artifact_type`, and `location`.
Those pointers were useful metadata but were not proof that Conductor had read the artifact, checked
its contents, or preserved the exact validation result used for admission.

A strategy release also needs to accumulate evidence over time: research evidence exists before
validation, validation evidence arrives later, and a reviewed release may require fresh validation.
Forcing every new evidence artifact into `conductor.toml` would make configuration the mutable
research ledger and would blur specialist research state with Conductor-owned admission state.

## Decision

Conductor accepts portable specialist artifacts through `conductor evidence-ingest`.

The first supported schemas are:

- `clockwork.research.v1` from Clockwork;
- `edgelab.validation.v1` from EdgeLab.

Conductor independently validates the declared producer/artifact type, recomputes the producer's
content-addressed `evidence_id`, checks schema-specific admission semantics, and stores the exact JSON
payload plus the imported file SHA-256 in `strategy_release_evidence`. Attachments are append-only and
an exact replay is idempotent. Reusing an evidence ID with different content fails closed.

Configured `evidence = [...]` references remain immutable seed metadata for a strategy version. They
do not satisfy validation admission. Portable evidence may be appended to that same version because
it is separately persisted, content-addressed, and never replaces an earlier attachment.

A `RESEARCH -> VALIDATED` transition requires at least one ingested validation artifact whose
artifact type is `validation`, decision is `pass`, and `eligible_for_promotion` is true. A
`REVIEW -> VALIDATED` transition requires qualifying validation evidence ingested after the most
recent transition to `REVIEW`.

Clockwork research evidence can establish readiness for independent validation but cannot by itself
promote a release. EdgeLab validation evidence can satisfy the statistical admission proof but does
not skip the explicit Conductor release transition or any later shadow/live gate.

## Consequences

- Conductor consumes real specialist artifacts instead of trusting file pointers in configuration.
- The exact evidence used for admission survives source-file movement, process restart, and later
  research changes because the payload and hashes are retained in the ledger.
- Specialist repositories remain independent: they produce evidence but cannot grant broker
  authority or mutate Conductor release state.
- New transitions into `VALIDATED` become evidence-backed rather than operator labels alone.
- New evidence producers require an explicit schema adapter/validator in Conductor instead of being
  accepted as arbitrary JSON.
