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

## Reuse after the runner discovers full content identity

The promotion step now implements the selected compatible-proof reuse rule:
a canonical object that already exists with the expected size can reuse an
accepted SQLite proof matching full SHA-256, MD5, exact size and canonical key.
Quarantined, unaccepted or inconsistent donor records cannot supply the proof.
A newly copied/recreated canonical object always receives a new independent
public read-back, even if an old database row remembers that key.

This applies after validating the current source's frozen metadata and terminal
runner receipt, not by guessing that matching manufacturer/filename means equal
bytes. Donor manufacturer does not constrain cryptographically equal content.
The existing immutable-key contract is the same one used by bulk checksum
recovery. Preserve the donor's actual remote_verified_at and reused_from; do not
claim a fresh network verification occurred. Completion, source CAS and exact
staging cleanup still run. This avoids repeating full body reads when concurrent
source observations converge on an already accepted byte object.


## Recovery independent of catalogue scheduling

A source can become public through checksum reuse while an earlier delegated
journal still needs promotion or staging cleanup. Ordinary batch scheduling
skips already public, evicted rows, and bounded shard workers can finish with
other retained journals still queued. Recover those operations independently:

```sh
uv run python single_fetch_recovery.py --root archive --limit 10 --timeout 30
uv run python single_fetch_recovery.py --root archive --watch --interval 120
```

The recovery pass does not start a new origin download. It takes the existing
source claim nonblockingly, resumes dispatched job identities or staged
promotion, and retries exact staging cleanup. A spec that was never dispatched
is aborted rather than dispatched by recovery. Metadata changes after acceptance
cannot make cleanup fall through to a new transfer. Active source/object owners
and uncertain job observations stay untouched for the next pass. Counters report
finished retained operations, not newly published unique files.

The watch supervisor uses a fresh child per bounded pass, with separate watch
and pass claims. Capability URLs and upload IDs remain in private journals;
recovery output contains counters and exception types only.


## Files above 2 GiB: measured need and proposed rollout

The 2026-11-15 HEAD audit found a 2,577,562,464-byte official LOOPERS PLUS
trial. `runner_file_upload.MAX_BYTES` is currently 2 GiB, so that file retains
the ordinary host path. Two separate official trials, 908,505,423 and
1,226,234,475 bytes, have been dispatched as bounded pilots; their complete
source/upload/public-read-back timings are not yet available. HEAD establishes
only transport size and availability, never content integrity.

The proposed next worker limit is 4 GiB, retaining the 8 MiB part size, one
part payload in worker memory, 1 GiB runner disk reserve, 20-minute workflow and
30-minute capability limits. The host still requires full independent canonical
read-back before accepting SQLite proof. This is a bounded rollout proposal,
not a statement that larger transfers already work.

Alternatives considered:

* Retain the host path for all larger files. It works with existing resumable
  disk files and avoids a worker rollout, but keeps the measured slow host S3
  route for the larger trials.
* Advance the existing `archive-runner` branch. Already running jobs keep their
  commit identity, but a prepared, undispatched journal can freeze the old head
  and dispatch the new one. Avoid introducing that mismatch during collection.
* Select a new immutable worker branch for new operations, while accepting the
  exact saved legacy refs for retained jobs. This preserves their frozen commit
  checks and requires no interruption of active sources. This is the selected
  rollout approach if the larger-file pilot proceeds.

Before dispatching a larger pilot, validate a 512-part specification with
realistic synthetic SigV4 capabilities against both the 1 MiB decoded and
60,000-character encoded bounds, then test that oversized bodies and invalid
part sizes still fail before publication. Keep tests and worker packaging green.
Create the new pinned worker ref only at the tested commit. Do not move the old
ref or rewrite any saved job head, nonce, upload ID or dispatch intent.

Remaining concerns are source speed within the existing job deadline, the
encoded input size and full-public-read-back latency. Observe the current
908 MB / 1.23 GB pilots first; they do not block metadata discovery or other
body collection. The implementing agent will choose and dispatch the actual
above-2-GiB pilot after the specification bounds and tested-ref rollout are
verified. Acceptance additionally requires source hash/size checks, server part
acknowledgements, canonical read-back, SQLite CAS and staging cleanup. Pages
visibility remains a separate publication gate. Limits above 4 GiB and longer
job/capability lifetimes remain undecided until that evidence exists; they are
not prerequisites for implementing the bounded 4 GiB path.


