"""Background LocalAI indexing. Stored vectors never supply returned records."""
import asyncio
from datetime import datetime, timezone
import math

import aiohttp
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store

from .catalogue import Redactor, revision, search_chunks, hybrid_search, ranked_search


def unit_vector(value):
    if not isinstance(value, list) or not value or any(
        isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x)
        for x in value
    ):
        raise ValueError("Invalid embedding vector.")
    norm = math.sqrt(sum(x * x for x in value))
    if not math.isfinite(norm) or norm == 0:
        raise ValueError("Invalid embedding norm.")
    return [x / norm for x in value]


class EmbeddingIndex:
    def __init__(self, hass, entry, manager):
        self.hass, self.manager = hass, manager
        self.store = Store(hass, 1, f"dashboard_mcp.embeddings.{entry.entry_id}")
        self.signal = f"dashboard_mcp_index_{entry.entry_id}"
        self.status = dict(state="disabled", indexed_records=0, total_records=0,
                           completed_chunks=0, total_chunks=0, indexed_revision=None,
                           last_completed=None, last_error=None)
        self.vectors, self.identity, self.target = {}, None, None
        self.generation, self.task = 0, None
        self.wake = asyncio.Event()
        self.query_cache = {}
        self.query_lock = asyncio.Lock()

    def notify(self, **values):
        self.status.update(values)
        async_dispatcher_send(self.hass, self.signal)

    def settings(self):
        o = self.manager.options
        return {k: o[k] for k in (
            "semantic_search", "embedding_base_url", "embedding_api_key", "embedding_model",
            "embedding_batch_size", "embedding_min_similarity", "dashboards", "blocked_entities",
            "keep_image_paths")}

    def invalidate(self):
        self.generation += 1
        self.query_cache.clear()
        self.notify(state="updating" if self.manager.options["semantic_search"] else "disabled")
        self.wake.set()

    async def start(self):
        self.task = self.hass.async_create_background_task(self.run(), "Dashboard MCP embedding index")

    async def stop(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

    async def request(self, texts, settings):
        base = settings["embedding_base_url"].strip().rstrip("/")
        model = settings["embedding_model"].strip()
        if not base.startswith(("http://", "https://")) or not model:
            raise ValueError("Configure an HTTP(S) embedding base URL and model name.")
        headers = {}
        if settings["embedding_api_key"]:
            headers["Authorization"] = "Bearer " + settings["embedding_api_key"]
        session = async_get_clientsession(self.hass)
        async with session.post(base + "/embeddings", json={"model": model, "input": texts},
                                headers=headers, timeout=aiohttp.ClientTimeout(total=60),
                                allow_redirects=False) as response:
            if response.status != 200:
                raise ValueError(f"Embedding endpoint returned HTTP {response.status}.")
            body = await response.json()
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list) or len(data) != len(texts):
            raise ValueError("Embedding response count does not match inputs.")
        if any(not isinstance(x, dict) or type(x.get("index")) is not int for x in data):
            raise ValueError("Embedding response is missing integer indices.")
        if sorted(x["index"] for x in data) != list(range(len(texts))):
            raise ValueError("Embedding response indices do not match inputs.")
        vectors = [unit_vector(x.get("embedding")) for x in sorted(data, key=lambda x: x["index"])]
        if len({len(v) for v in vectors}) != 1:
            raise ValueError("Embedding response dimensions differ.")
        return vectors

    @staticmethod
    def documents(records, settings):
        redactor = Redactor(settings["blocked_entities"])
        documents, record_chunks = {}, {}
        for record in records:
            if record.fields["entity_id"] in redactor.blocked:
                continue
            chunks = search_chunks(redactor.clean(record.fields))
            record_chunks[record.id] = list(chunks)
            documents.update(chunks)
        return documents, record_chunks

    async def run(self):
        try:
            saved = await self.store.async_load()
            if saved:
                vectors = {k: unit_vector(v) for k, v in saved["vectors"].items()}
                if len({len(v) for v in vectors.values()}) > 1:
                    raise ValueError("Stored dimensions differ.")
                self.vectors, self.identity = vectors, saved["identity"]
                self.status["last_completed"] = saved.get("last_completed")
        except Exception:
            self.vectors, self.identity = {}, None
        while self.manager.active:
            self.wake.clear()
            try:
                await self.update()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Exclude server response bodies and URLs, which may contain secrets.
                self.notify(state="error", last_error=f"Index update failed ({type(exc).__name__}).")
            if self.wake.is_set():
                continue
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=60)
            except asyncio.TimeoutError:
                pass

    async def update(self):
        settings, generation = self.settings(), self.generation
        if not settings["semantic_search"]:
            self.vectors, self.identity, self.target = {}, None, None
            await self.store.async_remove()
            self.notify(state="disabled", indexed_records=0, total_records=0,
                        completed_chunks=0, total_chunks=0, indexed_revision=None, last_error=None)
            return
        identity = revision([settings["embedding_base_url"], settings["embedding_model"],
                             sorted(settings["blocked_entities"]), "chunks-v1"])
        if identity != self.identity:
            self.vectors, self.identity, self.target = {}, identity, None
            self.query_cache.clear()
            await self.store.async_remove()
        _, _, _, records, _ = await self.manager.snapshot()
        documents, record_chunks = self.documents(records, settings)
        target = revision([identity, record_chunks])
        if target == self.target and self.status["state"] == "ready":
            return
        self.notify(state="updating" if self.target else "indexing", total_records=len(records),
                    total_chunks=len(documents), last_error=None)
        self.vectors = {k: v for k, v in self.vectors.items() if k in documents}
        await self.save()
        missing = [k for k in documents if k not in self.vectors]
        self.progress(records, record_chunks)
        for start in range(0, len(missing), settings["embedding_batch_size"]):
            if generation != self.generation or settings != self.settings():
                self.wake.set()
                return
            keys = missing[start:start + settings["embedding_batch_size"]]
            vectors = await self.request([documents[k] for k in keys], settings)
            if generation != self.generation or settings != self.settings():
                self.wake.set()
                return
            if self.vectors and len(next(iter(self.vectors.values()))) != len(vectors[0]):
                self.vectors = {}
                await self.store.async_remove()
                raise ValueError("Embedding dimensions changed.")
            self.vectors.update(zip(keys, vectors))
            self.progress(records, record_chunks)
            await self.save()
        # A dashboard may change during the LocalAI requests.
        _, _, _, latest, _ = await self.manager.snapshot()
        _, latest_chunks = self.documents(latest, settings)
        if generation != self.generation or settings != self.settings() or revision([identity, latest_chunks]) != target:
            self.wake.set()
            return
        self.target = target
        self.query_cache.clear()
        self.notify(state="ready", indexed_revision=target,
                    last_completed=datetime.now(timezone.utc).isoformat(), last_error=None)
        await self.save()

    def progress(self, records, chunks):
        done = sum(all(k in self.vectors for k in keys) for keys in chunks.values())
        done += len(records) - len(chunks)
        self.notify(completed_chunks=len(self.vectors), indexed_records=done)

    async def save(self):
        await self.store.async_save({"identity": self.identity, "vectors": self.vectors,
                                    "last_completed": self.status["last_completed"]})

    async def query(self, query):
        if not self.settings()["semantic_search"] or self.status["state"] != "ready":
            return None
        async with self.query_lock:
            settings, generation, target = self.settings(), self.generation, self.target
            key = revision([self.identity, query])
            try:
                vector = self.query_cache.get(key)
                if vector is None:
                    text = Redactor(settings["blocked_entities"]).text(query)
                    vector = (await self.request([text], settings))[0]
                if generation != self.generation or settings != self.settings() or target != self.target:
                    return None
                if self.vectors and len(vector) != len(next(iter(self.vectors.values()))):
                    self.vectors, self.target = {}, None
                    self.query_cache.clear()
                    await self.store.async_remove()
                    raise ValueError("Query vector dimensions differ from the index.")
                if len(self.query_cache) >= 128:
                    self.query_cache.pop(next(iter(self.query_cache)))
                self.query_cache[key] = vector
                return vector, target, generation
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError, KeyError):
                self.notify(state="error", last_error="Query embedding failed; using keyword search.")
                self.wake.set()
                return None

    def search(self, rows, query, prepared):
        if prepared and self.status["state"] == "ready":
            vector, target, generation = prepared
            if target == self.target and generation == self.generation:
                return hybrid_search(rows, query, vector, self.vectors,
                                     self.manager.options["embedding_min_similarity"]), f"hybrid:{target}"
        return ranked_search(rows, query), "keyword"
