"""Pluggable embedding backends.

Retrieval quality is a dial, not a yes/no. This server works with no embeddings
at all (BM25 over SQLite FTS5 is genuinely good on a corpus of named business
documents), and gets better as you plug a real embedder in:

  none          BM25 only. Zero cost, zero setup, no semantic recall:
                "closing dates" will not find a file that says "possession".
  fastembed     Local ONNX (BAAI/bge-small-en-v1.5, 384-d). ~130 MB model
                downloaded once from HuggingFace, then fully offline. No torch.
  azure_openai  text-embedding-3-small/large via an Azure OpenAI deployment.
                Best quality; data stays in your Azure tenant/region.
  openai        Same models on api.openai.com. Only if that is allowed.

`auto` picks azure_openai -> openai -> fastembed -> none, in that order.

Whichever backend indexed the corpus is recorded in the DB. Mixing vectors from
two different models silently produces garbage rankings, so index.py refuses to
query when the stored backend and the live one disagree.
"""
from __future__ import annotations

import os
import time

import numpy as np


class Embedder:
    name = "none"
    dim = 0

    def encode(self, texts: list[str], kind: str = "passage") -> np.ndarray | None:
        return None

    def info(self) -> dict:
        return {"backend": self.name, "dim": self.dim}


class NoEmbedder(Embedder):
    """Lexical only. Explicit, not a silent failure."""


class FastEmbed(Embedder):
    def __init__(self, model: str = "BAAI/bge-small-en-v1.5"):
        from fastembed import TextEmbedding
        self._m = TextEmbedding(model_name=model)
        self.name = f"fastembed:{model}"
        self.dim = len(next(iter(self._m.embed(["probe"]))))

    def encode(self, texts, kind="passage"):
        vecs = np.array(list(self._m.embed(list(texts))), dtype=np.float32)
        return _l2(vecs)


class AzureOpenAIEmbed(Embedder):
    """Azure OpenAI embeddings.

    Needs AZURE_OPENAI_ENDPOINT + AZURE_OPENAI_EMBED_DEPLOYMENT, and either
    AZURE_OPENAI_API_KEY or nothing at all (falls back to the same Entra
    credential the Graph client uses, via Cognitive Services User).
    """

    def __init__(self, deployment: str | None = None, endpoint: str | None = None,
                 api_version: str = "2024-10-21", batch: int = 96):
        self.endpoint = (endpoint or os.environ["AZURE_OPENAI_ENDPOINT"]).rstrip("/")
        self.deployment = deployment or os.environ["AZURE_OPENAI_EMBED_DEPLOYMENT"]
        self.api_version = api_version
        self.batch = batch
        self.key = os.environ.get("AZURE_OPENAI_API_KEY")
        self.name = f"azure_openai:{self.deployment}"
        self.dim = len(self.encode(["probe"])[0])

    def _headers(self):
        if self.key:
            return {"api-key": self.key, "Content-Type": "application/json"}
        import graph                                   # reuse the signed-in identity
        tok = graph._credential().get_token(
            "https://cognitiveservices.azure.com/.default").token
        return {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}

    def encode(self, texts, kind="passage"):
        import requests
        url = (f"{self.endpoint}/openai/deployments/{self.deployment}/embeddings"
               f"?api-version={self.api_version}")
        out = []
        texts = list(texts)
        for i in range(0, len(texts), self.batch):
            part = [t[:8000] for t in texts[i:i + self.batch]]
            for attempt in range(4):
                r = requests.post(url, headers=self._headers(),
                                  json={"input": part}, timeout=120)
                if r.status_code == 429:
                    time.sleep(int(r.headers.get("Retry-After", 2 ** attempt)))
                    continue
                r.raise_for_status()
                break
            else:
                raise RuntimeError("Azure OpenAI embeddings: throttled out")
            out.extend(d["embedding"] for d in r.json()["data"])
        return _l2(np.array(out, dtype=np.float32))


class OpenAIEmbed(Embedder):
    def __init__(self, model: str = "text-embedding-3-small", batch: int = 128):
        self.model, self.batch = model, batch
        self.key = os.environ["OPENAI_API_KEY"]
        self.name = f"openai:{model}"
        self.dim = len(self.encode(["probe"])[0])

    def encode(self, texts, kind="passage"):
        import requests
        out, texts = [], list(texts)
        for i in range(0, len(texts), self.batch):
            r = requests.post(
                "https://api.openai.com/v1/embeddings",
                headers={"Authorization": f"Bearer {self.key}"},
                json={"model": self.model,
                      "input": [t[:8000] for t in texts[i:i + self.batch]]},
                timeout=120)
            r.raise_for_status()
            out.extend(d["embedding"] for d in r.json()["data"])
        return _l2(np.array(out, dtype=np.float32))


def _l2(v: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(v, axis=1, keepdims=True)
    n[n == 0] = 1.0
    return (v / n).astype(np.float32)


def from_stored_name(stored: str) -> Embedder | None:
    """Rebuild the exact embedder an index was built with.

    Stored names carry the model ('fastembed:BAAI/bge-small-en-v1.5'), so a
    query must not be encoded by whatever the default happens to be now -
    different model, different vector space, quietly wrong ranking.
    """
    if not stored or stored == "none":
        return None
    backend, _, detail = stored.partition(":")
    kw = {}
    if detail:
        kw = {"fastembed": {"model": detail},
              "openai": {"model": detail},
              "azure_openai": {"deployment": detail}}.get(backend, {})
    emb = get(backend, **kw)
    if emb.name != stored:
        raise RuntimeError(
            f"index was built with '{stored}' but the live backend resolves to "
            f"'{emb.name}'. Vectors from two models are not comparable.")
    return emb


_CACHE: dict[str, Embedder] = {}


def get(backend: str = "auto", **kw) -> Embedder:
    key = f"{backend}|{sorted(kw.items())}"     # kwargs pick the model, so they key the cache
    if key in _CACHE:
        return _CACHE[key]
    order = ([backend] if backend not in ("auto", None)
             else ["azure_openai", "openai", "fastembed", "none"])
    last = None
    for b in order:
        try:
            if b == "none":
                e: Embedder = NoEmbedder()
            elif b == "fastembed":
                e = FastEmbed(**kw)
            elif b == "azure_openai":
                e = AzureOpenAIEmbed(**kw)
            elif b == "openai":
                e = OpenAIEmbed(**kw)
            else:
                raise ValueError(f"unknown embedding backend '{b}'")
            _CACHE[key] = e
            return e
        except Exception as exc:                       # not configured / not installed
            last = f"{b}: {exc}"
            continue
    raise RuntimeError(f"no embedding backend available ({last})")
