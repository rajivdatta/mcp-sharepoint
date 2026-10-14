"""The indexing pipeline: SharePoint -> parse -> chunk -> SQLite -> vectors.

Kept out of server.py so it can be run headless (`python pipeline.py <folder>`)
for a first bulk crawl, which is the slow part, and then scheduled.
"""
from __future__ import annotations

import json
import sys
import time

import embed as embedlib
import extract
import graph
import index as idx

# Files we never download: no text to get, or a parser we do not ship.
SKIP_EXT = {"png", "jpg", "jpeg", "gif", "bmp", "tif", "tiff", "svg", "heic",
            "zip", "7z", "rar", "gz", "mp4", "mov", "avi", "mp3", "wav", "exe",
            "dll", "msi", "pbix", "twbx", "vsdx", "mdb", "accdb", "bak"}


def classify_item(it: dict) -> str:
    return extract.classify(it.get("ext", ""))


def index_folder(folder: str = "", site: str | None = None,
                 library: str | None = None, recursive: bool = True,
                 max_files: int = 2000, max_file_mb: int = 40,
                 embed_backend: str = "auto", reindex: bool = False,
                 rows_as_chunks: bool = False, time_budget_s: int = 1800,
                 prune_deleted: bool = True, progress=None) -> dict:
    """Crawl a folder and (re)build its slice of the index."""
    t0 = time.time()
    drive_id = graph.resolve_drive(site, library)
    files, folders = graph.walk(drive_id, folder, recursive, max_files)

    counts = {"seen": len(files), "indexed": 0, "skipped_unchanged": 0,
              "skipped_type": 0, "skipped_too_big": 0, "failed": 0,
              "chunks": 0, "risk_files": 0}
    failures, seen_ids = [], set()

    for it in files:
        seen_ids.add(it["item_id"])
        if time.time() - t0 > time_budget_s:
            counts["stopped_on_time_budget"] = True
            break
        ext = (it.get("ext") or "").lower()
        kind = classify_item(it)

        if ext in SKIP_EXT or kind in ("image",):
            idx.upsert_file(it, drive_id, kind, "catalogued")
            counts["skipped_type"] += 1
            continue

        _, etag, status = idx.existing_etag(drive_id, it["item_id"])
        if not reindex and etag and etag == it.get("etag") and status == "indexed":
            counts["skipped_unchanged"] += 1
            continue

        if (it.get("size") or 0) > max_file_mb * 1024 * 1024:
            idx.upsert_file(it, drive_id, kind, "too_big",
                            error=f"{round(it['size']/1e6,1)} MB > {max_file_mb} MB")
            counts["skipped_too_big"] += 1
            continue

        try:
            raw = graph.download(drive_id, it["item_id"],
                                 max_bytes=max_file_mb * 1024 * 1024)
            chunks, profile, meta = [], None, None

            if kind == "structured":
                profile = extract.profile_structured(raw, ext, it["name"])
                card = extract.schema_card(profile, it["name"], it["path"])
                chunks.append({"text": card, "page": None, "chunk_type": "schema"})
                if rows_as_chunks:
                    for t in profile.get("tables", []):
                        for row in t.get("sample", [])[:50]:
                            chunks.append({
                                "text": f"{it['name']} :: {t['name']} :: "
                                        + "; ".join(f"{k}={v}" for k, v in row.items()),
                                "page": None, "chunk_type": "row"})
                body = card
            else:
                doc = extract.extract_text(raw, ext, it["name"])
                meta = doc.get("meta")
                chunks = [dict(c, chunk_type="text")
                          for c in extract.chunk_pages(doc["pages"])]
                body = "\n".join(p["text"] for p in doc["pages"])[:400_000]

            risks = extract.scan_risks(body)
            file_id = idx.upsert_file(it, drive_id, kind, "indexed",
                                      profile=profile, meta=meta,
                                      risks=risks or None)
            idx.replace_chunks(file_id, chunks)
            counts["indexed"] += 1
            counts["chunks"] += len(chunks)
            if risks:
                counts["risk_files"] += 1
        except Exception as exc:
            idx.upsert_file(it, drive_id, kind, "failed", error=str(exc)[:400])
            counts["failed"] += 1
            failures.append({"file": it["name"], "path": it["path"],
                             "error": str(exc)[:200]})
        if progress and counts["indexed"] % 25 == 0:
            progress(counts)

    # Only prune when the crawl actually saw the whole subtree. A walk cut short
    # by max_files or the time budget would otherwise delete every file it never
    # got to - the index would silently shrink on each partial run.
    complete = (not counts.get("stopped_on_time_budget")
                and len(files) < max_files)
    if prune_deleted and complete:
        counts["pruned_deleted"] = idx.delete_missing(drive_id, folder, seen_ids)
    elif prune_deleted:
        counts["pruning_skipped"] = ("crawl was truncated (max_files or time "
                                     "budget) - nothing pruned")

    emb_report = embed_pending(embed_backend)

    roots = set(json.loads(idx.meta_get("roots", "[]")))
    roots.add(f"{site or graph.config().get('default_site','')}/"
              f"{library or graph.config().get('default_library','')}/{folder}".strip("/"))
    idx.meta_set("roots", json.dumps(sorted(roots)))
    idx.meta_set("last_run", time.strftime("%Y-%m-%dT%H:%M:%S"))

    return {"folder": folder or "(library root)", "drive_id": drive_id,
            "subfolders_walked": len(folders), "seconds": round(time.time() - t0, 1),
            "counts": counts, "embedding": emb_report,
            "failures": failures[:15]}


