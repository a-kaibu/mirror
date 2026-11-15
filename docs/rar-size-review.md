# Evidence-backed RAR size review

DALK_TW.RAR is published as 104,857,600 bytes but the observed complete source is
437,482,960 bytes. Its full SHA-1 matches two 2010 CDX captures. An operational
read-only inspection decoded three members (668,411,633 bytes) and independently
matched their RAR4 CRCs. Independent historical body retrieval is still pending.
CDX length is a stored WARC length and is not the original response byte count.

Keep ordinary published-size/checksum checks. Extend the existing explicit
size review to RAR4 only after two independently retrieved full copies match
size, MD5 and SHA-256, and the full SHA-1 matches the exact observed CDX target.
Retain publisher metadata and its HTML fingerprint in SQLite, create a corrected
source version, and require ordinary public HF read-back before acceptance.

Alternatives: leaving the record failed preserves the discrepancy but cannot
finish preservation. Ignoring published size admits unverified variants. The
selected explicit review accepts extra body reads and an optional native decoder
dependency to retain both provenance and independently checked content identity.

The native decoder runs in a fresh POSIX process: 512 MiB address space, 180 CPU
seconds, 240-second alarm and 250-second parent timeout. Compressed input is capped
at 512 MiB, decoded data at 1 GiB and members at 10,000. Header CRCs and decoded
member CRCs are compared explicitly; successful libarchive reads alone did not
reject a deliberately corrupted stored member in the operational fixture test.
Only plain RAR4 main/file/end headers, regular files and directories are supported.
Split, encrypted, nonregular or unsupported archives fail closed. No extraction
or execution occurs. Existing ZIP reviews keep their current limits and behavior.

Remaining risks: libarchive availability and decoder limitations can prevent this
optional review. Parser/decoder size and entry counts must agree. This does not
verify game installation or behavior. RAR5, Windows review support and automatic
large RAR discrepancy collection remain undecided until an observed source needs
them; they do not block this RAR4 review or ordinary downloads on those platforms.

Gates: normal, corrupt-data, corrupt-header, truncated, split/encrypted and expansion
bound tests; review checksum/metadata-CAS tests; full existing suite and wheel;
actual independent historical SHA-256 plus RAR CRC proof; HF full-body verification
and later Pages-link verification. The live historical copy is not yet proof.
