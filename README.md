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

`manifest.json` is the **single JSON export**. It contains manufacturers, products,
files, nested sources/attempts, and saved-page records. It is generated atomically
from SQLite, ignored by Git, and never edited or imported as the source of truth.
There are no separate JSONL or search catalogs.

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

Downloads stream to partial files and resume only with Range/If-Range validators.
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
The catalog contains 11 manufacturers, 13 products and 28 source-derived file
entries. Three files are publicly verified: this Holyseal patch and a LatherCraft
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
individually; an archived page is not proof of a recoverable binary. This project
has not yet completed a full crawl of either source.

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

`python wayback_recovery.py --limit 20` checks failed Holyseal downloads using
the CDX index for each original binary URL. All returned successful capture dates
are retained in SQLite, including repeated digests: a broken replay at one date
must not hide another date. HTML captures and unrelated paths are excluded.
Index digests are provenance, not a replacement for publisher MD5 validation.
`--refresh` queries successful cached index lookups again. Empty results and
request failures are recorded separately. Results reaching the 10,000 capture
limit are marked failed, never reported as an exhaustive search.
After discovery, `archive-batch --site holyseal --retry-failed` tries alternate
dates (newest first), checks file contents, publishes and reads back the result.