Implementation update: the worker validates up to 4 GiB and new jobs select
`archive-runner-v2`. The previous `archive-runner` and saved `main` jobs remain
permitted with their original exact head checks. The old ref is not advanced.
A realistic synthetic 512-part SigV4 spec encoded to 34,572 characters and
316,450 decoded bytes, within the existing bounds. Boundary, rejected-before-GET,
legacy-resume and frozen-head rejection tests pass. The worker emits source byte
progress and acknowledged selected-part byte counts without source or capability
URLs; its memory buffering remains unchanged.

The 908,505,423-byte official trial completed runner origin download in 349.299 s
and upload in 33.875 s, then passed host public canonical verification and staging
cleanup. A transient receipt request failure was recovered using the same job,
without a second source download. Its accepted proof is in SQLite. The 1.23 GB
trial remains queued; it has not supplied another throughput result. The first
completed pilot and bounded-memory code permit the 2.58 GB pilot to proceed
without waiting for that second result. At the measured rates its transfer could
approach the unchanged 20-minute job deadline; that timing remains a pilot risk,
not a guarantee. A terminal timeout must abort the exact upload before any later
source attempt, and no source is accepted from partial transfer progress.


Large staged control operations now use a bounded 600-second S3 read timeout
when the frozen transport size exceeds 2 GiB; smaller operations retain 60
seconds. The 2,577,562,464-byte pilot returned all 308 acknowledged parts, but
host completion hit the former read timeout before a staged object appeared.
Longer completion/copy waiting preserves that same upload and receipt. It does
not extend worker GET/PUT timeouts, workflow lifetime, capability expiry or the
workflow observation window, and is not itself an acceptance proof. Recovery
selects this client from the saved size rather than mutable source metadata.


## 継続取得の巡回位置

`delegated_archive.py` は、既存の通常取得と並行して新規ファイルの委譲取得を進める。
配布元ごとにSQLiteの `delegated_scan_cursor` に巡回位置を保存し、体験版・パッチ、動画、その他の順で巡回する。
各パスは既定で最大100件のIDだけを読み、最大4件の転送を試す。メタデータ照会だけで終わった候補は、この転送枠を消費しない。
照会だけで終わった処理の合計が60秒に達したら、新しい候補の走査を次のパスへ持ち越す。進行中の照会や転送は中断せず、ファイル転送と公開検証にかかった時間はこの60秒に含めない。
先頭が非対応、使用中、失敗でも次の候補へ進み、末尾まで進むと先頭へ戻る。
ネットワーク処理の前に読み取りカーソルを閉じ、巡回位置をコミットする。

```sh
uv run python delegated_archive.py --site dmm --watch
uv run python delegated_archive.py --site dlsite --watch
```

監視はパスごとに新しい子プロセスを起動する。
同じサイトの巡回は専用ロック、ファイルの取得は既存の主キー・同一内容・公開オブジェクトのロックで保護する。
ローカルに途中ファイルがある候補と関連付けを保留した候補は触らない。
委譲に非対応の候補は通常取得に残し、この処理自身はホストで本体を取得しない。
ジョブの観測がタイムアウトしても同じ状態を保持し、`single_fetch_recovery.py` が元の配布元への再取得なしに処理を継続する。
この巡回位置は取得完了の証明ではない。完了は既存の公開URLからの全量ハッシュ検証とSQLiteへの受理で判定する。

Actions側の全ミラー試行が終了し、対応するmultipartの中止が確認できた候補は、失敗記録を残したまま通常取得へ戻す。
`delegated_host_routes` に元のメタデータのSHA-256と最後のジョブIDを記録し、同じ観測をこの巡回から再委譲しない。
元データが変更された候補は再検討する。終了を確認できないジョブや不明な中止結果は通常取得へ戻さず、同じジョブの復旧処理に残す。


