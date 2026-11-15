# Saved HTML reparse cache

Background reparse uses SQLite as its only persistent state. `mirror_reparse`
retains its existing seven-column schema; `mirror_reparse_files` stores a separate
path, verified source hash and file signature. The signature contains device,
inode, size, modification time and change time. A skip requires the current parser
version, a successful prior parse, the same path/hash and an unchanged signature.

An absent cache is primed by reading and hashing the HTML once, preserving the
original parse time when the bytes and parser are unchanged. Parser updates,
changed paths/signatures and prior errors force a read. Files changed during the
read do not acquire a cache signature and are checked again on the next pass.
Cache writes are committed in batches of 128 without holding a writer transaction
while reading HTML. No catalogue JSON is materialized. Shared parser source
fingerprints are computed once per pass instead of once per adapter.

On 2026-11-14, two live passes completed in 5.848 and 5.433 seconds while accepting
new captures. A separate unchanged pass completed in 6.464 seconds with zero HTML
reads, measured independently of SQLite I/O. Receipts are private under
`archive/.work/reparse-file-cache-live.json` and
`archive/.work/reparse-file-cache-html-live.json`. Tests cover file replacement at
the same size/mtime, deletion and restoration, changes during reading, parser
updates, old-cache migration and unchanged HTML requiring no read.


Navigation-only edits to `mirror_sources.navigable` also retain the extraction
fingerprint. Other source changes still invalidate it; a direct or dynamic
reference to that helper outside the crawler conservatively restores the full
module fingerprint. During the live cutover, equality of all non-navigation AST
nodes was checked against the previous committed source before promoting 55,209
exact old parser identities. Original hashes and parse times were retained.

DMM product-review URLs are now observation-only navigation. Existing observed
links and captured HTML are kept. The 1,232 pending review targets were marked
`excluded_navigation`; product pages and review query parameters on product URLs
remain eligible. This reduces traversal work without narrowing the distribution
file archive or deleting source evidence.

## Legacy HTML Windows extensions (2026-11-15)

The saved Lapistan publisher document for `fq4demo.avi` contains CP932 bytes
`87 57` for the Roman numeral in `ファーストクイーンⅣ`. Python's strict
`shift_jis` decoder rejected that extension; the replacement fallback produced
`ファーストクイーン�W`. A Fuzzy2 observation explicitly inherited the title
from that source ID, so the damaged text also affected its product association.
The original cached HTML bytes remain intact and permit a source-based repair.

`decode_html` maps the HTML Shift_JIS label family, including `windows-31j`, to
CP932 before both strict decoding and the mixed-Wayback replacement fallback.
The label set follows the [Encoding Standard](https://encoding.spec.whatwg.org/#names-and-labels).
Other encodings and the existing script-charset exclusion remain unchanged.
Regression cases cover all eight labels in HTTP and HTML declarations, Windows
extension characters, browser character mapping, and mixed replay text.

Reparsing the actual saved document retained the same item ID, filename, posted
MD5 and size while recovering the complete title. The original document and
its directly linked Fuzzy2 observation were updated in SQLite. The existing
source-upsert behavior can wake a failed row when descriptive metadata changes;
this is a retry, not evidence that the file has become downloadable. Full public
byte verification is still required before a download URL is exposed. Parser
fingerprints include the decoder source, so ordinary fresh reparse passes also
pick up this correction without deleting raw HTML or archived file objects.


Waffle's shared support index uses `div.article > h5` on
`/official/patch.html`; its detailed support page uses
`div.patch_list > h6` on `/official/patch_.html`. Both are scoped to Waffle's
publisher host and brand. Parse each file against its own product heading,
independently of the game that reached the page. Detailed support can publish
an additional file URL absent from the index. Retain old differently named
patch records; a newer link does not prove their byte identity.
