"""Local index: SQLite catalogue + FTS5 lexical + optional dense vectors.

Design notes worth knowing before you change anything here:

* One SQLite file (`data/index.db`) holds the catalogue, the chunk text, the
  BM25 index and the vectors. No server, no container, no Azure resource.
  Copy the file to move the index; delete it to start over.

* Retrieval is HYBRID. BM25 finds exact nouns (a job number, "R57MLOBK", a
  person's name) that embeddings routinely miss; vectors find paraphrase
  ("closing dates" -> "possession schedule") that BM25 cannot. They are fused
  with Reciprocal Rank Fusion, which needs no score calibration between the
  two - the usual reason naive hybrid ranking goes wrong.

* Incremental by design: a file is re-parsed only when its cTag/eTag changes.
  Re-running `index_folder` over 5,000 files after one edit costs one download.

* The vector matrix is loaded into RAM on first search and cached. That is the
  honest ceiling of the local tier: roughly 200k chunks at 384-d. Past that,
  move to Azure AI Search (see README, "When to graduate").
"""
from __future__ import annotations

import json
import pathlib
import re
import sqlite3
import time

import numpy as np

_HERE = pathlib.Path(__file__).parent
DB_PATH = _HERE / "data" / "index.db"

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS files (
  file_id     INTEGER PRIMARY KEY,
  drive_id    TEXT NOT NULL,
  item_id     TEXT NOT NULL,
  name        TEXT, path TEXT, web_url TEXT,
  ext         TEXT, kind TEXT,
  size        INTEGER, modified TEXT, created TEXT,
  created_by  TEXT, modified_by TEXT,
  etag        TEXT, content_hash TEXT,
  status      TEXT, error TEXT,
  n_chunks    INTEGER DEFAULT 0,
  profile_json TEXT, meta_json TEXT, risk_json TEXT,
  indexed_at  TEXT,
  UNIQUE(drive_id, item_id)
);
CREATE INDEX IF NOT EXISTS ix_files_path ON files(path);
CREATE INDEX IF NOT EXISTS ix_files_ext  ON files(ext);
CREATE INDEX IF NOT EXISTS ix_files_mod  ON files(modified);

CREATE TABLE IF NOT EXISTS chunks (
  chunk_id INTEGER PRIMARY KEY,
  file_id  INTEGER NOT NULL REFERENCES files(file_id) ON DELETE CASCADE,
  ord      INTEGER, page INTEGER, chunk_type TEXT,
  text     TEXT, n_chars INTEGER
);
CREATE INDEX IF NOT EXISTS ix_chunks_file ON chunks(file_id);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts
  USING fts5(text, content='chunks', content_rowid='chunk_id',
             tokenize='porter unicode61');

