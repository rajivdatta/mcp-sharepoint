"""sharepoint - MCP server for reading and indexing a SharePoint document library.

It does two separable jobs:

  LIVE      browse / inventory / describe / read - straight Graph calls, always
            current, no index required.
  INDEXED   index_folder builds a local SQLite catalogue + BM25 + (optional)
            vectors; search_documents then answers questions in ~50 ms without
            re-reading SharePoint.

Typical flow:
  1. whoami                     - confirm the signed-in account
  2. list_libraries             - find the document library
  3. inventory_folder           - what is in there, by type/size/age (no download)
  4. index_folder               - parse + chunk + embed it once
  5. search_documents           - hybrid retrieval with citations
  6. describe_file / read_file  - drill into one thing

READ-ONLY: this server never writes, moves, or deletes anything in SharePoint.
"""
from __future__ import annotations

import json

from fastmcp import FastMCP

import embed as embedlib
import extract
import graph
import index as idx
import pipeline

mcp = FastMCP("sharepoint")

MAX_CHARS = 60_000          # hard ceiling on any single tool response


def _dump(obj) -> str:
    s = json.dumps(obj, indent=2, default=str, ensure_ascii=False)
    if len(s) > MAX_CHARS:
        s = s[:MAX_CHARS] + f"\n... [truncated at {MAX_CHARS} chars - narrow the request]"
    return s


def _err(exc: Exception) -> str:
    return f"SharePoint MCP error: {type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------- #
# connection & discovery
# --------------------------------------------------------------------------- #
@mcp.tool()
def whoami() -> str:
    """Confirm the connection: signed-in identity, auth mode, default site and
    library, and whether results are permission-trimmed. Use this first."""
    try:
        cfg = graph.config()
        st = idx.stats()
        out = {**graph.whoami(),
               "default_site": cfg.get("default_site"),
               "default_library": cfg.get("default_library"),
               "hostname": cfg.get("hostname"),
               "allowlist": graph.allowlist_status(),
               "index": {"files": st["files"], "chunks": st["chunks"],
                         "backend": st["embedding_backend"]}}
        diag = graph.scope_diagnosis()
        if diag:
            out["BLOCKER"] = diag
        return _dump(out)
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def list_sites(query: str = "", limit: int = 20) -> str:
    """Find SharePoint sites by keyword (e.g. 'Team', 'Data'). Returns site id,
    display name and URL - feed either the name or the URL to other tools."""
    try:
        return _dump(graph.search_sites(query or "*", limit))
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def list_libraries(site: str = "") -> str:
    """List the document libraries (drives) on a site, with their size. `site`
    accepts 'Team', '/sites/Team', a full URL, or blank for the configured
    default."""
    try:
        return _dump(graph.list_drives(site or None))
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def browse_folder(folder: str = "", site: str = "", library: str = "",
                  limit: int = 200) -> str:
    """List one folder's immediate contents (folders first, then files) with
    size, modified date and who touched it last. Non-recursive: use
    inventory_folder for a whole subtree."""
    try:
        drive = graph.resolve_drive(site or None, library or None)
        items = graph.list_children(drive, folder, limit)
        items.sort(key=lambda i: (not i["is_folder"], i["name"].lower()))
        return _dump({"folder": folder or "(library root)",
                      "count": len(items),
                      "items": [{k: v for k, v in i.items()
                                 if k not in ("etag", "hash", "item_id")}
                                for i in items]})
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def inventory_folder(folder: str = "", site: str = "", library: str = "",
                     recursive: bool = True, max_files: int = 3000) -> str:
    """Profile a whole folder subtree WITHOUT downloading anything: counts and
    size by file kind (structured / unstructured / code / image), by extension
    and by year last modified, plus the largest and most recent files, stale
    files, duplicate file names and the subfolder list. This is the fastest
    answer to 'what data is in this SharePoint folder?'."""
    try:
        return _dump(pipeline.inventory(folder, site or None, library or None,
                                        recursive, max_files))
    except Exception as exc:
        return _err(exc)


