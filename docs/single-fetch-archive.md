# Single source fetch for delegated archival

Status: design selected; staging worker and combined public hash read-back are
implemented and unit tested. Staged completion, conditional promotion, public
proof and source CAS are implemented and unit tested in `staged_archive.py`.
Durable job orchestration is implemented in `single_fetch.py`, including source
and equivalent claims, frozen-spec identity, timeout resume, failed-run abort
and promotion recovery. Opt-in batch integration (`archive-batch --single-fetch` and selected-site
`archive_watch.py --single-fetch`) is implemented. Production migration remains.
Existing collection stays active during implementation. This replaces the double
origin read for new eligible files; existing verified-local upload remains usable.

## Evidence and requirements

The current runner route first downloads locally, then refetches on Actions. In a
500,533,525-byte job, runner download took 29.727 s and upload 36.567 s; serial host
download/read-back and duplicate source observations dominate elapsed time.
SQLite remains authoritative. Pages keeps one generated JSON. Canonical objects
remain `files/<sha256>`. Never send reusable S3 credentials to a runner.

HF documents same-namespace CopyObject and copy-source If-Match support:
https://huggingface.co/docs/hub/storage-buckets-s3#other-api-differences
A real 19,760-byte object was copied in 0.269 s, independently read back with the
expected SHA-256, and an incorrect source ETag was rejected. Temporary probe
objects were deleted. Local evidence: `.work/single-fetch-copy-capability-20261115.json`.
Combined public read-back also matched the existing MD5, SHA-256 and size for
that 19,760-byte object using one public request. Local evidence:
`.work/single-fetch-combined-readback-live.json`. Existing SHA/size-only callers
retain their tuple interface and do not compute an unused MD5.

## Selected flow

1. Freeze the source record/data identity. Claim its source and known equivalent
   observations before any origin body read. A source checksum/manufacturer claim
   coordinates new controllers; existing per-source locks protect older workers.
2. Use a published exact size, or bounded public HEAD, for transport limits. A
   rounded size caption is never a validation size. Start with the existing 2 GiB,
   20-minute runner and 30-minute capability bounds; unsupported sources continue
   through the ordinary single-download host path.
3. Create a unique `staging/<64 lowercase hex nonce>` multipart upload. Give the
   runner only size-bound, short-lived UploadPart URLs for this exact upload.
4. Fetch the source body once on the runner. Reject HTML and encoded responses;
   check exact transport size and every published MD5/SHA-256 constraint before
   PUT. Compute both hashes, including when a publisher supplied neither. Return
   hashes, acknowledged part sizes/ETags/hashes and timings, without capabilities,
   reusable keys or body artifacts. Source fetch adapters must retain supported
   TLS, referer and public-download handling.
5. The host validates the trusted workflow commit, frozen record identity, exact
   part selection and server ListParts acknowledgements, then completes staging.
   A job receipt is not publication proof. Persist each acknowledged transition.
6. Claim `files/<computed sha256>`. Reuse a compatible accepted proof when present;
   otherwise copy the staged object with CopySourceIfMatch to that canonical key.
   Perform one full public canonical read-back computing MD5, SHA-256 and size.
   Require canonical SHA identity and all publisher constraints before source CAS.
7. Commit the accepted proof in SQLite. Delete only this operation's exact staging
   object after its multipart capabilities are invalidated by completion/abort.
   Metadata publication and Pages remain independent of body transfers.

The staging body is unadvertised and never appears in manifest download links.
One successful attempt has one full origin fetch, no host source-body copy, and
one canonical public read-back. HEAD/redirects and failed-attempt retries are not
additional successful full-body downloads.

## Resume and ownership

Private journals, not the public catalogue, hold nonce, staging key/upload ID,
source identity/URL, bounded capabilities, workflow run/head identity and part
receipts. Write dispatch intent before dispatch. Observation failure is not a
terminal job: resume its handle, never start a replacement on timeout alone.
Abort that exact upload before abandoning any still-capable worker. Never abort
an upload owned by another source/controller. A completed staged body can resume
promotion without refetching its origin. A lost copy/completion acknowledgement
requires object inspection and read-back; source CAS is the final acceptance gate.

On checksum mismatch or corrupt public read-back, leave the source unaccepted.
Do not delete or overwrite another accepted canonical object. On source metadata
change, retain valid content proof but do not assign it to the changed observation.
Preserve the exact cleanup handle until staging deletion has been confirmed.

## Alternatives and decision

* Keep delegated refetch: already tested and easy to resume, but repeats origin
  traffic and host storage. Retained only for pre-existing local bodies.
* Host-only download/upload: one origin read and simpler ownership, but measured
  host S3 throughput is poor. Retained for ineligible protocols/large files.
