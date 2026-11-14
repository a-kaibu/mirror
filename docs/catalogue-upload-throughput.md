# Catalogue upload throughput

Status: bounded two-part snapshot transport implemented; targeted concurrency
and failure/resume tests pass. Actual next-generation rate remains unverified.
Body collection has moved to single-fetch on LatherCraft and the Suezo service;
a separate official-site watcher is active. SQLite remains authoritative and
Pages still produces one derived JSON. No additional public manifest is proposed.

## Evidence

The catalogue staged at 2026-11-14T17:38:15Z is 230,701,369 compressed bytes.
Eleven acknowledged 8 MiB parts represented 92,274,688 bytes at inspection.
Its owner PID 619940 was live. TCP diagnostics on that process's S3 connection
showed 4,254,736 bytes sent, 517,338 retransmitted, about 192 ms RTT, congestion
window 8 and about 400 kbit/s delivery rate. This establishes retransmission on
this connection; it does not locate the failing network hop or prove a provider
limit. Do not change host TCP, qdisc, swap or unrelated networking settings.

A bounded synthetic multipart experiment sent two 524,300-byte parts:

* One connection: 41.890 s for 1,048,600 acknowledged bytes.
* Two connections: 27.579 s for the same acknowledged byte count.

ListParts confirmed each part size, then every exact temporary upload was
aborted. Evidence: archive/.work/snapshot-parallel-probe-20261115.json.
This is one small measurement under concurrent collection, not proof of a
production speedup or sustained 8 MiB-part behavior.

## Decision and alternatives

Keep the in-flight immutable snapshot and its current owner. Interruption would
waste progress and cannot safely load new code into that process. Implement at
most two concurrent part uploads for future immutable catalogue snapshots.
Retain the sequential body upload path and delegated PartClient behavior.

Remaining sequential forever is simplest, but prolongs stale public metadata
under the measured poor single-connection path. Sending recent acceptance
information as a separate public JSON or an ad hoc Pages overlay would add a
second publication protocol and stale/merge rules; it is unnecessary for this
first bounded transport improvement. Raising concurrency beyond two is deferred
until an actual snapshot demonstrates rate, memory and recovery behavior.

## Required invariants and validation

Keep object/source claims, exact immutable snapshot identity, server ListParts
comparison, completion and full anonymous read-back gates. Workers only upload
parts; the owning main thread writes SQLite checkpoints and completes the object.
Hold no more than two 8 MiB payloads for normal snapshots. Join all active uploads
before returning failure; persist successful sibling acknowledgements even when
one part fails. Retry only missing or inconsistent parts, with ordered completion
and a full source digest check. Do not accept source records from part receipts.

Test concurrent out-of-order acknowledgements, one failed part with a successful
sibling, bounded workers/payloads, resume, unchanged sequential body behavior and
snapshot promotion/public proof. Then inspect the next actual generation rather
than claiming production improvement from the small synthetic experiment.

Decision is two workers for future snapshots, conditional on those tests. Actual
sustained speed remains unverified and is measured during rollout. There is no
user approval gate; active archival transfers and the single SQLite/JSON model
remain in scope throughout the change.

Implementation is `_snapshot_parts` in multipart_upload.py, selected only by the
immutable snapshot route. The same object claim and full local/public hash gates
surround it. The main thread records all completed sibling results before
releasing ownership, including on failure. Regression tests force part 2 to
finish before part 1, verify ordered completion and at most two payloads, and
prove a successful sibling is not resent after the other fails. No active owner
is restarted to load this change; the catalogue supervisor's next fresh child
will load it naturally.
