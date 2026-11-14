# Repairing a changing public review table

The ranged `userreview` export reached 2,241,219 rows while the source had 2,241,303. Re-running the completed range plan repeats a count comparison without discovering earlier insertions. Ordinary keyed reconciliation rebuilds all text, and its broad queries can hit the same public SQL cost limit that required ranges initially.

Use a bounded range scan of per-game counts and content fingerprints. For supported scalar columns, PostgreSQL `json_build_array` and Python JSON with comma-space separators produce the same row representation. Three actual public reviews verified this representation without logging their text. A second live probe compared all 118 captured reviews for game 1 against the remote ordered group digest and count; both matched (2026-11-14T02:00:52Z). The existing review worker entered this repair path on its next fresh pass; its early range scan used about 32 MiB RSS and no swap. These checks establish the representation and live dispatch, not full-table repair completion. Each game fingerprint hashes its ordered row fingerprints. A changed game is downloaded into durable staging, checked against the remote fingerprint and count, and atomically replaces only that game's old rows. Unchanged games retain their captured bodies. A game removed or made private is removed only following a verified empty source group.

Queries retain the existing public visibility predicate. Only integer leading keys and scalar integer, boolean, text, date and timestamp columns are supported initially. Text pagination requires the existing binary-collation check. Unsupported types fail closed rather than claiming a matching fingerprint. Fingerprints detect changes; they are not publisher authenticity claims. Final SQLite publication continues to require all source tables, schema/count/integrity checks, and full anonymous SHA-256 verification.

Each range response contains at most 257 groups; responses with 257 groups split the range before processing. Cost rejections also split ranges. Each body page retains the existing 500-row and response-byte bounds. Local rows are hashed in bounded pages, releasing cursors before any network call. Network calls never hold a SQLite write transaction. Per-table claims remain the responsibility of the existing caller; no new concurrent collector is introduced.

A range is complete only when its source summary is identical before and after processing. If the final full source count changes, the summary ranges are checked again, retaining verified body repairs. A checkpoint persists the pending range and group download cursor. A failure leaves old group rows intact. Schema changes invalidate staging. Source changes during a group download reject staging and retry against a fresh fingerprint.

Alternatives: repeating count checks cannot repair missing/deleted keys; whole-table text rebuilds discard the reuse opportunity and fail broad cost limits; keys alone miss content updates; timestamp-only comparisons assume every update advances a timestamp. Content fingerprints cost server hashing and one local pass, but transfer only summaries plus changed groups. This is the accepted tradeoff for the roughly 2.2 million review rows.

A remote atomic snapshot across HTTP requests is unavailable. Receipts describe the collection/check interval, not a PostgreSQL transaction snapshot. A continuously changing game can defer completion; bounded retries leave the goal active rather than accepting mismatched rows. The implementation must prove additions before the old cursor, deletions, equal-count content updates, network restart, schema/type rejection, visibility predicates and cost-driven range splits. Actual full-table completion and publication remain unproven until the live checks finish. No user decision blocks implementation; broader type support waits until a real need and matching serialization evidence exist.

Live range checks twice observed a changed source after verified group repairs.
Such a range now splits into smaller intervals and rechecks them in the same
bounded worker, retaining repaired groups. This avoids the normal five-minute
network/error cooldown for a changing multi-game range. Children cover the exact
parent interval; no completed range is accepted from a changed summary. A hot
singleton still defers, so continual edits cannot cause unbounded subdivision or
a tight request loop. SQL request pacing and full final count checks remain.
