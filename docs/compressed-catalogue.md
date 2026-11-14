# Compressed catalogue publication

Status: implementation plan, 2026-11-14. The local SQLite remains authoritative.

## Observed problem

A legacy worker holds `.publish.lock` while uploading a frozen 1,219,760,128-byte
catalogue. Its two file read positions are not server acknowledgements. The
canonical public SQLite last checked at 2026-11-14 08:08:23 UTC consequently
predates later file-publication proofs. Body uploads and provenance collection
continue separately. Pages currently downloads `index.sqlite3` on pushes and
hourly at minute 23; its generated JSON remains a derivative, not the database.

A streaming gzip level-1 measurement of that immutable snapshot produced
204,071,844 bytes (16.73%) in 4.664 seconds, with a 19,760 KiB process RSS peak.
The temporary output was removed. This measures compression cost and byte
reduction, not end-to-end publication throughput. Evidence is in the private
`archive/.work/catalogue-compression-audit.json` receipt.

## Direction

Publish a gzip transport of a consistent SQLite backup and let Pages restore it
with Python standard-library streaming decompression. Trial, patch and movie
payloads remain byte-for-byte unchanged. Do not expose storage implementation
text in the site. Do not add a public JSON pointer or another JSON catalogue.

Use the existing immutable staging, destination lock, resumable-part validation,
full anonymous read-back and source-generation safeguards. Compression must use
bounded buffers, a disk reserve, and deterministic gzip headers. Freeze the
SQLite before network I/O. A retry resumes the exact pending compressed generation
instead of recompressing a moving database. Validate the local SQLite and record
both raw and compressed SHA-256 values privately. Complete public compressed-byte
read-back before treating that transport generation as published.

`index.sqlite3.gz` is the proposed transport key. Keep the existing raw object
through migration; it is the bootstrap fallback while gzip has never been
published. Before changing restore precedence, explicitly choose and test whether
raw publication is retired after cutover or retained as a synchronized mirror.
HTTP Last-Modified alone cannot order snapshots: a slow legacy upload can finish
later while containing older data. Do not silently choose it as newer.

## Alternatives

* Raw-only publication preserves the current URL, but transmits about six times
  as many bytes in this measured generation and keeps the current long wait.
* A compressed transport reduces the measured bytes by 83.27%, uses only the
  standard library, and leaves the SQLite data model unchanged. It introduces a
  transport cutover and requires explicit ordering of legacy and new publishers.
* Maintaining equally current raw and gzip copies still requires every large raw
  upload. It is viable for compatibility, but cannot be a prerequisite for Pages
  to consume a verified compressed generation or it retains the current delay.

## Risks and required evidence

* A gzip object must never be confused with a raw SQLite. Test keys, staging paths,
  expected byte counts, decompression failure, HTML responses and integrity checks.
* A successful multipart acknowledgement is insufficient. Test lost completion
  responses, read-back failures, process restarts and retention of accepted parts.
* Legacy workers must not overwrite the new canonical transport or hold its new
  lock indefinitely. Inventory live publishers, preserve verified local bodies
  and existing transfers, and prove a safe handover before interrupting any lane.
* Partial decompression must not replace the Pages database. Restore to a temporary
  file, bound output and disk usage, check SQLite integrity, then atomically replace.
* Native HF upload speed has not been measured because no HF user token is
  configured. Existing S3 credentials remain usable; this plan does not assume
  native-auth availability or claim that compression fixes the network path.

## Decisions before cutover

The implementing agent must settle canonical transport precedence and legacy
publisher handover after inspecting current workers, before changing the deployed
Pages restore contract. These block cutover, not codec or staging tests. A finite
live compressed upload plus full read-back must pass before claiming publication
works. Tests and remote CI alone do not establish this. Remaining accepted costs
are temporary staging disk, one bounded compression pass per frozen generation,
and continued publication lag while each generation is transferred.


## Codec implementation, 2026-11-14

`catalogue_transport.py` now provides deterministic, level-1 streaming gzip for
an already immutable SQLite backup, plus bounded atomic restoration. Both paths
retain a 1 GiB free-disk reserve; restoration defaults to a 16 GiB expanded limit.
Gzip CRC/truncation and SQLite quick-check must pass before a restored generation
replaces its destination. Temporary derivatives are removed on failure. The codec
is not yet wired to publication or Pages: the raw canonical contract remains in
force pending the live compressed transport and handover gates above. Tests cover
determinism, complete recovery, corrupt/truncated/HTML input, invalid SQLite,
expansion limits, disk pressure, symlinks and preservation of a prior generation.


A finite real-backup round trip passed: 1,219,760,128 raw bytes became
204,071,844 bytes, then restored to the identical raw SHA-256. Total elapsed time
was 50.838 seconds including both SQLite checks, with 24,656 KiB peak RSS. Both
temporary derivatives were removed. The private receipt is
`archive/.work/catalogue-codec-live.json`. This proves the codec on real data;
it does not establish remote compressed publication or Pages cutover.


## Transport integration and chosen cutover, 2026-11-14

`snapshot_upload.publish(..., compressed=True)` now stages a consistent compacted
SQLite backup, deterministic gzip bytes and both digests in a private durable
generation. The compressed key, staging directory, destination claim and multipart
checkpoint are separate from the raw transport. Existing raw publishers can
finish without overwriting this generation. All accepted-part, lost-completion
and complete anonymous read-back rules remain shared with the original uploader.

After the first complete verified gzip publication, `index.sqlite3.gz` becomes
the canonical Pages transport. Raw `index.sqlite3` is retained only as bootstrap
fallback while gzip does not exist, independent of HTTP Last-Modified. A present
but invalid gzip must fail a build and preserve the deployed site, not silently
fall back to a possibly stale raw generation. Future default publishers will use
gzip; an already running legacy raw upload is allowed to finish. No source body
transfer needs interruption for this migration. The local SQLite remains SSOT.

A single finite real publication is running independently of the legacy raw
publication lock. Pages restore and the default publisher have not yet switched;
that requires the successful full public gzip read-back receipt. Safe local tests
now cover compressed staging and proof retention, resume of the exact frozen
snapshot after failed read-back, independent/exclusive transport claims, and
rejection of unmanaged paths and arbitrary destination keys.


## Restore CLI prepared, 2026-11-14

`python catalogue_restore.py --output archive/index.sqlite3` now implements the
chosen gzip-first contract. Only an HTTP 404 for gzip permits the raw bootstrap;
timeouts, 403/429/5xx, malformed gzip, CRC/truncation, invalid SQLite and expansion
limit failures preserve the destination and fail. Raw bootstrap has the same
SQLite checks, size bound, disk reserve and atomic replacement. Response handles
and temporary files close on every path. Both modules are included in the wheel
and the installed-package smoke check. The Pages workflow remains on the old
restore command until the real compressed publication has a verified receipt.