CREATE TABLE IF NOT EXISTS vectors (
  chunk_id INTEGER PRIMARY KEY REFERENCES chunks(chunk_id) ON DELETE CASCADE,
  vec BLOB NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

_conn: sqlite3.Connection | None = None
_vec_cache: tuple | None = None          # (ids ndarray, matrix ndarray, stamp)


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(SCHEMA)
        _conn.execute("PRAGMA foreign_keys=ON")
        _conn.commit()
    return _conn


def meta_get(key: str, default=None):
    row = conn().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def meta_set(key: str, value: str) -> None:
    conn().execute("INSERT INTO meta(key,value) VALUES(?,?) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (key, str(value)))
    conn().commit()


# --------------------------------------------------------------------------- #
# writes
# --------------------------------------------------------------------------- #
def existing_etag(drive_id: str, item_id: str) -> tuple[int | None, str | None, str | None]:
    row = conn().execute(
        "SELECT file_id, etag, status FROM files WHERE drive_id=? AND item_id=?",
        (drive_id, item_id)).fetchone()
    return (row["file_id"], row["etag"], row["status"]) if row else (None, None, None)


def upsert_file(item: dict, drive_id: str, kind: str, status: str,
                profile: dict | None = None, meta: dict | None = None,
                risks: list | None = None, error: str | None = None) -> int:
    c = conn()
    c.execute("""
      INSERT INTO files (drive_id,item_id,name,path,web_url,ext,kind,size,modified,
                         created,created_by,modified_by,etag,content_hash,status,
                         error,profile_json,meta_json,risk_json,indexed_at)
      VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
      ON CONFLICT(drive_id,item_id) DO UPDATE SET
        name=excluded.name, path=excluded.path, web_url=excluded.web_url,
        ext=excluded.ext, kind=excluded.kind, size=excluded.size,
        modified=excluded.modified, created=excluded.created,
        created_by=excluded.created_by, modified_by=excluded.modified_by,
        etag=excluded.etag, content_hash=excluded.content_hash,
        status=excluded.status, error=excluded.error,
        profile_json=excluded.profile_json, meta_json=excluded.meta_json,
        risk_json=excluded.risk_json, indexed_at=excluded.indexed_at
    """, (drive_id, item["item_id"], item.get("name"), item.get("path"),
          item.get("web_url"), item.get("ext"), kind, item.get("size"),
          item.get("modified"), item.get("created"), item.get("created_by"),
          item.get("modified_by"), item.get("etag"), item.get("hash"), status,
          error, json.dumps(profile) if profile else None,
          json.dumps(meta) if meta else None,
          json.dumps(risks) if risks else None,
          time.strftime("%Y-%m-%dT%H:%M:%S")))
    c.commit()
    return c.execute("SELECT file_id FROM files WHERE drive_id=? AND item_id=?",
                     (drive_id, item["item_id"])).fetchone()["file_id"]


def replace_chunks(file_id: int, chunks: list) -> list[int]:
    """Swap a file's chunks atomically and return the new chunk ids."""
    global _vec_cache
    c = conn()
    old = [r["chunk_id"] for r in
           c.execute("SELECT chunk_id FROM chunks WHERE file_id=?", (file_id,))]
    if old:
        c.executemany("INSERT INTO chunks_fts(chunks_fts, rowid, text) "
                      "VALUES('delete', ?, (SELECT text FROM chunks WHERE chunk_id=?))",
                      [(i, i) for i in old])
        c.execute("DELETE FROM vectors WHERE chunk_id IN "
                  "(SELECT chunk_id FROM chunks WHERE file_id=?)", (file_id,))
        c.execute("DELETE FROM chunks WHERE file_id=?", (file_id,))
    ids = []
    for i, ch in enumerate(chunks):
        cur = c.execute(
            "INSERT INTO chunks (file_id,ord,page,chunk_type,text,n_chars) "
            "VALUES (?,?,?,?,?,?)",
            (file_id, i, ch.get("page"), ch.get("chunk_type", "text"),
             ch["text"], len(ch["text"])))
        rid = cur.lastrowid
        c.execute("INSERT INTO chunks_fts(rowid, text) VALUES (?,?)", (rid, ch["text"]))
        ids.append(rid)
    c.execute("UPDATE files SET n_chunks=? WHERE file_id=?", (len(ids), file_id))
    c.commit()
    _vec_cache = None
    return ids


def store_vectors(chunk_ids: list[int], vecs: np.ndarray) -> None:
    global _vec_cache
    c = conn()
    c.executemany("INSERT OR REPLACE INTO vectors (chunk_id, vec) VALUES (?,?)",
                  [(cid, vecs[i].astype(np.float32).tobytes())
                   for i, cid in enumerate(chunk_ids)])
    c.commit()
    _vec_cache = None


def pending_vector_chunks(limit: int = 100000) -> list[sqlite3.Row]:
    return conn().execute(
        "SELECT chunk_id, text FROM chunks WHERE chunk_id NOT IN "
        "(SELECT chunk_id FROM vectors) LIMIT ?", (limit,)).fetchall()


def delete_missing(drive_id: str, folder: str, seen_item_ids: set) -> int:
    """Drop rows for files that vanished from SharePoint under `folder`."""
    global _vec_cache
    c = conn()
    rows = c.execute("SELECT file_id,item_id FROM files WHERE drive_id=? AND path LIKE ?",
                     (drive_id, f"{folder.strip('/')}%" if folder else "%")).fetchall()
    gone = [r["file_id"] for r in rows if r["item_id"] not in seen_item_ids]
    for fid in gone:
        replace_chunks(fid, [])
        c.execute("DELETE FROM files WHERE file_id=?", (fid,))
    c.commit()
    _vec_cache = None
    return len(gone)


# --------------------------------------------------------------------------- #
# search
# --------------------------------------------------------------------------- #
_TOKEN = re.compile(r"[A-Za-z0-9_]{2,}")


def _fts_query(q: str) -> str:
    """FTS5 MATCH syntax is unforgiving; build a safe OR-of-quoted-terms query
    and keep any quoted phrases the caller supplied."""
    phrases = re.findall(r'"([^"]+)"', q)
    rest = re.sub(r'"[^"]+"', " ", q)
    terms = [t for t in _TOKEN.findall(rest)]
    parts = [f'"{p}"' for p in phrases] + [f'"{t}"' for t in terms]
    return " OR ".join(parts) if parts else '""'


def _vector_matrix():
    global _vec_cache
    stamp = conn().execute("SELECT COUNT(*), MAX(chunk_id) FROM vectors").fetchone()
    if _vec_cache and _vec_cache[2] == tuple(stamp):
        return _vec_cache[0], _vec_cache[1]
    rows = conn().execute("SELECT chunk_id, vec FROM vectors ORDER BY chunk_id").fetchall()
    if not rows:
        return None, None
    ids = np.array([r["chunk_id"] for r in rows], dtype=np.int64)
    mat = np.vstack([np.frombuffer(r["vec"], dtype=np.float32) for r in rows])
    _vec_cache = (ids, mat, tuple(stamp))
    return ids, mat


def _hydrate(chunk_ids: list[int]) -> dict:
    if not chunk_ids:
        return {}
    qs = ",".join("?" * len(chunk_ids))
    rows = conn().execute(f"""
        SELECT c.chunk_id, c.page, c.chunk_type, c.text,
               f.name, f.path, f.web_url, f.ext, f.kind, f.modified, f.size
        FROM chunks c JOIN files f USING(file_id)
        WHERE c.chunk_id IN ({qs})""", chunk_ids).fetchall()
    return {r["chunk_id"]: dict(r) for r in rows}


def search(query: str, k: int = 8, embedder=None, mode: str = "hybrid",
           ext: str | None = None, kind: str | None = None,
           path_prefix: str | None = None, candidates: int = 60,
           snippet_chars: int = 700) -> dict:
    """Hybrid BM25 + vector search fused with RRF. Returns ranked chunks."""
    c = conn()
    where, args = [], []
    if ext:
        where.append("f.ext IN (%s)" % ",".join("?" * len(ext.split(","))))
        args += [e.strip().lower().lstrip(".") for e in ext.split(",")]
    if kind:
        where.append("f.kind=?")
        args.append(kind)
    if path_prefix:
        where.append("f.path LIKE ?")
        args.append(f"{path_prefix.strip('/')}%")
    filt = (" AND " + " AND ".join(where)) if where else ""

    lex_rank: dict[int, int] = {}
    if mode in ("hybrid", "lexical"):
        try:
            rows = c.execute(f"""
                SELECT c.chunk_id
                FROM chunks_fts JOIN chunks c ON c.chunk_id = chunks_fts.rowid
                JOIN files f USING(file_id)
                WHERE chunks_fts MATCH ?{filt}
                ORDER BY bm25(chunks_fts) LIMIT ?""",
                [_fts_query(query)] + args + [candidates]).fetchall()
            lex_rank = {r["chunk_id"]: i for i, r in enumerate(rows)}
        except sqlite3.OperationalError as exc:
            lex_rank = {}
            if mode == "lexical":
                raise RuntimeError(f"FTS query failed: {exc}")

    vec_rank: dict[int, int] = {}
    used_vectors = False
    if mode in ("hybrid", "semantic") and embedder is not None and embedder.dim:
        ids, mat = _vector_matrix()
        if ids is not None:
            qv = embedder.encode([query], kind="query")[0]
            sims = mat @ qv
            allowed = None
            if where:
                allowed = {r["chunk_id"] for r in c.execute(
                    f"SELECT c.chunk_id FROM chunks c JOIN files f USING(file_id) "
                    f"WHERE 1=1{filt}", args)}
            order = np.argsort(-sims)[:candidates * (4 if allowed else 1)]
            rank = 0
            for j in order:
                cid = int(ids[j])
                if allowed is not None and cid not in allowed:
                    continue
                vec_rank[cid] = rank
                rank += 1
                if rank >= candidates:
                    break
            used_vectors = True

    K = 60.0
    fused: dict[int, float] = {}
    for cid, r in lex_rank.items():
        fused[cid] = fused.get(cid, 0.0) + 1.0 / (K + r)
    for cid, r in vec_rank.items():
        fused[cid] = fused.get(cid, 0.0) + 1.0 / (K + r)
    top = sorted(fused, key=fused.get, reverse=True)[:k]
    meta = _hydrate(top)

    results = []
    for cid in top:
        m = meta.get(cid)
        if not m:
            continue
        results.append({
            "score": round(fused[cid], 5),
            "matched_by": ("both" if cid in lex_rank and cid in vec_rank
                           else "keyword" if cid in lex_rank else "semantic"),
            "file": m["name"], "path": m["path"], "web_url": m["web_url"],
            "kind": m["kind"], "ext": m["ext"], "modified": m["modified"],
            "page": m["page"], "chunk_type": m["chunk_type"],
            "text": m["text"][:snippet_chars],
        })
    return {"query": query, "mode": mode,
            "retrieval": {"lexical_hits": len(lex_rank),
                          "vector_hits": len(vec_rank),
                          "vectors_used": used_vectors,
                          "embedding_backend": meta_get("embed_backend", "none")},
            "results": results}


def find_files(name_like: str | None = None, ext: str | None = None,
               kind: str | None = None, path_prefix: str | None = None,
               modified_after: str | None = None, min_size: int | None = None,
               limit: int = 100) -> list:
    where, args = ["1=1"], []
    if name_like:
        where.append("name LIKE ?")
        args.append(f"%{name_like}%")
    if ext:
        where.append("ext IN (%s)" % ",".join("?" * len(ext.split(","))))
        args += [e.strip().lower().lstrip(".") for e in ext.split(",")]
    if kind:
        where.append("kind=?")
        args.append(kind)
    if path_prefix:
        where.append("path LIKE ?")
        args.append(f"{path_prefix.strip('/')}%")
    if modified_after:
        where.append("modified >= ?")
        args.append(modified_after)
    if min_size:
        where.append("size >= ?")
        args.append(min_size)
    rows = conn().execute(
        f"SELECT name,path,ext,kind,size,modified,modified_by,status,n_chunks,web_url "
        f"FROM files WHERE {' AND '.join(where)} ORDER BY modified DESC LIMIT ?",
        args + [limit]).fetchall()
    return [dict(r) for r in rows]


def stats() -> dict:
    c = conn()
    total = c.execute("SELECT COUNT(*) n, COALESCE(SUM(size),0) b FROM files").fetchone()
    by_kind = [dict(r) for r in c.execute(
        "SELECT kind, COUNT(*) files, COALESCE(SUM(size),0) bytes "
        "FROM files GROUP BY kind ORDER BY files DESC")]
    by_status = [dict(r) for r in c.execute(
        "SELECT status, COUNT(*) files FROM files GROUP BY status")]
    by_ext = [dict(r) for r in c.execute(
        "SELECT ext, COUNT(*) files FROM files GROUP BY ext ORDER BY 2 DESC LIMIT 15")]
    ch = c.execute("SELECT COUNT(*) n, COALESCE(SUM(n_chars),0) c FROM chunks").fetchone()
    vec = c.execute("SELECT COUNT(*) n FROM vectors").fetchone()["n"]
    risky = c.execute("SELECT COUNT(*) n FROM files WHERE risk_json IS NOT NULL").fetchone()["n"]
    return {
        "db": str(DB_PATH),
        "db_size_mb": round(DB_PATH.stat().st_size / 1e6, 2) if DB_PATH.exists() else 0,
        "files": total["n"], "bytes": total["b"],
        "chunks": ch["n"], "chunk_chars": ch["c"],
        "vectors": vec,
        "vector_coverage_pct": round(100.0 * vec / ch["n"], 1) if ch["n"] else 0.0,
        "embedding_backend": meta_get("embed_backend", "none"),
        "embedding_dim": int(meta_get("embed_dim", 0) or 0),
        "last_index_run": meta_get("last_run"),
        "indexed_roots": json.loads(meta_get("roots", "[]")),
        "files_with_risk_findings": risky,
        "by_kind": by_kind, "by_status": by_status, "top_extensions": by_ext,
    }


def risks(limit: int = 50) -> list:
    rows = conn().execute(
        "SELECT name, path, web_url, risk_json FROM files "
        "WHERE risk_json IS NOT NULL LIMIT ?", (limit,)).fetchall()
    return [{"file": r["name"], "path": r["path"], "web_url": r["web_url"],
             "findings": json.loads(r["risk_json"])} for r in rows]


def get_profile(path: str) -> dict | None:
    row = conn().execute(
        "SELECT name,path,kind,ext,size,modified,profile_json,meta_json,status,error "
        "FROM files WHERE path=? OR name=? ORDER BY length(path) LIMIT 1",
        (path.strip("/"), path)).fetchone()
    if not row:
        return None
    out = dict(row)
    out["profile"] = json.loads(out.pop("profile_json") or "null")
    out["meta"] = json.loads(out.pop("meta_json") or "null")
    return out


def file_text(path: str, max_chars: int = 20000, page: int | None = None) -> dict | None:
    row = conn().execute(
        "SELECT file_id,name,path,web_url FROM files WHERE path=? OR name=? LIMIT 1",
        (path.strip("/"), path)).fetchone()
    if not row:
        return None
    q = "SELECT page, text FROM chunks WHERE file_id=?"
    args = [row["file_id"]]
    if page is not None:
        q += " AND page=?"
        args.append(page)
    rows = conn().execute(q + " ORDER BY ord", args).fetchall()
    text = "\n\n".join(r["text"] for r in rows)
    return {"file": row["name"], "path": row["path"], "web_url": row["web_url"],
            "chars": len(text), "truncated": len(text) > max_chars,
            "text": text[:max_chars]}


def reset() -> None:
    global _conn, _vec_cache
    if _conn:
        _conn.close()
    _conn = None
    _vec_cache = None
    if DB_PATH.exists():
        DB_PATH.unlink()
    for suffix in ("-wal", "-shm"):
        p = DB_PATH.with_name(DB_PATH.name + suffix)
        if p.exists():
            p.unlink()
