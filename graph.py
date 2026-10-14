"""Auth + thin Microsoft Graph client for the SharePoint MCP.

Delegated auth by default (WAM broker). That matters: a SharePoint reader
running app-only `Sites.Read.All` can read the whole tenant, so anything it
surfaces bypasses the library's own permissions. Delegated means this server
can only ever see what the signed-in user can already see in the browser.

Config lives in `config.json`; no secrets in it (a service-principal config
names an env var that is loaded from `.env`).
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import time
import urllib.parse

import requests
from dotenv import load_dotenv

_HERE = pathlib.Path(__file__).parent
load_dotenv(_HERE / ".env")
# utf-8-sig: a BOM is easy to introduce by editing this file from PowerShell.
_CFG = json.loads((_HERE / "config.json").read_text(encoding="utf-8-sig"))

GRAPH = _CFG.get("base_url", "https://graph.microsoft.com/v1.0")
SCOPE = _CFG.get("scope", "https://graph.microsoft.com/.default")
HOSTNAME = _CFG.get("hostname", "contoso.sharepoint.com")
_TIMEOUT = _CFG.get("timeout_seconds", 90)
_MAX_DOWNLOAD = int(_CFG.get("max_download_mb", 80)) * 1024 * 1024

_cred = None
_AUTH_RECORD = _HERE / ".auth_record.json"


# --------------------------------------------------------------------------- #
# auth
# --------------------------------------------------------------------------- #
_op_cred = None


def _operator_credential():
    """The human's own WAM sign-in.

    Used for Key Vault even when SharePoint itself is reached app-only, so the
    app's certificate can be fetched without ever being stored locally.
    """
    global _op_cred
    if _op_cred is None:
        _op_cred = _build_broker(_CFG.get("tenant_id"),
                                 "https://vault.azure.net/.default")
    return _op_cred


def _build_broker(tenant, first_scope):
    import ctypes
    from azure.identity import AuthenticationRecord, TokenCachePersistenceOptions
    from azure.identity.broker import InteractiveBrowserBrokerCredential
    try:
        handle = (ctypes.windll.kernel32.GetConsoleWindow()
                  or ctypes.windll.user32.GetForegroundWindow())
    except Exception:
        handle = 0

    record = None
    if _AUTH_RECORD.exists():
        try:
            record = AuthenticationRecord.deserialize(
                _AUTH_RECORD.read_text(encoding="utf-8"))
        except Exception:
            record = None

    # WAM needs a window to parent its dialog to. With no console (the MCP
    # server is launched headless) the native broker aborts the whole process,
    # so only go interactive when there is a window. Once the record + DPAPI
    # cache exist, renewal is silent and the window no longer matters.
    if not handle and record is None:
        raise RuntimeError(
            "No interactive sign-in cached and no console window to show the "
            "Windows account prompt. Run `setup_auth.py` once from a real "
            "terminal (or set \"auth\": \"interactive\" in config.json).")

    kwargs = dict(tenant_id=tenant, parent_window_handle=handle or 0,
                  cache_persistence_options=TokenCachePersistenceOptions(
                      name="mcp-sharepoint"))
    if _CFG.get("client_id") and _CFG.get("auth") == "broker":
        kwargs["client_id"] = _CFG["client_id"]
    elif _CFG.get("operator_client_id"):
        kwargs["client_id"] = _CFG["operator_client_id"]
    if record is not None:
        kwargs["authentication_record"] = record
    elif _CFG.get("use_default_broker_account", True):
        kwargs["use_default_broker_account"] = True
    elif _CFG.get("login_hint"):
        # login_hint and the account picker are mutually exclusive; passing both
        # makes MSAL drop the hint with a warning.
        kwargs["login_hint"] = _CFG["login_hint"]

    cred = InteractiveBrowserBrokerCredential(**kwargs)
    if record is None:
        rec = cred.authenticate(scopes=[first_scope])
        _AUTH_RECORD.write_text(rec.serialize(), encoding="utf-8")
    return cred


def _credential():
    global _cred
    if _cred is not None:
        return _cred
    auth = _CFG.get("auth", "broker")
    tenant = _CFG.get("tenant_id")
    if auth == "broker":
        _cred = _build_broker(tenant, SCOPE)
    elif auth == "interactive":
        from azure.identity import (AuthenticationRecord, InteractiveBrowserCredential,
                                    TokenCachePersistenceOptions)
        record = None
        if _AUTH_RECORD.exists():
            try:
                record = AuthenticationRecord.deserialize(
                    _AUTH_RECORD.read_text(encoding="utf-8"))
            except Exception:
                record = None
        _cred = InteractiveBrowserCredential(
            tenant_id=tenant, client_id=_CFG.get("client_id"),
            login_hint=_CFG.get("login_hint"), authentication_record=record,
            cache_persistence_options=TokenCachePersistenceOptions(name="mcp-sharepoint"))
        if record is None:
            rec = _cred.authenticate(scopes=[SCOPE])
            _AUTH_RECORD.write_text(rec.serialize(), encoding="utf-8")
    elif auth == "service-principal":
        from azure.identity import ClientSecretCredential
        _cred = ClientSecretCredential(
            tenant_id=_CFG["tenant_id"], client_id=_CFG["client_id"],
            client_secret=os.environ[_CFG["client_secret_env"]])
    elif auth == "service-principal-cert":
        from azure.identity import CertificateCredential
        kv = _CFG.get("certificate_keyvault")
        if kv:
            # Pull the cert with the operator's own identity; nothing on disk.
            import keyvault
            data, password = keyvault.certificate(_operator_credential(), kv)
            _cred = CertificateCredential(
                tenant_id=_CFG["tenant_id"], client_id=_CFG["client_id"],
                certificate_data=data, password=password)
        else:
            _cred = CertificateCredential(
                tenant_id=_CFG["tenant_id"], client_id=_CFG["client_id"],
                certificate_path=_CFG["certificate_path"],
                password=os.environ.get(_CFG.get("certificate_password_env") or "_none_"))
    elif auth == "azure-cli":
        from azure.identity import AzureCliCredential
        _cred = AzureCliCredential(tenant_id=tenant) if tenant else AzureCliCredential()
    elif auth == "default":
        from azure.identity import DefaultAzureCredential
        _cred = DefaultAzureCredential()
    else:
        raise ValueError(f"Unknown auth type '{auth}'.")
    return _cred


def _token() -> str:
    return _credential().get_token(SCOPE).token


def _headers(extra: dict | None = None) -> dict:
    h = {"Authorization": f"Bearer {_token()}", "Accept": "application/json"}
    if extra:
        h.update(extra)
    return h


def is_app_only() -> bool:
    return _CFG.get("auth", "broker") in ("service-principal", "service-principal-cert")


REQUIRED_SCOPES = ("Files.Read.All", "Sites.Read.All")


def token_scopes() -> list:
    """The delegated scopes on the current token, read from its own `scp` claim.

    Worth the 10 lines: when the client app lacks SharePoint permissions, Graph
    does not 403 on `/sites/{id}/drives` - it returns an empty list, which reads
    as 'this site has no libraries' and sends you looking in the wrong place.
    """
    import base64
    try:
        payload = _token().split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return sorted((claims.get("scp") or claims.get("roles") or "").split()
                      if isinstance(claims.get("scp") or claims.get("roles"), str)
                      else (claims.get("roles") or []))
    except Exception:
        return []


def scope_diagnosis() -> dict | None:
    """None when the token can read SharePoint; an explanation when it cannot."""
    if is_app_only():
        return None
    scopes = token_scopes()
    if not scopes:
        return None
    missing = [s for s in REQUIRED_SCOPES if s not in scopes]
    if not missing:
        return None
    return {
        "problem": "The signed-in token has no SharePoint permissions.",
        "missing_delegated_scopes": missing,
        "client_id": _CFG.get("client_id"),
        "symptom": "Drive/library lists come back EMPTY instead of 403.",
        "fix": "Point config.json 'client_id' at an app registration that holds "
               "delegated Files.Read.All + Sites.Read.All (see IT-REQUEST.md).",
        "token_scopes": scopes,
    }


# --------------------------------------------------------------------------- #
# REST plumbing
# --------------------------------------------------------------------------- #
class GraphError(RuntimeError):
    pass


def _url(path: str) -> str:
    return path if path.startswith("http") else f"{GRAPH}{path}"


def get(path: str, params: dict | None = None, retries: int = 3) -> dict:
    for attempt in range(retries + 1):
        r = requests.get(_url(path), headers=_headers(), params=params, timeout=_TIMEOUT)
        if (r.status_code == 429 or r.status_code >= 500) and attempt < retries:
            time.sleep(int(r.headers.get("Retry-After", 2 ** attempt)))
            continue
        if not r.ok:
            raise GraphError(f"{r.status_code} GET {r.url}\n{r.text[:800]}")
        return r.json() if r.content else {}
    raise GraphError("exhausted retries")


def post(path: str, body: dict) -> dict:
    r = requests.post(_url(path), headers=_headers({"Content-Type": "application/json"}),
                      json=body, timeout=_TIMEOUT)
    if not r.ok:
        raise GraphError(f"{r.status_code} POST {r.url}\n{r.text[:800]}")
    return r.json() if r.content else {}


def paged(path: str, params: dict | None = None, max_items: int | None = None) -> list:
    """Follow @odata.nextLink, collecting `value` arrays."""
    out, nxt, first = [], None, True
    while True:
        data = get(nxt or path, params if first else None)
        first = False
        out.extend(data.get("value", []))
        nxt = data.get("@odata.nextLink")
        if not nxt or (max_items and len(out) >= max_items):
            break
    return out[:max_items] if max_items else out


# --------------------------------------------------------------------------- #
# sites & drives
# --------------------------------------------------------------------------- #
def whoami() -> dict:
    if is_app_only():
        return {"auth": _CFG.get("auth"),
                "identity": "app-only service principal",
                "warning": "App-only sees the whole tenant - results are NOT "
                           "permission-trimmed for whoever uses this agent."}
    me = get("/me", {"$select": "displayName,userPrincipalName,mail,id,jobTitle"})
    return {"auth": _CFG.get("auth"), "identity": me.get("userPrincipalName"),
            "displayName": me.get("displayName"), "id": me.get("id"),
            "trimming": "delegated - you only see what this account can see"}


# --------------------------------------------------------------------------- #
# allowlist
# --------------------------------------------------------------------------- #
# A GUARDRAIL, NOT A SECURITY BOUNDARY. The token decides what is reachable;
# this only decides what this server is willing to ask for. Anyone who can edit
# config.json can widen it. It exists because the app-only identity currently in
# use holds tenant-wide Sites.Read.All - 138 site collections, including HR and
# Health & Safety - and nothing about normal use of this tool should ever go
# near them. Real trimming comes from delegated auth (see IT-REQUEST.md).
_ALLOW = _CFG.get("allowlist") or {}
_allow_cache: dict | None = None
_resolving_allow = False


def allowlist_enabled() -> bool:
    return bool(_ALLOW.get("enabled"))


def _allowed() -> dict:
    """Resolve the configured site and library names to ids, once."""
    global _allow_cache, _resolving_allow
    if _allow_cache is not None:
        return _allow_cache
    _resolving_allow = True                 # suppress checks while building the set
    try:
        sites, drives = set(), set()
        libs = [x.lower() for x in (_ALLOW.get("libraries") or [])]
        for s in _ALLOW.get("sites") or []:
            info = resolve_site(s)
            sites.add(info["id"])
            for d in list_drives(s):
                if not libs or (d["name"] or "").lower() in libs:
                    drives.add(d["id"])
        _allow_cache = {"sites": sites, "drives": drives}
    finally:
        _resolving_allow = False
    return _allow_cache


def _deny(what: str, detail: str) -> "GraphError":
    return GraphError(json.dumps({
        "blocked_by": "config allowlist",
        "what": what,
        "detail": detail,
        "allowed_sites": _ALLOW.get("sites") or [],
        "allowed_libraries": _ALLOW.get("libraries") or [],
        "allowed_folders": _ALLOW.get("folders") or [],
        "note": "This is a local guardrail in config.json, not a permission. "
                "Widen it deliberately if the request is legitimate.",
    }, indent=2))


def _check_site(site_id: str, label: str) -> None:
    if not allowlist_enabled() or _resolving_allow:
        return
    if site_id not in _allowed()["sites"]:
        raise _deny("site", f"'{label}' is outside the configured sites")


def _check_drive(drive_id: str) -> None:
    if not allowlist_enabled() or _resolving_allow:
        return
    if drive_id not in _allowed()["drives"]:
        raise _deny("library", "that document library is outside the configured libraries")


def _check_folder(folder: str) -> None:
    prefixes = [p.strip("/").lower() for p in (_ALLOW.get("folders") or []) if p.strip("/")]
    if not allowlist_enabled() or _resolving_allow or not prefixes:
        return
    q = (folder or "").strip("/").lower()
    if not any(q == p or q.startswith(p + "/") for p in prefixes):
        raise _deny("folder", f"'{folder or '(library root)'}' is outside the configured folders")


def allowlist_status() -> dict:
    if not allowlist_enabled():
        return {"enabled": False,
                "warning": "No allowlist. The server will read anything the signed-in "
                           "identity can reach."}
    out = {"enabled": True,
           "sites": _ALLOW.get("sites") or [],
           "libraries": _ALLOW.get("libraries") or [],
           "folders": _ALLOW.get("folders") or ["(whole library)"],
           "note": "Guardrail only - it limits what this server asks for, not what "
                   "the credential could reach."}
    try:
        a = _allowed()
        out["resolved"] = {"sites": len(a["sites"]), "libraries": len(a["drives"])}
    except Exception as exc:
        out["resolve_error"] = str(exc)[:200]
    return out


_SITE_CACHE: dict[str, dict] = {}


def resolve_site(site: str | None = None) -> dict:
    """Accept a site path ('Team' or '/sites/Team'), a full URL, or a site id."""
    site = site or _CFG.get("default_site") or ""
    key = site.lower()
    if key in _SITE_CACHE:
        return _SITE_CACHE[key]
    if not site:
        out = get(f"/sites/{HOSTNAME}")
    elif re.match(r"^[\w.-]+,[0-9a-f-]{36},[0-9a-f-]{36}$", site):
        out = get(f"/sites/{site}")
    elif site.startswith("http"):
        u = urllib.parse.urlparse(site)
        parts = [p for p in u.path.split("/") if p]
        if len(parts) >= 2 and parts[0].lower() in ("sites", "teams"):
            out = get(f"/sites/{u.netloc}:/{parts[0]}/{parts[1]}")
        else:
            out = get(f"/sites/{u.netloc}")
    else:
        p = site.strip("/")
        if not p.lower().startswith(("sites/", "teams/")):
            p = f"sites/{p}"
        out = get(f"/sites/{HOSTNAME}:/{p}")
    slim = {"id": out["id"], "name": out.get("name"),
            "displayName": out.get("displayName"), "webUrl": out.get("webUrl")}
    _check_site(slim["id"], site or "(default site)")
    _SITE_CACHE[key] = slim
    return slim


def search_sites(query: str, limit: int = 20) -> list:
    data = get("/sites", {"search": query, "$top": limit})
    rows = [{"id": s["id"], "name": s.get("name"), "displayName": s.get("displayName"),
             "webUrl": s.get("webUrl")} for s in data.get("value", [])]
    if allowlist_enabled() and not _resolving_allow:
        # Filter rather than fail: site discovery is how someone works out what
        # they are allowed to point at, and a bare refusal teaches nothing.
        allowed = _allowed()["sites"]
        kept = [r for r in rows if r["id"] in allowed]
        for r in kept:
            r["allowed"] = True
        if len(kept) < len(rows):
            kept.append({"note": f"{len(rows) - len(kept)} further site(s) matched but are "
                                 f"outside the config allowlist and were withheld",
                         "allowed_sites": _ALLOW.get("sites") or []})
        return kept[:limit + 1]
    return rows[:limit]


def list_drives(site: str | None = None) -> list:
    s = resolve_site(site)
    out = paged(f"/sites/{s['id']}/drives", {"$top": 200})
    if not out:
        diag = scope_diagnosis()
        if diag:
            raise GraphError(json.dumps(diag, indent=2))
    return [{"id": d["id"], "name": d.get("name"), "webUrl": d.get("webUrl"),
             "driveType": d.get("driveType"),
             "quotaUsedMB": round((d.get("quota") or {}).get("used", 0) / 1e6, 1)}
            for d in out]


_DRIVE_CACHE: dict[str, str] = {}


def resolve_drive(site: str | None = None, library: str | None = None) -> str:
    """Return a driveId for a document-library name (default from config)."""
    library = library or _CFG.get("default_library") or "Documents"
    if library.startswith("b!"):
        # A raw drive id bypasses site and library resolution, so it has to be
        # checked directly or it would be a hole straight through the allowlist.
        _check_drive(library)
        return library
    key = f"{site or ''}|{library}".lower()
    if key in _DRIVE_CACHE:
        return _DRIVE_CACHE[key]
    drives = list_drives(site)
    for d in drives:
        if (d["name"] or "").lower() == library.lower():
            _check_drive(d["id"])
            _DRIVE_CACHE[key] = d["id"]
            return d["id"]
    names = ", ".join(d["name"] or "?" for d in drives)
    raise GraphError(f"Library '{library}' not found. Available: {names}")


# --------------------------------------------------------------------------- #
# items
# --------------------------------------------------------------------------- #
_SELECT = ("id,name,size,webUrl,eTag,cTag,lastModifiedDateTime,createdDateTime,"
           "file,folder,parentReference,createdBy,lastModifiedBy")


def _item_path(it: dict) -> str:
    raw = ((it.get("parentReference") or {}).get("path") or "")
    folder = raw.split("root:", 1)[-1] if "root:" in raw else ""
    return urllib.parse.unquote(f"{folder}/{it.get('name', '')}".lstrip("/"))


def slim_item(it: dict) -> dict:
    f = it.get("file") or {}
    hashes = f.get("hashes") or {}
    return {
        "item_id": it.get("id"),
        "name": it.get("name"),
        "path": _item_path(it),
        "is_folder": "folder" in it,
        "child_count": (it.get("folder") or {}).get("childCount"),
        "size": it.get("size", 0),
        "ext": pathlib.PurePosixPath(it.get("name", "")).suffix.lower().lstrip("."),
        "mime": f.get("mimeType"),
        "hash": hashes.get("quickXorHash") or hashes.get("sha256Hash"),
        "modified": it.get("lastModifiedDateTime"),
        "created": it.get("createdDateTime"),
        "modified_by": ((it.get("lastModifiedBy") or {}).get("user") or {}).get("displayName"),
        "created_by": ((it.get("createdBy") or {}).get("user") or {}).get("displayName"),
        "etag": it.get("cTag") or it.get("eTag"),
        "web_url": it.get("webUrl"),
    }


def _folder_ref(drive_id: str, folder: str = "") -> str:
    folder = (folder or "").strip("/")
    if not folder:
        return f"/drives/{drive_id}/root"
    return f"/drives/{drive_id}/root:/{urllib.parse.quote(folder)}:"


def list_children(drive_id: str, folder: str = "", limit: int = 2000) -> list:
    _check_drive(drive_id)
    _check_folder(folder)
    items = paged(f"{_folder_ref(drive_id, folder)}/children",
                  {"$top": 200, "$select": _SELECT}, max_items=limit)
    return [slim_item(i) for i in items]


def get_item(drive_id: str, folder_path: str) -> dict:
    _check_drive(drive_id)
    _check_folder(folder_path)
    return slim_item(get(_folder_ref(drive_id, folder_path), {"$select": _SELECT}))


def get_item_by_id(drive_id: str, item_id: str) -> dict:
    _check_drive(drive_id)
    it = slim_item(get(f"/drives/{drive_id}/items/{item_id}", {"$select": _SELECT}))
    _check_folder(str(pathlib.PurePosixPath(it["path"]).parent))
    return it


def walk(drive_id: str, folder: str = "", recursive: bool = True,
         max_files: int = 5000, max_depth: int = 12) -> tuple[list, list]:
    """Breadth-first walk of a folder subtree. Returns (files, folders)."""
    files: list = []
    folders: list = []
    queue = [((folder or "").strip("/"), 0)]
    while queue and len(files) < max_files:
        cur, depth = queue.pop(0)
        for it in list_children(drive_id, cur):
            if it["is_folder"]:
                folders.append(it)
                if recursive and depth < max_depth:
                    queue.append((it["path"], depth + 1))
            else:
                files.append(it)
                if len(files) >= max_files:
                    break
    return files, folders


def search_drive(drive_id: str, query: str, limit: int = 50) -> list:
    _check_drive(drive_id)
    q = urllib.parse.quote(query.replace("'", "''"))
    items = paged(f"/drives/{drive_id}/root/search(q='{q}')",
                  {"$top": 100, "$select": _SELECT}, max_items=limit)
    out = [slim_item(i) for i in items]
    prefixes = [p.strip("/").lower() for p in (_ALLOW.get("folders") or []) if p.strip("/")]
    if allowlist_enabled() and prefixes and not _resolving_allow:
        # Search spans the whole library, so filter its results to the allowed
        # folders rather than refusing the call outright.
        out = [i for i in out
               if any(i["path"].lower().startswith(p + "/") or i["path"].lower() == p
                      for p in prefixes)]
    return out


def download(drive_id: str, item_id: str, max_bytes: int | None = None) -> bytes:
    """Download item content.

    Graph 302-redirects to pre-authenticated storage; the Authorization header
    must NOT follow it (storage rejects it), so redirects are handled by hand.
    """
    _check_drive(drive_id)          # the last gate before bytes actually move
    cap = max_bytes or _MAX_DOWNLOAD
    r = requests.get(_url(f"/drives/{drive_id}/items/{item_id}/content"),
                     headers=_headers(), allow_redirects=False,
                     timeout=_TIMEOUT, stream=True)
    if r.status_code in (301, 302, 303, 307, 308):
        loc = r.headers["Location"]
        r.close()
        r = requests.get(loc, timeout=_TIMEOUT, stream=True)
    if not r.ok:
        raise GraphError(f"download {r.status_code}: {r.text[:300]}")
    buf, total = bytearray(), 0
    for chunk in r.iter_content(262144):
        buf += chunk
        total += len(chunk)
        if total > cap:
            r.close()
            raise GraphError(f"file exceeds max_download_mb ({cap // 1048576} MB)")
    return bytes(buf)


def config() -> dict:
    return {k: v for k, v in _CFG.items() if "secret" not in k.lower()}
