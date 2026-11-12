"""Bounded-disk archival batches; only verified public copies permit eviction."""
import json
from pathlib import Path
import shutil

from archiver import download, now, publish, publish_metadata


def publicly_archived(row, namespace, bucket):
    return bool(row["sha256"] and row["remote_sha256"] == row["sha256"] and row["remote_verified_at"]
        and row["remote_key"] == f"{namespace}/{bucket}/files/{row['sha256']}")


def reuse_verified(store, row, namespace, bucket):
    record = json.loads(row["data"])
    if not record.get("expected_md5") or not record.get("brand"):
        return False
    maker = store.db.execute("SELECT p.manufacturer_id FROM item_catalog c JOIN products p ON p.id=c.product_id WHERE c.item_id=?", (row["id"],)).fetchone()
    candidates = store.db.execute("""SELECT i.* FROM items i JOIN item_catalog c ON c.item_id=i.id
        JOIN products p ON p.id=c.product_id WHERE i.id!=? AND i.md5=? AND p.manufacturer_id=?
        AND i.status IN ('verified','archived') AND i.remote_verified_at IS NOT NULL""", (row["id"], record["expected_md5"], maker[0])).fetchall()
    candidates = [r for r in candidates if publicly_archived(r, namespace, bucket)
        and (record.get("expected_size") is None or record["expected_size"] == r["size"])]
    # A published MD5 is a source association, not a replacement for SHA-256.
    # Only reuse an unambiguous, already fully read-back-verified byte object.
    if len({r["sha256"] for r in candidates}) != 1:
        return False
    prior = candidates[0]
    return store.update_if_current(row["id"], row["data"], status="archived", path=None, md5=prior["md5"], sha256=prior["sha256"], size=prior["size"],
        remote_key=prior["remote_key"], remote_sha256=prior["remote_sha256"], remote_verified_at=prior["remote_verified_at"],
        reused_from=prior["id"], error=None)


def evict_verified(store, ident, namespace, bucket):
    row = store.db.execute("SELECT * FROM items WHERE id=?", (ident,)).fetchone()
    if not row or not row["path"] or not publicly_archived(row, namespace, bucket):
        return 0
    path = store.root / row["path"]
    # Do not delete user files, symlinks or files another unpublished row needs.
    if path.is_symlink() or not path.resolve().is_relative_to((store.root / "files").resolve()):
        raise ValueError("refusing to evict a path outside managed downloads")
    references = store.db.execute("SELECT * FROM items WHERE path=?", (row["path"],)).fetchall()
    if not all(publicly_archived(r, namespace, bucket) for r in references):
        return 0
    size = path.stat().st_size if path.exists() else 0
    path.unlink(missing_ok=True)
    store.db.execute("UPDATE items SET status='archived',path=NULL WHERE path=?", (row["path"],))
    store.db.commit()
    return size


def archive_batch(store, fetcher, namespace="vn-archive", bucket="mirrors", profile="hf", site="both",
                  limit=None, keep_local=False, reserve_bytes=16 * 1024**3, checkpoint_every=10, retry_failed=False):
    stats = {"processed": 0, "published": 0, "reused": 0, "failed": 0, "evicted_bytes": 0}
    fetcher.reserve_bytes = reserve_bytes
    def priority(row):
        record = json.loads(row["data"])
        from catalog import release_info
        kind = release_info(record)[0]
        return (0 if kind in {"trial", "patch"} else (1 if kind == "movie" else 2), record.get("expected_size") or 2**63, row["id"])
    visited = set()
    while True:
        candidates = [r for r in store.rows(site) if r["id"] not in visited and (retry_failed or r["status"] != "failed")]
        if not candidates:
            break
        row = min(candidates, key=priority)
        visited.add(row["id"])
        if publicly_archived(row, namespace, bucket) and row["status"] in {"verified", "archived"}:
            if not keep_local:
                stats["evicted_bytes"] += evict_verified(store, row["id"], namespace, bucket)
            continue
        if limit is not None and stats["processed"] >= limit:
            break
        stats["processed"] += 1
        if reuse_verified(store, row, namespace, bucket):
            stats["reused"] += 1
        else:
            record = json.loads(row["data"])
            required = (record.get("expected_size") or 4 * 1024**3) + reserve_bytes
            if not keep_local and shutil.disk_usage(store.root).free < required:
                for candidate in store.rows():
                    stats["evicted_bytes"] += evict_verified(store, candidate["id"], namespace, bucket)
                    if shutil.disk_usage(store.root).free >= required:
                        break
            if shutil.disk_usage(store.root).free < required:
                raise OSError("insufficient disk space; verified public files retained, partial transfers preserved")
            if download(store, fetcher, site, ident=row["id"], export=False):
                stats["failed"] += 1
                continue
            # Publishing failures stop the batch; do not fill disk with a queue
            # that cannot be uploaded. Source failures above remain retryable.
            stats["published"] += publish(store, namespace, bucket, profile, site, ident=row["id"], metadata=False)
            if not keep_local:
                stats["evicted_bytes"] += evict_verified(store, row["id"], namespace, bucket)
        print("batch", now(), json.dumps(stats), flush=True)
        if (stats["published"] + stats["reused"]) % checkpoint_every == 0:
            publish_metadata(store, namespace, bucket, profile)
    publish_metadata(store, namespace, bucket, profile)
    return stats
