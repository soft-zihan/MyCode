"""Langfuse 评估集成 — Trace 拉取 / Score 提交 / Dataset 回流

REST API 封装（requests + Basic Auth），供评估管道使用：
- Code evaluators：eval/langfuse/code_evaluators.py
- LLM-as-Judge：eval/langfuse/judge.py
- 失败案例回流：eval/langfuse/dataset_sync.py

密钥来源：环境变量 LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_BASE_URL
（生产由 start.sh 加载 .env；脚本可用 load_langfuse_env() 手动加载）
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import requests


def load_langfuse_env(project_root: Path | None = None) -> None:
    """从 .env 加载 Langfuse 密钥（已存在的环境变量优先）。"""
    root = project_root or Path(__file__).resolve().parent.parent.parent
    env_file = root / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"'))


class LangfuseClient:
    """Langfuse 公共 REST API 客户端（评估管道专用）。"""

    MAX_RETRIES = 4

    def __init__(
        self,
        public_key: str | None = None,
        secret_key: str | None = None,
        base_url: str | None = None,
        timeout: int = 30,
    ):
        self.public_key = public_key or os.environ.get("LANGFUSE_PUBLIC_KEY", "")
        self.secret_key = secret_key or os.environ.get("LANGFUSE_SECRET_KEY", "")
        self.base_url = (base_url or os.environ.get("LANGFUSE_BASE_URL", "")).rstrip("/")
        self.timeout = timeout
        if not (self.public_key and self.secret_key and self.base_url):
            raise RuntimeError(
                "LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_BASE_URL 未配置"
            )
        self._session = requests.Session()
        self._session.auth = (self.public_key, self.secret_key)

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        """带退避重试的请求（429/5xx 重试，尊重 Retry-After）。"""
        last_resp = None
        for attempt in range(self.MAX_RETRIES):
            resp = self._session.request(method, url, timeout=self.timeout, **kwargs)
            if resp.status_code != 429 and resp.status_code < 500:
                return resp
            last_resp = resp
            retry_after = resp.headers.get("Retry-After")
            wait = float(retry_after) if retry_after else (2 ** attempt)
            time.sleep(min(wait, 30))
        return last_resp

    # ── Traces ──────────────────────────────────────────────

    def fetch_traces(
        self,
        limit: int = 50,
        page: int = 1,
        name: str | None = None,
        session_id: str | None = None,
        from_timestamp: str | None = None,
    ) -> list[dict[str, Any]]:
        """拉取 trace 列表（不含 observations 详情）。"""
        params: dict[str, Any] = {"limit": limit, "page": page}
        if name:
            params["name"] = name
        if session_id:
            params["sessionId"] = session_id
        if from_timestamp:
            params["fromTimestamp"] = from_timestamp
        resp = self._request("GET", f"{self.base_url}/api/public/traces", params=params)
        resp.raise_for_status()
        return resp.json().get("data", [])

    def fetch_trace(self, trace_id: str) -> dict[str, Any]:
        """拉取单条 trace（响应内嵌完整 observations 列表，含 metadata.attributes）。"""
        resp = self._request("GET", f"{self.base_url}/api/public/traces/{trace_id}")
        resp.raise_for_status()
        return resp.json()

    # ── Scores ──────────────────────────────────────────────

    def create_score(
        self,
        trace_id: str,
        name: str,
        value: float | bool | str,
        data_type: str | None = None,
        comment: str | None = None,
        observation_id: str | None = None,
    ) -> dict[str, Any]:
        """提交评估分数（NUMERIC / BOOLEAN / CATEGORICAL）。

        data_type 缺省时按 value 类型推断。
        """
        if data_type is None:
            if isinstance(value, bool):
                data_type = "BOOLEAN"
            elif isinstance(value, (int, float)):
                data_type = "NUMERIC"
            else:
                data_type = "CATEGORICAL"
        # Langfuse BOOLEAN score 的 value 必须是数值（0/1），不接受 JSON bool
        if data_type == "BOOLEAN" and isinstance(value, bool):
            value = 1.0 if value else 0.0
        body: dict[str, Any] = {
            "traceId": trace_id,
            "name": name,
            "value": value,
            "dataType": data_type,
        }
        if comment:
            body["comment"] = comment[:1000]
        if observation_id:
            body["observationId"] = observation_id
        resp = self._request("POST", f"{self.base_url}/api/public/scores", json=body)
        resp.raise_for_status()
        return resp.json()

    # ── Datasets ────────────────────────────────────────────

    def create_dataset(self, name: str, description: str = "") -> dict[str, Any]:
        """创建 dataset（幂等：已存在时 Langfuse 返回现有对象）。"""
        body = {"name": name}
        if description:
            body["description"] = description
        resp = self._request("POST", f"{self.base_url}/api/public/v2/datasets", json=body)
        resp.raise_for_status()
        return resp.json()

    def upsert_dataset_item(
        self,
        dataset_name: str,
        input: Any,
        expected_output: Any = None,
        metadata: dict | None = None,
        item_id: str | None = None,
    ) -> dict[str, Any]:
        """写入/更新 dataset item（item_id 缺省时由 Langfuse 生成）。"""
        body: dict[str, Any] = {"datasetName": dataset_name, "input": input}
        if expected_output is not None:
            body["expectedOutput"] = expected_output
        if metadata is not None:
            body["metadata"] = metadata
        if item_id:
            body["id"] = item_id
        resp = self._request("POST", f"{self.base_url}/api/public/dataset-items", json=body)
        resp.raise_for_status()
        return resp.json()


_client: LangfuseClient | None = None


def get_client() -> LangfuseClient:
    """惰性单例（先尝试加载 .env）。"""
    global _client
    if _client is None:
        load_langfuse_env()
        _client = LangfuseClient()
    return _client
