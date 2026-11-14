# Static catalogue payload

`build-site` writes the public profile of the single `manifest.json`. It preserves
all product/file identities, manufacturer relations, sources, original URLs and
filenames, mirrors, posted/measured checksums and sizes, availability, current
errors and publication proofs. Static HTML, search and download filename handling
use the same schema-version-1 core fields.

The public profile omits raw source fields, the complete attempt log, saved-page
inventory and full linked external database rows. Those remain authoritative in
SQLite. The default `Store.export()` and `export_manifest(store)` still produce
the complete archival view. A site build uses `export_manifest(public=True)` and
compact JSON encoding. Both modes read related tables in one SQLite snapshot and
atomically replace the generated file.

On 2026-11-14 the deployed JSON measured 263,413,599 bytes. A public-profile export
from the live local database measured 116,914,722 bytes and preserved all 76,337
catalogue file entries across 4,277 manufacturers, checked against the same read
snapshot. Peak RSS was 350,328 KiB and elapsed time 121.73 seconds. The deployed
baseline and local measurement use different catalogue generations. The private
receipt is `archive/.work/public-catalogue-live.json`.

Tests check that origin URLs, errors and posted checksums survive in the public
view; full source fields, attempt history and linked metadata remain available
from the same SQLite; and static output still contains just one JSON catalogue.