# --------------------------------------------------------------------------- #
# single-file detail
# --------------------------------------------------------------------------- #
@mcp.tool()
def describe_file(path: str, site: str = "", library: str = "",
                  sample_rows: int = 5) -> str:
    """Full detail for one file, live from SharePoint. `path` is the path inside
    the library, e.g. 'Team/Shared_Data/Sales/orders_2026.xlsx'.

    Structured files (csv/tsv/xlsx/json/parquet) return a schema profile per
    sheet: column names, inferred types, null %, distinct counts, min/max,
    candidate keys and sample rows. Unstructured files (pdf/docx/pptx/txt/html)
    return page/slide counts, document metadata, an outline and a text preview.
    """
    try:
        drive = graph.resolve_drive(site or None, library or None)
        it = graph.get_item(drive, path)
        kind = extract.classify(it["ext"])
        out = {"file": {k: v for k, v in it.items() if k != "item_id"}, "kind": kind}
        if kind in ("image", "report", "other"):
            out["note"] = "catalogued by metadata only - no text extractor for this type"
            return _dump(out)
        raw = graph.download(drive, it["item_id"])
        if kind == "structured":
            out["profile"] = extract.profile_structured(raw, it["ext"], it["name"],
                                                        sample_rows)
        else:
            doc = extract.extract_text(raw, it["ext"], it["name"])
            pages = doc["pages"]
            out["document"] = {
                "format": doc["format"], "meta": doc["meta"],
                "pages": len(pages), "characters": doc["char_count"],
                "outline": [ln[3:] for p in pages
                            for ln in p["text"].splitlines()
                            if ln.startswith("## ")][:40],
                "preview": "\n".join(p["text"] for p in pages)[:3000],
            }
        risks = extract.scan_risks(json.dumps(out)[:400_000])
        if risks:
            out["risk_findings"] = risks
        return _dump(out)
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def read_file(path: str, site: str = "", library: str = "",
              max_chars: int = 15000, page_from: int = 0, page_to: int = 0) -> str:
    """Extract the text of one document (pdf/docx/pptx/txt/md/html/eml/ipynb).
    Use `page_from`/`page_to` (1-based, 0 = all) to page through a long file
    instead of pulling it all into context at once."""
    try:
        drive = graph.resolve_drive(site or None, library or None)
        it = graph.get_item(drive, path)
        raw = graph.download(drive, it["item_id"])
        doc = extract.extract_text(raw, it["ext"], it["name"])
        pages = doc["pages"]
        if page_from or page_to:
            lo = page_from or 1
            hi = page_to or len(pages)
            pages = [p for p in pages if lo <= (p["page"] or 1) <= hi]
        text = "\n\n".join(f"[p.{p['page']}]\n{p['text']}" for p in pages)
        return _dump({"file": it["name"], "path": it["path"],
                      "web_url": it["web_url"],
                      "total_pages": len(doc["pages"]),
                      "returned_pages": len(pages),
                      "characters": len(text), "truncated": len(text) > max_chars,
                      "text": text[:max_chars]})
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def preview_table(path: str, site: str = "", library: str = "",
                  sheet: str = "", rows: int = 20) -> str:
    """First N rows of a csv/tsv/xlsx/json/parquet file as records. `sheet`
    picks one Excel sheet (blank = the first non-empty one)."""
    try:
        drive = graph.resolve_drive(site or None, library or None)
        it = graph.get_item(drive, path)
        raw = graph.download(drive, it["item_id"])
        prof = extract.profile_structured(raw, it["ext"], it["name"], sample_rows=rows)
        tables = prof["tables"]
        if sheet:
            tables = [t for t in tables if t["name"].lower() == sheet.lower()]
        elif tables:
            tables = [next((t for t in tables if t["rows"]), tables[0])]
        return _dump({"file": it["name"], "path": it["path"],
                      "available_sheets": [t["name"] for t in prof["tables"]],
                      "tables": tables})
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def search_sharepoint(query: str, site: str = "", library: str = "",
                      limit: int = 30) -> str:
    """Live SharePoint keyword search across a library (Microsoft Search - file
    names and indexed content). Finds files that have never been indexed
    locally; for ranked, cited passages use search_documents instead."""
    try:
        drive = graph.resolve_drive(site or None, library or None)
        hits = graph.search_drive(drive, query, limit)
        return _dump({"query": query, "count": len(hits),
                      "results": [{k: v for k, v in h.items()
                                   if k not in ("etag", "hash", "item_id")}
                                  for h in hits]})
    except Exception as exc:
        return _err(exc)


