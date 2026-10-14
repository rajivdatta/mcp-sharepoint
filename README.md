# sharepoint — MCP server for reading and indexing a SharePoint library

Reads a SharePoint folder, tells you what structured and unstructured data is in
it, and (optionally) builds a local searchable index so questions get answered in
milliseconds instead of by re-downloading files.

It does two separable jobs. You can use the first without ever touching the
second.

| | **LIVE mode** | **INDEXED mode** |
|---|---|---|
| Source | Microsoft Graph, every call | local SQLite (`data/index.db`) |
| Freshness | always current | as of the last `index_folder` |
| Cost per question | 1–N Graph calls + a download | one SQLite query (~50 ms) |
| Answers | "what's in this folder", "show me this file" | "which document says X", ranked + cited |
| Setup | sign in | sign in, then crawl once |

**Read-only.** Nothing here writes, moves, or deletes anything in SharePoint.

---

## Install

```powershell
cd <path>\mcp-sharepoint
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe selftest.py        # 35 offline checks, no network
.\.venv\Scripts\python.exe setup_auth.py      # one-time sign-in, from a REAL terminal
```

Register with Claude Code (user scope):

```powershell
claude mcp add sharepoint --scope user -- `
  "<path>\mcp-sharepoint\.venv\Scripts\python.exe" `
  "<path>\mcp-sharepoint\server.py"
```

### Why `setup_auth.py` must run in a real terminal

Auth is WAM (the Windows account broker), which needs a window to parent its
dialog to. The MCP server starts headless, so the *first* interactive sign-in
has to happen once from a console. It writes `.auth_record.json` and a
DPAPI-encrypted MSAL cache; after that the server renews tokens silently and the
window stops mattering. If no cached sign-in exists and there's no console, the
server raises a clear error rather than letting the native broker kill the
process.

### Permissions — the part that actually bites

The token needs delegated **`Files.Read.All`** (and `Sites.Read.All` to enumerate
sites and libraries). Two traps, both hit during the build of this server:

* **The Azure CLI client id (`04b07795-…`) may not work.** It is a handy
  default for Fabric/Power BI/ARM, but its Graph token can carry
  only directory scopes — no `Files.*`, no `Sites.*`. The symptom is not a 403:
  `/sites/{id}/drives` returns an **empty list**, and `/sites/{id}/drive` returns
  `accessDenied`. Point `client_id` at an app registration that actually holds
  the SharePoint scopes.
* **Device code flow is often blocked by Conditional Access.** Use WAM
  (`"auth": "broker"`) or the browser redirect (`"auth": "interactive"`).

`config.json`:

```jsonc
{
  "auth": "broker",                 // broker | interactive | service-principal | service-principal-cert
  "tenant_id": "<tenant-guid>",
  "client_id": "<app with delegated Files.Read.All + Sites.Read.All>",
  "login_hint": "you@contoso.com",
  "hostname": "contoso.sharepoint.com",
  "default_site": "Team/Sub",       // 'Team', 'Team/Sub', a full URL, or a site id
  "default_library": "Documents",   // list_libraries tells you the real names
  "max_download_mb": 80
}
```

**Delegated vs app-only is a security decision, not a convenience one.** App-only
(`service-principal*`) reads every site the app is granted, so *anyone* who can
talk to the agent sees everything the app can see — permission trimming is gone.
Delegated means the server can only ever see what the signed-in user could see in
the browser. `whoami` reports which mode is live, and app-only mode says so in a
warning field. If app-only is unavoidable, use `Sites.Selected` with explicit
per-site grants instead of tenant-wide `Sites.Read.All`.

### The config allowlist

`config.json` carries an allowlist that caps what the server will ask for:

```jsonc
"allowlist": {
  "enabled": true,
  "sites": ["Team/Sub"],
  "libraries": ["Shared_Data"],
  "folders": []          // empty = the whole library
}
```

**It is a guardrail, not a permission.** The token decides what is *reachable*;
this decides what the server is *willing to ask for*. Anyone who can edit
`config.json` can widen it, so it is worth exactly nothing against a determined
user and quite a lot against an ordinary mistake. Real per-user trimming comes
from delegated auth.

It exists because an app-only identity typically holds tenant-wide
`Sites.Read.All`, which can mean hundreds of sites, including sensitive ones
such as HR. With the allowlist on, an ordinary question cannot wander into any
of them.

Enforced at every chokepoint, not just the front door: site resolution, library
resolution, **raw drive ids** (which would otherwise skip both), folder listing,
item fetch, library search and — last of all — `download`, immediately before
any bytes move. Verified by test: sites and libraries outside the allowlist
are refused, while the allowlisted library works, and passing a
foreign drive id directly to `list_children`, `search_drive`, `get_item` or
`download` is refused too.

