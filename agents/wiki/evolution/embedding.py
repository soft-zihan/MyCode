"""Embedding 语义搜索 — 核心检索机制。

参考：projects/llm-wiki-memory/scripts/lib/wiki-search.mjs
实现：query embedding + leaf embedding + cosine similarity 排序
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from agents.wiki.wiki_manager import WikiEntry


# 兜底零向量/缓存元信息用默认维度；真实维度以 API 返回向量为准
# （cosine_similarity 对维度不一致返回 0.0，天然防跨模型混算）
EMBEDDING_DIM = 1024


def content_hash(text: str) -> str:
    """计算内容哈希（embedding 缓存键）。"""
    return hashlib.sha256(text.encode()).hexdigest()


def get_embedding_model() -> str:
    """embedding 模型名（全局配置 ~/.my-code/config.json embedding.model，BC-30）。"""
    from agents.config import get_embedding_config
    return get_embedding_config().model


def _resolve_model(model: str | None) -> str:
    return model or get_embedding_model()


def get_cache_dir() -> Path:
    """embedding 缓存目录（全局，BC-30）。

    缓存键是内容 sha256、文件按模型分名，天然跨工作区安全共享；
    放全局（~/.mycode/embed-cache）使评测 wipe / 工作区清理不再导致
    全量冷启动——冷 embed 串行 + ollama 重试曾把召回推到 94s（预算 20s）。
    """
    d = Path.home() / ".mycode" / "embed-cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_cache_path(model: str | None = None) -> Path:
    """缓存文件路径（模型名消毒为合法文件名，如 BAAI/bge-... → BAAI_bge-...）。"""
    safe = re.sub(r"[^\w.\-]", "_", _resolve_model(model))
    return get_cache_dir() / f"{safe}.json"


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
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._cache, ensure_ascii=False))
        tmp.replace(path)
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
    """生成文本的 embedding（后端/模型/密钥来自全局配置，失败时重试）。"""
    from agents.config import get_embedding_config

    model = _resolve_model(model)
    cached = get_cached_embedding(text, model)
    if cached is not None:
        return cached

    cfg = get_embedding_config()
    if cfg.backend == "openai":
        embedding = await _call_openai_embedding(text, model)
    elif cfg.backend == "ollama":
        embedding = await _call_ollama_embedding(text, model)
    else:
        raise RuntimeError(f"unsupported embedding backend: {cfg.backend!r} (openai | ollama)")
    set_cached_embedding(text, embedding, model)
    return embedding


async def _call_openai_embedding(text: str, model: str) -> list[float]:
    """调用 OpenAI 兼容 /embeddings API（SiliconFlow 等，BC-30）。"""
    from agents.config import get_embedding_config

    cfg = get_embedding_config()
    url = f"{cfg.base_url.rstrip('/')}/embeddings"
    headers = {"Content-Type": "application/json"}
    if cfg.api_key:
        headers["Authorization"] = f"Bearer {cfg.api_key}"
    payload = {"model": model, "input": text}

    max_retries = 3
    async with _get_semaphore():
        for attempt in range(max_retries):
            try:
                session = _get_http_session()
                async with session.post(url, json=payload, headers=headers, timeout=30) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data["data"][0]["embedding"]
                    body = (await resp.text())[:200]
                    raise RuntimeError(f"embeddings API returned {resp.status}: {body}")
            except Exception as e:
                if attempt == max_retries - 1:
                    raise RuntimeError(f"embedding failed after {max_retries} attempts: {e}")
                await asyncio.sleep(1)

    raise RuntimeError("embedding failed")


# (loop, session, semaphore) 三元组：session/semaphore 均绑定事件循环，
# 换 loop（如线程内 asyncio.run）时自动重建，防 "attached to a different loop"
_http_state: tuple[Any, Any, Any] | None = None
# 限并发：compile/consolidate 批量 embed 不得挤占前台召回队列（BC-30）
_EMBED_CONCURRENCY = 4


def _get_http_session():
    global _http_state
    loop = asyncio.get_running_loop()
    if _http_state is None or _http_state[0] is not loop or _http_state[1].closed:
        import aiohttp
        _http_state = (loop, aiohttp.ClientSession(), asyncio.Semaphore(_EMBED_CONCURRENCY))
    return _http_state[1]


def _get_semaphore() -> asyncio.Semaphore:
    _get_http_session()
    assert _http_state is not None
    return _http_state[2]


async def _call_ollama_embedding(text: str, model: str) -> list[float]:
    """调用 ollama embedding API（共享 session + 全局限并发）。"""
    url = "http://localhost:11434/api/embeddings"
    payload = {"model": model, "prompt": text}

    max_retries = 3
    async with _get_semaphore():
        for attempt in range(max_retries):
            try:
                session = _get_http_session()
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
