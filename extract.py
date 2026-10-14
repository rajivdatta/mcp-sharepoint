"""Content extraction + profiling for SharePoint files.

Two jobs, decided by extension:

  STRUCTURED  (csv/tsv/xlsx/xls/json/parquet) -> a *profile*: sheets, columns,
              inferred types, null %, distinct counts, sample values, row count.
              This is what makes "what data is in this folder?" answerable
              without reading whole files into the model.

  UNSTRUCTURED (pdf/docx/pptx/txt/md/html/eml) -> plain text plus structure
              hints (page numbers, headings, slide titles) so chunks can cite
              a page.

Everything else is catalogued by metadata only.

Heavy parsers are imported lazily so a missing optional dependency degrades one
file type instead of the whole server.
"""
from __future__ import annotations

import csv
import io
import json
import re
import warnings
import zipfile

# Real-world workbooks trip these constantly (unsupported extensions, defined
# names openpyxl cannot resolve). They are noise, not findings, and a crawl of a
# few thousand files emits thousands of them.
warnings.filterwarnings("ignore", module="openpyxl")

STRUCTURED_EXT = {"csv", "tsv", "txt_delimited", "xlsx", "xlsm", "xls", "json",
                  "parquet", "jsonl", "ndjson"}
UNSTRUCTURED_EXT = {"pdf", "docx", "doc", "pptx", "ppt", "txt", "md", "rtf",
                    "html", "htm", "aspx", "msg", "eml", "xml", "log"}
IMAGE_EXT = {"png", "jpg", "jpeg", "gif", "bmp", "tif", "tiff", "svg", "heic"}
CODE_EXT = {"py", "sql", "ps1", "js", "ts", "yml", "yaml", "ipynb", "r", "sh",
            "bat", "dax", "m", "tmdl"}


def classify(ext: str) -> str:
    ext = (ext or "").lower().lstrip(".")
    if ext in STRUCTURED_EXT:
        return "structured"
    if ext in CODE_EXT:
        return "code"
    if ext in UNSTRUCTURED_EXT:
        return "unstructured"
    if ext in IMAGE_EXT:
        return "image"
    if ext in {"pbix", "pbip", "rdl", "twb", "twbx"}:
        return "report"
    return "other"


# --------------------------------------------------------------------------- #
# text decoding
# --------------------------------------------------------------------------- #
def _decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _sniff_delimiter(sample: str) -> str:
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;|\t").delimiter
    except Exception:
        counts = {d: sample.count(d) for d in [",", ";", "|", "\t"]}
        return max(counts, key=counts.get) if max(counts.values()) else ","


# --------------------------------------------------------------------------- #
# structured profiling
# --------------------------------------------------------------------------- #
def _profile_frame(df, name: str, sample_rows: int) -> dict:
    import pandas as pd  # noqa: F401  (pandas already imported by caller)
    n = len(df)
    cols = []
    for c in df.columns:
        s = df[c]
        nn = int(s.notna().sum())
        col = {
            "name": str(c),
            "dtype": str(s.dtype),
            "nulls_pct": round(100.0 * (n - nn) / n, 1) if n else 0.0,
            "distinct": int(s.nunique(dropna=True)) if n else 0,
        }
        vals = s.dropna().unique()[:3]
        col["samples"] = [str(v)[:60] for v in vals]
        if str(s.dtype).startswith(("int", "float")) and nn:
            try:
                col["min"] = float(s.min())
                col["max"] = float(s.max())
            except Exception:
                pass
        if col["distinct"] == n and n > 1 and col["nulls_pct"] == 0:
            col["candidate_key"] = True
        cols.append(col)
    head = df.head(sample_rows)
    return {
        "name": name,
        "rows": int(n),
        "columns": len(cols),
        "schema": cols,
        "sample": json.loads(head.to_json(orient="records", date_format="iso")),
    }