`list_sites` filters rather than refuses — site discovery is how someone works
out what they may point at — and says how many results it withheld. `whoami`
reports the allowlist, and reports its absence as a warning when it is off.

### App-only via Key Vault (optional)

If no delegated app id is available, the server can run as an app registration
that holds application `Sites.Read.All`, with its certificate **never written
to disk**:

```jsonc
{
  "auth": "service-principal-cert",
  "client_id": "<app-id>",
  "operator_client_id": "<client id used for your own sign-in to Key Vault>",
  "certificate_keyvault": {
    "vault": "<vault-name>",
    "cert_secret": "<pfx-secret-name>",
    "password_secret": "<pfx-password-secret-name>"
  }
}
```

`keyvault.py` pulls the PKCS12 into memory at startup using **your own** WAM
sign-in, and hands it straight to `CertificateCredential`. Two consequences,
both wanted: no credential at rest in this folder, and whoever runs the server
must have `get` on the vault secret — revoking their Key Vault access revokes
their use of the app identity. (`certificate_path` + a password env var still
works if you prefer a local file.)

This mode is **not permission-trimmed**: it reads every site the app is
granted, regardless of who is asking. Prefer `broker` whenever a delegated
client id is available.

---

## Tools

**Discovery / live**

| Tool | What it does |
|---|---|
| `whoami` | signed-in identity, auth mode, trimming status, index summary |
| `list_sites` | find sites by keyword |
| `list_libraries` | document libraries on a site, with size |
| `browse_folder` | one folder's contents, non-recursive |
| `inventory_folder` | **whole subtree profiled without downloading anything** |
| `search_sharepoint` | live Microsoft Search across a library |

**Single file**

| Tool | What it does |
|---|---|
| `describe_file` | schema profile (structured) or outline + metadata (unstructured) |
| `preview_table` | first N rows of a csv/xlsx/json/parquet |
| `read_file` | extracted text, with page ranges |

**Index**

| Tool | What it does |
|---|---|
| `index_folder` | crawl → parse → chunk → store → embed. Incremental by eTag |
| `index_status` | counts, vector coverage, backend, DB size, roots |
| `embed_index` | embed chunks that have no vector yet |
| `reset_index` | delete the local DB (`confirm='yes'`) |

**Retrieval**

| Tool | What it does |
|---|---|
| `search_documents` | hybrid BM25 + vector search, ranked, with page citations |
| `find_files` | instant metadata query over the index |
| `get_indexed_profile` / `get_indexed_text` | stored profile / text, no re-download |
| `risk_findings` | files where the secret/PII sweep matched, `high` severity called out |
| `rescan_risks` | re-run the sweep over indexed text after tuning patterns (no downloads) |

---

## What "details" means per file type

**Structured** — `csv` `tsv` `xlsx` `xlsm` `json` `jsonl` `parquet`
Per sheet/table: row count, column count, and for every column its name,
inferred dtype, null %, distinct count, three sample values, min/max for
numerics, and a `candidate_key` flag when it is unique and non-null. Plus N
sample rows. CSV delimiter is sniffed (a pipe-delimited `.csv` profiles
correctly).

**Unstructured** — `pdf` `docx` `pptx` `txt` `md` `html` `aspx` `xml` `eml`
`msg` `ipynb`
Text with structure preserved: PDF keeps real page numbers, DOCX headings become
`## ` lines and tables are flattened to `a | b | c`, PPTX keeps one page per
slide **including speaker notes**. A PDF with no text layer is flagged
`needs OCR` instead of being silently indexed as empty — otherwise a scanned
contract looks indexed and never matches anything.

**Everything else** — catalogued by metadata only (name, size, dates, author,
URL). Images, archives, media, `.pbix` are never downloaded.

---

## Indexing and vectoring — the deep dive

### Why an index at all

Without one, every question re-downloads and re-parses files, and a single
40-page PDF costs ~20k tokens of context. With one, the model sees ~8 passages
of ~700 characters — the relevant ones — with citations. The index is what turns
"read this folder" into "answer questions about this folder".

### The pipeline

```
Graph walk ──► eTag check ──► download ──► parse ──► chunk ──► SQLite
  (metadata)     (skip if      (only if   (profile   (~1400     ├─ files     catalogue
                  unchanged)    changed)   or text)   chars)    ├─ chunks    text
                                                                ├─ chunks_fts BM25
                                                                └─ vectors   float32
```

**Chunking.** ~1400 characters with 200 overlap, split on paragraph boundaries,
and **never across a page boundary**. That last rule is what lets every result
cite "p. 7" honestly instead of approximately. Overlap exists so a sentence
split across two chunks still matches in one of them.

