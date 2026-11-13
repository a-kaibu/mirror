# VN Mirror Archive

Preserve VN trial editions, patches and promotional movies in the public
[HF bucket](https://huggingface.co/buckets/vn-archive/mirrors), with a static
manufacturer-oriented download catalog for GitHub Pages. Trials and patches have
priority. Python 3.11+. The project owner has permission from the source owners.

## Source of truth

`archive/index.sqlite3` is the only source of truth. `items` stores source records
and download progress; `manufacturers`, `manufacturer_aliases`, `products` and
`item_catalog` store the cross-source catalog. Source sites are provenance, not
catalog sections. Known manufacturer spellings are normalized with Unicode NFKC,
whitespace folding and case folding. Further aliases are explicit curator choices:

```sh
uv run python archiver.py alias-maker "alternate spelling" "canonical name"
```

Products match by manufacturer and normalized title. Manufacturer-unknown titles
remain separated by their source page. Do not guess brands, patch compatibility,
version numbers or product associations. Patch version, platform and applicability
are recorded when the source supplies them; source labels and fields are retained.
SHA-256 confirms byte identity: identical bytes share one HF object, while catalog
entries retain each product association and all source filenames and URLs.
Different revisions remain separate. Unretrieved files stay in the catalog with
no download link and a recorded failure or pending state.

Split distributions retain each published `.r00`–`.r99` or three-digit volume as
a separate file. MultiInside catalogues also retain explicitly typed DAT parts
and BAT joiners when their companion volumes or installer appear in the same
product block and directory. Parts keep their own published checksums; missing
checksums are left absent. Files are preserved without running the joiners.

`manifest.json` is the **single JSON export**. It contains manufacturers, products,
files, nested sources/attempts, and saved-page records. It is generated atomically
from SQLite, ignored by Git, and never edited or imported as the source of truth.
There are no separate JSONL or search catalogs.

Source matching streams a compact ID index and loads full donor records from
SQLite only when needed, rechecking their current checksum, URL and quarantine.
Startup classification and platform URL seeding also stream source records.
HTML discovery uses bounded workers: `mirror_sources.py --workers 2`, and
`metadata_watch.py --workers 1 --max-pages 10` (one source per mode, two total
with `--mode both`). Fresh metadata batches release their DOMs and caches at exit.
Official continuation defaults to one worker. Increase these limits only with
enough memory; source coverage and saved frontiers are retained at lower limits.

## Setup and small run

```sh
uv sync --locked
uv run python -m unittest discover -s tests -v
uv run python archiver.py bucket-init
uv run python archiver.py crawl --site both --max-pages 1 --max-items 2
uv run python archiver.py status
uv run python archiver.py download --limit 2
uv run python archiver.py verify
uv run python archiver.py upload --limit 2
uv run python archiver.py build-site --output dist
python3 -m http.server --directory dist 8000
```

S3 uploads use `~/.aws/credentials` profile `hf`; credentials never enter the DB,
exports, site or repository. Namespace `vn-archive`, bucket `mirrors` are defaults.
Pass `--root /path/to/archive` **before** the subcommand. One crawler per source and one archival batch can operate on the same archive using SQLite
WAL. Transfers use a metadata comparison before recording success so changed source
expectations are never silently overwritten. Do not run overlapping download batches. `download --id <source-record-id>` selects a
specific source record; IDs appear in the manifest and SQLite.

Large public read-backs use two bounded byte ranges when the server advertises
range support and a strong ETag. Each response must match its requested offsets,
total length and validator; bytes feed the complete SHA-256 in their original
order. Timed-out or truncated ranges retry independently with the same validator.
Ignored ranges fall back to a complete streaming read, and verification failures
retain the local file for retry.

`uv run python publication_watch.py --watch` drains verified local files that
remain unpublished after a batch interruption. It shares per-file claims with
the download workers, skips busy or quarantined observations, and never starts a
source download. Publication and full read-back failures retain local files for
the next pass. A fresh process runs each pass with a five-minute cooldown; add
`--keep-local` to retain working copies after successful publication.

## Large collections

After crawling, `uv run python archiver.py archive-batch --site lathercraft`
processes trials and patches before movies, starting with smaller known files.
Each file is downloaded, validated, uploaded and fully read back before its local
working copy can be removed. `--keep-local` retains copies. A 16 GiB disk reserve
is enforced during transfers; partial downloads are preserved for resumption.
Checkpoint metadata is published every ten successes and at batch completion.
Upload failures stop the batch; individual source failures remain recorded for retry.

A source entry with the same published MD5 and compatible exact size can reference
an unambiguous, already publicly verified file for the same manufacturer. The
`reused_from` field records this association without claiming a fresh source fetch.
Files with differing hashes or makers are not merged this way. `archived` status
means the public copy is verified and the local working copy need not exist.
Repeated uploads skip previously verified objects, and unchanged source HTML is
not repeatedly uploaded. SQLite remains the only authoritative state.

## Current sources

- **LatherCraft**: real `/items?page=N` listings and `/items/{id}` detail pages.
  Product, brand, item label, publication date, filename, exact byte count and MD5
  are extracted from the actual strong-label paragraphs. The detail heading is
  used instead of the site's global H1. Default listing transport is HTTP because
  HTTPS on the main host was refused in live testing; explicit HTTPS seeds remain
  available with `--lathercraft-url`. Download URLs retain the source scheme.
- **Holyseal**: the starting capture is
  `https://web.archive.org/web/20181025223313/http://holyseal.net/cgi-bin/newinfo.cgi?mode=file&flt=try`.
  Default seeds cover `flt=try`, `flt=fix`, and `flt=demo`, in that order. Limits
  apply per seed. Pagination follows actual HTML links, preserving category
  filters. There is no fixed-year restriction. Wayback can return a different
  capture; its actual timestamp is kept and used for relative links. Both
  `dltbl` and `fixtbl` tables are parsed release by release across rows, associating
  the correct filename, version label, date, MD5 and mirror links. Entries without
  surviving links are retained. Archived binary URLs are tried before live URLs.
- **Suezo**: dedicated site crawling is deferred. Existing Wayback binary copies
  linked from Holyseal can be recovered as Holyseal provenance.

Pass repeated `--holyseal-url` options to replace the default seeds with explicit
captures. Original HTML bytes, including legacy encodings, are saved. HTML schema
failures are reported rather than treated as empty successful crawls.

## Files, integrity and publication

```text
archive/                         # local, ignored by Git
  index.sqlite3                  # authoritative metadata and state
  manifest.json                  # generated export
  html/<page-id>.html             # original source response
  files/<source>/<record-id>/...  # local verified downloads

HF vn-archive/mirrors/
  ABOUT.txt
  index.sqlite3                  # consistent backup of SQLite for Pages builds
  manifest.json                  # generated from the same catalog
  html/<page-id>.html
  files/<sha256>                 # ID only, no Japanese or original filename
```

Downloads stream to URL-bound partial files. Resume uses an HTTP validator, or a
published MD5/SHA-256 followed by complete-file verification. Files with neither
a validator nor a published digest restart from the beginning.
Range offsets, response length, exact published size and MD5 are checked. HTML
error pages and empty files are rejected. SHA-256 is stored and rechecked before
upload. Rounded sizes such as `1.4GB` are preserved as text, never enforced as
exact bytes. Verified local files are skipped on subsequent download runs.

Uploads use HF's S3 gateway with path-style addressing and 256 MiB multipart
chunks. The full public HTTPS copy is downloaded anonymously and compared by
SHA-256 and byte count before recording publication. Anonymous S3 access is not
used: the gateway requires a signature even for public buckets. HTML, JSON and
the SQLite backup also receive anonymous read-back verification. The backup uses
SQLite's backup API; journals, partial files, credentials and unrelated workspace
files are not uploaded. SQLite backups contain source metadata and error history
and are public, like the JSON export. Work on one authoritative local DB, rather
than replacing it with an older published snapshot.

## Static Pages and original download names

`build-site` creates manufacturer pages, product sections, static HF download links,
Pico CSS 2.1.1 (vendored, MIT), local search/filter controls and one `manifest.json`.
All relative URLs support project Pages subpaths. No runtime server, Node build,
WASM SQLite, third-party asset CDN or database connection is needed. Static links
and the catalog work without JavaScript; search filters operate on rendered HTML.

HF objects have SHA-256 names. A service worker streams bytes directly from HF
and supplies a UTF-8 `Content-Disposition` filename using the original name from
the catalog. It does not buffer entire multi-GB demos in a Blob. Browsers without
service workers still get a direct HF link, which saves with the HF ID; the original
filename is shown for manual renaming. Serve via HTTPS (Pages) or localhost; opening
HTML with `file://` does not enable the service worker. This worker caches neither
binary files nor catalog state. Interrupted downloads are retried from the page.

`.github/workflows/pages.yml` downloads the public SQLite backup, generates the
HTML and JSON, then deploys the Pages artifact. Neither the DB nor generated site
nor JSON is committed. Enable **GitHub Actions** as the Pages source and run the
workflow after publishing catalog updates. There is no scheduled job. The public HF SQLite snapshot has also been restored into a fresh directory and
built with the same standard-library-only command as CI. The repository is
https://github.com/a-kaibu/mirror and the Pages deployment target is
https://a-kaibu.github.io/mirror/.

## Validation

```sh
uv run python -m unittest discover -s tests -v
uv run --with playwright playwright install chromium firefox
uv run --with playwright python tests/browser_e2e.py
```

Tests cover actual saved LatherCraft trial/patch/movie HTML and Holyseal release
and patch tables, source merging, aliases, byte deduplication, SQLite migration and
backup integrity, download resume/failover, corruption rejection, public verification,
HTML escaping, relative URLs and generation of one JSON file. The opt-in browser
test retrieves the existing public HF `ABOUT.txt` without uploading test files,
and checks original Japanese filenames and exact downloaded bytes, search,
390px layouts, project subpaths and no-JS links in Chromium and Firefox.

Live validation on 2026-11-12 recovered Holyseal-listed `amayui_ap2.zip`
(85,957,024 bytes), matched its published MD5
`f4effd8953817db89c48f73af1b0ee40`, uploaded it as
`files/9be3296ff0c86ab79767c9716e2226fa2e401e2c6b4c0ca0519ab544f3f09266`,
and anonymously downloaded the full HF copy with identical SHA-256 and size.
The public SQLite backup, single JSON and saved HTML also passed read-back.
The initial smoke-test catalog contained 11 manufacturers, 13 products and 28 source-derived file
entries. At that initial checkpoint, three files were publicly verified: this Holyseal patch and a LatherCraft
trial and patch. The LatherCraft trial also exercised multipart upload. Every
published binary passed full anonymous HF read-back by SHA-256 and size. Both
Chromium and Firefox passed the opt-in browser check.

LatherCraft storage1 presents a certificate that expired on 2026-09-19. For
legacy source downloads, `sources.toml` explicitly trusts the exact
observed leaf certificate by SHA-256. The TLS handshake must present that exact
certificate before an HTTP request is sent; a replacement certificate fails closed.
This is a legacy source trust decision rather than normal CA/expiry verification.
The pin is host-scoped and reselected on redirects (including proxy tunnels).
HF and GitHub use normal TLS. Files without a published MD5 are still collected;
the missing expectation is retained, the exact size is checked when available,
and computed hashes plus the certificate pin are recorded. Each download
attempt records any certificate pin used. Remove the pin when normal TLS works
again; never automatically update a changed pin. With this configuration, the
399,322-byte LatherCraft patch `IDOL_SIMAI_patch_PK_ver1_0_1.zip` downloaded and
matched its published size and MD5. The 365,177,702-byte trial `ANIM_mamalove2_trial.zip` also downloaded
with matching published MD5 `12965112bfab77dfc27a8b702d8b285b` and size.
Storage2 still timed out.

Some Holyseal product captures and binary links return 404. These are recorded
individually; an archived page is not proof of a recoverable binary. LatherCraft metadata collection reached all 109 listing pages and 2,164 listed
entries. Binary acquisition and Holyseal collection are still in progress.

## JSON search on GitHub Pages

Open `https://a-kaibu.github.io/mirror/search/?kind=trial` in a browser.
The page filters the single generated `manifest.json` using JavaScript and displays
JSON. It has no link in the ordinary catalog UI. Parameters:

- `kind`: `trial`, `patch`, `movie`, `other`
- `q`: NFKC-normalized search across maker, product and filenames; spaces mean AND
- `manufacturer`: maker ID or exact normalized name
- `available`: `true` or `false`
- `limit`: 1–200 (default 50), `offset`: default 0

Example: `search/?kind=trial&available=true&limit=20`.
The response includes `total`, `limit`, `offset`, `generated_at`, and `results`.
Pages serves HTML; JavaScript produces the JSON display in the browser. For code,
import `parameters` and `search` from `search.mjs`, fetch `manifest.json`, then call
`search(manifest, parameters(new URL(searchURL)))`. Direct HTTP clients can fetch
`manifest.json`; no server or second persisted JSON catalog is needed.

Run the search tests with `node --test tests/search.test.mjs`.

### Binary captures from other dates

`python wayback_recovery.py --limit 20` checks failed downloads across sources using
the CDX index for each original binary URL. All returned successful capture dates
are retained in SQLite, including repeated digests: a broken replay at one date
must not hide another date. HTML captures and unrelated paths are excluded.
Index digests are provenance, not a replacement for publisher MD5 validation.
`--refresh` queries successful cached index lookups again. Empty results and
request failures are recorded separately. Limited passes query unseen URLs first,
then the least recently attempted URLs, so recurring failures do not starve later
records. Shared file URLs use a single index lookup for all matching records.
Results reaching the 10,000 capture
limit are marked failed, never reported as an exhaustive search.
After discovery, `archive-batch --site holyseal --retry-failed` tries alternate
dates (newest first), checks file contents, publishes and reads back the result.

`python redirect_recovery.py --site d_station --limit 100` checks archived
redirect responses for pending or failed download CGI URLs with publisher MD5 or SHA-256.
It records the actual response and Location in SQLite, then adds only observed
original file URLs to the source record. Multiple captures with identical redirect
bodies remain separate because their Location headers may differ. The CGI's
capture date is never reused as proof of a binary capture: run
`wayback_recovery.py --site d_station` to index the discovered file URLs separately.
Completed scan receipts let subsequent limited batches advance to other CGI URLs.
Busy download claims are deferred without marking the scan complete; cached
redirect observations remain available for a later pass.

`python filename_recovery.py` fills missing filenames on publicly verified files
from same-manufacturer observations bound to the identical SHA-256. Publisher
hashes and exact sizes must agree when present; ambiguous names are left unresolved.
The evidence and an atomic recovery receipt remain in SQLite. `--watch` checks
again every five minutes without blocking active downloads.

When a source edit invalidates a verified public build, SQLite keeps that build
as a separate historical observation with its original download evidence. The
changed observation returns to pending without inheriting old byte measurements.
Identical SHA-256 results still merge into one file entry per product.

`python reuse_recovery.py --watch` checks every five minutes for waiting or failed
observations whose published MD5 and manufacturer match an already verified public
file. Exact sizes and SHA-256 constraints must agree; existing matching SHA-256
identity is preserved. Active item claims are deferred. This updates SQLite and
the local JSON without downloading or uploading the binary again.

## ErogameScape metadata

`python erogamescape.py --export --upload` reads the public SQL form with bounded
500-row keyset queries. It imports every game, brand, and Holyseal ID mapping into
SQLite, retaining source HTML, each SQL query, and per-row acquisition times.
Resume uses the last committed ID. `--refresh` rereads existing IDs.
Automatic links use unambiguous normalized brand+title matches or Holyseal IDs.

The standalone `erogamescape.sqlite3` contains `brandlist`, `gamelist`, `holyseal`,
`provenance`, and `archive_info`. It includes names, release dates, platforms,
genres, official homepages and trial URLs. Export requires each scan to reach an
empty result and the imported row counts to match the live source counts.
The HF copy is fully read back and checked by SHA-256 and byte count.

Local metadata matching reads manufacturer aliases and Holyseal product mappings
once, computes candidate links before taking the SQLite writer, and atomically
refreshes only automatic links. Curator links remain intact and conflicting
product IDs stay unmatched.

`manifest.json` is generated locally and deployed with GitHub Pages. It is never
uploaded to HF. The archive catalog backup on HF is `index.sqlite3`.

## Publisher trial collection

`python official.py` scans official product/trial pages for brands matched to the
ErogameScape catalog. It prioritizes catalog entries with trial URLs, follows
same-host download/support links within the product directory, and stores every
fetched HTML page. Direct trials, patches and movie links enter the existing
SQLite catalog as `site=official`; CDN links explicitly present in source HTML
are supported. Published digests are attached only within a single-file context.
Publisher flyers, banners and wallpapers are outside this scan's scope.

`official_scans` records per-game results. Unknown binary links, failed pages,
and traversal bounds require review; a page without direct links is not evidence
that every possible distribution mechanism was supported. `--retry` retries review
entries. `--limit N` permits bounded validation. Existing completed scans resume
without re-fetching their pages. Ordinary archive batches can process these new
records, retaining the original page and ErogameScape IDs as provenance.

### Full public database export

`python erogamescape_full.py --upload` enumerates live public table/view schemas
and captures every column of public data tables into
`archive/erogamescape.full.sqlite3`. Authentication, private service preferences,
operational logs and pending moderation edits are excluded before data requests;
`_source_schema.excluded_reason` records the exact exclusions. Private reviews
are filtered according to the public profile visibility flag.

Compound/string primary keys use keyset pagination. Tables without keys use a
deterministic row JSON order and preserve duplicate rows. Decimal values remain
exact text; PostgreSQL arrays/JSON/enums and source type metadata are preserved.
Each page, cursor, error and response hash is recorded for resumption. A source
schema change or count mismatch fails that table instead of reporting completion.
Acquisition spans multiple HTTP requests rather than a single remote transaction.

Publication requires every included table to complete, matching source/local row
counts, and `PRAGMA integrity_check`. The database is checkpointed and held stable
while uploaded as `erogamescape.sqlite3`; full anonymous SHA-256/size read-back is
required. Until full capture finishes, HF retains the previous catalog snapshot.
Use `--max-tables N --max-pages N` for bounded validation; reruns resume.

### Holyseal inventory coverage

`python holy_inventory.py` checks missing product IDs from the imported ErogameScape
Holyseal mapping. It asks the official Wayback availability API for actual capture
dates, tries both documented Holyseal host spellings, validates the returned host,
CGI path and product ID, then runs the same detail parser on the saved capture.
There is no fixed-year assumption. IDs already represented in source records are
skipped. `holy_inventory` distinguishes parsed, failed and `no_capture_returned`;
an empty availability response is not proof that no capture ever existed.
`--retry` rechecks unresolved IDs; `--limit N` bounds validation runs.
This complements list pagination rather than treating a partial listing scan as
proof of full coverage. Source HTML and metadata remain in the archive SQLite.

### Additional mirrors and distribution portals

`mirror_sources.toml` registers the legacy mirrors and Getchu, DMM/FANZA,
Moepedia and DLsite. Run `python mirror_sources.py --site all --max-pages 100`
to resume persistent discovery. Native LatherCraft/Holyseal crawlers run separately.
Legacy sources use actual Wayback availability timestamps; platform seeds come
from imported game metadata. Unmatched files and unsupported pages remain in
`mirror_links` and `mirror_frontier` for review, rather than implying completeness.
Moepedia trial sections have a dedicated parser that excludes purchase links.
Discovered records use the usual verified upload pipeline, for example
`python archiver.py archive-batch --site moepedia`.

For parallel transfers use the same `--shards N` and distinct `--shard-index`
values from zero through N-1. Per-record process locks protect partial files
against overlapping batches. Busy records are skipped for the current pass
and remain queued for a later pass, so other files can proceed. Metadata
publication is serialized separately.

### Suezo recovery monitoring

The units in `deploy/` check Suezo every three hours at 00:00, 03:00, etc.
They require the checkout and virtualenv at `%h/prj/mirror`. Register all three
units with `systemctl --user link` using absolute paths, then run
`systemctl --user enable --now mirror-suezou-watch.timer`.
The probe requires recognizable catalogue content and binary links, not merely
HTTP 200. Recovery triggers live discovery and the separate archive worker.
Each check inspects up to three catalogue or publisher pages and advances a
saved cursor. Observed descendants are checked before unrelated year indexes,
even when already present in the saved catalogue. Pending descendants resume
at the next scheduled check when the three-page budget is exhausted. Response HTML and
health-check history are retained locally with their SHA-256 values in SQLite.
The persistent timer catches missed runs
when the user manager returns; checks require the host to be running.
HF credentials remain in the local AWS profile and are not copied into CI.

### VNDB SQLite export

Download a dated near-complete public dump from https://dl.vndb.org/dump/ and run
`python vndb.py archive/vndb-db-YYYY-MM-DD.tar.zst --upload`. The converter requires
`zstd` on PATH. It imports every table/column declared in `schema_plain.sql`,
validates COPY field counts, preserves NULL and escape sequences, and verifies
SQLite integrity and table counts before atomically replacing `archive/vndb.sqlite3`.
Arrays and enums retain their PostgreSQL text representation; booleans become 0/1.
Original schema, timestamp, README and all supplied licenses are embedded in
`_source_documents`, with dump SHA-256 and conversion provenance in `_archive_info`.
The SQLite file is published as `vndb.sqlite3` and anonymously read back in full.
This is the official public dump scope; content omitted by VNDB is not reconstructed.
When refreshing, replace the complete snapshot so deleted/opted-out rows are removed.

### Continuous discovery and acquisition

`python mirror_sources.py --site all --max-pages 10 --workers 2` visits every
additional source through a pool of at most two workers. Archive HTML requests share pacing and Retry-After
state within the process. Live platform sessions have separate cookie jars.
`python mirror_history.py --site lapistan` queues distinct historical HTML bodies
from CDX, including captures before closure. Explicit dated frontier URLs preserve
their replay date when following navigation.

`python holy_watch.py --watch` continues Holyseal's saved dated product pages
with its native parser, then checks untried product IDs from ErogameScape. Each
phase is bounded to 50 entries; subsequent batches run fresh parser code and keep
source claims separate from the file-transfer workers.

`python metadata_watch.py` continues saved archive and platform frontiers in
10-page batches per source with fresh parser code, one source per mode at a time.
Existing discovery claims are deferred;
archive sources share one paced fetcher. Native and curated adapters keep their
own acquisition jobs. Run long-lived jobs with stdout/stderr redirected to a log
file so a disconnected terminal does not terminate acquisition.

`python mirror_reparse.py --watch` checks saved HTML every minute with fresh parser
code. Parser/source fingerprints avoid repeating unchanged work. No network
requests are needed for reanalysis. Different MD5/size builds keep separate records.

`python archive_watch.py` watches acquisition queues for all additional sources,
with one independent worker per source. Existing body workers can finish safely
because record claims prevent duplicate transfers. Empty queues do not publish
metadata repeatedly. Failed sources retry immediately only when a new, untried
Wayback binary capture appears; publication failures retain local verified files
and retry after cooldown. Native/core batches remain separately managed.

### Independent provenance publication

Run `python provenance_watch.py` alongside acquisition workers. Each pass runs
in a fresh child process so transfer fixes are loaded without retaining old
modules across retries. The supervisor and individual passes use distinct
claims. `--once` performs one pass and returns a failure status on errors. It
uploads and fully verifies saved HTML under a separate provenance lock, then
checkpoints SQLite. Body batches checkpoint only SQLite, so an HTML backlog cannot stall
file transfers. SQLite snapshots still serialize their complete upload/read-back
transaction. `--once` drains one current HTML snapshot and exits. Both publication
paths exclude `manifest.json` and `ABOUT.txt` from HF.

### Creator-published Rance releases

`python hanny.py` follows only download links in the curated article bodies in
`hanny_sources.toml`; sidebars and comments are excluded. It records the published
Drive filename, creator, version and source conditions. `python archive_watch.py
--site hanny` downloads and publishes the results. Public Google Drive confirmation
forms are followed at transfer time; sign-in-required files fail without using an
account. Range validators, complete-body hashing and anonymous HF read-back remain
required. Full releases appear under 本編 (`kind=full`), separately from trials and
patches; frontend JSON search accepts the same filter.

Creator-linked MEGA folders are collected with `python hanny_mods.py --source
ludrasawm --megadl /path/to/megadl` (or `--source daisengoku`). The native megatools
client must be installed separately. Personal account configuration is disabled;
only the complete public folder link from the article is used. Original relative
paths, creator, applicable commercial base games and source conditions remain in
metadata. Native transfer completion produces a local SHA-256 receipt; every file
is checked against it before individual HF publication and full public read-back.
After a publication interruption, `--import-only` resumes from that receipt without
re-downloading the folder. Transfer quotas are respected; interrupted downloads
remain available for the native client's normal resume.

A creator-supplied note in the native staging directory may also link a public
Google Drive mirror. Inspect and archive that inventory with:

```sh
uv run python hanny_drive.py --source daisengoku --list-only
uv run python hanny_drive.py --source daisengoku
```

The note must match its native file receipt. Drive listing uses anonymous access
and preserves distinct relative paths for package and download variants. SQLite
tracks the complete listed inventory and marks the folder complete after every
file has passed public read-back verification. Interrupted publication resumes
from verified local files; Drive acquisitions retain their own source URLs and
verification records.

The VNDB snapshot is available at
https://huggingface.co/buckets/vn-archive/mirrors/resolve/vndb.sqlite3?download=true.

`python alice_support.py` supplements the product crawl with the official support
pages linked by the Rance guide: Rance 03, Sengoku Rance, Rance 10, and GALZOO
Island (including its hard-mode add-on). Each updater section retains its own
version and application conditions, including required earlier updates. The
collector uses ErogameScape game/brand identities and records its coverage in
`official_support_scans`, leaving the primary product scans intact. Files enter
the normal `official` download/publication queue.

The registered `alice_archives` and `alicefree` sources collect the original
free-distribution packages, including separate PC-88/PC-98/MSX/X68000/FM TOWNS/
Windows editions, CD images, multipart RAR files and the separately labelled
Kichikuou booklet. All links come from the published catalogue HTML. Platform
labels, exact sizes where published, multipart grouping and source conditions
are retained; packages are not unpacked or converted for an emulator.

```sh
python mirror_sources.py --site alice_archives --mode live --max-pages 100
python mirror_sources.py --site alicefree --mode live --max-pages 10
python archive_watch.py --site alice_archives --site alicefree
```

Each source has one sequential transfer worker. RetroPC page navigation and
file requests retain their actual same-site referring page as documented by
the public download site. Publisher support patches remain in the `official`
queue and are collected before full games within that queue.

Getchu's trial download service ended in June 2023 and removed all product-page
download links ([official notice](https://www.getchu.com/getchu/news20230623001.html)).
The default discovery mode therefore follows archived product pages, asking
Wayback for a capture near January 2022. This timestamp is a discovery hint,
not proof that a file is intact: actual capture dates remain in provenance,
and independent publisher checksums are still required for matching builds.
Previously queued current-page requests are retained as
`deferred_service_ended` rather than repeatedly fetching pages without files.

Getchu product pages also expose demo movies through `video`/`source` tags.
These are collected using the actual `trial.getchu.com` media URL and matching
product/brand identity; historical capture dates are retained. Captures already
confirmed by Wayback are fetched before pages that still need availability
lookups, and the published `all/demo.html` catalogue links are followed.

When the public ErogameScape SQL form rejects a broad query's execution cost,
`erogamescape_ranges.py` partitions the leading primary key into smaller indexed
ranges without changing the server's limits. Ranges and composite-key cursors
are committed with each batch. Text ranges require a verified binary collation;
unsupported key types fail explicitly. Source row counts and the existing full
SQLite publication gate still have to pass before replacing the public snapshot.
For keyless history tables with one non-null timestamp, the exporter also
uses `erogamescape_timestamp.py`. It verifies that the finite timestamp domain
covers the source, recursively splits costly or large indexed ranges, and
stages pages before committing each complete partition. Pages start at 5,000
rows; responses above the 16 MiB transport limit halve the saved page size
without advancing the offset. Equal timestamps and
identical rows retain their multiplicity. Saved partitions resume through the
normal full-export command. A final recount proves gap-free range coverage;
out-of-domain timestamps, count drift and unsupported schemas fail explicitly.
Visibility-filtered tables cannot use this unfiltered history path.

A failed public table can be resumed independently while the main exporter
continues later tables:

```sh
python erogamescape_ranges.py --table fanza_doujin_sales
```

Legacy `published_ftp_links` also enter CDX discovery using their exact
anonymous FTP URL and FTP-success status 226 (HTTP uses status 200). Cached
FTP lookups made with the HTTP filter are refreshed. FTP default port 21 is
normalized within FTP only; an HTTP
service or another port cannot stand in for that source. Actual indexed binary
capture dates enter the normal checksum-validated download queue. Missing
captures and lookup errors remain recorded separately. The success-code
convention follows Internet Archive's [FTP CDX handler](https://github.com/internetarchive/CDX-Writer/blob/master/cdx_writer.py).

Wayback transport retains the protocol of published replay pages and their
relative links. If a replay's HTTPS TCP port is explicitly refused, a binary
transfer may try the identical HTTP capture only when an expected MD5 or
SHA-256 is present; both attempts are logged and the digest must match. CDX
index lookups likewise try their public HTTP endpoint on connection refusal.
Certificate failures, HTTP access denials and rate limits do not trigger a
transport change. This does not relax validation for other source hosts.

Size discrepancies remain explicit failures until independently checked.
`size_reviews.py` can compare a locally observed ZIP with an independently
fetched historical copy at an actual CDX capture. It requires identical full
SHA-256/MD5/size, a matching CDX SHA-1 digest and valid ZIP CRCs. A contradictory
publisher checksum is never overridden. The review is bound to the exact saved
HTML hash and source-file identity; changed pages or links require new evidence.

```sh
python size_reviews.py --site alice_archives --publish
```

SQLite `size_reviews` retains the original metadata and full evidence. The
reviewed source version preserves `publisher_expected_size` and pins the
verified SHA-256 while retaining the original source claim. Re-crawling the same
HTML reapplies the same review; earlier builds remain separate. In particular,
SHA-256-only changes also create separate source versions, including when a
source erroneously repeats an MD5. Unresolved payloads and their observations
are retained locally, and publication failures resume from verified local
files instead of fetching the source again. The size-review path bounds ZIPs
to 64 MiB transferred and 128 MiB decompressed; larger discrepancies stay open
for a separate review rather than relaxing the download checks.


### Early Fuzzy2 checksum documents

`python fuzzy2.py --watch` checks saved TYPE-MOON evaluation pages and imports
variants only after retrieving their explicitly linked MD5 documents. The public
checksum text, actual replay URL and document SHA-256 are retained with source
provenance. Audio variants remain separate; rounded sizes are descriptive.
The normal acquisition workers download and publish these checksum-bound items.

Holysealの製品ページは、取得済みURLを起点に別日時のHTMLも調べられます。
`uv run python holy_history.py --watch` はCDX索引から内容の異なる保存版を収集待ちに追加し、
`holy_watch.py --watch` がそのHTMLを解析します。配布欄のない日時だけで取得不能とは判断しません。
索引照会には上限を設け、成功結果をSQLiteに保存し、失敗したURLは未照会分の後で順番に再試行します。

閉鎖ミラーの作品別・メーカー別ページも、`uv run python mirror_history.py --site both --observed --watch`
で別日時の保存版を調査できます。登録済みサイトで実際に観測したURLに限り、内容の異なるHTMLを追加します。
複数サイトを交互に調査し、失敗した索引照会は未照会URLの後で古い順に再試行します。

一時的な取得失敗は `uv run python transient_recovery.py --watch` で再試行できます。
各ミラーURLの最新試行を確認し、HTTP 429・サーバー障害・タイムアウト等を対象にします。
初回は3時間、以降は最大24時間まで待機時間を延ばし、SQLiteの `transient_retries` に再投入理由を残します。
404・チェックサム不一致・HTML応答・恒久的な名前解決エラーだけの配布物は、この処理では再投入しません。

公式サイトで1回のページ上限に達した作品は、`uv run python official_continue.py --watch` で続きを調べられます。
未処理URL・巡回深度・配布種別の手掛かり・訪問済みURLをSQLiteの `official_scans` に保存し、
次のバッチで未処理分から再開します。古い「残り件数のみ」の記録は一度起点ページから再構成します。