def profile_structured(raw: bytes, ext: str, filename: str,
                       sample_rows: int = 5, max_rows: int = 200_000) -> dict:
    import pandas as pd
    ext = ext.lower().lstrip(".")
    out: dict = {"kind": "structured", "format": ext, "tables": [], "warnings": []}

    if ext in ("csv", "tsv", "txt", "log"):
        text = _decode(raw)
        delim = "\t" if ext == "tsv" else _sniff_delimiter(text[:8192])
        df = pd.read_csv(io.StringIO(text), sep=delim, nrows=max_rows,
                         low_memory=False, on_bad_lines="skip")
        out["delimiter"] = delim
        out["tables"].append(_profile_frame(df, filename, sample_rows))
        # A CSV that is really one long text column is not structured data.
        if df.shape[1] == 1:
            out["warnings"].append("single column - may be free text, not a table")

    elif ext in ("xlsx", "xlsm", "xls"):
        xl = pd.ExcelFile(io.BytesIO(raw))
        for sheet in xl.sheet_names:
            try:
                df = xl.parse(sheet, nrows=max_rows)
            except Exception as exc:
                out["warnings"].append(f"sheet '{sheet}': {exc}")
                continue
            if df.empty:
                out["tables"].append({"name": sheet, "rows": 0, "columns": 0,
                                      "schema": [], "sample": []})
                continue
            out["tables"].append(_profile_frame(df, sheet, sample_rows))
        out["sheet_count"] = len(xl.sheet_names)

    elif ext in ("json", "jsonl", "ndjson"):
        text = _decode(raw)
        if ext in ("jsonl", "ndjson"):
            rows = [json.loads(ln) for ln in text.splitlines() if ln.strip()][:max_rows]
        else:
            data = json.loads(text)
            rows = data if isinstance(data, list) else [data]
        try:
            df = pd.json_normalize(rows)
            out["tables"].append(_profile_frame(df, filename, sample_rows))
        except Exception as exc:
            out["warnings"].append(f"not tabular JSON: {exc}")
            out["raw_preview"] = text[:2000]

    elif ext == "parquet":
        df = pd.read_parquet(io.BytesIO(raw))
        out["tables"].append(_profile_frame(df.head(max_rows), filename, sample_rows))

    else:
        raise ValueError(f"not a structured extension: {ext}")
    return out


_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def path_words(path: str) -> str:
    """Folder and file names as separate words.

    Found in a pilot corpus: `CycleTime/Cycle Target Timeline.csv` has columns
    'Total Days', 'Quad 1'..'Quad 4', 'QA_Close' - so neither BM25 nor the
    embedder could connect it to "how long does each construction stage take".
    The answer was sitting in the folder name, which BM25 tokenises as the
    single token 'CycleTime' and never matches. Splitting camelCase and
    punctuation turns the filing structure itself into retrievable vocabulary.
    """
    seen, out = set(), []
    for token in re.split(r"[/\\ ._\-]+", path):
        for word in _CAMEL.split(token):
            w = word.strip()
            if len(w) > 1 and w.lower() not in seen:
                seen.add(w.lower())
                out.append(w)
    return " ".join(out)


def schema_card(profile: dict, filename: str, path: str) -> str:
    """One searchable paragraph per table.

    Embedding the *schema* (not the rows) is what lets "which file has closing
    dates by community?" find a spreadsheet nobody has opened in a year.
    """
    terms = path_words(path)
    parts = []
    for t in profile.get("tables", []):
        cols = ", ".join(
            f"{c['name']} ({c['dtype']}"
            + (f", e.g. {c['samples'][0]}" if c.get("samples") else "") + ")"
            for c in t.get("schema", [])[:80])
        parts.append(
            f"File: {filename}\nPath: {path}\nTable/sheet: {t['name']}\n"
            f"Rows: {t['rows']}  Columns: {t['columns']}\nColumns: {cols}\n"
            f"Subject terms: {terms}")
    return ("\n\n".join(parts)
            or f"File: {filename}\nPath: {path}\nSubject terms: {terms}\n(no tables found)")