**Structured files get a "schema card" instead of their rows.** One paragraph
per sheet: file name, path, sheet name, row/column counts, and every column with
its type and a sample value. Embedding the *schema* is what makes
*"which file has closing dates by community?"* find a spreadsheet nobody has
opened in a year. Indexing the rows themselves would flood the index with
millions of near-identical chunks and answer worse — if you want to query rows,
query the source system, not a vector store. (`rows_as_chunks=True` exists for
small reference tables; use it sparingly.)

**Incremental.** A file is re-parsed only when its SharePoint `cTag` changes, so
the second run over 5,000 files costs one download per edited file. Files deleted
from SharePoint are pruned from the index on each run.

### Retrieval is hybrid, and that is not optional

| | finds | misses |
|---|---|---|
| **BM25** (SQLite FTS5) | exact tokens: `R57MLOBK`, a lot number, a person's name, `F44H201` | paraphrase — "closing dates" will not match "possession schedule" |
| **Vectors** (cosine) | meaning: "possession schedule", "handover timing" | rare literal strings; embeddings blur identifiers into noise |

Both run, and their **ranks** are fused with Reciprocal Rank Fusion
(`score = Σ 1/(60 + rank)`). RRF uses rank position, not score magnitude, which
sidesteps the usual way naive hybrid search goes wrong: BM25 scores and cosine
similarities live on incomparable scales, so any weighted-sum fusion needs
calibration that drifts the moment the corpus changes. Every result reports
whether it matched `keyword`, `semantic` or `both` — when results look wrong,
that field tells you which half to fix.

### Embedding backends

Set `embed_backend` on `index_folder`, or leave it `auto`.

| Backend | Quality | Cost | Setup | Notes |
|---|---|---|---|---|
| `none` | keyword only | £0 | none | BM25 over named business documents is genuinely decent. Honest default. |
| `fastembed` | good | £0 | `pip install fastembed` | Local ONNX `bge-small-en-v1.5`, 384-d, no torch. One ~130 MB model download, then fully offline. |
| `azure_openai` | best | ~$0.02 / 1M tokens | an AOAI deployment | `text-embedding-3-small`, 1536-d. Data stays in your Azure region. Auth by key **or** your Entra sign-in. |
| `openai` | best | same | an API key | Only if sending corpus text to `api.openai.com` is permitted. It usually is not. |

`auto` resolves `azure_openai → openai → fastembed → none`.

**Cost, concretely.** 10,000 documents averaging 8 pages ≈ 40M tokens ≈
**$0.80 one-off** on `text-embedding-3-small`, plus a fraction of a cent per
query. Embedding cost is never the reason not to do this.

**One hard rule the code enforces:** you cannot mix embedding models in one
index. Vectors from two models share a space only by coincidence, and the
failure is silent — rankings just quietly get worse. `embed_index` refuses when
the stored backend and the live one disagree; `reset_index` and rebuild instead.

### Sizing — where the local tier stops

Vectors are loaded into a NumPy matrix on first search and cached (invalidated on
write). Search is an exact brute-force dot product — no ANN index, because exact
is *faster* than approximate below ~10⁵ vectors and never has recall cliffs.

| Corpus | Chunks | RAM at 384-d | RAM at 1536-d | Search |
|---|---|---|---|---|
| 1,000 files | ~25k | 38 MB | 150 MB | ~10 ms |
| 10,000 files | ~250k | 380 MB | 1.5 GB | ~80 ms |
| 50,000 files | ~1.2M | 1.8 GB | 7 GB | too slow |

Rule of thumb: the local tier is comfortable to about **200k chunks at 384-d**
(≈ 8,000 documents). Past that, graduate.

### Lessons from real corpora

* **Duplicate file names are often real**, not a bug — monthly snapshots kept
  side by side. Worth knowing before anyone "de-duplicates" a library.
* **A truncated crawl never prunes.** When a crawl hits `max_files`, pruning
  declines to run — a partial crawl that pruned would delete every file it
  never reached. Crawl a folder at a time, or raise the cap.
* **A schema card is only as good as its column names.** A table whose subject
  lives only in its *folder name* (`CycleTime/`) is unreachable by either search
  half. Schema cards therefore carry a **Subject terms** line built from the
  path with camelCase and punctuation split out (`CycleTime` → `Cycle Time`).
* **Near-duplicate documents drown a small table.** When dozens of monthly
  reports say nearly the same thing, one schema card cannot out-rank them. **Use
  `kind='structured'` when you are looking for a table.**
