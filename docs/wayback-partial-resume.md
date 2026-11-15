# Wayback partial-file transport recovery

Two real URL-bound trial prefixes (354,418,688 and 283,115,520 bytes) had been
preserved while one public Wayback port was refused. On 2026-11-15, same-URL
Range probes returned the exact requested offsets and complete sizes of
386,838,020 and 493,430,161 bytes. Exact-target CDX lookups retained their own
capture dates and whole-file SHA-1 evidence in SQLite. Recovery copies protect
the original prefixes; final acceptance still requires the publisher MD5.

Previously the outer download failover selected an alternate transport URL,
which selected a different partial-file name. The original prefix remained on
disk but was not resumed through the reachable port. For an already resumable
prefix, transfer now retries the same request on the other public Wayback port
inside the original transfer. The checkpoint remains bound to its original URL;
Range and If-Range headers, capture timestamp, original path and query are kept.
HTTP to HTTPS is allowed after connection refusal. HTTPS to HTTP additionally
requires an independent publisher checksum for the complete file. Certificate
errors and HTTP denials do not trigger this retry.

This does not merge prefixes between capture dates, original URLs, or files.
Zero-offset downloads retain their existing failover and audit behavior. Range,
validator, size and whole-file checksum checks are unchanged, and an interrupted
alternate response resumes the same checkpoint on the next call.