def embed_pending(backend: str = "auto", batch: int = 256,
                  max_chunks: int = 50_000) -> dict:
    """Embed every chunk that has no vector yet."""
    try:
        emb = embedlib.get(backend)
    except Exception as exc:
        return {"backend": "none", "error": str(exc)}
    if not emb.dim:
        idx.meta_set("embed_backend", "none")
        idx.meta_set("embed_dim", 0)
        return {"backend": "none",
                "note": "lexical (BM25) only - no embedding backend configured"}

    stored = idx.meta_get("embed_backend")
    if stored and stored != "none" and stored != emb.name:
        return {"backend": emb.name, "error":
                f"index was built with '{stored}'. Mixing embedding models "
                f"corrupts ranking. Re-run with the same backend, or call "
                f"reset_index() and rebuild."}

    rows = idx.pending_vector_chunks(max_chunks)
    done = 0
    for i in range(0, len(rows), batch):
        part = rows[i:i + batch]
        vecs = emb.encode([r["text"] for r in part], kind="passage")
        idx.store_vectors([r["chunk_id"] for r in part], vecs)
        done += len(part)
    idx.meta_set("embed_backend", emb.name)
    idx.meta_set("embed_dim", emb.dim)
    return {"backend": emb.name, "dim": emb.dim, "embedded_now": done,
            "pending_before": len(rows)}


def rescan_risks(path_prefix: str = "") -> dict:
    """Re-run the secret/PII sweep over already-indexed text - no downloads.

    Needed whenever the patterns or the redaction change: the stored findings
    were produced by the old rules, and a finding that leaked part of a key in
    its context stays leaked until it is rewritten.
    """
    c = idx.conn()
    where, args = "", []
    if path_prefix:
        where, args = " WHERE path LIKE ?", [f"{path_prefix.strip('/')}%"]
    rows = c.execute(f"SELECT file_id, name, path FROM files{where}", args).fetchall()
    changed = flagged = 0
    for r in rows:
        text = "\n".join(x["text"] for x in c.execute(
            "SELECT text FROM chunks WHERE file_id=? ORDER BY ord", (r["file_id"],)))
        if not text:
            continue
        risks = extract.scan_risks(text)
        c.execute("UPDATE files SET risk_json=? WHERE file_id=?",
                  (json.dumps(risks) if risks else None, r["file_id"]))
        changed += 1
        flagged += bool(risks)
    c.commit()
    return {"files_rescanned": changed, "files_with_findings": flagged}


def inventory(folder: str = "", site: str | None = None, library: str | None = None,
              recursive: bool = True, max_files: int = 3000) -> dict:
    """Metadata-only sweep: what is in this folder, without downloading it."""
    drive_id = graph.resolve_drive(site, library)
    files, folders = graph.walk(drive_id, folder, recursive, max_files)

    by_kind: dict = {}
    by_ext: dict = {}
    by_year: dict = {}
    total = 0
    names: dict = {}
    for f in files:
        k = classify_item(f)
        e = f.get("ext") or "(none)"
        sz = f.get("size") or 0
        total += sz
        d = by_kind.setdefault(k, {"files": 0, "bytes": 0})
        d["files"] += 1
        d["bytes"] += sz
        d = by_ext.setdefault(e, {"files": 0, "bytes": 0})
        d["files"] += 1
        d["bytes"] += sz
        yr = (f.get("modified") or "----")[:4]
        by_year[yr] = by_year.get(yr, 0) + 1
        names.setdefault(f["name"].lower(), []).append(f["path"])

    dupes = [{"name": n, "copies": len(p), "paths": p[:5]}
             for n, p in names.items() if len(p) > 1]
    dupes.sort(key=lambda d: -d["copies"])
    big = sorted(files, key=lambda f: -(f.get("size") or 0))[:10]
    recent = sorted(files, key=lambda f: (f.get("modified") or ""), reverse=True)[:10]
    stale = [f for f in files if (f.get("modified") or "9") < "2024"]

    def slim(f):
        return {"name": f["name"], "path": f["path"],
                "MB": round((f.get("size") or 0) / 1e6, 2),
                "modified": (f.get("modified") or "")[:10],
                "modified_by": f.get("modified_by")}

    return {
        "folder": folder or "(library root)",
        "totals": {"files": len(files), "folders": len(folders),
                   "size_MB": round(total / 1e6, 1),
                   "truncated_at_max_files": len(files) >= max_files},
        "by_kind": {k: {"files": v["files"], "MB": round(v["bytes"] / 1e6, 1)}
                    for k, v in sorted(by_kind.items(), key=lambda x: -x[1]["files"])},
        "by_extension": {k: v["files"] for k, v in
                         sorted(by_ext.items(), key=lambda x: -x[1]["files"])[:20]},
        "by_modified_year": dict(sorted(by_year.items(), reverse=True)),
        "largest": [slim(f) for f in big],
        "most_recent": [slim(f) for f in recent],
        "stale_before_2024": len(stale),
        "duplicate_names": dupes[:10],
        "subfolders": [f["path"] for f in folders][:60],
    }


if __name__ == "__main__":
    fld = sys.argv[1] if len(sys.argv) > 1 else ""
    print(json.dumps(index_folder(fld, progress=lambda c: print(c, flush=True)),
                     indent=2, default=str))
