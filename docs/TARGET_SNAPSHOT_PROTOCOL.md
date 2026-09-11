# Conductor Target Snapshot Protocol v1

Status: V0.3 implementation contract.

## Principle

A strategy publishes **complete desired economic state**, never broker orders and never trade deltas.
Conductor owns capital allocation, quantity resolution, portfolio risk, cross-strategy netting,
reconciliation, execution, and economic ownership.

The replacement key is `(strategy_id, book_id)`.

A higher accepted revision completely replaces the previous snapshot for that key. Instruments or
structures absent from the new complete snapshot have desired exposure of zero. Revision gaps are
allowed because snapshots are self-contained.

## Envelope

Conductor uses a CloudEvents-style JSON envelope:

- `specversion`: `1.0`
- `id`: producer-generated immutable event ID
- `source`: authorized strategy producer URN
- `type`: versioned Conductor event type
- `subject`: `<strategy_id>/<book_id>`
- `time`: event creation timestamp
- `dataschema`: versioned schema identifier
- `data`: economic snapshot payload

Conductor combines `(source, id)` identity with a monotonic book `revision`.

### Replay/revision rules

1. Same `(source, id)` and same canonical payload hash: `duplicate`, no state change.
2. Same `(source, id)` and different payload hash: hard reject (`event_id_payload_collision`).
3. Higher revision for `(strategy_id, book_id)`: accept and replace desired book state.
4. Lower revision: reject (`stale_revision`).
5. Same revision with a different event ID: reject (`revision_conflict`).
6. Revision gaps are valid.
7. A `(strategy_id, book_id)` cannot change protocol family across revisions.

The ID/revision check, immutable event write, and desired-book replacement are one SQLite
transaction.

## LinearTargetSnapshot v1

Event type:

`com.defacto.conductor.linear-target-snapshot.v1`

Data schema:

`urn:defacto:conductor:schema:linear-target-snapshot:v1`

`weight` is a signed fraction of the strategy's Conductor-owned capital budget. Conductor does not
automatically normalize target weights to 100%. Gross and net exposure are economic choices made
by the strategy, subject to later Conductor portfolio constraints.

Example:

```json
{
  "specversion": "1.0",
  "id": "etsa-42",
  "source": "urn:defacto:strategy:etsa",
  "type": "com.defacto.conductor.linear-target-snapshot.v1",
  "subject": "ETSA/main",
  "time": "2026-09-11T20:00:00Z",
  "dataschema": "urn:defacto:conductor:schema:linear-target-snapshot:v1",
  "data": {
    "strategy_id": "ETSA",
    "book_id": "main",
    "revision": 42,
    "as_of": "2026-09-11T20:00:00Z",
    "valid_until": "2026-09-12T20:00:00Z",
    "weight_basis": "strategy_budget",
    "targets": [
      {"instrument": "EQ.US.AAPL", "weight": "0.08"},
      {"instrument": "EQ.US.MSFT", "weight": "-0.06"}
    ]
  }
}
```

An empty `targets` array is the strategy/book flatten semantic.

## StructureTargetSnapshot v1

Event type:

`com.defacto.conductor.structure-target-snapshot.v1`

Data schema:

`urn:defacto:conductor:schema:structure-target-snapshot:v1`

Structures preserve leg identity, ratios, and structure-level risk intent. Example use cases include
futures curve spreads, spot/perp carry, option spreads, and calendars.

```json
{
  "specversion": "1.0",
  "id": "gc-18",
  "source": "urn:defacto:strategy:futurescope",
  "type": "com.defacto.conductor.structure-target-snapshot.v1",
  "subject": "FUTURESCOPE/gc_curve",
  "time": "2026-09-11T20:00:00Z",
  "dataschema": "urn:defacto:conductor:schema:structure-target-snapshot:v1",
  "data": {
    "strategy_id": "FUTURESCOPE",
    "book_id": "gc_curve",
    "revision": 18,
    "as_of": "2026-09-11T20:00:00Z",
    "sizing_basis": "strategy_budget_risk",
    "targets": [
      {
        "structure_id": "gc-z26-g27",
        "legs": [
          {"instrument": "FUT.COMEX.GC.202612", "ratio": "1"},
          {"instrument": "FUT.COMEX.GC.202702", "ratio": "-1"}
        ],
        "target_risk_fraction": "0.25"
      }
    ]
  }
}
```

V0.3 validates and durably stores structure snapshots. It intentionally does **not** resolve them
into executable leg quantities yet; structure sizing/lifecycle is a later portfolio-engine layer.

## StrategyProfile is separate from schema

A schema answers whether a document is well formed. A Conductor-owned `StrategyProfile` answers
whether a particular producer is allowed to publish it and where its economic state belongs.

Profiles own:

- authorized `source`;
- sleeve assignment;
- allowed event types;
- allowed books;
- maximum signal age and clock skew;
- later: asset-class permissions and strategy-level risk limits.

A producer cannot select its own broker, venue, capital allocation, or portfolio risk limits.

## Staleness

Effective expiry is the earlier of:

- producer `valid_until` (when present); and
- `as_of + StrategyProfile.max_age`.

Freshness is checked both when a snapshot is accepted and again when desired state is loaded for
execution. Previously accepted state therefore cannot remain executable forever.

## Instruments

Protocol examples use Conductor-owned canonical IDs such as:

- `EQ.US.AAPL`
- `FUT.COMEX.GC.202612`
- `CRYPTO.HL.BTC-PERP`

The broker/exchange resolver maps these stable IDs to IBKR, Nautilus, Hyperliquid, or other venue
identifiers. Full instrument-registry enforcement is a later milestone.

## Transport

V0.3 uses an atomic local-file inbox because it is simple, inspectable, and reliable on both
Windows and Lubuntu. The protocol itself is transport-independent.

Producer write path:

1. serialize validated snapshot to a temporary file in the destination filesystem;
2. flush and `fsync`;
3. atomically hard-link the completed temporary file into `inbox/` without overwrite; and
4. remove the temporary link.

Unsafe event IDs are hashed for the transport filename. The event ID inside the document remains
unchanged. Conductor's supported Windows/NTFS and Lubuntu/ext4 deployments provide the required
hard-link primitive.

Consumer paths:

- `accepted/`
- `duplicate/`
- `rejected/`

Each consumed file receives a small `.result.json` sidecar.
