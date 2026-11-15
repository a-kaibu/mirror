# Reviewed product aliases

## Evidence and scope

The same Nameless game currently has two product IDs: LatherCraft calls it
`DeepOne`, while the official-source record calls it `Deep One -ディープワン-`.
The cached LatherCraft item 1394 links to
`http://nameless.nexton-net.jp/game/do01/`; Erogamescape game 26297 names the
latter title and publishes its trial page under the same product directory.
These observations establish product identity. They do not establish that the
unavailable official Dropbox patch is byte-identical to the acquired mirror.

SQLite remains authoritative. Original item JSON, titles, checksums, remote
proofs and download states must remain unchanged. Public JSON continues to use
the existing product and file schema. This change provides explicit reviewed
aliases, without automatic fuzzy matching or another public artifact.

## Alternatives and decision

* Keeping both products preserves provenance but splits one game's downloads.
* Rewriting source titles loses the source spelling and can be undone by a
  later parser run.
* A reviewed product-ID mapping preserves source JSON and survives reparsing.
  Choose this option. Store alias ID, canonical product ID, JSON evidence and
  review time in `product_aliases`. Both products must exist and have the same
  known manufacturer. Reject self-links, chains and cycles.

Apply the mapping to `item_catalog` with SQLite triggers on insertion and
product updates. This also handles six-column writes from collectors that are
already running older Python code, so productive downloads need no restart.
Installing an alias remaps existing catalogue rows atomically. Conflicting
canonical targets are rejected; remove the review and reindex before replacing it.
The internal review API is `catalog.alias_product(store, alias_id, product_id,
evidence={...})`; nonempty evidence is required. Retain both product rows as the
names referenced by the review; the manifest
already exports only products referenced by catalogue rows.

Manufacturer alias review must carry product mappings into the resulting
manufacturer before it reindexes affected items. Conflicting mappings abort
that review instead of silently choosing a title. Product garbage collection
must retain products referenced by either end of a reviewed alias.

## Risks and validation

A wrong review groups unrelated downloads. Limit reviews to one manufacturer,
require nonempty evidence, retain original payloads, and test rejection of
cross-maker and cyclic mappings. Reversal consists of deleting the mapping and
reindexing affected source records; no file deletion or transfer is involved.

Grouping is separate from byte reuse: the dead official DeepOne link remains
pending unless its own integrity constraints can be satisfied. The acquired
LatherCraft body keeps its original MD5, size and public verification.

Validate reparsing, reopening, old-style catalogue writes, manufacturer alias
migration, rollback on conflicting mappings, public JSON grouping, and exact
preservation of item rows. Then apply only the evidence-backed DeepOne review.
The disposable Pages snapshot copies the resolved catalogue associations, not
the review table. Test snapshot validation and restoration explicitly. Verify
public deployment after a new frozen SQLite generation includes the review.

## Remaining decisions

Bulk discovery of aliases is deferred until independent official-URL evidence
can be checked for each candidate. It is not a blocker for this explicit review.
The current implementation must reject chains rather than guess their meaning.
A future curator can flatten mappings in one transaction if multi-step reviews
become necessary. No approval or external dependency blocks implementation.