# --------------------------------------------------------------------------- #
# unstructured extraction
# --------------------------------------------------------------------------- #
def extract_text(raw: bytes, ext: str, filename: str = "") -> dict:
    """Return {'kind','pages':[{'page':n,'text':...}],'meta':{...}}."""
    ext = ext.lower().lstrip(".")
    meta: dict = {}
    pages: list = []

    if ext == "pdf":
        import pymupdf as fitz
        with fitz.open(stream=raw, filetype="pdf") as doc:
            meta = {k: v for k, v in (doc.metadata or {}).items() if v}
            meta["page_count"] = doc.page_count
            for i, page in enumerate(doc, 1):
                pages.append({"page": i, "text": page.get_text("text")})
            if sum(len(p["text"].strip()) for p in pages) < 40 * doc.page_count:
                meta["warning"] = ("little or no text layer - likely a scan; "
                                   "needs OCR to be searchable")

    elif ext == "docx":
        import docx
        d = docx.Document(io.BytesIO(raw))
        lines = []
        for p in d.paragraphs:
            if not p.text.strip():
                continue
            style = (p.style.name or "") if p.style is not None else ""
            lines.append(f"## {p.text}" if style.startswith("Heading") else p.text)
        for tb in d.tables:
            for row in tb.rows:
                cells = [c.text.strip() for c in row.cells]
                if any(cells):
                    lines.append(" | ".join(cells))
        pages = [{"page": 1, "text": "\n".join(lines)}]
        meta = {"paragraphs": len(d.paragraphs), "tables": len(d.tables)}

    elif ext == "pptx":
        from pptx import Presentation
        prs = Presentation(io.BytesIO(raw))
        for i, slide in enumerate(prs.slides, 1):
            buf = []
            for shape in slide.shapes:
                if shape.has_text_frame and shape.text_frame.text.strip():
                    buf.append(shape.text_frame.text)
                elif getattr(shape, "has_table", False):
                    for row in shape.table.rows:
                        buf.append(" | ".join(c.text for c in row.cells))
            notes = ""
            try:
                if slide.has_notes_slide:
                    notes = slide.notes_slide.notes_text_frame.text.strip()
            except Exception:
                pass
            if notes:
                buf.append(f"[speaker notes] {notes}")
            pages.append({"page": i, "text": "\n".join(buf)})
        meta = {"slide_count": len(prs.slides)}

    elif ext in ("html", "htm", "aspx", "xml"):
        from lxml import html as lhtml
        try:
            tree = lhtml.fromstring(raw)
            for bad in tree.xpath("//script|//style"):
                bad.getparent().remove(bad)
            pages = [{"page": 1, "text": re.sub(r"\n{3,}", "\n\n",
                                                tree.text_content())}]
        except Exception:
            pages = [{"page": 1, "text": _decode(raw)}]

    elif ext == "ipynb":
        nb = json.loads(_decode(raw))
        buf = []
        for c in nb.get("cells", []):
            src = "".join(c.get("source", []))
            buf.append(f"[{c.get('cell_type')}]\n{src}")
        pages = [{"page": 1, "text": "\n\n".join(buf)}]

    elif ext in ("eml", "msg"):
        if ext == "eml":
            import email
            from email import policy
            m = email.message_from_bytes(raw, policy=policy.default)
            body = m.get_body(preferencelist=("plain", "html"))
            text = body.get_content() if body else ""
            meta = {"from": str(m.get("From")), "to": str(m.get("To")),
                    "subject": str(m.get("Subject")), "date": str(m.get("Date"))}
            pages = [{"page": 1, "text": f"Subject: {meta['subject']}\n"
                                         f"From: {meta['from']}\n\n{text}"}]
        else:
            try:
                import extract_msg
                m = extract_msg.Message(io.BytesIO(raw))
                meta = {"from": m.sender, "to": m.to, "subject": m.subject,
                        "date": str(m.date)}
                pages = [{"page": 1, "text": f"Subject: {m.subject}\n"
                                             f"From: {m.sender}\n\n{m.body}"}]
            except ImportError:
                raise RuntimeError("`.msg` needs `pip install extract-msg`")

    elif ext in ("doc", "ppt"):
        raise RuntimeError(f"legacy binary '{ext}' not supported - "
                           "re-save as the Open XML format")

    else:                                              # txt, md, sql, py, ...
        pages = [{"page": 1, "text": _decode(raw)}]

    total = sum(len(p["text"]) for p in pages)
    return {"kind": "unstructured", "format": ext, "pages": pages,
            "meta": meta, "char_count": total}


# --------------------------------------------------------------------------- #
# chunking
# --------------------------------------------------------------------------- #
_PARA = re.compile(r"\n\s*\n")


def chunk_pages(pages: list, target_chars: int = 1400, overlap: int = 200,
                min_chars: int = 80) -> list:
    """Paragraph-aware chunks that never straddle a page boundary.

    Staying inside one page means every chunk can cite "p. 7" honestly, which
    is the difference between a citation and a guess.
    """
    chunks = []
    for p in pages:
        text = re.sub(r"[ \t]+", " ", (p.get("text") or "")).strip()
        if len(text) < min_chars:
            continue
        paras = [x.strip() for x in _PARA.split(text) if x.strip()]
        buf = ""
        for para in paras:
            if len(buf) + len(para) + 2 <= target_chars:
                buf = f"{buf}\n\n{para}" if buf else para
                continue
            if buf:
                chunks.append({"page": p.get("page"), "text": buf})
                buf = ""
            if len(para) > target_chars:
                # A single oversized paragraph (a wall-of-text page, a log dump)
                # is hard-split rather than carried, so no chunk can grow past
                # target_chars and blow an embedding model's input limit.
                step = max(target_chars - overlap, 1)
                for i in range(0, len(para), step):
                    piece = para[i:i + target_chars]
                    if len(piece) >= min_chars:
                        chunks.append({"page": p.get("page"), "text": piece})
            else:
                buf = para
        if len(buf) >= min_chars:
            chunks.append({"page": p.get("page"), "text": buf})
    return chunks