* Reusable S3 credentials on Actions: simple direct upload, but unnecessary broad
  authority. Rejected in favor of existing per-upload capabilities.
* Selected staging/copy flow: supports MD5-only and hashless publisher pages while
  deriving canonical SHA on the runner. Accepts extra temporary-object lifecycle
  work and a server-side copy to eliminate the initial host body download.

## Risks, open work and release gates

Copy semantics passed the small live test; larger-object promotion and recovery
remain unverified. Test receipt/server disagreement, source mutation, lost dispatch
and completion acknowledgements, still-running workers, source CAS changes and
cleanup failure. No source link may become downloadable from runner success alone.

Implementation tasks: staging-capability validation and runner fetch; combined
public MD5/SHA read-back; durable host coordinator; batch selection and cross-site
claims; fallback/mirror behavior; actual single-fetch pilot and Pages visibility.
These are implementation gates, not requests for further user approval. Existing
collectors remain active until a real candidate passes all gates. Do not claim the
entire integration complete from the small copy probe or unit tests.

Runtime bounds remain at the proven pilot limits initially. Raising size or
concurrency is deferred until the implementing agent measures source time, queue
wait, upload/read-back time, disk and peak memory on the real single-fetch path.
That decision does not block implementing the bounded path.

## First real single-fetch proof

The 14,288,847-byte `yakin_re_update1.0.1.zip` patch passed the complete pipeline
on workflow run 43626025396 at commit 9ea3d8d. Runner origin GET took 5.136 s,
part upload 0.770 s. Public canonical read-back accepted publisher MD5
`ff9d9ebd5a39e8a33752db374b2dd923`, SHA-256
`0714717221f2162534acc6db4ff28dcbcc86764f6216dc239d81ae69bb38b287`
and exact size. SQLite has archived status and no local path; canonical HEAD
matched and the exact staging key returned missing after cleanup. Safe evidence:
`.work/single-fetch-pilot-verified-20261115.json` (under the archive root).
The earlier STORAGE2 attempt ended in URLError after 60 seconds and its known
multipart upload was aborted before trying observed STORAGE1. This is evidence
for the new route, not proof that all remaining sources or Pages are complete.

The opt-in batch route uses published exact sizes initially. Unknown-size,
protocol/adapter-only and existing host partials retain the normal host route.
That fallback does not enable delegated refetch after the host download. An
observation timeout preserves its journal and defers the source without falling
back to a second origin downloader. Existing collectors keep their previous
behavior until explicitly migrated after bounded real batch validation.

Terminal runner failures now enter SQLite attempts with the observed source URL
and job identity. Only acknowledged exact-upload abort permits mirror failover.
One pass attempts each eligible observed URL at most once; exhausted sources are
marked failed without stopping independent files. An uncertain abort, observation
timeout or failed receipt retrieval cannot start a fallback origin download.

Unknown publisher sizes can now use a HEAD-only probe through the existing
Fetcher (same URL encoding, TLS pins, rate limits and identity encoding). Only
HTTP 200, a positive bounded integer Content-Length and non-HTML/unencoded
headers permit the runner route. The observed size is a transport bound; it does
not populate expected_size or constitute content proof. HEAD refusal/missing or
unusable length retains the normal host download route without a host probe GET.

HEAD discovery is capped at three observed candidate URLs per selection before
normal host fallback. Dated binary URLs already recorded in wayback_candidates
are included and accepted as observed source URLs; no replay dates are invented.

The first HEAD-bound official-site pilot (Waffle `setup_2.lzh`, 74,924 bytes,
workflow run 43627913576) passed the same full canonical public read-back and
SQLite acceptance with no publisher size and no local body. Origin fetch took
3.670 s and delegated upload 0.434 s. Safe evidence:
`.work/single-fetch-head-official-verified-20261115.json`. This does not establish
that every official source supports HEAD; refused/invalid HEAD retains fallback.

## Stable runner source reference

A real Suezo job was safely aborted when a main-branch push raced dispatch: its
run head differed from the frozen expected head, delaying the service restart.
New jobs now bind both dispatch_ref and head_sha to the dedicated archive-runner
branch, initially provisioned at tested commit 4bbcb0cc661f990c983bf1f03fcf0cafff7d2a14.
Main-branch changes no longer move that reference. Existing journals without
explicit dispatch_ref retain main and their original identity checks; do not
rewrite an already dispatched job onto a different reference. A changed runner
head still aborts the exact upload rather than accepting a mismatched receipt.

GitHub documents workflow_dispatch ref as a branch or tag name:
https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event
Do not assume a raw commit SHA is a supported dispatch reference. The separate
worker branch is deliberately updated only for a tested worker release after
its pending/running delegated jobs have drained. It is not automatically moved
by main pushes. Runner-source updates must retain this release gate and inspect
actual workflow run head identities during rollout.