## 5 GB boundary and a third immutable worker release

A live SQLite audit on 2026-11-15 found one pending known-size body above
4 GiB: `kakyuusei_re_trial.zip`, 4,849,670,669 bytes. It has no managed partial
and no current source claim. Three completed LatherCraft bodies of
2,304,018,588 / 2,270,730,862 / 2,255,431,663 bytes took respectively
110.072 / 151.621 / 153.456 seconds to download and 250.276 / 165.159 / 84.448
seconds to upload. These measurements support a bounded larger pilot; they do
not prove its completion time or public acceptance.

Select a maximum of exactly **5,000,000,000 bytes**, not 5 GiB. Keep 8 MiB
parts, the runner's 1 GiB disk reserve, 20-minute workflow and 30-minute
capabilities, and existing independent public hash/size verification. This
also stays within the documented [S3 single-operation copy boundary](https://docs.aws.amazon.com/AmazonS3/latest/API/API_CopyObject.html).
HF behavior at this size remains a live verification gate, rather than a
claim derived from AWS documentation. A maximum-sized spec has 597 parts and
must pass both encoded/decoded dispatch limits with synthetic real SigV4 URLs.

Alternatives are retaining the slow ordinary host path, or introducing
streaming/ranged workers and multipart server-side copy for larger objects.
The ordinary path stays available. Streaming/ranged acquisition is unnecessary
for this measured gap and would add new source-version and recovery contracts.
Do not raise the bound above 5 GB until a separate design establishes copy,
source identity, capability and runtime limits for the actual larger cohort.

Use a detached worktree for implementation and tests. Publish the tested
`archive-runner-v3` ref before making it the default in the running checkout.
Keep `archive-runner`, `archive-runner-v2`, and saved `main` journals on their
original exact heads, without redispatching or rewriting their identities.
The pilot must additionally prove sufficient runner disk, origin digests/size,
part acknowledgements, completion/copy, public read-back, SQLite CAS and cleanup.
Queue/source delays may still exceed the existing budget; retain the same
journal on observation timeout and use the ordinary route after confirmed
terminal failure. There is no implementation blocker after boundary tests and
pinned-ref checks pass. The implementing agent will select the actual pilot
only after these gates; public acceptance and Pages visibility remain unproven
until separately observed.

Boundary-test result: a synthetic real-SigV4 specification for exactly
5,000,000,000 bytes contains 597 parts, 40,108 encoded characters and 368,980
decoded JSON bytes. Its round-trip succeeded within the existing 60,000 /
1,048,576 limits. Both old pinned branch heads were independently checked before
creating v3; neither is moved as part of this rollout.


## Bounded probes and recovered mirrors

A Holyseal candidate had 22 observed mirror URLs. Three archived URLs occupied
the entire unknown-size HEAD budget while an already observed Suezo URL, whose
host passed the existing three-hour health check, was last. Delegate selection
now applies the same fresh-health evidence as ordinary acquisition before
spending that unchanged three-probe budget; primary LatherCraft storage keeps
its existing preference. Only the order of already observed URLs changes.
Healthy host state neither adds new live URLs nor proves the body exists or
its content. Stale health cannot promote a mirror. All published constraints,
full worker checks, canonical public read-back and source claims still apply.
Increasing the probe budget would spend more time on dead historical URLs;
using unlisted live URLs would change the source authorization contract.
Neither is required to fix the observed scheduling gap.

The 4,849,670,669-byte pilot subsequently completed on `archive-runner-v3`
(commit `86505bddb699b7736f2aa58a9e2da711cbcdd085`, run `43657944663`).
Origin download took 230.066 s and upload 528.028 s. Complete/copy and an
independent full canonical public read-back passed; SQLite accepted SHA-256
`cb63cd0c5d8e578206070e6c01e263dc9c1f924b10f7f2009aabb359c53de6a2`
at 2026-11-15T01:10:26.902859Z, with matching publisher constraints and staging
cleanup. End-to-end observation including queue/control/read-back took
2,813.525 s. Host peak RSS was 82,384 KiB; final swap was 15,544 KiB. This is
real evidence at 4,849,670,669 bytes, not a live test of every size through the
5,000,000,000-byte boundary. Its Pages link is a separate pending generation.


## Finite passes across remaining sources

`delegated_sources.py` runs explicit source keys through the existing bounded
per-source acquisition in at most two fresh child processes. It visits all
selected sources once before its next round; each child handles a finite pass
without `--watch`, so later sources are not trapped behind permanent watchers.
SQLite scan cursors, per-source pass locks, primary/alias/object claims, partial
file preservation and normal-route fallback remain in `delegated_archive.py`.

```sh
uv run python delegated_sources.py --site kokoron --site multi_inside --site d_station --site getchu --site moepedia --site official --workers 2 --limit 2 --watch
```

The source coordinator uses one lock per archive root. A failed child or launch
is recorded by source and exception type, and does not prevent later sources
from being visited. Round success means passes finished, not that every body is
archived. Body proof and publication retain their existing independent gates.

Independent permanent watchers for every remaining source would add process
load. Putting permanent watchers into a two-thread pool would starve sources
after the first two. Finite fresh children keep memory bounded and pick up host
code fixes on later visits. Large public read-backs can still occupy a slot for
a while; existing collectors continue in parallel. No timeout kills a child
with unaccepted body work, and no JSON manifest is generated on the host.


## Release idle normal-watcher memory between passes

The host audit on 2026-11-15 found 68 repository processes holding 2,974,364 KiB
RSS and 3,618,144 KiB swapped pages (about 2.84 and 3.45 GiB respectively). Several
legacy normal watchers retained hundreds of MiB while their current source queues
were empty. Existing productive transfers are not interrupted to reclaim memory.

`archive_watch.py --site alice_archives --site alicefree --site hanny --fresh-processes`
retains normal-route acquisition but runs each current queue pass in a fresh finite
`--once` child. The parent owns the existing per-source watch lock, so a replacement
waits for the former watcher to exit. Children close SQLite on success and failure;
failed passes cool down for at least 300 seconds. Parent shutdown waits for a child
to finish its existing pass; it does not kill productive body work. The default
legacy mode remains available. This rollout is limited to observed idle sources;
the broad active collectors remain running with their existing claims.


## Snapshot request bodies and write deadlines

Two independent full-checkpoint attempts ended in ConnectionClosedError. The
underlying exception was TLS write timeout, not read timeout. The installed
urllib3 connection pool applies its connect timeout during request transmission
and switches to read timeout after sending. Snapshot parts were raw bytes, so
urllib3 passed one entire 8 MiB part to socket.sendall on this slow host route.

Use a seekable in-memory stream for each snapshot part. This makes urllib3 send
bounded blocks while retaining exact Content-Length and SDK retry rewind support.
Keep at most two current part payloads and close streams after each request,
including failure. Do not change upload IDs, part numbers, part geometry, stored
acknowledgements, immutable snapshot bytes or the independent public read-back.

Alternatives were increasing connection deadlines for all requests (also delays
actual connection failures) or shrinking multipart geometry (would require a new
generation/upload for already acknowledged parts). The stream boundary fix keeps
the existing transfer identity and ordinary connection policy. Native/delegated
file-body upload adapters retain their existing byte-input contract.

A regression using the installed urllib3 HTTPConnection reproduces the oversized
sendall failure before the change, checks small wire writes afterward, and verifies
exact wire bytes, Content-Length framing, rewind and stream closure. Existing
parallel-resume tests still require successful sibling ACKs to survive a failure.
Live network improvement remains to be measured on the same paused snapshot;
this change cannot guarantee against an independent remote connection failure.


## Explicit historical lookup for paused items

A source may remain pending with a protected partial file even when its live URL
now returns 403. The default Wayback discovery still selects failed items only.
Use `uv run python wayback_recovery.py --site official --id ITEM_ID --limit 1`
to investigate an explicitly selected pending or failed item without fabricating
a failed status or touching its partial body. Repeat `--id` for multiple items.
Completed and verified-local items are skipped, unknown/out-of-site IDs fail
before network access, and only observed source URLs enter the CDX query. The
existing network-query limit, cached-index evidence, exact URL matching and
per-capture timestamps remain unchanged. Source rows are selected in SQLite
without first materializing the entire JSON catalogue.


The first stream-only change still encountered a live ConnectionClosedError.
The SDK pool config overrides urllib3 default blocks with 128 KiB, which the
initial 8 KiB HTTPConnection regression did not reproduce. Snapshot streams now
return at most 16 KiB for positive read requests; read-all, seek, tell and total
Content-Length semantics stay intact for signing and retries. The regression
uses botocore.httpsession.BUFFER_SIZE and fails without this short-read bound.
Live throughput and any remaining independent connection failures remain under
observation; no receive ACK or public-body verification is inferred from tests.

## Header-only Range size discovery (2026-11-15)

A Getchu trial capture returned `bytes 0-0/173277013` for a Range GET. Selecting
the exact observed HTTPS capture with that size let the existing runner fetch
the complete ZIP and the host verify the canonical HF object. A later live check
also obtained the size through HEAD; this evidence does not establish a permanent
HEAD restriction for that source. The Range fallback is a bounded alternative
for unsupported or incomplete HEAD responses, not a requirement for every capture.
The source's missing publisher MD5 remains missing; observed size and measured
hashes are not retroactively publisher claims.

Unknown-size selection still considers at most three observed candidate URLs.
A valid HEAD response is sufficient. Missing Content-Length, HEAD 405/501, or a
connection-refused Wayback capture confirmed in this item's exact-target CDX
candidates may additionally use one `Range: bytes=0-0`
metadata request. The shared Wayback helper may try the same capture on its other
public port after connection refusal, but this applies only to the metadata
probe. The original source URL is retained unchanged in the runner specification.
HEAD 403/404/429/5xx, certificate errors, generic timeouts, HTML, encoded responses,
malformed lengths and oversized bodies do not trigger the extra request.

A Range response must be binary identity encoding and either an exact `bytes
0-0/TOTAL` 206 response (Content-Length 1 when present), or a 200 response with a
bounded complete Content-Length when Range was ignored. Responses close at their
headers; the host never reads their bodies. The normal worker size and publisher
checksum checks, source quarantine, canonical full public read-back, source CAS,
claims and legacy-job resume rules remain unchanged. Range discovery does not
create an available download or waive publisher constraints.

The connection-refused fallback is restricted to the item's registered CDX
candidate URLs. A date copied from a publisher HTML page is not proof that its
binary was captured on that date and must not add a metadata GET for every dead
replay link. This keeps large historical queues from gaining extra requests for
unconfirmed captures. Native HEAD 405/501 and missing-length responses retain
their bounded method/metadata fallback.

## Public Dropbox downloads

The host size probe and the delegated worker share the existing public-download
adapter. Dropbox file shares use the documented `dl=1` transport, preserving
other public access parameters. The publisher's original shared URL remains in
the item, job specification and accepted provenance. Metadata requests close at
headers; measured transport size does not become a publisher size claim. The
worker still validates the complete body before uploading, and promotion still
requires the canonical public body proof.

The worker is pinned at `archive-runner-v5`, commit
`ee5dc951b348eb1d0a0302b20b772ca2f1356868`. This ref was created after 925 local
tests, wheel packaging and CI passed. Existing v1 through v4 refs and saved jobs
retain their original identities; only new jobs select v5.

Dropbox's documented public link mechanism:
https://help.dropbox.com/share/force-download


## Normal file upload write size

Ordinary immutable-file uploads use the same seekable part stream as snapshots.
An 8 MiB multipart part keeps its size, number, SHA-256 and opaque ETag; positive SDK reads return at most 16 KiB so the HTTP client does not send the whole part in one TLS write.
Existing multipart checkpoints remain valid and acknowledged parts are reused on resume.
The delegated-upload adapter hashes seekable bodies in bounded reads and restores their initial position, including when the payload is rejected.
The normal-upload regression test uses the real HTTP connection with a socket that rejects large writes; rewind, exact wire bytes, Content-Length and bounded upload memory are checked alongside resume tests.