# --------------------------------------------------------------------------- #
# risk sweep
# --------------------------------------------------------------------------- #
# Live plaintext service passwords turn up in real document libraries,
# so indexing without a sweep would republish it into
# a vector store. Patterns are deliberately narrow to keep noise down.
RISK_PATTERNS = [
    ("password", re.compile(r"(?i)\b(pass(word|wd)|pwd)\b\s*[:=]\s*\S{4,}")),
    ("connection_string", re.compile(r"(?i)(Password|Pwd)\s*=\s*[^;\s]{4,};?")),
    ("api_key", re.compile(r"(?i)\b(api[_-]?key|secret|token)\b\s*[:=]\s*[A-Za-z0-9._\-]{12,}")),
    ("aws_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("azure_sas", re.compile(r"\bsig=[A-Za-z0-9%/+]{20,}")),
    ("private_key", re.compile(r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.")),
    ("sin_ssn", re.compile(r"\b\d{3}[- ]\d{3}[- ]\d{3}\b|\b\d{3}-\d{2}-\d{4}\b")),
    ("credit_card", re.compile(r"\b(?:4\d{3}|5[1-5]\d{2})[- ]?\d{4}[- ]?\d{4}[- ]?\d{4}\b")),
]


# Detection patterns match the *marker* ("-----BEGIN PRIVATE KEY-----", "pwd=").
# The secret itself is usually the bytes AFTER the marker, which no detection
# pattern covers - so a context snippet would still carry the key material.
# These scrubbers run on top of every redaction and kill the payload too.
# Length alone is not enough: a context snippet is cut mid-token, so a 64-char
# PEM line can arrive as 31 characters and slip under a length threshold. These
# run at 16+ and additionally require the run to look opaque rather than like a
# long English word - digits present, or base64 padding characters.
_OPAQUE_RUN = re.compile(r"[A-Za-z0-9+/]{16,}={0,2}")
_HEX_RUN = re.compile(r"\b[0-9a-fA-F]{16,}\b")


def _looks_opaque(s: str) -> bool:
    digits = sum(c.isdigit() for c in s)
    return ("+" in s or "/" in s or "=" in s
            or digits >= 2
            or (any(c.isupper() for c in s) and any(c.islower() for c in s)
                and digits >= 1))


HIGH_SEVERITY = {"private_key", "api_key", "aws_key", "azure_sas", "jwt",
                 "credit_card"}


def redact(text: str, aggressive: bool = False) -> str:
    """Replace every risk match with a label.

    Applies ALL detection patterns, because a snippet flagged for one routinely
    contains another (a password line sitting next to an access key), and a
    partial redaction is not a redaction. Then scrubs long opaque tokens, which
    is what actually carries a private key.
    """
    for label, rx in RISK_PATTERNS:
        text = rx.sub(f"[{label} REDACTED]", text)
    text = _HEX_RUN.sub("[token REDACTED]", text)
    # `aggressive` drops the "looks opaque" test: for a snippet already flagged
    # as containing a private key, over-redacting a triage hint costs nothing,
    # while under-redacting republishes the key.
    return _OPAQUE_RUN.sub(
        lambda m: ("[token REDACTED]"
                   if aggressive or _looks_opaque(m.group(0)) else m.group(0)),
        text)


def _luhn(digits: str) -> bool:
    ds = [int(c) for c in digits if c.isdigit()][::-1]
    total = 0
    for i, d in enumerate(ds):
        if i % 2:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return len(ds) >= 13 and total % 10 == 0


# Numeric patterns need a boundary test the regex cannot express: a card-shaped
# run sitting inside a longer number or a decimal is not a card. A real case -
# the schema card sample value "0.4545455..." matched the card pattern and
# reported a spreadsheet of bug-test percentages as holding a card number.
_NUMERIC_PATTERNS = {"credit_card", "sin_ssn"}


def _numeric_match_is_real(label: str, text: str, m) -> bool:
    before = text[m.start() - 1] if m.start() else " "
    after = text[m.end()] if m.end() < len(text) else " "
    if before.isdigit() or before == "." or after.isdigit() or after == ".":
        return False
    return _luhn(m.group(0)) if label == "credit_card" else True


def scan_risks(text: str, max_hits: int = 20) -> list:
    hits = []
    high = any(any(_numeric_match_is_real(label, text, m)
                   for m in rx.finditer(text)) if label in _NUMERIC_PATTERNS
               else rx.search(text) is not None
               for label, rx in RISK_PATTERNS if label in HIGH_SEVERITY)
    for label, rx in RISK_PATTERNS:
        for m in rx.finditer(text):
            if label in _NUMERIC_PATTERNS and not _numeric_match_is_real(label, text, m):
                continue
            snippet = text[max(0, m.start() - 60):m.end() + 40].replace("\n", " ")
            hits.append({"type": label,
                         "severity": "high" if label in HIGH_SEVERITY else "review",
                         "line": text[:m.start()].count("\n") + 1,
                         "context": redact(snippet, aggressive=high).strip()[:180]})
            if len(hits) >= max_hits:
                return hits
    return hits


def is_ooxml(raw: bytes) -> bool:
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            return "[Content_Types].xml" in z.namelist()
    except Exception:
        return False