# --------------------------------------------------------------------------- #
# indexing
# --------------------------------------------------------------------------- #
@mcp.tool()
def index_folder(folder: str = "", site: str = "", library: str = "",
                 recursive: bool = True, max_files: int = 2000,
                 max_file_mb: int = 40, embed_backend: str = "auto",
                 reindex: bool = False, rows_as_chunks: bool = False,
                 time_budget_s: int = 1800) -> str:
    """Build or refresh the local index for a folder: download each file, profile
    tables / extract text, chunk it, store it in SQLite with a BM25 index, then
    embed the chunks if an embedding backend is configured.

    Incremental - a file is re-parsed only when its SharePoint eTag changed, so
    re-running this is cheap. Set `reindex=True` to force everything.
    `embed_backend`: auto | azure_openai | openai | fastembed | none.
    Run it on a folder at a time for a big library; it honours `time_budget_s`.
    """
    try:
        return _dump(pipeline.index_folder(
            folder, site or None, library or None, recursive, max_files,
            max_file_mb, embed_backend, reindex, rows_as_chunks, time_budget_s))
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def index_status() -> str:
    """What the local index currently holds: file and chunk counts, vector
    coverage, embedding backend and dimension, DB size, indexed roots, counts by
    kind/extension/status, and how many files tripped the secret scan."""
    try:
        return _dump(idx.stats())
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def embed_index(backend: str = "auto") -> str:
    """Embed any chunks that have no vector yet (e.g. after adding an embedding
    backend to an index that started out BM25-only). Refuses to mix models."""
    try:
        return _dump(pipeline.embed_pending(backend))
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def reset_index(confirm: str = "") -> str:
    """Delete the local index file and start over. Pass confirm='yes' to run.
    Only touches the local DB - SharePoint is never modified."""
    if confirm != "yes":
        return "Refusing: pass confirm='yes' to delete the local index."
    try:
        idx.reset()
        return "Local index deleted. Run index_folder to rebuild."
    except Exception as exc:
        return _err(exc)


# --------------------------------------------------------------------------- #
# retrieval over the index
# --------------------------------------------------------------------------- #
@mcp.tool()
def search_documents(query: str, k: int = 8, mode: str = "hybrid",
                     ext: str = "", kind: str = "", path_prefix: str = "",
                     snippet_chars: int = 700) -> str:
    """Ask the indexed corpus a question and get back the most relevant passages
    with their file, path, page and SharePoint URL.

    `mode`: hybrid (default - BM25 + vectors fused by RRF), lexical (exact terms
    only), semantic (meaning only). Filter with `ext` ('pdf,docx'),
    `kind` (structured|unstructured|code|report) or `path_prefix`.
    Each result says whether it matched by keyword, semantics or both.
    """
    try:
        emb, emb_note = None, None
        if mode in ("hybrid", "semantic"):
            try:
                emb = embedlib.from_stored_name(idx.meta_get("embed_backend", "none"))
            except Exception as exc:
                emb_note = str(exc)          # surfaced, never silently downgraded
        out = idx.search(query, k, emb, mode, ext or None, kind or None,
                         path_prefix or None, snippet_chars=snippet_chars)
        if emb_note:
            out["retrieval"]["embedding_warning"] = emb_note
        return _dump(out)
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def find_files(name_like: str = "", ext: str = "", kind: str = "",
               path_prefix: str = "", modified_after: str = "",
               min_size_mb: float = 0, limit: int = 100) -> str:
    """Metadata query over the INDEX (instant, no Graph calls): find files by
    name fragment, extension, kind, path prefix, modified date (ISO, e.g.
    '2026-01-01') or minimum size."""
    try:
        return _dump(idx.find_files(
            name_like or None, ext or None, kind or None, path_prefix or None,
            modified_after or None, int(min_size_mb * 1e6) or None, limit))
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def get_indexed_profile(path: str) -> str:
    """The stored schema profile / document metadata for an indexed file, without
    re-downloading it from SharePoint."""
    try:
        out = idx.get_profile(path)
        return _dump(out) if out else f"'{path}' is not in the index."
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def get_indexed_text(path: str, max_chars: int = 20000, page: int = 0) -> str:
    """The stored extracted text of an indexed file, reassembled from its chunks.
    `page` (1-based, 0 = all) limits it to one page/slide."""
    try:
        out = idx.file_text(path, max_chars, page or None)
        return _dump(out) if out else f"'{path}' is not in the index."
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def risk_findings(limit: int = 50) -> str:
    """Files where the indexer's secret/PII sweep matched: passwords, connection
    strings, API keys, private keys, JWTs, SIN/SSN, card numbers. Contexts are
    redacted. Review this BEFORE exposing the index to anyone else - a RAG index
    happily resurfaces a credential that was buried in a document nobody reads.
    """
    try:
        out = idx.risks(limit)
        high = [f for f in out
                if any(h.get("severity") == "high" for h in f["findings"])]
        return _dump({"files_with_findings": len(out),
                      "high_severity_files": len(high), "findings": out})
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def rescan_risks(path_prefix: str = "") -> str:
    """Re-run the secret/PII sweep over already-indexed text without downloading
    anything. Use after the detection patterns or redaction rules change —
    stored findings keep the old rules' contexts until they are rewritten."""
    try:
        return _dump(pipeline.rescan_risks(path_prefix))
    except Exception as exc:
        return _err(exc)


if __name__ == "__main__":
    mcp.run()