* **Hybrid earns its keep, but not on every query.** Where the halves agree,
  results are marked `both` and ranking is clearly better; on a repetitive
  corpus hybrid mostly reshuffles near-identical documents. `matched_by` tells
  you which case you are in.

### When to graduate to Azure AI Search

Graduate when any of these becomes true:

* more than ~10k documents, or the vector matrix no longer fits comfortably in RAM
* **more than one person** uses it — a local index has no per-user permission
  trimming, so it would show every user everything the *indexer* could see
* you want the index refreshed on a schedule rather than when you remember
* you want semantic re-ranking, query rewriting, or answer synthesis

The migration is smaller than it looks, because the hard parts — extraction,
chunking, schema cards — are the same. What changes is where chunks and vectors
live. Two routes:

1. **`remoteSharePoint` knowledge source** (2026-05-01-preview) — zero-copy: no
   index, no copy of the files. It calls the M365 Copilot Retrieval API under
   the covers, and query-time ACL **and sensitivity-label** trimming come free by
   passing the user's token in `x-ms-query-source-authorization`. Limits: 200
   requests/user/hour, 25 results, text only.
2. **Push your own index** — this server's extraction feeds an AI Search index
   directly. Note the native *SharePoint-in-M365 indexer* is **not** an option
   for tenants with Conditional Access: its limitations page says they are not
   supported.

And the part that makes the transition cheap: **an Azure AI Search knowledge base
is itself an MCP server**, at
`https://<svc>.search.windows.net/knowledgebases/<kb>/mcp?api-version=2026-05-01-preview`,
exposing a `knowledge_base_retrieve` tool. Claude Code points at that URL instead
of this server, and the tool contract barely changes. Check that your region
supports agentic retrieval and the semantic ranker.

---

## Security

`index_folder` runs a redacting secret/PII sweep over every file's text:
passwords, connection strings, API keys, AWS keys, SAS signatures, private keys,
JWTs, SIN/SSN, card numbers. Matches are stored per file and surfaced by
`risk_findings` with the value **redacted** — all patterns are applied to every
snippet, because a flagged password line routinely sits next to an access key,
and a partial redaction is not a redaction.

Redaction is deliberately paranoid, for a reason found during the first real
crawl: detection patterns match the *marker* (`-----BEGIN PRIVATE KEY-----`,
`pwd=`), but the secret is the bytes **after** it, which no detection pattern
covers — so a context snippet still carried PEM key material. Two extra rules
now apply on top of every redaction:

* opaque-run scrubbing (16+ base64/hex characters that don't look like a word),
  because a snippet is cut mid-token and a 64-character PEM line can arrive as
  31 characters and slip under any length threshold;
* when a file trips a **high-severity** pattern, every long run in every snippet
  from that file is scrubbed unconditionally — over-redacting a triage hint
  costs nothing, under-redacting republishes the key.

This is not a theoretical control: on a real 270-file crawl it flagged 6
files, two of them genuine plaintext credentials. The rest were documents that
merely *reference* Key Vault secret names — correct practice, flagged for
review, not findings. Expect roughly that ratio and triage accordingly.

**Note what this does not fix:** the *index itself* still contains the document
text, secrets included — that is inherent to any RAG index over a corpus like
this. Redaction protects the transcript, not the database. Treat `data/index.db`
as sensitive, and sweep the source folder.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `list_libraries` returns `[]`, `/drive` gives `accessDenied` | the client id has no `Files.Read.All` / `Sites.Read.All` — see Permissions |
| Process exits with code 5, no traceback | WAM with no console window. Run `setup_auth.py` from a real terminal |
| `Unexpected UTF-8 BOM` | `config.json` was saved by PowerShell's `Set-Content -Encoding utf8`. Handled now (read as `utf-8-sig`), but prefer an editor that writes plain UTF-8 |
| `Library 'X' not found` | the error lists the real library names; use one |
| PDF indexed with 0 useful chunks | no text layer — it is a scan. Needs OCR |
| `AADSTS530…` on sign-in | device-code flow. Use broker or interactive |
| Search returns nothing sensible | check `index_status` → `vector_coverage_pct`. 0% means BM25 only; add an embedding backend |

## Files

| File | Role |
|---|---|
| `server.py` | MCP tool surface |
| `graph.py` | auth + Microsoft Graph client |
| `extract.py` | per-format extraction, profiling, chunking, secret sweep |
| `index.py` | SQLite catalogue + FTS5 + vectors + hybrid search |
| `embed.py` | pluggable embedding backends |
| `pipeline.py` | the crawl→index pipeline; also runnable headless |
| `selftest.py` | 35 offline checks |
| `setup_auth.py` | one-time interactive sign-in |
