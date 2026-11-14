# Delegated part uploads

Status: finite opt-in part transport implemented and a real archive-file pilot
passed, 2026-11-14. Existing collectors retain their local upload path.

The Suezo-origin `otosen3_trial_setup.exe` (638,504,629 bytes) was refetched in
320.552 seconds and uploaded as 77 accepted parts in 77.169 seconds, run
43591699596. The host completed the upload and verified the complete anonymous
public read-back against SHA-256 before updating SQLite. Pages visibility remains
a separate pending catalogue-publication check.

## Evidence and intended outcome

The same 45,117,424-byte public file took 1,617.36 seconds through the local
resumable uploader and 5.125 seconds of part sending on a GitHub runner. The runner
also downloaded it in 1.101 seconds. Run 43586870205 returned six parts, which the
host independently matched against S3 ListParts, then aborted. Existing public
object size and ETag remained unchanged. Its private receipt is
`archive/.work/transfer-probe.json`. A subsequent host comparison using the same
presigned-URL/urllib transport timed out after 20.381 seconds on an 8 MiB part;
that upload was also aborted. This isolates a host-side/route effect beyond the
original SDK framing, but does not identify a particular ISP or server policy.

A useful next integration is one real verified local file, explicitly selected,
with a known successful public source URL. The runner refetches that exact source
and verifies the locally established SHA-256 and size before sending any part.
The local database remains the only source of truth. Do not send entire local
files through repository artifacts or publish credentials to a relay.

## Proposed boundary

* Keep the existing per-source and per-object claims around publication.
* Freeze the source metadata identity, local path, SHA-256, MD5, size and successful
  source URL before dispatch. Quarantined sources remain excluded. Ineligible
  schemes or sources without a successful URL use the ordinary uploader.
* Keep CreateMultipartUpload, CompleteMultipartUpload and AbortMultipartUpload on
  the host. Give a runner only expiring, size-bound UploadPart capabilities for
  that one upload. Never send reusable S3 keys. Read inputs from the event file,
  and never interpolate capabilities into shell commands or printed environment.
* Preserve the existing private multipart checkpoint and part size. Validate
  reused parts by local SHA-256 plus server size/opaque ETag. Delegate only missing
  parts; overwriting reused parts could invalidate ETags already chosen for
  completion. A partial checkpoint must remain compatible with local resume.
* Download the complete source into a bounded temporary file before any part PUT.
  Require the whole expected SHA-256 and exact size. A newer file at the same URL,
  an error page or an IP-restricted source must fail without sending its bytes.
  Use the project's pinned TLS/source fetch rules for supported public mirrors.
* Return only a receipt: input identity, file digest/size, selected part numbers,
  part SHA-256 values, sizes and ETags, plus timings. Artifacts contain no payload,
  capability URL, upload ID or credentials. Cap pilot size and job duration.
* Before accepting each delegated result into the existing multipart cache,
  compare its part SHA-256 with the actual local payload and its size/ETag with
  the server. Then let the existing helper assemble completion, perform full
  anonymous public read-back and CAS the source proof. A runner success alone
  must never create a download link or evict a local body.

## Integration seam

Prefer an opt-in client adapter around the current multipart helper rather than
another completion or eviction engine. At the first missing `upload_part`, dispatch
one bounded job for all missing parts. Keep its result privately. For each later
helper call, compare the actual local part payload SHA-256 with the receipt and
return only the independently confirmed server ETag; the current helper then writes
its ordinary checkpoint. Never rewrite parts the helper already reused. Keep the
normal host completion, anonymous whole-object read-back and source CAS unchanged.
Require an explicit source ID for the initial CLI option. Do not turn this on for
all watchers before a real eligible source passes the complete end-to-end gate.
The diagnostic currently limits its source to an existing HF object and 64 MiB;
production refetch uses the separate bounded runner-file workflow with
public-source/TLS checks and the resource bounds below.

## Alternatives and trade-offs

Local-only upload is simpler and remains the fallback, but the measured host path
is hundreds of times slower for this file. Delegated refetch avoids that outgoing
path without registering reusable credentials, at the cost of another source
read, workflow scheduling and a bounded temporary runner file. It only benefits
sources that the runner can access. Direct native HF upload remains unmeasured
without an HF user token. An additional public file relay introduces another
service and data path and is unnecessary for this first integration.

## Failure and resume requirements

Persist the dispatch nonce, upload ID and run handle privately before relying on
acknowledgements. Poll that same run after observation errors; never redispatch
merely because polling timed out. A queued or temporarily invisible run is not
terminal. Do not fall back to local writes on the same upload while a delegated
job can still change parts. Confirm cancellation/terminal state first, or abort
that exact upload to invalidate its capabilities before retiring its checkpoint.
Keep the verified local body throughout. A failed source refetch must not change
source metadata or claim another build. Existing accepted parts may only be
retained when their local and server proof remains valid.

## Implemented pilot bounds

The CLI is `upload --id SOURCE_ID --runner-upload --files-only`. It requires a
verified local body no larger than 2 GiB and a successful original HTTP(S) URL.
Part capabilities expire after 30 minutes; the job is limited to 20 minutes and
the host observation window to 25 minutes. Only receipt artifacts are retained
for one day. This option does not regenerate the full catalogue JSON.

The private multipart phase is `delegated` until the job is terminal and all
selected parts match local bytes and server acknowledgements. Their checkpoints
and the return to `active` are one SQLite transaction. A local client must defer
while a live delegation remains. On controller errors the exact upload is aborted
to invalidate its capabilities; normal resume handles NoSuchUpload before creating
a replacement. A hard-killed controller resumes the same private job journal with
the opt-in command. A metadata CAS failure or lost host-completion acknowledgement
retains accepted bytes and resumes through the existing public-read-back path.

## Pilot decisions and gates

Start with one manual file and no scheduled bulk runner collector. Before raising
limits or enabling another lane, measure its source-download time, queue delay,
accepted-part throughput, total elapsed time and resource cost. The implementing
agent must select a currently unclaimed eligible local body and settle the bounded
production size/deadline and cancellation path before dispatch. These are technical
implementation gates, not missing authorization to continue existing archival work.
Tests must cover changed source bytes, partial checkpoints, ETag mismatch, lost
acknowledgements, still-running jobs, safe fallback and metadata CAS conflicts.
Finally prove a real newly published object with complete public read-back and
actual Pages visibility before declaring that integration complete. Continue all
other named sources through their existing paths throughout the pilot.
