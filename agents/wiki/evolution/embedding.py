"""Embedding 语义搜索 — 核心检索机制。

参考：projects/llm-wiki-memory/scripts/lib/wiki-search.mjs
实现：query embedding + leaf embedding + cosine similarity 排序
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

from agents.wiki.wiki_manager import get_wiki_dir, WikiEntry


DEFAULT_EMBEDDING_MODEL = "qwen3-embedding"
EMBEDDING_DIM = 1024
CACHE_DIR_NAME = ".embed-cache"


def get_embedding_model() -> str:
    """embedding 模型名（settings embed.model，默认 qwen3-embedding）。"""
    from agents.wiki.evolution.settings import get_setting
    return str(get_setting("embed.model", DEFAULT_EMBEDDING_MODEL))


def _resolve_model(model: str | None) -> str:
    return model or get_embedding_model()


def get_cache_dir() -> Path:
    """获取 embedding 缓存目录。"""
    d = get_wiki_dir() / CACHE_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_cache_path(model: str | None = None) -> Path:
    """获取缓存文件路径。"""
    return get_cache_dir() / f"{_resolve_model(model)}.json"


class EmbeddingCache:
    """内存缓存 — 启动时加载一次，后续直接从内存读取。"""
    
    _instance: EmbeddingCache | None = None
    _cache: dict[str, Any]
    _model: str
    _dirty: bool
    _load_time: float
    
    def __init__(self, model: str | None = None):
        self._model = _resolve_model(model)
        self._dirty = False
        self._load_time = 0.0
        self._load()
    
    @classmethod
    def get(cls, model: str | None = None) -> EmbeddingCache:
        resolved = _resolve_model(model)
        if cls._instance is None or cls._instance._model != resolved:
            cls._instance = cls(resolved)
        return cls._instance
    
    def _load(self) -> None:
        import time
        t0 = time.perf_counter()
        path = get_cache_path(self._model)
        if not path.exists():
            self._cache = {"model": self._model, "dim": EMBEDDING_DIM, "entries": {}}
            return
        try:
            data = json.loads(path.read_text())
            if data.get("model") != self._model:
                self._cache = {"model": self._model, "dim": EMBEDDING_DIM, "entries": {}}
            else:
                self._cache = data
        except (json.JSONDecodeError, KeyError):
            self._cache = {"model": self._model, "dim": EMBEDDING_DIM, "entries": {}}
        self._load_time = time.perf_counter() - t0
    
    def get_embedding(self, text: str) -> list[float] | None:
        h = content_hash(text)
        return self._cache["entries"].get(h)
    
    def set_embedding(self, text: str, embedding: list[float]) -> None:
        h = content_hash(text)
        self._cache["entries"][h] = embedding
        self._dirty = True
    
    def save(self) -> None:
        if not self._dirty:
            return
        path = get_cache_path(self._model)
        path.write_text(json.dumps(self._cache, ensure_ascii=False))
        self._dirty = False
    
    @property
    def size(self) -> int:
        return len(self._cache["entries"])


def get_cached_embedding(text: str, model: str | None = None) -> list[float] | None:
    """从缓存获取 embedding。"""
    return EmbeddingCache.get(model).get_embedding(text)


def set_cached_embedding(text: str, embedding: list[float], model: str | None = None) -> None:
    """缓存 embedding。"""
    cache = EmbeddingCache.get(model)
    cache.set_embedding(text, embedding)
    cache.save()


async def embed_text(text: str, model: str | None = None) -> list[float]:
    """生成文本的 embedding。

    模型/后端由 settings embed.model / embed.backend 配置，失败时重试。
    """
    from agents.wiki.evolution.settings import get_setting

    model = _resolve_model(model)
    cached = get_cached_embedding(text, model)
    if cached is not None:
        return cached

    backend = str(get_setting("embed.backend", "ollama"))
    if backend != "ollama":
        raise RuntimeError(f"unsupported embed.backend: {backend!r} (only 'ollama' is implemented)")

    embedding = await _call_ollama_embedding(text, model)
    set_cached_embedding(text, embedding, model)
    return embedding


async def _call_ollama_embedding(text: str, model: str) -> list[float]:
    """调用 ollama embedding API。"""
    import aiohttp

    url = "http://localhost:11434/api/embeddings"
    payload = {"model": model, "prompt": text}

    max_retries = 3
    for attempt in range(max_retries):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=30) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data["embedding"]
                    else:
                        raise RuntimeError(f"ollama returned {resp.status}")
        except Exception as e:
            if attempt == max_retries - 1:
                raise RuntimeError(f"ollama embedding failed after {max_retries} attempts: {e}")
            await asyncio.sleep(1)

    raise RuntimeError("ollama embedding failed")


async def embed_batch(texts: list[str], model: str | None = None) -> list[list[float]]:
    """批量生成 embeddings。"""
    model = _resolve_model(model)
    results: list[list[float] | None] = [None] * len(texts)
    to_embed: list[tuple[int, str]] = []

    for i, text in enumerate(texts):
        cached = get_cached_embedding(text, model)
        if cached is not None:
            results[i] = cached
        else:
            to_embed.append((i, text))

    if to_embed:
        tasks = [embed_text(text, model) for _, text in to_embed]
        embeddings = await asyncio.gather(*tasks, return_exceptions=True)

        for (i, _), emb in zip(to_embed, embeddings):
            if isinstance(emb, Exception):
                results[i] = [0.0] * EMBEDDING_DIM
            else:
                results[i] = emb

    return results


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """计算余弦相似度。"""
    if len(a) != len(b) or len(a) == 0:
        return 0.0

    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5

    if norm_a == 0 or norm_b == 0:
        return 0.0

    return dot / (norm_a * norm_b)


def embed_text_for_leaf(entry: WikiEntry) -> str:
    """构造 leaf 的 embedding 文本。

    参考：projects/llm-wiki-memory/scripts/lib/wiki-core.mjs:embedTextForLeaf
    格式：title · tags · subject + body
    """
    parts = []

    title = entry.name
    if title:
        parts.append(title)

    tags = entry.meta.get("tags", "")
    if tags:
        if isinstance(tags, list):
            parts.append(", ".join(tags))
        else:
            parts.append(str(tags))

    subject = entry.meta.get("subject", "")
    if subject:
        if isinstance(subject, list):
            parts.append(" / ".join(subject))
        else:
            parts.append(str(subject))

    description = entry.meta.get("description", "")
    if description:
        parts.append(description)

    header = " · ".join(parts)
    body = entry.content

    if header:
        return f"{header}\n\n{body}"
    return body


class ColdBudget:
    """Cold embedding 预算控制。

    参考：projects/llm-wiki-memory/scripts/lib/cold-budget.mjs
    限制单次请求中未缓存 embedding 的数量，防止延迟爆炸。
    """

    def __init__(self, max_cold: int = 50):
        self.max_cold = max_cold
        self.spent = 0
        self.skipped = 0

    def can_draw(self) -> bool:
        """是否可以执行一次 cold embedding。"""
        return self.spent < self.max_cold

    def draw(self) -> bool:
        """尝试执行一次 cold embedding，返回是否成功。"""
        if self.can_draw():
            self.spent += 1
            return True
        self.skipped += 1
        return False

    def is_cold_skip(self) -> bool:
        """当前是否因为预算耗尽而跳过。"""
        return self.spent >= self.max_cold


async def semantic_search(
    query: str,
    entries: list[WikiEntry],
    score_threshold: float = 0.1,
    max_results: int = 10,
    cold_budget: ColdBudget | None = None,
) -> list[tuple[WikiEntry, float]]:
    """语义搜索 — 用 embedding cosine similarity 排序。

    参考：projects/llm-wiki-memory/scripts/lib/wiki-search.mjs:searchOneTree
    """
    if not query or not entries:
        return []

    if cold_budget is None:
        cold_budget = ColdBudget()

    query_vec = await embed_text(query)

    scored: list[tuple[WikiEntry, float]] = []

    for entry in entries:
        embed_text_content = embed_text_for_leaf(entry)
        h = content_hash(embed_text_content)

        cached = get_cached_embedding(embed_text_content)
        if cached is not None:
            entry_vec = cached
        else:
            if not cold_budget.draw():
                continue
            try:
                entry_vec = await embed_text(embed_text_content)
            except Exception:
                continue

        score = cosine_similarity(query_vec, entry_vec)
        if score >= score_threshold:
            scored.append((entry, score))

    scored.sort(key=lambda x: -x[1])
    return scored[:max_results]


async def chunk_aware_search(
    query: str,
    entry: WikiEntry,
    chunk_size: int = 500,
    chunk_overlap: int = 50,
    penalty: float = 0.1,
) -> float:
    """Chunk-aware 评分 — 长文档分块取最高分（带 penalty）。

    参考：projects/llm-wiki-memory/scripts/lib/embed-chunk.mjs:scoreLeaf
    """
    query_vec = await embed_text(query)

    text = embed_text_for_leaf(entry)
    words = text.split()

    if len(words) <= chunk_size:
        entry_vec = await embed_text(text)
        return cosine_similarity(query_vec, entry_vec)

    chunks: list[str] = []
    for i in range(0, len(words), chunk_size - chunk_overlap):
        chunk = " ".join(words[i:i + chunk_size])
        chunks.append(chunk)

    if not chunks:
        return 0.0

    best_score = 0.0
    for chunk in chunks:
        chunk_vec = await embed_text(chunk)
        score = cosine_similarity(query_vec, chunk_vec)
        best_score = max(best_score, score)

    num_chunks = len(chunks)
    penalized_score = best_score - penalty * (num_chunks - 1) / num_chunks
    return max(0.0, penalized_score)
