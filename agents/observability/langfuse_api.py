"""Langfuse public API client."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agents.observability.tracer import get_langfuse_base_url


def load_langfuse_env(project_root: Path | None = None) -> None:
    root = project_root or Path(__file__).resolve().parent.parent.parent
    env_file = root / ".env"
    if not env_file.exists():
        return

    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"'))


def _parse_timestamp(value: str | datetime | None) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value

    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"

    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _model_to_dict(model: Any) -> dict[str, Any]:
    return model.model_dump(by_alias=True, exclude_none=True, mode="json")


class LangfuseApiClient:
    MAX_RETRIES = 4

    def __init__(
        self,
        public_key: str | None = None,
        secret_key: str | None = None,
        base_url: str | None = None,
        timeout: int = 30,
    ):
        self.public_key = public_key or os.environ.get("LANGFUSE_PUBLIC_KEY", "").strip()
        self.secret_key = secret_key or os.environ.get("LANGFUSE_SECRET_KEY", "").strip()
        self.base_url = (base_url or get_langfuse_base_url()).rstrip("/")
        self.timeout = timeout

        if not self.public_key or not self.secret_key:
            raise RuntimeError("LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY 未配置")

        from langfuse import Langfuse

        self._client = Langfuse(
            public_key=self.public_key,
            secret_key=self.secret_key,
            base_url=self.base_url,
            tracing_enabled=False,
            timeout=timeout,
        )
        self._request_options = {
            "max_retries": self.MAX_RETRIES,
            "timeout_in_seconds": timeout,
        }

    @property
    def api(self) -> Any:
        return self._client.api

    def fetch_traces(
        self,
        limit: int = 50,
        page: int = 1,
        name: str | None = None,
        session_id: str | None = None,
        from_timestamp: str | datetime | None = None,
    ) -> list[dict[str, Any]]:
        response = self.api.trace.list(
            page=page,
            limit=limit,
            name=name,
            session_id=session_id,
            from_timestamp=_parse_timestamp(from_timestamp),
            request_options=self._request_options,
        )
        return [_model_to_dict(trace) for trace in response.data]

    def fetch_trace(self, trace_id: str) -> dict[str, Any]:
        return _model_to_dict(self.api.trace.get(trace_id, request_options=self._request_options))

    def create_score(
        self,
        trace_id: str,
        name: str,
        value: float | bool | str,
        data_type: str | None = None,
        comment: str | None = None,
        observation_id: str | None = None,
    ) -> dict[str, Any]:
        from langfuse.api.commons.types.score_data_type import ScoreDataType

        if data_type is None:
            if isinstance(value, bool):
                data_type = "BOOLEAN"
            elif isinstance(value, (int, float)):
                data_type = "NUMERIC"
            else:
                data_type = "CATEGORICAL"

        score_value: float | str
        if data_type == "BOOLEAN" and isinstance(value, bool):
            score_value = 1.0 if value else 0.0
        elif data_type == "NUMERIC":
            score_value = float(value)
        else:
            score_value = str(value)

        response = self.api.scores.create(
            name=name,
            value=score_value,
            data_type=ScoreDataType(data_type),
            trace_id=trace_id,
            observation_id=observation_id,
            comment=comment[:1000] if comment else None,
            request_options=self._request_options,
        )
        return _model_to_dict(response)

    def create_dataset(self, name: str, description: str = "") -> dict[str, Any]:
        response = self.api.datasets.create(
            name=name,
            description=description or None,
            request_options=self._request_options,
        )
        return _model_to_dict(response)

    def upsert_dataset_item(
        self,
        dataset_name: str,
        input: Any,
        expected_output: Any = None,
        metadata: dict | None = None,
        item_id: str | None = None,
        source_trace_id: str | None = None,
    ) -> dict[str, Any]:
        response = self.api.dataset_items.create(
            dataset_name=dataset_name,
            input=input,
            expected_output=expected_output,
            metadata=metadata,
            id=item_id,
            source_trace_id=source_trace_id,
            request_options=self._request_options,
        )
        return _model_to_dict(response)

    def create_dataset_run_item(
        self,
        run_name: str,
        dataset_item_id: str,
        trace_id: str | None = None,
        observation_id: str | None = None,
        run_description: str | None = None,
        metadata: dict | None = None,
    ) -> dict[str, Any]:
        response = self.api.dataset_run_items.create(
            run_name=run_name,
            dataset_item_id=dataset_item_id,
            trace_id=trace_id,
            observation_id=observation_id,
            run_description=run_description,
            metadata=metadata,
            request_options=self._request_options,
        )
        return _model_to_dict(response)

    def fetch_dataset_runs(self, dataset_name: str, page: int = 1, limit: int = 50) -> list[dict[str, Any]]:
        response = self.api.datasets.get_runs(
            dataset_name,
            page=page,
            limit=limit,
            request_options=self._request_options,
        )
        return [_model_to_dict(run) for run in response.data]

    def fetch_project(self) -> dict[str, Any]:
        response = self.api.projects.get(request_options=self._request_options)
        projects = getattr(response, "data", []) or []
        if not projects:
            return {}
        return _model_to_dict(projects[0])


_client: LangfuseApiClient | None = None


def get_client() -> LangfuseApiClient:
    global _client

    if _client is None:
        load_langfuse_env()
        _client = LangfuseApiClient()
    return _client
